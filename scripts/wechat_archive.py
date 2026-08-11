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
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

VERSION = "0.5.0"
CHANNELS_API_BASE = "http://127.0.0.1:2022"
CHANNEL_VIDEO_MEDIA_TYPE = 4
ARTICLE_HOSTS = {"mp.weixin.qq.com"}
MEDIA_HOST_SUFFIXES = (".qpic.cn", ".qq.com", ".weixin.qq.com")
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
JOB_ID_RE = re.compile(r"^(article|channel|media)-\d{8}T\d{6}Z-[0-9a-f]{8}$")
INPUT_NAME_RE = re.compile(r"^[A-Za-z0-9._ -]{1,180}$")


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
        raise ArchiveError("fetch_failed", f"下载失败：{exc}", 69) from exc


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


def channels_api(path: str, *, query: dict | None = None, body: dict | None = None) -> dict:
    url = CHANNELS_API_BASE + path
    if query:
        url += "?" + urlencode(query)
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    request = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST" if data else "GET")
    try:
        with build_opener().open(request, timeout=20) as response:
            payload = json.loads(response.read().decode())
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ArchiveError("channels_backend_unavailable", f"视频号采集后端不可用：{exc}", 69) from exc
    if not isinstance(payload, dict) or payload.get("code") != 0:
        message = payload.get("msg", "视频号采集后端返回错误。") if isinstance(payload, dict) else "视频号采集后端响应无效。"
        raise ArchiveError("channels_backend_error", str(message), 69)
    return payload.get("data") or {}


def channels_payload_data(payload: dict) -> dict:
    if payload.get("errCode", 0) != 0:
        raise ArchiveError("channels_backend_error", str(payload.get("errMsg") or "视频号接口返回错误。"), 69)
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
        with build_opener().open(request, timeout=20) as response:
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
    safe_url = validate_https_url(url, {"weixin.qq.com", "channels.weixin.qq.com"})
    job_id, job_dir, manifest = new_job(root, "channel", safe_url)
    try:
        eid = resolve_channel_share_eid(safe_url)
        profile = channels_payload_data(channels_api("/api/channels/feed/profile", query={"eid": eid}))
        obj = profile.get("object") or {}
        if not ordinary_channel_video(obj):
            raise ArchiveError("channel_video_not_found", "分享链接中没有找到普通视频。")
        contact = obj.get("contact") or {}
        nickname = str(contact.get("nickname") or "未知博主")
        output_dir = root / "video_channels" / safe_channel_name(nickname, "unknown_author")
        records, task_ids = submit_channel_objects([obj], output_dir)
        record = records[0]
        if record["disposition"] == "failed" or not task_ids:
            raise ArchiveError("channels_task_create_failed", record.get("message") or "创建视频下载任务失败。", 69)
        manifest.update(
            {
                "status": "queued",
                "author": {"nickname": nickname, "username": contact.get("username", "")},
                "output_dir": str(output_dir),
                "counts": {
                    "discovered": 1,
                    "eligible": 1,
                    "submitted": int(record["disposition"] == "submitted"),
                    "skipped": int(record["disposition"] == "skipped"),
                    "submission_failed": 0,
                    "waiting": 1,
                    "downloading": 0,
                    "completed": 0,
                    "failed": 0,
                },
                "upstream_task_ids": task_ids,
                "upstream_tasks": records,
            }
        )
        write_json(job_dir / "manifest.json", manifest)
        return {"ok": True, "job_id": job_id, "status": manifest["status"], "submitted": manifest["counts"]["submitted"], "manifest": str(job_dir / "manifest.json")}
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
    return {"ok": True, "job": manifest}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
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
    subparsers.add_parser("transcriber-status")
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
    model = Path(os.environ.get("WECHAT_WHISPER_MODEL", "").strip() or root / "models" / "ggml-small.bin").expanduser()
    return {
        "ok": True,
        "version": VERSION,
        "archive_root": str(root),
        "enabled": os.environ.get("WECHAT_ARCHIVE_ENABLED", "").strip().lower() in {"1", "true", "yes"},
        "ffmpeg": shutil.which("ffmpeg"),
        "whisper_cli": shutil.which("whisper-cli"),
        "whisper_model": str(model) if model.is_file() else None,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = archive_root()
    try:
        if args.action == "archive-article":
            require_enabled()
            result = archive_article(args.url, root)
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
        elif args.action == "transcriber-status":
            result = channel_transcriber_status(root)
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
