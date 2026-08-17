# Link Video Downloader by ZhenxiangAI / Launch Kit

统一仓库：<https://github.com/Zhenxiangai/link-video-downloader-zhenxiangai>

分享配图：[social-preview.jpg](./assets/social-preview.jpg)

> Hermes 新机稳定安装标识：`Zhenxiangai/link-video-downloader-zhenxiangai/skill-releases/v1.2.3/wechat-archive`。内部 Skill ID 继续使用 `wechat-archive`，已有用户可原位升级。

## 中文短帖

我开源了 **Link Video Downloader by ZhenxiangAI**：把微信公众号、微信视频号、B站、小红书或抖音链接发给 Hermes，它会在 Apple Silicon Mac 上生成整洁的本地内容包。

视频目录只保留一个正式视频，并生成带时间线的 `原始逐字稿.txt/.srt/.json`；小红书图文保留正文与配图。视频号保留当前可见历史批量能力；公众号支持先盘点历史，再按用户确认范围保存 HTML、Markdown 和配图。

内容、Cookie 和登录态默认留在用户自己的 Mac；Safari/Chrome Cookie 导入、微信 CA/代理和登录都是分开授权动作。

GitHub：<https://github.com/Zhenxiangai/link-video-downloader-zhenxiangai>

## 中文长帖

### 标题

开源：一个项目提取视频号、B站、小红书和抖音

### 正文

做内容研究和知识库时，真正麻烦的不是“下载某一条视频”，而是不同平台有不同入口、登录态、文件名和失败状态。

所以我做了 **Link Video Downloader by ZhenxiangAI**。

它目前可以：

- 用同一个 `extract --url` 自动识别公众号、视频号、B站、小红书和抖音；
- 把视频、正文、配图和任务清单放在标题目录中；
- 对视频生成带时间线的三种中文名原始逐字稿；
- 只保留一个正式视频，不把备用路线产物复制进目录；
- 使用 `content-*` / `batch-*` 和 `manifest.json` 跟踪下载、转写、授权等待、完成与失败；
- 保留视频号博主当前可见历史批量；
- 支持公众号历史盘点，并按用户确认范围归档文章正文与配图；
- 通过 Hermes 自然语言安装、提交和查询任务。

新统一链路已真实跑通 B站 1080P、小红书图文/视频、抖音视频和视频号 1080P。四个视频任务都只有一个正式视频和三种中文名逐字稿，所有正式产物均与 manifest 的字节数和 SHA-256 一致。

项目坚持 local-first。它不会把 Cookie、证书、系统代理或登录藏在一次静默安装中；Hermes 会先说明变化，再等用户批准、点击或亲自登录。

仓库：<https://github.com/Zhenxiangai/link-video-downloader-zhenxiangai>

## English short post

I open-sourced **Link Video Downloader by ZhenxiangAI** for Apple Silicon Macs.

Give Hermes one WeChat Official Account, WeChat Channels, Bilibili, Xiaohongshu, or Douyin link. It creates an organized local package with one formal video when applicable, Chinese-named timestamped raw TXT/SRT/JSON transcripts, article text and images, plus a traceable manifest.

Channels creator history remains available. Official Account history is inventoried first and only the user-confirmed scope is archived. Cookies, CAs, proxy changes, and account login remain separate approvals.

GitHub: <https://github.com/Zhenxiangai/link-video-downloader-zhenxiangai>

## English long post

### Title

Show HN: One local extractor for WeChat, Bilibili, Xiaohongshu, and Douyin

### Body

I built **Link Video Downloader by ZhenxiangAI**, a local content extractor for WeChat Official Accounts, WeChat Channels, Bilibili, Xiaohongshu, and Douyin.

One `extract --url` command identifies the platform and creates a central job. The resident worker produces an organized content package: exactly one formal video when applicable, Chinese-named timestamped raw TXT/SRT/JSON transcripts, article Markdown and local images, plus a manifest with status, byte counts, and hashes.

The project retains Channels creator-history batches. Bilibili, Xiaohongshu, and Douyin use a pinned transparent derivative core that remains runnable if its upstream disappears. Official Account history is inventoried first and only the user-confirmed scope is archived.

The unified path has completed real Bilibili 1080p, Xiaohongshu image-text/video, Douyin video, and WeChat Channels 1080p. Every video package has exactly one formal video and three Chinese-named transcript files, and every formal artifact matches its manifest byte count and SHA-256.

Archive data stays local. Cookie import, local CA/system proxy, and account login are explained and approved separately.

Repository: <https://github.com/Zhenxiangai/link-video-downloader-zhenxiangai>

## 建议标签 / Suggested tags

中文：`#开源` `#多平台视频` `#内容归档` `#语音转文字` `#AI Agent` `#Hermes`

English: `#opensource` `#localfirst` `#transcription` `#aiagents` `#macOS`

## 发布前准确性清单

- 四个视频平台的真实单链接、唯一视频、逐字稿和 manifest 全部通过；公众号发布前仅做隔离小样本的两阶段验收，不宣称已下载全量历史。
- 隔离新 HOME 完成透明核心安装、自检与同 Job 授权续跑；v1.2.2 另完成 600 任务恢复模拟和真实 600 条批次只读重放；v1.2.3 改用 GitHub Contents API 的版本化 Skill 入口，并通过 Hermes 隔离安装验收。
- 不宣称 Windows、Linux 或 Intel Mac 支持。
- 不把原始逐字稿描述为完美文案。
- 不发布 Cookie、证书、账号数据、真实媒体/逐字稿或含个人路径的 manifest。
- 明确软件许可证不授予平台内容抓取、转载、传播或商业使用权。
