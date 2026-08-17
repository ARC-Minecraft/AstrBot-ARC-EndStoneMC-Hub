"""AstrBot 弧光 EndStone 消息中枢插件。"""

from __future__ import annotations

import asyncio
import re

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType

from .binding_store import BindingStore
from .hub_server import ArcHubServer, is_known_arc_command
from .mc_ai_event import McAiMessageEvent

PLUGIN_NAME = "astrbot_plugin_endstone_arc"
ARC_GUARD_PLUGIN = "astrbot_plugin_arc_guard"
_HUB_DISPLAY = "弧光EndStone消息中枢"
_cq_pattern = re.compile(r"\[CQ:(\w+)([^\]]*)\]")
_MC_AI_IDENTITY_HINT = (
    "当前是 Minecraft 游戏内对话，没有 QQ 群号。"
    "发送者 ID：该玩家若已绑定 QQ 则为 QQ 号（可与 QQ 侧记忆对上同一个人），"
    "未绑定则使用 XUID。昵称只是当前游戏名。"
    "执行游戏指令时，在玩家发来这条消息的那台 Minecraft 服务器上执行。"
    "玩家问在线人数、谁在线、TPS、服务器信息或要你执行游戏指令时，"
    "必须调用对应工具查询或执行，禁止凭空编造数字或名单。"
    "优先用工具执行指令；只有工具不可用时，才在可见回复里使用 "
    "[execution_command:实际游戏指令] 标记。"
)


class EndstoneArcMessageCenter(Star):
    """弧光 EndStone 消息中枢：WebSocket Hub + QQ 桥接。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self._hub: ArcHubServer | None = None
        self._binding_store: BindingStore | None = None
        self._group_umo: dict[str, str] = {}
        self._platform_id: str = str(self.config.get("platform_id") or "")
        self._start_task: asyncio.Task | None = None

    async def initialize(self):
        """Start Hub after plugin load."""
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self._binding_store = BindingStore(data_dir)
        self._hydrate_umos_from_config()
        self._start_task = asyncio.create_task(self._start_hub())

    async def terminate(self):
        """Stop Hub on unload/reload."""
        if self._start_task and not self._start_task.done():
            self._start_task.cancel()
            try:
                await self._start_task
            except asyncio.CancelledError:
                pass
        if self._hub:
            await self._hub.stop()
            self._hub = None

    def _hydrate_umos_from_config(self) -> None:
        platform_id = self._platform_id.strip()
        if not platform_id:
            return
        for gid in self._target_group_ids():
            umo = str(MessageSession(platform_id, MessageType.GROUP_MESSAGE, str(gid)))
            self._group_umo.setdefault(str(gid), umo)

    def _target_group_ids(self) -> set[str]:
        raw = self.config.get("target_groups") or []
        ids: set[str] = set()
        for item in raw:
            text = str(item).strip()
            if text:
                ids.add(text)
        return ids

    def _group_names(self) -> dict[str, str]:
        raw = self.config.get("group_names") or {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): str(v) for k, v in raw.items() if str(v).strip()}

    async def _start_hub(self) -> None:
        assert self._binding_store is not None
        admins_raw = self.config.get("admins") or []
        admins = [str(a) for a in admins_raw if str(a).strip()]
        self._hub = ArcHubServer(
            host=str(self.config.get("ws_host") or "0.0.0.0"),
            port=int(self.config.get("ws_port") or 19136),
            token=str(self.config.get("auth_token") or ""),
            hub_server_name=str(self.config.get("hub_server_name") or _HUB_DISPLAY),
            binding_store=self._binding_store,
            send_qq=self._send_to_all_target_groups,
            set_group_card=self._set_group_card_all_targets,
            mute_qq=self._mute_qq_all_targets,
            get_arc_guard_api=self._get_arc_guard_api,
            group_names=self._group_names(),
            hub_admins=admins,
            sync_group_card=bool(self.config.get("sync_group_card", True)),
        )
        self._hub.process_ai_chat = self._process_ai_chat
        try:
            self._hub.ai_chat_timeout = float(self.config.get("ai_chat_timeout") or 180)
        except (TypeError, ValueError):
            self._hub.ai_chat_timeout = 180.0
        await self._hub.start()
        if bool(self.config.get("startup_announce", True)):
            # Delay slightly so platform adapters are ready.
            await asyncio.sleep(2)
            try:
                await self._send_to_all_target_groups(
                    f"[{self.config.get('hub_server_name') or _HUB_DISPLAY}]\n"
                    f"[{_HUB_DISPLAY}] 已启动"
                )
            except Exception as e:
                logger.warning(f"[{_HUB_DISPLAY}] 启动播报失败: {e}")

    def _get_arc_guard_api(self):
        """Resolve Arc Guard cross-plugin API when the plugin is active.

        Returns:
            Arc Guard ``get_api()`` result, or None if unavailable.
        """
        try:
            meta = self.context.get_registered_star(ARC_GUARD_PLUGIN)
        except Exception as e:
            logger.debug(f"[{_HUB_DISPLAY}] 查找弧光护卫失败: {e}")
            return None
        if not meta or not getattr(meta, "activated", False):
            return None
        star = getattr(meta, "star_cls", None)
        if star is None:
            return None
        get_api = getattr(star, "get_api", None)
        if not callable(get_api):
            return None
        try:
            return get_api()
        except Exception as e:
            logger.warning(f"[{_HUB_DISPLAY}] 调用弧光护卫 get_api 失败: {e}")
            return None

    async def _mute_qq_all_targets(self, user_id: str, seconds: int) -> bool:
        """Mute a QQ user in every configured target group (Arc Guard accumulate).

        Prefers ``astrbot_plugin_arc_guard`` ``mute_user_in_group`` so duration
        is remaining + added. Uses ``respect_whitelist=True`` (normal Arc Guard
        punishment). Falls back to raw ``set_group_ban`` if Arc Guard is
        unavailable.

        Args:
            user_id: QQ user id.
            seconds: Extra mute seconds to accumulate (or set on fallback).

        Returns:
            True if at least one group ban succeeded.
        """
        platform_id = (self._platform_id or "").strip()
        if not platform_id:
            logger.warning(f"[{_HUB_DISPLAY}] 禁言失败：未学习到 platform_id")
            return False
        platform = self.context.get_platform_inst(platform_id)
        if platform is None:
            logger.warning(f"[{_HUB_DISPLAY}] 禁言失败：找不到平台 {platform_id}")
            return False
        bot = platform.get_client()
        if bot is None:
            logger.warning(f"[{_HUB_DISPLAY}] 禁言失败：平台无 client")
            return False

        uid = str(user_id or "").strip()
        duration = max(1, int(seconds))
        api = self._get_arc_guard_api()
        mute_in_group = getattr(api, "mute_user_in_group", None) if api else None
        ok_any = False

        for gid in self._target_group_ids():
            if callable(mute_in_group):
                try:
                    result = await mute_in_group(
                        bot,
                        str(gid),
                        uid,
                        duration,
                        respect_whitelist=True,
                    )
                    if result.get("ok"):
                        ok_any = True
                        logger.info(
                            f"[{_HUB_DISPLAY}] 弧光护卫累加禁言 group={gid} user={uid} "
                            f"remaining={result.get('remaining_before')}s "
                            f"+ add={result.get('added')}s "
                            f"-> total={result.get('total')}s"
                        )
                    else:
                        logger.warning(
                            f"[{_HUB_DISPLAY}] 弧光护卫禁言未成功 group={gid} "
                            f"user={uid}: {result.get('error') or 'unknown'}"
                        )
                except Exception as e:
                    logger.warning(
                        f"[{_HUB_DISPLAY}] 弧光护卫禁言异常 group={gid} user={uid}: {e}"
                    )
                continue

            try:
                await bot.call_action(
                    "set_group_ban",
                    group_id=int(gid),
                    user_id=int(uid),
                    duration=duration,
                )
                ok_any = True
            except Exception as e:
                logger.warning(
                    f"[{_HUB_DISPLAY}] 禁言失败 group={gid} user={user_id}: {e}"
                )
        return ok_any

    async def _set_group_card_all_targets(self, user_id: int, card: str) -> None:
        """Set QQ group card via aiocqhttp for every configured target group."""
        platform_id = (self._platform_id or "").strip()
        if not platform_id:
            logger.warning(f"[{_HUB_DISPLAY}] 改群名片失败：未学习到 platform_id")
            return
        platform = self.context.get_platform_inst(platform_id)
        if platform is None:
            logger.warning(f"[{_HUB_DISPLAY}] 改群名片失败：找不到平台 {platform_id}")
            return
        bot = platform.get_client()
        if bot is None:
            logger.warning(f"[{_HUB_DISPLAY}] 改群名片失败：平台无 client")
            return
        for gid in self._target_group_ids():
            try:
                await bot.call_action(
                    "set_group_card",
                    group_id=int(gid),
                    user_id=int(user_id),
                    card=str(card),
                )
            except Exception as e:
                logger.warning(
                    f"[{_HUB_DISPLAY}] 改群名片失败 group={gid} user={user_id}: {e}"
                )

    async def _send_to_all_target_groups(self, text: str) -> None:
        groups = self._target_group_ids()
        if not groups:
            logger.warning("[弧光EndStone消息中枢] 未配置 target_groups，无法发往 QQ")
            return
        for gid in groups:
            umo = self._resolve_umo(gid)
            if not umo:
                logger.warning(
                    f"[弧光EndStone消息中枢] 群 {gid} 尚无 unified_msg_origin，"
                    "请先在该群发一条消息或配置 platform_id"
                )
                continue
            try:
                ok = await self.context.send_message(
                    umo, MessageEventResult().message(text)
                )
                if not ok:
                    logger.warning(
                        f"[弧光EndStone消息中枢] 发送到群 {gid} 失败（未找到平台）: {umo}"
                    )
            except Exception as e:
                logger.error(f"[弧光EndStone消息中枢] 发送到群 {gid} 异常: {e}")

    def _resolve_umo(self, group_id: str | int) -> str | None:
        gid = str(group_id)
        if gid in self._group_umo:
            return self._group_umo[gid]
        platform_id = self._platform_id.strip()
        if not platform_id:
            return None
        umo = str(MessageSession(platform_id, MessageType.GROUP_MESSAGE, gid))
        self._group_umo[gid] = umo
        return umo

    def _remember_umo(self, event: AstrMessageEvent) -> None:
        gid = event.get_group_id()
        if not gid:
            return
        self._group_umo[str(gid)] = event.unified_msg_origin
        if not self._platform_id:
            self._platform_id = event.get_platform_id()

    def _resolve_display_name(self, event: AstrMessageEvent) -> str:
        assert self._binding_store is not None
        user_id = str(event.get_sender_id() or "")
        bound = self._binding_store.get_qq_player(user_id) if user_id else ""
        if bound:
            return bound
        name = (event.get_sender_name() or "").strip()
        return name or user_id or "未知"

    def _extract_forward_text(self, event: AstrMessageEvent) -> str:
        text = (event.message_str or "").strip()
        if text:
            return self._truncate(self._replace_cq_codes(text), 150)

        parts: list[str] = []
        try:
            for comp in event.get_messages():
                type_name = type(comp).__name__.lower()
                if hasattr(comp, "text") and getattr(comp, "text"):
                    parts.append(str(getattr(comp, "text")))
                elif "image" in type_name:
                    parts.append("[图片]")
                elif "record" in type_name or "voice" in type_name:
                    parts.append("[语音]")
                elif "video" in type_name:
                    parts.append("[视频]")
                elif "at" in type_name:
                    qq = getattr(comp, "qq", None) or getattr(comp, "target", "")
                    parts.append(f"@{qq}" if qq else "[@]")
                elif "face" in type_name:
                    parts.append("[表情]")
                elif "reply" in type_name:
                    parts.append("[回复]")
        except Exception:
            pass
        joined = "".join(parts).strip()
        return self._truncate(joined, 150) if joined else ""

    @staticmethod
    def _replace_cq_codes(text: str) -> str:
        def _repl(match: re.Match[str]) -> str:
            cq_type = match.group(1)
            params = match.group(2) or ""
            if cq_type == "image":
                return "[图片]"
            if cq_type == "video":
                return "[视频]"
            if cq_type == "record":
                return "[语音]"
            if cq_type == "face":
                return "[表情]"
            if cq_type == "at":
                if "qq=all" in params:
                    return "@全体成员"
                m = re.search(r"qq=(\d+)", params)
                return f"@{m.group(1)}" if m else "[@]"
            if cq_type == "reply":
                return "[回复]"
            return f"[{cq_type}]"

        return _cq_pattern.sub(_repl, text)

    @staticmethod
    def _truncate(text: str, max_length: int) -> str:
        if len(text) <= max_length:
            return text
        return text[: max_length - 1] + "…"

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """Forward target-group QQ messages/commands to connected MC servers."""
        if event.get_extra("mc_ai_event"):
            return
        if not self._hub:
            return

        gid = event.get_group_id()
        if not gid:
            return

        targets = self._target_group_ids()
        if targets and str(gid) not in targets:
            return

        self._remember_umo(event)

        raw = (event.message_str or "").strip()
        # Prefer raw OneBot text when available for command detection with CQ noise.
        try:
            raw_message = ""
            if event.message_obj and event.message_obj.raw_message:
                rm = event.message_obj.raw_message
                if isinstance(rm, dict):
                    raw_message = str(rm.get("raw_message") or "")
                else:
                    raw_message = str(getattr(rm, "raw_message", "") or "")
            if raw_message.strip():
                raw = raw_message.strip()
        except Exception:
            pass

        display_name = self._resolve_display_name(event)
        sender_role = "member"
        try:
            raw_obj = event.message_obj.raw_message if event.message_obj else None
            if isinstance(raw_obj, dict):
                sender_role = str((raw_obj.get("sender") or {}).get("role") or "member")
        except Exception:
            pass

        if raw.startswith("/"):
            # Only /mc ... ARC commands; leave AstrBot built-ins (e.g. /help) alone.
            if not is_known_arc_command(raw):
                return
            await self._hub.push_command_forward(
                raw_message=raw,
                user_id=event.get_sender_id(),
                display_name=display_name,
                group_id=gid,
                sender_role=sender_role,
            )
            event.stop_event()
            return

        if not bool(self.config.get("forward_qq_chat", True)):
            return

        text = self._extract_forward_text(event)
        if not text or text == "[空消息]":
            return

        await self._hub.push_qq_chat(display_name, text, gid)

    @filter.command("弧光状态", alias={"arcstatus", "arc_hub_status"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_hub_status(self, event: AstrMessageEvent):
        """Show connected MC servers."""
        if not self._hub:
            yield event.plain_result(f"{_HUB_DISPLAY}尚未启动")
            return
        names = self._hub.connected_server_names()
        catalog = self._hub.get_server_catalog()
        lines = [
            f"{_HUB_DISPLAY}状态",
            f"监听: {self.config.get('ws_host')}:{self.config.get('ws_port')}",
            f"已连接子服: {len(names)}",
        ]
        for item in catalog:
            mark = "✅" if item["name"] in names else "·"
            lines.append(f"{mark} [{item['id']}] {item['name']}")
        if not catalog:
            lines.append("（暂无注册记录）")
        yield event.plain_result("\n".join(lines))

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """Append MC extra system instructions without replacing AstrBot persona."""
        if not event.get_extra("mc_ai_event"):
            return
        parts: list[str] = [_MC_AI_IDENTITY_HINT]
        extra = event.get_extra("mc_ai_extra_system")
        if extra:
            text = str(extra).strip()
            if text:
                parts.append(text)
        prefix = (req.system_prompt or "").rstrip()
        block = "# Minecraft Server Extra Instructions\n\n" + "\n\n".join(parts)
        req.system_prompt = f"{prefix}\n\n{block}\n" if prefix else f"{block}\n"

    async def _call_mc_ai_tool(
        self, event: AstrMessageEvent, action: str, args: dict | None = None
    ) -> str:
        """Run a Minecraft AI Helper tool for the current MC conversation.

        Args:
            event: Current AstrBot event.
            action: Helper action name.
            args: Extra arguments for the action.

        Returns:
            Plain text for the LLM.
        """
        if not event.get_extra("mc_ai_event"):
            return "该工具只在 Minecraft 游戏内对话中可用，当前不是 MC 会话。"
        if not self._hub:
            return "弧光消息中枢尚未启动。"
        server_name = str(event.get_extra("mc_ai_server") or "").strip()
        if not server_name:
            return "无法确定 Minecraft 服务器名称。"
        payload = dict(args or {})
        payload.setdefault("is_op", bool(event.get_extra("mc_ai_is_op")))
        payload.setdefault("player_name", str(event.get_extra("mc_ai_player_name") or ""))
        payload.setdefault("player_xuid", str(event.get_extra("mc_ai_xuid") or ""))
        try:
            resp = await self._hub.call_ai_tool(server_name, action, payload, timeout=20)
        except Exception as e:
            logger.warning(f"[{_HUB_DISPLAY}] MC 工具 {action} 失败: {e}")
            return f"调用 Minecraft 工具失败: {e}"
        if not isinstance(resp, dict):
            return "Minecraft 工具返回格式异常"
        if not resp.get("ok"):
            return str(resp.get("error") or "Minecraft 工具执行失败")
        return str(resp.get("text") or "").strip() or "（无返回）"

    @filter.llm_tool(name="mc_list_players")
    async def mc_list_players(self, event: AstrMessageEvent, reason: str) -> str:
        """查询当前 Minecraft 服务器在线玩家名单与人数。玩家问起谁在线、有没有某某、在线人数时必须调用，禁止编造。

        Args:
            reason(string): 简要说明为何查询，例如「玩家问谁在线」
        """
        _ = reason
        return await self._call_mc_ai_tool(event, "list")

    @filter.llm_tool(name="mc_get_tps")
    async def mc_get_tps(self, event: AstrMessageEvent, reason: str) -> str:
        """查询当前 Minecraft 服务器 TPS / MSPT 等性能数据。玩家问起卡不卡、TPS、延迟时必须调用，禁止编造。

        Args:
            reason(string): 简要说明为何查询，例如「玩家问 TPS」
        """
        _ = reason
        return await self._call_mc_ai_tool(event, "tps")

    @filter.llm_tool(name="mc_server_info")
    async def mc_server_info(self, event: AstrMessageEvent, reason: str) -> str:
        """查询当前 Minecraft 服务器基本信息（名称、版本、在线人数、运行时长等）。不要编造。

        Args:
            reason(string): 简要说明为何查询，例如「玩家问服务器信息」
        """
        _ = reason
        return await self._call_mc_ai_tool(event, "info")

    @filter.llm_tool(name="mc_run_command")
    async def mc_run_command(self, event: AstrMessageEvent, command: str) -> str:
        """在当前 Minecraft 服务器控制台执行一条游戏指令。禁止 stop、kill；gamemode 仅 OP 玩家明确要求时可用。需要真实改游戏世界或给效果时调用。

        Args:
            command(string): 不含斜杠的游戏指令，例如 effect Steve night_vision 30 0 true
        """
        command_line = str(command or "").strip()
        if not command_line:
            return "指令为空"
        return await self._call_mc_ai_tool(event, "cmd", {"command": command_line})

    async def _process_ai_chat(self, data: dict) -> dict:
        """Run one Minecraft player message through AstrBot's conversation pipeline.

        Args:
            data: Hub ``ai_chat`` payload.

        Returns:
            ``{"ok": True, "reply": "..."}`` or ``{"ok": False, "error": "..."}``.
        """
        player_name = str(data.get("player_name") or "player").strip() or "player"
        player_xuid = str(data.get("player_xuid") or data.get("xuid") or "").strip()
        server_name = str(data.get("server_name") or "mc").strip() or "mc"
        content = str(data.get("content") or "").strip()
        extra_system = str(data.get("extra_system_prompt") or "").strip()
        channel = str(data.get("channel") or "public").strip() or "public"
        is_op = bool(data.get("is_op", False))
        if not content:
            return {"ok": False, "error": "空消息"}

        bound_qq = ""
        if self._binding_store is not None:
            bound_qq = self._binding_store.resolve_bound_qq(player_name, player_xuid)
        sender_id = bound_qq or player_xuid or f"name_{player_name}"

        status = "OP玩家" if is_op else "普通玩家"
        channel_label = "GUI私聊" if channel == "gui" else "公开聊天"
        user_text = f"{player_name}({status})[{channel_label}]: {content}"

        event = McAiMessageEvent(
            player_name=player_name,
            player_xuid=player_xuid,
            sender_id=sender_id,
            server_name=server_name,
            message=user_text,
            extra_system_prompt=extra_system,
            is_op=is_op,
            channel=channel,
            bound_qq=bound_qq,
        )
        await self.context.get_event_queue().put(event)
        try:
            timeout = float(getattr(self._hub, "ai_chat_timeout", 180) or 180)
            reply = await asyncio.wait_for(event.wait_reply(), timeout=timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "error": "AstrBot 对话超时"}
        except Exception as e:
            logger.error(f"[{_HUB_DISPLAY}] MC AI 对话失败: {e}")
            return {"ok": False, "error": str(e)}

        reply_text = str(reply or "").strip()
        if not reply_text:
            return {"ok": False, "error": "AstrBot 未返回文本"}
        return {"ok": True, "reply": reply_text}
