"""Validate all daily parts before committing any domain record."""

import json

from companion_v2.agenda.planning_inputs import validate as validate_plan
from companion_v2.agenda.planning_output import parse_output as parse_plan
from companion_v2.execution.models import TaskError, digest, encode, require, stamp
from companion_v2.memory.reflection_output import parse_output as parse_memory, validate_output as validate_memory
from companion_v2.role_work.journal.writing_output import draft_output, object_output
from .inputs import configuration, diary_job
from . import expression


def validate_inputs(service, context, inputs, *, committed=False):
    current = configuration(service.app, context)
    original = inputs["settings"]
    if any(current[key] != original[key] for key in ("timezone", "local_time", "parts", "diary_style")):
        raise TaskError("daily_settings_changed", "daily settings changed during generation")
    binding = service.app.state_maintenance.bindings.get(context.key)
    if binding is None or binding[:2] != (context, inputs["instruction"]):
        raise TaskError("daily_role_changed", "the original role was rebound")
    if committed:
        return
    if inputs.get("expression"):
        expression.validate(service.runtime, context, inputs["expression"])
    if inputs["plan"]:
        validate_plan(service.app.calendar_planning, inputs["plan"], service.tasks.now())
    if inputs["memory_batch"]:
        reflection = service.app.memory_reflection
        row = inputs["memory_snapshot"] if inputs.get("preview") else reflection._row(context, inputs["memory_batch"])
        reflection._validate_inputs(context, row)
    if inputs["diary"]:
        job = diary_job(service.app, context, inputs, ())
        service.app.journal_writer._check_job(job)
        if not inputs.get("preview"):
            service.app.journal_writer._target_current(job)
        service.app.journal_writer._inputs(job, service.tasks.now())


def parse(service, context, inputs, raw):
    value = object_output(raw)
    if not inputs["parts"].get("expression") and set(value) == {"diary", "day_plan", "memory"}:
        value["expression"] = None
    if set(value) != {"diary", "day_plan", "memory", "expression"}:
        raise TaskError("daily_output_invalid", "daily output needs exactly its four named parts")
    parsed = {"diary": None, "day_plan": None, "memory": None, "expression": None}
    for name, enabled in inputs["parts"].items():
        if not enabled:
            if value[name] is not None:
                raise TaskError("daily_disabled_part", "disabled daily parts must be null")
        elif type(value[name]) is not dict:
            raise TaskError("daily_output_invalid", "enabled daily parts need their original object")
    if inputs["parts"]["diary"]:
        body = dict(value["diary"])
        ids = body.pop("material_ids", None)
        if type(ids) is not list or any(type(item) is not str for item in ids):
            raise TaskError("daily_output_invalid", "diary must identify its selected original materials")
        job = diary_job(service.app, context, inputs, ids)
        service.app.journal_writer._check_job(job)
        service.app.journal_writer._inputs(job, service.tasks.now())
        if body.get("recall") is not None or body.get("afterglow") != "":
            raise TaskError("daily_output_invalid", "daily diary does not contain dream fields")
        parsed["diary"] = (job, draft_output(encode(body), job))
    if inputs["parts"]["day_plan"]:
        items, rejected = parse_plan(encode(value["day_plan"]), inputs["plan"])
        if rejected:
            raise TaskError("daily_plan_invalid", "a daily plan item failed its original validator")
        parsed["day_plan"] = items
    if inputs["parts"]["memory"]:
        reflection = service.app.memory_reflection
        row = inputs["memory_snapshot"] if inputs.get("preview") else reflection._row(context, inputs["memory_batch"])
        output = parse_memory({"text": encode(value["memory"]), "finish_reason": "stop"})
        result = validate_memory(reflection, context, json.loads(row["manifest_json"]), output)
        if result.get("rejected") or result.get("issues"):
            raise TaskError("daily_memory_invalid", "memory candidates failed their original evidence checks")
        parsed["memory"] = result
    if inputs["parts"].get("expression"):
        parsed["expression"] = expression.parse(value["expression"], inputs["expression"])
    return parsed


def commit(service, context, inputs, run_id, parsed, *, run=None, effect_started=False):
    app, runtime = service.app, service.runtime
    receipts = {"diary": None, "day_plan": None, "memory": None, "expression": None}
    with runtime.transaction() as connection:
        # The checkpoint predates this transaction; a rollback leaves it available
        # for local recovery without issuing another model request.
        if run is not None:
            run.validate()
            if not effect_started:
                run.begin_effect()
        validate_inputs(service, context, inputs)
        if parsed["diary"] is not None:
            job, draft = parsed["diary"]
            app.journal_writer._target_current(job)
            receipt = app.role_journal.save(draft, expected_revision=0, operation_id=run_id + ":diary")
            receipts["diary"] = {"operation_id": receipt.operation_id, "revision": receipt.head.revision}
        if parsed["day_plan"] is not None:
            plan = inputs["plan"]
            receipts["day_plan"] = []
            for index, (event, basis) in enumerate(parsed["day_plan"]):
                identifier = "planned-daily-" + digest([run_id, index])
                app.calendar.command(context, identifier=identifier, action="create", expected_revision=0,
                    operation_key=run_id + ":plan:" + str(index), data=event, sources=plan["sources"], planning=basis)
                receipts["day_plan"].append(identifier)
        if parsed["memory"] is not None:
            reflection, batch_id = app.memory_reflection, inputs["memory_batch"]
            row = reflection._row(context, batch_id)
            record = parsed["memory"]
            reflection.memory.writer._reserve(context, batch_id, digest([row["request_hash"], record]))
            connection.execute("UPDATE memory_reflections SET state='modelled', result_json=?, updated_at=? WHERE batch_id=?",
                (encode(record), stamp(service.tasks.now()), batch_id))
            result = require(reflection.apply(context, batch_id))
            if result["state"] != "complete":
                raise TaskError("daily_memory_commit_failed", "memory writes must finish in the daily transaction")
            receipts["memory"] = {"batch_id": batch_id, "state": result["state"]}
        if parsed.get("expression") is not None:
            receipts["expression"] = expression.commit(service, context, inputs["expression"],
                                                       parsed["expression"], run_id, connection)
        service.update(run_id, state="complete", receipt_json=encode(receipts), updated_at=stamp(service.tasks.now()))
        service.committed.add(run_id)
        if run is not None:
            run.record_operation(run_id)
    return receipts
