# 弧光 EndStone 消息中枢

AstrBot 插件（目录名 `astrbot_plugin_endstone_arc`）：QQ ↔ Minecraft 的 WebSocket 中枢。

仓库：[ARC-Minecraft/AstrBot-ARC-EndStoneMC-Hub](https://github.com/ARC-Minecraft/AstrBot-ARC-EndStoneMC-Hub)

## 配套依赖

本中枢与 MC 子服插件**成对使用**，缺一不可：

| 组件 | 仓库 |
|------|------|
| **本仓库（AstrBot 中枢）** | [AstrBot-ARC-EndStoneMC-Hub](https://github.com/ARC-Minecraft/AstrBot-ARC-EndStoneMC-Hub) |
| **MC 子服客户端** | [EndstoneMC-ARC-QQ-Sync-Plugin](https://github.com/ARC-Minecraft/EndstoneMC-ARC-QQ-Sync-Plugin) |

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

MC 子服侧（[QQ Sync](https://github.com/ARC-Minecraft/EndstoneMC-ARC-QQ-Sync-Plugin)）只需：`hub_host` / `hub_port` / `hub_token`（及可选互不相同的 `server_name`）。

## 启停与连接提示

子服可能被直接杀进程，发不出 `server_stop`。因此 **QQ 与跨服提示都以中枢 WebSocket 连上/断开为准**：

- 连上 → 扇出 `server_connected` + QQ「服务器已启动！」
- 断开 → 扇出 `server_disconnected` + QQ「服务器已停止！」
- 子服自带的 `server_start` / `server_stop` 不再向 QQ/其他服重复播报

## 许可证

[MIT License](LICENSE)
