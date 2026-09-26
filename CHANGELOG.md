# Changelog

## 0.3.19 — 2026-09-26

### 修复

- **「QQ 里发起的回合，5 条被动额度用完后就 follow 不到了」**（用户实测原话）。
  日志把因果链摆得很清楚（`docker logs astrbot` 里 4 次 `40034128`）：

  ```
  botpy.errors.ServerError: 回复消息失败，被动回复时间或者次数超过限制
  在调用插件 astrbot_plugin_dsh 的处理函数 on_message 时出现异常：…
  ```

  即：额度用尽 → 插件 `event.send()` **抛异常** → 从 `on_message` 冒出去 →
  **SSE 读取循环中断、那条流关闭** → ingress 的 `res.on('close')` **把整条回合记录删掉**
  → 群里再 `/follow` 就报「没有在跑的回合」。可那个回合在 DSH 那边**还在正常跑**。

  两处修：

  - **插件侧**：新增 `_try_deliver()`，发送失败**不抛**，返回 `(ok, why)`；额度用尽
    （`40034128`）时安静收场（`_log_quota_exhausted()`，只记日志、**不再尝试发送**——
    额度用完就是发不出去，再发还是撞墙）。回合照旧在 DSH 跑完，用户发一条新消息
    ＋ `/dsh follow` 就能接着看。
  - **ingress 侧**：`res.on('close')` 不再删除记录，而是**降级成无源流记录**
    （`res = null`、清空 followers、刷新时间戳），于是 follow 随时能再接上。

### 验证

三个离线 harness（同一份源码 + 桩 ctx + 一次性 `DSH_HOME`）：

- `follow_detach_strict_harness.mjs`（**本次的核心**）：掐断 QQ 那条流、并**故意让
  agent.status 保持 idle**（不给「follow 现场建记录」兜底）→ 带修复 HTTP **200** 且收到
  断开后的正文；**改回旧行为则 409「没有在跑的回合」**，与服务端日志里的现象一致。
- `follow_web_harness.mjs`：网页发起的回合 ✅
- `follow_offline_harness.mjs`：QQ 发起的回合回归 ✅

**两条测量教训（都是我自己踩的）**：

1. 第一版 detach harness **在旧代码下也通过** —— 因为 0.3.18 的「follow 现场建记录」把它兜住了，
   两条修复在该场景下冗余。**必须构造能让被测修复唯一决定结果的场景**，否则测试是空转的。
2. 又一次 `.NET` 静态调用无视 `cd`：我用 `[IO.File]::ReadAllText('lib/index.js')` 做临时回滚，
   实际写去了别处 —— 于是「回滚后仍通过」的结论是假的。**回滚验证本身也要先确认它真的改到了文件**
   （这次改成绝对路径 + 先断言匹配数为 1 才动手）。

## 0.3.18 — 2026-09-26

### 修复

- **`/dsh follow` 对「网页发起的回合」永远报「没有在跑的回合」**（用户 0.3.17 实测：
  「我刚试了网页跑起来，然后 follow 说没在跑」）。0.3.17 的 follow 只对**从 QQ 发起**的回合
  有效 —— 因为 `state.turns` 只由 `/inbound` 创建，**网页发起的回合在桥里根本不存在**，
  而它恰恰是这个功能最主要的目标场景。

  修法：新增**无源头流的回合记录**（`createHeadlessTurn`）。当 `session/event` 来自一个
  已绑定、但没有回合记录的会话时，就为它建一条记录（`res: null`），于是：
  - 跟随者可以随时挂上来（`GET /follow` 在现场发现 agent 正在跑时也会主动建）；
  - 事件照常被认领（turn 号、`turn/end` 收尾）；
  - 没有跟随者时它什么都不写，只是一条内存记录 + 自己的孤儿看门狗。

  同时把「回合的输出」全程走 sink：保活注释、digest 汇报、`turn.res?.end()` 等都能容忍
  `res: null`；跟随者挂上时若已有 `startedAt` 会重新挂起进度定时器。

### 顺带修掉一个我自己引入的回归

- headless 记录让 `approval/request`、`user-questions/request` 有机会命中**网页发起的回合**——
  那会把审批卡/提问框从网页上抢走、然后在群里没人应答。现在只接管「QQ 那边真的能答」的回合：
  **有源流，或此刻有人跟随**；否则照旧 `next()` 放行给网页。

### 验证

两个离线 harness（同一份源码 + 桩 ctx + 一次性 `DSH_HOME`）：

- `follow_web_harness.mjs`（**专测这次的坑**）：完全不发 `/inbound`，直接推 `session/event`
  模拟网页在跑 → `/follow` 返回 **200** 并收到 `text: 网页回合的正文 ⏎ ⏎ —— 本回合结束`
  与 `done`，回合结束后再 `/follow` 返回 409 ✅
- `follow_offline_harness.mjs`（回归）：QQ 发起的回合，正文同时到达两条流 ✅

顺带记一条**测量纪律**：第一版断言在推送后 300ms 就检查正文，得到 false —— 那不是丢正文，
是末段正文被 `endGraceMs(1200ms)` 压着、到 `turn/end` 才一起发。**采样太早不等于行为错**，
断言要么等够时间、要么断言真正的语义。

## 0.3.17 — 2026-09-26

### 新增

- **`/dsh follow`：把「正在跑的回合」接到群里这条新消息上（临时跟随，到本回合结束）。**
  以前只有「在群里发起的会话」能持续输出到群里 —— 因为那条 QQ 消息自己开着一条 SSE。
  网页/别处发起的回合没有这条流，群里就永远看不到。现在群里（或私聊）发一次
  `/dsh follow`，就能把那条回合的正文、工具行、结束标记实时接到这条新消息上，
  **回合结束自动断开**，不做长期订阅。
- 顺带解决**官方 QQ 被动回复 5 条上限**：额度是**按「被回复的那条消息」算的**
  （每条消息 5 分钟 / 5 次，见 QQ 开放平台「消息收发概述 → 频率与时效规则」），
  所以每发一条新消息就换来一份新的 5 条额度。5 条用完时再发一条新消息 +
  `/dsh follow`，就接着输出。
  - 日志层面也认了这个错误码：`40034128`（被动回复时间或次数超限）会被单独识别，
    提示「发一条新消息即可换新额度」，不再和 `40034105`（没有主动消息权限）混为一谈。

### 实现

- ingress 里把「回合的输出」从**单条流**改成**多 sink**：`turnWrite()` 会写向发起者
  那条流＋所有跟随者；`turn/end`、被打断、孤儿看门狗、卸载等收尾路径都会遍历所有 sink。
  `approval` / `question` 仍只发给发起者（那是给人做决定的，不适合刷进群）。
- 新端点 `GET /follow?umo=…`（新能力 `follow-active-turn`）：附带 `409`（当前没有在跑的回合）
  与 `400`（会话未绑定）两种明确拒绝，方便插件侧给准确的提示。

### 验证

- 离线 harness（`D:\dswk\.dsh-inbox\follow_offline_harness.mjs`）：用**同一份源码** + 桩 ctx
  在临时端口起一个实例，断言「正文同时到达主任务流与 follow 流、follow 流收到结束标记、
  回合结束两条流都收尾、回合结束后 `/follow` 返回 409」——全部通过（exit 0）。
  这样不必重启 `dsh web` 就能把行为测干净。

## 0.3.16 — 2026-09-24

### 修复

- **群里「@机器人 + 自己直接发一张图」会被当成 DSH 续聊接管**，哪怕 `bound_media_passthrough`
  和 `official_at_continue` 都关着。原因：官方通道那条「引用一张图 + @机器人」的特例
  （AstrBot 不建 `Reply`，被引附件只能从原始 payload 的 `msg_elements[].attachments[]` 里捡）
  用的是 `_has_inbound_media()` —— 它把**本条消息自带的附件**和**被引消息里的附件**混在一起，
  于是「自己发的图」也满足了条件。那条分支还完全不看 `bound_media_passthrough`，所以关掉也没用。
- 修法：新增 `_has_quoted_media()`，只看**被引消息**（aiocqhttp 看 `Reply` 段里的组件，
  官方通道看 `msg_elements[].attachments[]`），那条特例改用它。于是：

  | 场景 | 之前 | 现在 |
  |---|---|---|
  | `@机器人` + 本条直接带一张图 | 接住 ❌ | **不接住** ✅（`bound_media_passthrough` 打开时才会接住） |
  | `@机器人` + 引用一条带图的消息 | 接住 | 接住（特例保留） |
  | `@机器人` + 只有文字 | 不接住 | 不接住 |

  容器内探针验证（`D:\dswk\.dsh-inbox\capture_media_probe.py`）：上表三行 + 开关打开那行都符合预期。

## 0.3.15 — 2026-09-22

### 修复

- **回合进行中又来一条消息（进 DSH 队列）、随后在网页上把它撤回 → QQ 侧永远卡在
  「⏳ 已跑 1 秒 · 正在执行」**。探针复现（连发两条 `/inbound`，第 2 条随即断开）：

  ```
  A: ack | status(被新消息打断)          ← 旧流被关，没有 done
  B: ack | status(⏳ 已跑 1 秒 · 正在执行) ← 无限重复
  ```

  根因三件事叠加：

  1. 新消息顶掉旧回合时**没有停掉旧回合的周期汇报定时器**。那个定时器是按 `sessionId`
     查回合的，于是它找到的是**新记录**；而新记录还没跑起来（没有 `startedAt`），
     `Date.now() - (startedAt || Date.now())` 恒为 0 → 永远输出「已跑 0 秒 · 正在执行」。
  2. digest 档在 `startedAt` 还没设时就发汇报（正是上面这条的放大器）。
  3. 新消息**可能压根跑不起来**：它进的是 DSH 的 `next-turn` 队列，用户若随即在网页上
     撤回，就永远等不到这一轮的 `turn/start` / `turn/end`，那条 SSE 会一直挂着。

  修法：

  - 顶掉旧回合时 `stopHeartbeat()` + 给旧流补一个 `done`（别再让它 EOF 得莫名其妙）；
  - digest 档在 `startedAt` 未设时**不发**汇报；
  - 新增**孤儿回合看门狗**：同一回合 25 秒内毫无事件、且 `agent.status !== 'running'`
    （也不在等人回答）→ 判定这一轮不会有输出，回一句说明 + `done` 收尾，不再无限挂着。

  离线harness 复现与验证：修复前顶掉后仍刷 6 条「已跑 1 秒 · 正在执行」，修复后 0 条。
  真机探针（`D:\dswk\.dsh-inbox\queue_probe.mjs` / `queue_probe2.mjs`）留作回归用。

## 0.3.14 — 2026-09-20

### 修复

- **群里随手发个 `1` 会蹦出「现在没有等你回答的提问或审批，编号没生效」**：没有未决提问时，
  ingress 会把 AstrBot 转发来的纯编号拦下来回这句说明（0.3.0 起的设计：免得被当成新任务丢给
  模型）。但用户常年在群里发个 `1` 也会蹦出这句，吵。
  现在**不回话**了（SSE 只回 `done` 不带正文）—— 编号在没有未决提问时本来就是无效输入，
  静默归档即可。有未决提问时照旧接住（`已记录你的回答` / `已记录审批回复`）。

## 0.3.13 — 2026-09-20

### 修复

- **别人复制机器人的正文，你再引用那条复制品 → 被当成「引用 DSH 回复续聊」送进了 DSH**（实测踩到）。
  根因：判断「引的是不是 DSH 的正文」只能按**内容**认（归一化指纹 / 零宽标记 / 固定词），
  而复制品与原文一模一样，分不出来。
- 现在加了一条**正交信号**：消息里 **@ 了别人（不是机器人）就不算续聊** —— `@某人 + 引用`
  显然是在跟那个人说话，不是跟 DSH 说话。新方法 `_mentions_someone_else()` 同时看
  组件链里的 `At` 与官方 payload 的 `mentions`（list 与「被适配器转成字符串」两种形状都认）。
- 验证（容器内探针，构造真实 payload 逐层判定）：

  | 场景 | @了别人 | 引的是 DSH 正文 | 接住 |
  |---|---|---|---|
  | `@某人` + 引用（复制的）DSH 正文 | 是 | 是 | **否** ✅ |
  | `@机器人` + 引用 DSH 正文 | 否 | 是 | 是（照旧） |
  | 不 @ + 引用 DSH 正文 | 否 | 是 | 是（照旧） |
  | `@某人` + 不引用 | 是 | 否 | 否 |

## 0.3.12 — 2026-09-20

### 修复

- **名单外的人「引用 DSH 的回复」问一句，会被再回一遍白名单提示（复读机）**：群里有人引用机器人
  那条白名单提示问「所以现在到底是什么情况」，插件把它当成「引用 DSH 回复 = 续聊」接住，
  又回了一遍同样的提示 —— 实机在群里循环了一次（14:18 一次、14:21 又一次）。
  现在 `_is_explicit_bridge_command()` 只认**真正的 `/dsh …`**：回答类（数字/批准/取消）与
  「引用 DSH 回复」都静默放行、交回人格。引用仍然会被接住（名单内的人照旧能靠引用续聊），
  只是名单外的人不再收到那句提示。
- 排障记录：这次的判定是用容器内探针脚本钉死的（`python /tmp/capture_probe.py`，构造假 event +
  真实 payload 调 `_should_capture` / `_is_explicit_bridge_command`）。关键事实：
  **AstrBot 日志里 `message_reference: {'message_id': None}` 不能证明「没有引用」** ——
  被引正文在原始 payload 的 `msg_elements` 里，而那个字段 AstrBot 的日志根本不打印
  （botpy 还会凭空造出 `{'message_id': None}`）。

## 0.3.11 — 2026-09-13

### 修复

- **群里随手打个数字会被怼「你不在 DSH 白名单里」**：`on_message` 先判断「这条要不要接住」、
  再判断「这个人有没有权限」。而纯编号 / `批准` / `取消` 是**被特意接住**的（不接住就会被
  人格当闲聊答掉，提问的连号选项 `1`、`1,3` 就没法用了），于是任何一个名单外的群友打个 `1`
  都会收到白名单提示 —— 既吵，又等于当众告诉大家「这个群里挂了台 DSH」。
  现在只有**明确在调桥**的消息才回那句说明：`/dsh …`、或引用 DSH 的回复。回答类消息静默放行，
  交回人格（跟没装桥一样）；名单外的人本来也答不了 —— ingress 会按发起人 `senderId` 拒掉。

## 0.3.10 — 2026-09-12

### 修复

- **「老会话都切不动 / 报会话不存在」的真因**：`sessionPersistence.list()` 返回的是
  `{header, revision, sizeBytes}` **快照**，不是带 `id` 的 header。`isPersisted` 里写的是
  `h.id === sessionId` —— **永远不成立**，于是只要会话不在内存里（没在跑），`/dsh use` 一律报
  「会话不存在」，只有当前活着的那个能切。现在用 `stat(id)` 判存在（不存在时返回 `undefined`）。
- **修 0.3.9 的过度过滤**：同一个形状错误让 `/dsh ls` 把**所有**冷会话都藏掉了（只剩当前那一条）。
  现在读 `snapshot.header.id`。列表判据与 `/dsh use` 依旧一致，但这次是「能切的都列出来」。
- **`/dsh rename` 从来没持久化过**：它调的是 `sessionPersistence.update()` —— 这个方法**不存在**，
  又是一次「`if (svc?.method)` 静默空转」，改名只在内存里活到重启。现在走真正的
  `sessionTitle.rename(session, title)`（要求会话是活动的那一个；它 append 一条 `session/title`
  事件，durable）。
- 顺带把插件里**所有**宿主服务调用对着 DSH 源码核了一遍（`sessions` / `agents` /
  `workspaceRegistry` / `sessionPersistence` / `sessionController` / `sessionProjections` /
  `permissionPresets` / `commands` / `attachments` / `skills` / `sessionTitle`），
  方法名与签名现在都能对上。

## 0.3.9 — 2026-09-12

> 归因更正（见 0.3.10）：这一版把「老会话切不动」归给了「归档幽灵」，
> 真正的原因其实是 `isPersisted` 读错了 `list()` 的返回形状；归档过滤本身是对的，保留。

### 修复

- **`/dsh ws <新目录>` 在网页里不成组，会话掉进「未分组 / 其他」**：登记工作区调的是
  `workspaceRegistry.add()` / `.register()` —— **这两个方法根本不存在**，于是静默什么都没做
  （真实 API 是 `create(path, title)`：同路径复用、不覆盖原标题）。现在改用 `create()`；
  顺带让「会话创建时所在目录还没有工作区实体」的旧账也自动补建，不再永久留在「未分组」。
- **`/dsh ls` 列出一堆同名会话，只有最新的能切，老的都报「会话不存在」**：
  归档的会话是**仍然占着** workspace `sessionIds` 槽位的（DSH 的设计：取消归档要能回原位），
  但它已经点不开。现在列表同时排掉「已归档」和「日志已不在（`sessionPersistence.list()`
  里没有）」的条目 —— 判据与 `/dsh use` 能不能接上完全一致，所以**列出来的就一定切得动**。
- **`/dsh use` 失败会把群永久绑在一个不存在的会话上**：原先先写绑定/落盘、再校验是否存在，
  校验不过就抛错，绑定却留下了。现在先校验再接上，失败不动绑定；报错信息也带上短 id，
  并提示 `/dsh ls` 或 `/dsh new`。

## 0.3.8 — 2026-09-12

### 修复

- **网页端那张提问卡片不会被收掉**：`ask_user_question` 是「两边并行」的 —— 我们把它发到 QQ，
  同时 `dsh-api-remotes` 会把它当 waterfall 事件转发给浏览器，`dsh-client-ui-user-questions`
  据此接管输入区渲染成提问框。浏览器只在两种情况下收卡片：自己答了，或者 host 给它发 cancel 帧。
  我们在 QQ 侧抢先答完之后，gateway 里那条事件还挂着、浏览器收不到任何通知，卡片就一直留在
  输入区 —— **实测连「等 10 分钟超时作废、整轮都结束了」之后输入区上还是那张卡片**。
  现在 QQ 侧一定下来（答了 / 取消 / 超时）就按 agent 找到那条 pending 事件并用同一个结果结掉它，
  gateway 会顺手给每个客户端发 cancel 帧，卡片随之消失。摸的是 `typertGateway` 的内部成员，
  拿不到就静默跳过（最坏只是卡片多留一会儿，QQ 那条链路不受影响）。
- **插件卸载会卡死**：`server.close()` 只停止接受新连接，然后等**已有**连接自己结束；QQ 那条
  SSE 可能挂着一整个回合，于是卸载永远走不完 —— 插件卡在「卸载中」、`3188` 端口消失、信标被清，
  AstrBot 那边察觉不到桥已经没了，而且此后任何热重载都卡在同一个地方（本次排障就是被它绊住的）。
  现在卸载时先把还开着的 SSE 正常收尾（发 `done` + `end`），再用 `closeAllConnections()` 兜底，
  并给 `close()` 加 2 秒上限。

### 新增

- `GET /health` 现在返回 `capabilities`（`url-inbound-probe` / `file-token` / `withdraw-web-card`），
  给两边一个不靠猜的版本判据 —— 以前只能「探某个路由是不是 404」来推断对面是不是新代码。

## 0.3.7 — 2026-09-12

### 变更

- **诊断日志默认关**：DSH 侧 `traceLog`、AstrBot 侧 `trace_delivery` 从「默认开」改成「默认关」
  （0.3.6 上线时为了排「正文慢一拍」，实机验证已通过）。要排障时临时打开：
  - `traceLog: true` → `%DSH_HOME%/dsh-astrbot-ingress/trace.log`（`adopt-turn` / `drop-stale` /
    `grace-flush` / `turn-end`，>1MB 自动归档 `.old`）；
  - AstrBot 插件 `trace_delivery` 打开 → 日志里 `[dsh-trace]` 前缀（SSE 到达时刻、每条正文的发送
    时刻、被动/主动）。顺带补上了两处漏网的无条件日志（`body … chars`/`turn done`），它们之前没受开关控制。
- AstrBot 插件配置项 `trace_delivery` **补进配置 schema**（此前只能手改 JSON），默认 `false`。
- 修掉 `_conf_schema.json` 里**重复的 `ingress_url` 键**（JSON 解析取后者，前者是死配置）。

### 文档

- 两个 README 都新增「**快速开始**」：**只需要填 `token`**（同机连它都能留空），地址自动探测、
  文件不用挂盘；并给出「你的情况 → 还要多填什么」对照表。
- 刷新过时的「最少配置」表述：Docker 下 `ingress_url` 已可留空（0.3.6 起的候选探测），
  出站文件不再需要共享盘（0.3.6 起的拉取兜底），共享目录从「必需」降级为「出站图片 / 视频才可能要」。

## 0.3.6 — 2026-09-12

### 新增

- **出站文件的「拉取兜底」**：出站文件过去要求 AstrBot 能直接读到源文件（`D:\x` → `/mnt/d/x` 的挂载
  约定），Docker 官方 compose 只挂 `./data` 时根本读不到。现在：
  1. 先照旧试本地路径（挂了盘的用户零开销）；
  2. 看不到 → `POST /file-token`（Bearer，带 `{path, umo}`），ingress 按**该会话的工作区**校验
     （工作区外 / 敏感路径照旧拒绝）并签发**一次性、120 秒有效**的凭证；
  3. 插件 `GET {ingress_url}/file/<token>` 流式拉到自己的 `data/temp/` → 按原逻辑发出 → 用完删掉。
  配置：`outbound_pull`（默认开）/ `outbound_pull_max_mb`（默认 200）；DSH 侧 `outboundUrlMaxMb`。
- **ingress 地址也做成候选**：`ingress_url` 留空时依次用 信标 → `host.docker.internal:3188` →
  `127.0.0.1:3188` → `ingress_url_candidates`，用 `/health`（无需 token）探一遍取第一个通的，
  结果缓存 300 秒。**Docker 部署于是只剩 `token` 一个必填字段。**
- `/file-token`、`/file/<token>` 两个路由都走同一套 Bearer 鉴权；凭证一次性、过期即废。

## 0.3.5 — 2026-09-12

### 修复

- **URL 入站的探测死锁**：`_probe_via_ingress` 用了同步 `httpx.Client`，而它跑在 AstrBot 的事件循环里、
  要请求的 `/api/file/<token>` 又是**同一个进程的 dashboard** 提供的 —— 于是「我等我自己」，真 token 探测
  每次都卡满 5 秒超时（日志还误报成「候选不可达」），结果永远回退到共享目录 / base64。改用
  `httpx.AsyncClient` 即可。同时把探测失败的日志拆细：真 token 与假 token 各探一次，
  能区分「宿主不可达」与「token 不被接受」。

## 0.3.4 — 2026-09-12

### 新增

- **URL 入站支持候选自动探测**（`inbound_url_base` 留空也能用）：
  - 候选顺序：显式 `inbound_url_base` → `http://127.0.0.1:<dashboard 端口>` → `inbound_url_candidates`
    （每行一个完整地址或裸端口）。
  - **先探测再使用**：插件登记一次性 token，请 ingress `GET /probe-url?url=…`（新增接口，Bearer 鉴权，
    5 秒超时、只读一小段就断开）真取一次，第一个通的才用；结果（含失败）缓存 600 秒。
  - 为什么不直接猜：AstrBot 在 Docker 里**看不到**宿主机把 6185 映射成了哪个端口（本机是 6185→10000），
    猜错就会在回合中途才发现附件没了。
  - token 是**单次消费**的（`FileTokenService.handle_file()` 里 `staged_files.pop`），所以探测与真传
    各登记一次，不能复用。
  - 候选都不通时会在聊天里**提醒一次**并列出试过哪些地址（比只说「太大」可操作）。

## 0.3.3 — 2026-09-12

### 新增

- **URL 入站**：Docker 部署可以不再挂共享盘。插件把附件登记成 AstrBot 的**一次性 URL**
  （`{inbound_url_base}/api/file/<token>`，token 默认 5 分钟、免登录），ingress 自己流式下载进
  `cwd/.dsh-inbox/`。
  - `inbound_url_base`：**DSH 视角**能访问到的 AstrBot 地址（例：`docker port` 里 6185 映射到的宿主端口
    `http://127.0.0.1:10000`）。**别照抄 `callback_api_base`** —— 那是容器内地址，宿主机解析不了。
  - `inbound_url_mode`：`auto`（默认，只有 >12MB 才走 URL）/ `always`（全走，彻底不要共享目录）/ `off`。
  - `inbound_url_max_mb`：单文件上限（默认 200）；DSH 侧另有 `inboundUrlMaxMb` / `inboundUrlTimeoutMs`。
  - ingress 侧 `lib/fetchfile.js`：流式落盘 + 边下边计数（超限立即中断并删半截文件）+ 超时 + 失败清干净，
    带单测（正常 / 超限 / 404 / 超时 / 非 http / 缺 fetch）。
  - 回退链不变：URL 失败 / 未配 base / 超上限 → 共享目录 → base64 → 「太大」提示。

## 0.3.2 — 2026-09-12

### 修复

- **上一条的正文要等下一段正文才出现**（QQ 里就是「永远慢一拍」；严重时上一轮的答案会
  出现在下一轮的流里）。两个原因，都已修：
  1. 每条助手消息的**最后一段**被压着等 `turn/end`，只为拼上 `—— 本回合结束`；中间只要隔着
     工具阶段（几十秒到几分钟），正文就一直压着不发。现在只压 `endGraceMs`（默认 1200ms）：
     `turn/end` 在这段时间内到就合并成一条，到不了就**先把正文发出去**，结束标记随后单独发。
  2. 新消息进来时旧回合的 SSE 被掐掉，而旧回合**迟到的事件**只按 `sessionId` 找回合，于是被算到
     新一轮头上。DSH 的每个事件都带 `turn: number`（`SessionEventMap`），现在按轮次号归位：
     被顶掉的旧轮次记进 `staleTurns`，它迟到的事件直接丢掉，不再串到新一轮；打断旧回合时也会先把
     压着的正文补发给它自己的流。
- AstrBot 插件加 `trace_delivery` 诊断开关（暂时默认开）：每轮多几行 `[dsh-trace]` 日志
  （SSE 事件到达时刻、每条正文的发送时刻、passive/active 与发送耗时）。定位完会改成默认关。

### 复现（本地探针，旧行为）

同一会话「上一条还在跑就发下一条」：

```
T1 ack → +1252ms 「被新消息打断」→ 流关闭（T1 的正文还没发出去）
T2 ack → +534ms  收到 text: "AAAA ⏎ —— 本回合结束"   ← T1 的答案跑进了 T2 的流里
```

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
