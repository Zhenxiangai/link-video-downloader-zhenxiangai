#!/usr/bin/env python3
"""Restricted local archive worker for Hermes."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import html
import http.cookiejar
import ipaddress
import json
import mimetypes
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, HTTPCookieProcessor, ProxyHandler, Request, build_opener

sys.pycache_prefix = str(Path.home() / "Library" / "Caches" / "wechat-archive" / "pycache")

VERSION = "1.2.2"
TRANSPARENT_CORE_REVISION = "8c137bf1a56106a050f12567fe0ed587bccea042"
TRANSPARENT_CORE_SHA256 = "acccec7f474bfc605fe01113e2d06b28908c1602e877c5aa0985db39d6cb20d2"
CHANNELS_API_BASE = "http://127.0.0.1:2022"
CHANNEL_VIDEO_MEDIA_TYPE = 4
CHANNEL_VIDEO_FINALIZE_WAIT_SECONDS = 10.0
CHANNEL_VIDEO_FINALIZE_POLL_SECONDS = 0.25
ARTICLE_HOSTS = {"mp.weixin.qq.com"}
MEDIA_HOST_SUFFIXES = (".qpic.cn", ".qq.com", ".weixin.qq.com")
XHS_MEDIA_HOST_SUFFIXES = (".xhscdn.com", ".xhsimg.com")
CHANNEL_HOST_SUFFIXES = (".weixin.qq.com", ".video.qq.com")
MAX_HTML_BYTES = 8 * 1024 * 1024
MAX_MEDIA_BYTES = 20 * 1024 * 1024
MAX_MEDIA_ITEMS = 128
MAX_TOTAL_MEDIA_BYTES = 512 * 1024 * 1024
MAX_INPUT_MEDIA_BYTES = 1024 * 1024 * 1024
MAX_AUDIO_BYTES = 768 * 1024 * 1024
MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024
CHUNK_BYTES = 64 * 1024
SAFE_SUBPROCESS_ENV_KEYS = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL"}
UNAVAILABLE_PAGE_TITLES = {"账号已迁移", "内容已被删除", "此内容因违规无法查看"}
VERIFICATION_MARKERS = {"当前访问环境异常", "访问过于频繁", "请完成验证", "安全验证", "操作频繁", "验证码"}
JOB_ID_RE = re.compile(r"^(article|channel|media|content|batch)-\d{8}T\d{6}Z-[0-9a-f]{8}$")
INPUT_NAME_RE = re.compile(r"^[A-Za-z0-9._ -]{1,180}$")
SENSITIVE_QUERY_KEYS = {"key", "uin", "pass_ticket", "appmsg_token"}
OFFICIAL_ARTICLE_QUERY_KEYS = {"__biz", "mid", "idx", "sn"}
PLATFORM_DIRS = {
    "wechat_channels": "视频号",
    "wechat_official_account": "公众号",
    "bilibili": "B站",
    "xiaohongshu": "小红书",
    "douyin": "抖音",
}
PLATFORM_COOKIE_DOMAINS = {
    "bilibili": (".bilibili.com", ".b23.tv"),
    "xiaohongshu": (".xiaohongshu.com", ".xhslink.cn", ".xhscdn.com", ".xhsimg.com"),
    "douyin": (".douyin.com", ".iesdouyin.com", ".douyincdn.com", ".byteimg.com", ".bytedance.com"),
}


class ArchiveError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = 65):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def archive_root() -> Path:
    configured = os.environ.get("WECHAT_ARCHIVE_ROOT")
    return Path(configured).expanduser() if configured else Path.home() / "Documents" / "WeChatArchive"


def mp_api_token() -> str:
    path = Path(os.environ.get("WECHAT_MP_TOKEN_FILE") or Path.home() / ".local" / "share" / "wechat-archive" / "mp-api-token").expanduser()
    try:
        token = path.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError) as exc:
        raise ArchiveError("channels_backend_token_missing", "公众号本地服务凭证不可用，请重新运行安装。", 69) from exc
    if not token:
        raise ArchiveError("channels_backend_token_missing", "公众号本地服务凭证不可用，请重新运行安装。", 69)
    return token


def require_enabled() -> None:
    if os.environ.get("WECHAT_ARCHIVE_ENABLED", "").strip().lower() not in {"1", "true", "yes"}:
        raise ArchiveError("archive_disabled", "本地归档尚未启用。请先确认飞书访问策略。", 69)


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def private_atomic_write(path: Path, data: bytes) -> None:
    """Atomically write a user-private file without a permissive interval."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_json(path: Path, value: dict) -> None:
    private_atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def new_job(root: Path, kind: str, source: str) -> tuple[str, Path, dict]:
    job_id = f"{kind}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    jobs_dir = root / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(jobs_dir, 0o700)
    job_dir = jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(job_dir, 0o700)
    manifest = {
        "schema_version": 1,
        "tool_version": VERSION,
        "job_id": job_id,
        "kind": kind,
        "status": "running",
        "created_at": utc_now(),
        "source": source,
        "outputs": [],
    }
    write_json(job_dir / "manifest.json", manifest)
    return job_id, job_dir, manifest


def host_allowed(host: str, exact: set[str], suffixes: tuple[str, ...] = ()) -> bool:
    return host in exact or any(host.endswith(suffix) for suffix in suffixes)


def _split_url(raw: str):
    try:
        return urlsplit(raw.strip())
    except ValueError as exc:
        raise ArchiveError("invalid_url", "URL 格式无效。") from exc


def _safe_port(parsed) -> int | None:
    try:
        return parsed.port
    except ValueError as exc:
        raise ArchiveError("invalid_url", "URL 端口无效。") from exc


def validate_https_url(raw: str, exact_hosts: set[str], suffixes: tuple[str, ...] = ()) -> str:
    if not raw or len(raw) > 4096:
        raise ArchiveError("invalid_url", "URL 为空或过长。")
    parsed = _split_url(raw)
    host = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme != "https" or not host:
        raise ArchiveError("invalid_url", "只接受 HTTPS URL。")
    if parsed.username or parsed.password or _safe_port(parsed) not in (None, 443):
        raise ArchiveError("invalid_url", "URL 不得包含账号信息或非 443 端口。")
    if not host_allowed(host, exact_hosts, suffixes):
        raise ArchiveError("host_not_allowed", f"不允许访问主机：{host}")
    return urlunsplit(("https", parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))


def assert_public_dns(host: str) -> None:
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ArchiveError("dns_failed", f"无法解析主机：{host}", 69) from exc
    if not addresses:
        raise ArchiveError("dns_failed", f"主机没有可用地址：{host}", 69)
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if not ip.is_global:
            raise ArchiveError("unsafe_address", f"主机解析到非公网地址：{address}")


class SafeRedirectHandler(HTTPRedirectHandler):
    def __init__(self, exact_hosts: set[str], suffixes: tuple[str, ...]):
        super().__init__()
        self.exact_hosts = exact_hosts
        self.suffixes = suffixes

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        safe_url = validate_https_url(newurl, self.exact_hosts, self.suffixes)
        assert_public_dns(_split_url(safe_url).hostname or "")
        return super().redirect_request(req, fp, code, msg, headers, safe_url)


def validate_public_https_url(raw: str) -> str:
    parsed = _split_url(raw)
    host = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme != "https" or not host or parsed.username or parsed.password or _safe_port(parsed) not in (None, 443):
        raise ArchiveError("invalid_media_url", "媒体地址不是安全的公网 HTTPS URL。")
    assert_public_dns(host)
    return urlunsplit(("https", parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))


class PublicHTTPSRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return super().redirect_request(req, fp, code, msg, headers, validate_public_https_url(newurl))


def fetch_limited(
    raw_url: str,
    *,
    exact_hosts: set[str],
    suffixes: tuple[str, ...] = (),
    max_bytes: int,
) -> tuple[bytes, str, str]:
    safe_url = validate_https_url(raw_url, exact_hosts, suffixes)
    assert_public_dns(_split_url(safe_url).hostname or "")
    request = Request(safe_url, headers={"User-Agent": "Mozilla/5.0 WeChatArchive/0.1"})
    opener = build_opener(SafeRedirectHandler(exact_hosts, suffixes))
    last_error = None
    for _ in range(3):
        try:
            with opener.open(request, timeout=25) as response:
                length = response.headers.get("Content-Length")
                if length and int(length) > max_bytes:
                    raise ArchiveError("response_too_large", f"响应超过 {max_bytes} 字节。")
                chunks: list[bytes] = []
                size = 0
                while chunk := response.read(CHUNK_BYTES):
                    size += len(chunk)
                    if size > max_bytes:
                        raise ArchiveError("response_too_large", f"响应超过 {max_bytes} 字节。")
                    chunks.append(chunk)
                return b"".join(chunks), response.headers.get_content_type(), response.geturl()
        except ArchiveError:
            raise
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            last_error = exc
    raise ArchiveError("fetch_failed", f"下载失败：{last_error}", 69) from last_error


def fetch_official_article_with_session(raw_url: str) -> tuple[bytes, str, str]:
    safe_url = validate_https_url(raw_url, ARTICLE_HOSTS)
    endpoint = CHANNELS_API_BASE + "/api/mp/article/content?" + urlencode({"url": safe_url, "token": mp_api_token()})
    request = Request(endpoint, headers={"Accept": "text/html,application/xhtml+xml", "X-WXMP-Local-Client": "1"})
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=25) as response:
            content_type = response.headers.get_content_type()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                raise ArchiveError("official_article_session_invalid", "公众号会话后端未返回 HTML。", 69)
            length = response.headers.get("Content-Length")
            if length and int(length) > MAX_HTML_BYTES:
                raise ArchiveError("response_too_large", f"响应超过 {MAX_HTML_BYTES} 字节。")
            data = response.read(MAX_HTML_BYTES + 1)
            if len(data) > MAX_HTML_BYTES:
                raise ArchiveError("response_too_large", f"响应超过 {MAX_HTML_BYTES} 字节。")
            return data, content_type, safe_url
    except ArchiveError:
        raise
    except HTTPError as exc:
        if exc.code in {400, 401, 403}:
            raise ArchiveError("reauthentication_required", "公众号文章会话当前不可用。", 69) from exc
        raise ArchiveError("official_article_fetch_failed", "公众号正文读取失败。", 69) from exc
    except (URLError, TimeoutError, ValueError) as exc:
        raise ArchiveError("channels_backend_unavailable", "公众号本地会话后端不可用。", 69) from exc


def download_public_https(raw_url: str, target: Path, *, referer: str, max_bytes: int = 2 * 1024 * 1024 * 1024) -> None:
    safe_url = validate_public_https_url(raw_url)
    request = Request(safe_url, headers={"User-Agent": "Mozilla/5.0", "Referer": referer, "Range": "bytes=0-"})
    temporary = target.with_suffix(target.suffix + ".part")
    temporary.unlink(missing_ok=True)
    try:
        with build_opener(PublicHTTPSRedirectHandler()).open(request, timeout=30) as response, temporary.open("wb") as output:
            length = response.headers.get("Content-Length")
            if length and int(length) > max_bytes:
                raise ArchiveError("response_too_large", "视频超过允许大小。")
            size = 0
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise ArchiveError("response_too_large", "视频超过允许大小。")
                output.write(chunk)
            if length and size != int(length):
                raise ArchiveError("media_download_incomplete", "媒体下载不完整。", 69)
        if temporary.stat().st_size <= 0:
            raise ArchiveError("video_missing", "下载的视频为空。")
        os.replace(temporary, target)
    except (HTTPError, URLError, TimeoutError, OSError, ArchiveError) as exc:
        temporary.unlink(missing_ok=True)
        if isinstance(exc, ArchiveError):
            raise
        raise ArchiveError("media_download_failed", "媒体下载失败。", 69) from exc


class WeChatArticleParser(HTMLParser):
    BLOCK_TAGS = {"p", "div", "section", "h1", "h2", "h3", "h4", "li", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.author = ""
        self.description = ""
        self.content_depth = 0
        self.skip_depth = 0
        self.content_found = False
        self.text_parts: list[str] = []
        self.images: list[str] = []
        self.in_title = False
        self.title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        if tag == "meta":
            key = (values.get("property") or values.get("name") or "").lower()
            content = values.get("content", "").strip()
            if key in {"og:title", "twitter:title"} and content:
                self.title = content
            elif key in {"author", "og:article:author", "article:author"} and content:
                self.author = content
            elif key in {"description", "og:description"} and content:
                self.description = content
        if tag == "title":
            self.in_title = True
        if values.get("id") == "js_content" and self.content_depth == 0:
            self.content_depth = 1
            self.content_found = True
        elif self.content_depth:
            self.content_depth += 1
        if not self.content_depth:
            return
        if tag in {"script", "style"}:
            self.skip_depth += 1
            return
        if tag in self.BLOCK_TAGS or tag == "br":
            self.text_parts.append("\n")
        if tag == "img":
            source = values.get("data-src") or values.get("data-backsrc") or values.get("src")
            if source and source not in self.images:
                self.images.append(source)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if not self.content_depth:
            return
        if tag in {"script", "style"} and self.skip_depth:
            self.skip_depth -= 1
        elif tag in self.BLOCK_TAGS:
            self.text_parts.append("\n")
        self.content_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
        if self.content_depth and not self.skip_depth:
            cleaned = re.sub(r"\s+", " ", data).strip()
            if cleaned:
                self.text_parts.append(cleaned)

    def article_text(self) -> str:
        if not self.title:
            self.title = re.sub(r"\s+", " ", "".join(self.title_parts)).strip()
        joined = " ".join(self.text_parts)
        if not joined.strip() and self.description:
            joined = re.sub(r"\\x([0-9a-fA-F]{2})", lambda match: chr(int(match.group(1), 16)), self.description)
            joined = re.sub(r"<[^>]+>", "", html.unescape(joined))
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in joined.splitlines()]
        return "\n\n".join(line for line in lines if line)


def media_extension(content_type: str, url: str) -> str:
    guessed = mimetypes.guess_extension(content_type or "") if content_type else None
    if guessed in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
        return ".jpg" if guessed == ".jpeg" else guessed
    suffix = Path(urlsplit(url).path).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp"} else ".bin"


MediaFetcher = Callable[[str], tuple[bytes, str, str]]


def archive_article_html(
    url: str,
    html_bytes: bytes,
    root: Path,
    *,
    media_fetcher: MediaFetcher | None = None,
    job_context: tuple[str, Path, dict] | None = None,
) -> dict:
    safe_url = validate_https_url(url, ARTICLE_HOSTS)
    job_id, job_dir, manifest = job_context or new_job(root, "article", safe_url)
    manifest["source"] = safe_url
    try:
        original = job_dir / "original.html"
        atomic_write(original, html_bytes)
        parser = WeChatArticleParser()
        parser.feed(html_bytes.decode("utf-8", errors="replace"))
        body = parser.article_text()
        if parser.title in UNAVAILABLE_PAGE_TITLES:
            raise ArchiveError("article_unavailable", f"公众号页面不可用：{parser.title}")
        verification_text = f"{parser.title}\n{body}"
        verification_hits = sum(marker in verification_text for marker in VERIFICATION_MARKERS)
        generic_title = parser.title.strip() in {"微信公众平台", "WeChat", "微信"}
        if verification_hits >= 2 or (generic_title and verification_hits >= 1):
            raise ArchiveError("article_verification_required", "公众号返回了安全验证页面，未归档为正文。", 69)
        if len(body) < 20:
            raise ArchiveError("article_not_found", "页面中没有找到可归档的公众号正文。")

        downloaded: list[dict] = []
        failed_media: list[dict] = []
        total_media_bytes = 0
        if media_fetcher:
            for index, source in enumerate(parser.images, start=1):
                if index > MAX_MEDIA_ITEMS:
                    failed_media.append({"source": "[remaining images]", "error": "media_item_limit"})
                    break
                absolute = urljoin(safe_url, html.unescape(source))
                try:
                    safe_media = validate_https_url(absolute, set(), MEDIA_HOST_SUFFIXES)
                    data, content_type, final_url = media_fetcher(safe_media)
                    if total_media_bytes + len(data) > MAX_TOTAL_MEDIA_BYTES:
                        failed_media.append({"source": absolute, "error": "media_total_limit"})
                        break
                    total_media_bytes += len(data)
                    target = job_dir / "media" / f"{index:03d}{media_extension(content_type, final_url)}"
                    atomic_write(target, data)
                    downloaded.append(
                        {
                            "source": safe_media,
                            "path": str(target.relative_to(job_dir)),
                            "sha256": sha256_file(target),
                            "bytes": len(data),
                        }
                    )
                except ArchiveError as exc:
                    failed_media.append({"source": absolute, "error": exc.code})

        title = parser.title or "未命名公众号文章"
        markdown_lines = [f"# {title}", "", f"- 来源：{safe_url}", f"- 归档时间：{utc_now()}"]
        if job_context and manifest.get("published_at"):
            markdown_lines.append(f"- 发布日期：{manifest['published_at']}")
        if parser.author:
            markdown_lines.append(f"- 作者：{parser.author}")
        markdown_lines.extend(["", body, ""])
        if downloaded:
            markdown_lines.extend(["## 本地媒体", ""])
            markdown_lines.extend(f"![图片 {i}](./{item['path']})" for i, item in enumerate(downloaded, start=1))
            markdown_lines.append("")
        markdown = job_dir / "article.md"
        atomic_write(markdown, "\n".join(markdown_lines).encode("utf-8"))

        manifest.update(
            {
                "status": "completed",
                "completed_at": utc_now(),
                "title": title,
                "author": parser.author,
                "media": {"downloaded": downloaded, "failed": failed_media},
                "outputs": [
                    {"path": "original.html", "sha256": sha256_file(original)},
                    {"path": "article.md", "sha256": sha256_file(markdown)},
                ],
            }
        )
        write_json(job_dir / "manifest.json", manifest)
        return {"ok": True, "job_id": job_id, "status": "completed", "job_dir": str(job_dir), "title": title}
    except Exception as exc:
        manifest.update(
            {
                "status": "failed",
                "completed_at": utc_now(),
                "error": {"code": getattr(exc, "code", "unexpected_error"), "message": str(exc)},
            }
        )
        write_json(job_dir / "manifest.json", manifest)
        raise


def archive_article(url: str, root: Path) -> dict:
    safe_url = validate_https_url(url, ARTICLE_HOSTS)
    context = new_job(root, "article", safe_url)
    job_id, job_dir, manifest = context
    try:
        data, content_type, final_url = fetch_limited(safe_url, exact_hosts=ARTICLE_HOSTS, max_bytes=MAX_HTML_BYTES)
    except Exception as exc:
        manifest.update(
            {
                "status": "failed",
                "completed_at": utc_now(),
                "error": {"code": getattr(exc, "code", "unexpected_error"), "message": str(exc)},
            }
        )
        write_json(job_dir / "manifest.json", manifest)
        raise
    if content_type not in {"text/html", "application/xhtml+xml"}:
        error = ArchiveError("unexpected_content_type", f"文章响应类型不是 HTML：{content_type}")
        manifest.update({"status": "failed", "completed_at": utc_now(), "error": {"code": error.code, "message": str(error)}})
        write_json(job_dir / "manifest.json", manifest)
        raise error

    def fetch_media(media_url: str) -> tuple[bytes, str, str]:
        return fetch_limited(
            media_url,
            exact_hosts=set(),
            suffixes=MEDIA_HOST_SUFFIXES,
            max_bytes=MAX_MEDIA_BYTES,
        )

    return archive_article_html(final_url, data, root, media_fetcher=fetch_media, job_context=(job_id, job_dir, manifest))


def submission_url(raw: str) -> tuple[str, str]:
    if not raw or len(raw) > 4096:
        raise ArchiveError("invalid_url", "URL 为空或过长。")
    parsed = _split_url(raw)
    host = (parsed.hostname or "").rstrip(".").lower()
    port = _safe_port(parsed)
    if parsed.scheme not in {"http", "https"} or not host:
        raise ArchiveError("invalid_url", "只接受 HTTP(S) 分享链接。")
    if parsed.username or parsed.password or port not in (None, 80, 443):
        raise ArchiveError("invalid_url", "URL 不得包含账号信息或非标准端口。")
    if host == "mp.weixin.qq.com":
        platform = "wechat_official_account"
    elif host in {"weixin.qq.com", "channels.weixin.qq.com"} or host.endswith(".video.qq.com"):
        platform = "wechat_channels"
    elif host == "b23.tv" or host == "bilibili.com" or host.endswith(".bilibili.com"):
        platform = "bilibili"
    elif host == "xhslink.cn" or host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com"):
        platform = "xiaohongshu"
    elif host == "douyin.com" or host.endswith(".douyin.com"):
        platform = "douyin"
    else:
        raise ArchiveError("unsupported_url", f"暂不支持该链接主机：{host}")
    if parsed.scheme == "http" and host != "xhslink.cn":
        raise ArchiveError("invalid_url", "该平台只接受 HTTPS 分享链接。")
    netloc = host if port in (None, 80, 443) else parsed.netloc.lower()
    return urlunsplit(("https", netloc, parsed.path or "/", parsed.query, "")), platform


def canonical_url(raw: str) -> str:
    parsed = _split_url(raw)
    host = (parsed.hostname or "").rstrip(".").lower()
    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    if host == "mp.weixin.qq.com":
        query_items = [(key, value) for key, value in query_items if key in OFFICIAL_ARTICLE_QUERY_KEYS]
    else:
        query_items = [(key, value) for key, value in query_items if key.lower() not in SENSITIVE_QUERY_KEYS]
    query = urlencode(query_items)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", query, ""))


def stable_content_id(platform: str, url: str) -> str:
    return hashlib.sha256(f"{platform}\0{canonical_url(url)}".encode()).hexdigest()[:16]


def official_article_id(biz: str, mid: str, idx: str, sn: str) -> str:
    return hashlib.sha256(f"wechat_official_account\0{biz}\0{mid}\0{idx}\0{sn}".encode()).hexdigest()[:16]


def official_article_metadata(raw_url: str, html_bytes: bytes | None = None) -> dict:
    text = html_bytes.decode("utf-8", errors="replace") if html_bytes else ""

    def value(name: str) -> str:
        parsed = dict(parse_qsl(urlsplit(html.unescape(raw_url)).query, keep_blank_values=True))
        query_name = "__biz" if name == "biz" else name
        if parsed.get(query_name):
            return parsed[query_name]
        match = re.search(rf"var\s+{name}\s*=\s*['\"]([^'\"]+)['\"]", text)
        return html.unescape(match.group(1)) if match else ""

    biz, mid, idx, sn = (value(name) for name in ("biz", "mid", "idx", "sn"))
    if all((biz, mid, idx, sn)):
        article_url = urlunsplit(
            (
                "https",
                "mp.weixin.qq.com",
                "/s",
                urlencode({"__biz": biz, "mid": mid, "idx": idx, "sn": sn}),
                "",
            )
        )
        content_id = official_article_id(biz, mid, idx, sn)
    else:
        article_url = canonical_url(raw_url)
        content_id = stable_content_id("wechat_official_account", article_url)
    nickname = ""
    match = re.search(r"var\s+nickname\s*=\s*htmlDecode\(['\"]([^'\"]*)['\"]\)", text)
    if match:
        nickname = html.unescape(match.group(1))
    published_at = None
    publish_match = re.search(r"var\s+(?:ct|publish_time)\s*=\s*['\"]?(\d{10})['\"]?", text)
    if publish_match:
        published_at = datetime.fromtimestamp(int(publish_match.group(1)), timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "biz": biz,
        "account_id": hashlib.sha256(f"wechat_official_account\0{biz}".encode()).hexdigest()[:16] if biz else "",
        "account_name": nickname,
        "content_id": content_id,
        "canonical_url": article_url,
        "published_at": published_at,
    }


def archive_relative(root: Path, path: Path) -> str:
    return str(path.resolve().relative_to(root.resolve()))


def output_record(root: Path, path: Path, role: str) -> dict:
    return {"role": role, "path": archive_relative(root, path), "bytes": path.stat().st_size, "sha256": sha256_file(path)}


def safe_content_title(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[\\/:\x00-\x1f]", "_", value).strip(" .") or fallback
    while len(cleaned.encode("utf-8")) > 180:
        cleaned = cleaned[:-1]
    return cleaned or fallback


def submit_content(url: str, root: Path) -> dict:
    normalized, platform = submission_url(url)
    safe_source = canonical_url(normalized)
    job_id, job_dir, manifest = new_job(root, "content", safe_source)
    manifest.update(
        {
            "status": "queued",
            "updated_at": utc_now(),
            "platform": platform,
            "content_id": stable_content_id(platform, safe_source),
            "content_type": "article" if platform == "wechat_official_account" else "video",
            "canonical_url": safe_source,
            "route": None,
            "output_dir": None,
        }
    )
    if platform == "wechat_channels":
        manifest["auto_resume"] = True
    manifest_path = job_dir / "manifest.json"
    write_json(manifest_path, manifest)
    return {
        "ok": True,
        "job_id": job_id,
        "status": "queued",
        "platform": platform,
        "manifest": archive_relative(root, manifest_path),
    }


def frozen_channel_child_job_id(parent_job_id: str, item: dict) -> str:
    match = re.fullmatch(r"batch-(\d{8}T\d{6}Z)-[0-9a-f]{8}", parent_job_id)
    if not match:
        raise ArchiveError("invalid_job_id", "父任务 Job ID 格式错误。")
    identity = f"{parent_job_id}\0{item.get('id')}".encode()
    return f"content-{match.group(1)}-{hashlib.sha256(identity).hexdigest()[:8]}"


def submit_frozen_channel_content(item: dict, root: Path, parent_job_id: str) -> dict:
    normalized, platform = submission_url(str(item["url"]))
    if platform != "wechat_channels":
        raise ArchiveError("invalid_channels_url", "冻结的视频号子任务必须使用视频号链接。")
    safe_source = canonical_url(normalized)
    frozen = item.get("payload") or {}
    if not isinstance(frozen, dict) or not frozen:
        raise ArchiveError("channel_inventory_invalid", "冻结的视频号作品对象缺失。", 69)
    job_id = frozen_channel_child_job_id(parent_job_id, item)
    job_dir = root / "jobs" / job_id
    manifest_path = job_dir / "manifest.json"
    existing = read_json_if_valid(manifest_path) if manifest_path.is_file() else None
    if existing:
        if existing.get("parent_job_id") != parent_job_id or str(existing.get("content_id")) != str(item["id"]):
            raise ArchiveError("creator_child_identity_conflict", "冻结子任务身份冲突。", 69)
        return {
            "ok": True,
            "job_id": job_id,
            "status": existing.get("status"),
            "platform": "wechat_channels",
            "manifest": archive_relative(root, manifest_path),
        }
    job_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "tool_version": VERSION,
        "job_id": job_id,
        "kind": "content",
        "status": "staged",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "source": safe_source,
        "outputs": [],
        "platform": "wechat_channels",
        "content_id": str(item["id"]),
        "content_type": "video",
        "canonical_url": safe_source,
        "title": str(item["title"]),
        "route": "wechat_channels_backend",
        "output_dir": None,
        "auto_resume": True,
        "channel_object": frozen,
        "parent_job_id": parent_job_id,
    }
    write_json(manifest_path, manifest)
    return {
        "ok": True,
        "job_id": job_id,
        "status": "staged",
        "platform": "wechat_channels",
        "manifest": archive_relative(root, manifest_path),
    }


def activate_staged_channel_content(manifest_path: Path, parent_job_id: str) -> dict:
    lock_path = manifest_path.with_suffix(".json.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        manifest = read_json_if_valid(manifest_path) or {}
        if manifest.get("parent_job_id") != parent_job_id:
            raise ArchiveError("creator_child_identity_conflict", "冻结子任务父任务身份冲突。", 69)
        if manifest.get("status") == "staged":
            manifest.update({"status": "downloading", "updated_at": utc_now()})
            write_json(manifest_path, manifest)
        return manifest
    finally:
        os.close(descriptor)


def adopt_legacy_frozen_channel_content(
    manifest_path: Path,
    item: dict,
    parent_job_id: str,
) -> dict:
    lock_path = manifest_path.with_suffix(".json.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        manifest = read_json_if_valid(manifest_path) or {}
        frozen = item.get("payload") or {}
        if (
            manifest.get("platform") != "wechat_channels"
            or str(manifest.get("content_id") or "") != str(item.get("id") or "")
            or not isinstance(frozen, dict)
            or not frozen
        ):
            raise ArchiveError("creator_child_identity_conflict", "旧版冻结子任务身份冲突。", 69)
        existing_parent = manifest.get("parent_job_id")
        if existing_parent and existing_parent != parent_job_id:
            raise ArchiveError("creator_child_identity_conflict", "旧版冻结子任务父任务身份冲突。", 69)
        submitted = bool(manifest.get("upstream_task_ids"))
        manifest.update(
            {
                "parent_job_id": parent_job_id,
                "channel_object": frozen,
                "title": str(item.get("title") or manifest.get("title") or "未命名视频"),
                "route": "wechat_channels_backend",
                "status": manifest.get("status") if submitted else "downloading",
                "updated_at": utc_now(),
            }
        )
        write_json(manifest_path, manifest)
        return manifest
    finally:
        os.close(descriptor)


def submit_official_batch(url: str, root: Path) -> dict:
    normalized, platform = submission_url(url)
    if platform != "wechat_official_account":
        raise ArchiveError("invalid_official_account_url", "公众号历史批次只接受公众号文章链接。")
    job_id, job_dir, manifest = new_job(root, "batch", canonical_url(normalized))
    manifest.update(
        {
            "status": "queued",
            "updated_at": utc_now(),
            "platform": platform,
            "account": {"name": "", "account_id": ""},
            "pagination": {"pages": 0, "next_offset": 0, "can_continue": True, "complete": False},
            "counts": {
                "discovered": 0,
                "submitted": 0,
                "skipped_existing": 0,
                "processing": 0,
                "completed": 0,
                "unavailable": 0,
                "failed": 0,
            },
            "items": [],
        }
    )
    manifest_path = job_dir / "manifest.json"
    write_json(manifest_path, manifest)
    return {
        "ok": True,
        "job_id": job_id,
        "status": "queued",
        "platform": platform,
        "manifest": archive_relative(root, manifest_path),
    }


def finalize_official_article(manifest: dict, manifest_path: Path, root: Path, final_url: str) -> dict:
    job_dir = manifest_path.parent
    title = str(manifest.get("title") or "未命名公众号文章")
    content_id = str(manifest["content_id"])
    output_dir = root / "content" / PLATFORM_DIRS["wechat_official_account"] / f"{safe_content_title(title, '未命名公众号文章')}--{content_id}"
    image_dir = output_dir / "配图"
    output_dir.mkdir(parents=True, exist_ok=True)

    original = output_dir / "original.html"
    body = output_dir / "正文.md"
    os.replace(job_dir / "original.html", original)
    markdown = (job_dir / "article.md").read_text(encoding="utf-8").replace("./media/", "./配图/")
    atomic_write(body, markdown.encode())
    (job_dir / "article.md").unlink()

    outputs = [output_record(root, original, "original_html"), output_record(root, body, "body_markdown")]
    source_images = job_dir / "media"
    if source_images.is_dir():
        image_dir.mkdir(parents=True, exist_ok=True)
        for source in sorted(source_images.iterdir()):
            target = image_dir / source.name
            os.replace(source, target)
            outputs.append(output_record(root, target, "image"))
        source_images.rmdir()

    manifest.pop("media", None)
    manifest.pop("error", None)
    manifest.update(
        {
            "status": "completed",
            "updated_at": utc_now(),
            "completed_at": utc_now(),
            "platform": "wechat_official_account",
            "content_type": "article",
            "canonical_url": canonical_url(final_url),
            "route": "wechat_article_html",
            "output_dir": archive_relative(root, output_dir),
            "outputs": outputs,
        }
    )
    write_json(manifest_path, manifest)
    return manifest


def process_official_article(manifest: dict, manifest_path: Path, root: Path) -> dict:
    safe_url = validate_https_url(str(manifest["source"]), ARTICLE_HOSTS)
    data, content_type, final_url = fetch_official_article_with_session(safe_url)
    if content_type not in {"text/html", "application/xhtml+xml"}:
        raise ArchiveError("unexpected_content_type", f"文章响应类型不是 HTML：{content_type}")
    article = official_article_metadata(final_url, data)
    manifest.update({"content_id": article["content_id"], "canonical_url": article["canonical_url"]})
    if not manifest.get("published_at") and article.get("published_at"):
        manifest["published_at"] = article["published_at"]
    write_json(manifest_path, manifest)

    def fetch_media(media_url: str) -> tuple[bytes, str, str]:
        return fetch_limited(media_url, exact_hosts=set(), suffixes=MEDIA_HOST_SUFFIXES, max_bytes=MAX_MEDIA_BYTES)

    archive_article_html(
        article["canonical_url"],
        data,
        root,
        media_fetcher=fetch_media,
        job_context=(str(manifest["job_id"]), manifest_path.parent, manifest),
    )
    if (manifest.get("media") or {}).get("failed"):
        raise ArchiveError("article_media_incomplete", "公众号文章存在未取得配图，内容包未完成。", 69)
    return finalize_official_article(manifest, manifest_path, root, article["canonical_url"])

def _process_content_job_unlocked(
    manifest_path: Path,
    root: Path,
    deadline: float | None = None,
    start_only: bool = False,
) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") not in {"queued", "downloading", "transcribing"}:
        return manifest
    manifest.update({"status": "downloading", "updated_at": utc_now()})
    write_json(manifest_path, manifest)
    try:
        if manifest.get("platform") == "wechat_channels":
            if start_only and manifest.get("upstream_task_ids"):
                return manifest
            return process_channel_content(manifest, manifest_path, root, deadline=deadline)
        if manifest.get("platform") == "wechat_official_account":
            return process_official_article(manifest, manifest_path, root)
        if manifest.get("platform") == "bilibili":
            return process_transparent_video(manifest, manifest_path, root)
        if manifest.get("platform") == "xiaohongshu":
            return process_xiaohongshu(manifest, manifest_path, root)
        if manifest.get("platform") == "douyin":
            return process_transparent_video(manifest, manifest_path, root)
        raise ArchiveError("platform_not_implemented", f"平台尚未接入 worker：{manifest.get('platform')}", 69)
    except Exception as exc:
        channels_transient = isinstance(exc, ArchiveError) and exc.code in {
            "channels_backend_unavailable",
            "channels_backend_error",
            "channels_share_resolve_failed",
            "channels_task_create_failed",
        }
        recovery_expired = deadline is not None and isinstance(exc, ArchiveError) and exc.code == "recovery_window_expired"
        if isinstance(exc, ArchiveError) and (
            exc.code in {"reauthentication_required", "channels_authorization_required"}
            or channels_transient
            or recovery_expired
        ):
            platform = str(manifest.get("platform"))
            cookie_jar = platform_cookie_jar(platform)
            channels = platform == "wechat_channels"
            manifest.update(
                {
                    "status": "waiting_for_authorization" if channels or not cookie_jar.is_file() else "waiting_for_reauthentication",
                    "updated_at": utc_now(),
                    "next_action": (
                        (
                            "视频号会话当前不可用，原任务已保留。请等用户方便使用 Mac 时，由用户在 Mac 微信中手动打开下面的原链接并停留 10 秒；后台检测到会话恢复后会继续同一 Job：\n\n"
                            f"{manifest.get('source')}"
                        )
                        if channels
                        else "请在已授权的 Safari 或 Chrome 登录该平台后，导入持久 Cookie 并继续原任务。"
                    ),
                }
            )
            manifest.pop("completed_at", None)
            manifest.pop("failed_stage", None)
            manifest["last_retry_error"] = {"code": exc.code, "message": str(exc)}
            write_json(manifest_path, manifest)
            return manifest
        failed_stage = "transcription" if manifest.get("status") == "transcribing" else "downloading"
        manifest.update(
            {
                "status": "failed",
                "updated_at": utc_now(),
                "completed_at": utc_now(),
                "failed_stage": failed_stage,
                "error": {"code": getattr(exc, "code", "unexpected_error"), "message": str(exc)},
            }
        )
        write_json(manifest_path, manifest)
        return manifest

def process_content_job(
    manifest_path: Path,
    root: Path,
    resume_waiting: bool = False,
    resume_delivery: bool = False,
    deadline: float | None = None,
    start_only: bool = False,
) -> dict:
    lock_path = manifest_path.with_suffix(".json.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return read_json_if_valid(manifest_path) or {}
        if resume_waiting:
            manifest = read_json_if_valid(manifest_path) or {}
            if manifest.get("status") in {"waiting_for_authorization", "waiting_for_reauthentication"}:
                manifest.update({"status": "queued", "updated_at": utc_now()})
                manifest.pop("next_action", None)
                manifest.pop("error", None)
                write_json(manifest_path, manifest)
        if resume_delivery:
            manifest = read_json_if_valid(manifest_path) or {}
            recoverable = (
                manifest.get("platform") == "wechat_channels"
                and manifest.get("status") == "failed"
                and (manifest.get("error") or {}).get("code") == "channel_video_missing"
            )
            if not recoverable or len(channel_local_video_candidates(manifest, manifest_path, root)) != 1:
                return manifest
            manifest.update(
                {
                    "status": "downloading",
                    "updated_at": utc_now(),
                    "channel_delivery_recovery": True,
                }
            )
            manifest.pop("completed_at", None)
            manifest.pop("failed_stage", None)
            manifest.pop("error", None)
            write_json(manifest_path, manifest)
        return _process_content_job_unlocked(
            manifest_path,
            root,
            deadline=deadline,
            start_only=start_only,
        )
    finally:
        os.close(descriptor)


def parse_utc_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_json_if_valid(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def requeue_waiting_content_job(manifest_path: Path, frozen_object: dict | None = None) -> bool:
    lock_path = manifest_path.with_suffix(".json.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        manifest = read_json_if_valid(manifest_path) or {}
        if manifest.get("platform") != "wechat_channels" or manifest.get("status") not in {
            "waiting_for_authorization",
            "waiting_for_reauthentication",
        }:
            return False
        if not manifest.get("channel_object"):
            if not isinstance(frozen_object, dict) or not frozen_object:
                return False
            manifest["channel_object"] = frozen_object
        manifest.update({"status": "queued", "updated_at": utc_now()})
        manifest.pop("next_action", None)
        manifest.pop("error", None)
        write_json(manifest_path, manifest)
        return True
    finally:
        os.close(descriptor)


def _resume_creator_batch_children_unlocked(manifest: dict, manifest_path: Path, root: Path) -> tuple[dict, int]:
    resumed = 0
    items_by_child = {
        str(item.get("child_job_id")): item
        for item in ((manifest.get("inventory") or {}).get("items") or [])
        if item.get("child_job_id")
    }
    for child_id in manifest.get("child_job_ids") or []:
        child_path = root / "jobs" / str(child_id) / "manifest.json"
        item = items_by_child.get(str(child_id)) or {}
        frozen = item.get("payload") if isinstance(item, dict) else None
        if requeue_waiting_content_job(child_path, frozen_object=frozen):
            resumed += 1
    if resumed:
        manifest.pop("next_action", None)
        manifest.pop("error", None)
        manifest.pop("session_retry_after", None)
    return _refresh_creator_batch_unlocked(manifest, manifest_path, root), resumed


def resume_creator_batch_children(manifest: dict, manifest_path: Path, root: Path) -> tuple[dict, int]:
    descriptor = acquire_creator_batch_lock(manifest_path)
    if descriptor is None:
        return read_json_if_valid(manifest_path) or manifest, 0
    try:
        current = read_json_if_valid(manifest_path) or manifest
        return _resume_creator_batch_children_unlocked(current, manifest_path, root)
    finally:
        os.close(descriptor)


def _resume_waiting_channel_creators_unlocked(
    root: Path,
    retry_interval: int = 900,
    ignore_retry_after: bool = False,
    deadline: float | None = None,
) -> int:
    """Low-frequency, non-UI retry for the waiting creator backlog.

    The retry only calls the existing local Channels backend. It never opens or
    controls WeChat. When a short realtime window becomes available, every due
    task is advanced in the same pass. Probing stops as soon as that realtime
    window is unavailable again.
    """
    if retry_interval < 60:
        raise ArchiveError("invalid_retry_interval", "视频号会话重试间隔不能小于 60 秒。")
    now = datetime.now(timezone.utc)
    resumed_total = 0
    manifest_paths = list((root / "jobs").glob("batch-????????T??????Z-????????/manifest.json"))

    def recovery_priority(path: Path) -> tuple[int, str]:
        candidate = read_json_if_valid(path) or {}
        frozen = bool(candidate.get("selection") or candidate.get("child_job_ids"))
        return (0 if frozen else 1, str(path))

    for manifest_path in sorted(manifest_paths, key=recovery_priority):
        if deadline is not None and time.monotonic() >= deadline:
            break
        descriptor = acquire_creator_batch_lock(manifest_path)
        if descriptor is None:
            continue
        try:
            manifest = read_json_if_valid(manifest_path)
            if not manifest:
                continue
            if (
                manifest.get("kind") != "creator_batch"
                or manifest.get("platform") != "wechat_channels"
                or manifest.get("status") not in {"waiting_for_authorization", "waiting_for_reauthentication"}
            ):
                continue
            retry_after = parse_utc_timestamp(manifest.get("session_retry_after"))
            if not ignore_retry_after and retry_after is not None and retry_after > now:
                continue
            source = str(manifest.get("source") or "")
            selected = bool(manifest.get("selection") or manifest.get("child_job_ids"))
            if not selected and not source:
                continue
            next_retry = (now + timedelta(seconds=retry_interval)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            try:
                manifest["session_retry_after"] = next_retry
                write_json(manifest_path, manifest)
                if selected:
                    _, resumed_children = _resume_creator_batch_children_unlocked(manifest, manifest_path, root)
                    if resumed_children:
                        resumed_total += 1
                    continue
                result = _inspect_channel_creator_unlocked(
                    source,
                    root,
                    existing_job=(str(manifest.get("job_id") or manifest_path.parent.name), manifest_path.parent, manifest),
                    deadline=deadline,
                )
            except Exception as exc:
                try:
                    refreshed = read_json_if_valid(manifest_path) or manifest
                    refreshed["session_retry_after"] = next_retry
                    refreshed["last_retry_error"] = {
                        "code": getattr(exc, "code", "unexpected_error"),
                        "message": str(exc),
                    }
                    write_json(manifest_path, refreshed)
                except OSError:
                    pass
                return resumed_total
            if result.get("status") in {
                "awaiting_download_count",
                "submitting",
                "processing",
                "completed",
                "completed_with_failures",
            }:
                resumed_total += 1
                continue
            refreshed = read_json_if_valid(manifest_path) or manifest
            refreshed["session_retry_after"] = next_retry
            write_json(manifest_path, refreshed)
            return resumed_total
        finally:
            os.close(descriptor)
    return resumed_total


def resume_waiting_channel_creators_once(
    root: Path,
    retry_interval: int = 900,
    ignore_retry_after: bool = False,
    deadline: float | None = None,
) -> int:
    lock_path = root / "state" / "channels-recovery.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(lock_path.parent, 0o700)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if ignore_retry_after:
                raise ArchiveError("recovery_window_busy", "已有视频号恢复流程正在处理任务，请稍后重试。", 75)
            return 0
        return _resume_waiting_channel_creators_unlocked(root, retry_interval, ignore_retry_after, deadline)
    finally:
        os.close(descriptor)


def resume_waiting_channel_content(root: Path, deadline: float | None = None) -> int:
    resumed = 0
    pattern = "content-????????T??????Z-????????/manifest.json"
    for manifest_path in sorted((root / "jobs").glob(pattern)):
        if deadline is not None and time.monotonic() >= deadline:
            break
        manifest = read_json_if_valid(manifest_path)
        if not manifest or manifest.get("platform") != "wechat_channels":
            continue
        waiting = manifest.get("status") in {"waiting_for_authorization", "waiting_for_reauthentication"}
        frozen_unsubmitted = (
            manifest.get("status") in {"queued", "downloading"}
            and bool(manifest.get("channel_object"))
            and not manifest.get("upstream_task_ids")
        )
        if not waiting and not frozen_unsubmitted:
            continue
        result = process_content_job(
            manifest_path,
            root,
            resume_waiting=True,
            deadline=deadline,
            start_only=True,
        )
        if result.get("status") in {"downloading", "transcribing", "completed"}:
            resumed += 1
    return resumed


def recover_channel_session(
    root: Path,
    timeout: int = 300,
    poll_interval: int = 5,
    started_at: float | None = None,
    cleanup_reserve: int = 0,
) -> dict:
    """Wait for one user-opened Channels page, then drain the waiting backlog."""
    if timeout < 10 or timeout > 900:
        raise ArchiveError("invalid_recovery_timeout", "视频号恢复窗口必须在 10 到 900 秒之间。")
    if poll_interval < 1 or poll_interval > 10:
        raise ArchiveError("invalid_recovery_poll_interval", "视频号恢复检测间隔必须在 1 到 10 秒之间。")
    if cleanup_reserve < 0 or cleanup_reserve >= timeout:
        raise ArchiveError("invalid_cleanup_reserve", "恢复窗口清理预留时间无效。")
    elapsed = max(0.0, time.time() - started_at) if started_at is not None else 0.0
    remaining_budget = timeout - cleanup_reserve - elapsed
    if remaining_budget <= 0:
        return {
            "ok": False,
            "platform": "wechat_channels",
            "status": "recovery_window_expired",
            "resumed_creator_batches": 0,
            "resumed_content_jobs": 0,
        }
    deadline = time.monotonic() + remaining_budget
    probe_keyword = f"__hermes_session_probe_{uuid.uuid4().hex}__"
    while True:
        status = channel_session_status(root, probe_keyword=probe_keyword, deadline=deadline)
        if status.get("ok"):
            if time.monotonic() >= deadline:
                return {
                    "ok": False,
                    "platform": "wechat_channels",
                    "status": "recovery_window_expired",
                    "resumed_creator_batches": 0,
                    "resumed_content_jobs": 0,
                }
            resumed_batches = resume_waiting_channel_creators_once(
                root,
                retry_interval=60,
                ignore_retry_after=True,
                deadline=deadline,
            )
            resumed_content = resume_waiting_channel_content(root, deadline=deadline)
            if time.monotonic() >= deadline:
                return {
                    "ok": False,
                    "platform": "wechat_channels",
                    "status": "recovery_window_expired",
                    "resumed_creator_batches": resumed_batches,
                    "resumed_content_jobs": resumed_content,
                }
            return {
                "ok": True,
                "platform": "wechat_channels",
                "status": "realtime_window_ready",
                "resumed_creator_batches": resumed_batches,
                "resumed_content_jobs": resumed_content,
                "last_realtime_ready_at": status.get("last_realtime_ready_at"),
            }
        if time.monotonic() >= deadline:
            return {
                "ok": False,
                "platform": "wechat_channels",
                "status": "recovery_window_expired",
                "resumed_creator_batches": 0,
                "resumed_content_jobs": 0,
            }
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            continue
        time.sleep(min(poll_interval, remaining))


def refresh_creator_parent_for_child(child: dict, root: Path) -> bool:
    if child.get("status") != "completed":
        return False
    parent_id = str(child.get("parent_job_id") or "")
    if not JOB_ID_RE.fullmatch(parent_id) or not parent_id.startswith("batch-"):
        return False
    parent_path = root / "jobs" / parent_id / "manifest.json"
    parent = read_json_if_valid(parent_path)
    if not parent or parent.get("kind") != "creator_batch" or parent.get("platform") != "wechat_channels":
        return False
    refreshed = refresh_creator_batch(parent, parent_path, root)
    return refreshed.get("status") == "completed"


def reconcile_terminal_creator_batches_once(root: Path) -> int:
    jobs_root = root / "jobs"
    for parent_path in sorted(jobs_root.glob("batch-????????T??????Z-????????/manifest.json")):
        parent = read_json_if_valid(parent_path)
        if not parent or parent.get("kind") != "creator_batch" or parent.get("status") != "completed_with_failures":
            continue
        needs_refresh = False
        for item in (parent.get("inventory") or {}).get("items") or []:
            if item.get("error_code") != "channel_video_missing" or not item.get("child_job_id"):
                continue
            child_path = jobs_root / str(item["child_job_id"]) / "manifest.json"
            child = read_json_if_valid(child_path)
            if child and child.get("status") == "completed":
                needs_refresh = True
                break
        if not needs_refresh:
            continue
        refreshed = refresh_creator_batch(parent, parent_path, root)
        if refreshed.get("status") == "completed":
            return 1
    return 0


def content_worker_once(root: Path) -> tuple[dict, bool]:
    jobs_root = root / "jobs"
    jobs_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(jobs_root, 0o700)
    resumed_creators = resume_waiting_channel_creators_once(root)
    reconciled_creators = reconcile_terminal_creator_batches_once(root)
    progress_channel_jobs_once(root)
    for manifest_path in sorted(jobs_root.glob("batch-????????T??????Z-????????/manifest.json")):
        manifest = read_json_if_valid(manifest_path)
        if not manifest:
            continue
        if manifest.get("kind") == "creator_batch" and manifest.get("status") in {
            "submitting",
            "processing",
            "waiting_for_authorization",
            "waiting_for_reauthentication",
        }:
            if manifest.get("status") == "submitting":
                submit_creator_batch_children(manifest, manifest_path, root)
            else:
                refresh_creator_batch(manifest, manifest_path, root)
    processed = bool(resumed_creators or reconciled_creators)
    for manifest_path in sorted(jobs_root.glob("content-????????T??????Z-????????/manifest.json")):
        manifest = read_json_if_valid(manifest_path)
        if not manifest:
            continue
        if manifest.get("status") in {"queued", "downloading", "transcribing"}:
            process_content_job(manifest_path, root)
            processed = True
            break
    if not processed:
        for manifest_path in sorted(jobs_root.glob("content-????????T??????Z-????????/manifest.json")):
            manifest = read_json_if_valid(manifest_path)
            recoverable = (
                manifest
                and manifest.get("platform") == "wechat_channels"
                and manifest.get("status") == "failed"
                and (manifest.get("error") or {}).get("code") == "channel_video_missing"
                and len(channel_local_video_candidates(manifest, manifest_path, root)) == 1
            )
            if not recoverable:
                continue
            recovered = process_content_job(manifest_path, root, resume_delivery=True)
            refresh_creator_parent_for_child(recovered, root)
            processed = True
            break
    if not processed:
        actionable_batch_states = {"queued", "discovering", "processing"}
        waiting_batch_states = {"waiting_for_authorization", "waiting_for_reauthentication"}
        official_batches: list[tuple[int, Path]] = []
        for manifest_path in jobs_root.glob("batch-????????T??????Z-????????/manifest.json"):
            manifest = read_json_if_valid(manifest_path)
            if not manifest or manifest.get("kind") != "batch":
                continue
            status = manifest.get("status")
            if status in actionable_batch_states:
                official_batches.append((0, manifest_path))
            elif status in waiting_batch_states:
                official_batches.append((1, manifest_path))
        if official_batches:
            _, manifest_path = min(official_batches, key=lambda item: (item[0], item[1].name))
            process_official_batch(manifest_path, root)
            processed = True
    statuses = []
    for pattern in ("content-????????T??????Z-????????/manifest.json", "batch-????????T??????Z-????????/manifest.json"):
        for manifest_path in jobs_root.glob(pattern):
            manifest = read_json_if_valid(manifest_path)
            if manifest:
                statuses.append(manifest.get("status"))
    counts = {status: statuses.count(status) for status in sorted(set(statuses)) if status}
    worker = {
        "schema_version": 1,
        "tool_version": VERSION,
        "kind": "content_worker",
        "status": "running",
        "pid": os.getpid(),
        "updated_at": utc_now(),
        "counts": {"total": len(statuses), **counts},
    }
    write_json(jobs_root / "content-worker" / "manifest.json", worker)
    return worker, processed


def watch_content(root: Path, interval: int, once: bool) -> dict:
    if interval < 1:
        raise ArchiveError("invalid_interval", "轮询间隔必须至少为 1 秒。")
    while True:
        worker, _ = content_worker_once(root)
        if once:
            worker["status"] = "stopped"
            write_json(root / "jobs" / "content-worker" / "manifest.json", worker)
            return {"ok": True, "manifest": "jobs/content-worker/manifest.json", "counts": worker["counts"]}
        time.sleep(interval)


def content_worker_status(root: Path) -> dict:
    manifest_path = root / "jobs" / "content-worker" / "manifest.json"
    if not manifest_path.is_file():
        raise ArchiveError("content_worker_not_started", "内容 worker 尚未生成 manifest。", 66)
    worker = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {"ok": True, "worker": worker, "manifest": "jobs/content-worker/manifest.json"}


def transparent_core_sha256(root: Path) -> str:
    if not root.is_dir() or root.is_symlink():
        return ""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            return ""
        if not path.is_file():
            continue
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(CHUNK_BYTES), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def transparent_core_root() -> Path:
    bundled = Path(__file__).resolve().parents[1] / "vendor" / "transparent-core"
    installed = Path.home() / ".local" / "share" / "wechat-archive" / "transparent-core" / TRANSPARENT_CORE_REVISION
    for core in (bundled, installed):
        if not core.exists():
            continue
        if transparent_core_sha256(core) != TRANSPARENT_CORE_SHA256:
            raise ArchiveError("transparent_core_invalid", f"透明派生核心校验失败：{core}", 69)
        return core
    raise ArchiveError("transparent_core_missing", "透明派生核心未安装。请重新运行 bootstrap.sh install。", 69)


def platform_cookie_jar(platform: str) -> Path:
    return Path.home() / "Library" / "Application Support" / "wechat-archive" / "cookies" / f"{platform}.txt"


class SilentCookieLogger:
    def debug(self, message: str) -> None:
        pass

    info = debug
    warning = debug
    error = debug


def transparent_core_needs_login(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in ("cookie", "login", "fresh"))


def import_browser_cookies(platform: str, browser: str, root: Path) -> dict:
    domains = PLATFORM_COOKIE_DOMAINS.get(platform)
    if not domains:
        raise ArchiveError("invalid_platform", "Cookie 导入只支持 B站、小红书和抖音。")
    core = transparent_core_root()
    sys.path.insert(0, str(core))
    try:
        from yt_dlp.cookies import YoutubeDLCookieJar, extract_cookies_from_browser  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ArchiveError("transparent_core_invalid", "透明派生核心无法加载 Cookie 模块。", 69) from exc
    try:
        browser_jar = extract_cookies_from_browser(browser, logger=SilentCookieLogger())
    except Exception as exc:
        raise ArchiveError("cookie_import_failed", "无法从浏览器导入 Cookie；请确认已登录并允许访问。", 69) from exc
    target = platform_cookie_jar(platform)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    temporary = target.with_suffix(".tmp")
    filtered = YoutubeDLCookieJar(str(temporary))
    for cookie in browser_jar:
        domain = cookie.domain.lower()
        if any(domain == suffix.lstrip(".") or domain.endswith(suffix) for suffix in domains):
            filtered.set_cookie(cookie)
    if not len(filtered):
        raise ArchiveError("platform_cookies_missing", "所选浏览器中没有找到该平台的登录 Cookie。", 69)
    filtered.save()
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)

    resumed = 0
    for manifest_path in (root / "jobs").glob("content-????????T??????Z-????????/manifest.json"):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("platform") == platform and manifest.get("status") in {"waiting_for_authorization", "waiting_for_reauthentication"}:
            manifest.update({"status": "queued", "updated_at": utc_now()})
            manifest.pop("next_action", None)
            manifest.pop("error", None)
            write_json(manifest_path, manifest)
            resumed += 1
    for manifest_path in (root / "jobs").glob("batch-????????T??????Z-????????/manifest.json"):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("kind") == "creator_batch" and manifest.get("platform") == platform and manifest.get("status") == "waiting_for_reauthentication":
            manifest.update({"status": "processing", "updated_at": utc_now()})
            manifest.pop("next_action", None)
            write_json(manifest_path, manifest)
    return {"ok": True, "platform": platform, "browser": browser, "status": "imported", "cookie_count": len(filtered), "resumed_jobs": resumed}


def resume_job(job_id: str, root: Path) -> dict:
    if not JOB_ID_RE.fullmatch(job_id):
        raise ArchiveError("invalid_job_id", "Job ID 格式错误。")
    manifest_path = root / "jobs" / job_id / "manifest.json"
    if not manifest_path.is_file():
        raise ArchiveError("job_not_found", "没有找到该任务。", 66)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("platform") != "wechat_channels":
        raise ArchiveError("job_not_resumable", "只有视频号授权等待任务可以手动恢复。", 66)
    if manifest.get("status") not in {"waiting_for_authorization", "waiting_for_reauthentication"}:
        raise ArchiveError("job_not_waiting", "该任务当前不在授权等待状态。", 66)

    if manifest.get("kind") == "creator_batch":
        if manifest.get("selection") or manifest.get("child_job_ids"):
            manifest, _ = resume_creator_batch_children(manifest, manifest_path, root)
            return {
                "ok": manifest.get("status") != "failed",
                "job_id": job_id,
                "status": manifest["status"],
                "platform": manifest.get("platform"),
            }
        source = str(manifest.get("source") or "")
        if not source:
            raise ArchiveError("job_source_missing", "任务缺少原始视频号链接。", 70)
        result = inspect_channel_creator(source, root, existing_job=(job_id, manifest_path.parent, manifest))
        return result

    if manifest.get("kind") != "content":
        raise ArchiveError("job_not_resumable", "该视频号任务类型不支持手动恢复。", 66)
    manifest = process_content_job(manifest_path, root, resume_waiting=True)
    return {"ok": manifest.get("status") != "failed", "job_id": job_id, "status": manifest["status"], "platform": manifest.get("platform")}


def resolve_transparent_core_url(url: str, platform: str) -> str:
    host = (_split_url(url).hostname or "").lower()
    if platform == "bilibili" and host == "b23.tv":
        exact_hosts, suffixes = {"b23.tv", "bilibili.com"}, (".bilibili.com",)
    elif platform == "xiaohongshu" and host == "xhslink.cn":
        exact_hosts, suffixes = {"xhslink.cn", "xiaohongshu.com"}, (".xiaohongshu.com",)
    elif platform == "douyin" and host == "v.douyin.com":
        exact_hosts, suffixes = {"v.douyin.com", "douyin.com"}, (".douyin.com", ".iesdouyin.com")
    else:
        return url
    try:
        _, _, final_url = fetch_limited(url, exact_hosts=exact_hosts, suffixes=suffixes, max_bytes=8 * 1024 * 1024)
        return final_url
    except ArchiveError:
        if platform != "bilibili":
            raise
        request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with build_opener(SafeRedirectHandler(exact_hosts, suffixes)).open(request, timeout=25) as response:
                return response.geturl()
        except HTTPError as exc:
            final_url = exc.geturl()
            parsed = _split_url(final_url)
            host = (parsed.hostname or "").lower()
            if host_allowed(host, exact_hosts, suffixes) and re.search(r"/video/BV[0-9A-Za-z]{10}", parsed.path):
                return final_url
            raise ArchiveError("fetch_failed", f"下载失败：{exc}", 69) from exc
        except (URLError, TimeoutError, ValueError) as exc:
            raise ArchiveError("fetch_failed", f"下载失败：{exc}", 69) from exc


def download_with_transparent_core(url: str, platform: str, work_dir: Path) -> tuple[dict, Path]:
    url = resolve_transparent_core_url(url, platform)
    core = transparent_core_root()
    sys.path.insert(0, str(core))
    try:
        from yt_dlp import YoutubeDL  # type: ignore[import-not-found]
        from yt_dlp.utils import DownloadError  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ArchiveError("transparent_core_invalid", "透明派生核心无法加载。", 69) from exc

    work_dir.mkdir(parents=True, exist_ok=True)
    cookie_jar = platform_cookie_jar(platform)
    options = {
        "cachedir": False,
        "continuedl": True,
        "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
        "fragment_retries": 3,
        "ffmpeg_location": require_executable("WECHAT_FFMPEG", "ffmpeg"),
        "merge_output_format": "mp4",
        "logger": SilentCookieLogger(),
        "noplaylist": True,
        "noprogress": True,
        "outtmpl": str(work_dir / "source.%(ext)s"),
        "overwrites": True,
        "quiet": True,
        "retries": 3,
        "socket_timeout": 30,
        "no_warnings": True,
    }
    if cookie_jar.is_file():
        options["cookiefile"] = str(cookie_jar)
    try:
        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=True)
    except DownloadError as exc:
        if transparent_core_needs_login(exc):
            raise ArchiveError("reauthentication_required", "平台登录态缺失或已失效。", 69) from exc
        raise ArchiveError("transparent_core_failed", "透明派生核心提取失败。", 69) from exc
    candidates = [
        path
        for path in work_dir.glob("source.*")
        if path.is_file() and path.suffix.lower() not in {".part", ".ytdl", ".json"}
    ]
    if not candidates:
        raise ArchiveError("video_missing", "提取核心完成后没有找到视频文件。", 69)
    return info, max(candidates, key=lambda path: path.stat().st_size)


def inspect_with_transparent_core(url: str, platform: str) -> dict:
    url = resolve_transparent_core_url(url, platform)
    core = transparent_core_root()
    sys.path.insert(0, str(core))
    try:
        from yt_dlp import YoutubeDL  # type: ignore[import-not-found]
        from yt_dlp.utils import DownloadError  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ArchiveError("transparent_core_invalid", "透明派生核心无法加载。", 69) from exc
    options = {"cachedir": False, "quiet": True, "no_warnings": True, "logger": SilentCookieLogger()}
    cookie_jar = platform_cookie_jar(platform)
    if cookie_jar.is_file():
        options["cookiefile"] = str(cookie_jar)
    try:
        with YoutubeDL(options) as downloader:
            return downloader.extract_info(url, download=False, process=False)
    except DownloadError as exc:
        if transparent_core_needs_login(exc):
            raise ArchiveError("reauthentication_required", "平台登录态缺失或已失效。", 69) from exc
        raise ArchiveError("transparent_core_failed", "透明派生核心解析失败。", 69) from exc


def timeline_text(json_path: Path) -> bytes:
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    segments = payload.get("transcription") or payload.get("segments") or []
    lines = []
    for segment in segments:
        timestamps = segment.get("timestamps") or {}
        start = str(timestamps.get("from") or "").replace(".", ",").split(",", 1)[0]
        end = str(timestamps.get("to") or "").replace(".", ",").split(",", 1)[0]
        text = str(segment.get("text") or "").strip()
        if start and end and text:
            lines.extend([f"[{start} → {end}]", text, ""])
    if not lines:
        raise ArchiveError("transcript_segments_missing", "结构化逐字稿没有可用时间分段。", 70)
    return ("\n".join(lines).rstrip() + "\n").encode()


def canonical_video_url(platform: str, info: dict, fallback: str) -> str:
    content_id = str(info.get("id") or "")
    if platform == "bilibili":
        match = re.search(r"BV[0-9A-Za-z]{10}", str(info.get("bvid") or content_id or fallback))
        if match:
            page_match = re.search(r"_p(\d+)$", content_id)
            page = int(page_match.group(1)) if page_match else int(info.get("page_number") or 1)
            suffix = f"?p={page}" if page > 1 else ""
            return f"https://www.bilibili.com/video/{match.group(0)}/{suffix}"
    if platform == "xiaohongshu" and content_id:
        return f"https://www.xiaohongshu.com/explore/{content_id}"
    if platform == "douyin" and content_id:
        return f"https://www.douyin.com/video/{content_id}"
    return canonical_url(str(info.get("webpage_url") or fallback))


def transcribe_content_video(video: Path, output_dir: Path, root: Path) -> list[dict]:
    prefix = output_dir / "原始逐字稿"
    _, generated = run_whisper(
        video,
        root,
        output_dir / ".content-audio.wav",
        prefix,
        ["-otxt", "-osrt", "-oj"],
        (".txt", ".srt", ".json"),
    )
    by_suffix = {path.suffix: path for path in generated}
    if set(by_suffix) != {".txt", ".srt", ".json"}:
        raise ArchiveError("transcript_incomplete", "三种原始逐字稿未全部生成。", 70)
    atomic_write(by_suffix[".txt"], timeline_text(by_suffix[".json"]))
    return [
        output_record(root, by_suffix[".txt"], "transcript_txt"),
        output_record(root, by_suffix[".srt"], "transcript_srt"),
        output_record(root, by_suffix[".json"], "transcript_json"),
    ]


def ensure_video_readable(video: Path) -> None:
    ffprobe = Path(require_executable("WECHAT_FFMPEG", "ffmpeg")).with_name("ffprobe")
    if not ffprobe.is_file():
        raise ArchiveError("missing_command", f"缺少命令：{ffprobe}", 69)
    result = subprocess.run(
        [
            str(ffprobe),
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video),
        ],
        capture_output=True,
        text=True,
        env=safe_subprocess_environment(),
    )
    if result.returncode or result.stdout.strip() != "video":
        raise ArchiveError("invalid_video", "下载结果不是可读取的视频。", 69)


def finalize_video_content(
    manifest: dict,
    manifest_path: Path,
    root: Path,
    info: dict,
    source_video: Path,
    route: str,
) -> dict:
    title = str(info.get("title") or "未命名视频")
    content_id = str(info.get("id") or manifest["content_id"])
    platform = str(manifest["platform"])
    output_dir = root / "content" / PLATFORM_DIRS[platform] / f"{safe_content_title(title, '未命名视频')}--{content_id}"
    output_dir.mkdir(parents=True, exist_ok=True)
    video = output_dir / "video.mp4"
    if source_video.suffix.lower() != ".mp4":
        raise ArchiveError("unexpected_video_container", f"提取结果不是 MP4：{source_video.suffix}", 69)
    ensure_video_readable(source_video)
    os.replace(source_video, video)
    manifest.update(
        {
            "status": "transcribing",
            "updated_at": utc_now(),
            "title": title,
            "content_id": content_id,
            "content_type": "video",
            "canonical_url": canonical_video_url(platform, info, str(manifest["source"])),
            "route": route,
            "output_dir": archive_relative(root, output_dir),
            "outputs": [output_record(root, video, "video")],
        }
    )
    write_json(manifest_path, manifest)
    transcripts = transcribe_content_video(video, output_dir, root)
    manifest["outputs"].extend(transcripts)
    manifest.update({"status": "completed", "updated_at": utc_now(), "completed_at": utc_now()})
    manifest.pop("error", None)
    manifest.pop("channel_delivery_recovery", None)
    write_json(manifest_path, manifest)
    return manifest


def process_transparent_video(manifest: dict, manifest_path: Path, root: Path) -> dict:
    work_dir = manifest_path.parent / "work"
    platform = str(manifest["platform"])
    try:
        info, source_video = download_with_transparent_core(str(manifest["source"]), platform, work_dir)
        route = "transparent_core"
    except ArchiveError as exc:
        if platform == "douyin" and manifest.get("douyin_media"):
            info, source_video = download_douyin_inventory_fallback(manifest, work_dir)
            route = "douyin_inventory_media"
        elif platform != "bilibili" or exc.code not in {"transparent_core_failed", "reauthentication_required", "video_missing"}:
            raise
        else:
            try:
                info, source_video = download_bilibili_fallback(str(manifest["source"]), work_dir)
            except ArchiveError:
                if exc.code == "reauthentication_required":
                    raise exc
                raise
            route = "bilibili_api_cdn"
    return finalize_video_content(manifest, manifest_path, root, info, source_video, route)


def resolve_xiaohongshu_url(url: str) -> str:
    return resolve_transparent_core_url(url, "xiaohongshu")


def finalize_xiaohongshu_image_text(manifest: dict, manifest_path: Path, root: Path, info: dict) -> dict:
    title = str(info.get("title") or "未命名小红书图文")
    content_id = str(info.get("id") or manifest["content_id"])
    description = str(info.get("description") or "").strip()
    if not description:
        raise ArchiveError("body_missing", "小红书图文没有可归档正文。", 69)
    output_dir = root / "content" / PLATFORM_DIRS["xiaohongshu"] / f"{safe_content_title(title, '未命名小红书图文')}--{content_id}"
    image_dir = output_dir / "配图"
    image_dir.mkdir(parents=True, exist_ok=True)
    image_outputs = []
    image_paths = []
    seen = set()
    for item in info.get("thumbnails") or []:
        image_url = str(item.get("url") or "")
        parsed_image = _split_url(image_url)
        image_host = (parsed_image.hostname or "").lower()
        if parsed_image.scheme == "http" and host_allowed(image_host, set(), XHS_MEDIA_HOST_SUFFIXES):
            image_url = urlunsplit(("https", parsed_image.netloc, parsed_image.path, parsed_image.query, ""))
        key = Path(urlsplit(image_url).path).name.split("!", 1)[0]
        if not image_url or key in seen:
            continue
        seen.add(key)
        data, content_type, final_url = fetch_limited(
            image_url,
            exact_hosts=set(),
            suffixes=XHS_MEDIA_HOST_SUFFIXES,
            max_bytes=MAX_MEDIA_BYTES,
        )
        target = image_dir / f"{len(image_paths) + 1:02d}{media_extension(content_type, final_url)}"
        atomic_write(target, data)
        image_paths.append(target)
        image_outputs.append(output_record(root, target, "image"))
    if not image_paths:
        raise ArchiveError("image_missing", "小红书图文没有可归档配图。", 69)

    canonical = canonical_video_url("xiaohongshu", info, str(manifest["source"]))
    lines = [
        f"# {title}",
        "",
        f"- 来源：{canonical}",
        f"- 作品标识：`{content_id}`",
        "- 内容类型：图文",
        "",
        description,
        "",
        "## 配图",
        "",
    ]
    for index, path in enumerate(image_paths, start=1):
        lines.extend([f"![配图 {index}](配图/{path.name})", ""])
    body = output_dir / "正文.md"
    atomic_write(body, "\n".join(lines).encode())
    manifest.update(
        {
            "status": "completed",
            "updated_at": utc_now(),
            "completed_at": utc_now(),
            "title": title,
            "content_id": content_id,
            "content_type": "image_text",
            "canonical_url": canonical,
            "route": "transparent_core_page",
            "output_dir": archive_relative(root, output_dir),
            "outputs": [output_record(root, body, "body_markdown"), *image_outputs],
        }
    )
    manifest.pop("error", None)
    write_json(manifest_path, manifest)
    return manifest


def process_xiaohongshu(manifest: dict, manifest_path: Path, root: Path) -> dict:
    resolved_url = resolve_xiaohongshu_url(str(manifest["source"]))
    info = inspect_with_transparent_core(resolved_url, "xiaohongshu")
    if info.get("formats"):
        work_dir = manifest_path.parent / "work"
        downloaded_info, source_video = download_with_transparent_core(resolved_url, "xiaohongshu", work_dir)
        return finalize_video_content(manifest, manifest_path, root, downloaded_info, source_video, "transparent_core")
    return finalize_xiaohongshu_image_text(manifest, manifest_path, root, info)


def bilibili_json(path: str, query: dict) -> dict:
    data, content_type, _ = fetch_limited(
        "https://api.bilibili.com" + path + "?" + urlencode(query),
        exact_hosts={"api.bilibili.com"},
        max_bytes=4 * 1024 * 1024,
    )
    if content_type not in {"application/json", "text/json", "text/plain"}:
        raise ArchiveError("bilibili_api_invalid", "B站运行时接口没有返回 JSON。", 69)
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ArchiveError("bilibili_api_invalid", "B站运行时接口响应无效。", 69) from exc
    if payload.get("code") != 0:
        raise ArchiveError("bilibili_api_failed", "B站运行时接口返回失败。", 69)
    return payload.get("data") or {}


def bilibili_creator_inventory(url: str) -> tuple[dict, list[dict]]:
    resolved = resolve_transparent_core_url(url, "bilibili")
    match = re.search(r"BV[0-9A-Za-z]{10}", resolved)
    if not match:
        raise ArchiveError("bilibili_id_missing", "B站链接没有解析出作品标识。", 69)
    view = bilibili_json("/x/web-interface/view", {"bvid": match.group(0)})
    owner = view.get("owner") or {}
    creator_id = str(owner.get("mid") or "")
    if not creator_id:
        raise ArchiveError("bilibili_creator_missing", "B站作品没有返回 UP 主标识。", 69)

    core = transparent_core_root()
    sys.path.insert(0, str(core))
    try:
        from yt_dlp import YoutubeDL  # type: ignore[import-not-found]
        from yt_dlp.utils import DownloadError  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ArchiveError("transparent_core_invalid", "透明派生核心无法加载。", 69) from exc
    options = {"cachedir": False, "extract_flat": True, "quiet": True, "no_warnings": True, "logger": SilentCookieLogger()}
    cookie_jar = platform_cookie_jar("bilibili")
    if cookie_jar.is_file():
        options["cookiefile"] = str(cookie_jar)
    try:
        with YoutubeDL(options) as downloader:
            playlist = downloader.extract_info(f"https://space.bilibili.com/{creator_id}/video", download=False)
    except DownloadError as exc:
        message = str(exc).lower()
        if "412" in message or "blocked" in message or "rejected" in message:
            raise ArchiveError("bilibili_rate_limited", "B站作者列表触发风控，请稍后导入持久 B 站 Cookie 再继续。", 69) from exc
        if transparent_core_needs_login(exc):
            raise ArchiveError("reauthentication_required", "B站登录态缺失或已失效。", 69) from exc
        raise ArchiveError("transparent_core_failed", "B站作者列表解析失败。", 69) from exc

    items = []
    for entry in playlist.get("entries") or []:
        content_id = str(entry.get("id") or "")
        if not re.fullmatch(r"BV[0-9A-Za-z]{10}", content_id):
            continue
        items.append(
            {
                "id": content_id,
                "url": f"https://www.bilibili.com/video/{content_id}/",
                "title": str(entry.get("title") or "未命名视频"),
                "published_at": entry.get("timestamp") or entry.get("release_timestamp"),
                "child_job_id": None,
            }
        )
    return {
        "id": creator_id,
        "name": str(owner.get("name") or ""),
        "url": f"https://space.bilibili.com/{creator_id}/video",
    }, items


def douyin_creator_inventory(url: str) -> tuple[dict, list[dict]]:
    resolved = resolve_transparent_core_url(url, "douyin")
    info = inspect_with_transparent_core(resolved, "douyin")
    creator_id = str(info.get("channel_id") or "")
    if not creator_id:
        raise ArchiveError("douyin_creator_missing", "抖音作品没有返回作者标识。", 69)
    cookie_path = platform_cookie_jar("douyin")
    if not cookie_path.is_file():
        raise ArchiveError("reauthentication_required", "请先导入持久抖音 Cookie。", 69)
    jar = http.cookiejar.MozillaCookieJar(str(cookie_path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except (OSError, http.cookiejar.LoadError) as exc:
        raise ArchiveError("reauthentication_required", "抖音 Cookie 无法读取，请重新导入。", 69) from exc
    opener = build_opener(HTTPCookieProcessor(jar))
    items = []
    cursor = 0
    seen_cursors = set()
    seen_ids = set()
    while True:
        query = {
            "device_platform": "webapp",
            "aid": "6383",
            "channel": "channel_pc_web",
            "sec_user_id": creator_id,
            "max_cursor": cursor,
            "count": 18,
            "publish_video_strategy_type": 2,
            "locate_query": "false",
            "pc_client_type": 1,
            "version_code": "170400",
            "version_name": "17.4.0",
            "cookie_enabled": "true",
            "browser_language": "zh-CN",
            "browser_platform": "MacIntel",
            "browser_name": "Chrome",
            "platform": "PC",
        }
        request = Request(
            "https://www.douyin.com/aweme/v1/web/aweme/post/?" + urlencode(query),
            headers={"User-Agent": "Mozilla/5.0", "Referer": f"https://www.douyin.com/user/{creator_id}", "Accept": "application/json"},
        )
        try:
            with opener.open(request, timeout=30) as response:
                payload = json.loads(response.read(4 * 1024 * 1024))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ArchiveError("douyin_creator_failed", "抖音作者作品列表读取失败。", 69) from exc
        if payload.get("status_code") != 0:
            raise ArchiveError("reauthentication_required", "抖音登录态已失效或触发平台验证。", 69)
        for aweme in payload.get("aweme_list") or []:
            content_id = str(aweme.get("aweme_id") or "")
            video = aweme.get("video") or {}
            media_urls = (video.get("play_addr") or {}).get("url_list") or []
            ordinary_video = aweme.get("aweme_type") == 0 and not (aweme.get("images") or []) and media_urls
            if ordinary_video and content_id and content_id not in seen_ids:
                seen_ids.add(content_id)
                items.append(
                    {
                        "id": content_id,
                        "url": f"https://www.douyin.com/video/{content_id}",
                        "title": str((aweme.get("desc") or "未命名视频")).strip(),
                        "published_at": aweme.get("create_time"),
                        "child_job_id": None,
                    }
                )
        if not payload.get("has_more"):
            break
        next_cursor = int(payload.get("max_cursor") or 0)
        if not next_cursor or next_cursor == cursor or next_cursor in seen_cursors:
            raise ArchiveError("douyin_cursor_invalid", "抖音作者分页游标没有前进。", 69)
        seen_cursors.add(cursor)
        cursor = next_cursor
    items.sort(key=lambda item: int(item.get("published_at") or 0), reverse=True)
    return {
        "id": creator_id,
        "name": str(info.get("channel") or info.get("uploader") or ""),
        "url": f"https://www.douyin.com/user/{creator_id}",
    }, items


def inspect_creator(url: str, root: Path) -> dict:
    normalized, platform = submission_url(url)
    if platform == "wechat_channels":
        return inspect_channel_creator(normalized, root)
    if platform == "bilibili":
        creator, items = bilibili_creator_inventory(normalized)
    elif platform == "douyin":
        creator, items = douyin_creator_inventory(normalized)
    else:
        raise ArchiveError("creator_batch_unsupported", "作者历史批量当前只支持视频号、B站和抖音。")
    if not items:
        raise ArchiveError("creator_inventory_empty", "该作者当前没有可下载视频。", 66)
    job_id, job_dir, manifest = new_job(root, "batch", canonical_url(normalized))
    manifest.update(
        {
            "kind": "creator_batch",
            "status": "awaiting_download_count",
            "updated_at": utc_now(),
            "platform": platform,
            "creator": creator,
            "inventory": {"captured_at": utc_now(), "available": len(items), "items": items},
            "selection": None,
            "child_job_ids": [],
            "counts": {"selected": 0, "processing": 0, "completed": 0, "failed": 0},
            "next_action": f"该博主当前可下载视频共 {len(items)} 个，默认从最新开始。你要下载多少个？",
        }
    )
    write_json(job_dir / "manifest.json", manifest)
    return {
        "ok": True,
        "job_id": job_id,
        "status": manifest["status"],
        "platform": platform,
        "creator": creator,
        "available": len(items),
        "question": manifest["next_action"],
        "manifest": archive_relative(root, job_dir / "manifest.json"),
    }


def _refresh_creator_batch_unlocked(manifest: dict, manifest_path: Path, root: Path) -> dict:
    processing = completed = failed = waiting_authorization = waiting_reauthentication = 0
    for item in (manifest.get("inventory") or {}).get("items") or []:
        child_id = item.get("child_job_id")
        if not child_id:
            continue
        child_path = root / "jobs" / str(child_id) / "manifest.json"
        if not child_path.is_file():
            processing += 1
            continue
        child = read_json_if_valid(child_path)
        if not child:
            item["result"] = "failed"
            item["error_code"] = "child_manifest_invalid"
            failed += 1
            continue
        if child.get("status") == "completed":
            item["result"] = "completed"
            item.pop("error_code", None)
            completed += 1
        elif child.get("status") == "failed":
            item["result"] = "failed"
            item["error_code"] = (child.get("error") or {}).get("code")
            failed += 1
        elif child.get("status") == "waiting_for_authorization":
            item["result"] = "waiting_for_authorization"
            waiting_authorization += 1
        elif child.get("status") == "waiting_for_reauthentication":
            item["result"] = "waiting_for_reauthentication"
            waiting_reauthentication += 1
        else:
            item["result"] = "processing"
            processing += 1
    selected = int((manifest.get("selection") or {}).get("limit") or 0)
    manifest["counts"] = {
        "selected": selected,
        "processing": processing,
        "waiting_for_authorization": waiting_authorization,
        "waiting_for_reauthentication": waiting_reauthentication,
        "completed": completed,
        "failed": failed,
    }
    if waiting_reauthentication:
        manifest["status"] = "waiting_for_reauthentication"
    elif waiting_authorization:
        manifest["status"] = "waiting_for_authorization"
    elif selected and not processing:
        manifest.update({"status": "completed_with_failures" if failed else "completed", "completed_at": utc_now()})
    elif selected:
        manifest["status"] = "processing"
    manifest["updated_at"] = utc_now()
    write_json(manifest_path, manifest)
    return manifest


def _submit_creator_batch_children_unlocked(manifest: dict, manifest_path: Path, root: Path) -> dict:
    limit = int((manifest.get("selection") or {}).get("limit") or 0)
    items = (manifest.get("inventory") or {}).get("items") or []
    parent_job_id = str(manifest.get("job_id") or manifest_path.parent.name)
    for item in items[:limit]:
        existing_child_id = str(item.get("child_job_id") or "")
        if existing_child_id and manifest.get("platform") != "wechat_channels":
            continue
        if manifest.get("platform") == "wechat_channels":
            deterministic_id = frozen_channel_child_job_id(parent_job_id, item)
            if existing_child_id and existing_child_id != deterministic_id:
                child_path = root / "jobs" / existing_child_id / "manifest.json"
                adopted = adopt_legacy_frozen_channel_content(child_path, item, parent_job_id)
                child = {
                    "job_id": existing_child_id,
                    "manifest": archive_relative(root, child_path),
                    "status": adopted.get("status"),
                }
            else:
                child = submit_frozen_channel_content(item, root, parent_job_id)
        else:
            child = submit_content(str(item["url"]), root)
        if manifest.get("platform") == "douyin":
            child_path = root / str(child["manifest"])
            child_manifest = json.loads(child_path.read_text(encoding="utf-8"))
            child_manifest["douyin_media"] = {"content_id": item["id"]}
            write_json(child_path, child_manifest)
        if existing_child_id and existing_child_id != child["job_id"]:
            raise ArchiveError("creator_child_identity_conflict", "父任务中的冻结子任务身份冲突。", 69)
        item.update({"child_job_id": child["job_id"], "result": "processing"})
        if child["job_id"] not in manifest["child_job_ids"]:
            manifest["child_job_ids"].append(child["job_id"])
        manifest.update({"status": "submitting", "updated_at": utc_now()})
        write_json(manifest_path, manifest)
        if manifest.get("platform") == "wechat_channels":
            activate_staged_channel_content(root / str(child["manifest"]), parent_job_id)
    return _refresh_creator_batch_unlocked(manifest, manifest_path, root)


def creator_batch_lock_path(manifest_path: Path) -> Path:
    return manifest_path.with_suffix(".json.batch.lock")


def acquire_creator_batch_lock(manifest_path: Path) -> int | None:
    lock_path = creator_batch_lock_path(manifest_path)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    os.chmod(lock_path, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    return descriptor


def refresh_creator_batch(manifest: dict, manifest_path: Path, root: Path) -> dict:
    descriptor = acquire_creator_batch_lock(manifest_path)
    if descriptor is None:
        return read_json_if_valid(manifest_path) or manifest
    try:
        latest = read_json_if_valid(manifest_path) or manifest
        return _refresh_creator_batch_unlocked(latest, manifest_path, root)
    finally:
        os.close(descriptor)


def submit_creator_batch_children(manifest: dict, manifest_path: Path, root: Path) -> dict:
    descriptor = acquire_creator_batch_lock(manifest_path)
    if descriptor is None:
        return read_json_if_valid(manifest_path) or manifest
    try:
        current = read_json_if_valid(manifest_path) or manifest
        return _submit_creator_batch_children_unlocked(current, manifest_path, root)
    finally:
        os.close(descriptor)


def download_creator_plan(job_id: str, limit: int, root: Path) -> dict:
    if not JOB_ID_RE.fullmatch(job_id):
        raise ArchiveError("invalid_job_id", "Job ID 格式错误。")
    manifest_path = root / "jobs" / job_id / "manifest.json"
    if not manifest_path.is_file():
        raise ArchiveError("job_not_found", "没有找到该任务。", 66)
    descriptor = acquire_creator_batch_lock(manifest_path)
    if descriptor is None:
        raise ArchiveError("job_busy", "该任务正在提交，请稍后查询同一任务状态。", 75)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("kind") != "creator_batch" or manifest.get("status") != "awaiting_download_count":
            raise ArchiveError("job_not_waiting_for_count", "该任务当前不在等待下载数量。", 66)
        items = (manifest.get("inventory") or {}).get("items") or []
        if limit < 1 or limit > len(items):
            raise ArchiveError("invalid_download_count", f"下载数量应在 1 到 {len(items)} 之间。")
        manifest.update({"status": "submitting", "updated_at": utc_now(), "selection": {"limit": limit, "order": "newest"}})
        manifest.pop("next_action", None)
        write_json(manifest_path, manifest)
        manifest = _submit_creator_batch_children_unlocked(manifest, manifest_path, root)
    finally:
        os.close(descriptor)
    return {
        "ok": True,
        "job_id": job_id,
        "status": manifest["status"],
        "platform": manifest["platform"],
        "counts": manifest["counts"],
        "child_job_ids": manifest["child_job_ids"],
        "manifest": archive_relative(root, manifest_path),
    }


def download_bilibili_fallback(url: str, work_dir: Path) -> tuple[dict, Path]:
    page, _, final_url = fetch_limited(
        url,
        exact_hosts={"b23.tv", "bilibili.com"},
        suffixes=(".bilibili.com",),
        max_bytes=8 * 1024 * 1024,
    )
    match = re.search(r"BV[0-9A-Za-z]{10}", final_url) or re.search(rb"BV[0-9A-Za-z]{10}", page)
    if not match:
        raise ArchiveError("bilibili_id_missing", "B站链接没有解析出作品标识。", 69)
    bvid = match.group(0).decode() if isinstance(match.group(0), bytes) else match.group(0)
    view = bilibili_json("/x/web-interface/view", {"bvid": bvid})
    pages = view.get("pages") or []
    page_number = max(1, int(dict(parse_qsl(urlsplit(final_url).query)).get("p") or 1))
    if page_number > len(pages):
        raise ArchiveError("bilibili_page_missing", "B站分P不存在。", 69)
    cid = pages[page_number - 1].get("cid")
    play = bilibili_json(
        "/x/player/playurl",
        {"bvid": bvid, "cid": cid, "qn": 64, "fnval": 0, "fourk": 1, "platform": "html5"},
    )
    durl = (play.get("durl") or [None])[0] or {}
    candidates = [durl.get("url"), *(durl.get("backup_url") or [])]
    target = work_dir / "source.mp4"
    last_error = None
    for candidate in [candidate for candidate in candidates if candidate] * 3:
        try:
            download_public_https(str(candidate), target, referer=f"https://www.bilibili.com/video/{bvid}/")
            ensure_video_readable(target)
            break
        except ArchiveError as exc:
            target.unlink(missing_ok=True)
            last_error = exc
    else:
        raise last_error or ArchiveError("bilibili_media_missing", "B站没有返回可用媒体地址。", 69)
    title = str(view.get("title") or "未命名视频")
    if len(pages) > 1:
        part = str(pages[page_number - 1].get("part") or "")
        title = f"{title} - P{page_number} {part}".strip()
    return {
        "id": f"{bvid}_p{page_number}",
        "bvid": bvid,
        "page_number": page_number,
        "title": title,
        "webpage_url": f"https://www.bilibili.com/video/{bvid}/?p={page_number}",
    }, target


def download_douyin_inventory_fallback(manifest: dict, work_dir: Path) -> tuple[dict, Path]:
    content_id = str((manifest.get("douyin_media") or {}).get("content_id") or manifest.get("content_id") or "")
    cookie_path = platform_cookie_jar("douyin")
    if not cookie_path.is_file():
        raise ArchiveError("reauthentication_required", "请先导入持久抖音 Cookie。", 69)
    jar = http.cookiejar.MozillaCookieJar(str(cookie_path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except (OSError, http.cookiejar.LoadError) as exc:
        raise ArchiveError("reauthentication_required", "抖音 Cookie 无法读取，请重新导入。", 69) from exc
    query = {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "aweme_id": content_id,
        "pc_client_type": 1,
        "version_code": "170400",
        "version_name": "17.4.0",
        "cookie_enabled": "true",
        "browser_language": "zh-CN",
        "browser_platform": "MacIntel",
        "browser_name": "Chrome",
        "platform": "PC",
    }
    request = Request(
        "https://www.douyin.com/aweme/v1/web/aweme/detail/?" + urlencode(query),
        headers={"User-Agent": "Mozilla/5.0", "Referer": str(manifest["source"]), "Accept": "application/json"},
    )
    try:
        with build_opener(HTTPCookieProcessor(jar)).open(request, timeout=30) as response:
            payload = json.loads(response.read(4 * 1024 * 1024))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ArchiveError("douyin_media_failed", "抖音视频地址读取失败。", 69) from exc
    detail = payload.get("aweme_detail") or {}
    media_urls = ((detail.get("video") or {}).get("play_addr") or {}).get("url_list") or []
    if payload.get("status_code") != 0 or str(detail.get("aweme_id") or "") != content_id or not media_urls:
        raise ArchiveError("reauthentication_required", "抖音登录态已失效或触发平台验证。", 69)
    target = work_dir / "source.mp4"
    last_error = None
    for candidate in media_urls:
        try:
            download_public_https(str(candidate), target, referer=str(manifest["source"]))
            ensure_video_readable(target)
            break
        except ArchiveError as exc:
            target.unlink(missing_ok=True)
            last_error = exc
    else:
        raise last_error or ArchiveError("douyin_media_missing", "抖音没有返回可用视频地址。", 69)
    return {
        "id": content_id,
        "title": str(detail.get("desc") or manifest.get("title") or "未命名视频"),
        "webpage_url": manifest["source"],
    }, target


def recovery_timeout(deadline: float | None, maximum: float = 20.0) -> float:
    if deadline is None:
        return maximum
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ArchiveError("recovery_window_expired", "视频号恢复窗口已结束，未完成任务保持原状态。", 69)
    return min(maximum, max(0.1, remaining))


def channels_api(
    path: str,
    *,
    query: dict | None = None,
    body: dict | None = None,
    deadline: float | None = None,
) -> dict:
    url = CHANNELS_API_BASE + path
    if path.startswith("/api/mp/"):
        query = dict(query or {}, token=mp_api_token())
    if query:
        url += "?" + urlencode(query)
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    request = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST" if data else "GET")
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=recovery_timeout(deadline)) as response:
            payload = json.loads(response.read().decode())
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ArchiveError("channels_backend_unavailable", f"视频号采集后端不可用：{exc}", 69) from exc
    if not isinstance(payload, dict) or payload.get("code") != 0:
        message = payload.get("msg", "视频号采集后端返回错误。") if isinstance(payload, dict) else "视频号采集后端响应无效。"
        if isinstance(payload, dict) and payload.get("code") == 400 and str(message) in {
            "请先初始化客户端 socket 连接",
            "please initialize the client socket connection first",
        }:
            raise ArchiveError("channels_authorization_required", "视频号采集会话尚未就绪。", 69)
        raise ArchiveError("channels_backend_error", str(message), 69)
    return payload.get("data") or {}


def channels_payload_data(payload: dict) -> dict:
    if payload.get("errCode", 0) != 0:
        message = str(payload.get("errMsg") or "视频号接口返回错误。")
        if message == "WXU.API.finderGetCommentDetail is not a function":
            raise ArchiveError("channels_authorization_required", "当前连接的页面尚未提供视频号详情接口。", 69)
        raise ArchiveError("channels_backend_error", message, 69)
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        raise ArchiveError("channels_backend_error", "视频号接口响应无效。", 69)
    return data


def safe_channel_name(value: str, fallback: str) -> str:
    return (re.sub(r"[\\/:\x00-\x1f]", "_", value).strip(" .")[:180] or fallback)


def resolve_channel_share_metadata(share_url: str, deadline: float | None = None) -> dict:
    short_uri = urlsplit(share_url).path.rstrip("/").rsplit("/", 1)[-1]
    page_url = f"https://channels.weixin.qq.com/finder-preview/pages/sph?id={short_uri}"
    request = Request(
        "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info?" + urlencode({"_pageUrl": page_url}),
        data=json.dumps({"baseReq": {"generalToken": ""}, "shortUri": short_uri}).encode(),
        headers={"Content-Type": "application/json", "Origin": "https://channels.weixin.qq.com", "Referer": page_url},
        method="POST",
    )
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=recovery_timeout(deadline)) as response:
            payload = json.loads(response.read().decode())
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ArchiveError("channels_share_resolve_failed", f"视频号分享链接解析失败：{exc}", 69) from exc
    data = payload.get("data") or {}
    eid = ((data.get("sceneInfo") or {}).get("dynamicExportId"))
    if payload.get("errCode") != 0 or not eid:
        raise ArchiveError("channels_share_resolve_failed", str(payload.get("errMsg") or "分享链接没有返回视频标识。"), 69)
    author = data.get("authorInfo") or {}
    return {
        "eid": str(eid),
        "nickname": str(author.get("nickname") or ""),
        "avatar": str(author.get("headImgUrl") or ""),
    }


def resolve_channel_share_eid(share_url: str, deadline: float | None = None) -> str:
    return str(resolve_channel_share_metadata(share_url, deadline=deadline)["eid"])


def recent_channel_author(captured_after_ms: int) -> dict | None:
    database = Path(
        os.environ.get(
            "WECHAT_CHANNELS_DATA_DB",
            str(Path.home() / ".local" / "share" / "wx_channels_download" / "v260810" / "runtime" / "data.db"),
        )
    )
    if not database.is_file():
        return None
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT a.external_id, COALESCE(a.nickname, ''), COALESCE(a.avatar_url, '')
                FROM browse_history AS h
                JOIN browse_history_account AS ha ON ha.browse_history_id = h.id
                JOIN account AS a ON a.id = ha.account_id
                WHERE h.updated_at >= ? AND ha.role = 'author'
                ORDER BY h.updated_at DESC
                """,
                (captured_after_ms,),
            ).fetchall()
    except sqlite3.Error:
        return None
    if len(rows) != 1 or not rows[0][0]:
        return None
    return {"username": rows[0][0], "nickname": rows[0][1], "avatar": rows[0][2], "signature": ""}


def known_official_account_for_source(source_url: str, root: Path) -> dict | None:
    source = canonical_url(source_url)
    candidates: dict[str, dict] = {}
    database = Path(
        os.environ.get(
            "WECHAT_CHANNELS_DATA_DB",
            str(Path.home() / ".local" / "share" / "wx_channels_download" / "v260810" / "runtime" / "data.db"),
        )
    )
    if database.is_file():
        try:
            with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
                rows = connection.execute(
                    """
                    SELECT COALESCE(h.source_url, ''), h.url, COALESCE(a.nickname, '')
                    FROM browse_history AS h
                    JOIN browse_history_account AS ha ON ha.browse_history_id = h.id
                    JOIN account AS a ON a.id = ha.account_id
                    WHERE h.type = 'article' AND ha.role = 'author'
                    """
                ).fetchall()
        except sqlite3.Error:
            rows = []
        for captured_source, article_url, nickname in rows:
            if canonical_url(str(captured_source or "")) != source:
                continue
            metadata = official_article_metadata(html.unescape(str(article_url or "")))
            biz = str(metadata.get("biz") or "")
            if biz:
                candidates[biz] = {
                    "biz": biz,
                    "account_id": hashlib.sha256(f"wechat_official_account\0{biz}".encode()).hexdigest()[:16],
                    "account_name": str(nickname or ""),
                }
    for manifest_path in (root / "jobs").glob("batch-????????T??????Z-????????/manifest.json"):
        candidate = read_json_if_valid(manifest_path)
        if not candidate or canonical_url(str(candidate.get("source") or "")) != source:
            continue
        account = candidate.get("account") or {}
        biz = str(account.get("biz") or "")
        if biz:
            candidates[biz] = {
                "biz": biz,
                "account_id": str(account.get("account_id") or "")
                or hashlib.sha256(f"wechat_official_account\0{biz}".encode()).hexdigest()[:16],
                "account_name": str(account.get("name") or candidates.get(biz, {}).get("account_name") or ""),
            }
    if len(candidates) != 1:
        return None
    return next(iter(candidates.values()))


def resolve_channel_author_from_url(share_url: str, deadline: float | None = None) -> dict:
    """Resolve a Channels share URL without opening or controlling WeChat."""
    safe_url = validate_https_url(share_url, {"weixin.qq.com", "channels.weixin.qq.com"})
    metadata = resolve_channel_share_metadata(safe_url, deadline=deadline)
    public_avatar = str(metadata.get("avatar") or "").strip()
    try:
        profile = channels_payload_data(
            channels_api("/api/channels/feed/profile", query={"eid": metadata["eid"]}, deadline=deadline)
        )
        contact = (profile.get("object") or {}).get("contact") or {}
        if contact.get("username"):
            public_nickname = str(metadata.get("nickname") or "").strip()
            profile_nickname = str(contact.get("nickname") or "").strip()
            if public_nickname and profile_nickname != public_nickname:
                raise ArchiveError(
                    "channel_author_selection_required",
                    "公开分享页与详情接口返回的博主昵称不一致，无法安全自动绑定；请用户在手机端选择正确博主。",
                    66,
                )
            return {
                "username": contact["username"],
                "nickname": contact.get("nickname", ""),
                "avatar": contact.get("headUrl", ""),
                "signature": contact.get("signature", ""),
            }
    except ArchiveError as exc:
        if exc.code != "channels_authorization_required":
            raise

    nickname = str(metadata.get("nickname") or "").strip()
    avatar = str(metadata.get("avatar") or "").strip()
    if nickname:
        if not avatar:
            raise ArchiveError(
                "channel_author_selection_required",
                "公开分享页缺少可核对的博主头像，不能只按昵称自动绑定；请用户在手机端选择正确博主。",
                66,
            )
        search = search_channel_author(nickname, deadline=deadline)
        exact = [candidate for candidate in search["candidates"] if candidate.get("nickname") == nickname]
        avatar_matches = [candidate for candidate in exact if avatar and candidate.get("avatar") == avatar]
        if avatar and exact and not avatar_matches:
            raise ArchiveError(
                "channel_author_selection_required",
                "公开分享页头像与同名搜索结果不一致，请用户在手机端选择正确博主。",
                66,
            )
        choices = avatar_matches if avatar else exact
        if len(choices) == 1:
            return choices[0]
        if len(choices) > 1:
            raise ArchiveError("channel_author_selection_required", "公开分享页对应多个同名视频号，请用户在手机端选择正确博主。", 66)

    raise ArchiveError(
        "channels_authorization_required",
        "视频号会话当前无法从分享链接唯一识别博主。任务已保留；请用户方便使用 Mac 时，在 Mac 微信中手动打开原链接后继续。",
        69,
    )


def search_channel_author(query: str, deadline: float | None = None) -> dict:
    data = channels_payload_data(
        channels_api("/api/channels/contact/search", query={"keyword": query}, deadline=deadline)
    )
    candidates = []
    for item in data.get("infoList") or []:
        contact = item.get("contact") or {}
        if contact.get("username"):
            candidates.append(
                {
                    "username": contact["username"],
                    "nickname": contact.get("nickname", ""),
                    "avatar": contact.get("headUrl", ""),
                    "signature": contact.get("signature", ""),
                }
            )
    return {"ok": True, "query": query, "count": len(candidates), "candidates": candidates}


def download_channel_url(url: str, root: Path) -> dict:
    submitted = submit_content(url, root)
    manifest_path = root / "jobs" / submitted["job_id"] / "manifest.json"
    try:
        creator = resolve_channel_author_from_url(url)
        register_channel_creator(root, url, creator)
        manifest = process_content_job(manifest_path, root)
    except ArchiveError as exc:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        waiting = exc.code in {
            "channels_authorization_required",
            "channels_backend_unavailable",
            "channels_backend_error",
            "channels_share_resolve_failed",
            "channels_task_create_failed",
        }
        manifest.update(
            {
                "status": "waiting_for_authorization" if waiting else "failed",
                "updated_at": utc_now(),
                "error": {"code": exc.code, "message": str(exc)},
            }
        )
        if not waiting:
            manifest["completed_at"] = utc_now()
        else:
            manifest["next_action"] = str(exc)
        write_json(manifest_path, manifest)
    return {
        "ok": manifest["status"] != "failed",
        "job_id": submitted["job_id"],
        "status": manifest["status"],
        "platform": "wechat_channels",
        "manifest": archive_relative(root, manifest_path),
    }


def ordinary_channel_video(obj: dict) -> bool:
    desc = obj.get("objectDesc") or {}
    return desc.get("mediaType") == CHANNEL_VIDEO_MEDIA_TYPE and bool(desc.get("media")) and not obj.get("liveInfo")



def submit_channel_objects(
    objects: list[dict],
    output_dir: Path,
    deadline: float | None = None,
) -> tuple[list[dict], list[int]]:
    if not objects:
        return [], []
    result = channels_api(
        "/api/v1/download_task/create",
        body={
            "objects": [
                {
                    "platform": "wxchannels",
                    "content": obj,
                    "download_dir": str(output_dir),
                    "config": {"spec": "original"},
                }
                for obj in objects
            ]
        },
        deadline=deadline,
    )
    task_items = result.get("tasks") or []
    records = []
    task_ids = []
    for index, obj in enumerate(objects):
        item = task_items[index] if index < len(task_items) else {}
        data = item.get("data") or {}
        task_id = int(data.get("id") or 0)
        code = item.get("code")
        if code == 0 and data.get("skipped"):
            disposition = "skipped"
        elif code == 0 and task_id:
            disposition = "submitted"
        elif task_id:
            disposition = "skipped"
        else:
            disposition = "failed"
        if task_id:
            task_ids.append(task_id)
        records.append(
            {
                "object_id": obj.get("id", ""),
                "upstream_task_id": task_id or None,
                "disposition": disposition,
                "message": item.get("msg", ""),
            }
        )
    return records, task_ids


def process_channel_content(
    manifest: dict,
    manifest_path: Path,
    root: Path,
    deadline: float | None = None,
) -> dict:
    work_dir = manifest_path.parent / "work"
    if manifest.get("channel_delivery_recovery") is True:
        videos = wait_for_channel_video([], manifest, manifest_path, root, deadline=deadline)
        info = {"id": manifest["content_id"], "title": manifest["title"], "webpage_url": manifest["source"]}
        return finalize_video_content(manifest, manifest_path, root, info, videos[0], "wechat_channels_backend")
    task_ids = manifest.get("upstream_task_ids") or []
    if not task_ids:
        obj = manifest.get("channel_object") or {}
        if not obj:
            safe_url = validate_https_url(str(manifest["source"]), {"weixin.qq.com", "channels.weixin.qq.com"})
            eid = resolve_channel_share_eid(safe_url, deadline=deadline)
            profile = channels_payload_data(
                channels_api("/api/channels/feed/profile", query={"eid": eid}, deadline=deadline)
            )
            obj = profile.get("object") or {}
        if not ordinary_channel_video(obj):
            raise ArchiveError("channel_video_not_found", "分享链接中没有找到普通视频。")
        records, task_ids = submit_channel_objects([obj], work_dir, deadline=deadline)
        if not task_ids:
            raise ArchiveError("channels_task_create_failed", records[0].get("message") or "创建视频下载任务失败。", 69)
        description = str((obj.get("objectDesc") or {}).get("description") or "未命名视频号视频")
        manifest.pop("channel_object", None)
        manifest.update(
            {
                "status": "downloading",
                "updated_at": utc_now(),
                "title": description,
                "content_id": str(obj.get("id") or manifest["content_id"]),
                "route": "wechat_channels_backend",
                "upstream_task_ids": task_ids,
            }
        )
        write_json(manifest_path, manifest)
        return manifest

    records = channel_task_records(task_ids, auto_resume=manifest.get("auto_resume") is True)
    statuses = [record.get("status") for record in records]
    if any(status in {6, 7} for status in statuses):
        raise ArchiveError("channels_download_failed", "视频号后端下载任务失败。", 69)
    if not statuses or any(status != 5 for status in statuses):
        manifest.update({"status": "downloading", "updated_at": utc_now()})
        write_json(manifest_path, manifest)
        return manifest

    videos = wait_for_channel_video(records, manifest, manifest_path, root, deadline=deadline)
    info = {"id": manifest["content_id"], "title": manifest["title"], "webpage_url": manifest["source"]}
    return finalize_video_content(manifest, manifest_path, root, info, videos[0], "wechat_channels_backend")


def usable_channel_video(path: Path) -> bool:
    try:
        return path.suffix.lower() == ".mp4" and path.is_file() and not path.is_symlink() and path.stat().st_size > 0
    except OSError:
        return False


def channel_video_within(path: Path, allowed_root: Path) -> Path | None:
    try:
        if path.is_symlink():
            return None
        resolved_root = allowed_root.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
        resolved_path.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved_path if usable_channel_video(resolved_path) else None


def channel_local_video_candidates(manifest: dict, manifest_path: Path, root: Path) -> list[Path]:
    candidates: list[Path] = []
    work_dir = manifest_path.parent / "work"
    if work_dir.is_dir() and not work_dir.is_symlink():
        for path in work_dir.rglob("*.mp4"):
            trusted = channel_video_within(path, work_dir)
            if trusted:
                candidates.append(trusted)

    output_root = root / "content" / PLATFORM_DIRS["wechat_channels"]
    for output in manifest.get("outputs") or []:
        if output.get("role") != "video" or not output.get("path"):
            continue
        trusted = channel_video_within(root / str(output["path"]), output_root)
        if trusted:
            candidates.append(trusted)

    title = safe_content_title(str(manifest.get("title") or "未命名视频"), "未命名视频")
    content_id = str(manifest.get("content_id") or "")
    if content_id:
        archived = output_root / f"{title}--{content_id}" / "video.mp4"
        trusted = channel_video_within(archived, output_root)
        if trusted:
            candidates.append(trusted)

    unique = {str(path): path for path in candidates}
    return list(unique.values())


def channel_video_candidates(records: list[dict], manifest: dict, manifest_path: Path, root: Path) -> list[Path]:
    candidates = channel_local_video_candidates(manifest, manifest_path, root)
    work_dir = manifest_path.parent / "work"
    for record in records:
        for item in record.get("files") or []:
            path = (Path(item.get("download_dir") or "") / (item.get("name") or "")).expanduser().absolute()
            trusted = channel_video_within(path, work_dir)
            if trusted:
                candidates.append(trusted)
    unique = {str(path): path for path in candidates}
    return list(unique.values())


def wait_for_channel_video(
    records: list[dict],
    manifest: dict,
    manifest_path: Path,
    root: Path,
    deadline: float | None = None,
) -> list[Path]:
    end = time.monotonic() + max(0.0, CHANNEL_VIDEO_FINALIZE_WAIT_SECONDS)
    if deadline is not None:
        end = min(end, deadline)
    previous_state: tuple[str, int, int] | None = None
    while True:
        videos = channel_video_candidates(records, manifest, manifest_path, root)
        if len(videos) == 1:
            try:
                stat = videos[0].stat()
                current_state = (str(videos[0]), stat.st_size, stat.st_mtime_ns)
            except OSError:
                current_state = None
            if current_state is not None and current_state == previous_state:
                return videos
            previous_state = current_state
        else:
            previous_state = None
        if len(videos) > 1:
            raise ArchiveError("channel_video_ambiguous", "视频号后端返回了多个 MP4 文件，无法安全选择。", 69)
        now = time.monotonic()
        if now >= end:
            raise ArchiveError("channel_video_missing", "视频号后端完成后仍未找到稳定且唯一的 MP4 文件。", 69)
        time.sleep(min(CHANNEL_VIDEO_FINALIZE_POLL_SECONDS, end - now))


def official_batch_page_items(page: dict) -> list[dict]:
    try:
        messages = json.loads(str(page.get("general_msg_list") or "{}"))
    except json.JSONDecodeError as exc:
        raise ArchiveError("official_account_page_invalid", "公众号历史页响应无效。", 69) from exc
    items = []
    for message in messages.get("list") or []:
        published = int((message.get("comm_msg_info") or {}).get("datetime") or 0)
        primary = message.get("app_msg_ext_info") or {}
        for article in [primary, *(primary.get("multi_app_msg_item_list") or [])]:
            raw_url = html.unescape(str(article.get("content_url") or ""))
            if not raw_url:
                continue
            parsed = urlsplit(raw_url)
            normalized = urlunsplit(("https", "mp.weixin.qq.com", parsed.path or "/s", parsed.query, ""))
            normalized, platform = submission_url(normalized)
            if platform != "wechat_official_account":
                continue
            metadata = official_article_metadata(normalized)
            items.append(
                {
                    "content_id": metadata["content_id"],
                    "title": str(article.get("title") or "未命名公众号文章"),
                    "published_at": datetime.fromtimestamp(published, timezone.utc).isoformat().replace("+00:00", "Z") if published else None,
                    "canonical_url": metadata["canonical_url"],
                    "child_job_id": None,
                    "result": "discovered",
                    "error_code": None,
                }
            )
    return items


def existing_official_content(root: Path, content_id: str) -> dict | None:
    candidates = []
    pattern = "content-????????T??????Z-????????/manifest.json"
    for manifest_path in (root / "jobs").glob(pattern):
        child = read_json_if_valid(manifest_path)
        if not child:
            continue
        if child.get("platform") == "wechat_official_account" and child.get("content_id") == content_id:
            candidates.append(child)
    if not candidates:
        return None
    return next((child for child in candidates if child.get("status") == "completed"), candidates[0])


def _refresh_official_batch_unlocked(manifest: dict, manifest_path: Path, root: Path) -> dict:
    if manifest.get("status") == "awaiting_download_count" and not manifest.get("selection"):
        return manifest
    completed = unavailable = failed = processing = 0
    selected_items = (manifest.get("items") or [])[: int((manifest.get("selection") or {}).get("limit") or 0)]
    skipped = sum(item.get("result") == "skipped_existing" for item in selected_items)
    for item in selected_items:
        if item.get("result") == "skipped_existing":
            continue
        child_id = item.get("child_job_id")
        child_path = root / "jobs" / str(child_id) / "manifest.json"
        if not child_id or not child_path.is_file():
            processing += 1
            continue
        child = read_json_if_valid(child_path)
        if not child:
            item.update({"result": "failed", "error_code": "child_manifest_invalid"})
            failed += 1
            continue
        status = child.get("status")
        if status == "completed":
            item.update({"result": "completed", "error_code": None})
            completed += 1
        elif status == "failed":
            code = str((child.get("error") or {}).get("code") or "unexpected_error")
            item.update({"result": "unavailable" if code == "article_unavailable" else "failed", "error_code": code})
            if code == "article_unavailable":
                unavailable += 1
            else:
                failed += 1
        else:
            item.update({"result": "processing", "error_code": None})
            processing += 1
    discovered = len(manifest.get("items") or [])
    manifest["counts"] = {
        "discovered": discovered,
        "selected": len(selected_items),
        "submitted": len(selected_items) - skipped,
        "skipped_existing": skipped,
        "processing": processing,
        "completed": completed,
        "unavailable": unavailable,
        "failed": failed,
    }
    if processing:
        manifest["status"] = "processing"
    elif unavailable or failed:
        manifest.update({"status": "completed_with_failures", "completed_at": utc_now()})
    else:
        manifest.update({"status": "completed", "completed_at": utc_now()})
    manifest["updated_at"] = utc_now()
    write_json(manifest_path, manifest)
    return manifest


def refresh_official_batch(manifest: dict, manifest_path: Path, root: Path) -> dict:
    descriptor = acquire_creator_batch_lock(manifest_path)
    if descriptor is None:
        return read_json_if_valid(manifest_path) or manifest
    try:
        latest = read_json_if_valid(manifest_path) or manifest
        return _refresh_official_batch_unlocked(latest, manifest_path, root)
    finally:
        os.close(descriptor)


def _submit_official_batch_children_unlocked(manifest: dict, manifest_path: Path, root: Path) -> dict:
    limit = int((manifest.get("selection") or {}).get("limit") or 0)
    for item in (manifest.get("items") or [])[:limit]:
        if item.get("child_job_id"):
            continue
        existing = existing_official_content(root, str(item["content_id"]))
        if existing:
            existing_path = root / "jobs" / str(existing["job_id"]) / "manifest.json"
            existing_changed = False
            for field in ("title", "published_at"):
                if not existing.get(field) and item.get(field):
                    existing[field] = item[field]
                    existing_changed = True
            if existing_changed:
                write_json(existing_path, existing)
            item.update(
                {
                    "child_job_id": existing["job_id"],
                    "result": "skipped_existing" if existing.get("status") == "completed" else "processing",
                }
            )
        else:
            submitted = submit_content(str(item["canonical_url"]), root)
            child_path = root / str(submitted["manifest"])
            child = json.loads(child_path.read_text(encoding="utf-8"))
            child.update(
                {
                    "content_id": item["content_id"],
                    "parent_job_id": manifest["job_id"],
                    "title": item.get("title") or child.get("title"),
                    "published_at": item.get("published_at"),
                }
            )
            write_json(child_path, child)
            item.update({"child_job_id": submitted["job_id"], "result": "processing"})
        manifest.update({"status": "processing", "updated_at": utc_now()})
        write_json(manifest_path, manifest)
    return _refresh_official_batch_unlocked(manifest, manifest_path, root)


def submit_official_batch_children(manifest: dict, manifest_path: Path, root: Path) -> dict:
    descriptor = acquire_creator_batch_lock(manifest_path)
    if descriptor is None:
        return read_json_if_valid(manifest_path) or manifest
    try:
        current = read_json_if_valid(manifest_path) or manifest
        return _submit_official_batch_children_unlocked(current, manifest_path, root)
    finally:
        os.close(descriptor)


def discover_official_batch(manifest: dict, manifest_path: Path, root: Path) -> dict:
    data, content_type, final_url = fetch_limited(str(manifest["source"]), exact_hosts=ARTICLE_HOSTS, max_bytes=MAX_HTML_BYTES)
    if content_type not in {"text/html", "application/xhtml+xml"}:
        raise ArchiveError("unexpected_content_type", f"文章响应类型不是 HTML：{content_type}")
    reference = official_article_metadata(final_url, data)
    if not reference["biz"]:
        reference = known_official_account_for_source(str(manifest["source"]), root) or reference
    if not reference["biz"]:
        raise ArchiveError("official_account_not_identified", "参考文章和本次采集窗口都没有唯一识别出公众号。", 69)
    manifest["account"] = {"name": reference["account_name"], "account_id": reference["account_id"]}
    if reference["account_name"]:
        manifest["title"] = reference["account_name"]
    pagination = manifest["pagination"]
    offset = int(pagination.get("next_offset") or 0)
    try:
        page = channels_api("/api/mp/msg/list", query={"biz": reference["biz"], "offset": offset})
    except ArchiveError:
        first_authorization = int(pagination.get("pages") or 0) == 0
        manifest.update(
            {
                "status": "waiting_for_authorization" if first_authorization else "waiting_for_reauthentication",
                "updated_at": utc_now(),
                "next_action": "请启用已说明的微信公众号采集阶段，并在微信中打开参考文章；任务将从已保存游标继续。",
            }
        )
        write_json(manifest_path, manifest)
        return manifest

    known = {item["content_id"] for item in manifest.get("items") or []}
    for item in official_batch_page_items(page):
        if item["content_id"] not in known:
            manifest["items"].append(item)
            known.add(item["content_id"])
    has_more = bool(int(page.get("can_msg_continue") or 0))
    next_offset = int(page.get("next_offset") or 0)
    pagination.update(
        {
            "pages": int(pagination.get("pages") or 0) + 1,
            "next_offset": next_offset if has_more else None,
            "can_continue": has_more,
            "complete": not has_more,
        }
    )
    manifest["counts"]["discovered"] = len(manifest["items"])
    manifest.pop("next_action", None)
    if has_more:
        if next_offset <= offset:
            raise ArchiveError("official_account_cursor_invalid", "公众号分页游标没有前进。", 69)
        manifest.update({"status": "discovering", "updated_at": utc_now()})
        write_json(manifest_path, manifest)
        return manifest
    available = len(manifest["items"])
    manifest["items"].sort(key=lambda item: str(item.get("published_at") or ""), reverse=True)
    manifest.update(
        {
            "status": "awaiting_download_count",
            "updated_at": utc_now(),
            "selection": None,
            "next_action": f"该公众号当前可抓取文章共 {available} 篇，默认从最新开始。你要抓取多少篇？",
        }
    )
    write_json(manifest_path, manifest)
    return manifest


def _process_official_batch_unlocked(manifest_path: Path, root: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") not in {
        "queued",
        "discovering",
        "processing",
        "waiting_for_authorization",
        "waiting_for_reauthentication",
    }:
        return manifest
    try:
        if (manifest.get("pagination") or {}).get("complete"):
            selected_items = (manifest.get("items") or [])[: int((manifest.get("selection") or {}).get("limit") or 0)]
            if any(not item.get("child_job_id") for item in selected_items):
                return _submit_official_batch_children_unlocked(manifest, manifest_path, root)
            return _refresh_official_batch_unlocked(manifest, manifest_path, root)
        manifest.update({"status": "discovering", "updated_at": utc_now()})
        write_json(manifest_path, manifest)
        return discover_official_batch(manifest, manifest_path, root)
    except Exception as exc:
        manifest.update(
            {
                "status": "failed",
                "updated_at": utc_now(),
                "completed_at": utc_now(),
                "error": {"code": getattr(exc, "code", "unexpected_error"), "message": str(exc)},
            }
        )
        write_json(manifest_path, manifest)
        return manifest


def process_official_batch(manifest_path: Path, root: Path) -> dict:
    descriptor = acquire_creator_batch_lock(manifest_path)
    if descriptor is None:
        return read_json_if_valid(manifest_path) or {}
    try:
        return _process_official_batch_unlocked(manifest_path, root)
    finally:
        os.close(descriptor)


def download_official_batch_plan(job_id: str, limit: int, root: Path) -> dict:
    if not JOB_ID_RE.fullmatch(job_id):
        raise ArchiveError("invalid_job_id", "Job ID 格式错误。")
    manifest_path = root / "jobs" / job_id / "manifest.json"
    if not manifest_path.is_file():
        raise ArchiveError("job_not_found", "没有找到该任务。", 66)
    descriptor = acquire_creator_batch_lock(manifest_path)
    if descriptor is None:
        raise ArchiveError("job_busy", "该任务正在提交，请稍后查询同一任务状态。", 75)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("kind") != "batch"
            or manifest.get("platform") != "wechat_official_account"
            or manifest.get("status") != "awaiting_download_count"
        ):
            raise ArchiveError("job_not_waiting_for_count", "该任务当前不在等待抓取数量。", 66)
        items = manifest.get("items") or []
        if limit < 1 or limit > len(items):
            raise ArchiveError("invalid_download_count", f"抓取数量应在 1 到 {len(items)} 之间。")
        manifest.update(
            {
                "status": "processing",
                "updated_at": utc_now(),
                "selection": {"limit": limit, "order": "newest"},
            }
        )
        manifest.pop("next_action", None)
        write_json(manifest_path, manifest)
        manifest = _submit_official_batch_children_unlocked(manifest, manifest_path, root)
    finally:
        os.close(descriptor)
    return {
        "ok": True,
        "job_id": job_id,
        "status": manifest["status"],
        "platform": manifest["platform"],
        "counts": manifest["counts"],
        "manifest": archive_relative(root, manifest_path),
    }


def channel_author_result(root: Path, manifest: dict) -> dict:
    result = {
        "ok": True,
        "job_id": manifest["job_id"],
        "status": manifest["status"],
        "author": manifest["author"],
        "pages": manifest.get("pages", 0),
        "discovered": (manifest.get("counts") or {}).get("discovered", 0),
        "submitted": (manifest.get("counts") or {}).get("submitted", 0),
        "manifest": str(root / "jobs" / manifest["job_id"] / "manifest.json"),
    }
    if manifest.get("status") == "awaiting_download_count":
        result.update(
            {
                "available": (manifest.get("counts") or {}).get("eligible", 0),
                "question": (manifest.get("next_action") or "").split("。请询问", 1)[0] + "。你要下载多少个？",
            }
        )
    return result


def channel_creator_registry_path(root: Path) -> Path:
    return root / "state" / "channels-creators.json"


def channel_session_snapshot_path(root: Path) -> Path:
    return root / "state" / "channels-session.json"


def load_channel_session_snapshot(root: Path) -> dict:
    snapshot = read_json_if_valid(channel_session_snapshot_path(root)) or {}
    realtime_status = snapshot.get("realtime_status")
    if not isinstance(realtime_status, str) or not realtime_status:
        realtime_status = "unknown"
    registered_creators = snapshot.get("registered_creators")
    if not isinstance(registered_creators, int) or isinstance(registered_creators, bool) or registered_creators < 0:
        registered_creators = 0
    return {
        "schema_version": 1,
        "updated_at": snapshot.get("updated_at"),
        "realtime_status": realtime_status,
        "last_ready_at": snapshot.get("last_ready_at"),
        "registered_creators": registered_creators,
    }


def save_channel_session_snapshot(root: Path, realtime_status: str, registered_creators: int) -> dict:
    path = channel_session_snapshot_path(root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    lock_path = path.with_suffix(path.suffix + ".lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        with os.fdopen(descriptor, "r+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            previous = load_channel_session_snapshot(root)
            now = utc_now()
            snapshot = {
                "schema_version": 1,
                "updated_at": now,
                "realtime_status": realtime_status,
                "last_ready_at": now if realtime_status == "ready" else previous.get("last_ready_at"),
                "registered_creators": registered_creators,
            }
            private_atomic_write(path, (json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n").encode())
            return snapshot
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def try_save_channel_session_snapshot(root: Path, realtime_status: str, registered_creators: int) -> dict:
    try:
        return save_channel_session_snapshot(root, realtime_status, registered_creators)
    except OSError:
        return load_channel_session_snapshot(root)


def load_channel_creator_registry(root: Path) -> dict:
    path = channel_creator_registry_path(root)
    if not path.is_file():
        return {"schema_version": 1, "updated_at": None, "creators": {}, "sources": {}}
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArchiveError("channels_creator_registry_invalid", "视频号博主注册表无法读取。", 70) from exc
    if not isinstance(registry.get("creators"), dict) or not isinstance(registry.get("sources"), dict):
        raise ArchiveError("channels_creator_registry_invalid", "视频号博主注册表格式错误。", 70)
    return registry


def register_channel_creator(root: Path, source: str, creator: dict) -> None:
    username = str(creator.get("username") or "")
    if not username:
        raise ArchiveError("channel_author_not_found", "视频号博主身份为空。", 66)
    canonical_source = canonical_url(source) if source.startswith("https://") else source
    path = channel_creator_registry_path(root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    lock_path = path.with_suffix(path.suffix + ".lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        with os.fdopen(descriptor, "r+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            registry = load_channel_creator_registry(root)
            registry["creators"][username] = {
                "username": username,
                "nickname": str(creator.get("nickname") or ""),
                "avatar": str(creator.get("avatar") or ""),
                "signature": str(creator.get("signature") or ""),
                "last_source": canonical_source,
                "updated_at": utc_now(),
            }
            registry["sources"][canonical_source] = username
            registry["updated_at"] = utc_now()
            private_atomic_write(path, (json.dumps(registry, ensure_ascii=False, indent=2) + "\n").encode())
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def registered_channel_creator(root: Path, source: str) -> dict | None:
    registry = load_channel_creator_registry(root)
    canonical_source = canonical_url(source) if source.startswith("https://") else source
    username = registry["sources"].get(canonical_source)
    creator = registry["creators"].get(username) if username else None
    return dict(creator) if isinstance(creator, dict) else None


def resolve_channel_author(
    author: str,
    root: Path | None = None,
    deadline: float | None = None,
) -> dict:
    root = root or archive_root()
    if author.startswith("https://"):
        registered = registered_channel_creator(root, author)
        if registered:
            return registered
        creator = resolve_channel_author_from_url(author, deadline=deadline)
        register_channel_creator(root, author, creator)
        return creator
    if author.endswith("@finder"):
        registered = load_channel_creator_registry(root)["creators"].get(author)
        return dict(registered) if isinstance(registered, dict) else {"username": author, "nickname": ""}
    search = search_channel_author(author, deadline=deadline)
    candidates = search["candidates"]
    exact = [candidate for candidate in candidates if candidate["nickname"] == author]
    choices = exact or candidates
    if not choices:
        raise ArchiveError("channel_author_not_found", "没有找到该视频号博主。", 66)
    if len(choices) != 1:
        raise ArchiveError("channel_author_selection_required", "博主名称不唯一，请使用视频号分享链接。", 66)
    register_channel_creator(root, author, choices[0])
    return choices[0]


def discover_channel_author(selected: dict, deadline: float | None = None) -> tuple[list[list[dict]], int]:
    pages = []
    discovered = 0
    next_marker = ""
    while True:
        recovery_timeout(deadline)
        page = channels_payload_data(
            channels_api(
                "/api/channels/contact/feed/list",
                query={"username": selected["username"], "next_marker": next_marker},
                deadline=deadline,
            )
        )
        objects = page.get("object") or []
        discovered += len(objects)
        pages.append([obj for obj in objects if ordinary_channel_video(obj)])
        if not selected.get("nickname") and objects:
            selected["nickname"] = ((objects[0].get("contact") or {}).get("nickname") or selected["username"])
        next_marker = str(page.get("lastBuffer") or "")
        if page.get("continueFlag") == 0 or not next_marker:
            return pages, discovered


def inspect_channel_author(author: str, root: Path) -> dict:
    selected = resolve_channel_author(author, root)
    pages, discovered = discover_channel_author(selected)
    objects = [obj for page in pages for obj in page]
    available = len(objects)
    job_id, job_dir, manifest = new_job(root, "channel", author)
    manifest.update(
        {
            "status": "awaiting_download_count",
            "updated_at": utc_now(),
            "author": selected,
            "pages": len(pages),
            "inventory": {
                "captured_at": utc_now(),
                "items": [
                    {
                        "id": str(obj.get("id") or ""),
                        "title": str((obj.get("objectDesc") or {}).get("description") or "未命名视频"),
                        "payload": obj,
                    }
                    for obj in objects
                ],
            },
            "counts": {"discovered": discovered, "eligible": available, "selected": 0, "submitted": 0},
            "upstream_task_ids": [],
            "upstream_tasks": [],
            "auto_resume": False,
            "next_action": f"当前可下载视频共 {available} 个。请询问用户要下载多少个，收到数量后再继续同一 Job。",
        }
    )
    write_json(job_dir / "manifest.json", manifest)
    return {
        "ok": True,
        "job_id": job_id,
        "status": manifest["status"],
        "author": selected,
        "pages": len(pages),
        "available": available,
        "question": f"该博主当前可下载视频共 {available} 个。你要下载多少个？",
        "manifest": str(job_dir / "manifest.json"),
    }


def _inspect_channel_creator_unlocked(
    author: str,
    root: Path,
    *,
    existing_job: tuple[str, Path, dict] | None = None,
    deadline: float | None = None,
) -> dict:
    job_id, job_dir, manifest = existing_job or new_job(root, "batch", author)
    manifest.update({"kind": "creator_batch", "platform": "wechat_channels"})
    try:
        selected = resolve_channel_author(author, root, deadline=deadline)
        pages, _ = discover_channel_author(selected, deadline=deadline)
        objects = [obj for page in pages for obj in page]
        if not objects:
            raise ArchiveError("creator_inventory_empty", "该视频号博主当前没有可下载视频。", 66)
    except ArchiveError as exc:
        retryable = exc.code in {
            "channels_authorization_required",
            "channels_backend_unavailable",
            "channels_backend_error",
            "channels_share_resolve_failed",
            "recovery_window_expired",
        }
        manifest.update(
            {
                "status": "waiting_for_authorization" if retryable else "failed",
                "updated_at": utc_now(),
                "error": {"code": exc.code, "message": str(exc)},
            }
        )
        if retryable:
            manifest["next_action"] = "视频号会话暂不可用。任务已保存；请用户方便使用 Mac 时，在 Mac 微信中手动打开原链接并停留 10 秒，然后继续同一任务。"
        else:
            manifest["completed_at"] = utc_now()
            manifest.pop("next_action", None)
        write_json(job_dir / "manifest.json", manifest)
        return {
            "ok": retryable,
            "job_id": job_id,
            "status": manifest["status"],
            "platform": "wechat_channels",
            "next_action": manifest.get("next_action"),
            "manifest": archive_relative(root, job_dir / "manifest.json"),
        }
    items = [
        {
            "id": str(obj.get("id") or ""),
            "url": author,
            "title": str((obj.get("objectDesc") or {}).get("description") or "未命名视频"),
            "published_at": obj.get("createtime"),
            "payload": obj,
            "child_job_id": None,
        }
        for obj in objects
    ]
    manifest.update(
        {
            "kind": "creator_batch",
            "status": "awaiting_download_count",
            "updated_at": utc_now(),
            "platform": "wechat_channels",
            "creator": {
                "id": selected["username"],
                "name": selected.get("nickname", ""),
                "url": author,
            },
            "inventory": {"captured_at": utc_now(), "available": len(items), "items": items},
            "selection": None,
            "child_job_ids": [],
            "counts": {"selected": 0, "processing": 0, "completed": 0, "failed": 0},
            "next_action": f"该博主当前可下载视频共 {len(items)} 个，默认从最新开始。你要下载多少个？",
        }
    )
    manifest.pop("error", None)
    write_json(job_dir / "manifest.json", manifest)
    return {
        "ok": True,
        "job_id": job_id,
        "status": manifest["status"],
        "platform": "wechat_channels",
        "creator": manifest["creator"],
        "available": len(items),
        "question": manifest["next_action"],
        "manifest": archive_relative(root, job_dir / "manifest.json"),
    }


def inspect_channel_creator(
    author: str,
    root: Path,
    *,
    existing_job: tuple[str, Path, dict] | None = None,
    deadline: float | None = None,
) -> dict:
    if existing_job is None:
        return _inspect_channel_creator_unlocked(author, root, deadline=deadline)
    job_id, job_dir, manifest = existing_job
    manifest_path = job_dir / "manifest.json"
    descriptor = acquire_creator_batch_lock(manifest_path)
    if descriptor is None:
        current = read_json_if_valid(manifest_path) or manifest
        return {
            "ok": current.get("status") != "failed",
            "job_id": job_id,
            "status": current.get("status"),
            "platform": "wechat_channels",
            "manifest": archive_relative(root, manifest_path),
        }
    try:
        current = read_json_if_valid(manifest_path) or manifest
        remains_unselected_waiting = (
            current.get("status") in {"waiting_for_authorization", "waiting_for_reauthentication"}
            and not current.get("selection")
            and not current.get("child_job_ids")
        )
        if not remains_unselected_waiting:
            return {
                "ok": current.get("status") != "failed",
                "job_id": job_id,
                "status": current.get("status"),
                "platform": "wechat_channels",
                "manifest": archive_relative(root, manifest_path),
            }
        return _inspect_channel_creator_unlocked(
            author,
            root,
            existing_job=(job_id, job_dir, current),
            deadline=deadline,
        )
    finally:
        os.close(descriptor)


def download_channel_plan(job_id: str, limit: int, root: Path) -> dict:
    if not JOB_ID_RE.fullmatch(job_id):
        raise ArchiveError("invalid_job_id", "Job ID 格式错误。")
    manifest_path = root / "jobs" / job_id / "manifest.json"
    if not manifest_path.is_file():
        raise ArchiveError("job_not_found", "没有找到该任务。", 66)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("kind") != "channel" or manifest.get("status") != "awaiting_download_count":
        raise ArchiveError("job_not_waiting_for_count", "该任务当前不在等待下载数量。", 66)
    available = int((manifest.get("counts") or {}).get("eligible") or 0)
    if limit < 1 or limit > available:
        raise ArchiveError("invalid_download_count", f"下载数量应在 1 到 {available} 之间。")

    selected = manifest.get("author") or {}
    inventory = (manifest.get("inventory") or {}).get("items") or []
    objects = [item.get("payload") for item in inventory if isinstance(item.get("payload"), dict)]
    eligible = len(objects)
    requested = limit
    if requested > eligible:
        raise ArchiveError("channel_inventory_invalid", "本地冻结的视频号作品清单不完整，请重新盘点。", 69)

    manifest.update(
        {
            "status": "submitting",
            "updated_at": utc_now(),
            "selection": {"limit": requested},
            "auto_resume": True,
            "counts": {
                "discovered": int((manifest.get("counts") or {}).get("discovered") or eligible),
                "eligible": eligible,
                "selected": 0,
                "submitted": 0,
                "skipped": 0,
                "submission_failed": 0,
                "waiting": 0,
                "downloading": 0,
                "completed": 0,
                "failed": 0,
            },
        }
    )
    manifest.pop("next_action", None)
    output_dir = root / "video_channels" / safe_channel_name(selected.get("nickname") or selected["username"], "unknown_author")
    manifest["output_dir"] = str(output_dir)
    chosen_objects = objects[:requested]
    for start in range(0, requested, 15):
        chosen = chosen_objects[start : start + 15]
        records, task_ids = submit_channel_objects(chosen, output_dir)
        manifest["upstream_tasks"].extend(records)
        manifest["upstream_task_ids"].extend(task_ids)
        manifest["counts"]["selected"] += len(chosen)
        manifest["counts"]["submitted"] += sum(record["disposition"] == "submitted" for record in records)
        manifest["counts"]["skipped"] += sum(record["disposition"] == "skipped" for record in records)
        manifest["counts"]["submission_failed"] += sum(record["disposition"] == "failed" for record in records)
        write_json(manifest_path, manifest)
    manifest["upstream_task_ids"] = list(dict.fromkeys(manifest["upstream_task_ids"]))
    manifest["counts"]["failed"] = manifest["counts"]["submission_failed"]
    manifest["status"] = "queued" if manifest["upstream_task_ids"] else "failed"
    if manifest["status"] == "failed":
        manifest["completed_at"] = utc_now()
    write_json(manifest_path, manifest)
    return channel_author_result(root, manifest)


def channel_task_records(task_ids: list, *, auto_resume: bool) -> list[dict]:
    records = [channels_api("/api/v1/download_task/list", query={"task_id": task_id}) for task_id in task_ids]
    statuses = [record.get("status") for record in records]
    paused_ids = [int(record.get("id") or 0) for record in records if record.get("status") == 3]
    if auto_resume and paused_ids and not any(status in {0, 1, 2, 4} for status in statuses):
        channels_api("/api/v1/download_task/resume", body={"task_ids": paused_ids})
        records = [channels_api("/api/v1/download_task/list", query={"task_id": task_id}) for task_id in task_ids]
    return records


def refresh_channel_manifest(manifest: dict, manifest_path: Path, root: Path) -> dict:
    task_ids = manifest.get("upstream_task_ids") or []
    if not task_ids:
        return manifest
    records = channel_task_records(task_ids, auto_resume=manifest.get("auto_resume") is True)
    by_id = {int(record.get("id") or 0): record for record in records}
    waiting = downloading = paused = completed = failed = 0
    outputs = []
    for record in records:
        status = record.get("status")
        if status in {0, 1}:
            waiting += 1
        elif status in {2, 4}:
            downloading += 1
        elif status == 3:
            paused += 1
        elif status == 5:
            completed += 1
        elif status in {6, 7}:
            failed += 1
        files = []
        for item in record.get("files") or []:
            file_path = (Path(item.get("download_dir") or "") / (item.get("name") or "")).expanduser().absolute()
            try:
                exists = file_path.is_file()
            except OSError:
                exists = False
            file_record = {"path": str(file_path), "exists": exists, "status": item.get("status", "")}
            files.append(file_record)
            if file_record["exists"]:
                outputs.append(str(file_path))
        record["files"] = files
    for task in manifest.get("upstream_tasks") or []:
        if task.get("upstream_task_id") in by_id:
            current = by_id[task["upstream_task_id"]]
            task.update({"download_status": current.get("status"), "progress": current.get("progress", 0), "files": current.get("files", [])})
    counts = manifest.setdefault("counts", {})
    counts.update(
        {
            "waiting": waiting,
            "downloading": downloading,
            "paused": paused,
            "completed": completed,
            "failed": int(counts.get("submission_failed", 0)) + failed,
        }
    )
    manifest["outputs"] = list(dict.fromkeys(outputs))
    update_channel_transcriptions(manifest)
    if manifest.get("status") == "paused_by_user":
        pass
    elif downloading:
        manifest["status"] = "downloading"
    elif waiting:
        manifest["status"] = "queued"
    elif paused:
        manifest["status"] = "paused"
    elif counts["failed"] or (manifest.get("transcription_counts") or {}).get("failed"):
        manifest["status"] = "completed_with_failures" if completed else "failed"
    elif (manifest.get("transcription_counts") or {}).get("pending"):
        manifest["status"] = "transcribing"
    else:
        manifest["status"] = "completed"
    if manifest["status"] in {"completed", "completed_with_failures", "failed"}:
        manifest["completed_at"] = utc_now()
    else:
        manifest.pop("completed_at", None)
    manifest["tool_version"] = VERSION
    manifest["updated_at"] = utc_now()
    write_json(manifest_path, manifest)
    return manifest


def create_channel_task(url: str, root: Path) -> dict:
    safe_url = validate_https_url(url, {"channels.weixin.qq.com"}, CHANNEL_HOST_SUFFIXES)
    job_id, job_dir, manifest = new_job(root, "channel", safe_url)
    manifest.update(
        {
            "status": "waiting_for_manual_capture",
            "completed_at": utc_now(),
            "next_action": "请在微信客户端中打开并播放该视频；安全捕获 worker 尚未启用。",
        }
    )
    write_json(job_dir / "manifest.json", manifest)
    return {
        "ok": True,
        "job_id": job_id,
        "status": manifest["status"],
        "job_dir": str(job_dir),
        "next_action": manifest["next_action"],
    }


def resolve_inbox_file(root: Path, input_name: str) -> Path:
    if not INPUT_NAME_RE.fullmatch(input_name) or input_name in {".", ".."}:
        raise ArchiveError("invalid_input_name", "媒体文件名只能包含字母、数字、空格、点、下划线和连字符。")
    inbox = (root / "inbox").resolve()
    candidate = (inbox / input_name).resolve(strict=True)
    if candidate.parent != inbox or not candidate.is_file():
        raise ArchiveError("unsafe_input_path", "媒体文件必须直接位于归档 inbox 目录。")
    return candidate


def require_executable(config_name: str, default_name: str) -> str:
    configured = os.environ.get(config_name)
    resolved = shutil.which(configured) if configured else shutil.which(default_name)
    if not resolved:
        raise ArchiveError("missing_command", f"缺少命令：{configured or default_name}", 69)
    return resolved


def safe_subprocess_environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if key in SAFE_SUBPROCESS_ENV_KEYS}
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def safe_media_demuxer(source: Path) -> str:
    size = source.stat().st_size
    if size <= 0 or size > MAX_INPUT_MEDIA_BYTES:
        raise ArchiveError("media_size_limit", "媒体文件为空或超过大小限制。", 69)
    with source.open("rb") as handle:
        header = handle.read(16)
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return "mov"
    if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
        return "wav"
    if header.startswith(b"ID3") or (len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0):
        return "mp3"
    if header.startswith(b"OggS"):
        return "ogg"
    if header.startswith(b"\x1a\x45\xdf\xa3"):
        return "matroska"
    raise ArchiveError("unsafe_media_container", "只接受自包含的常见媒体容器。", 69)


def run_whisper(source: Path, root: Path, audio: Path, prefix: Path, flags: list[str], suffixes: tuple[str, ...]) -> tuple[Path, list[Path]]:
    demuxer = safe_media_demuxer(source)
    model_value = os.environ.get("WECHAT_WHISPER_MODEL", "").strip() or str(root / "models" / "ggml-small.bin")
    model = Path(model_value).expanduser().resolve(strict=True)
    if not model.is_file():
        raise ArchiveError("invalid_model", "WECHAT_WHISPER_MODEL 不是文件。", 69)
    ffmpeg = require_executable("WECHAT_FFMPEG", "ffmpeg")
    whisper = require_executable("WECHAT_WHISPER_CLI", "whisper-cli")
    try:
        subprocess.run(
            [ffmpeg, "-nostdin", "-y", "-loglevel", "error", "-protocol_whitelist", "file,pipe", "-f", demuxer, "-i", str(source), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", "-fs", str(MAX_AUDIO_BYTES), str(audio)],
            check=True,
            timeout=1800,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=safe_subprocess_environment(),
        )
        if not audio.is_file() or audio.stat().st_size < 44 or audio.stat().st_size >= MAX_AUDIO_BYTES - 4096:
            raise ArchiveError("audio_size_limit", "解码音频为空或达到大小限制。", 69)
        subprocess.run(
            [whisper, "-m", str(model), "-f", str(audio), "-l", "auto", *flags, "-of", str(prefix)],
            check=True,
            timeout=7200,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=safe_subprocess_environment(),
        )
        outputs = []
        output_bytes = 0
        for suffix in suffixes:
            path = prefix.with_suffix(suffix)
            if path.is_file():
                output_bytes += path.stat().st_size
                if output_bytes > MAX_TRANSCRIPT_BYTES:
                    raise ArchiveError("transcript_size_limit", "转写结果超过大小限制。", 69)
                outputs.append(path)
        if not outputs:
            raise ArchiveError("transcript_missing", "转写命令成功退出，但没有生成转录文件。", 70)
        return model, outputs
    finally:
        audio.unlink(missing_ok=True)


def channel_transcription_record(source: Path) -> dict:
    target = source.with_suffix(".txt")
    if target.is_file():
        return {"source": str(source), "path": str(target), "status": "completed", "sha256": sha256_file(target)}
    return {"source": str(source), "path": str(target), "status": "pending"}


def transcribe_channel_video(source: Path, root: Path, work_dir: Path) -> dict:
    target = source.with_suffix(".txt")
    token = uuid.uuid4().hex
    prefix = work_dir / f".channel-transcript-{token}"
    try:
        model, generated = run_whisper(
            source,
            root,
            work_dir / f".channel-audio-{token}.wav",
            prefix,
            ["-otxt"],
            (".txt",),
        )
        atomic_write(target, generated[0].read_bytes())
        return {
            "source": str(source),
            "path": str(target),
            "status": "completed",
            "model": model.name,
            "sha256": sha256_file(target),
        }
    except (ArchiveError, OSError, subprocess.SubprocessError) as exc:
        return {
            "source": str(source),
            "path": str(target),
            "status": "failed",
            "attempt_pid": os.getpid(),
            "error": {"code": getattr(exc, "code", "transcription_failed"), "message": str(exc)},
        }
    finally:
        prefix.with_suffix(".txt").unlink(missing_ok=True)


def update_channel_transcriptions(manifest: dict) -> None:
    records = []
    for value in manifest.get("outputs", []):
        source = Path(value)
        if source.suffix.lower() != ".mp4" or not source.is_file():
            continue
        records.append(channel_transcription_record(source))
    manifest["transcriptions"] = records
    manifest["transcription_counts"] = {
        status: sum(item["status"] == status for item in records) for status in ("completed", "pending", "failed")
    }


def transcribe_channel_directory_once(root: Path) -> tuple[dict, bool]:
    video_root = root / "video_channels"
    work_dir = root / "jobs" / "channel-transcriber"
    manifest_path = work_dir / "manifest.json"
    video_root.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    previous = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    previous_by_source = {item.get("source"): item for item in previous.get("outputs", [])}
    sources = sorted(video_root.rglob("*.mp4"), key=lambda path: (path.stat().st_size, str(path)))
    records = []
    processed = False
    last_completed_at = previous.get("last_completed_at")
    for source in sources:
        stat = source.stat()
        fingerprint = {"source_bytes": stat.st_size, "source_mtime_ns": stat.st_mtime_ns}
        record = channel_transcription_record(source)
        old = previous_by_source.get(str(source), {})
        if record["status"] == "pending" and old.get("status") == "failed" and old.get("attempt_pid") == os.getpid() and all(old.get(key) == value for key, value in fingerprint.items()):
            record = old
        elif record["status"] == "pending" and not processed:
            processed = True
            record = transcribe_channel_video(source, root, work_dir)
            if record["status"] == "completed":
                last_completed_at = utc_now()
        record.update(fingerprint)
        records.append(record)
    counts = {status: sum(item["status"] == status for item in records) for status in ("completed", "pending", "failed")}
    manifest = {
        "schema_version": 1,
        "tool_version": VERSION,
        "kind": "channel_transcriber",
        "status": "running",
        "pid": os.getpid(),
        "watch_dir": str(video_root),
        "updated_at": utc_now(),
        "last_completed_at": last_completed_at,
        "counts": {"total": len(records), **counts},
        "outputs": records,
    }
    write_json(manifest_path, manifest)
    return manifest, processed


def progress_channel_jobs_once(root: Path) -> None:
    for manifest_path in sorted((root / "jobs").glob("channel-*/manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (
                manifest.get("kind") != "channel"
                or manifest.get("auto_resume") is not True
                or manifest.get("status") in {"completed", "completed_with_failures", "failed", "paused_by_user"}
            ):
                continue
            refresh_channel_manifest(manifest, manifest_path, root)
        except (ArchiveError, OSError, json.JSONDecodeError):
            continue


def watch_channel_transcripts(root: Path, interval: int, once: bool) -> dict:
    if interval < 1:
        raise ArchiveError("invalid_interval", "轮询间隔必须至少为 1 秒。")
    while True:
        manifest, processed = transcribe_channel_directory_once(root)
        if once:
            manifest["status"] = "stopped"
            write_json(root / "jobs" / "channel-transcriber" / "manifest.json", manifest)
            return {"ok": True, "manifest": str(root / "jobs" / "channel-transcriber" / "manifest.json"), "counts": manifest["counts"]}
        if not processed:
            time.sleep(interval)


def channel_transcriber_status(root: Path) -> dict:
    manifest_path = root / "jobs" / "channel-transcriber" / "manifest.json"
    if not manifest_path.is_file():
        raise ArchiveError("transcriber_not_started", "无人值守转写 worker 尚未生成 manifest。", 66)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    worker = {key: manifest.get(key) for key in ("tool_version", "status", "pid", "updated_at", "last_completed_at", "counts")}
    return {"ok": True, "worker": worker, "manifest": str(manifest_path)}


def transcribe_media(input_name: str, root: Path) -> dict:
    source = resolve_inbox_file(root, input_name)
    job_id, job_dir, manifest = new_job(root, "media", input_name)
    try:
        model, paths = run_whisper(
            source,
            root,
            job_dir / "audio.wav",
            job_dir / "transcript",
            ["-otxt", "-osrt", "-oj"],
            (".txt", ".srt", ".json"),
        )
        outputs = [{"path": path.name, "sha256": sha256_file(path)} for path in paths]
        manifest.update(
            {
                "status": "completed",
                "completed_at": utc_now(),
                "input_sha256": sha256_file(source),
                "model": model.name,
                "outputs": outputs,
            }
        )
        write_json(job_dir / "manifest.json", manifest)
        return {"ok": True, "job_id": job_id, "status": "completed", "job_dir": str(job_dir), "outputs": outputs}
    except subprocess.TimeoutExpired as exc:
        error = ArchiveError("command_timeout", f"命令超时：{exc.cmd}", 70)
        manifest.update({"status": "failed", "completed_at": utc_now(), "error": {"code": error.code, "message": str(error)}})
        write_json(job_dir / "manifest.json", manifest)
        raise error from exc
    except subprocess.CalledProcessError as exc:
        error = ArchiveError("command_failed", f"命令退出码：{exc.returncode}", 70)
        manifest.update({"status": "failed", "completed_at": utc_now(), "error": {"code": error.code, "message": str(error)}})
        write_json(job_dir / "manifest.json", manifest)
        raise error from exc
    except Exception as exc:
        manifest.update(
            {
                "status": "failed",
                "completed_at": utc_now(),
                "error": {"code": getattr(exc, "code", "unexpected_error"), "message": str(exc)},
            }
        )
        write_json(job_dir / "manifest.json", manifest)
        raise


def job_status(job_id: str, root: Path) -> dict:
    if not JOB_ID_RE.fullmatch(job_id):
        raise ArchiveError("invalid_job_id", "Job ID 格式错误。")
    manifest_path = root / "jobs" / job_id / "manifest.json"
    if not manifest_path.is_file():
        raise ArchiveError("job_not_found", "没有找到该任务。", 66)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("kind") == "channel":
        manifest = refresh_channel_manifest(manifest, manifest_path, root)
    elif manifest.get("kind") == "creator_batch":
        manifest = refresh_creator_batch(manifest, manifest_path, root)
    elif manifest.get("kind") == "batch" and (manifest.get("pagination") or {}).get("complete"):
        manifest = refresh_official_batch(manifest, manifest_path, root)
    summary = {
        key: manifest.get(key)
        for key in ("job_id", "kind", "platform", "status", "title", "created_at", "updated_at", "completed_at", "counts")
        if manifest.get(key) is not None
    }
    if manifest.get("status") == "awaiting_download_count":
        items = manifest.get("items") or (manifest.get("inventory") or {}).get("items") or []
        summary["available"] = len(items)
        summary["question"] = manifest.get("next_action")
    elif manifest.get("status") in {"waiting_for_authorization", "waiting_for_reauthentication"}:
        summary["next_action"] = "请在方便使用 Mac 时恢复该平台授权，然后继续同一 Job；不要重复提交链接。"
    if manifest.get("error"):
        summary["error"] = {
            key: manifest["error"].get(key)
            for key in ("code", "stage")
            if manifest["error"].get(key) is not None
        }
    summary["outputs"] = [
        {key: output.get(key) for key in ("role", "bytes", "sha256") if output.get(key) is not None}
        for output in manifest.get("outputs") or []
    ]
    return {"ok": True, "job": summary}


def channel_session_status(
    root: Path | None = None,
    probe_keyword: str | None = None,
    deadline: float | None = None,
) -> dict:
    """Probe the existing local Channels session without opening WeChat."""
    root = root or archive_root()
    registry = load_channel_creator_registry(root)
    registered = len(registry["creators"])
    keyword = probe_keyword or f"__hermes_session_probe_{uuid.uuid4().hex}__"
    try:
        search_channel_author(keyword, deadline=deadline)
    except ArchiveError as exc:
        if exc.code == "channels_authorization_required":
            snapshot = try_save_channel_session_snapshot(root, "authorization_required", registered)
            return {
                "ok": False,
                "platform": "wechat_channels",
                "status": "authorization_required",
                "realtime_status": "authorization_required",
                "capabilities": {
                    "realtime_author_search": False,
                    "realtime_creator_feed": False,
                    "registered_creator_lookup": registered > 0,
                    "frozen_job_state": True,
                },
                "registered_creators": registered,
                "last_realtime_ready_at": snapshot.get("last_ready_at"),
                "next_action": "本地登记和冻结任务仍可使用；只有刷新最新作品或识别未登记博主时，才需要用户方便使用 Mac 时手动打开一次视频号链接。",
            }
        try_save_channel_session_snapshot(root, "unavailable", registered)
        return {
            "ok": False,
            "platform": "wechat_channels",
            "status": "unavailable",
            "error": {"code": exc.code, "message": str(exc)},
        }
    snapshot = try_save_channel_session_snapshot(root, "ready", registered)
    return {
        "ok": True,
        "platform": "wechat_channels",
        "status": "author_search_ready",
        "capabilities": {
            "author_search": True,
            "realtime_author_search": True,
            "registered_creator_lookup": registered > 0,
            "creator_feed": False,
            "new_creator_resolution": False,
            "frozen_job_state": True,
        },
        "registered_creators": registered,
        "last_realtime_ready_at": snapshot.get("last_ready_at"),
        "note": "只证明博主搜索接口可用；分享链接资料和作品列表将在真实任务中分别验证。",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    extract = subparsers.add_parser("extract")
    extract.add_argument("--url", required=True)
    official_batch = subparsers.add_parser("extract-official-account")
    official_batch.add_argument("--url", required=True)
    official_plan = subparsers.add_parser("download-official-account-plan")
    official_plan.add_argument("--job-id", required=True)
    official_plan.add_argument("--limit", required=True, type=int, help="总抓取篇数")
    article = subparsers.add_parser("archive-article")
    article.add_argument("--url", required=True)
    channel = subparsers.add_parser("capture-channel")
    channel.add_argument("--url", required=True)
    channel_url = subparsers.add_parser("download-channel-url")
    channel_url.add_argument("--url", required=True)
    channel_search = subparsers.add_parser("search-channel-author")
    channel_search.add_argument("--query", required=True)
    channel_inspect = subparsers.add_parser("inspect-channel-author")
    channel_inspect.add_argument("--author", required=True, help="视频号分享链接")
    channel_plan = subparsers.add_parser("download-channel-plan")
    channel_plan.add_argument("--job-id", required=True)
    channel_plan.add_argument("--limit", required=True, type=int, help="总下载数")
    creator_inspect = subparsers.add_parser("inspect-creator")
    creator_inspect.add_argument("--url", required=True)
    creator_plan = subparsers.add_parser("download-creator-plan")
    creator_plan.add_argument("--job-id", required=True)
    creator_plan.add_argument("--limit", required=True, type=int, help="总下载数")
    transcribe = subparsers.add_parser("transcribe")
    transcribe.add_argument("--input-name", required=True)
    watcher = subparsers.add_parser("watch-channel-transcripts")
    watcher.add_argument("--interval", type=int, default=10)
    watcher.add_argument("--once", action="store_true")
    content_watcher = subparsers.add_parser("watch-content")
    content_watcher.add_argument("--interval", type=int, default=10)
    content_watcher.add_argument("--once", action="store_true")
    cookie_import = subparsers.add_parser("import-browser-cookies")
    cookie_import.add_argument("--platform", required=True, choices=sorted(PLATFORM_COOKIE_DOMAINS))
    cookie_import.add_argument("--browser", required=True, choices=("safari", "chrome"))
    resume = subparsers.add_parser("resume")
    resume.add_argument("--job-id", required=True)
    recover_session = subparsers.add_parser("recover-channel-session")
    recover_session.add_argument("--timeout", type=int, default=300)
    recover_session.add_argument("--poll-interval", type=int, default=5)
    recover_session.add_argument("--started-at", type=float)
    recover_session.add_argument("--cleanup-reserve", type=int, default=0)
    subparsers.add_parser("channel-session-status")
    subparsers.add_parser("transcriber-status")
    subparsers.add_parser("content-worker-status")
    status = subparsers.add_parser("status")
    status.add_argument("--job-id", required=True)
    subparsers.add_parser("self-check")
    return parser


def self_check(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    validate_https_url("https://mp.weixin.qq.com/s/example", ARTICLE_HOSTS)
    try:
        validate_https_url("http://127.0.0.1/test", ARTICLE_HOSTS)
    except ArchiveError:
        pass
    else:
        raise ArchiveError("self_check_failed", "URL 边界检查失败。", 70)
    expected_platforms = {
        "https://weixin.qq.com/sph/example": "wechat_channels",
        "https://mp.weixin.qq.com/s/example": "wechat_official_account",
        "https://b23.tv/example": "bilibili",
        "http://xhslink.cn/o/example": "xiaohongshu",
        "https://v.douyin.com/example/": "douyin",
    }
    for url, expected in expected_platforms.items():
        if submission_url(url)[1] != expected:
            raise ArchiveError("self_check_failed", f"平台识别检查失败：{expected}", 70)
    core = transparent_core_root()
    sys.path.insert(0, str(core))
    try:
        from yt_dlp import YoutubeDL  # type: ignore[import-not-found]  # noqa: F401
    except ImportError as exc:
        raise ArchiveError("transparent_core_invalid", "透明派生核心无法加载。", 69) from exc
    model = Path(os.environ.get("WECHAT_WHISPER_MODEL", "").strip() or root / "models" / "ggml-small.bin").expanduser()
    return {
        "ok": True,
        "version": VERSION,
        "archive_root": str(root),
        "enabled": os.environ.get("WECHAT_ARCHIVE_ENABLED", "").strip().lower() in {"1", "true", "yes"},
        "transparent_core": str(core),
        "ffmpeg": shutil.which("ffmpeg"),
        "whisper_cli": shutil.which("whisper-cli"),
        "whisper_model": str(model) if model.is_file() else None,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = archive_root()
    try:
        if args.action in {"extract", "archive-article"}:
            require_enabled()
            result = submit_content(args.url, root)
        elif args.action == "extract-official-account":
            require_enabled()
            result = submit_official_batch(args.url, root)
        elif args.action == "download-official-account-plan":
            require_enabled()
            result = download_official_batch_plan(args.job_id, args.limit, root)
        elif args.action == "capture-channel":
            require_enabled()
            result = create_channel_task(args.url, root)
        elif args.action == "download-channel-url":
            require_enabled()
            result = download_channel_url(args.url, root)
        elif args.action == "search-channel-author":
            require_enabled()
            result = search_channel_author(args.query)
        elif args.action == "inspect-channel-author":
            require_enabled()
            result = inspect_channel_author(args.author, root)
        elif args.action == "download-channel-plan":
            require_enabled()
            result = download_channel_plan(args.job_id, args.limit, root)
        elif args.action == "inspect-creator":
            require_enabled()
            result = inspect_creator(args.url, root)
        elif args.action == "download-creator-plan":
            require_enabled()
            result = download_creator_plan(args.job_id, args.limit, root)
        elif args.action == "transcribe":
            require_enabled()
            result = transcribe_media(args.input_name, root)
        elif args.action == "watch-channel-transcripts":
            require_enabled()
            result = watch_channel_transcripts(root, args.interval, args.once)
        elif args.action == "watch-content":
            require_enabled()
            result = watch_content(root, args.interval, args.once)
        elif args.action == "import-browser-cookies":
            require_enabled()
            result = import_browser_cookies(args.platform, args.browser, root)
        elif args.action == "resume":
            require_enabled()
            result = resume_job(args.job_id, root)
        elif args.action == "recover-channel-session":
            require_enabled()
            result = recover_channel_session(
                root,
                args.timeout,
                args.poll_interval,
                started_at=args.started_at,
                cleanup_reserve=args.cleanup_reserve,
            )
        elif args.action == "channel-session-status":
            require_enabled()
            result = channel_session_status(root)
        elif args.action == "transcriber-status":
            result = channel_transcriber_status(root)
        elif args.action == "content-worker-status":
            result = content_worker_status(root)
        elif args.action == "status":
            result = job_status(args.job_id, root)
        else:
            result = self_check(root)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except ArchiveError as exc:
        print(json.dumps({"ok": False, "error": {"code": exc.code, "message": str(exc)}}, ensure_ascii=False))
        return exc.exit_code
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": {"code": "local_error", "message": str(exc)}}, ensure_ascii=False))
        return 70


if __name__ == "__main__":
    sys.exit(main())
