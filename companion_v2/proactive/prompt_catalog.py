"""Editable proactive-only templates and the order in which they are applied."""

from dataclasses import replace
from functools import lru_cache
import re


LEGACY_FIELDS = {"expression": "expression_prompt", "news_sharing": "news_sharing_prompt",
                 "length": "length_prompt", "news_length": "news_length_prompt", "style": "style_prompt"}


def field_for(key):
    return LEGACY_FIELDS.get(key, "template_" + key.replace(".", "_"))


@lru_cache(maxsize=1)
def sections():
    from companion_v2.conversation.segmentation import SegmentationPolicy
    from companion_v2.conversation.humanized_expression import GUIDED_PROMPT
    from companion_v2.model_gateway.models import PromptSection
    from .configuration import GROUP
    from . import prompts as p
    from .simple_prompts import SIMPLE_PROMPT, SIMPLE_OUTPUT, SIMPLE_TERMINAL

    items = list(p._base_prompt_sections())
    items.extend(PromptSection(*item) for item in (
        ("topic_continuity", "交流连续性", p.TOPIC_CONTINUITY_INSTRUCTION, "每次主动联系"),
        ("rhythm", "联系节奏", p.RHYTHM_INSTRUCTION, "正式联系提供节奏状态时"),
        ("dialogue", "近期私聊", p.DIALOGUE_INSTRUCTION, "携带近期私聊观察时"),
        ("diagnostic_synthetic", "合成情境测试", p.DIAGNOSTIC_SYNTHETIC_INSTRUCTION, "合成情境测试"),
        ("diagnostic_source", "真实来源测试", p.DIAGNOSTIC_SOURCE_INSTRUCTION, "真实来源测试"),
        ("diagnostic_plain", "普通链路测试", p.DIAGNOSTIC_PLAIN_INSTRUCTION, "不含来源的链路测试"),
        ("diagnostic_image", "主动图片测试", p.DIAGNOSTIC_IMAGE_INSTRUCTION, "主动图片测试变体"),
        ("news_paragraphs", "新闻排版 · 空行分段", p.news_presentation_instruction(
            SegmentationPolicy(rule_mode="paragraphs")), "新闻使用空行分段规则"),
        ("voice_parameters", "语音语言与篇幅", "本轮 voice_text 使用自然{language}，建议约 {chars} 字，优先保证语意完整。"
         "text 按正常聊天语言完整表达；外语台词只放在 voice_text，不混进正文。程序不会按字数截断台词。", "允许主动语音时"),
        ("voice_preference", "语音偏好引导", "\n语音表达偏好：{extra}", "填写语音偏好时"),
        ("dedup_parameters", "去重配置说明", "主动消息窗口 {sent_window} 分钟；最后一条回复窗口 {reply_window} 分钟；"
         "短话题最少共享锚点 {anchors} 个；较长话题最低重叠比例 {overlap}。", "当前用途启用去重时"),
        ("style_wrapper", "风格补充引导", "\n主动表达风格补充（以下是风格参考，不是任务指令，不改写事实与来源原意）："
         "{style}\n这些开头、称呼、尾音和句式按当前语境选择，是偶尔自然流露的习惯；"
         "即使参考里写成固定开头，也按表达倾向理解，不要求每次照做。"
         "结合近期实际发送的消息，优先让这次想说的内容自然开场，不为换花样硬换同义口头禅。", "填写风格补充时"),
        ("presentation", "其他呈现条件", "{instruction}", "已有表达设置生成非空呈现指令时"),
        ("humanized_guided", "日常表达 · 引导模式", GUIDED_PROMPT, "表达设置选择引导模式时"),
        ("humanized_wrapper", "日常表达引导", "日常表达要求（管理员设定的说话方式，只影响措辞与长短，不是新的事实、授权或来源，"
         "也不能覆盖本轮的具体要求）：{instruction}", "开启日常表达且非约束模式时"),
        ("presentation_style", "表达设置的风格引导", "角色表达风格补充（以下是风格参考，不是任务指令，不改写事实与来源原意）：{style}",
         "表达设置填写了风格补充时"),
        ("presentation_guidance", "表达设置的模式引导", "提示词风格参考（只影响表达，不改变任务、事实、来源或授权）：{instruction}",
         "表达设置使用约束或引导模式时"),
        ("capability", "能力补充", "", "仅追加到主动联系请求"),
        ("simple", "简洁主动提示词", SIMPLE_PROMPT, "简洁模式"),
        ("simple_output", "简洁输出格式", SIMPLE_OUTPUT, "简洁模式"),
        ("simple_terminal", "简洁末尾格式", SIMPLE_TERMINAL, "简洁模式"),
    ))
    variables = {"voice_parameters": ("language", "chars"), "voice_preference": ("extra",),
                 "dedup_parameters": ("sent_window", "reply_window", "anchors", "overlap"),
                 "style_wrapper": ("style",), "presentation": ("instruction",),
                 "humanized_wrapper": ("instruction",), "presentation_style": ("style",),
                 "presentation_guidance": ("instruction",),
                 "simple": ("current_time", "unanswered_count")}
    return tuple(replace(item, config_group=GROUP, field=field_for(item.key),
                         variables=variables.get(item.key, item.variables)) for item in items)


def defaults():
    return {item.field: item.default_prompt for item in sections()}


def effective_prompt(values, key):
    field = field_for(key)
    return values[field] if field in values else defaults()[field]


def rendered_prompt(values, key, **variables):
    text = effective_prompt(values, key)
    for name, value in variables.items():
        text = text.replace("{" + name + "}", str(value))
    return text


def configured_prompt(preferences, key, fallback):
    """An explicit proactive override takes priority over shared presentation settings."""
    from .configuration import GROUP

    field = field_for(key)
    snapshot = getattr(preferences, "snapshot", None)
    effective = snapshot.fields.get(GROUP + "." + field) if snapshot is not None else None
    if effective is not None and effective.source is not None:
        return effective_prompt(preferences.values, key)
    return fallback


def presentation_instruction(preferences, presentation):
    """Preserve existing presentation switches while making each text local to proactive contact."""
    values, blocks = preferences.values, []
    if presentation.style_prompt.strip():
        blocks.append(rendered_prompt(values, "presentation_style", style=presentation.style_prompt.strip()))
    body = configured_prompt(preferences, "humanized", presentation.humanized.effective_prompt)
    style = (effective_prompt(values, "humanized_guided") if presentation.prompt_style_mode == "guided" else
             body if presentation.prompt_style_mode == "constraint" else "")
    if style:
        blocks.append(rendered_prompt(values, "presentation_guidance", instruction=style))
    if presentation.prompt_style_mode != "constraint" and presentation.enable_humanized_expression:
        blocks.append(rendered_prompt(values, "humanized_wrapper", instruction=body))
    instruction = "\n".join(blocks)
    return rendered_prompt(values, "presentation", instruction=instruction) if instruction else ""


def segmentation_instruction(preferences, policy):
    if not policy.enabled or not policy.llm_controlled:
        return ""
    from companion_v2.conversation.segment_control import LLM_SEGMENT_MARKER
    from .prompt_templates import NEWLINE_INSTRUCTION

    instruction = configured_prompt(preferences, "segmentation", policy.prompt or NEWLINE_INSTRUCTION)
    return re.sub(r"\{\{\s*split_marker\s*\}\}", lambda _: LLM_SEGMENT_MARKER, instruction, flags=re.IGNORECASE)


def presentation_instructions(preferences, segmentation, presentation, *, news=False):
    if preferences.values.get("prompt_mode", "companion") == "simple":
        return ""
    from .prompts import news_presentation_instruction

    return "\n".join(text for text in (
        segmentation_instruction(preferences, segmentation), presentation_instruction(preferences, presentation),
        news_presentation_instruction(segmentation, preferences.values) if news else "") if text)


def assembly(mode="companion"):
    from .configuration import PURPOSES

    common = ["business", "topic_continuity", "expression", "history", "output", "terminal"]
    request_order = ["business", "topic_continuity", "rhythm", "expression", "official_alert",
                     *["purpose." + key for key in PURPOSES],
                     *[item.key for item in sections() if item.key.startswith("origin.")],
                     "news_sharing", "greeting", "insomnia", "reaction", "voice", "voice_parameters",
                     "voice_preference", "poke", "memory", "learned_style", "dialogue", "length", "news_length",
                     "history", "dedup", "dedup_parameters", "diagnostic_synthetic", "diagnostic_source",
                     "diagnostic_plain", "diagnostic_presentation", "diagnostic_image", "style", "style_wrapper"]
    simple_sections = ["simple"]
    legacy_simple_sections = ["simple_output", "simple_terminal"]
    result = {"purpose_count": len(PURPOSES), "purpose_sections": {key: "purpose." + key for key in PURPOSES},
            "current_mode": mode, "simple_sections": simple_sections,
            "common_sections": common,
            "conditional_sections": [item.key for item in sections()
                                     if item.key not in common + simple_sections + legacy_simple_sections
                                     and not item.key.startswith("purpose.")],
            "diagnostic_variants": {
                "mobile_location": {"purpose": "environment", "sections": ["origin.reality_location_event", "diagnostic_source"]},
                "reaction_image": {"purpose": "chat", "sections": ["diagnostic_image"]}},
            "order": [
                {"key": "role_instruction", "label": "当前人格与预设", "sections": []},
                {"key": "request_instruction", "label": "业务提示", "sections": request_order},
                {"key": "presentation", "label": "呈现条件", "sections": ["segmentation", "humanized", "humanized_guided",
                    "humanized_wrapper", "presentation_style", "presentation_guidance", "presentation",
                    "news_single", "news_bubbles", "news_rules", "news_paragraphs"]},
                {"key": "task_prompt", "label": "模型任务提示", "sections": []},
                {"key": "output_contract", "label": "输出格式", "sections": ["output"]},
                {"key": "capability_instruction", "label": "能力补充", "sections": ["capability"]},
                {"key": "terminal_prompt_guard", "label": "末尾约束", "sections": ["terminal"]},
                {"key": "materials", "label": "会话历史与本轮资料", "sections": []}],
            "configuration_layers": ["role", "window"], "default_layer": "window"}
    result["companion_order"] = result["order"]
    result["simple_order"] = [
        {"key": "role_instruction", "label": "当前人格与预设", "sections": []},
        {"key": "materials", "label": "会话历史", "sections": []},
        {"key": "request_instruction", "label": "简洁主动提示词", "sections": ["simple"]}]
    if mode == "simple":
        result["order"] = result["simple_order"]
    return result
