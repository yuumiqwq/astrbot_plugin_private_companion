"""Durable daily diary, next-day planning and evidence-based memory in one call."""

import asyncio
from datetime import date, datetime, time, timedelta, timezone
import json
import logging
from zoneinfo import ZoneInfo

from companion_v2.conversation.foreground import BackgroundPromptTrigger, PromptMaterial, PromptRequest, SealedForegroundContext
from companion_v2.execution.models import (Lane, ProviderBinding, SideEffect, TaskContext, TaskDefinition,
    TaskError, TaskRegistration, TaskResult, TaskSpec, TERMINAL, digest, encode, require, stamp)
from companion_v2.execution.runner import ExactTaskRunner
from companion_v2.execution.service import outcome
from companion_v2.model_gateway.models import ModelTaskProfile
from . import inputs as daily_inputs
from . import output
from . import prompts

logger = logging.getLogger(__name__)
KIND = "role.daily_review"
OWNER = "role.daily"


def daily_profile(provider, *, max_output_tokens=2048):
    return ModelTaskProfile(KIND, Lane.BACKGROUND, provider,
        "沿用当前人格和对话，完成本次每日整理；只返回约定的三个结果。",
        max_output_tokens=max_output_tokens, prompt_sections=prompts.sections())


class RoleDailyReview:
    def __init__(self, runtime):
        self.runtime = runtime
        self.workers, self.results, self.pending, self.locks = {}, {}, {}, {}
        self.next_checks = {}
        self.committed = set()
        self.closed = False
        self.registrations = (TaskRegistration(TaskDefinition(KIND, OWNER, "conversation.reply", Lane.BACKGROUND,
            side_effect=SideEffect.LOCAL, binding=ProviderBinding(OWNER, "local", "1", "daily-1"),
            timeout_seconds=600, max_attempts=1, concurrency=1), self.validate),)

    def attach(self, app):
        self.app, self.tasks, self.gateway = app, app.tasks, app.gateway
        self.runner = ExactTaskRunner(self.tasks, frozenset({KIND}), self.generate, waiting=app.background_waiting)
        app.journal_schedule.delegated = self.handles
        app.memory_schedule.delegated = self.handles

    def handles(self, context):
        hook = getattr(self.gateway, "request_context", None)
        supports = getattr(hook, "supports", None)
        return (not self.closed and KIND in self.gateway.router.profiles
                and callable(supports) and supports(context.scope))

    async def yield_preparation(self):
        self.app.require_running()
        await asyncio.sleep(0)

    def identifier(self, context, local_date, zone_name):
        return "role-daily-" + digest([self.app.calendar.records.role_key(context), local_date, zone_name])

    def _row(self, run_id):
        return self.runtime.read_one("SELECT * FROM role_daily_runs WHERE run_id=?", (run_id,))

    def status(self, run_id):
        row = self._row(run_id)
        return {"run_id": run_id, "state": row["state"], "date": row["local_date"],
                "timezone": row["timezone"], "task_id": row["task_id"],
                "receipts": json.loads(row["receipt_json"]) if row["receipt_json"] else None}

    async def run(self, context, instruction, *, local_date=None, zone_name=None):
        self.app.require_running()
        if not self.handles(context):
            raise TaskError("daily_context_unavailable", "daily review needs its configured shared private context")
        settings = daily_inputs.configuration(self.app, context)
        zone_name = zone_name or settings["timezone"]
        local_date = local_date or self.tasks.now().astimezone(ZoneInfo(zone_name)).date().isoformat()
        run_id = self.identifier(context, local_date, zone_name)
        lock = self.locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            row = self._row(run_id)
            if row is None:
                if not any(settings["parts"].values()):
                    return {"state": "disabled", "date": local_date, "run_id": run_id}
                binding = self.app.state_maintenance.bindings.get(context.key)
                if binding is None or binding[:2] != (context, instruction):
                    raise TaskError("daily_role_changed", "daily preparation needs its original binding")
                await self.app.calendar_planning.guard(context, instruction)
                captured = await daily_inputs.capture(self, context, instruction, local_date, zone_name, run_id)
                now = stamp(self.tasks.now())
                with self.runtime.transaction() as connection:
                    connection.execute("INSERT OR IGNORE INTO role_daily_runs VALUES (?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,?)",
                        (run_id, self.app.calendar.records.role_key(context), context.key, local_date, zone_name,
                         "prepared", encode(context), encode(captured), now, now))
                row = self._row(run_id)
            original = TaskContext.restore(json.loads(row["context_json"]))
            if original.key != context.key:
                return self.status(run_id)
            if row["state"] in ("complete", "failed", "unknown"):
                return self.status(run_id)
            if row["state"] == "received" and row["task_id"]:
                task = self.runtime.read_one("SELECT state FROM execution_tasks WHERE task_id=?", (row["task_id"],))
                held = self.runtime.read_one("SELECT 1 FROM execution_attempts WHERE task_id=? AND slot_held=1", (row["task_id"],))
                if held or task is not None and task["state"] not in TERMINAL:
                    return self.status(run_id)
            if row["state"] == "running":
                task = self.runtime.read_one("SELECT state FROM execution_tasks WHERE task_id=?", (row["task_id"],))
                held = self.runtime.read_one("SELECT 1 FROM execution_attempts WHERE task_id=? AND slot_held=1", (row["task_id"],))
                if held or task is not None and task["state"] not in TERMINAL:
                    return self.status(run_id)
                # An interrupted request has no replay permission. Durable output
                # would have advanced the row to received before any domain save.
                with self.runtime.transaction() as connection:
                    connection.execute("UPDATE role_daily_runs SET state='unknown',updated_at=? WHERE run_id=?",
                                       (stamp(self.tasks.now()), run_id))
                return self.status(run_id)
            inputs = daily_inputs.restore(context, row["inputs_json"])
            self.pending[run_id] = (context, inputs)
            try:
                if not any(inputs["parts"].values()):
                    with self.runtime.transaction() as connection:
                        connection.execute("UPDATE role_daily_runs SET state='complete',receipt_json=?,updated_at=? WHERE run_id=?",
                            (encode({"empty": True}), stamp(self.tasks.now()), run_id))
                    return self.status(run_id)
                if row["state"] == "received":
                    # No model is involved in recovery. A new task lease fences
                    # an old process while preserving the saved request/output.
                    count = self.runtime.read_one("SELECT COUNT(*) n FROM execution_tasks WHERE kind=? AND json_extract(record_json,'$.spec.request_ref')=?", (KIND, run_id))["n"]
                    suffix = ":recover:" + str(count)
                else:
                    suffix = ":generate"
                task_id, now = run_id + suffix, self.tasks.now()
                prior = self.runtime.read_one("SELECT state FROM execution_tasks WHERE task_id=?", (task_id,))
                if prior and prior["state"] in TERMINAL:
                    with self.runtime.transaction() as connection:
                        connection.execute("UPDATE role_daily_runs SET state='failed',receipt_json=?,updated_at=? WHERE run_id=?",
                            (encode({"code": "daily_preparation_interrupted"}), stamp(now), run_id))
                    return self.status(run_id)
                if prior is None:
                    spec = TaskSpec(task_id, context, KIND, run_id, task_id, now, now + timedelta(seconds=600))
                    require(self.tasks.submit(spec))
                with self.runtime.transaction() as connection:
                    connection.execute("UPDATE role_daily_runs SET task_id=?,updated_at=? WHERE run_id=?",
                                       (task_id, stamp(now), run_id))
                result = require(await self.runner.run(context, task_id))
                if result.stop_code:
                    current = self._row(run_id)
                    permanent = result.stop_code in {"planning_window_expired", "planning_settings_changed",
                        "planning_role_changed", "planning_calendar_changed", "daily_settings_changed", "daily_role_changed",
                        "memory_settings_changed", "memory_reflection_stale", "journal_revision_conflict", "journal_deleted"}
                    if current["state"] not in ("received", "complete", "failed") or current["state"] == "received" and permanent:
                        dispatched = self.runtime.read_one("SELECT 1 FROM model_calls WHERE task_id=? AND state IN ('dispatched','unknown')", (task_id,))
                        state = "unknown" if dispatched and not permanent else "failed"
                        with self.runtime.transaction() as connection:
                            connection.execute("UPDATE role_daily_runs SET state=?,receipt_json=?,updated_at=? WHERE run_id=?",
                                (state, encode({"code": result.stop_code}), stamp(self.tasks.now()), run_id))
                return self.status(run_id)
            except TaskError as error:
                with self.runtime.transaction() as connection:
                    connection.execute("UPDATE role_daily_runs SET state='failed',receipt_json=?,updated_at=? WHERE run_id=? AND state!='complete'",
                        (encode({"code": error.code}), stamp(self.tasks.now()), run_id))
                return self.status(run_id)
            finally:
                self.pending.pop(run_id, None)
                self.committed.discard(run_id)
                self.next_checks[run_id] = self.tasks.now() + timedelta(minutes=1)

    @outcome
    def validate(self, spec, at):
        self.app.require_running()
        value = self.pending.get(spec.request_ref)
        if value is None or value[0] != spec.context:
            raise TaskError("daily_request_unavailable", "daily request is not held by its original worker")
        output.validate_inputs(self, *value, committed=spec.request_ref in self.committed)
        return True

    def request_for(self, context, inputs, run_id):
        event = BackgroundPromptTrigger(run_id, context.actor.principal, context.scope, self.tasks.now())
        task = (inputs.get("prompts", prompts.DEFAULTS)["global"].rstrip() + "\n"
            + "\n\n".join(inputs["instructions"])
            + "\n" + prompts.output_contract())
        materials = (PromptMaterial(OWNER, "daily_parts", encode({"local_date": inputs["local_date"],
            "timezone": inputs["timezone"], "enabled": inputs["parts"]})), *inputs["materials"])
        return PromptRequest(event, inputs["instruction"], SealedForegroundContext(tuple(materials), (), ()),
            task, context.role_binding, output_format="role_daily_review", response_format="json_object",
            response_schema_json=encode(daily_inputs.DAILY_SCHEMA))

    async def generate(self, run):
        run_id = run.task.spec.request_ref
        context, inputs = self.pending[run_id]
        row = self._row(run_id)
        if row["state"] == "received":
            raw = json.loads(row["output_json"])
        else:
            request = self.request_for(context, inputs, run_id)
            with self.runtime.transaction() as connection:
                run.validate()
                connection.execute("UPDATE role_daily_runs SET state='running',updated_at=? WHERE run_id=?",
                                   (stamp(self.tasks.now()), run_id))
            completion = await self.gateway.generate(run, request)
            captured = completion.captured_output
            text = captured.text if captured is not None else completion.draft.text if completion.draft else ""
            finish = captured.finish_reason if captured is not None else "stop" if completion.draft else "invalid"
            raw = {"text": text, "finish_reason": finish, "call_id": completion.call_id}
            with self.runtime.transaction() as connection:
                run.validate()
                run.begin_effect()
                connection.execute("UPDATE role_daily_runs SET state='received',output_json=?,updated_at=? WHERE run_id=?",
                                   (encode(raw), stamp(self.tasks.now()), run_id))
                run.record_operation(run_id)
        if raw["finish_reason"] not in (None, "stop", "end_turn"):
            raise TaskError("daily_output_incomplete", "daily output must be complete before any domain write")
        try:
            parsed = output.parse(self, context, inputs, raw["text"])
        except (TaskError, ValueError, TypeError, KeyError) as error:
            with self.runtime.transaction() as connection:
                connection.execute("UPDATE role_daily_runs SET state='failed',receipt_json=?,updated_at=? WHERE run_id=?",
                    (encode({"code": getattr(error, "code", "daily_output_invalid")}), stamp(self.tasks.now()), run_id))
            raise
        output.commit(self, context, inputs, run_id, parsed, run=run, effect_started=row["state"] != "received")
        self.app.notify()
        return TaskResult("succeeded", effect="committed", result_ref=run_id, receipt_ref=run_id, operation_id=run_id)

    def _bindings(self):
        for bindings in self.app.calendar_planning._role_bindings().values():
            selected = [item for item in bindings if self.handles(item[0])]
            if selected:
                yield self.app.calendar_planning._role_anchor(selected)

    def advance(self):
        if self.closed:
            return
        for key, worker in tuple(self.workers.items()):
            if worker.done():
                self.workers.pop(key)
                if not worker.cancelled():
                    error = worker.exception()
                    self.results[key] = ({"state": "failed", "code": getattr(error, "code", type(error).__name__)}
                                         if error else worker.result())
        for context, instruction, _ in self._bindings():
            if len(self.workers) >= self.tasks.limits.background_slots:
                break
            settings = daily_inputs.configuration(self.app, context)
            if not any(settings["parts"].values()):
                continue
            zone = ZoneInfo(settings["timezone"])
            local = self.tasks.now().astimezone(zone)
            due = datetime.combine(local.date(), time.fromisoformat(settings["local_time"]), zone)
            recovery = self.runtime.read_one("SELECT local_date,timezone FROM role_daily_runs WHERE origin_key=? AND state IN ('prepared','running','received') ORDER BY created_at LIMIT 1", (context.key,))
            if recovery:
                day, zone_name = recovery["local_date"], recovery["timezone"]
            elif local >= due:
                day, zone_name = local.date().isoformat(), settings["timezone"]
            else:
                continue
            run_id = self.identifier(context, day, zone_name)
            if run_id in self.workers:
                continue
            if run_id in self.locks and self.locks[run_id].locked():
                continue
            if self.next_checks.get(run_id, self.tasks.now()) > self.tasks.now():
                continue
            row = self._row(run_id)
            if row and row["state"] in ("complete", "failed", "unknown"):
                continue
            worker = asyncio.create_task(self.run(context, instruction, local_date=day, zone_name=zone_name),
                                         name="companion:daily:" + run_id)
            self.workers[run_id] = worker
            self.next_checks[run_id] = self.tasks.now() + timedelta(minutes=1)
            worker.add_done_callback(lambda done: self.app.notify())

    def next_wake(self):
        if self.closed:
            return None
        dates = []
        for context, _, _ in self._bindings():
            settings = daily_inputs.configuration(self.app, context)
            if not any(settings["parts"].values()):
                continue
            zone = ZoneInfo(settings["timezone"])
            local = self.tasks.now().astimezone(zone)
            due = datetime.combine(local.date(), time.fromisoformat(settings["local_time"]), zone)
            if due <= local:
                due += timedelta(days=1)
            dates.append(due.astimezone(timezone.utc))
        return min(dates) if dates else None

    @property
    def active_handles(self):
        return self.runner.active_handles + tuple(self.workers)

    def cancel_running(self):
        for worker in self.workers.values():
            worker.cancel()

    async def close(self):
        self.closed = True
        self.cancel_running()
        await self.runner.close()
        if self.workers:
            await asyncio.wait(tuple(self.workers.values()), timeout=0.25)
        for worker in self.workers.values():
            if worker.done() and not worker.cancelled():
                worker.exception()
        self.workers.clear()
