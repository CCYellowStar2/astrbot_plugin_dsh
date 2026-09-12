# Changelog

## 0.3.1 — 2026-09-12

### 文档

- 插件 README 把「本插件的另一半是 [`dsh-astrbot-ingress`](https://github.com/CCYellowStar2/dsh-astrbot-ingress)」
  提到开头并补上仓库链接（原来只在文末出现，且开头那句写的是「本目录的上一级仓库」——在插件市场页面上没意义），
  文末那句重复的删掉。
- 本文件 0.3.0 小节收敛为功能说明。

## 0.3.0 — 2026-09-12

配置更省事、长代码块不再被切坏。

### 新增

- **同机零配置**：ingress 启动后把**实际监听端口 + token**写进 `~/.dsh/astrbot-ingress.json`
  （`kind: dsh-astrbot-ingress`，0600 权限，每 30 秒刷新 `updatedAt`，退出时自己收拾，pid 换人就不动）。
  AstrBot 插件在 `ingress_url` / `token` 留空时自动读它（60 秒缓存，超过 10 分钟没刷新算失效，
  不是本插件写的信标不认），读不到才回退 `127.0.0.1:3188`。同机部署**不用再复制 token**；
  容器部署看不到这个文件，照旧手填 `host.docker.internal:3188` + token。DSH 侧 `beacon: false` 可关。
- 长消息分片**照顾代码块**：切在未闭合的 ```` ``` ```` 里时，本条补上闭合、下一条用原语言标签重新打开
  （`splitForIm` / `openFence`，见 `test/pure.test.js`）。从前只按换行/句号断，长代码块被腰斩后
  两边各自渲染成一坨。
- 会话键三级兜底：`unified_msg_origin` → `session_id` → `sender_id`。有些适配器 / 自建调用不带 umo，
  从前会把它们全挤进同一个空键里。

## 0.2.0 — 2026-09-10

从「能收发文字」扩到完整 QQ 桥。

### 新增

- `/dsh model`：列出/切换会话模型（`sessionController.modelCatalog` / `selectModel`），支持编号或 `provider/model`，可带推理档位。
- `/dsh perm`：查看/切换权限预设（`permissionPresets`），中文标签；优先走 DSH 自带 `/permission`（活会话写入器会把政策变更告诉模型）。切的是当前会话，durable。
- `/dsh compact`：转发 DSH 自带 `/compact`，报告压缩前后上下文占用。
- `/dsh last [N]`：从会话日志补发最近 N 条助手回复，用于超时/重载/断线后找回已跑完的结果。
- `/dsh status` 增加上下文占用（`contextPressure` 投影）、累计 token 与系统/工具/消息构成；已绑定会话显示当前模型。
- 工具过程可见：`full` 档下每个 `tool/call` 发一行摘要（`🔧 bash npm test`），同回合最多 20 行，`showToolCalls` 可关；`toolLineBatch`（默认 5）把连续工具行合并成一条再发。
- 过程显示档位 `progress_mode`（AstrBot 插件配置，默认 `digest`）：`digest` = 思考/工具行攒着，每 `progress_interval_sec` 秒（默认 60）汇总一条 `⏳ 已跑 3 分钟 · 工具 7 次 · 最近：edit · bash`（这段时间没调工具就报「仍在思考：<思考首行>」，在等权限批准时不报）；`full` = 旧的实时刷屏；`minimal` = 过程一声不吭。三种档位一律照发结果、文件、报错、`任务已停止`、附件提示与权限确认——丢的是过程，不是需要你动作的东西。档位由插件逐次随请求带给 ingress（`progress: {mode, intervalSec}`），没带的旧插件用 ingress 自身的 `progressMode` 兜底。
- 保活与档位解耦：`digest` 的周期汇报本身就是心跳；`minimal` 档改发 SSE 注释行 `: ping`（AstrBot 解析时忽略，但字节到达，不会撞「读空闲超时」）。
- `—— 本回合结束` 归到「结果」而不是「过程」：**任何档位都发**（`minimal` 也不例外），发完文件、长任务后用户靠它确认跑完了。第一版把它当过程砍掉，用户立刻发现「发完不知道结束没有」。
- 官方 QQ 机器人主动消息：`official_send_mode` = `passive-first`（默认，第一条被动、之后主动）/ `proactive` / `passive`，主动失败自动回退被动重试；用于绕开被动回复「5 分钟内最多 5 次」（40034128）的限制。注意 `40034105 主动消息失败, 无权限` 不一定是没权益——群聊要在开放平台给该群单独开启「主动通知」；撞到后本进程不再尝试主动。
- 思考内容出站：`reasoningMode` = `off` / `first-line`（默认，只发第一行）/ `full`。
- 「提问」也通到 QQ：`ask_user_question` 走的是和审批**并排**的另一条 waterfall 事件（`user-questions/request`，网页端由 `dsh-client-ui-user-questions` 接）。从前只接了审批，所以 QQ 侧收不到问题、DSH 一直干等。现在 ingress 一并接管：问题带编号发到 QQ，可回 `1` / `1,3` / `答 自定义` / `取消`，也可引用那条（NapCat）或 `@机器人 + 说话`（官方 Bot）。超时和 `取消` **绝不替你选**（`plan-review` 的选项里就写着「批准」），工具带说明失败后由模型接着走；等回答期间暂停周期汇报；只有发起那一轮的人能答。AstrBot 侧 `_looks_like_approval` 扩成认任意编号（`3`、`1,3`、`1 3`）与 `取消`，否则纯数字选项会被人格当闲聊答掉；没有未决提问时，ingress 会把纯编号拦下来回一句说明，不当作新任务。
- 命令词优先于提问回答：有提问挂着时 `stop` / `status` / `send …` 照旧当命令执行（踩过：回 `stop` 想停回合，却被当成「用户回答：stop」喂给模型）。自由文本回答用 `答 …` 前缀绕开。
- **官方 QQ 机器人也支持引用续聊**（原来以为不行，是看漏了）：官方 API 压根不下发 `message_reference`——AstrBot 日志里那个 `{'message_id': None}` 是 botpy 用 `data.get("message_reference", {})` 凭空造的空对象；被引消息的**正文**其实在原始 payload 的 `msg_elements[0].content`（还有 `msg_elements[0].msg_idx` 与 `message_scene.ext` 里的 `ref_msg_idx=`/`msg_idx=`），而 botpy 的模型里没这个字段、直接丢掉。插件现在用 `_install_botpy_raw_payload_shim()` 把它捡回来：botpy 的 `GroupMessage`/`C2CMessage` 声明了 `__slots__` 塞不了属性，所以换成**不声明 `__slots__` 的子类**挂进 `botpy.connection` 的构造点（子类自带 `__dict__`），把原始 `data` 存成 `self.raw_payload`；`_official_quoted_text()` 再从中取出被引正文做同一套标记/指纹判定。`@机器人 + 说话` 也照旧可用。
  - 教训：诊断第三方 SDK「拿不到某字段」时，**不能只看它解析后的对象**——`__repr__` 甚至可能造出并不存在的字段。
- 提问在 QQ 与 DSH 网页上**并行**，谁先答算谁的：ingress 一边把问题发到 QQ，一边照常 `next()` 让网页端显示那张卡片。最早做成「QQ 轮次独占」，结果群里在跑的时候网页卡片点不了（人常常两边都开着）。网页那条路没人接或出错都不影响 QQ（失败即视作永不 settle），QQ 超时/取消仍照旧作废。
- **官方 Bot 引用消息里的图/文件也能拿**：官方虽然不下发 `message_reference`，但被引附件原样躺在原始 payload 的 `msg_elements[].attachments[]`（`content_type`/`filename`/`size`/`url`，**url 可直接下载**）。AstrBot 的官方适配器只把**本条消息**的附件变成 `Image` 段（不建 `Reply`），所以被引媒体以前完全收不到。现在 `_official_quoted_media_segments()` 把它们转成组件、并进 `_iter_media_segments()` 的统一收集流程（`Image.fromURL` → `convert_to_file_path()` 遇 http 自动下载 → 共享目录 → DSH）；`_should_capture()` 增加「官方 + @机器人 + 有附件 + 已绑定 → 捕获」，所以**引用一张图 + @机器人**（或引用 + `/dsh …`）就能把图交给 DSH。
  - 实测依据：同一条引用消息里 `chain=['At','Plain']`（AstrBot 没给图），而 `msg_elements=[{"attachments":[{"content_type":"image/png","filename":"…jpg","size":47001,"url":"https://multimedia.nt.qq.com.cn/download?…"}]}]`（官方给了）。
- 会话隔离 `session_scope`（AstrBot 插件配置，默认 `group` = 现状）：`user` = **群里每人一条**独立会话（各自独立的上下文 / `cwd` / 模型 / 权限 / 待审批）。实现只在插件侧把上桥的 `umo` 改写成 `umo#u<uid>`，**ingress 一行未改**（它只把 umo 当索引键，实际发送走 AstrBot 事件），所以会话/回合/审批/提问/进度全都自动跟着隔离。想吃回共用：各自发 `/dsh use <同一个短id>`（`/dsh ls` 查短 id）手动组队；多人绑同一条会话是**串行 + 打断**的，适合轮流用。切成 `user` 后原群会话默认闲置，自己也要 `/dsh use <短id>` 接回来。隔离的是**上下文**不是隐私（回复仍发在群里）。
- 入站暂存目录支持**同机自动兜底**：`inbound_share_dir` 留空且 AstrBot 不在容器里时（与 DSH 同一个文件系统），插件向网关问该会话的 `cwd`（`GET /binding` 本来就返回这个字段，**ingress 无需改动**），自动用 `<DSH 工作区>/.dsh-inbox` —— 同机部署收大文件因此**零配置**，`inbound_dsh_prefix` 也不用填（两边是同一个路径）。`cwd` 缓存 5 分钟，`/dsh ws` 换工作区后会自动刷新；分容器仍按原样填两项。
- 出站文件：技能 `dsh-qq-send-file` + `[SEND_FILE: 绝对路径]`，或 `/dsh send <路径>`；随插件 `registerProvider` 自动进技能目录。
- 出站图片内联：png/jpg/gif/webp/bmp 且 ≤5MB 用 `Image` 段发，不再走文件卡片（`send_inline_images`）。
- 出站视频：mp4/mov/avi/mkv/webm 用 `Video` 段发（QQ 里可直接播放），失败自动回退成文件卡片（`send_inline_videos`）。
- 官方 QQ 机器人（`qqofficial`）自动用 Markdown 发文字，表格与 `![图]()` 降级，被拒时由 AstrBot 回退纯文本；出站文件强制 `direct`。
- 引用续聊更稳：DSH 出站文本带两个零宽空格隐藏标记，识别不到时退回归一化指纹（去空白/markdown 后比前缀 40 字、后缀 30 字）与固定标记词。
- 官方 Bot 续聊用 `@`：`official_at_continue`。原文写「官方适配器拿不到引用内容，所以引用那条路在官方 Bot 上不可靠」——**2026-09-12 已推翻**：被引正文其实在原始 payload 的 `msg_elements` 里（botpy 不认识该字段才丢了），引用续聊已实现，见本文件上方对应条目。`@机器人 + 直接说话` 这条路照旧可用（`_is_bot_mention` 比对 `At.qq` 与自身 qq）。
- 每个群/私聊默认独立会话；`/dsh new` 用 `session-<uuid>` 显式创建；工作区/会话/`steer`/`rename`/`end` 等命令齐全。
- 附件经 HTTP base64 入站（不依赖共享盘符），图/文件落 `.dsh-inbox/`；引用消息里的附件也会收集。
- 大文件入站：配 `inbound_share_dir` + `inbound_dsh_prefix` 后图/文件/视频都先落共享目录、只传路径（绕开 12MB base64 上限，URL 附件流式下载到该目录），本轮结束后自动删除暂存副本。
- 视频入站（直接发或引用）：`Video` 段落盘并把路径交给模型，消息里标明可用 `ffprobe`/`ffmpeg` 处理。
- 出站发件目录可配（`send_file_mode` / `send_protocol_path` / `send_outbox_dir`），同机直装与 Docker 分容器都覆盖。
- 回合结束时在最后一条回复末尾附 `—— 本回合结束`。
- 纯函数拆到 `lib/pure.js` 并加 `node:test` 单测（`npm test`）。

### 修复

- 长任务丢结果：AstrBot 侧 `httpx.Timeout` 从「整轮总时长」改为「无空闲输出」超时，DSH 跑几十分钟不再被掐断。
- `agentPreset` 不再写死 `routing-suite`（该预设已不存在，会让会话头带上无效 preset、之后任何 resume 都失败）；改为解析 `agentPresets.defaultId` 并校验，且在 `setup` 里 `mount`。
- 引用「机器人自己的任意消息」都会触发 DSH 的误判：改为只认真正的 DSH 出站内容。
- 内部协议原文（inject 说明、SEND_FILE 占位）会泄漏到 QQ：出站统一过滤。
- 协议端读不到文件：Docker 分容器时先把文件落到协议端能读的目录再发。
- 共享目录不再凭空创建：只补建最后一级、父目录不存在就当挂载没配（原先 `mkdir -p` 会造出协议端看不到的目录，最后报难懂的 ENOENT）；出站候选去掉 AstrBot 私有目录（协议端永远读不到），内置猜测仅在挂载确实存在时生效，否则回退 `direct`。入站同理。

### 已知限制

- 群「上传到群文件」走 `group_upload` 通知，不是聊天附件，收不到（监听它会误触发）。
- 语音（`Record`）入站仍未处理；需要的话在 AstrBot 打开内置 STT（`provider_stt_settings.enable`），语音（含引用链里的）会被转成文字，本插件当普通文字处理。
- 视频只落到工作区、由模型用 ffmpeg 处理：DSH 没有视频内容块，模型看不到画面，抽出来的帧也需要换成带视觉的模型才能看。
- 出站视频受 QQ 限制（一般只认 mp4/H.264 且有大小上限），不符时发送失败会回退成文件卡片，NapCat 个人号也可能仍显示为文件。
- 会话默认一个群共用一条；要按人隔离把 `session_scope` 切成 `user`（见上方新增条目）。隔离的是上下文，不是隐私 —— 回复仍发在群里。
