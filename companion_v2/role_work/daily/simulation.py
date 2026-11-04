"""Run the daily model request as a preview without committing domain outputs."""

from datetime import timedelta
from zoneinfo import ZoneInfo

from companion_v2.execution.models import TaskError, TaskResult, TaskSpec, digest, encode, require, stamp
from companion_v2.execution.runner import ExactTaskRunner
from companion_v2.foundation.refs import OperationRef, VersionRef
from companion_v2.role_work.journal.writing_output import object_output
from companion_v2.storage.models import OperationState
from companion_v2.storage.repositories import OperationRepository

from . import inputs as daily_inputs
from . import output
from . import expression
from .service import KIND


RESULT_PREFIX = "daily.simulation.v1:"


async def simulate_daily_review(app, context, instruction, *, attempt_id, expression_only=False):
    app.require_running()
    service = app.daily_review
    if not service.handles(context):
        raise TaskError("daily_context_unavailable", "daily preview needs its shared private context")
    if type(attempt_id) is not str or not attempt_id or len(attempt_id) > 128:
        raise ValueError("daily preview needs a bounded attempt identity")
    now = service.tasks.now()
    settings = daily_inputs.configuration(app, context)
    zone_name = settings["timezone"]
    local_date = now.astimezone(ZoneInfo(zone_name)).date().isoformat()
    run_id = "daily-simulation-" + digest([context.key, attempt_id])
    task_id = run_id + ":generate"
    expression_snapshot = await expression.capture(service, context, automatic=not expression_only)
    # Preview capture performs only synchronous work. Its temporary diary source,
    # memory selection and source index remain within this savepoint, with no
    # network await or model request while the transaction is held.
    with app.runtime.transaction() as connection:
        connection.execute("SAVEPOINT daily_simulation_capture")
        try:
            inputs = await daily_inputs.capture(service, context, instruction, local_date,
                                                zone_name, run_id, preview=True,
                                                expression_only=expression_only,
                                                expression_snapshot=expression_snapshot)
        finally:
            connection.execute("ROLLBACK TO SAVEPOINT daily_simulation_capture")
            connection.execute("RELEASE SAVEPOINT daily_simulation_capture")
    output.validate_inputs(service, context, inputs)
    base = {"simulation": True, "local_date": local_date, "timezone": zone_name,
            "parts_enabled": inputs["parts"], "attempt_id": attempt_id,
            "delivery_state": "not_requested", "receipt_ref": None}
    if not any(inputs["parts"].values()):
        return {**base, "state": "skipped", "result": {name: None for name in inputs["parts"]},
                "raw_response": "", "task_id": None, "call_id": None,
                "stages": [{"stage": "source_capture", "state": "passed"}]}
    result, completion_call = None, None

    async def generate(run):
        nonlocal result, completion_call
        request = service.request_for(context, inputs, run_id)
        completion = await service.gateway.generate(run, request)
        completion_call = completion.call_id
        captured = completion.captured_output
        text = captured.text if captured is not None else completion.draft.text if completion.draft else ""
        finish = captured.finish_reason if captured is not None else "stop" if completion.draft else "invalid"
        if finish not in (None, "stop", "end_turn"):
            raise TaskError("daily_output_incomplete", "daily preview requires a complete response")
        run.validate()
        result = {**base, "state": "passed", "result": None, "raw_response": text,
                  "task_id": task_id, "call_id": completion.call_id, "finish_reason": finish,
                  "generated_at": stamp(service.tasks.now()),
                  "stages": [{"stage": stage, "state": "passed"} for stage in
                             ("source_capture", "model_generation")]}
        try:
            parsed = output.parse(service, context, inputs, text)
            result["result"] = object_output(text)
            result["result"].setdefault("expression", None)
            rejected = list(getattr(parsed["expression"], "rejections", ()))
            if rejected:
                state = "partial" if parsed["expression"].signals else "failed"
                result.update(state=state, stage="output_validation", expression_validation={
                    "accepted_rules": len(parsed["expression"].signals), "rejected_rules": len(rejected),
                    "rejections": rejected, "cursor_advanced": False})
                rejected_indices = {item["index"] for item in rejected}
                section = result["result"]["expression"]
                section["signals"] = [item for index, item in enumerate(section["signals"], 1)
                                      if index not in rejected_indices]
                if state == "failed":
                    result["code"] = rejected[0]["code"]
                result["stages"].append({"stage": "output_validation", "state": state})
            else:
                result["stages"].append({"stage": "output_validation", "state": "passed"})
        except (TaskError, ValueError, TypeError, KeyError) as error:
            result.update(state="failed", stage="output_validation",
                          code=getattr(error, "code", "daily_output_invalid"), error=str(error))
            result["stages"].append({"stage": "output_validation", "state": "failed"})
            # Keep the actual returned body even when the domain contract rejects
            # it. This is an administrator-requested preview, never a domain save.
        key = RESULT_PREFIX + context.key + ":" + attempt_id
        operation_id = "daily-simulation-result-" + digest([context.key, attempt_id])
        operation = OperationRef(operation_id, "daily.simulation", context.scope,
                                 VersionRef(operation_id, 1), operation_id)
        operations = OperationRepository(app.runtime)
        with app.runtime.transaction() as connection:
            run.validate()
            run.begin_effect()
            require(operations.reserve(operation, parameter_hash=digest(result), observed_at=service.tasks.now()))
            connection.execute("INSERT INTO runtime_metadata(key,value) VALUES (?,?)", (key, encode(result)))
            stale = connection.execute("SELECT key FROM runtime_metadata WHERE key LIKE ? "
                "ORDER BY rowid DESC LIMIT -1 OFFSET 20", (RESULT_PREFIX + context.key + ":%",)).fetchall()
            for row in stale:
                connection.execute("DELETE FROM runtime_metadata WHERE key=?", (row["key"],))
            require(operations.transition(operation_id, expected_state=OperationState.ACCEPTED,
                next_state=OperationState.COMPLETED, observed_at=service.tasks.now()))
            run.record_operation(operation_id)
        return TaskResult("succeeded", effect="committed", result_ref=key,
                          receipt_ref=operation_id, operation_id=operation_id)

    runner = ExactTaskRunner(service.tasks, frozenset({KIND}), generate)
    service.pending[run_id] = (context, inputs)
    try:
        require(service.tasks.submit(TaskSpec(task_id, context, KIND, run_id, task_id,
                                             now, now + timedelta(seconds=600))))
        executed = require(await runner.run(context, task_id))
        if executed.stop_code:
            raise TaskError(executed.stop_code, "daily preview did not complete")
        if result is None:
            raise TaskError("daily_simulation_result_missing", "daily preview produced no result")
        return result
    except BaseException:
        if completion_call is not None:
            service.gateway.ledger.discard(completion_call)
        raise
    finally:
        await runner.close()
        service.pending.pop(run_id, None)
