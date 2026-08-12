<div align="center">

[简体中文](./README.md) · **English**

# Link Video Downloader by ZhenxiangAI

**Multi-platform link download, image-text archiving, and raw transcription**

**WeChat Channels · Bilibili · Xiaohongshu · Douyin**

Submit one link and keep the video, image-text post, timeline-aligned raw transcript, and traceable manifest on your own Mac.

![Local content archive flow](./docs/assets/social-preview.jpg)

[![macOS Apple Silicon](https://img.shields.io/badge/macOS-Apple%20Silicon-111827?logo=apple&logoColor=white)](#runtime-boundary)
[![Latest Release](https://img.shields.io/github/v/release/Zhenxiangai/link-video-downloader-zhenxiangai?label=release&color=8b5cf6)](https://github.com/Zhenxiangai/link-video-downloader-zhenxiangai/releases/latest)
[![Local first](https://img.shields.io/badge/data-local--first-10b981)](#content-packages)
[![Project License](https://img.shields.io/badge/original%20code-MIT-f59e0b)](./LICENSE)

</div>

> [!IMPORTANT]
> The project is now **Link Video Downloader by ZhenxiangAI**. `v1.0.2` retains the internal Hermes Skill ID `wechat-archive` and existing local directories, so installed users can upgrade in place.

## Platform matrix

| Platform | Single link | History batch | Formal package |
|---|---|---|---|
| WeChat Channels | Video | Existing creator search and currently visible history remain available | `video.mp4` + three raw transcript formats |
| Bilibili | Video | Later release | `video.mp4` + three raw transcript formats |
| Xiaohongshu | Image-text or video | Later release | `正文.md` + `配图/`, or video and transcripts |
| Douyin | Video | Later release | `video.mp4` + three raw transcript formats |

A formal video directory contains exactly one `video.mp4`. `原始逐字稿.txt` is mechanically generated from timestamped JSON segments in chronological order; SRT and JSON are retained. Raw transcripts are not polished, corrected, summarized, or translated.

Bilibili uses the repository-pinned transparent derivative core by default and switches to the runtime API/CDN fallback only when core extraction fails. Both routes have completed real validation; a task retains only the single video from its successful route.

The existing WeChat Official Account single-article and history-batch code, tasks, and archives are retained, but collection is paused outside the current video-release baseline and will resume in a later iteration.

## Unified CLI

```bash
export WECHAT_ARCHIVE_ENABLED=1
SCRIPT="$PWD/scripts/wechat_archive.py"
PYTHON="${HERMES_HOME:-$HOME/.hermes}/hermes-agent/venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"

"$PYTHON" "$SCRIPT" extract --url '<one supported URL>'
"$PYTHON" "$SCRIPT" status --job-id 'content-YYYYMMDDTHHMMSSZ-1234abcd'
```

`extract` immediately returns one `content-*` job and a relative manifest path. The persistent content worker then downloads, archives, and transcribes sequentially.

Legacy `download-channel-url` remains a unified-entry alias. Channels creator history and manual transcription retain their dedicated commands. See [SKILL.md](./SKILL.md) for routing.

## Content packages

```text
~/Documents/WeChatArchive/
├── content/
│   ├── 视频号/<title>--<content-id>/
│   ├── B站/<title>--<content-id>/
│   ├── 小红书/<title>--<content-id>/
│   └── 抖音/<title>--<content-id>/
├── jobs/
│   ├── content-.../manifest.json
│   ├── content-worker/manifest.json
│   └── channel-transcriber/manifest.json
├── models/ggml-small.bin
└── video_channels/       # retained legacy Channels batch layout
```

Each central manifest records status, title, content type, canonical URL, actual route, relative artifact paths, byte counts, and SHA-256 hashes.

## Install and upgrade in place

From a repository checkout, run:

```bash
sh ./scripts/bootstrap.sh doctor
sh ./scripts/bootstrap.sh install
sh ./scripts/bootstrap.sh status
```

`install` reuses Hermes' managed Python and obtains or reuses the pinned transparent derivative core, FFmpeg, whisper.cpp, the pinned model, and the Channels backend; no separate global Python is required. The core comes from an immutable commit and is verified as a complete source tree. It manages three user LaunchAgents: the backend, the legacy Channels transcriber, and the central content worker. Installation itself does not import Cookies, install a CA, change the system proxy, or log in to any account.

New computers and existing users install or upgrade the same Skill and rerun the three commands above:

```bash
hermes skills install 'https://raw.githubusercontent.com/Zhenxiangai/link-video-downloader-zhenxiangai/v1.0.2/SKILL.md' --category social-media --name wechat-archive --force --yes
```

Existing `article-*`, `batch-*`, `channel-*`, `media-*`, `video_channels/`, and manifests remain in place. V1 adds no migrator, automatic updater, or rollback manager.

## Authentication and approvals

- Bilibili, Xiaohongshu, and Douyin can import a platform-specific persistent Cookie jar from an already signed-in Safari or Chrome only after explicit approval; a new Mac does not need Chrome installed for this. Ordinary jobs read the jar, not the browser.
- When Channels capture is needed, the local CA, HTTP/HTTPS proxy, and WeChat login remain separately approved actions.
- A job keeps the same ID when it enters `waiting_for_authorization` or `waiting_for_reauthentication`.
- Cookies, account credentials, browser profiles, CA private keys, and proxy snapshots never enter Git, content packages, manifests, or Hermes responses.

## Current acceptance status

The new unified path has completed real Bilibili 1080p video, Xiaohongshu image-text and video, Douyin video, and WeChat Channels 1080p video. Every verified video package contains one formal video and three Chinese-named transcript files. The Xiaohongshu image-text package contains `正文.md` and four valid images. Every formal artifact matches the byte count and SHA-256 recorded in its manifest.

`v1.0.1` closed the Hermes URL-install gap. `v1.0.2` changes only the public brand and repository address and makes pinned-core extraction independent of the repository directory name. The internal Skill ID, local data, and four-platform capability boundary remain unchanged.

## Runtime boundary

- V1 supports Apple Silicon Macs only. Windows, Linux, Intel Macs, a GUI, and a web console are out of scope.
- Platform pages, internal interfaces, and anti-bot rules can change. A pinned source snapshot survives upstream removal, but future platform changes still require maintenance.
- “Complete history” means content currently visible and accessible to the signed-in account; it excludes deleted, private, unauthorized paid, or risk-control-blocked content.
- Software licenses do not grant rights to scrape, republish, distribute, or commercially use platform content. Users remain responsible for account terms and content rights.

## Licensing and distribution

The root [MIT License](./LICENSE) covers ZhenxiangAI original files and files explicitly under MIT only. The vendored ZhenxiangAI transparent derivative core retains the real `yt-dlp 2026.07.04` source, Unlicense, upstream attribution, and complete third-party license text.

Releases contain auditable source and notices only. They exclude FFmpeg, whisper.cpp, model weights, the restricted Channels backend binary, Cookies, CAs, login state, and real archives. Read [THIRD_PARTY_NOTICES.md](./THIRD_PARTY_NOTICES.md).

## Contributing

Real breakage samples, documentation improvements, and platform-adapter Issues are welcome. Never upload Cookies, certificates, account data, real media/transcripts, or manifests containing personal paths.
