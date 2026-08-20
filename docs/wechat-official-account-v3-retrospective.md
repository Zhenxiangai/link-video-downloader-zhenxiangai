# 微信公众号历史抓取 v3 实战复盘

> 状态：v1.2.5 最终归档与发布复盘
> 日期：2026-08-20
> 实测对象：公开微信公众号（发行文档已匿名化）
> 种子文章：公开文章链接（发行文档不保留）

## 1. 结论

这次故障不是用户操作错误，也不是历史分页接口失效，而是两个独立兼容问题叠加：

1. 当前 Mac 微信打开文章时，关键会话出现在 `/mp/relatedsearchword` 请求；它带齐会话查询字段，但不再带 Cookie。旧核心只接受 `/s + Cookie` 或旧版 `/mp/profile_ext`，因此反复要求重登也无法保存会话。
2. 包装层每翻一页都会重写批次 `account`，却没有保存已确认的 `biz`。当临时来源映射过期后，任务在第 27 页、游标 274 处中断，尽管前 509 条记录已安全落盘。

两个问题修复后，同一 Job 从原游标续跑，完整盘点出 523 篇文章，无需从头重抓。

## 2. 本次真实结果

### 已验证事实

- 种子文章所属公众号已通过精确 URL 和本地采集结果完成身份确认；
- 历史清单：523 篇；
- 分页游标严格前进并正常收口；
- 清单阶段失败：0；
- 509 条已发现记录在中断后完整保留；
- 修复后从游标 274 续跑至 523 条；
- 用户明确授权后提交 523 篇全文下载；
- 达到 100 篇成功后开始本轮开源项目升级；
- 抽查产物同时包含 Markdown 正文、原始 HTML 和本地配图；
- 抽查正文不是验证码页或空壳页面；
- 下载过程使用 3 个受文件锁保护的 worker；
- 发现 2 篇历史列表存在但正文不可解析，错误为 `article_not_found`；最终二次结构复核确认两页均无标题、正文和图片，保留为源页面不可用；
- 本轮采集窗口关闭后，系统代理已恢复，公众号凭据文件权限为 `0600`。

### 不能扩大解释的边界

- “523 篇可枚举”不等于 523 篇正文最终都仍可访问；已删除、迁移或受限文章可能只能保留清单记录。
- `article_not_found` 需要二次复核后才能判定为源页面不可用或解析兼容问题；本任务的二次复核与判定证据见 [`v1.2.5-validation.md`](./v1.2.5-validation.md)。
- 当前修复验证的是 macOS 当前微信客户端行为，不代表未来微信版本不会再次改变会话入口。

## 3. 故障与修复时间线

### 阶段 A：旧触发路径失效

最初依次尝试：

- 打开公开文章；
- 从聊天重新打开；
- 重启并重新登录微信；
- 进入公众号主页并滚动；
- 等待旧 `/mp/profile_ext?action=home|getmsg` 会话。

结果均是后端能看到文章或公众号身份，但无法建立可回放的历史会话，表现为 `reauthentication_required`。

### 阶段 B：建立隔离 sidecar

为避免影响正式后端，创建独立候选服务：

- 正式后端保持不变；
- 候选后端使用独立 API、代理、运行目录和凭据文件；
- 构建、测试和真实采集均在 sidecar 中完成；
- 失败时只停止 sidecar，不替换正式二进制。

这让协议诊断、回滚和真实验证互不干扰。

### 阶段 C：脱敏诊断发现真实入口

诊断默认关闭，只在受控 marker 存在时记录结构信息：

- path；
- query 字段名；
- 是否存在 Cookie；
- 状态码与 Content-Type。

明确不记录：

- query 值；
- Cookie 内容；
- headers 原文；
- 请求或响应正文；
- `uin`、`key`、`pass_ticket`、`appmsg_token` 等实际值。

证据显示当前微信的 `/mp/relatedsearchword` 请求同时具备：

- `__biz`
- `uin`
- `key`
- `pass_ticket`
- `appmsg_token`
- `mid`
- `idx`
- `sessionid`

但 `has_cookie=false`。旧规则因此必然拒绝。

### 阶段 D：核心捕获边界修复

新增无 Cookie 会话入口，但不是放宽到任意请求：

- 协议必须是 HTTPS；
- host 必须精确等于 `mp.weixin.qq.com`；
- path 必须精确等于 `/mp/relatedsearchword`；
- 上述 8 个字段必须全部非空；
- 任何其他无 Cookie 路径全部拒绝；
- 缺任一字段全部拒绝；
- `/s` 文章请求仍要求同请求 Cookie；
- 旧 `/mp/profile_ext?action=home|getmsg` 继续兼容；
- 持久化的 refresh URI 不带真实会话值；
- 凭据文件仅当前用户可读写。

### 阶段 E：分页状态保存缺陷

核心会话生效后，历史接口成功返回第一页 10 篇并给出下一游标。归档器持续翻页到 509 条时再次中断。

根因是：

```text
manifest.account = {name, account_id}
```

覆盖了此前已确认的 `biz`。内存中的临时来源映射过期后，任务无法从公开文章 HTML 重新获得 `biz`，于是报 `official_account_not_identified`。

修复后每页持久保存：

```text
manifest.account = {name, account_id, biz}
```

同一 Job 保留 509 条既有记录，从 `next_offset=274` 继续，最终收口为 523 条。

## 4. 代码改动

### 核心仓库 `wx_channels_download`

分支：`fix/wxmp-cookie-less-session-v3`

- `pkg/scraper/wxmp/plugin.go`
  - 新增严格受限的 `/mp/relatedsearchword` 会话来源；
  - 保存 `appmsg_token`；
  - 保持精确域名、HTTPS、路径和字段完整性约束。
- `pkg/scraper/wxmp/plugin_test.go`
  - 完整字段成功；
  - 8 个必填字段逐个缺失均拒绝；
  - 错误路径拒绝；
  - Cookie 为空仍不影响合法新入口；
  - 旧 `/s` 缺 Cookie 规则不变。

### 包装仓库 `link-video-downloader-zhenxiangai`

分支：`fix/official-account-history-v3`

- `scripts/wechat_archive.py`
  - 每页持久保存已确认的 `biz`，支持内存映射过期后的游标续跑。
- `tests/test_official_batch_resilience.py`
  - 验证分页后内存对象与磁盘 manifest 都保留 `biz`。
- `SKILL.md`
  - 更新当前微信两种会话来源；
  - 增加无 Cookie 白名单边界；
  - 增加分页时持久保存 `biz` 的恢复要求。

## 5. 验证要求

合并前必须同时通过：

### 核心层

- 新增定向测试；
- 公众号包完整测试；
- API 安全测试；
- `go generate ./...`；
- 完整 Go 测试；
- 候选二进制构建与本地签名；
- sidecar API 启动烟雾测试；
- 无令牌访问敏感正文接口仍返回 403。

### 包装层

- 新增分页持久化定向测试；
- 公众号批次回归测试；
- 全量 Python 测试；
- Python 编译检查；
- `git diff --check`；
- 真实任务从中断游标续跑；
- 真实全文产物抽查；
- 最终数量、失败项、正文、HTML、配图和索引核对。

## 6. 发布状态与边界

`v1.2.4` 固定已公开并经远端重新下载复验的核心 `v260810-zhenxiangai.3`：Release ZIP SHA-256 为 `54f54ce3f65def9ae922dea5892a77c78aaeec2c67f1aa295204393d71c05dba`，其中 macOS ARM64 二进制 SHA-256 为 `fddf28b5327690f0164bf905294784288495b1322d759bbc6a24120c82a5da37`。

`v1.2.5` 继续固定同一核心版本，只更新包装层文章归档逻辑：纯图片文章必须同时具备零验证标记、非通用标题、真实 `js_content` 容器和至少一张成功归档图片；旧 HTTP 图片只允许 authority 按 ASCII 小写规范化后精确等于 `mmbiz.qpic.cn` 或 `mmbiz.qpic.cn:80` 时升级为 HTTPS。任意其他 HTTP、空 userinfo、空端口、尾点主机、替代端口写法、主机后缀欺骗和异常 authority 均保持拒绝。

主项目发布门禁包括：

1. 根目录与版本化 Skill 包内容逐字节一致；
2. 完整 Python 测试、编译检查和 `git diff --check` 通过；
3. 在隔离 HOME 中从公开版本入口安装；
4. bootstrap 从公开核心 Release 下载，并同时通过 ZIP 与二进制校验；
5. 自检与 API 最小烟雾测试通过。

本次两轮发版累计修复四项已证实的兼容缺陷，不宣称微信接口永久稳定。真实任务最终保留 523 条历史记录：521 篇完成正文、原始 HTML 和媒体归档，2 篇历史记录仍存在但源页面已不可用；共验证 2828 个输出文件，SHA-256 失败、重复内容 ID、重复原文链接、缺失内容目录、孤立内容目录和敏感会话值命中均为 0。匿名化验证方法与输出见 [`v1.2.5-validation.md`](./v1.2.5-validation.md)。

## 7. 一句话经验

遇到“用户明明重新登录和打开了页面，但会话始终不可用”时，不要继续重复要求用户操作；应先用不记录任何值的结构诊断确认客户端实际请求入口，再以精确路径和字段完整性建立最小白名单，并把恢复所需身份持久化到任务状态，而不是依赖短时内存映射。
