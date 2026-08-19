# 弧光 EndStone 消息中枢
[![Codacy Grade](https://app.codacy.com/project/badge/Grade/ac1ce35120504313aee1c2fd0cda7277)](https://app.codacy.com/gh/ARC-Minecraft/AstrBot-ARC-EndStoneMC-Hub/dashboard?utm_source=gh&utm_medium=referral&utm_content=&utm_campaign=Badge_grade)


AstrBot 插件（目录名 `astrbot_plugin_endstone_arc`）：QQ ↔ Minecraft 的 WebSocket 中枢。

仓库：[ARC-Minecraft/AstrBot-ARC-EndStoneMC-Hub](https://github.com/ARC-Minecraft/AstrBot-ARC-EndStoneMC-Hub)

## 配套依赖

本中枢与 MC 子服插件**成对使用**：

| 组件 | 仓库 |
|------|------|
| **本仓库（AstrBot 中枢）** | [AstrBot-ARC-EndStoneMC-Hub](https://github.com/ARC-Minecraft/AstrBot-ARC-EndStoneMC-Hub) |
| **MC 子服 QQ 互通** | [EndstoneMC-ARC-QQ-Sync-Plugin](https://github.com/ARC-Minecraft/EndstoneMC-ARC-QQ-Sync-Plugin) |
| **MC 子服 AI 助手（可选）** | [EndstoneMC-ARC-AI-Helper](https://github.com/ARC-Minecraft/EndstoneMC-ARC-AI-Helper) |

```
QQ / 其他平台
      ↕  AstrBot 平台适配器
本插件「弧光EndStone消息中枢」（WebSocket，默认 :19136）
      ↕  Hub JSON 协议
EndstoneMC-ARC-QQ-Sync-Plugin（各 MC 子服）
```

## 职责

- 监听 WebSocket（默认 `0.0.0.0:19136`），接受各 MC 子服 HubClient
- 群聊 / 指令 ↔ 游戏双向转发
- 跨服事件扇出（join / quit / chat / death / custom）固定开启
- 群指令统一要求 `/mc` 前缀（如 `/mc help`、`/mc cmd stop`），剥前缀后再下发子服
- QQ 绑定数据权威存储（`data.json` / data_rpc）
- 可选同步群名片（`sync_group_card`）
- **MC AI 对话 RPC**（`ai_chat`）：[AI Helper](https://github.com/ARC-Minecraft/EndstoneMC-ARC-AI-Helper) 将玩家消息送入 AstrBot 正式对话管线，人格 / 记忆由 AstrBot 维护。身份映射：已绑定则发送者 ID = **QQ 号**，未绑定则用 **XUID**；不传群号。弧光天星回复同样走弧光护卫关键词检测：命中则拦截原文，并对触发者施加与玩家自己说违禁词相同的处罚。服务角色 `ai_helper` 不占用子服编号、也不会播报开停服
- **MC AI 服务器工具**（`ai_tool`）：给大模型提供 `mc_list_servers` / `mc_list_players` / `mc_get_tps` / `mc_server_info` / `mc_run_command` / `mc_jail_player` / `mc_release_player` / `mc_list_prisoners` / `mc_skyeye_player` / `mc_skyeye_combat` / `mc_skyeye_location`。游戏内打在消息来源服；外部对话需插件管理员先发 **`/mc activate`** 激活本会话（会话 ID 可为非数字字符串）。多开服通过 `server` 指定；**天眼查询 `server` 可留空，会搜索全部已连接服务器**（玩家不必在线）。能识别 QQ 群身份时，入狱 / 天眼 / 任意改世界指令仅管理员；**已绑定 QQ 用户**可在求助时对**本人绑定角色**使用 tp / effect / spawnpoint 等自救指令；**未绑定**用户无权执行改世界类工具。

## 安装

将本仓库内容放入 AstrBot：

```text
AstrBot/data/plugins/astrbot_plugin_endstone_arc/
```

然后在 AstrBot 中启用插件，并按下方配置填写群号、端口等。

## 配置要点

| 项 | 说明 |
|----|------|
| `ws_port` | 与 MC `hub_port` / FRP 一致，默认 19136 |
| `auth_token` | 与 MC `hub_token` 一致 |
| `target_groups` | 同步的 QQ 群 |
| `admins` | 管理员 ID；首次启动写入 `admins.json`，之后 `/mc addadmin` 会同步回此项 |
| `super_admins` | 超级管理员 ID；可用 `/mc addadmin` / `/mc deladmin` 任免管理员 |
| `sync_group_card` | 绑定后是否改群名片 |
| `forward_qq_chat` | 是否转发普通群聊到 MC |
| `ai_chat_timeout` | MC AI 助手走 AstrBot 对话的超时秒数，默认 180 |
| `server_aliases` | QQ 里称呼到正式 `server_name` 的映射，例如 `主服` → `弧光基岩重塑服务器` |

MC 子服侧（[QQ Sync](https://github.com/ARC-Minecraft/EndstoneMC-ARC-QQ-Sync-Plugin)）只需：`hub_host` / `hub_port` / `hub_token`（及可选互不相同的 `server_name`）。

AI Helper 使用独立连接：`register.role = "ai_helper"`，不会占用子服编号，也不会播报开停服。欢迎包含 `ai_chat: true` 与 `features: ["ai_chat", "ai_tools"]`。需要先升级本中枢，再启用 AI Helper 的 AstrBot 对话。

## MC AI 工具参数一览

大模型通过 AstrBot `llm_tool` 调用；`event` 由框架注入，**不是**模型参数。外部对话须先 `/mc activate`。

`server` 通则：游戏内可留空（打在消息来源服）。QQ / 其它入口多开服时，查询与改世界一般要填名称、编号或别名。**天眼三类工具例外**：`server` 可留空，会搜全部已连接服务器；指定服查空也会自动再搜其它服。

| 工具 | 权限 | 参数 | 必填 | 默认 | 含义 |
|------|------|------|------|------|------|
| `mc_list_servers` | 已激活即可 | `reason` | 是 | | 为何查询，例如「要确认打哪台服」 |
| `mc_list_players` | 已激活即可 | `reason` | 是 | | 为何查询，例如「玩家问谁在线」 |
| | | `server` | 多开服时要填 | 空 | 目标服名称 / 编号 / 别名 |
| `mc_get_tps` | 已激活即可 | `reason` | 是 | | 为何查询，例如「玩家问 TPS」 |
| | | `server` | 多开服时要填 | 空 | 目标服名称 / 编号 / 别名 |
| `mc_server_info` | 已激活即可 | `reason` | 是 | | 为何查询，例如「玩家问服务器信息」 |
| | | `server` | 多开服时要填 | 空 | 目标服名称 / 编号 / 别名 |
| `mc_run_command` | 管理员；或已绑定用户仅限本人自救 | `command` | 是 | | 不含 `/` 的游戏指令。例：`effect Steve night_vision 30 0 true`；劈闪电：`execute at Steve run summon lightning_bolt ~ ~ ~`。禁止 `stop` / `kill`；`gamemode` 仅 OP/管理员 |
| | | `server` | 多开服时要填 | 空 | 目标服名称 / 编号 / 别名 |
| `mc_jail_player` | 仅管理员 | `player_name` | 是 | | 要关押的游戏内玩家名 |
| | | `duration` | 否 | 空 | 刑期分钟数，或 `-1` / `life` / `无期`；空则用服默认一键入狱时长 |
| | | `reason` | 否 | 空 | 入狱原因 |
| | | `server` | 多开服时要填 | 空 | 目标服名称 / 编号 / 别名 |
| `mc_release_player` | 仅管理员 | `player_name` | 是 | | 要释放的游戏内玩家名 |
| | | `server` | 多开服时要填 | 空 | 目标服名称 / 编号 / 别名 |
| `mc_list_prisoners` | 已激活即可 | `reason` | 是 | | 为何查询，例如「玩家问谁在坐牢」 |
| | | `server` | 多开服时要填 | 空 | 目标服名称 / 编号 / 别名 |
| `mc_skyeye_player` | 仅管理员 | `player_name` | 是 | | 游戏内玩家名；**不要求在线** |
| | | `minutes` | 否 | `30` | 由模型按用户说法换算的回溯**分钟数**。一天=`1440`，一小时=`60` |
| | | `action` | 否 | 空 | 限定行为类型，如 `BlockBreak` / `BlockPlace` / `ActorDamage` / `PlayerDeath` |
| | | `server` | 否 | 空 | **建议留空搜全服**；只查某一台时才填 |
| `mc_skyeye_combat` | 仅管理员 | `player_name` | 是 | | 游戏内玩家名；不要求在线 |
| | | `minutes` | 否 | `30` | 同上，模型换算后的分钟数 |
| | | `server` | 否 | 空 | **建议留空搜全服** |
| `mc_skyeye_location` | 仅管理员 | `x` | 是 | | X 坐标 |
| | | `y` | 是 | | Y 坐标 |
| | | `z` | 是 | | Z 坐标 |
| | | `radius` | 否 | `8` | 半径格数 |
| | | `dimension` | 否 | 空 | 维度，如 `minecraft:overworld`；空表示不限 |
| | | `minutes` | 否 | `30` | 模型换算后的回溯分钟数 |
| | | `server` | 否 | 空 | **建议留空搜全服** |

权限补充：QQ 群里「管理员」= 插件管理员 / 超级管理员，或能识别出的群主 / 群管。`mc_run_command` 对已绑定用户开放 tp / effect / spawnpoint 等自救，且只能打在本人绑定角色上；未绑定用户不能改世界。

## 启停与连接提示

子服可能被直接杀进程，发不出 `server_stop`。因此 **QQ 与跨服提示都以中枢 WebSocket 连上/断开为准**：

- 连上 → 扇出 `server_connected` + QQ「服务器已启动！」
- 断开 → 扇出 `server_disconnected` + QQ「服务器已停止！」
- 子服自带的 `server_start` / `server_stop` 不再向 QQ/其他服重复播报

AI Helper 的 `ai_helper` 连接**不走**上述开停服播报。

## 更新日志

- **1.6.13**：天眼查询不再要求指定服务器或玩家在线：`server` 留空（或指定服查不到）时会搜索全部已连接服务器。回溯时长由大模型按用户说法写入 `minutes`（一天=1440）。
- **1.6.12**：Minecraft 消息中枢里的弧光天星回复也走弧光护卫：命中关键词则拦截原文，并对触发玩家施加与自己说违禁词相同的处罚（监狱 / 群禁言 / 击杀 / 警告）。身份仍是绑定 QQ，否则 XUID。
- **1.6.11**：适配 QQ 官方机器人新的 `<@member_openid>` 提及格式。此前 `/mc addadmin @群名片` 会把 openid 开头的 `824346` 误当成 QQ 号。同时把运行时管理员列表写回插件配置。
- **1.6.10**：`/mc 绑定` 改为中枢本地处理，不再广播到所有子服。修复群内绑定时子服在 WebSocket 循环上同步等 data_rpc、心跳超时、全部断连的问题。需 QQ Sync ≥ 1.0.2。
- **1.6.9**：QQ 群 AI 求助权限分层：已绑定用户可对本人角色使用 tp / effect / spawnpoint 等自救指令；未绑定用户无权调用 `mc_run_command`（需先 `/mc 绑定`）。需 AI Helper ≥ 1.2.6。
- **1.6.8**：新增持久化管理员/超级管理员权限模型。`/mc addadmin @QQ`、`/mc deladmin @QQ`、`/mc admins` 由中枢本地处理；超级管理员可任免管理员，管理员与超级管理员其它权限一致。
- **1.6.7**：MC 聊天/进服/开停服等广播改为发往所有 `/mc activate` 过的会话；`/mc help`、`/mc servers` 等中枢本地指令回复只回来源会话，不再固定往 `target_groups` 发。
- **1.6.6**：修 `/mc` 指令被 LLM 聊天抢先消费的问题：用高优先级自定义过滤器在唤醒阶段拦截，并兼容 wake_prefix 剥掉开头 `/` 后的 `mc activate` 形式。
- **1.6.5**：新增 `/mc activate`（仅 `admins` 配置的管理员可用），在本会话激活 Minecraft AI 工具并持久化会话 ID（支持非数字 ID）。未激活会话不可调用工具。
- **1.6.4**：Minecraft 工具不再限定 `target_groups` / QQ 群号；其它适配器或无私聊群号的入口也能调用。QQ 群里改世界仍仅管理员。
- **1.6.3**：新增天眼查询工具 `mc_skyeye_player` / `mc_skyeye_combat` / `mc_skyeye_location`，需弧光核心 ≥ 0.8.8 与 AI Helper ≥ 1.2.5。QQ 里与执行指令一样仅管理员。
- **1.6.2**：新增监狱一键入狱工具 `mc_jail_player` / `mc_release_player` / `mc_list_prisoners`，需游戏内监狱插件 ≥ 0.0.2 与 AI Helper ≥ 1.2.4。QQ 里入狱/释放与执行指令一样仅管理员。
- **1.6.1**：工具说明补充劈闪电正确格式 `execute at 玩家名 run summon lightning_bolt ~ ~ ~`，避免写成 `effect 玩家名 summon`。
- **1.6.0**：QQ 群聊也可使用 Minecraft 工具（查在线 / TPS / 信息 / 执行指令）。多开服用 `server` 指定目标；执行指令仅插件管理员、群主和群管。需游戏内 AI Helper 在线。
- **1.5.1**：服务连接被同名替换时写日志，便于排查多开服 AI Helper 互踢。
- **1.5.0**：MC AI 身份改为「绑定 QQ 优先、否则 XUID」，不再传群号；执行指令仍在玩家发消息的那台子服上。需搭配 AI Helper ≥ 1.2.0。
- **1.4.0**：MC AI 会话改为群聊语义：用户 ID 用玩家 XUID、群号用服务器名称，便于记忆插件按 ID 对上人（改名仍是同一人）；新增 `ai_tool` 反向 RPC 与 `mc_list_players` / `mc_get_tps` / `mc_server_info` / `mc_run_command` 工具。需搭配 AI Helper ≥ 1.2.0。
- **1.3.0**：新增 `ai_chat` RPC，可把 Minecraft AI 助手消息送进 AstrBot 对话管线（人格 / 记忆由 AstrBot 维护）；`role=ai_helper` 服务连接不占用子服编号。

## 许可证

[MIT License](LICENSE)
