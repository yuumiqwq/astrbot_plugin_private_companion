"""Editable daily task instructions shared by scheduled runs and previews."""

from companion_v2.agenda.planning_output import INSTRUCTION as PLAN_INSTRUCTION
from companion_v2.configuration.models import ConfigLayer, ConfigTarget
from companion_v2.execution.models import encode, require
from companion_v2.memory.prompts import REFLECTION_INSTRUCTION
from companion_v2.model_gateway.models import PromptSection
from companion_v2.role_work.journal.prompts import COMMON, DIARY

GROUP = "model.role.daily_review"
GLOBAL = (
    "每日整理：用同一人格、预设对话与会话历史，总结 local_date 当天的日记和记忆，安排次日活动。"
    "本轮只调用一次模型。启用的各部分遵守下述资料与格式，关闭或已存在的部分严格输出 null。"
    "只返回 JSON 对象 {\"diary\":对象或null,\"day_plan\":对象或null,\"memory\":对象或null}。"
    "在总输出预算内简明完整地写完三个字段。日记通常简短，记忆只保留值得长期记住的少量变化。\n"
)
DIARY_TASK = (COMMON + DIARY.split("仅输出", 1)[0]
    + "diary 对象包含 body、form、recall:null、afterglow:\"\" 和 material_ids。"
      "material_ids 只选择本轮 writing_materials 中实际使用的原编号，最多 64 个，可为空。")
PLAN_TASK = (PLAN_INSTRUCTION.split("daily/horizon 输出", 1)[0]
    + "本轮规划 start 至 end 指定的明天，不规划今天剩余时间。day_plan 输出 {\"items\":["
      "{\"title\":\"计划标题\",\"start\":\"含时区ISO时间\",\"minutes\":30,\"detail\":\"准备怎样做\","
      "\"goal_id\":null,\"parent_id\":null}]}；通常安排一两件事，无新安排用空数组。")
MEMORY_TASK = (REFLECTION_INSTRUCTION
    + "\n上述记忆结果放入外层 memory 字段，使用本轮原始 evidence_handles；无可保存事项时保留空候选数组。")
DEFAULTS = {"global": GLOBAL, "diary": DIARY_TASK, "day_plan": PLAN_TASK, "memory": MEMORY_TASK}


def output_contract():
    from .inputs import DAILY_SCHEMA
    return ("本轮完整输出结构如下；保留字段名称、对象层级和数据类型，关闭项为 null：\n"
            + encode(DAILY_SCHEMA))


def sections():
    from companion_v2.model_gateway.prompt_composer import FINAL_PROMPT_GUARD
    from companion_v2.adapters.astrbot.model_wire import BACKGROUND_OUTPUT_NOTES

    titles = {"global": "每日整理全局提示词", "diary": "日记提示词",
              "day_plan": "次日日程提示词", "memory": "记忆整理提示词"}
    items = tuple(PromptSection(key, titles[key], text, "每次每日整理" if key == "global" else "对应任务启用时",
        config_group=GROUP, field="template_" + key) for key, text in DEFAULTS.items())
    return (*items,
        PromptSection("diary_style", "日记表达偏好", "", "日记任务启用且偏好非空时",
                      config_group="role.life", field="diary_style_prompt"),
        PromptSection("output_contract", "输出 Schema", output_contract(), "每次每日整理"),
        PromptSection("wire_contract", "输出格式约束", "仅输出任务要求的 JSON 对象，不添加 Markdown 围栏。", "每次每日整理"),
        PromptSection("terminal_prompt_guard", "末尾约束", FINAL_PROMPT_GUARD + "\n"
                      + BACKGROUND_OUTPUT_NOTES["role_daily_review"], "每次每日整理"),
        PromptSection("automatic_context", "自动上下文来源", "\n".join((
            "AstrBot 当前人格、预设对话与会话历史，使用同一会话的长度管理。",
            "当前窗口的时间感知、角色状态、日程与生活记录、已启用记忆等共享聊天资料。",
            "daily_parts：目标日期、时区与任务开关；writing_materials/recent_journal：日记材料与近期日记。",
            "planning_material：次日范围与既有安排；记忆任务：来源窗口、当前记忆与 evidence_handles。")),
            "按当前角色、窗口和任务开关自动读取"))


def read(app, context):
    target = ConfigTarget(context.scope.installation_id, ConfigLayer.WINDOW,
                          context.role_binding.role, context.scope)
    snapshot = require(app.configuration.read(context.actor, target=target,
        group_id=GROUP, observed_at=app.now()))
    return {key: snapshot.values[GROUP + ".template_" + key] for key in DEFAULTS}


def assembly():
    return {"configuration_layers": ["role", "window"], "default_layer": "window", "order": [
        {"key": "role_instruction", "label": "系统消息 · 当前人格", "sections": []},
        {"key": "global", "label": "每日整理全局", "sections": ["global"]},
        {"key": "task_parts", "label": "已启用任务", "sections": ["diary", "diary_style", "day_plan", "memory"]},
        {"key": "schema", "label": "输出 Schema", "sections": ["output_contract"]},
        {"key": "task_prompt", "label": "模型任务提示", "sections": ["task_prompt"]},
        {"key": "output_contract", "label": "输出格式约束", "sections": ["wire_contract"]},
        {"key": "terminal_prompt_guard", "label": "系统消息末尾约束", "sections": ["terminal_prompt_guard"]},
        {"key": "history", "label": "预设对话与会话历史消息", "sections": []},
        {"key": "automatic_context", "label": "当轮资料消息", "sections": ["automatic_context"]}]}
