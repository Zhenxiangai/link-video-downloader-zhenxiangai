#!/usr/bin/env python3
"""Restricted local archive worker for Hermes."""

from __future__ import annotations

import argparse
import hashlib
import html
import ipaddress
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

sys.pycache_prefix = str(Path.home() / "Library" / "Caches" / "wechat-archive" / "pycache")

VERSION = "1.0.2"
TRANSPARENT_CORE_REVISION = "8c137bf1a56106a050f12567fe0ed587bccea042"
TRANSPARENT_CORE_SHA256 = "acccec7f474bfc605fe01113e2d06b28908c1602e877c5aa0985db39d6cb20d2"
CHANNELS_API_BASE = "http://127.0.0.1:2022"
CHANNEL_VIDEO_MEDIA_TYPE = 4
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
SENSITIVE_QUERY_KEYS = {"key", "uin", "pass_ticket"}
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


def require_enabled() -> None:
    if os.environ.get("WECHAT_ARCHIVE_ENABLED", "").strip().lower() not in {"1", "true", "yes"}:
        raise ArchiveError("archive_disabled", "本地归档尚未启用。请先确认飞书访问策略。", 69)


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def write_json(path: Path, value: dict) -> None:
    atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def new_job(root: Path, kind: str, source: str) -> tuple[str, Path, dict]:
    job_id = f"{kind}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    job_dir = root / "jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=False)
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
    query = urlencode([(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key.lower() not in SENSITIVE_QUERY_KEYS])
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
    return {
        "biz": biz,
        "account_id": hashlib.sha256(f"wechat_official_account\0{biz}".encode()).hexdigest()[:16] if biz else "",
        "account_name": nickname,
        "content_id": content_id,
        "canonical_url": article_url,
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
    manifest_path = job_dir / "manifest.json"
    write_json(manifest_path, manifest)
    return {
        "ok": True,
        "job_id": job_id,
        "status": "queued",
        "platform": platform,
        "manifest": archive_relative(root, manifest_path),
    }


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
    data, content_type, final_url = fetch_limited(safe_url, exact_hosts=ARTICLE_HOSTS, max_bytes=MAX_HTML_BYTES)
    if content_type not in {"text/html", "application/xhtml+xml"}:
        raise ArchiveError("unexpected_content_type", f"文章响应类型不是 HTML：{content_type}")
    article = official_article_metadata(final_url, data)
    manifest.update({"content_id": article["content_id"], "canonical_url": article["canonical_url"]})
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


def process_content_job(manifest_path: Path, root: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") not in {"queued", "downloading", "transcribing"}:
        return manifest
    manifest.update({"status": "downloading", "updated_at": utc_now()})
    write_json(manifest_path, manifest)
    try:
        if manifest.get("platform") == "wechat_channels":
            return process_channel_content(manifest, manifest_path, root)
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
        if isinstance(exc, ArchiveError) and exc.code in {"reauthentication_required", "channels_authorization_required"}:
            platform = str(manifest.get("platform"))
            cookie_jar = platform_cookie_jar(platform)
            channels = platform == "wechat_channels"
            manifest.update(
                {
                    "status": "waiting_for_authorization" if channels or not cookie_jar.is_file() else "waiting_for_reauthentication",
                    "updated_at": utc_now(),
                    "next_action": (
                        "用户发送此视频号链接已授权当前任务临时启用本机 CA 与采集代理。"
                        "请自动启用采集、在已登录微信中打开或刷新该视频，并恢复同一 Job ID；"
                        "任务成功或失败后立即关闭采集；如本任务改动了系统代理，则恢复原设置。"
                        "只有遇到微信未登录、macOS 权限弹窗或无法自动打开视频时才请求用户介入。"
                        "如果无法自动打开，只向用户发送下面的操作，不要追加排障说明：\n\n"
                        "请复制这个视频号链接，粘贴到这台 Mac 的微信聊天中，然后点开并播放：\n\n"
                        f"{manifest.get('source')}\n\n"
                        "视频开始播放后，请回复“已打开”，我会继续同一个任务。"
                        if channels
                        else "请在已授权的 Safari 或 Chrome 登录该平台后，导入持久 Cookie 并继续原任务。"
                    ),
                }
            )
            manifest.pop("completed_at", None)
            manifest.pop("failed_stage", None)
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


def content_worker_once(root: Path) -> tuple[dict, bool]:
    jobs_root = root / "jobs"
    jobs_root.mkdir(parents=True, exist_ok=True)
    processed = False
    for manifest_path in sorted(jobs_root.glob("content-????????T??????Z-????????/manifest.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("status") in {"queued", "downloading", "transcribing"}:
            process_content_job(manifest_path, root)
            processed = True
            break
    if not processed:
        active_batch_states = {
            "queued",
            "discovering",
            "processing",
            "waiting_for_authorization",
            "waiting_for_reauthentication",
        }
        for manifest_path in sorted(jobs_root.glob("batch-????????T??????Z-????????/manifest.json")):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("status") in active_batch_states:
                process_official_batch(manifest_path, root)
                processed = True
                break
    statuses = []
    for pattern in ("content-????????T??????Z-????????/manifest.json", "batch-????????T??????Z-????????/manifest.json"):
        for manifest_path in jobs_root.glob(pattern):
            statuses.append(json.loads(manifest_path.read_text(encoding="utf-8")).get("status"))
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
    return {"ok": True, "platform": platform, "browser": browser, "status": "imported", "cookie_count": len(filtered), "resumed_jobs": resumed}


def resume_job(job_id: str, root: Path) -> dict:
    if not JOB_ID_RE.fullmatch(job_id):
        raise ArchiveError("invalid_job_id", "Job ID 格式错误。")
    manifest_path = root / "jobs" / job_id / "manifest.json"
    if not manifest_path.is_file():
        raise ArchiveError("job_not_found", "没有找到该任务。", 66)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("kind") != "content" or manifest.get("platform") != "wechat_channels":
        raise ArchiveError("job_not_resumable", "只有完成视频号采集授权后才使用手动恢复。", 66)
    if manifest.get("status") not in {"waiting_for_authorization", "waiting_for_reauthentication"}:
        raise ArchiveError("job_not_waiting", "该任务当前不在授权等待状态。", 66)
    manifest.update({"status": "queued", "updated_at": utc_now()})
    manifest.pop("next_action", None)
    manifest.pop("error", None)
    write_json(manifest_path, manifest)
    return {"ok": True, "job_id": job_id, "status": "queued", "platform": manifest.get("platform")}


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
    _, _, final_url = fetch_limited(url, exact_hosts=exact_hosts, suffixes=suffixes, max_bytes=8 * 1024 * 1024)
    return final_url


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
    write_json(manifest_path, manifest)
    return manifest


def process_transparent_video(manifest: dict, manifest_path: Path, root: Path) -> dict:
    work_dir = manifest_path.parent / "work"
    platform = str(manifest["platform"])
    try:
        info, source_video = download_with_transparent_core(str(manifest["source"]), platform, work_dir)
        route = "transparent_core"
    except ArchiveError as exc:
        if platform != "bilibili" or exc.code not in {"transparent_core_failed", "reauthentication_required", "video_missing"}:
            raise
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


def channels_api(path: str, *, query: dict | None = None, body: dict | None = None) -> dict:
    url = CHANNELS_API_BASE + path
    if query:
        url += "?" + urlencode(query)
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    request = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST" if data else "GET")
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=20) as response:
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


def resolve_channel_share_eid(share_url: str) -> str:
    short_uri = urlsplit(share_url).path.rstrip("/").rsplit("/", 1)[-1]
    page_url = f"https://channels.weixin.qq.com/finder-preview/pages/sph?id={short_uri}"
    request = Request(
        "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info?" + urlencode({"_pageUrl": page_url}),
        data=json.dumps({"baseReq": {"generalToken": ""}, "shortUri": short_uri}).encode(),
        headers={"Content-Type": "application/json", "Origin": "https://channels.weixin.qq.com", "Referer": page_url},
        method="POST",
    )
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=20) as response:
            payload = json.loads(response.read().decode())
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ArchiveError("channels_share_resolve_failed", f"视频号分享链接解析失败：{exc}", 69) from exc
    eid = (((payload.get("data") or {}).get("sceneInfo") or {}).get("dynamicExportId"))
    if payload.get("errCode") != 0 or not eid:
        raise ArchiveError("channels_share_resolve_failed", str(payload.get("errMsg") or "分享链接没有返回视频标识。"), 69)
    return str(eid)


def search_channel_author(query: str) -> dict:
    data = channels_payload_data(channels_api("/api/channels/contact/search", query={"keyword": query}))
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
    return submit_content(url, root)


def ordinary_channel_video(obj: dict) -> bool:
    desc = obj.get("objectDesc") or {}
    return desc.get("mediaType") == CHANNEL_VIDEO_MEDIA_TYPE and bool(desc.get("media")) and not obj.get("liveInfo")


def submit_channel_objects(objects: list[dict], output_dir: Path) -> tuple[list[dict], list[int]]:
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


def process_channel_content(manifest: dict, manifest_path: Path, root: Path) -> dict:
    work_dir = manifest_path.parent / "work"
    task_ids = manifest.get("upstream_task_ids") or []
    if not task_ids:
        safe_url = validate_https_url(str(manifest["source"]), {"weixin.qq.com", "channels.weixin.qq.com"})
        eid = resolve_channel_share_eid(safe_url)
        profile = channels_payload_data(channels_api("/api/channels/feed/profile", query={"eid": eid}))
        obj = profile.get("object") or {}
        if not ordinary_channel_video(obj):
            raise ArchiveError("channel_video_not_found", "分享链接中没有找到普通视频。")
        records, task_ids = submit_channel_objects([obj], work_dir)
        if not task_ids:
            raise ArchiveError("channels_task_create_failed", records[0].get("message") or "创建视频下载任务失败。", 69)
        description = str((obj.get("objectDesc") or {}).get("description") or "未命名视频号视频")
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

    records = [channels_api("/api/v1/download_task/list", query={"task_id": task_id}) for task_id in task_ids]
    statuses = [record.get("status") for record in records]
    if any(status in {6, 7} for status in statuses):
        raise ArchiveError("channels_download_failed", "视频号后端下载任务失败。", 69)
    if not statuses or any(status != 5 for status in statuses):
        manifest.update({"status": "downloading", "updated_at": utc_now()})
        write_json(manifest_path, manifest)
        return manifest

    videos = []
    for record in records:
        for item in record.get("files") or []:
            path = (Path(item.get("download_dir") or "") / (item.get("name") or "")).expanduser().absolute()
            if path.is_file() and path.suffix.lower() == ".mp4":
                videos.append(path)
    videos = list(dict.fromkeys(videos))
    if len(videos) != 1:
        raise ArchiveError("channel_video_missing", "视频号后端未返回唯一的 MP4 文件。", 69)
    info = {"id": manifest["content_id"], "title": manifest["title"], "webpage_url": manifest["source"]}
    return finalize_video_content(manifest, manifest_path, root, info, videos[0], "wechat_channels_backend")


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
        child = json.loads(manifest_path.read_text(encoding="utf-8"))
        if child.get("platform") == "wechat_official_account" and child.get("content_id") == content_id:
            candidates.append(child)
    if not candidates:
        return None
    return next((child for child in candidates if child.get("status") == "completed"), candidates[0])


def refresh_official_batch(manifest: dict, manifest_path: Path, root: Path) -> dict:
    completed = unavailable = failed = processing = 0
    skipped = sum(item.get("result") == "skipped_existing" for item in manifest.get("items") or [])
    for item in manifest.get("items") or []:
        if item.get("result") == "skipped_existing":
            continue
        child_id = item.get("child_job_id")
        child_path = root / "jobs" / str(child_id) / "manifest.json"
        if not child_id or not child_path.is_file():
            processing += 1
            continue
        child = json.loads(child_path.read_text(encoding="utf-8"))
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
        "submitted": discovered - skipped,
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


def submit_official_batch_children(manifest: dict, manifest_path: Path, root: Path) -> dict:
    for item in manifest.get("items") or []:
        if item.get("child_job_id"):
            continue
        existing = existing_official_content(root, str(item["content_id"]))
        if existing:
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
            child.update({"content_id": item["content_id"], "parent_job_id": manifest["job_id"]})
            write_json(child_path, child)
            item.update({"child_job_id": submitted["job_id"], "result": "processing"})
        manifest.update({"status": "processing", "updated_at": utc_now()})
        write_json(manifest_path, manifest)
    return refresh_official_batch(manifest, manifest_path, root)


def discover_official_batch(manifest: dict, manifest_path: Path, root: Path) -> dict:
    data, content_type, final_url = fetch_limited(str(manifest["source"]), exact_hosts=ARTICLE_HOSTS, max_bytes=MAX_HTML_BYTES)
    if content_type not in {"text/html", "application/xhtml+xml"}:
        raise ArchiveError("unexpected_content_type", f"文章响应类型不是 HTML：{content_type}")
    reference = official_article_metadata(final_url, data)
    if not reference["biz"]:
        raise ArchiveError("official_account_not_identified", "参考文章没有识别出公众号。", 69)
    manifest["account"] = {"name": reference["account_name"], "account_id": reference["account_id"]}
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
    manifest.update({"status": "processing", "updated_at": utc_now()})
    write_json(manifest_path, manifest)
    return submit_official_batch_children(manifest, manifest_path, root)


def process_official_batch(manifest_path: Path, root: Path) -> dict:
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
            if any(not item.get("child_job_id") for item in manifest.get("items") or []):
                return submit_official_batch_children(manifest, manifest_path, root)
            return refresh_official_batch(manifest, manifest_path, root)
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


def download_channel_author(author: str, root: Path) -> dict:
    if author.endswith("@finder"):
        selected = {"username": author, "nickname": ""}
    else:
        search = search_channel_author(author)
        candidates = search["candidates"]
        exact = [candidate for candidate in candidates if candidate["nickname"] == author]
        choices = exact or candidates
        if not choices:
            raise ArchiveError("channel_author_not_found", "没有找到该视频号博主。", 66)
        if len(choices) != 1:
            return {"ok": True, "status": "author_selection_required", "query": author, "candidates": choices}
        selected = choices[0]

    username = selected["username"]
    job_id, job_dir, manifest = new_job(root, "channel", username)
    manifest.update(
        {
            "author": selected,
            "pages": 0,
            "counts": {
                "discovered": 0,
                "eligible": 0,
                "submitted": 0,
                "skipped": 0,
                "submission_failed": 0,
                "waiting": 0,
                "downloading": 0,
                "completed": 0,
                "failed": 0,
            },
            "upstream_task_ids": [],
            "upstream_tasks": [],
        }
    )
    next_marker = ""
    try:
        while True:
            page = channels_payload_data(
                channels_api(
                    "/api/channels/contact/feed/list",
                    query={"username": username, "next_marker": next_marker},
                )
            )
            objects = page.get("object") or []
            manifest["pages"] += 1
            manifest["counts"]["discovered"] += len(objects)
            eligible = [obj for obj in objects if ordinary_channel_video(obj)]
            manifest["counts"]["eligible"] += len(eligible)
            if not selected.get("nickname") and objects:
                selected["nickname"] = ((objects[0].get("contact") or {}).get("nickname") or username)
            output_dir = root / "video_channels" / safe_channel_name(selected.get("nickname") or username, "unknown_author")
            records, task_ids = submit_channel_objects(eligible, output_dir)
            manifest["output_dir"] = str(output_dir)
            manifest["upstream_tasks"].extend(records)
            manifest["upstream_task_ids"].extend(task_ids)
            manifest["counts"]["submitted"] += sum(record["disposition"] == "submitted" for record in records)
            manifest["counts"]["skipped"] += sum(record["disposition"] == "skipped" for record in records)
            failed = sum(record["disposition"] == "failed" for record in records)
            manifest["counts"]["submission_failed"] += failed
            manifest["counts"]["failed"] = manifest["counts"]["submission_failed"]
            write_json(job_dir / "manifest.json", manifest)
            next_marker = str(page.get("lastBuffer") or "")
            if page.get("continueFlag") == 0 or not next_marker:
                break

        manifest["upstream_task_ids"] = list(dict.fromkeys(manifest["upstream_task_ids"]))
        manifest["status"] = "queued" if manifest["upstream_task_ids"] else ("failed" if manifest["counts"]["failed"] else "completed")
        if manifest["status"] in {"failed", "completed"}:
            manifest["completed_at"] = utc_now()
        write_json(job_dir / "manifest.json", manifest)
        return {
            "ok": True,
            "job_id": job_id,
            "status": manifest["status"],
            "author": selected,
            "pages": manifest["pages"],
            "discovered": manifest["counts"]["discovered"],
            "submitted": manifest["counts"]["submitted"],
            "manifest": str(job_dir / "manifest.json"),
        }
    except (ArchiveError, OSError, ValueError) as exc:
        manifest.update(
            {
                "status": "failed",
                "completed_at": utc_now(),
                "error": {"code": getattr(exc, "code", "local_error"), "message": str(exc)},
            }
        )
        write_json(job_dir / "manifest.json", manifest)
        raise


def refresh_channel_manifest(manifest: dict, manifest_path: Path, root: Path) -> dict:
    task_ids = manifest.get("upstream_task_ids") or []
    if not task_ids:
        return manifest
    records = [channels_api("/api/v1/download_task/list", query={"task_id": task_id}) for task_id in task_ids]
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
    if downloading:
        manifest["status"] = "downloading"
    elif waiting:
        manifest["status"] = "queued"
    elif paused:
        manifest["status"] = "paused"
    elif counts["failed"]:
        manifest["status"] = "completed_with_failures" if completed else "failed"
    else:
        manifest["status"] = "completed"
    if manifest["status"] in {"completed", "completed_with_failures", "failed"}:
        manifest["completed_at"] = utc_now()
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
    elif manifest.get("kind") == "batch" and (manifest.get("pagination") or {}).get("complete"):
        manifest = refresh_official_batch(manifest, manifest_path, root)
    return {"ok": True, "job": manifest}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    extract = subparsers.add_parser("extract")
    extract.add_argument("--url", required=True)
    official_batch = subparsers.add_parser("extract-official-account")
    official_batch.add_argument("--url", required=True)
    article = subparsers.add_parser("archive-article")
    article.add_argument("--url", required=True)
    channel = subparsers.add_parser("capture-channel")
    channel.add_argument("--url", required=True)
    channel_url = subparsers.add_parser("download-channel-url")
    channel_url.add_argument("--url", required=True)
    channel_search = subparsers.add_parser("search-channel-author")
    channel_search.add_argument("--query", required=True)
    channel_author = subparsers.add_parser("download-channel-author")
    channel_author.add_argument("--author", required=True)
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
        elif args.action == "capture-channel":
            require_enabled()
            result = create_channel_task(args.url, root)
        elif args.action == "download-channel-url":
            require_enabled()
            result = download_channel_url(args.url, root)
        elif args.action == "search-channel-author":
            require_enabled()
            result = search_channel_author(args.query)
        elif args.action == "download-channel-author":
            require_enabled()
            result = download_channel_author(args.author, root)
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
