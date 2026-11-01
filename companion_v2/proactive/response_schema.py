"""The existing proactive parser's two JSON contracts, visible at request end."""

import json


SKIP_REASONS = ("insufficient_context", "no_natural_opening", "purpose_mismatch",
                "repetitive", "respect_user_boundary", "other")


def contact_schema(mode="companion", *, official_alert=False):
    if mode == "simple":
        return {"type": "object", "additionalProperties": False,
            "required": ["send", "message"],
            "properties": {"send": {"type": "boolean"},
                           "message": {"type": "string", "maxLength": 65536}},
            "allOf": [{"if": {"properties": {"send": {"const": False}}},
                       "then": {"properties": {"message": {"const": ""}}},
                       "else": {"properties": {"message": {"pattern": r"\S"}}}}]}
    properties = {
        "decision": {"enum": ["send", "skip"]},
        "text": {"type": "string", "maxLength": 65536},
        "decision_confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "skip_reason": {"enum": list(SKIP_REASONS)},
        "reaction_intent": {"type": "string", "maxLength": 2000},
        "voice_intent": {"type": "boolean"},
        "voice_text": {"type": "string", "minLength": 1, "maxLength": 2000, "pattern": r"\S"},
        "poke_intent": {"type": "boolean"},
    }
    if official_alert:
        properties.update(coverage={"type": "string"},
                          coverage_quote={"type": "string", "maxLength": 1024})
    return {"type": "object", "additionalProperties": False,
        "required": ["decision", "text"], "properties": properties,
        "allOf": [
            {"if": {"properties": {"decision": {"const": "skip"}}},
             "then": {"properties": {"text": {"const": ""}, "reaction_intent": {"const": ""}},
                      "required": ["skip_reason"],
                      "not": {"anyOf": [{"required": [key]} for key in
                                         ("voice_intent", "voice_text", "poke_intent")]}},
             "else": {"properties": {"text": {"pattern": r"\S"}},
                      "not": {"required": ["skip_reason"]}}},
            {"if": {"required": ["voice_text"]},
             "then": {"required": ["voice_intent"], "properties": {"voice_intent": {"const": True}}}},
        ]}


def final_task(mode, schema):
    skip = ({"send": False, "message": ""} if mode == "simple" else
            {"decision": "skip", "text": "", "skip_reason": "no_natural_opening"})
    return {"instruction": "结合当前人格与前面的参考资料，判断本次是否主动联系。"
            "现在输出一个符合 response_schema 的完整 JSON 对象，正文换行使用 JSON 转义。"
            "决定不发送时也返回显式 JSON，参考 skip_example；空白输出不表示 skip。",
            "response_schema": schema, "skip_example": skip}


def final_task_prompt(mode, schema):
    task = final_task(mode, schema)
    send = ({"send": True, "message": "第一条消息\n第二条消息"} if mode == "simple" else
            {"decision": "send", "text": "第一条消息\n第二条消息", "decision_confidence": 0.9})
    field = "message" if mode == "simple" else "text"
    dump = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return ("现在完成本轮主动联系判断。前面的对话均为已发生的历史，本轮没有新的用户来信；"
            "结合当前时间和本轮资料，决定现在是否有合适的新内容可说。\n"
            "请只返回一个完整 JSON 对象。" + field + " 始终是一个字符串；需要多条消息时，"
            "在该字符串内用 JSON 转义换行 \\n 分隔，所有文字都放在这一对双引号内。"
            "发送示例：" + dump(send) + "。\n"
            "决定不发送时也返回 JSON 对象，例如：" + dump(task["skip_example"]) + "。\n"
            "本轮 JSON Schema：" + dump(schema))
