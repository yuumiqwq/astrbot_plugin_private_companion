"""Reuse the host's private conversation and native context length policy."""

from dataclasses import replace
import json
import re

from companion_v2.conversation.foreground import PromptMaterial
from companion_v2.foundation.refs import ScopeKind


class AstrBotSharedContext:
    def __init__(self, context, bridge, provider_id):
        self.context = context
        self.bridge = bridge
        self.provider_id = provider_id

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
            request: Prepared contact request, before model budgeting.

        Returns:
            Request with host-managed dialogue and the current persona.
        """
        scope = request.event.scope
        if scope.kind is not ScopeKind.PRIVATE:
            return request
        manager = self.context.conversation_manager
        shared = getattr(manager, "shared_history", None)
        prepare = getattr(self.context, "prepare_session_request", None)
        umo = f"{scope.platform}:FriendMessage:{scope.native_scope_id}"
        if shared is None or not callable(prepare) or shared.settings(umo) is None:
            return request

        persona = await self.bridge.verified_persona(scope, request.role_binding)
        conversation_id = await manager.get_curr_conversation_id(umo)
        if not conversation_id:
            return request
        conversation = await manager.get_conversation(umo, conversation_id)
        if conversation is None:
            raise ValueError("The selected AstrBot conversation is unavailable")
        provider = self.context.get_provider_by_id(self.provider_id)
        if provider is None:
            raise ValueError("The shared AstrBot provider is unavailable")

        from astrbot.api.provider import ProviderRequest

        # Retain delivery candidates for deduplication, while ordinary dialogue
        # comes exclusively from the same history used by AstrBot replies.
        materials = tuple(
            item
            for item in request.context.materials
            if item.owner != "conversation.private_history"
            and item.purpose
            not in {"confirmed_background_message", "confirmed_reply_context"}
        )
        task = json.dumps(
            [
                {"owner": item.owner, "purpose": item.purpose, "payload": item.payload}
                for item in materials
            ],
            ensure_ascii=False,
        )
        native_request = ProviderRequest(
            prompt=task,
            system_prompt=persona["prompt"],
            contexts=json.loads(conversation.history or "[]"),
        )
        await prepare(
            umo,
            provider,
            native_request,
            response_instruction=request.instruction
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
            role_instruction=persona["prompt"],
            role_guard_instruction=persona["prompt"],
            context=replace(request.context, materials=(*materials, material)),
        )
