"""Reuse ordinary private-reply material owners for role background work."""

from dataclasses import replace
import json
from types import SimpleNamespace

from companion_v2.conversation.foreground import PromptMaterial
from companion_v2.execution.models import TaskError

from .core_reply_context import build_core_reply_prompt


OWNER = "astrbot.role_context"
COMMON_SECTIONS = frozenset({
    "person_card", "knowledge_reference", "knowledge_member_clue", "perception",
    "proactive_inspirations", "relationship_profile", "memory", "legacy_memory",
    "relationship", "life", "growth", "self_review", "open_items", "role_state",
})
MEMORY_PURPOSES = frozenset({
    "pinned_memory", "current_experience", "current_memory", "memory_coverage",
    "current_input_handles", "historical_memory",
})


def latest_user_text(history):
    """Select the same window's latest textual query, retaining media as media."""
    for item in reversed(history):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(part["text"] for part in content
                if isinstance(part, dict) and part.get("type") == "text"
                and isinstance(part.get("text"), str))
    return ""


def common_materials(application, context, request, history):
    if context.scope != request.event.scope or context.role_binding != request.role_binding:
        raise TaskError("shared_context_changed", "background materials require the original task window and role")
    principal = getattr(request.event, "principal", None)
    if principal is not None and principal != context.actor.principal:
        raise TaskError("shared_context_changed", "background materials require the original task principal")
    at = getattr(request.event, "observed_at", None) or request.event.received_at
    native_input = SimpleNamespace(text=latest_user_text(history),
        event_id=getattr(request.event, "cause_id", None) or request.event.event_id,
        received_at=at)
    def collect():
        return build_core_reply_prompt(application, context, native_input, at=at,
                                       include_sections=COMMON_SECTIONS)

    runtime = getattr(application, "runtime", None)
    if callable(getattr(runtime, "transaction", None)):
        # Optional recall may create temporary query handles. Background context
        # reads use the captured text, so those handles need not survive or alter
        # state during a simulated request.
        with runtime.transaction() as db:
            db.execute("SAVEPOINT shared_role_materials")
            try:
                prompt = collect()
            finally:
                db.execute("ROLLBACK TO shared_role_materials")
                db.execute("RELEASE shared_role_materials")
    else:
        prompt = collect()
    materials = [PromptMaterial(OWNER, name, block) for name, block in prompt.context_blocks]
    if prompt.perception_context:
        materials.append(PromptMaterial(OWNER, "perception", json.dumps({
            "context": json.loads(prompt.perception_context),
            "instructions": prompt.perception_instructions,
            "guidance": prompt.perception_guidance,
        }, ensure_ascii=False)))
    return tuple(materials)


def merge_common_materials(materials, common):
    """Replace optional duplicate previews while retaining task evidence and inputs."""
    sections = {item.purpose for item in common}
    result = []
    for item in materials:
        if item.owner == OWNER:
            continue
        if "memory" in sections and item.owner == "memory" and item.purpose in MEMORY_PURPOSES:
            continue
        if item.owner == "proactive.contact" and item.purpose == "contact_intent":
            try:
                payload = json.loads(item.payload)
            except (TypeError, ValueError):
                payload = None
            if isinstance(payload, dict):
                if "role_state" in sections:
                    payload.pop("role_state", None)
                if "life" in sections:
                    payload.pop("recent_life", None)
                item = replace(item, payload=json.dumps(payload, ensure_ascii=False))
        result.append(item)
    return (*result, *common)
