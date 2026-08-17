<div align="center">

# Link Video Downloader by ZhenxiangAI

**把视频链接发给 Hermes，后台完成下载、整理和逐字稿生成。**

**微信公众号 · 视频号 · B站 · 小红书 · 抖音**

不需要守着终端，也不用自己整理文件。任务完成后，你会得到原视频、带时间线的逐字稿和完整任务记录。

**简体中文** · [English](./README_EN.md)

![多平台视频下载、归档与逐字稿生成](./docs/assets/social-preview.jpg)

[![macOS Apple Silicon](https://img.shields.io/badge/macOS-Apple%20Silicon-111827?logo=apple&logoColor=white)](#使用前需要准备什么)
[![Latest Release](https://img.shields.io/github/v/release/Zhenxiangai/link-video-downloader-zhenxiangai?label=release&color=8b5cf6)](https://github.com/Zhenxiangai/link-video-downloader-zhenxiangai/releases/latest)
[![Local first](https://img.shields.io/badge/data-local--first-10b981)](#数据保存在哪里)
[![Project License](https://img.shields.io/badge/original%20code-MIT-f59e0b)](./LICENSE)

</div>

## 这个项目能做什么？

这是一个给 Hermes 使用的本地内容归档 Skill。安装并完成首次设置后，你只需要把链接发给 Hermes：

- 发一个视频链接：下载这一条内容并生成逐字稿。
- 发一个博主链接并说明“批量抓取”：先告诉你目前能看到多少条，再询问你要下载多少条。
- 第一次跑通后，只要 Mac 在线且视频号的搜索与作品列表会话仍有效，从手机发送全新或已登记的博主链接即可后台处理；Hermes 不操作微信。
- 任务进入后台后：自动下载、转写、整理目录，并持续报告进度。
- 任务完成后：保留原视频、TXT 逐字稿、SRT 字幕、JSON 时间线和任务清单。

所有主要文件默认保存在你自己的 Mac 上，不会因为运行这个项目而自动上传到第三方网盘。

## 目前支持哪些平台？

| 平台 | 单条链接 | 博主历史批量 | 最终内容 |
|---|---|---|---|
| 微信公众号 | 支持文章 | 支持先盘点、再按确认范围归档 | HTML + Markdown + 配图 |
| 微信视频号 | 支持 | 支持 | 视频 + 三种逐字稿 |
| B站 | 支持 | 支持 | 视频 + 三种逐字稿 |
| 小红书 | 支持图文和视频 | 计划中 | 图文与配图，或视频与逐字稿 |
| 抖音 | 支持 | 支持 | 视频 + 三种逐字稿 |

“历史批量”是指平台当前向你的账号展示、并且你有权访问的内容，不包括已删除、私密、付费无权或被平台风控拦截的内容。

## 发出链接后，会经过哪些阶段？

### 1. 识别链接

Hermes 会先判断链接来自微信公众号、视频号、B站、小红书还是抖音。公众号文章链接默认先盘点当前可见历史、不下载；其他单链接会为本次请求创建独立内容任务。

### 2. 检查登录和授权

如果首次设置已经完成，任务会先复用已有会话。新分享链接先从微信公开分享接口读取昵称和头像，再通过现有搜索会话做精确匹配；识别成功后保存在仅当前用户可读的本地注册表。以后收到同一博主链接，可以直接用已保存身份刷新作品。搜索接口可用不等于作品列表接口也可用，真实任务会逐层验证；任一必要实时能力失效时，任务进入等待状态，但已登记身份、冻结作品清单、下载选择和原 Job 都会保留。

### 3. 确认抓取范围

- **单链接：** 默认就是下载这一条，不再重复询问。
- **批量抓取：** 先盘点博主目前可见的视频总数，此时不会下载；然后询问“要下载多少个？”，收到数量后才从最新内容开始执行。

### 4. 后台下载

任务会排队并在后台下载。批量任务会分批推进，不会同时启动几百个下载。

### 5. 生成逐字稿

下载完成后，项目会在本机提取音频并生成：

- `原始逐字稿.txt`：按时间顺序阅读；
- `原始逐字稿.srt`：可作为字幕使用；
- `原始逐字稿.json`：保留每段文字和时间信息。

原始逐字稿不会自动润色、改写或总结，避免把模型加工后的内容冒充原话。

### 6. 整理和交付

每条内容会进入自己的标题目录。Hermes 会报告完成数量、失败数量和任务状态；任务清单会记录文件大小和校验值，方便确认文件是否完整。

### 7. 恢复临时状态

日常视频号任务不会默认开启采集代理，也不会自动打开、点击、输入或读取微信。只有实时会话确实失效，且用户之后在 Mac 旁明确启动恢复时，才开启最长 5 分钟的受控窗口；用户手动打开任一视频号页面一次后，系统会批量推进全部积压任务，并在成功、失败或超时后自动关闭采集、还原网络状态。

## 实际对话是什么样？

### 下载一条视频

```text
你：<一个视频号、B站、小红书或抖音链接>

Hermes：已识别为 B站视频，任务已进入后台。
Hermes：下载完成，正在生成逐字稿。
Hermes：任务完成，已生成视频、TXT、SRT 和 JSON。
```

### 批量下载一个博主的视频

```text
你：<博主或作品链接>，批量抓取这个博主的视频

Hermes：该博主当前可下载视频共 913 个。默认从最新开始，你要下载多少个？

你：下载 5 个

Hermes：已按确认数量提交 5 个任务，正在后台下载和转写。
```

发送链接本身代表你同意处理这条公开链接；首次导入平台登录状态、安装视频号本地授权或遇到新的系统权限时，Hermes 仍会单独说明并征得你的同意。

## 使用前需要准备什么？

当前公开版本适用于 **Apple Silicon Mac**，并需要已经安装 Hermes。

首次使用通常只需要完成一次：

1. 安装这个 Skill 和本地处理组件；
2. 在 Safari 或 Chrome 登录需要使用的 B站、小红书或抖音账号，并允许按平台导入一次登录状态；
3. 使用视频号时，在微信 Mac 版登录自己的账号；
4. 允许一次视频号本地证书授权，并由用户本人手动打开一条视频号链接完成首次会话建立。

以后同一博主和已冻结批次通常不需要重复这些步骤。视频号实时能力依赖微信页面生命周期，页面关闭、账号退出、平台要求验证、Mac 休眠或关机、系统撤销权限时，仍可能需要你在一次受控恢复窗口里手动打开任一视频号页面。项目不会承诺永久登录。

## 安装或升级

在 Hermes 所在的 Mac 上运行：

```bash
hermes skills install 'Zhenxiangai/link-video-downloader-zhenxiangai/skill-releases/v1.2.3/wechat-archive' --category social-media --name wechat-archive --force --yes
```

该命令使用仓库内固定的 `v1.2.3` Skill 入口，由 Hermes 通过 GitHub Contents API 获取完整文件，避免 GitHub Raw 或 CDN 节点临时出现 429、503 和连接超时。

安装 Skill 后，可以直接对 Hermes 说：

```text
请检查并完成 Link Video Downloader 的首次设置。
```

Hermes 会先检查环境，再解释缺少哪些组件和权限。涉及安装软件、读取浏览器登录状态、添加视频号本地证书或修改系统权限时，它应该先告诉你影响并等待确认。

旧版本原位升级时，已有任务、归档、Cookie 和本地配置会继续保留。

## 哪些情况需要我介入？

正常情况下，首次设置之后只需要发链接。以下情况 Hermes 会暂停原任务并给出下一步：

- 微信、B站、小红书或抖音登录状态已经失效；
- 平台要求扫码、验证码或风险确认；
- macOS 弹出钥匙串或文件访问权限；
- 视频号会话失效：任务会保留，等你下次在 Mac 旁时，再由你本人手动打开 Hermes 给出的原始链接一次；
- 当前系统代理不是项目能够自动兼容的类型。

暂停不会等于重新开始。完成授权后，Hermes 应继续原来的任务，而不是重复下载或创建第二份任务。

等待中的新博主任务会由后台低频检查；检测到会话恢复后，会自动继续同一个任务。项目不会为了恢复任务去操作微信，也不会长期保持采集代理。

`channel-session-status` 返回 `author_search_ready` 时，只能证明博主搜索接口当前可用；它不会把新链接解析或作品列表读取冒充为已就绪。完整可用性以真实任务是否成功识别博主并冻结清单为准。

## 数据保存在哪里？

默认目录是：

```text
~/Documents/WeChatArchive/
├── content/
│   ├── 视频号/<标题>--<作品标识>/
│   ├── B站/<标题>--<作品标识>/
│   ├── 小红书/<标题>--<作品标识>/
│   └── 抖音/<标题>--<作品标识>/
├── jobs/
│   └── <任务编号>/manifest.json
└── state/
    └── channels-creators.json
```

视频目录只保留一个最终成功的 `video.mp4`，并在同一目录保存三种逐字稿。小红书图文内容保存为 `正文.md` 和配图目录。

Cookie、账号凭证、浏览器资料、证书私钥和代理快照不会进入公开仓库，也不会写入最终内容包。

## 当前版本已经验证到什么程度？

`v1.1.0` 已完成真实小批量验证：

- 视频号：盘点到 280 条可选内容，实际完成最新 2 条；
- B站：盘点到 913 条可选内容，实际完成最新 2 条；
- 抖音：完成作者列表中的 2 条；
- 三个平台合计 24 个正式产物，文件大小和校验值均与任务清单一致；
- 小红书的单条图文和视频流程也已完成真实验证。

`v1.2.0` 新增微信公众号归档：先盘点当前账户可见历史且不创建文章子任务，用户确认数量后再沿用同一父任务，从最新开始保存正文、HTML、配图和 manifest。公开发布前只用隔离小样本验收，不做全量历史。

`v1.2.1` 为公众号本地接口生成私有 Token，并固定使用已移除公共 CA/私钥的核心版本；安装不会自动启用抓包或改动系统代理。

`v1.2.2` 修复视频号后端已完成但 MP4 尚未稳定可见造成的假失败：交付阶段会有限等待，只接受受控工作目录或既有输出中唯一、非空且大小稳定的 MP4，并在本地文件稍后出现时自动、幂等恢复，同时回写父批次终态。发布验收包含 600 个任务的真实归档收口模拟，以及一次真实 600 条批次的只读重放；41 条历史假失败均被唯一识别。该版本同时修复 CA 证书状态校验和后端规范化证书文件名兼容问题。完整证据与边界见 [`docs/v1.2.2-validation.md`](./docs/v1.2.2-validation.md)。

`v1.2.3` 只修复公开安装链路：新增由 GitHub Contents API 提供的版本化 Skill 入口，并将公众号本地授权文件路径改用不会被 Hermes 误判为环境凭据的 `WECHAT_MP_AUTH_FILE`；旧的 `WECHAT_MP_TOKEN_FILE` 仍兼容。归档、下载和恢复逻辑没有变化。

这证明的是小批量端到端流程已经跑通，不代表平台接口未来永远不会变化。平台改版、风控或登录策略变化后，项目可能仍需要适配更新。

## 接下来准备做什么？

- 小红书博主历史批量抓取；
- 继续减少首次安装和登录失效时的人工步骤。

当前版本不承诺尚在计划中的能力。

<details>
<summary><strong>开发者和排障人员：查看命令、状态与技术边界</strong></summary>

### 常用命令

```bash
export WECHAT_ARCHIVE_ENABLED=1
SCRIPT="$PWD/scripts/wechat_archive.py"
PYTHON="${HERMES_HOME:-$HOME/.hermes}/hermes-agent/venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"

"${PYTHON}" "$SCRIPT" extract --url '<支持的单个链接>'
"${PYTHON}" "$SCRIPT" channel-session-status
"${PYTHON}" "$SCRIPT" status --job-id 'content-YYYYMMDDTHHMMSSZ-1234abcd'
```

仓库目录中的安装与状态命令：

```bash
sh ./scripts/bootstrap.sh doctor
sh ./scripts/bootstrap.sh install
sh ./scripts/bootstrap.sh status
```

作者批量采用同一个两阶段任务：

```bash
sh ./scripts/bootstrap.sh inspect-creator '<视频号、B站或抖音链接>'
sh ./scripts/bootstrap.sh download-creator-plan '<上一步任务编号>' '<确认数量>'
```

公众号历史同样先盘点、再确认数量：

```bash
WECHAT_ARCHIVE_ENABLED=1 "$PYTHON" "$SCRIPT" extract-official-account --url '<公众号文章链接>'
sh ./scripts/bootstrap.sh download-official-account-plan '<同一父任务编号>' '<确认篇数>'
```

### 常见任务状态

| 状态 | 含义 |
|---|---|
| `queued` | 已接收，等待后台处理 |
| `downloading` | 正在下载或归档 |
| `transcribing` | 视频已下载，正在生成逐字稿 |
| `awaiting_download_count` | 已盘点总数，等待用户确认下载数量 |
| `waiting_for_authorization` | 等待首次授权或微信打开目标内容 |
| `waiting_for_reauthentication` | 平台登录状态失效，等待重新登录 |
| `completed` | 下载、转写和清单均已完成 |
| `failed` | 任务失败，需要查看具体原因 |

`queued`、`downloading` 和 `transcribing` 只代表任务仍在进行，不代表最终文件已经完成。Hermes 应始终轮询同一个任务编号到终态。

### 登录态和视频号代理边界

- B站、小红书和抖音只在用户明确授权后，从已登录的 Safari 或 Chrome 按平台导入持久 Cookie；普通任务不会每次重新读取浏览器。
- 视频号的一次性初始化会在本地创建项目 CA；它只用于用户在场的手动会话恢复。日常新、老博主任务先复用已有会话，不启用采集链路。
- 博主身份保存在本地私有注册表，已知分享链接可直接映射到内部身份；新链接在现有会话中识别成功后自动登记。
- 使用 Clash Verge Rev/Mihomo 时，Skill 只加载当前任务的临时运行时路由，不写入用户持久配置。
- 不受支持的现有代理会保持原样，任务会明确停止而不是擅自关闭代理。

### 本地组件

安装器复用 Hermes 自带 Python，并安装或复用固定透明派生核心、FFmpeg、whisper.cpp、固定模型和视频号后端。它管理视频号后端、转写 worker 和内容 worker 三个用户级后台服务。

Release 只包含可审计源码与 notices，不包含 FFmpeg、whisper.cpp、模型、视频号后端二进制、Cookie、CA、登录态或真实归档。

</details>

## 使用边界

- 当前只承诺 Apple Silicon Mac；Windows、Linux、Intel Mac、GUI 和 Web 管理台不在当前范围内。
- 软件许可证不等于平台内容授权。请只下载、保存和使用你有权访问及处理的内容，并遵守平台规则。
- 项目不会自动润色、纠错、总结或翻译原始逐字稿；这些属于后续的独立处理。

## 许可证与第三方组件

根 [MIT License](./LICENSE) 只覆盖 ZhenxiangAI 原创文件及明确采用 MIT 的文件。仓库内的透明派生核心保留 `yt-dlp 2026.07.04` 的 Unlicense、真实来源和完整第三方许可文本。

详见 [THIRD_PARTY_NOTICES.md](./THIRD_PARTY_NOTICES.md)。

## 参与项目

欢迎提交真实失效样例、文档改进或平台适配 Issue。请勿上传 Cookie、证书、账号信息、真实媒体、逐字稿或包含个人路径的任务清单。
