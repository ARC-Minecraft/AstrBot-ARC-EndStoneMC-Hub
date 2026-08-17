"""Synthetic AstrBot event for MC AI Helper conversations."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncGenerator

from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata


def sanitize_session_part(value: str) -> str:
    """Strip characters that would break unified message origin parsing.

    Args:
        value: Raw server or player identifier.

    Returns:
        Safe token without ``:`` / ``|`` / ``!``.
    """
    text = str(value or "").strip() or "unknown"
    for ch in (":", "|", "!", "/", "\\"):
        text = text.replace(ch, "_")
    return text[:64]


def build_mc_ai_session_id(server_name: str, player_name: str) -> str:
    """Build a per-player AstrBot session id for Minecraft chats.

    Args:
        server_name: Game server display name.
        player_name: Minecraft player name.

    Returns:
        Session id used in ``webchat:FriendMessage:...``.
    """
    return f"mc_ai|{sanitize_session_part(server_name)}|{sanitize_session_part(player_name)}"


class McAiMessageEvent(AstrMessageEvent):
    """Friend-chat event whose replies are captured instead of sent to QQ."""

    def __init__(
        self,
        *,
        player_name: str,
        server_name: str,
        message: str,
        extra_system_prompt: str = "",
    ) -> None:
        session_id = build_mc_ai_session_id(server_name, player_name)
        platform_meta = PlatformMetadata(
            name="webchat",
            description="MC AI Helper via ARC EndStone Hub",
            id="webchat",
            support_streaming_message=False,
            support_proactive_message=False,
        )

        msg_obj = AstrBotMessage()
        msg_obj.type = MessageType.FRIEND_MESSAGE
        msg_obj.self_id = "arc_mc_ai"
        msg_obj.session_id = session_id
        msg_obj.message_id = uuid.uuid4().hex
        msg_obj.sender = MessageMember(user_id=session_id, nickname=player_name)
        msg_obj.message = [Plain(message)]
        msg_obj.message_str = message
        msg_obj.raw_message = {
            "source": "endstone_mc_ai",
            "server_name": server_name,
            "player_name": player_name,
        }
        msg_obj.timestamp = int(time.time())

        super().__init__(message, msg_obj, platform_meta, session_id)

        self.is_wake = True
        self.is_at_or_wake_command = True
        if extra_system_prompt:
            self.set_extra("mc_ai_extra_system", extra_system_prompt)

        self._reply_parts: list[str] = []
        self._reply_ready: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    def _collect(self, message: MessageChain | None) -> None:
        if message is None:
            return
        chain_type = getattr(message, "type", None)
        if chain_type in (
            "tool_call",
            "tool_call_result",
            "reasoning",
            "audio_chunk",
            "break",
        ):
            return
        chain = getattr(message, "chain", None) or []
        texts: list[str] = []
        for comp in chain:
            text = getattr(comp, "text", None)
            if isinstance(text, str) and text:
                texts.append(text)
        if texts:
            self._reply_parts.append("".join(texts))

    def collected_reply(self) -> str:
        """Return concatenated assistant text collected so far."""
        return "".join(self._reply_parts).strip()

    async def wait_reply(self) -> str:
        """Wait until the AstrBot pipeline finishes and return the reply text."""
        return await self._reply_ready

    async def send(self, message: MessageChain | None) -> None:
        self._collect(message)
        await super().send(message if message is not None else MessageChain())

    async def send_streaming(
        self,
        generator: AsyncGenerator[MessageChain, None],
        use_fallback: bool = False,
    ) -> None:
        async for chain in generator:
            self._collect(chain)
        await super().send_streaming(generator, use_fallback=use_fallback)

    def cleanup_temporary_local_files(self) -> None:
        super().cleanup_temporary_local_files()
        if not self._reply_ready.done():
            self._reply_ready.set_result(self.collected_reply())
