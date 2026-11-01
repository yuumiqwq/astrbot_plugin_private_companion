"""Reuse the host's private conversation and native context length policy."""

from dataclasses import replace
from collections.abc import Mapping
import json
import re

from companion_v2.conversation.foreground import PromptMaterial
from companion_v2.foundation.refs import ScopeKind


def native_newline_reply_instruction(context, umo, *, structured=False, proactive_mode="companion"):
    """Describe the same newline boundary that the opted-in host will send."""
    if not isinstance(umo, str) or ":FriendMessage:" not in umo:
        return ""
    manager = getattr(context, "conversation_manager", None)
    shared = getattr(manager, "shared_history", None)
    getter = getattr(context, "get_config", None)
    if shared is None or not callable(getter) or shared.settings(umo) is None:
        return ""
    config = getter(umo=umo)
    settings = config.get("platform_settings", {}).get("segmented_reply", {}) if isinstance(config, Mapping) else {}
    if (not settings.get("enable") or settings.get("split_mode", "regex") != "regex"
            or settings.get("regex") != r"[^\r\n]+"):
        return ""
    instruction = (
        "正文的每个非空行会分别发送为一条消息，空行不发送。"
        "希望放在同一条消息里的句子写在同一行，另发一条时换行；不使用特殊分段标记或分段数组。"
    )
    if structured and proactive_mode == "simple":
        return instruction + (
            "按本轮 JSON 输出格式返回，正文写在 message 字符串中，换行正确转义为 \\n；"
            "程序先解析 JSON，再按正文换行发送。send 为 false 时 message 为空，不产生消息。"
        )
    return instruction + (
        "仍按本轮 JSON 输出格式返回，正文写在 text 字符串中，换行正确转义为 \\n；"
        "程序先解析 JSON，再按正文换行发送。decision 为 skip 时 text 为空，不产生消息。"
        if structured else "直接输出聊天正文。"
    )


class AstrBotSharedContext:
    def __init__(self, context, bridge, provider_id, *, application=None):
        self.context = context
        self.bridge = bridge
        self.provider_id = provider_id
        self.application = application

    def supports(self, scope):
        """Whether the host manages this private window's shared history."""
        if scope.kind is not ScopeKind.PRIVATE:
            return False
        manager = self.context.conversation_manager
        shared = getattr(manager, "shared_history", None)
        umo = f"{scope.platform}:FriendMessage:{scope.native_scope_id}"
        return (
            shared is not None
            and callable(getattr(self.context, "prepare_session_request", None))
            and shared.settings(umo) is not None
        )

    def split_reply(self, context, text):
        """Use the host's configured paragraph boundary for shared QQ sessions."""
        scope = context.scope
        if scope.kind is not ScopeKind.PRIVATE:
            return None
        umo = f"{scope.platform}:FriendMessage:{scope.native_scope_id}"
        shared = getattr(self.context.conversation_manager, "shared_history", None)
        if shared is None or shared.settings(umo) is None:
            return None
        settings = (
            self.context.get_config(umo=umo)
            .get("platform_settings", {})
            .get("segmented_reply", {})
        )
        if not settings.get("enable") or len(text) > settings.get(
            "words_count_threshold", 1000000
        ):
            return (text,)
        if settings.get("split_mode", "regex") != "regex":
            return None
        pattern = settings.get("regex", "")
        if not pattern:
            return (text,)
        try:
            parts = re.findall(pattern, text, re.DOTALL | re.MULTILINE)
        except re.error:
            return (text,)
        if not parts or any(not isinstance(part, str) for part in parts):
            return (text,)
        return tuple(part.strip() for part in parts if part.strip())

    async def __call__(self, request):
        """Read the current host history only for opted-in private sessions.

        Args:
            request: Prepared background request, before model budgeting.

        Returns:
            Request with host-managed dialogue and the current persona.
        """
        return await self.for_task(request, None)

    async def for_task(self, request, task_context):
        scope = request.event.scope
        if not self.supports(scope):
            return request
        manager = self.context.conversation_manager
        prepare = self.context.prepare_session_request
        umo = f"{scope.platform}:FriendMessage:{scope.native_scope_id}"

        persona = await self.bridge.verified_persona(scope, request.role_binding)
        conversation_id = await manager.get_curr_conversation_id(umo)
        conversation = (
            await manager.get_conversation(umo, conversation_id)
            if conversation_id else None
        )
        if conversation_id and conversation is None:
            raise ValueError("The selected AstrBot conversation is unavailable")
        dialogue = json.loads(conversation.history or "[]") if conversation else []
        provider_id = self.provider_id
        selected_provider = getattr(self.context, "get_current_chat_provider_id", None)
        if callable(selected_provider):
            provider_id = await selected_provider(umo)
        provider = self.context.get_provider_by_id(provider_id)
        if provider is None:
            raise ValueError("The shared AstrBot provider is unavailable")

        from astrbot.api.provider import ProviderRequest

        # Retain delivery candidates for deduplication, while ordinary dialogue
        # comes exclusively from the same history used by AstrBot replies.
        materials = tuple(
            item
            for item in request.context.materials
            if item.owner not in {"conversation.private_history", "astrbot.conversation"}
            and item.purpose
            not in {"confirmed_background_message", "confirmed_reply_context"}
        )
        if self.application is not None and task_context is not None:
            from .shared_materials import common_materials, merge_common_materials
            common = common_materials(self.application, task_context, request, dialogue)
            materials = merge_common_materials(materials, common)
        task = json.dumps(
            [
                {"owner": item.owner, "purpose": item.purpose, "payload": item.payload}
                for item in materials
            ],
            ensure_ascii=False,
        )
        native_request = ProviderRequest(
            prompt=task,
            system_prompt=persona.get("system_prompt", persona["prompt"]),
            contexts=[
                *persona.get("begin_dialogs", ()),
                *dialogue,
            ],
        )
        instruction = request.instruction
        if getattr(request, "output_format", None) == "proactive_contact":
            newline_instruction = native_newline_reply_instruction(self.context, umo, structured=True,
                proactive_mode=getattr(request, "proactive_mode", "companion"))
            if newline_instruction:
                instruction += "\n" + newline_instruction
        await prepare(
            umo,
            provider,
            native_request,
            response_instruction=instruction
            + "\n"
            + "\n".join(text for _, text in getattr(request, "prompt_overrides", ()))
            + "\n"
            + (request.response_schema_json or ""),
        )
        history = [
            {"role": item["role"], "content": item["content"]}
            for item in native_request.contexts
            if item.get("role") in {"user", "assistant", "system"} and "content" in item
        ]
        material = PromptMaterial(
            "astrbot.conversation",
            "recent_private_dialogue",
            json.dumps(history, ensure_ascii=False),
        )
        return replace(
            request,
            instruction=instruction,
            role_instruction=persona.get("system_prompt", persona["prompt"]),
            role_guard_instruction=persona["prompt"],
            host_provider_id=provider_id,
            context=replace(request.context, materials=(*materials, material)),
        )
