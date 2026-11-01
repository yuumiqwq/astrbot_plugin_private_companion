"""A compact proactive task that shares the host dialogue and its delivery path."""

from dataclasses import replace
import json


SIMPLE_PROMPT = """结合当前人格、预设对话与聊天记录，自行判断此刻是否发送一条主动消息。
当前时间：{{current_time}}。最近主动联系后尚未得到回复的次数：{{unanswered_count}}。
决定发送时，用自己的自然说话方式表达；没有想说的内容时可以不发。
只输出 JSON：发送时为 {"send":true,"message":"实际正文"}，不发送时为 {"send":false,"message":""}。"""
SIMPLE_OUTPUT = (r"只输出一个 JSON 对象，字段为 send 和 message。send 必须是布尔值；为 true 时 message 是非空正文，"
                 r"为 false 时 message 是空字符串。message 中每个非空行单独发送，换行在 JSON 字符串中转义为 \n。")
SIMPLE_TERMINAL = "保留当前人格，只返回 send/message JSON；send 为 false 时不产生消息。"
MODES = ("companion", "simple")


def simple_request(request, job, preferences):
    from .prompt_catalog import effective_prompt
    from .response_schema import contact_schema

    materials = []
    local_now = preferences.observed_at
    for material in request.context.materials:
        if material.purpose == "contact_intent":
            source = json.loads(material.payload)
            local_now = source.get("local_now", local_now.isoformat())
            # Candidate thoughts can contain event-specific instructions. The
            # compact task keeps actual state and time, not those instructions.
            facts = {key: value for key, value in source.items() if key in {
                "now", "local_now", "local_date", "local_time", "weekday", "timezone",
                "role_state", "recent_life", "recent_timing"}}
            materials.append(replace(material, purpose="current_context",
                                     payload=json.dumps(facts, ensure_ascii=False)))
        elif material.owner not in {"expression", "expression.learning", "expression.style", "role.expression"}:
            materials.append(material)
    interaction = getattr(job, "interaction", None) or {}
    unanswered = interaction.get("contacts_since_last_input", 0)
    if type(unanswered) is not int or unanswered < 0:
        unanswered = 0
    prompt = effective_prompt(preferences.values, "simple")
    prompt = prompt.replace("{{current_time}}", str(local_now))
    prompt = prompt.replace("{{unanswered_count}}", str(unanswered))
    return replace(request, instruction=prompt, proactive_mode="simple",
        response_schema_json=json.dumps(contact_schema("simple")),
        context=replace(request.context, materials=tuple(materials)),
        prompt_overrides=(
            ("task_prompt", ""),
            ("output_contract", effective_prompt(preferences.values, "simple_output")),
            ("terminal_prompt_guard", effective_prompt(preferences.values, "simple_terminal")),
            ("capability_instruction", "")))


def normalize_output(text, *, mode="companion"):
    if mode not in MODES:
        raise ValueError("unknown proactive prompt mode")
    if mode == "companion":
        return json.loads(text)
    from companion_v2.adapters.astrbot.model_wire import strict_json

    data = strict_json(text)
    if (type(data) is not dict or set(data) != {"send", "message"}
            or type(data["send"]) is not bool or type(data["message"]) is not str
            or len(data["message"]) > 65536
            or data["send"] and not data["message"].strip()
            or not data["send"] and data["message"] != ""):
        raise ValueError("simple contact output requires boolean send and a matching message string")
    return {"decision": "send" if data["send"] else "skip", "text": data["message"]}


def parse_contact_output(text, *, mode="companion", official_alert=None):
    from .prompts import contact_output

    data = normalize_output(text, mode=mode)
    body, reaction = contact_output(json.dumps(data, ensure_ascii=False), official_alert=official_alert)
    return data, body, reaction
