# AstrBot 插件：DSH 桥

> AstrBot plugin that forwards selected IM conversations to a local
> [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) agent.
> AstrBot stays the gateway and the everyday persona; DSH takes over
> coding / tooling tasks when you call `/dsh`.

把 QQ / 其它 IM 里指定的对话转给本机 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)。AstrBot 继续当网关和日常人格，DSH 只在 `/dsh`（可改）接手写代码、跑工具。

**本插件只是桥的一半，另一半是 DSH 侧的 [`dsh-astrbot-ingress`](https://github.com/CCYellowStar2/dsh-astrbot-ingress)，不装它连不上。**
它跑在 DSH 进程里，把会话事件 / 审批 / 提问推给本插件，并在 `~/.dsh/astrbot-ingress.json` 写下实际端口与 token 供本插件自动发现。

```
QQ ──OneBot──▶ AstrBot（本插件）──HTTP──▶ DSH :3188 ──▶ Agent
```

## 依赖

- AstrBot ≥ 4.16
- 已启动、且已加载 `dsh-astrbot-ingress` 的 `dsh web`
- 平台不限：OneBot（NapCat / SnowLuma / aiocqhttp）、QQ 官方、Telegram、飞书、企微等，走 AstrBot 适配器

## 快速开始（只填一个 `token`）

1. DSH 侧装好 [`dsh-astrbot-ingress`](https://github.com/CCYellowStar2/dsh-astrbot-ingress) 并重启 `dsh web`。
2. 本插件装好后**重载**。
3. 只填 `token`：抄 `%DSH_HOME%/dsh-astrbot-ingress/config.json` 里的那段，粘进本插件的 `token`。
   （AstrBot 与 DSH 同一台机器时可以**留空** —— 插件会读 `%DSH_HOME%/astrbot-ingress.json` 信标。）
4. 回聊天窗口发 `/dsh 你好`。

**地址不用填**：`ingress_url` 留空会自己探（信标 → `host.docker.internal:3188` → `127.0.0.1:3188`
→ `ingress_url_candidates`），`/health` 第一个通的才用。**文件也不用挂盘**：入站大附件走一次性 URL，
出站文件这边看不到时向 ingress 要一次性凭证拉过来再发。

| 你的情况 | 除了 `token` 还要填什么 |
|---|---|
| 与 DSH 同一台机器 | **什么都不用**（`token` 也留空） |
| 本插件跑在 Docker（官方 compose） | **只填 `token`** |
| Linux 上的 Docker | 同上 + compose 加 `extra_hosts: ["host.docker.internal:host-gateway"]` |
| 宿主机映射端口不是 `6185`（如 `6185 → 10000`） | `inbound_url_candidates` 填宿主端口（`10000`），只影响走 URL 的大附件通道 |
| 与协议端（NapCat/SnowLuma）分容器且挂载点不同名 | `send_protocol_path`（+ 需要时 `send_outbox_dir`） |

## 安装

**方式一：AstrBot 插件市场**（推荐）——在 AstrBot WebUI 的插件市场搜索 `dsh` 或「DSH 桥」一键安装
（市场页面：<https://cloud.astrbot.app/market>）。

**方式二：填仓库地址** —— 在 AstrBot WebUI 的插件页填 `https://github.com/CCYellowStar2/astrbot_plugin_dsh`，
或把整个 `astrbot_plugin_dsh` 文件夹放进 AstrBot 的 `data/plugins/`。

装完重载 **DSH 桥**。同机部署**什么都不用填**：DSH 里的 ingress 插件会把端口和 token 写进
`~/.dsh/astrbot-ingress.json`，本插件自动读。容器部署（AstrBot 在 Docker 里）看不到那个文件，
**填上 `token` 即可** —— `ingress_url` 留空会自动探 `host.docker.internal:3188`。

## 配置

| 项 | 默认 | 说明 |
|---|---|---|
| `enabled` | 开 | 总开关 |
| `ingress_url` | 空 = 自动发现 | **留空即可，Docker 也一样**：依次尝试 信标文件（`~/.dsh/astrbot-ingress.json`）→ `http://host.docker.internal:3188` → `http://127.0.0.1:3188` → `ingress_url_candidates`，用 `/health` 探一遍取第一个通的（缓存 5 分钟）。只有都不通或网络特殊才手填。Linux 的 Docker 需要 compose 里加 `extra_hosts: ["host.docker.internal:host-gateway"]` |
| `token` | 空 = 自动发现 | 同机部署留空即可（从信标文件读）；容器部署**必填**，值与 DSH 侧 `%DSH_HOME%/dsh-astrbot-ingress/config.json` 的 `token` 相同。**这是唯一的必填项** |
| `command` | `dsh` | 唤醒前缀，聊天里发 `/dsh …` |
| `private_passthrough` | 关 | 打开后私聊全部进 DSH，不再走 AstrBot 人格 |
| `allow_users` | 空 | 允许使用 DSH 的用户 ID；空 = 仅管理员 |
| `allow_groups` | 空 | 允许使用 DSH 的群号；空 = 群里仅管理员可用 |
| `timeout_sec` | 600 | **无输出**多久算超时（按空闲算，DSH 还在发心跳就一直等） |
| `progress_mode` | `digest` | 过程显示档位，见「过程显示」 |
| `progress_interval_sec` | 60 | `digest` 的汇报间隔（10-300，须小于 `timeout_sec`） |
| `session_scope` | `group` | `group` = 一个群共用一条会话；`user` = 群里每人一条 |
| `quote_continue` | 开 | 引用机器人的回复即续聊（官方 Bot 同样支持） |
| `official_at_continue` | 开 | 官方群聊里 `@机器人 + 说话` 即续聊 |
| `bound_media_passthrough` | 开 | 已绑定的会话里，直接发的图 / 文件 / 视频也交给 DSH |
| `send_file_mode` | `auto` | `auto` = 容器内能找到共享目录就拷、否则发原路径；`direct` = 不拷贝；`shared` = 先落到协议端能读的目录 |
| `send_protocol_path` | 空 | 协议端**能读**的发件目录，如 `/app/napcat/data/dsh-outbox` |
| `send_outbox_dir` | 空 | 仅当 AstrBot **写不了**协议端路径时填（同一块盘上 AstrBot 的挂载点） |
| `send_inline_images` | 开 | png/jpg/gif/webp/bmp 且 ≤5MB 用图片消息发；关掉一律按文件发 |
| `send_inline_videos` | 开 | mp4/mov/avi/mkv/webm 用视频消息发，失败回退文件卡片 |
| `inbound_share_dir` | 空 | 入站大文件（>12MB）暂存目录（AstrBot 侧路径）。**同机部署留空即可**，会自动用 DSH 工作区下的 `.dsh-inbox`；分容器才要填，如 `/mnt/d/proj/.dsh-inbox` |
| `inbound_dsh_prefix` | 空 | 同一目录在 DSH 侧的写法，如 `D:\proj\.dsh-inbox` |
| `ingress_url_candidates` | 空 | ingress 地址的额外候选（完整地址或裸端口）。留空时内置 `host.docker.internal:3188` 与 `127.0.0.1:3188`，用 `/health` 探一遍取第一个通的 |
| `outbound_pull` | 开 | **出站兜底**：DSH 要发的文件如果 AstrBot 这边（容器里）看不到，就向 ingress 要一次性凭证自己拉过来再发 —— Docker 官方 compose 下出站文件因此不用挂盘 |
| `outbound_pull_max_mb` | 200 | 出站拉取的单文件上限（DSH 侧还有 `outboundUrlMaxMb`） |
| `inbound_url_base` | 空 = 自动候选 | **URL 入站**：DSH 能访问到的 AstrBot 基址，如 `http://127.0.0.1:10000`（**宿主机的端口**，不是容器里的 6185）。留空则自动探测候选 |
| `inbound_url_candidates` | 空 | 候选地址（每行一个完整地址或裸端口）。Docker 下把宿主机映射的端口列进来，插件会让 DSH 侧先探一次 |
| `inbound_url_mode` | `auto` | `auto` = 只有超过 12MB 的附件走 URL；`always` = 所有附件都走 URL（完全不依赖共享目录）；`off` = 关 |
| `inbound_url_max_mb` | 200 | URL 入站的单文件上限（DSH 侧 `inboundUrlMaxMb` 也要够） |
| `official_send_mode` | `passive-first` | 官方 QQ 机器人的发送方式，见下 |
| `trace_delivery` | 关 | 诊断：打开后每轮在 AstrBot 日志里多几行 `[dsh-trace]`（SSE 事件到达时刻 / 每条正文的发送时刻 / 被动还是主动）。只在排查「正文慢一拍」「消息顺序不对」这类跨进程时序问题时开，排完关掉 |

### 官方 QQ 机器人的发送方式

被动回复有硬限制：**同一会话 5 分钟内最多回复 5 次**（超了报 `40034128`），所以默认第 2 条起走主动消息。

| 值 | 行为 |
|---|---|
| `passive-first`（默认） | 本轮第一条作被动回复，之后转主动 |
| `proactive` | 全部走主动 |
| `passive` | 全被动（会撞 5 次上限） |

主动消息报 `40034105 主动消息失败, 无权限` 时，先在开放平台给**该群**开启「主动通知」；插件遇到该错误会自动回退被动，并在本进程内不再尝试主动。非官方通道（NapCat 等）不受影响。

## 过程显示

`progress_mode` 决定「过程」怎么发。三档都照发：最终结果、`—— 本回合结束`、文件、报错、`任务已停止`、附件提示、权限确认。

| 值 | 群里看到什么 |
|---|---|
| `digest`（默认） | `已交给 DeepSeek Harness…` → 每 `progress_interval_sec` 秒一条 `⏳ 已跑 3 分钟 · 工具 7 次 · 最近：edit · bash`（这段时间没调工具就报「仍在思考：<思考首行>」；等你批准权限时不报）→ 最终结果 + `—— 本回合结束` |
| `full` | 实时发思考首行、工具行（每 5 条并一条）、心跳摘要、`—— 本回合结束` |
| `minimal` | 过程不出声，只发最终结果 + `—— 本回合结束` |

`progress_interval_sec` **同时是保活间隔**：这条 SSE 在 AstrBot 侧按「读空闲」算超时，所以它必须小于 `timeout_sec`。

## 部署与共享目录

配置里填的都是**容器内视角**的路径；真正让文件互相读到的是 compose 里的挂载。目录会跨两个方向用，别混：

```
宿主机(DSH)             AstrBot 容器              协议端容器(NapCat/SnowLuma)
D:\proj\.dsh-inbox  ⇄   /mnt/d/proj/.dsh-inbox    —                     入站：AstrBot ↔ DSH
<共享盘>            ⇄   <AstrBot 挂载点>      ⇄   <协议端挂载点>         出站：AstrBot ↔ 协议端
```

### 同机直装（AstrBot 与协议端都在宿主机）

不用映射，也不用填这些路径：

| 项 | 值 |
|---|---|
| `send_file_mode` | `direct` |
| `inbound_share_dir` / `inbound_dsh_prefix` | 留空（≤12MB 走 base64；更大的自动落到 `<工作区>/.dsh-inbox`） |

### 不挂共享盘：URL 入站（Docker 尤其有用）

填 `inbound_url_base` 后，附件不再经 base64 或共享目录，而是由插件登记成**一次性 URL**
（AstrBot 的 `/api/file/<token>`，默认 5 分钟有效、不需要登录态），DSH 自己去下载并落进
`<工作区>/.dsh-inbox/`：

| 项 | 值 |
|---|---|
| `inbound_url_base` | **DSH 那台机器**能访问到的 AstrBot 地址。例：`http://127.0.0.1:10000`（`docker port` 里 6185 映射到的宿主端口），或局域网 IP `http://192.168.1.5:10000`。**留空也能用**：会按「本机 dashboard 端口 → `inbound_url_candidates`」自动探测 |
| `inbound_url_candidates` | 候选地址，每行一个：`10000` 或 `http://192.168.1.5:10000`。宿主机映射端口写这里，插件让 DSH 侧先取一次确认 |
| `inbound_url_mode` | `auto`（默认，>12MB 才走 URL）/ `always`（全走，彻底不要共享目录）/ `off` |
| `inbound_url_max_mb` | 默认 200；DSH 侧 `inboundUrlMaxMb` 也要够 |

- **别照抄 `callback_api_base`**：那是给协议端看的（Docker 里常是 `http://astrbot:6185`），
  宿主机上的 DSH 解析不了 —— 所以这里要填「宿主视角」的地址。
- 失败会自动回退：URL 拿不到 → 共享目录 → base64 → 最后才是「太大」提示。
- 出站不受影响：文件看不见时走 `outbound_pull` 兜底（见下），根本不依赖共享目录。

### AstrBot 与协议端分容器（Docker）

**把同一块宿主机目录在两个容器里挂成同一个路径** —— 配置最少，也不会两边对不上：

```yaml
services:
  astrbot:
    volumes:
      - ./napcat-data:/app/napcat/data   # 出站：与协议端挂同一路径
      - "D:/:/mnt/d"                     # 入站：DSH 要能读到 AstrBot 写的附件
  napcat:                                # 或 snowluma
    volumes:
      - ./napcat-data:/app/napcat/data   # 与 AstrBot 完全相同的挂载点
```

| 项 | NapCat | SnowLuma | 同机直装 |
|---|---|---|---|
| `send_file_mode` | `shared`（或 `auto`） | 同左 | `direct` |
| `send_protocol_path` | `/app/napcat/data/dsh-outbox` | `/app/snowluma-data/dsh-outbox` | 留空 |
| `send_outbox_dir` | 留空（两边同路径，用不上） | 留空 | 留空 |
| `inbound_share_dir` | `/mnt/d/<工作区>/.dsh-inbox` | 同左 | 留空（自动兜底） |
| `inbound_dsh_prefix` | `D:\<工作区>\.dsh-inbox` | 同左 | 留空 |

- **出站**：AstrBot 把文件写到 `send_protocol_path`，再把同一路径告诉协议端；两边挂载点相同时只写一份。挂载点确实不同名时，再补 `send_outbox_dir`（AstrBot 侧的名字）。
- **入站**：大文件由 AstrBot 写进 `inbound_share_dir`，把 `inbound_dsh_prefix` 那侧的路径发给 DSH。同机部署不用填这两项，插件会请网关给出该会话的工作区，自动用 `<工作区>/.dsh-inbox`。
- 内置了两个兜底目录（`/app/snowluma-data/dsh-outbox`、`/app/napcat/data/dsh-outbox`），**只在父目录确实存在时**才用，且只补建最后一级；稳妥起见显式填 `send_protocol_path`。
- Linux Docker 把 `D:/:/mnt/d` 换成 `-v /home/me/workspace:/mnt/workspace`，配置里的 `D:\...` 相应改成 `/mnt/workspace/...`。
- 官方 QQ 机器人由 AstrBot 自己上传附件，出站固定 `direct`，不需要共享盘。
- **出站文件已经不用挂盘了**：包里看不到文件时，插件向 ingress 要一张一次性凭证把文件拉进
  `data/temp` 再发（`outbound_pull`，默认开）。要挂共享盘的主要是**出站图片 / 视频**这类走
  AstrBot 自己上传路径的内容 —— 那种情况更省事的做法是填 AstrBot 主配置的 `callback_api_base`
  （协议端能访问到的 AstrBot 地址，如 `http://astrbot:6185`），让它注册成 URL 交给协议端下载。

## 用法

群聊默认要管理员，或把群号写进 `allow_groups`。唤醒词以 `/` 为例：

```
/dsh 帮我看这个报错
/dsh status
/dsh ws [n|路径]
/dsh ls
/dsh use <n|短id>
/dsh new
/dsh end
/dsh stop
/dsh steer 改用那个方案
/dsh rename 新标题
/dsh model [n|provider/model] [effort]
/dsh perm [n|名字]
/dsh compact
/dsh last [N]
/dsh send <路径>
```

- `status` 报上下文占用与累计 token 构成；`compact` 转发 DSH 自带的 `/compact`（会话空闲时）
- `model`、`perm`、`cwd`（`ws`）只作用于当前绑定会话，且 durable
- `last [N]` 从会话日志补发最近 N 条助手回复，用于超时、重载或断线后找回已跑完的结果
- `session_scope = group`（默认）时一个群共用一条会话；`= user` 时群里每人一条，各自有独立的上下文 / 工作区 / 模型 / 权限。想共用的人各自 `/dsh use <同一个短id>`（`/dsh ls` 查短 id）即可手动组队；多人绑同一条会话是串行 + 打断的，适合轮流用。注意切成 `user` 后原来的群会话默认闲置，自己也要 `/dsh use <短id>` 接回来；回复仍发在群里，隔离的是**上下文**而非隐私
- `/dsh end` 只解开绑定，不删 DSH 历史

### 续聊

- 本会话绑定过 DSH 后，**引用机器人的回复**即可续聊，不必每句 `/dsh`
- 官方群聊还可以 **`@机器人 + 说话`**（`official_at_continue`）
- 引用**人格**（如安魂曲）的回复不会被当成 DSH 续聊
- **`@了别人` 的引用不算续聊**：那是在跟那个人说话。顺带挡住一个坑 —— 「引的是不是 DSH 的正文」
  只能按内容认，别人把机器人的正文复制成自己的消息再被你引用时内容一模一样；有了这条判断就不会误接

### 附件与媒体

- **直接发**：图 / 文件 / 视频作为聊天附件发出，同一条消息里带 `/dsh …`；已绑定的会话里直接发附件也行（`bound_media_passthrough`）
- **引用发**：引用一条带图 / 文件的消息，再 `/dsh …` 或 `@机器人`（官方 Bot 同样支持）
- 附件经 HTTP 传到 DSH，落在当前工作区 `.dsh-inbox/`。群「上传到群文件」不是聊天附件，收不到
- **大文件**：>12MB 的图 / 文件 / 视频会先拷进暂存目录、只把路径发给 DSH（绕开 base64 上限），本轮结束后删掉暂存副本。目录取 `inbound_share_dir`；同机部署留空也会自动兜底成 `<DSH 工作区>/.dsh-inbox`
- **视频**：只落盘并把路径交给模型。DSH 没有视频内容块，模型看不到画面，但可以用 `ffmpeg` 读元信息、抽帧、转码，并把产物发回
- **语音**（`Record`）本插件不处理；需要的话在 AstrBot 打开内置 STT（`provider_stt_settings.enable`），语音会先被转成文字
- **出站**：模型按技能 `dsh-qq-send-file` 在回复里写 `[SEND_FILE: 工作区内绝对路径]`；用户也可 `/dsh send <路径>`
- **白名单**：管理员永远放行；群聊看 `allow_groups`，私聊看 `allow_users`（互不通用）。私聊里名单外的人不会收到拒绝提示，消息照常交给人格；群聊里名单外**明确在敲命令**（发 `/dsh …`）才会回一句说明。**回答类（纯数字 / `批准` / `取消`）和「引用 DSH 的回复」都不提示** —— 前者是被特意接住的（免得被人格当闲聊答掉），后者名单外的人引用那条提示来问「这什么情况」时再回一遍就成了复读机；两种都静默放行、交回人格（他们本来也接不上会话）
- **平台差异**：官方 QQ 机器人自动用 Markdown 发送（粗体 / 代码块会渲染，表格与 `![图]()` 降级），Markdown 被拒时回退纯文本；NapCat 等仍是纯文本

## 提问与回答

DSH 用 `ask_user_question` 问你时（含 plan 模式的计划确认），问题会发到聊天窗口：

```
❓ 需要你回答 (#2)
部署：怎么发？
  1. 被动 — 先被动后主动
  2. 主动
（这题可多选）

回复编号即可（多选用逗号，如 `1,3`）；想说别的就 `答 你的说法`；不想回答发 `取消`。
等待 10 分钟，超时不会替你选。
```

| 回答方式 | 例子 |
|---|---|
| 直接回编号 | `1`、`1,3`、`1 3`（选项跨问题连号） |
| 自定义回答 | `答 都用主动`、`回答：先按第二种试` |
| 引用 / @ 之后说话 | 引用那条提问再打字，或 `@机器人 用第二种` |

- **超时和 `取消` 都不会替你选**：计划确认的选项里就有「批准」，替你选等于自动放行。超时或取消时工具会带一句说明失败，模型自己接着往下走
- **命令词优先**：有提问挂着时 `stop` / `status` / `send …` 照旧当命令执行；想说自由文本回答用 `答 …` 前缀
- 等待上限沿用 ingress 的 `approvalTimeoutSec`（默认 600 秒）；等你回答期间那条周期 `⏳` 汇报会暂停
- QQ 与 DSH 网页**并行，谁先答算谁的**；只有发起那一轮的人能在 QQ 里回答

## 和 AstrBot 人格的关系

默认**不接管**日常聊天：没有 `/dsh`、没有引用 DSH 回复、也不是审批 / 提问的回复时，消息仍走你原来的人格。

## 故障

| 现象 | 处理 |
|---|---|
| 连不上 ingress | DSH 是否在跑、`3188` 是否监听、URL 按同机 / Docker 填对 |
| `401` | `token` 与 DSH 侧 `config.json` 不一致 |
| 群里发了没反应 | 需要 `/dsh`、`@机器人` 或引用机器人；确认管理员或白名单 |
| 模型只回文件路径、不描述画面 | 当前 DSH 模型不支持视觉，换带 image 输入的模型 |
| 主动消息 `40034105` | 在开放平台给该群开启「主动通知」 |
