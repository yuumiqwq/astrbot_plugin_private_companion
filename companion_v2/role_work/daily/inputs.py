"""Capture and restore the original domain inputs without another model call."""

from datetime import date, datetime, time, timedelta
import json
from zoneinfo import ZoneInfo

from companion_v2.agenda.planning_config import AUTOMATIC_SCHEDULE_MODES, planning_values
from companion_v2.agenda.planning_inputs import capture as capture_plan
from companion_v2.conversation.foreground import PromptMaterial
from companion_v2.conversation.models import InboundConversationEvent
from companion_v2.execution.models import TaskContext, TaskError, digest, encode, require
from companion_v2.foundation.refs import SourceRef, VersionRef
from companion_v2.role_work.life.configuration import dream_settings
from companion_v2.role_work.journal import codec
from companion_v2.role_work.journal.collection import JournalDayMaterials
from companion_v2.role_work.journal.materials import JournalMaterial, MaterialKind, material_document
from companion_v2.role_work.journal.models import JournalKind, JournalState
from companion_v2.role_work.journal.prompts import entry_document
from companion_v2.role_work.journal.writing import _CollectedJob
from companion_v2.memory.reflection_output import REFLECTION_SCHEMA
from companion_v2.sources.models import SourceRecord
from . import prompts
from . import expression


def configuration(app, context):
    life = dream_settings(app.runtime, context)
    state = planning_values(app.role_state.settings(context)[0])
    memory, memory_token = app.memory.settings.read(context)
    return {"timezone": life["timezone"], "local_time": life["diary_time"],
        "parts": {"diary": life["enabled"] and life["diary_enabled"],
                  "day_plan": state["calendar_enabled"] and state["planning_enabled"]
                      and state["schedule_simulation_mode"] in AUTOMATIC_SCHEDULE_MODES,
                  "memory": memory["memory.enabled"] and memory["memory.reflection_enabled"],
                  "expression": expression.enabled(app, context)},
        "diary_style": life["diary_style_prompt"], "memory_token": memory_token}


def event_for(app, context, run_id, at):
    text = "角色每日整理任务：记录当天日记，安排次日活动，并检查当天可保留的记忆。"
    source = SourceRef(run_id, "role.daily", context.scope, VersionRef(run_id, 1), at)
    require(app.role_journal.sources.publish(SourceRecord(source, context.audience, digest(text), text),
                                           observed_at=app.now()))
    return InboundConversationEvent(run_id, context.actor.principal, context.scope,
                                    source, "role.journal.write", digest(text), at)


async def capture(service, context, instruction, local_date, zone_name, run_id, *, preview=False,
                  expression_only=False, expression_snapshot=None):
    app, at = service.app, service.tasks.now()
    settings = configuration(app, context)
    prompt_values = prompts.read(app, context)
    if settings["timezone"] != zone_name:
        raise TaskError("daily_settings_changed", "daily timezone changed before preparation")
    selected = dict(settings["parts"])
    if expression_only:
        selected = {name: name == "expression" and expression.enabled(app, context, automatic=False)
                    for name in selected}
    if preview and not expression_only:
        # A preview can inspect tomorrow while automatic planning is paused.
        # The calendar's module switch and all access checks still apply.
        selected["day_plan"] = planning_values(app.role_state.settings(context)[0])["calendar_enabled"]
    materials, instructions = [], []
    day, zone = date.fromisoformat(local_date), ZoneInfo(zone_name)
    if day != at.astimezone(zone).date():
        raise TaskError("daily_date_invalid", "daily preparation uses the current local date")
    inputs = {"settings": settings, "instruction": instruction, "parts": selected,
              "local_date": local_date, "timezone": zone_name, "at": at,
              "diary": None, "plan": None, "memory_batch": None, "preview": preview,
              "prompts": prompt_values, "expression": None}
    if selected.get("expression"):
        snapshot = expression_snapshot
        if snapshot is None and not preview:
            snapshot = await expression.capture(service, context, automatic=not expression_only)
        if snapshot is None:
            selected["expression"] = False
        else:
            inputs["expression"] = snapshot
            materials.append(PromptMaterial("role.daily", "expression_comparison", encode(snapshot)))
            instructions.append("表达对照部分：\n" + prompt_values["expression"])
    if selected["diary"]:
        target = app.journal_triggers.target(context, JournalKind.DIARY, local_date, zone_name)
        head = app.role_journal.head(target.key)
        if not preview and (head.revision or head.state is JournalState.DELETED):
            selected["diary"] = False
        else:
            event = event_for(app, context, run_id, at)
            candidates = app.journal_material_collector.for_day(context, target)
            recent = app.journal_writer._recent(target)
            inputs["diary"] = {"event": event, "candidates": candidates, "recent": recent}
            materials.extend((PromptMaterial("role.journal", "writing_date", encode({
                "date": local_date, "timezone": zone_name, "kind": "diary"})),
                PromptMaterial("role.journal", "writing_materials", encode([
                    material_document(item) for item in candidates.materials]))))
            for reference in recent:
                entry = app.role_journal.get(reference.key, observed_at=at)
                materials.append(PromptMaterial("role.journal", "recent_journal", encode(entry_document(entry))))
            instructions.append("日记部分：\n" + prompt_values["diary"]
                + ("\n表达偏好：" + settings["diary_style"] if settings["diary_style"] else ""))
    if selected["day_plan"]:
        tomorrow = day + timedelta(days=1)
        plan = capture_plan(app.calendar_planning, context, instruction, "daily",
            "daily:" + zone_name + ":" + tomorrow.isoformat(), planning_date=tomorrow, manual=preview)
        inputs["plan"] = plan
        materials.append(PromptMaterial("calendar.planning", "planning_material", encode(plan["material"])))
        instructions.append("次日日程部分：\n" + prompt_values["day_plan"])
    if selected["memory"]:
        reflection = app.memory_reflection
        start = datetime.combine(day, time(), zone)
        previous = app.runtime.read_one("""SELECT inputs_json FROM role_daily_runs
            WHERE origin_key=? AND state='complete' AND json_extract(inputs_json,'$.memory_batch') IS NOT NULL
            ORDER BY created_at DESC LIMIT 1""", (context.key,))
        if previous is not None:
            prior_inputs = json.loads(previous["inputs_json"])
            prior = reflection._row(context, prior_inputs["memory_batch"])
            manifest = json.loads(prior["manifest_json"])
            # The ten minutes after 23:50 belong to the next review. A bounded
            # evidence selection also carries any unprocessed span forward.
            partial = any(manifest.get(key) for key in
                          ("remaining_sources", "unavailable_sources", "deferred_sources"))
            start = datetime.fromisoformat(manifest["start"] if partial else manifest["end"])
        inputs["memory_start"] = start.isoformat()
        prepared = require(reflection.prepare(context, request_id=run_id, start=start, end=at,
            role_instruction=instruction, maximum_sources=64, maximum_chars=48000))
        batch_id = prepared["batch_id"]
        for _ in range(512):
            row = reflection._row(context, batch_id)
            if row["state"] != "selecting":
                break
            reflection.selection.advance(context, row)
            if not preview:
                await service.yield_preparation()
        else:
            raise TaskError("daily_memory_selection_pending", "source index needs more preparation")
        if row["state"] not in ("prepared", "empty"):
            raise TaskError("daily_memory_unavailable", "the original memory window is already in use")
        inputs["memory_batch"] = batch_id
        request = reflection._request(context, row, json.loads(row["manifest_json"]))
        inputs["memory_snapshot"] = dict(reflection._row(context, batch_id))
        materials.extend(request.context.materials)
        instructions.append("记忆整理部分：\n" + prompt_values["memory"])
    inputs["materials"] = tuple(materials)
    inputs["instructions"] = tuple(instructions)
    return inputs


def restore(context, raw):
    data = json.loads(raw) if isinstance(raw, str) else dict(raw)
    data["at"] = datetime.fromisoformat(data["at"])
    if data["diary"]:
        diary = data["diary"]
        original = diary["event"]
        diary["event"] = InboundConversationEvent(original["event_id"], context.actor.principal, context.scope,
            codec.source(original["source"]), original["capability"], original["input_hash"],
            datetime.fromisoformat(original["received_at"]))
        values = diary["candidates"]
        items = tuple(JournalMaterial(**{**item, "kind": MaterialKind(item["kind"]),
            "source": codec.source(item["source"]) if item["source"] else None,
            "activity": VersionRef(**item["activity"]) if item["activity"] else None}) for item in values["materials"])
        diary["candidates"] = JournalDayMaterials(codec.context(values["context"]), values["task_context_key"],
            datetime.fromisoformat(values["collected_at"]), items, tuple(tuple(pair) for pair in values["page_counts"]))
        diary["recent"] = tuple(codec.reference(ref) for ref in diary["recent"])
    if data["plan"]:
        plan = data["plan"]
        plan["context"] = TaskContext.restore(plan["context"])
        for field in ("at", "start", "end"):
            plan[field] = datetime.fromisoformat(plan[field])
        plan["sources"] = tuple(codec.source(source) for source in plan["sources"])
        plan["existing"] = [tuple(item) for item in plan["existing"]]
        plan["intervention_dialogue"] = tuple(plan["intervention_dialogue"])
    data["materials"] = tuple(PromptMaterial(**{**item,
        "source": codec.source(item["source"]) if item["source"] else None}) for item in data["materials"])
    return data


def diary_job(app, context, inputs, material_ids):
    diary = inputs["diary"]
    selected = diary["candidates"].select(tuple(material_ids)).materials
    trigger_sources = () if inputs.get("preview") else (diary["event"].source,)
    sources = tuple(dict.fromkeys(trigger_sources + tuple(m.source for m in selected if m.source)))
    activities = tuple(dict.fromkeys(m.activity for m in selected if m.activity))
    return _CollectedJob(context, diary["event"], diary["candidates"].context, inputs["instruction"],
                         0, selected, sources, activities, diary["recent"])


DAILY_SCHEMA = {"type": "object", "required": ["diary", "day_plan", "memory", "expression"], "additionalProperties": False,
    "properties": {
        "diary": {"anyOf": [{"type": "null"}, {"type": "object", "additionalProperties": False,
            "required": ["body", "form", "recall", "afterglow", "material_ids"], "properties": {
                "body": {"type": "string"}, "form": {"type": "string"}, "recall": {"type": "null"},
                "afterglow": {"type": "string", "const": ""},
                "material_ids": {"type": "array", "maxItems": 64, "uniqueItems": True,
                                 "items": {"type": "string"}}}}]},
        "day_plan": {"anyOf": [{"type": "null"}, {"type": "object", "additionalProperties": False,
            "required": ["items"], "properties": {"items": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["title", "start", "minutes", "detail"],
                "properties": {"title": {"type": "string"}, "start": {"type": "string"},
                    "minutes": {"type": "integer", "minimum": 1}, "detail": {"type": "string"},
                    "goal_id": {"type": ["string", "null"]}, "parent_id": {"type": ["string", "null"]}}}}}}]},
        "memory": {"anyOf": [{"type": "null"}, REFLECTION_SCHEMA]},
        "expression": {"anyOf": [{"type": "null"}, expression.learning.schema(3)]}}}
