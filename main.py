"""AstrBot 弧光 EndStone 消息中枢插件。"""

from __future__ import annotations

import asyncio
import re

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType

from .binding_store import BindingStore
from .hub_server import ArcHubServer, is_known_arc_command

PLUGIN_NAME = "astrbot_plugin_endstone_arc"
ENMO_GUARD_PLUGIN = "astrbot_plugin_enmo_guard"
_HUB_DISPLAY = "弧光EndStone消息中枢"
_cq_pattern = re.compile(r"\[CQ:(\w+)([^\]]*)\]")


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
            get_enmo_api=self._get_enmo_api,
            group_names=self._group_names(),
            hub_admins=admins,
            sync_group_card=bool(self.config.get("sync_group_card", True)),
        )
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

    def _get_enmo_api(self):
        """Resolve ENMO Guard cross-plugin API when the plugin is active.

        Returns:
            ENMO ``get_api()`` result, or None if unavailable.
        """
        try:
            meta = self.context.get_registered_star(ENMO_GUARD_PLUGIN)
        except Exception as e:
            logger.debug(f"[{_HUB_DISPLAY}] 查找 ENMO 护卫失败: {e}")
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
            logger.warning(f"[{_HUB_DISPLAY}] 调用 ENMO get_api 失败: {e}")
            return None

    async def _mute_qq_all_targets(self, user_id: str, seconds: int) -> bool:
        """Mute a QQ user in every configured target group.

        Args:
            user_id: QQ user id.
            seconds: Mute duration in seconds.

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
        ok_any = False
        uid = int(user_id)
        duration = max(1, int(seconds))
        for gid in self._target_group_ids():
            try:
                await bot.call_action(
                    "set_group_ban",
                    group_id=int(gid),
                    user_id=uid,
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
