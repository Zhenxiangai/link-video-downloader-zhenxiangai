# 一站式多平台视频内容提取器 / Launch Kit

统一仓库：<https://github.com/Zhenxiangai/wechat-archive>

分享配图：[social-preview.jpg](./assets/social-preview.jpg)

> `v1.0.0` 真实标签发布后，机器安装入口为 <https://raw.githubusercontent.com/Zhenxiangai/wechat-archive/v1.0.0/SKILL.md>。标签出现前不发布该链接。

## 中文短帖

我开源了 **一站式多平台视频内容提取器**：把微信视频号、B站、小红书或抖音链接发给 Hermes，它会在 Apple Silicon Mac 上生成整洁的本地内容包。

视频目录只保留一个正式视频，并生成带时间线的 `原始逐字稿.txt/.srt/.json`；小红书图文保留正文与配图。视频号保留当前可见历史批量能力，公众号代码保留但不属于本次 V1 发布基线。

内容、Cookie 和登录态默认留在用户自己的 Mac；Chrome Cookie 导入、微信 CA/代理和登录都是分开授权动作。

GitHub：<https://github.com/Zhenxiangai/wechat-archive>

## 中文长帖

### 标题

开源：一个项目提取视频号、B站、小红书和抖音

### 正文

做内容研究和知识库时，真正麻烦的不是“下载某一条视频”，而是不同平台有不同入口、登录态、文件名和失败状态。

所以我把现有 `wechat-archive` 扩展成了 **一站式多平台视频内容提取器**。

它目前可以：

- 用同一个 `extract --url` 自动识别视频号、B站、小红书和抖音；
- 把视频、正文、配图和任务清单放在标题目录中；
- 对视频生成带时间线的三种中文名原始逐字稿；
- 只保留一个正式视频，不把备用路线产物复制进目录；
- 使用 `content-*` / `batch-*` 和 `manifest.json` 跟踪下载、转写、授权等待、完成与失败；
- 保留视频号博主当前可见历史批量；
- 通过 Hermes 自然语言安装、提交和查询任务。

新统一链路已真实跑通 B站 1080P、小红书图文/视频、抖音视频和视频号 1080P。四个视频任务都只有一个正式视频和三种中文名逐字稿，所有正式产物均与 manifest 的字节数和 SHA-256 一致。

项目坚持 local-first。它不会把 Cookie、证书、系统代理或登录藏在一次静默安装中；Hermes 会先说明变化，再等用户批准、点击或亲自登录。

仓库：<https://github.com/Zhenxiangai/wechat-archive>

## English short post

I open-sourced a **one-stop multi-platform video content extractor** for Apple Silicon Macs.

Give Hermes one WeChat Channels, Bilibili, Xiaohongshu, or Douyin link. It creates an organized local package with one formal video when applicable, Chinese-named timestamped raw TXT/SRT/JSON transcripts, Xiaohongshu post text and images, plus a traceable manifest.

Channels creator history remains available. Official Account code is retained but paused outside the V1 release baseline. Cookies, CAs, proxy changes, and account login remain separate approvals.

GitHub: <https://github.com/Zhenxiangai/wechat-archive>

## English long post

### Title

Show HN: One local extractor for WeChat, Bilibili, Xiaohongshu, and Douyin

### Body

I expanded **wechat-archive** from a WeChat Channels workflow into a one-stop local content extractor for WeChat Channels, Bilibili, Xiaohongshu, and Douyin.

One `extract --url` command identifies the platform and creates a central job. The resident worker produces an organized content package: exactly one formal video when applicable, Chinese-named timestamped raw TXT/SRT/JSON transcripts, article Markdown and local images, plus a manifest with status, byte counts, and hashes.

The project retains Channels creator-history batches. Bilibili, Xiaohongshu, and Douyin use a pinned transparent derivative core that remains runnable if its upstream disappears. Official Account code is retained but paused outside the V1 release baseline.

The unified path has completed real Bilibili 1080p, Xiaohongshu image-text/video, Douyin video, and WeChat Channels 1080p. Every video package has exactly one formal video and three Chinese-named transcript files, and every formal artifact matches its manifest byte count and SHA-256.

Archive data stays local. Cookie import, local CA/system proxy, and account login are explained and approved separately.

Repository: <https://github.com/Zhenxiangai/wechat-archive>

## 建议标签 / Suggested tags

中文：`#开源` `#多平台视频` `#内容归档` `#语音转文字` `#AI Agent` `#Hermes`

English: `#opensource` `#localfirst` `#transcription` `#aiagents` `#macOS`

## 发布前准确性清单

- 四个平台真实单链接、唯一视频、逐字稿和 manifest 全部通过。
- `v1.0.0/SKILL.md` 实际可读后才发布机器安装链接。
- 不宣称 Windows、Linux 或 Intel Mac 支持。
- 不把原始逐字稿描述为完美文案。
- 不发布 Cookie、证书、账号数据、真实媒体/逐字稿或含个人路径的 manifest。
- 明确软件许可证不授予平台内容抓取、转载、传播或商业使用权。
