# WeChat Archive Launch Kit / 推广发布素材

Canonical link / 统一入口：<https://github.com/Zhenxiangai/wechat-archive>

Social preview / 分享配图：[social-preview.jpg](./assets/social-preview.jpg)

## 中文短帖

我开源了一个本地优先的微信视频号归档工具：把分享链接或博主名称发给 Hermes，它会在 Mac 上完成单链下载、博主历史完整分页、批量任务和进度追踪；MP4 下载完成后，还会无人值守生成同目录、同名的原始 TXT 逐字稿。

真实流程已跑通 14 页、发现并创建 203 条任务，保留 22 个 MP4，并自动生成 22 个 TXT，最终 `pending=0`、`failed=0`。

项目面向 Apple Silicon Mac，数据默认只保存在本机。证书、系统代理和微信登录都由 Hermes 先解释、再等用户授权，不需要手工复制 Cookie。

GitHub：<https://github.com/Zhenxiangai/wechat-archive>

如果它也解决了你的内容归档问题，欢迎试用、提 Issue 或点一个 Star。

## 中文长帖

### 标题

开源了一个微信视频号本地归档工具：链接或博主历史 → MP4 + 原始逐字稿

### 正文

做内容研究或知识库时，我一直缺少一条完整的微信视频号归档链路：不只是下载某一条视频，还要能搜索博主、处理同名候选、遍历当前可见的历史作品、批量建任务，并在下载后自动得到可搜索的原始逐字稿。

所以我把这套本机流程整理成了开源项目 **wechat-archive**。

它目前可以：

- 下载单条微信视频号分享链接；
- 搜索博主并返回同名候选；
- 完整分页读取博主当前可见作品；
- 批量创建任务，并用 `job_id` 查询进度；
- 为每个任务保留 `manifest.json`；
- MP4 完成后，无人值守生成同目录、同名的原始 TXT；
- 归档公开微信公众号文章；
- 通过 Hermes 用自然语言部署和调用。

真实验收中，它遍历了 14 页，发现并创建 203 条任务；我按跑通流程的目标保留 22 个 MP4，后台转写 worker 最终生成 22 个同名 TXT，状态为 `pending=0`、`failed=0`。

这个项目坚持 local-first：真实视频、逐字稿和 manifest 默认只留在自己的 Mac。它不会把系统代理、证书或微信登录藏在“一键安装”背后；Hermes 会先说明具体变化，再等待电脑主人批准、点击或亲自登录。默认单链接路径不读取浏览器 Cookie。

当前自动部署只验收了 Apple Silicon Mac，微信页面变化也可能需要继续适配。逐字稿保留 Whisper 的原始识别结果，不自动润色、总结或翻译。

仓库与安装入口：<https://github.com/Zhenxiangai/wechat-archive>

如果你也在做内容归档、AI 知识库或视频研究，欢迎试用并把真实结果告诉我。觉得有用的话，也欢迎点一个 Star，让更多有同样需求的人看到它。

## English short post

I open-sourced **wechat-archive**, a local-first WeChat Channels archiver for Apple Silicon Macs.

Give Hermes a share link or creator name. It can resolve a single video, return same-name creator candidates, paginate currently visible creator history, create batch jobs, and track every run with a `job_id` and `manifest.json`. Completed MP4 files are transcribed unattended into same-directory, same-name raw TXT files.

One real acceptance run covered 14 pages and 203 queued items, kept 22 MP4 files, and produced 22 TXT transcripts with `pending=0` and `failed=0`.

Archive data stays local. Certificate, proxy, and WeChat-login steps are explained and approved separately; the default single-link path does not read a browser Cookie.

GitHub: <https://github.com/Zhenxiangai/wechat-archive>

Feedback, Issues, and Stars are welcome.

## English long post

### Title

Show HN: A local-first WeChat Channels archiver with unattended transcription

### Body

I built **wechat-archive** because saving one video was only a small part of the actual workflow. For research and knowledge-base work, I needed to start from either a share link or creator name, handle same-name candidates, paginate the creator's currently visible history, create batch jobs, and keep the resulting media traceable.

The project currently supports:

- single WeChat Channels share-link downloads;
- creator search with same-name candidates;
- full pagination of currently visible creator posts;
- batch task creation with `job_id` progress;
- a `manifest.json` for every archive job;
- unattended MP4-to-same-name raw TXT transcription;
- public WeChat Official Account article archiving;
- natural-language deployment and operation through Hermes.

In one real acceptance run, it paginated 14 pages and queued 203 items. I kept 22 completed MP4 files for the workflow test, and the unattended worker produced 22 same-directory TXT transcripts with `pending=0` and `failed=0`.

The project is local-first: MP4 files, transcripts, and manifests stay on the owner's Mac. Certificate, system-proxy, and WeChat-login steps are not hidden behind a silent installer. Hermes explains each stage and waits for the computer owner to approve, click, or log in personally. The default single-link path does not read a browser Cookie.

Automated onboarding has currently been verified on Apple Silicon Macs only. WeChat page changes may require adapter updates, and raw Whisper transcripts can contain recognition mistakes, so the original MP4 and manifest remain the source of truth.

Repository: <https://github.com/Zhenxiangai/wechat-archive>

Real-world feedback, documentation improvements, Issues, and Stars are welcome.

## Suggested tags

中文：`#微信视频号` `#开源` `#内容归档` `#语音转文字` `#AI Agent` `#Hermes`

English: `#opensource` `#localfirst` `#wechat` `#transcription` `#aiagents` `#macOS`

## Accuracy checklist

- Do not claim Windows or Intel Mac support.
- Do not describe raw TXT as polished copy or perfect transcription.
- Do not publish Cookies, certificates, account data, MP4/TXT archives, or manifests containing personal paths.
- Use the repository homepage as the human sharing link and the fixed `v0.6.0/SKILL.md` URL as the machine installation entry.
