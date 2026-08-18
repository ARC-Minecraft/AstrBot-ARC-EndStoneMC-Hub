"""WebSocket Hub compatible with EndstoneMC-ARC-QQ-Sync-Plugin HubClient."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection, serve

from astrbot.api import logger

from .binding_store import BindingStore

HUB_MAX_NUMERIC_SERVER_ID = 9999
_mc_format_code_pattern = re.compile("\u00a7.")

# QQ 群内由弧光 EndStone 消息中枢识别的指令（群内须 /mc 前缀）
# 例如 /mc help、/mc cmd stop；剥前缀后以内形 /help、/cmd 下发 QQ Sync
ARC_GROUP_COMMANDS = frozenset(
    {
        "help",
        "servers",
        "list",
        "tps",
        "info",
        "cmd",
        "who",
        "ban",
        "unban",
        "banlist",
        "unbindqq",
        "reload",
        "重启",
        "绑定",
    }
)


def strip_minecraft_format_codes(text: str) -> str:
    """Remove Minecraft section format codes from text.

    Args:
        text: Raw game text that may contain section signs.

    Returns:
        Clean text without format codes.
    """
    if not text:
        return text
    return _mc_format_code_pattern.sub("", text)


def strip_mc_command_prefix(raw_message: str) -> str | None:
    """Strip required /mc prefix into internal /command form.

    Args:
        raw_message: Raw group text.

    Returns:
        Internal command starting with / (e.g. /help), or None if not /mc.
    """
    s = (raw_message or "").strip()
    if len(s) < 3 or not s.lower().startswith("/mc"):
        return None
    rest = s[3:]
    if not rest:
        return "/help"
    if not rest[0].isspace():
        # Require a space after /mc (avoid /mchelp colliding with other bots).
        return None
    rest = rest.lstrip()
    if not rest:
        return "/help"
    if rest.startswith("/"):
        return rest
    return f"/{rest}"


def parse_hub_command_routing(raw_message: str) -> tuple[str, int | None]:
    """Parse optional numeric server id from a QQ group command.

    Args:
        raw_message: Raw group command text (/mc ... or internal /...).

    Returns:
        Tuple of (command line without server id, target server id or None).
    """
    normalized = strip_mc_command_prefix(raw_message)
    s = normalized if normalized is not None else (raw_message or "").strip()
    if not s.startswith("/"):
        return s, None

    parts = s.split()
    if not parts:
        return s, None

    head = parts[0]
    cmd = head[1:].lower() if head.startswith("/") else head.lower()
    args = parts[1:]

    def _valid_sid(token: str) -> int | None:
        if not token.isdigit():
            return None
        sid = int(token)
        if 1 <= sid <= HUB_MAX_NUMERIC_SERVER_ID:
            return sid
        return None

    if cmd == "cmd":
        if args and (sid := _valid_sid(args[0])) is not None:
            return f"/cmd {' '.join(args[1:])}".strip(), sid
        return s, None

    if cmd == "who":
        if len(args) >= 2 and (sid := _valid_sid(args[-1])) is not None:
            return f"/who {' '.join(args[:-1])}".strip(), sid
        return s, None

    query_cmds = {"list", "tps", "info", "banlist", "help", "reload", "servers"}
    if cmd in query_cmds and args and (sid := _valid_sid(args[-1])) is not None:
        return f"{head} {' '.join(args[:-1])}".strip(), sid

    return s, None


def is_known_arc_command(raw_message: str) -> bool:
    """Return whether a QQ message is an ARC MC group command.

    Args:
        raw_message: Raw group text; must start with /mc.

    Returns:
        True if the leading command is handled by the message center / MC plugin.
    """
    normalized = strip_mc_command_prefix(raw_message)
    if normalized is None:
        return False
    head = normalized.split(None, 1)[0][1:]
    if not head:
        return False
    # ASCII commands are matched case-insensitively; Chinese stay as-is.
    key = head.lower() if head.isascii() else head
    return key in ARC_GROUP_COMMANDS


def is_mc_activate_command(raw_message: str) -> bool:
    """Return whether the message is ``/mc activate`` (handled locally, not MC)."""
    normalized = strip_mc_command_prefix(raw_message)
    if normalized is None:
        return False
    head = normalized.split(None, 1)[0][1:].lower()
    return head == "activate"


SendQQCallback = Callable[[str], Awaitable[None]]
MuteQQCallback = Callable[[str, int], Awaitable[bool]]
GetArcGuardApiCallback = Callable[[], Any]


class ArcHubServer:
    """ARC message center Hub: accepts MC HubClient connections."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        token: str,
        hub_server_name: str,
        binding_store: BindingStore,
        send_qq: SendQQCallback,
        set_group_card: Callable[[int, str], Awaitable[None]] | None = None,
        mute_qq: MuteQQCallback | None = None,
        get_arc_guard_api: GetArcGuardApiCallback | None = None,
        group_names: dict[str, str] | None = None,
        hub_admins: list[str] | None = None,
        sync_group_card: bool = True,
    ):
        self.host = host
        self.port = int(port)
        self.token = token or ""
        self.hub_server_name = hub_server_name or "弧光EndStone消息中枢"
        self.binding_store = binding_store
        self.send_qq = send_qq
        self.set_group_card = set_group_card
        self.mute_qq = mute_qq
        self.get_arc_guard_api = get_arc_guard_api
        self.group_names = group_names or {}
        self.hub_admins = {str(a) for a in (hub_admins or []) if str(a).strip()}
        self.sync_group_card = bool(sync_group_card)

        self.connected_servers: dict[str, ServerConnection] = {}
        self.ws_to_server: dict[ServerConnection, str] = {}
        self.service_clients: dict[ServerConnection, dict[str, str]] = {}
        self._server_numeric_id_by_name: dict[str, int] = {}
        self._next_server_numeric_id = 1
        self._running = False
        self._server = None
        self._serve_task: asyncio.Task | None = None
        self.process_ai_chat: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = (
            None
        )
        self.ai_chat_timeout = 180.0
        self._pending_ai_tool: dict[str, tuple[ServerConnection, asyncio.Future]] = {}

    def _ensure_server_numeric_id(self, server_name: str) -> int:
        if server_name not in self._server_numeric_id_by_name:
            self._server_numeric_id_by_name[server_name] = self._next_server_numeric_id
            self._next_server_numeric_id += 1
        return self._server_numeric_id_by_name[server_name]

    def get_server_catalog(self) -> list[dict[str, Any]]:
        items = [
            {"id": sid, "name": name}
            for name, sid in self._server_numeric_id_by_name.items()
        ]
        items.sort(key=lambda x: x["id"])
        return items

    def connected_server_names(self) -> list[str]:
        return list(self.connected_servers.keys())

    async def start(self) -> None:
        """Start the WebSocket Hub listener."""
        if self._running:
            return
        self._running = True
        logger.info(
            f"[弧光EndStone消息中枢] 启动 WebSocket Hub ws://{self.host}:{self.port}"
        )
        self._serve_task = asyncio.create_task(self._run_ws_server())

    async def stop(self) -> None:
        """Stop Hub and close all MC connections."""
        self._fail_pending_ai_tools("弧光消息中枢已停止")
        self._running = False
        for ws in list(self.connected_servers.values()):
            try:
                await ws.close()
            except Exception:
                pass
        self.connected_servers.clear()
        self.ws_to_server.clear()
        self.service_clients.clear()
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            try:
                await server.wait_closed()
            except Exception:
                pass
        if self._serve_task is not None:
            try:
                await self._serve_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._serve_task = None
        logger.info("[弧光EndStone消息中枢] Hub 已停止")

    async def _run_ws_server(self) -> None:
        try:
            async with serve(
                self._handle_server_connection,
                self.host,
                self.port,
                ping_interval=20,
                ping_timeout=10,
            ) as server:
                self._server = server
                logger.info(
                    f"[弧光EndStone消息中枢] Hub 已监听 ws://{self.host}:{self.port}"
                )
                await server.wait_closed()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[弧光EndStone消息中枢] Hub 启动失败: {e}")
            self._running = False
        finally:
            self._server = None

    async def _handle_server_connection(self, websocket: ServerConnection) -> None:
        server_name = None
        try:
            try:
                raw = await asyncio.wait_for(websocket.recv(), timeout=15)
                data = json.loads(raw)
            except asyncio.TimeoutError:
                logger.warning("[弧光EndStone消息中枢] 连接超时，未收到注册消息")
                await websocket.close()
                return
            except json.JSONDecodeError:
                logger.warning("[弧光EndStone消息中枢] 收到无效注册消息")
                await websocket.close()
                return

            if data.get("type") != "register":
                logger.warning(
                    f"[弧光EndStone消息中枢] 期望 register，收到: {data.get('type')}"
                )
                await websocket.close()
                return

            if self.token and data.get("token") != self.token:
                logger.warning(
                    f"[弧光EndStone消息中枢] Token 验证失败: {data.get('server_name')}"
                )
                await websocket.close(1008, "认证失败")
                return

            server_name = str(data.get("server_name") or "未知服务器")
            role = str(data.get("role") or "server").strip().lower() or "server"
            is_service = role in {"ai_helper", "service"}

            if is_service:
                await self._bind_service_client(websocket, server_name, role)
                logger.info(
                    f"[弧光EndStone消息中枢] 服务客户端 [{server_name}] role={role} 已连接"
                )
                welcome = self._build_welcome_payload(my_server_id=None)
                await websocket.send(json.dumps(welcome, ensure_ascii=False))
            else:
                if server_name in self.connected_servers:
                    old_ws = self.connected_servers[server_name]
                    try:
                        await old_ws.close()
                    except Exception:
                        pass
                    self.ws_to_server.pop(old_ws, None)

                self.connected_servers[server_name] = websocket
                self.ws_to_server[websocket] = server_name
                remote_id = self._ensure_server_numeric_id(server_name)
                logger.info(
                    f"[弧光EndStone消息中枢] 子服 [{server_name}] 已连接（编号 {remote_id}）"
                )

                welcome = self._build_welcome_payload(my_server_id=remote_id)
                await websocket.send(json.dumps(welcome, ensure_ascii=False))

                # Hub WS connect is authoritative (force-killed MC may never send server_start).
                await self._broadcast_to_others(
                    server_name,
                    {
                        "type": "cross_server_event",
                        "from_server": server_name,
                        "event": "server_connected",
                    },
                )
                await self.send_qq(f"[{server_name}]\n服务器已启动！")

            async for message in websocket:
                try:
                    msg_data = json.loads(message)
                    await self._handle_server_message(server_name, websocket, msg_data)
                except json.JSONDecodeError:
                    logger.warning(f"[弧光EndStone消息中枢] [{server_name}] 无效 JSON")

        except websockets.exceptions.ConnectionClosed:
            if server_name:
                kind = "服务客户端" if websocket in self.service_clients else "子服"
                logger.info(f"[弧光EndStone消息中枢] {kind} [{server_name}] 断开连接")
        except Exception as e:
            logger.error(f"[弧光EndStone消息中枢] 处理子服连接出错: {e}")
        finally:
            was_service = websocket in self.service_clients
            self.service_clients.pop(websocket, None)
            self.ws_to_server.pop(websocket, None)
            if was_service:
                self._fail_pending_ai_tools("AI Helper 连接已断开", websocket)
            if server_name and not was_service:
                self.connected_servers.pop(server_name, None)
                # Hub WS drop is authoritative (force-killed MC may never send server_stop).
                await self._broadcast_to_others(
                    server_name,
                    {
                        "type": "cross_server_event",
                        "from_server": server_name,
                        "event": "server_disconnected",
                    },
                )
                try:
                    await self.send_qq(f"[{server_name}]\n服务器已停止！")
                except Exception:
                    pass

    async def _handle_server_message(
        self, server_name: str, ws: ServerConnection, data: dict
    ) -> None:
        msg_type = data.get("type")
        if msg_type == "game_event":
            await self._handle_game_event(server_name, data)
        elif msg_type == "api_send":
            text = data.get("text", "")
            if text:
                await self.send_qq(strip_minecraft_format_codes(str(text)))
        elif msg_type == "ping":
            await ws.send(json.dumps({"type": "pong"}))
        elif msg_type == "data_rpc":
            await self._handle_data_rpc(ws, data)
        elif msg_type == "set_group_card":
            if self.sync_group_card and self.set_group_card:
                try:
                    uid = int(data.get("user_id"))
                    card = str(data.get("card") or "")
                    if card:
                        await self.set_group_card(uid, card)
                except Exception as e:
                    logger.warning(f"[弧光EndStone消息中枢] 改群名片失败: {e}")
        elif msg_type == "restart_vote_online_list_response":
            # Restart vote is handled on MC side via command_forward; ignore here.
            pass
        elif msg_type == "ai_chat":
            asyncio.create_task(self._handle_ai_chat(server_name, ws, data))
        elif msg_type == "ai_tool_response":
            rid = str(data.get("request_id") or "")
            pending = self._pending_ai_tool.get(rid)
            if pending:
                _ws, fut = pending
                if fut and not fut.done():
                    fut.set_result(data)
        else:
            logger.debug(f"[弧光EndStone消息中枢] [{server_name}] 未知类型: {msg_type}")

    def _build_welcome_payload(self, my_server_id: int | None) -> dict[str, Any]:
        """Build hub_welcome JSON for a newly registered client.

        Args:
            my_server_id: Numeric id for game servers; None for service clients.

        Returns:
            Welcome payload including ``ai_chat`` capability flag.
        """
        payload: dict[str, Any] = {
            "type": "hub_welcome",
            "connected_servers": list(self.connected_servers.keys()),
            "hub_server_name": self.hub_server_name,
            "server_catalog": self.get_server_catalog(),
            "sync_group_card": self.sync_group_card,
            "hub_admins": sorted(self.hub_admins),
            "features": ["ai_chat", "ai_tools"],
            "ai_chat": True,
        }
        if my_server_id is not None:
            payload["my_server_id"] = my_server_id
        return payload

    async def _bind_service_client(
        self, websocket: ServerConnection, server_name: str, role: str
    ) -> None:
        """Register a non-game Hub client (e.g. AI Helper) without fan-out.

        Args:
            websocket: Client WebSocket.
            server_name: Client-chosen name (should not collide with game servers).
            role: Service role such as ``ai_helper``.
        """
        for old_ws, meta in list(self.service_clients.items()):
            if meta.get("name") == server_name:
                self.service_clients.pop(old_ws, None)
                logger.warning(
                    "[弧光EndStone消息中枢] 服务连接 [%s] 已被新连接替换（多开服请使用互不相同的 server_name）",
                    server_name,
                )
                try:
                    await old_ws.close()
                except Exception:
                    pass
                break
        self.service_clients[websocket] = {"name": server_name, "role": role}

    def _fail_pending_ai_tools(
        self, reason: str, websocket: ServerConnection | None = None
    ) -> None:
        """Wake pending MC tool RPCs when a helper connection drops.

        Args:
            reason: Error text stored on unfinished futures.
            websocket: If set, only fail RPCs waiting on this helper.
        """
        exc = ConnectionError(reason)
        for rid, (ws, fut) in list(self._pending_ai_tool.items()):
            if websocket is not None and ws is not websocket:
                continue
            if not fut.done():
                fut.set_exception(exc)
            self._pending_ai_tool.pop(rid, None)

    def find_ai_helper_ws(self, game_server: str) -> ServerConnection | None:
        """Find the AI Helper WebSocket for a Minecraft server name.

        Args:
            game_server: Game server display name.

        Returns:
            Connected helper WebSocket, or None.
        """
        target = str(game_server or "").strip()
        if not target:
            return None
        expected = f"{target}#ai-helper"
        for ws, meta in list(self.service_clients.items()):
            if str(meta.get("role") or "") != "ai_helper":
                continue
            name = str(meta.get("name") or "")
            if name == expected or name == target or name.split("#", 1)[0] == target:
                return ws
        return None

    def list_ai_helper_game_names(self) -> list[str]:
        """Return connected AI Helper game-server names without the ``#ai-helper`` suffix.

        Returns:
            Unique game server names currently registered as ``ai_helper``.
        """
        names: list[str] = []
        seen: set[str] = set()
        for meta in list(self.service_clients.values()):
            if str(meta.get("role") or "") != "ai_helper":
                continue
            raw = str(meta.get("name") or "").strip()
            game = raw.split("#", 1)[0].strip() if raw else ""
            if game and game not in seen:
                seen.add(game)
                names.append(game)
        names.sort()
        return names

    async def call_ai_tool(
        self,
        game_server: str,
        action: str,
        args: dict[str, Any] | None = None,
        *,
        timeout: float = 20,
    ) -> dict[str, Any]:
        """Ask the MC AI Helper to run a server tool and return its JSON result.

        Args:
            game_server: Origin Minecraft server name.
            action: Tool action such as ``list`` / ``tps`` / ``info`` / ``cmd``.
            args: Extra arguments for the action.
            timeout: Seconds to wait for the helper reply.

        Returns:
            Helper ``ai_tool_response`` payload.

        Raises:
            RuntimeError: When the helper is not connected.
            TimeoutError: When the helper does not reply in time.
        """
        ws = self.find_ai_helper_ws(game_server)
        if ws is None:
            raise RuntimeError(f"服务器 [{game_server}] 的 AI Helper 未连接")
        request_id = str(uuid.uuid4())
        fut = asyncio.get_running_loop().create_future()
        self._pending_ai_tool[request_id] = (ws, fut)
        await ws.send(
            json.dumps(
                {
                    "type": "ai_tool",
                    "request_id": request_id,
                    "action": str(action),
                    "args": args or {},
                },
                ensure_ascii=False,
            )
        )
        try:
            resp = await asyncio.wait_for(fut, timeout=max(5.0, float(timeout)))
        except asyncio.TimeoutError as e:
            raise TimeoutError(f"AI 工具超时: {action}") from e
        finally:
            self._pending_ai_tool.pop(request_id, None)
        if not isinstance(resp, dict):
            raise RuntimeError("AI 工具返回格式异常")
        return resp

    async def _handle_ai_chat(
        self, server_name: str, ws: ServerConnection, data: dict
    ) -> None:
        """Handle an MC AI chat RPC and reply on the same WebSocket.

        Args:
            server_name: Registered client name (game server or ``name#ai-helper``).
            ws: Source WebSocket.
            data: Incoming ``ai_chat`` payload.
        """
        request_id = data.get("request_id")
        if not self.process_ai_chat:
            await ws.send(
                json.dumps(
                    {
                        "type": "ai_chat_response",
                        "request_id": request_id,
                        "ok": False,
                        "error": "弧光消息中枢未就绪，无法进行 AstrBot 对话",
                    },
                    ensure_ascii=False,
                )
            )
            return

        payload = dict(data)
        if not str(payload.get("server_name") or "").strip():
            payload["server_name"] = str(server_name).split("#", 1)[0]
        try:
            result = await self.process_ai_chat(payload)
            if not isinstance(result, dict):
                result = {"ok": False, "error": "AstrBot 返回格式异常"}
        except Exception as e:
            logger.error(f"[弧光EndStone消息中枢] ai_chat 失败: {e}")
            result = {"ok": False, "error": str(e)}

        await ws.send(
            json.dumps(
                {
                    "type": "ai_chat_response",
                    "request_id": request_id,
                    "ok": bool(result.get("ok")),
                    "reply": result.get("reply") or "",
                    "error": result.get("error") or "",
                },
                ensure_ascii=False,
            )
        )

    async def _handle_data_rpc(self, ws: ServerConnection, data: dict) -> None:
        request_id = data.get("request_id")
        action = data.get("action")
        args = data.get("args") or {}
        try:
            result = self.binding_store.run_action(str(action), args)
            await ws.send(
                json.dumps(
                    {
                        "type": "data_rpc_response",
                        "request_id": request_id,
                        "ok": True,
                        "result": result,
                    },
                    ensure_ascii=False,
                )
            )
        except Exception as e:
            logger.error(f"[弧光EndStone消息中枢] data_rpc {action} 失败: {e}")
            await ws.send(
                json.dumps(
                    {
                        "type": "data_rpc_response",
                        "request_id": request_id,
                        "ok": False,
                        "error": str(e),
                    },
                    ensure_ascii=False,
                )
            )

    async def _handle_game_event(self, server_name: str, data: dict) -> None:
        event = data.get("event")
        player = strip_minecraft_format_codes(str(data.get("player", "")))
        message = strip_minecraft_format_codes(str(data.get("message", "")))
        session_count = data.get("session_count")
        playtime_str = data.get("playtime_str", "")
        origin = str(data.get("server_name") or server_name)
        raw_player_name = strip_minecraft_format_codes(
            str(data.get("raw_player_name") or "")
        ).strip()

        # Chat: optional Arc Guard before QQ / cross-server fan-out.
        if str(event or "") == "chat":
            blocked = await self._maybe_block_forbidden_chat(
                origin_server=server_name,
                display_player=player,
                raw_player_name=raw_player_name,
                message=message,
            )
            if blocked:
                return

        qq_msg = self._format_qq_message(
            origin,
            str(event or ""),
            player,
            message,
            session_count,
            str(playtime_str),
        )
        if qq_msg:
            await self.send_qq(qq_msg)

        # server_start/stop still go to QQ above; cross-server connection status
        # uses hub WS server_connected/disconnected (reliable on force-kill).
        if str(event or "") in ("server_start", "server_stop"):
            return

        await self._broadcast_to_others(
            server_name,
            {
                "type": "cross_server_event",
                "from_server": origin,
                "event": event,
                "player": player,
                "message": message,
            },
        )

    async def _maybe_block_forbidden_chat(
        self,
        *,
        origin_server: str,
        display_player: str,
        raw_player_name: str,
        message: str,
    ) -> bool:
        """Run Arc Guard on MC chat; punish and block forward when hit.

        Args:
            origin_server: Connected Hub server name that sent the event.
            display_player: Display label (may include title).
            raw_player_name: Bare player name when provided by MC.
            message: Chat text.

        Returns:
            True if the chat must not be forwarded to QQ / other servers.
        """
        if not self.get_arc_guard_api:
            return False
        try:
            api = self.get_arc_guard_api()
        except Exception as e:
            logger.warning(f"[弧光EndStone消息中枢] 获取弧光护卫 API 失败: {e}")
            return False
        if api is None:
            return False

        player_name = raw_player_name
        bound_qq = ""
        if player_name:
            bound_qq = self.binding_store.get_player_qq(player_name)
        if not player_name or not bound_qq:
            resolved_name, resolved_qq = self.binding_store.resolve_player_binding(
                display_player
            )
            if resolved_name:
                player_name = player_name or resolved_name
            if resolved_qq:
                bound_qq = resolved_qq
        if not player_name:
            player_name = display_player.strip()

        try:
            result = api.check(message, user_id=bound_qq or None)
        except Exception as e:
            logger.warning(f"[弧光EndStone消息中枢] 弧光护卫检测失败: {e}")
            return False

        if not result.get("should_punish"):
            return False

        hits = int(result.get("hits") or 1)
        mute_seconds = int(result.get("mute_seconds") or 60)
        reply = str(result.get("reply") or "").strip()
        minutes = max(1, (mute_seconds + 59) // 60)

        logger.info(
            "[弧光EndStone消息中枢] 弧光护卫命中 chat server=%s player=%s qq=%s hits=%s",
            origin_server,
            player_name,
            bound_qq or "-",
            hits,
        )

        # Mute bound QQ body in configured groups.
        if bound_qq and self.mute_qq:
            try:
                ok = await self.mute_qq(bound_qq, mute_seconds)
                logger.info(
                    "[弧光EndStone消息中枢] 弧光护卫禁言 qq=%s %ss ok=%s",
                    bound_qq,
                    mute_seconds,
                    ok,
                )
            except Exception as e:
                logger.error(
                    f"[弧光EndStone消息中枢] 弧光护卫禁言失败 qq={bound_qq}: {e}"
                )

        # In-game kill + warning on the originating server (silent, no QQ cmd echo).
        # Bedrock `/say` rejects `[` (selector/JSON), so use compact tellraw.
        warn_game = reply or f"竟敢辱骂至高无上的ENMO，枪毙{minutes}分钟！"
        kill_name = player_name.replace('"', "").strip()
        if " " in kill_name:
            kill_arg = f'"{kill_name}"'
        else:
            kill_arg = kill_name
        warn_text = f"[弧光护卫] {kill_name}: {warn_game}"
        tellraw_json = json.dumps(
            {"rawtext": [{"text": warn_text}]},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        await self._dispatch_silent_console_commands(
            origin_server,
            [
                f"kill {kill_arg}",
                f"tellraw @a {tellraw_json}",
            ],
        )

        # QQ notice (not the original insult).
        notice = f"[{origin_server}]\n[弧光护卫] {player_name}: {warn_game}"
        try:
            await self.send_qq(notice)
        except Exception as e:
            logger.warning(f"[弧光EndStone消息中枢] 弧光护卫群警告发送失败: {e}")

        return True

    async def _dispatch_silent_console_commands(
        self, server_name: str, commands: list[str]
    ) -> None:
        """Run console commands on one MC server without echoing results to QQ.

        Args:
            server_name: Connected server name.
            commands: Console command lines without leading slash.
        """
        target_sid = self._server_numeric_id_by_name.get(server_name)
        for cmd in commands:
            line = (cmd or "").strip()
            if not line:
                continue
            await self.broadcast_to_all(
                {
                    "type": "command_forward",
                    "raw_message": f"/cmd {line}",
                    "command_line": f"/cmd {line}",
                    "target_server_id": target_sid,
                    "user_id": "0",
                    "display_name": "弧光护卫",
                    "group_id": "0",
                    "sender_role": "admin",
                    "is_config_admin": True,
                    "silent": True,
                }
            )

    def _format_qq_message(
        self,
        server_name: str,
        event: str,
        player: str,
        message: str,
        session_count=None,
        playtime_str: str = "",
    ) -> str:
        prefix = f"[{server_name}]\n"
        if event == "chat":
            return f"{prefix}💬 {player}: {message}"
        if event == "join":
            if session_count is None:
                return f"{prefix}🟢 {player} 加入游戏"
            if int(session_count) <= 1:
                return f"{prefix}🌟 {player} 首次进入服务器！"
            return f"{prefix}🟢 {player} 加入游戏 (第{session_count}次游戏)"
        if event == "quit":
            if playtime_str:
                return f"{prefix}🔴 {player} 离开游戏 (总游戏时长: {playtime_str})"
            return f"{prefix}🔴 {player} 离开游戏"
        if event == "death":
            # Prefer full death line from MC (often already includes 💀 / 玩家…).
            body = (message or "").strip()
            if not body:
                body = f"{player} 死了" if player else "有玩家死了"
            if body.startswith("💀"):
                return f"{prefix}{body}"
            return f"{prefix}💀 {body}"
        if event == "custom":
            return f"{prefix}{message}" if message else ""
        # server_start/stop: QQ announce uses hub WS connected/disconnected instead
        # (force-killed MC may never emit server_stop).
        if event in ("server_start", "server_stop"):
            return ""
        return f"{prefix}{message}" if message else ""

    def format_group_help_text(self) -> str:
        """QQ-facing help: commands use the /mc prefix required by this hub."""
        return "\n".join(
            [
                f"[{self.hub_server_name}] 群指令（均以 /mc 开头）：",
                "/mc help — 显示本帮助",
                "/mc servers — 查看已连接子服编号",
                "/mc list [编号] — 在线玩家",
                "/mc tps [编号] — TPS / MSPT",
                "/mc info [编号] — 服务器信息",
                "/mc 绑定 <玩家名> — 绑定 QQ 到游戏角色",
                "/mc 重启 — 重启投票（须在线）",
                "/mc cmd [编号] <控制台命令> — 群管理可用",
                "/mc who <玩家名|QQ> [编号] — 插件管理员",
                "/mc ban|unban|banlist|unbindqq|reload — 插件管理员",
                "/mc activate — 插件管理员在本会话激活 Minecraft AI 工具",
                "",
                "说明：中枢会剥掉 /mc 后再发给各 MC 子服；省略编号则所有子服执行。",
                "AI 工具需先 /mc activate；会话 ID 可为非数字字符串。",
            ]
        )

    async def _broadcast_to_others(self, from_server: str, data: dict) -> None:
        msg = json.dumps(data, ensure_ascii=False)
        for name, ws in list(self.connected_servers.items()):
            if name == from_server:
                continue
            try:
                await ws.send(msg)
            except Exception:
                pass

    async def broadcast_to_all(self, data: dict) -> None:
        """Broadcast a Hub payload to every connected MC server.

        Args:
            data: JSON-serializable Hub message.
        """
        msg = json.dumps(data, ensure_ascii=False)
        for ws in list(self.connected_servers.values()):
            try:
                await ws.send(msg)
            except Exception:
                pass

    async def push_qq_chat(
        self, display_name: str, message: str, group_id: str | int
    ) -> None:
        """Push a QQ group chat message to all MC servers.

        Args:
            display_name: Sender display name in game.
            message: Cleaned chat text.
            group_id: Source QQ group id.
        """
        group_name = self.group_names.get(str(group_id), "")
        await self.broadcast_to_all(
            {
                "type": "qq_message",
                "display_name": display_name,
                "message": message,
                "group_name": group_name,
            }
        )

    async def push_command_forward(
        self,
        *,
        raw_message: str,
        user_id: str | int,
        display_name: str,
        group_id: str | int,
        sender_role: str = "member",
    ) -> None:
        """Forward a QQ group command to connected MC servers.

        Args:
            raw_message: Original command text.
            user_id: QQ user id.
            display_name: Bound player name or nickname.
            group_id: Source group id.
            sender_role: OneBot sender role.
        """
        # Normalize /mc ... -> /... before routing / forwarding to MC.
        internal = strip_mc_command_prefix(raw_message)
        forward_message = internal if internal is not None else raw_message.strip()

        if forward_message == "/servers":
            catalog = self.get_server_catalog()
            if not self.connected_servers:
                await self.send_qq(
                    f"[{self.hub_server_name}]\n当前没有已连接的 Minecraft 子服。"
                )
                return
            lines = [f"[{self.hub_server_name}]\n当前子服列表:"]
            for item in catalog:
                online = "✅" if item["name"] in self.connected_servers else "·"
                lines.append(f"[{item['id']}] {item['name']} {online}")
            await self.send_qq("\n".join(lines))
            return

        # Hub answers /mc help so QQ users see /mc-prefixed usage; MC keeps /help.
        if forward_message == "/help":
            await self.send_qq(self.format_group_help_text())
            return

        eff_line, route_sid = parse_hub_command_routing(forward_message)
        if route_sid is not None:
            mc_ids = {
                c["id"]
                for c in self.get_server_catalog()
                if c["name"] in self.connected_servers
            }
            if mc_ids and route_sid not in mc_ids:
                await self.send_qq(
                    f"[{self.hub_server_name}]\n"
                    f"❌ 无编号 {route_sid} 的服务器。\n"
                    f"💡 发送 /mc servers 查看当前子服列表"
                )
                return

        if not self.connected_servers:
            await self.send_qq(
                f"[{self.hub_server_name}]\n❌ 当前没有已连接的 Minecraft 子服。"
            )
            return

        await self.broadcast_to_all(
            {
                "type": "command_forward",
                "raw_message": forward_message,
                "command_line": eff_line,
                "target_server_id": route_sid,
                "user_id": user_id,
                "display_name": display_name,
                "group_id": group_id,
                "sender_role": sender_role,
                "is_config_admin": str(user_id) in self.hub_admins,
            }
        )
