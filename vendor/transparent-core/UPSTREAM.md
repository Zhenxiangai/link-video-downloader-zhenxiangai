# ZhenxiangAI 透明派生核心来源

本目录保存一份可独立运行的固定内容提取核心。ZhenxiangAI 负责本项目中的统一入口、任务编排、发布与后续适配维护；本目录中的派生源码不被表述为 ZhenxiangAI 原创。

- Upstream: <https://github.com/yt-dlp/yt-dlp>
- Release: `2026.07.04`
- Audited commit: `fdec00e0bf530dc6c3cc7b1dd780e95d9ae460e9`
- Upstream credits: pukkandan, current yt-dlp maintainers, and contributors
- Upstream license: Unlicense
- Runtime package: `yt_dlp/`

本派生快照只保留 Bilibili、小红书、TikTok/抖音 extractor、明确拒绝其他 URL 的 Generic fallback，以及这些目标模块所需的运行库。其他上游 extractor 已从发行树移除。

`LICENSE` 与 `THIRD_PARTY_LICENSES.txt` 原样保留。项目运行时直接加载本目录，不要求上游仓库、Homebrew `yt-dlp` 或上游发布服务器继续存在。
