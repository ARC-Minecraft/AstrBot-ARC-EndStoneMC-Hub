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
- **MC AI 对话 RPC**（`ai_chat`）：[AI Helper](https://github.com/ARC-Minecraft/EndstoneMC-ARC-AI-Helper) 将玩家消息送入 AstrBot 正式对话管线，人格 / 记忆由 AstrBot 维护。身份映射：发送者 ID = 玩家 **XUID**，群号 = **Minecraft 服务器名称**。服务角色 `ai_helper` 不占用子服编号、也不会播报开停服
- **MC AI 服务器工具**（`ai_tool`）：给大模型提供 `mc_list_players` / `mc_get_tps` / `mc_server_info` / `mc_run_command`，由对应服的 AI Helper 在游戏主线程执行（`stop`/`kill` 仍禁止，`gamemode` 仅 OP）

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
| `admins` | 插件管理指令 QQ（`/mc who`、`/mc ban` 等） |
| `sync_group_card` | 绑定后是否改群名片 |
| `forward_qq_chat` | 是否转发普通群聊到 MC |
| `ai_chat_timeout` | MC AI 助手走 AstrBot 对话的超时秒数，默认 180 |

MC 子服侧（[QQ Sync](https://github.com/ARC-Minecraft/EndstoneMC-ARC-QQ-Sync-Plugin)）只需：`hub_host` / `hub_port` / `hub_token`（及可选互不相同的 `server_name`）。

AI Helper 使用独立连接：`register.role = "ai_helper"`，不会占用子服编号，也不会播报开停服。欢迎包含 `ai_chat: true` 与 `features: ["ai_chat", "ai_tools"]`。需要先升级本中枢，再启用 AI Helper 的 AstrBot 对话。

## 启停与连接提示

子服可能被直接杀进程，发不出 `server_stop`。因此 **QQ 与跨服提示都以中枢 WebSocket 连上/断开为准**：

- 连上 → 扇出 `server_connected` + QQ「服务器已启动！」
- 断开 → 扇出 `server_disconnected` + QQ「服务器已停止！」
- 子服自带的 `server_start` / `server_stop` 不再向 QQ/其他服重复播报

AI Helper 的 `ai_helper` 连接**不走**上述开停服播报。

## 更新日志

- **1.4.0**：MC AI 会话改为群聊语义：用户 ID 用玩家 XUID、群号用服务器名称，便于记忆插件按 ID 对上人（改名仍是同一人）；新增 `ai_tool` 反向 RPC 与 `mc_list_players` / `mc_get_tps` / `mc_server_info` / `mc_run_command` 工具。需搭配 AI Helper ≥ 1.2.0。
- **1.3.0**：新增 `ai_chat` RPC，可把 Minecraft AI 助手消息送进 AstrBot 对话管线（人格 / 记忆由 AstrBot 维护）；`role=ai_helper` 服务连接不占用子服编号。

## 许可证

[MIT License](LICENSE)
