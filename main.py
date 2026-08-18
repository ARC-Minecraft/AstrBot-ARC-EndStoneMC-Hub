"""AstrBot 弧光 EndStone 消息中枢插件。"""

from __future__ import annotations

import asyncio
import re

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.event.filter import CustomFilter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType

from .activation_store import ActivationStore
from .binding_store import BindingStore
from .hub_server import (
    ArcHubServer,
    extract_event_raw_text,
    is_known_arc_command,
    is_mc_activate_command,
    normalize_mc_arc_raw_message,
)
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
    "要把玩家关进监狱、释放或查看在押名单时，必须调用 mc_jail_player / "
    "mc_release_player / mc_list_prisoners，不要用 mc_run_command 去跑 /jail。"
    "查询玩家位置、近期行为、打了谁、被谁打、坐标附近发生过什么时，必须调用 "
    "mc_skyeye_player / mc_skyeye_combat / mc_skyeye_location，禁止编造。"
    "优先调用 mc_run_command 执行其它指令；只有工具不可用时，才在可见回复里使用 "
    "[execution_command:实际游戏指令] 标记。"
    "effect 只能用于药水效果，例如 effect Steve slowness 20 0 true。"
    "劈闪电必须用 execute at 玩家名 run summon lightning_bolt ~ ~ ~ ，"
    "禁止 effect 玩家名 summon（summon 不是药水效果）。"
)
_QQ_MC_TOOL_HINT = (
    "当前对话已通过 /mc activate 接入弧光 Minecraft 中枢。"
    "查询在线、TPS、服务器信息或执行游戏指令时必须调用对应工具，禁止编造。"
    "关押玩家用 mc_jail_player，释放用 mc_release_player，查看在押用 mc_list_prisoners。"
    "查玩家位置/近期行为用 mc_skyeye_player，查打架用 mc_skyeye_combat，查坐标附近用 mc_skyeye_location。"
    "有多台 Minecraft 服务器时，先调用 mc_list_servers，再在其它工具里填写 server"
    "（名称、编号或别名），不要猜测。"
    "在能识别出 QQ 群主/群管身份的群聊里，mc_run_command / 入狱 / 天眼仅管理员可真正执行。"
    "effect 只能用于药水效果。劈闪电必须用 execute at 玩家名 run summon lightning_bolt ~ ~ ~。"
)


class McArcCommandFilter(CustomFilter):
    """Intercept ``/mc ...`` before LLM wake, regardless of message type."""

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        if event.get_extra("mc_ai_event"):
            return False
        raw = extract_event_raw_text(event)
        return normalize_mc_arc_raw_message(raw) is not None


class EndstoneArcMessageCenter(Star):
    """弧光 EndStone 消息中枢：WebSocket Hub + QQ 桥接。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self._hub: ArcHubServer | None = None
        self._binding_store: BindingStore | None = None
        self._activation_store: ActivationStore | None = None
        self._group_umo: dict[str, str] = {}
        self._platform_id: str = str(self.config.get("platform_id") or "")
        self._start_task: asyncio.Task | None = None

    async def initialize(self):
        """Start Hub after plugin load."""
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self._binding_store = BindingStore(data_dir)
        self._activation_store = ActivationStore(data_dir)
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

    def _admin_ids(self) -> set[str]:
        raw = self.config.get("admins") or []
        return {str(a).strip() for a in raw if str(a).strip()}

    def _is_hub_admin(self, event: AstrMessageEvent) -> bool:
        uid = str(event.get_sender_id() or "").strip()
        return bool(uid and uid in self._admin_ids())

    async def _reply_to_event(self, event: AstrMessageEvent, text: str) -> None:
        try:
            await event.send(MessageEventResult().message(text))
            return
        except Exception as error:
            logger.debug(f"[{_HUB_DISPLAY}] event.send 失败，改用 context.send_message: {error}")
        umo = str(event.unified_msg_origin or "").strip()
        if not umo:
            logger.warning(f"[{_HUB_DISPLAY}] 无法回复：缺少 unified_msg_origin")
            return
        try:
            await self.context.send_message(umo, MessageEventResult().message(text))
        except Exception as error:
            logger.warning(f"[{_HUB_DISPLAY}] 回复消息失败: {error}")

    def _is_tool_session_activated(self, event: AstrMessageEvent) -> bool:
        store = self._activation_store
        if store is None:
            return False
        umo = str(event.unified_msg_origin or "").strip()
        if umo and store.is_activated(umo):
            return True
        if umo and ":" in umo:
            umo_sid = umo.rsplit(":", 1)[-1].strip()
            if umo_sid and store.is_activated_by_session_id(umo_sid):
                return True
        for sid in (
            str(event.get_session_id() or "").strip(),
            str(event.get_group_id() or "").strip(),
        ):
            if sid and store.is_activated_by_session_id(sid):
                return True
        return False

    async def _handle_mc_activate(self, event: AstrMessageEvent) -> None:
        if not self._is_hub_admin(event):
            await self._reply_to_event(
                event,
                f"[{self.config.get('hub_server_name') or _HUB_DISPLAY}]\n"
                "❌ 仅插件管理员可使用 /mc activate。\n"
                "请在插件配置 admins 中填写你的账号 ID。",
            )
            return

        session_key = str(event.unified_msg_origin or "").strip()
        session_id = str(event.get_session_id() or event.get_group_id() or "").strip()
        if not session_key:
            await self._reply_to_event(
                event,
                f"[{self.config.get('hub_server_name') or _HUB_DISPLAY}]\n"
                "❌ 无法识别当前会话，请稍后再试。",
            )
            return

        self._remember_umo(event)
        label = session_id or session_key
        store = self._activation_store
        if store is None:
            await self._reply_to_event(event, f"[{_HUB_DISPLAY}] 激活存储未就绪。")
            return
        added = store.activate(
            session_key,
            session_id=session_id,
            label=label,
            by_admin=str(event.get_sender_id() or ""),
        )
        logger.info(
            f"[{_HUB_DISPLAY}] /mc activate session_key={session_key} "
            f"session_id={session_id} added={added}"
        )
        hub_name = self.config.get("hub_server_name") or _HUB_DISPLAY
        if added:
            text = (
                f"[{hub_name}]\n"
                "✅ 已在本会话激活 Minecraft AI 工具。\n"
                f"会话 ID: {label}\n"
                "此后本对话中的 AI 可调用 mc_list_players、mc_run_command 等工具。"
            )
        else:
            text = (
                f"[{hub_name}]\n"
                "ℹ️ 本会话已处于激活状态。\n"
                f"会话 ID: {label}"
            )
        await self._reply_to_event(event, text)

    def _server_aliases(self) -> dict[str, str]:
        raw = self.config.get("server_aliases") or {}
        if not isinstance(raw, dict):
            return {}
        aliases: dict[str, str] = {}
        for key, value in raw.items():
            alias = str(key or "").strip()
            target = str(value or "").strip()
            if alias and target:
                aliases[alias.lower()] = target
        return aliases

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

    @filter.custom_filter(McArcCommandFilter, priority=1000)
    async def on_mc_arc_hub_command(self, event: AstrMessageEvent):
        """Handle /mc ... locally before LLM chat consumes the message."""
        raw = extract_event_raw_text(event)
        mc_raw = normalize_mc_arc_raw_message(raw)
        if mc_raw is None:
            return

        self._remember_umo(event)

        if is_mc_activate_command(mc_raw):
            await self._handle_mc_activate(event)
            event.stop_event()
            return

        if not self._hub:
            await self._reply_to_event(event, f"{_HUB_DISPLAY}尚未启动")
            event.stop_event()
            return

        if not is_known_arc_command(mc_raw):
            await self._reply_to_event(
                event,
                f"[{self.config.get('hub_server_name') or _HUB_DISPLAY}]\n"
                "未知 /mc 指令。发送 /mc help 查看帮助。",
            )
            event.stop_event()
            return

        display_name = self._resolve_display_name(event)
        gid = str(event.get_group_id() or event.get_session_id() or "").strip()
        await self._hub.push_command_forward(
            raw_message=mc_raw,
            user_id=event.get_sender_id(),
            display_name=display_name,
            group_id=gid,
            sender_role=self._qq_sender_role(event),
        )
        event.stop_event()

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

        raw = extract_event_raw_text(event)

        if normalize_mc_arc_raw_message(raw) is not None:
            return

        display_name = self._resolve_display_name(event)
        sender_role = "member"
        try:
            raw_obj = event.message_obj.raw_message if event.message_obj else None
            if isinstance(raw_obj, dict):
                sender_role = str((raw_obj.get("sender") or {}).get("role") or "member")
        except Exception:
            pass

        if raw.startswith("/"):
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
        activated = (
            self._activation_store.list_sessions() if self._activation_store else []
        )
        lines.append(f"已激活 MC 工具会话: {len(activated)}")
        for item in activated[:5]:
            lines.append(
                f"• {item.get('label') or item.get('session_id') or item.get('session_key')}"
            )
        if len(activated) > 5:
            lines.append(f"… 另有 {len(activated) - 5} 个")
        yield event.plain_result("\n".join(lines))

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """Append MC extra system instructions without replacing AstrBot persona."""
        if event.get_extra("mc_ai_event"):
            parts: list[str] = [_MC_AI_IDENTITY_HINT]
            extra = event.get_extra("mc_ai_extra_system")
            if extra:
                text = str(extra).strip()
                if text:
                    parts.append(text)
        elif self._is_external_mc_tool_session(event):
            parts = [_QQ_MC_TOOL_HINT]
            listing = self._format_ai_helper_listing()
            if listing:
                parts.append("已连接的 Minecraft 服务器：\n" + listing)
        else:
            return
        prefix = (req.system_prompt or "").rstrip()
        block = "# Minecraft Server Extra Instructions\n\n" + "\n\n".join(parts)
        req.system_prompt = f"{prefix}\n\n{block}\n" if prefix else f"{block}\n"

    def _is_external_mc_tool_session(self, event: AstrMessageEvent) -> bool:
        """Return True when this AstrBot session may use Minecraft AI tools."""
        if event.get_extra("mc_ai_event"):
            return False
        return self._is_tool_session_activated(event)

    def _qq_sender_role(self, event: AstrMessageEvent) -> str:
        try:
            raw_obj = event.message_obj.raw_message if event.message_obj else None
            if isinstance(raw_obj, dict):
                return str((raw_obj.get("sender") or {}).get("role") or "member").lower()
        except Exception:
            pass
        return "member"

    def _qq_can_run_command(self, event: AstrMessageEvent) -> bool:
        """Whether this caller may run world-changing MC tools.

        Hub admins always can. In a QQ group with a known member role, only
        owner/admin can. Sessions without a group id rely on hub admin list.
        """
        uid = str(event.get_sender_id() or "").strip()
        if uid and uid in self._admin_ids():
            return True
        gid = str(event.get_group_id() or "").strip()
        if not gid:
            return False
        return self._qq_sender_role(event) in {"owner", "admin"}

    def _format_ai_helper_listing(self) -> str:
        if not self._hub:
            return ""
        helpers = self._hub.list_ai_helper_game_names()
        if not helpers:
            return ""
        catalog_ids = {
            str(item["name"]): int(item["id"])
            for item in self._hub.get_server_catalog()
        }
        lines: list[str] = []
        for name in helpers:
            sid = catalog_ids.get(name)
            if sid is not None:
                lines.append(f"[{sid}] {name}")
            else:
                lines.append(name)
        return "\n".join(lines)

    def _resolve_tool_server(
        self, event: AstrMessageEvent, server_hint: str
    ) -> tuple[str, str]:
        """Pick the AI Helper game server for a tool call.

        Args:
            event: Current AstrBot event.
            server_hint: Optional name, catalog id, or alias.

        Returns:
            ``(server_name, error)``. Exactly one side is non-empty.
        """
        hint = str(server_hint or "").strip()
        if event.get_extra("mc_ai_event"):
            origin = str(event.get_extra("mc_ai_server") or "").strip()
            if origin:
                return origin, ""
            return "", "无法确定 Minecraft 服务器名称。"
        if not self._hub:
            return "", "弧光消息中枢尚未启动。"
        helpers = self._hub.list_ai_helper_game_names()
        if not helpers:
            return "", "当前没有 Minecraft AI Helper 在线。"
        if not hint:
            if len(helpers) == 1:
                return helpers[0], ""
            listing = self._format_ai_helper_listing() or "\n".join(helpers)
            return "", "当前连了多台 Minecraft 服务器，请填写 server（名称、编号或别名）。已连接：\n" + listing

        aliases = self._server_aliases()
        mapped = aliases.get(hint.lower())
        if mapped:
            hint = mapped

        try:
            sid = int(hint)
        except ValueError:
            sid = None
        if sid is not None:
            for item in self._hub.get_server_catalog():
                if int(item["id"]) == sid:
                    name = str(item["name"])
                    if self._hub.find_ai_helper_ws(name) is not None:
                        return name, ""
                    return "", f"服务器 [{name}] 的 AI Helper 未连接"

        lowered = hint.lower()
        exact = [name for name in helpers if name.lower() == lowered]
        if len(exact) == 1:
            return exact[0], ""
        partial = [name for name in helpers if lowered in name.lower()]
        if len(partial) == 1:
            return partial[0], ""
        if len(partial) > 1:
            return "", "server 匹配到多台服，请改用更完整的名称或编号：\n" + "\n".join(partial)
        listing = self._format_ai_helper_listing() or "\n".join(helpers)
        return "", f"找不到服务器 [{hint}]。已连接：\n{listing}"

    async def _call_mc_ai_tool(
        self,
        event: AstrMessageEvent,
        action: str,
        args: dict | None = None,
        *,
        server: str = "",
        require_admin: bool = False,
    ) -> str:
        """Run a Minecraft AI Helper tool for MC chat or any AstrBot session.

        Args:
            event: Current AstrBot event.
            action: Helper action name.
            args: Extra arguments for the action.
            server: External-side server hint; ignored for in-game origin server.
            require_admin: If True, identifiable QQ group callers must be hub
                admin / group owner / admin. Sessions without a group id are allowed.
        """
        is_mc = bool(event.get_extra("mc_ai_event"))
        if not self._hub:
            return "弧光消息中枢尚未启动。"
        if not is_mc and not self._is_tool_session_activated(event):
            return (
                "该会话尚未激活 Minecraft 工具。"
                "请让插件管理员在本对话发送 /mc activate。"
            )
        if require_admin and not is_mc and not self._qq_can_run_command(event):
            return "没有权限：执行游戏指令仅限插件管理员、群主和群管理员。"

        server_name, error = self._resolve_tool_server(event, server)
        if error:
            return error
        payload = dict(args or {})
        if is_mc:
            payload.setdefault("is_op", bool(event.get_extra("mc_ai_is_op")))
            payload.setdefault("player_name", str(event.get_extra("mc_ai_player_name") or ""))
            payload.setdefault("player_xuid", str(event.get_extra("mc_ai_xuid") or ""))
        else:
            payload.setdefault("is_op", self._qq_can_run_command(event))
            payload.setdefault(
                "player_name",
                str(event.get_sender_name() or event.get_sender_id() or ""),
            )
            payload.setdefault("player_xuid", "")
        try:
            resp = await self._hub.call_ai_tool(server_name, action, payload, timeout=20)
        except Exception as e:
            logger.warning(f"[{_HUB_DISPLAY}] MC 工具 {action} 失败: {e}")
            return f"调用 Minecraft 工具失败: {e}"
        if not isinstance(resp, dict):
            return "Minecraft 工具返回格式异常"
        if not resp.get("ok"):
            return str(resp.get("error") or "Minecraft 工具执行失败")
        text = str(resp.get("text") or "").strip() or "（无返回）"
        if not is_mc and len(self._hub.list_ai_helper_game_names()) > 1:
            return f"[{server_name}]\n{text}"
        return text

    @filter.llm_tool(name="mc_list_servers")
    async def mc_list_servers(self, event: AstrMessageEvent, reason: str) -> str:
        """列出当前已连接、可执行工具的 Minecraft 服务器名称与编号。多开服时先调用再填写其它工具的 server。

        Args:
            reason(string): 简要说明为何查询，例如「要确认打哪台服」
        """
        _ = reason
        if not self._hub:
            return "弧光消息中枢尚未启动。"
        if not event.get_extra("mc_ai_event") and not self._is_tool_session_activated(event):
            return (
                "该会话尚未激活 Minecraft 工具。"
                "请让插件管理员在本对话发送 /mc activate。"
            )
        listing = self._format_ai_helper_listing()
        if not listing:
            return "当前没有 Minecraft AI Helper 在线。"
        return "已连接的 Minecraft 服务器：\n" + listing

    @filter.llm_tool(name="mc_list_players")
    async def mc_list_players(
        self, event: AstrMessageEvent, reason: str, server: str = ""
    ) -> str:
        """查询指定 Minecraft 服务器在线玩家名单与人数。玩家问起谁在线、有没有某某、在线人数时必须调用，禁止编造。

        Args:
            reason(string): 简要说明为何查询，例如「玩家问谁在线」
            server(string): 目标服务器名称、编号或别名；游戏内可留空；QQ 多开服必须填
        """
        _ = reason
        return await self._call_mc_ai_tool(event, "list", server=server)

    @filter.llm_tool(name="mc_get_tps")
    async def mc_get_tps(
        self, event: AstrMessageEvent, reason: str, server: str = ""
    ) -> str:
        """查询指定 Minecraft 服务器 TPS / MSPT 等性能数据。玩家问起卡不卡、TPS、延迟时必须调用，禁止编造。

        Args:
            reason(string): 简要说明为何查询，例如「玩家问 TPS」
            server(string): 目标服务器名称、编号或别名；游戏内可留空；QQ 多开服必须填
        """
        _ = reason
        return await self._call_mc_ai_tool(event, "tps", server=server)

    @filter.llm_tool(name="mc_server_info")
    async def mc_server_info(
        self, event: AstrMessageEvent, reason: str, server: str = ""
    ) -> str:
        """查询指定 Minecraft 服务器基本信息（名称、版本、在线人数、运行时长等）。不要编造。

        Args:
            reason(string): 简要说明为何查询，例如「玩家问服务器信息」
            server(string): 目标服务器名称、编号或别名；游戏内可留空；QQ 多开服必须填
        """
        _ = reason
        return await self._call_mc_ai_tool(event, "info", server=server)

    @filter.llm_tool(name="mc_run_command")
    async def mc_run_command(
        self, event: AstrMessageEvent, command: str, server: str = ""
    ) -> str:
        """在指定 Minecraft 服务器控制台执行一条游戏指令。禁止 stop、kill；gamemode 仅 OP/管理员明确要求时可用。需要真实改游戏世界或给效果时调用。劈闪电用 execute at 玩家名 run summon lightning_bolt ~ ~ ~，不要写成 effect 玩家名 summon。

        Args:
            command(string): 不含斜杠的游戏指令。给效果如 effect Steve night_vision 30 0 true；劈闪电如 execute at Steve run summon lightning_bolt ~ ~ ~
            server(string): 目标服务器名称、编号或别名；游戏内可留空；QQ 多开服必须填
        """
        command_line = str(command or "").strip()
        if not command_line:
            return "指令为空"
        return await self._call_mc_ai_tool(
            event,
            "cmd",
            {"command": command_line},
            server=server,
            require_admin=True,
        )

    @filter.llm_tool(name="mc_jail_player")
    async def mc_jail_player(
        self,
        event: AstrMessageEvent,
        player_name: str,
        duration: str = "",
        reason: str = "",
        server: str = "",
    ) -> str:
        """把指定玩家关进监狱（一键入狱）。玩家要求关人、入狱、坐牢、禁闭时必须调用，不要用 mc_run_command 执行 /jail。时长单位为分钟，可填 -1 或 无期；不填则用服务器默认时长。

        Args:
            player_name(string): 要关押的游戏内玩家名
            duration(string): 刑期分钟数，或 -1/life/无期；留空则用默认一键入狱时长
            reason(string): 入狱原因，可留空
            server(string): 目标服务器名称、编号或别名；游戏内可留空；QQ 多开服必须填
        """
        name = str(player_name or "").strip()
        if not name:
            return "玩家名为空"
        return await self._call_mc_ai_tool(
            event,
            "jail",
            {
                "player_name": name,
                "duration": str(duration or "").strip(),
                "reason": str(reason or "").strip(),
            },
            server=server,
            require_admin=True,
        )

    @filter.llm_tool(name="mc_release_player")
    async def mc_release_player(
        self, event: AstrMessageEvent, player_name: str, server: str = ""
    ) -> str:
        """释放监狱中的指定玩家。玩家要求放人、出狱、释放时调用。

        Args:
            player_name(string): 要释放的游戏内玩家名
            server(string): 目标服务器名称、编号或别名；游戏内可留空；QQ 多开服必须填
        """
        name = str(player_name or "").strip()
        if not name:
            return "玩家名为空"
        return await self._call_mc_ai_tool(
            event,
            "release",
            {"player_name": name},
            server=server,
            require_admin=True,
        )

    @filter.llm_tool(name="mc_list_prisoners")
    async def mc_list_prisoners(
        self, event: AstrMessageEvent, reason: str, server: str = ""
    ) -> str:
        """查询指定 Minecraft 服务器当前在押玩家名单。问起谁在坐牢、监狱里有谁时必须调用，禁止编造。

        Args:
            reason(string): 简要说明为何查询，例如「玩家问谁在坐牢」
            server(string): 目标服务器名称、编号或别名；游戏内可留空；QQ 多开服必须填
        """
        _ = reason
        return await self._call_mc_ai_tool(event, "prisoners", server=server)

    @filter.llm_tool(name="mc_skyeye_player")
    async def mc_skyeye_player(
        self,
        event: AstrMessageEvent,
        player_name: str,
        minutes: str = "30",
        action: str = "",
        server: str = "",
    ) -> str:
        """查询天眼：指定玩家现在在哪、是否在领地内，以及近几分钟做过什么（破坏/放置/交互/进出服等）。问起某人在哪、刚干了什么时必须调用，禁止编造。仅管理员。

        Args:
            player_name(string): 游戏内玩家名
            minutes(string): 回溯分钟数，默认 30
            action(string): 可选，限定行为类型，如 BlockBreak / BlockPlace / ActorDamage / PlayerDeath
            server(string): 目标服务器名称、编号或别名；游戏内可留空；QQ 多开服必须填
        """
        name = str(player_name or "").strip()
        if not name:
            return "玩家名为空"
        return await self._call_mc_ai_tool(
            event,
            "skyeye_player",
            {
                "player_name": name,
                "minutes": str(minutes or "30").strip(),
                "action": str(action or "").strip(),
            },
            server=server,
            require_admin=True,
        )

    @filter.llm_tool(name="mc_skyeye_combat")
    async def mc_skyeye_combat(
        self,
        event: AstrMessageEvent,
        player_name: str,
        minutes: str = "30",
        server: str = "",
    ) -> str:
        """查询天眼战斗记录：该玩家打了谁、被谁打了、死亡。问起打架、被打、击杀时必须调用，禁止编造。仅管理员。

        Args:
            player_name(string): 游戏内玩家名
            minutes(string): 回溯分钟数，默认 30
            server(string): 目标服务器名称、编号或别名；游戏内可留空；QQ 多开服必须填
        """
        name = str(player_name or "").strip()
        if not name:
            return "玩家名为空"
        return await self._call_mc_ai_tool(
            event,
            "skyeye_combat",
            {"player_name": name, "minutes": str(minutes or "30").strip()},
            server=server,
            require_admin=True,
        )

    @filter.llm_tool(name="mc_skyeye_location")
    async def mc_skyeye_location(
        self,
        event: AstrMessageEvent,
        x: str,
        y: str,
        z: str,
        radius: str = "8",
        dimension: str = "",
        minutes: str = "30",
        server: str = "",
    ) -> str:
        """查询天眼：某坐标附近谁做过什么、是否在领地内。问起这块地发生过什么、谁挖了这里时必须调用。仅管理员。

        Args:
            x(string): X 坐标
            y(string): Y 坐标
            z(string): Z 坐标
            radius(string): 半径格数，默认 8
            dimension(string): 维度，如 minecraft:overworld；可留空表示不限
            minutes(string): 回溯分钟数，默认 30
            server(string): 目标服务器名称、编号或别名；游戏内可留空；QQ 多开服必须填
        """
        return await self._call_mc_ai_tool(
            event,
            "skyeye_location",
            {
                "x": str(x or "").strip(),
                "y": str(y or "").strip(),
                "z": str(z or "").strip(),
                "radius": str(radius or "8").strip(),
                "dimension": str(dimension or "").strip(),
                "minutes": str(minutes or "30").strip(),
            },
            server=server,
            require_admin=True,
        )

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
