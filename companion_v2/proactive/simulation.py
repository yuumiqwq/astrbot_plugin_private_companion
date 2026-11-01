"""Model-backed message previews, with no contact publication or delivery port."""

import asyncio
from dataclasses import replace
from datetime import timedelta
import json

from companion_v2.conversation.reply_preparation import observed_statements, trim_blank_edges
from companion_v2.conversation.reply_settings import ReplySettings
from companion_v2.conversation.text_processing import TEXT_DEFAULTS, strip_internal_expression_protocol, text_policy
from companion_v2.conversation.voice_reply import strip_voice_markup
from companion_v2.execution.models import TaskError, TaskResult, TaskSpec, digest, encode, require, stamp
from companion_v2.execution.runner import ExactTaskRunner
from companion_v2.foundation.refs import OperationRef, VersionRef
from companion_v2.model_gateway.shared_role_context import prepare_role_context
from companion_v2.storage.models import OperationState
from companion_v2.storage.repositories import OperationRepository

from .budget import DIAGNOSTIC_TASK_PREFIX
from .contact_models import ContactJob
from .planner import ProactivePlanner
from .prompt_catalog import presentation_instructions
from .prompts import request_for
from .service import CONTACT_KIND, _SEGMENTATION_SETTINGS_KEYS, _segmentation_policy, role_state_for_prompt
from .simple_prompts import parse_contact_output
from .official_alert_events import SOURCE_OWNER as ALERT_OWNER, attribution_suffix


RESULT_PREFIX = "proactive.simulation.v1:"


def _memory_preview(contacts, job, records):
    # Recall normally persists temporary query handles. Resolve the same plain
    # prompt materials, then discard those handles before any asynchronous call.
    # No memory facts, cursors or diagnostic query sessions survive this preview.
    with contacts.runtime.transaction() as db:
        db.execute("SAVEPOINT message_simulation_memory")
        try:
            return contacts._memory_materials(job, records)
        finally:
            db.execute("ROLLBACK TO SAVEPOINT message_simulation_memory")
            db.execute("RELEASE SAVEPOINT message_simulation_memory")


async def simulate_contact(app, context, candidate, *, attempt_id, diagnostic_image=False):
    """Use current model routing and parsing, retaining only a diagnostic result.

    The existing task validator keeps checking role, sources and configuration.
    An exact runner executes this local preview handler; it never invokes the
    contact worker which publishes contacts, applies feedback or queues sends.
    """
    contacts = app.proactive
    contacts.require_running()
    if type(attempt_id) is not str or not attempt_id or len(attempt_id) > 128:
        raise ValueError("simulation requires a bounded attempt identity")
    now = contacts.tasks.now()
    opportunity, intents = candidate["opportunity"], candidate["intents"]
    if opportunity.scope != context.scope or not opportunity.opened_at <= now < opportunity.expires_at:
        raise TaskError("opportunity_not_current", "simulation source opportunity is unavailable")
    preferences = require(contacts.preferences.read(context, observed_at=now))
    contacts._adopt(context, preferences)
    feasible, by_id = [], {}
    for intent in intents:
        preferences.require_contact(intent.purpose, ignore_time_restrictions=True, ignore_master_switch=True)
        contacts._records(context, intent.thought.evidence.sources, now)
        thought = replace(intent.thought, weight=intent.thought.weight * preferences.weight(intent.purpose))
        feasible.append(thought)
        by_id[thought.thought_id] = intent
    selected = require(ProactivePlanner().select(opportunity, tuple(feasible), observed_at=now,
                                                selection_seed=attempt_id))
    intent = by_id[selected.selected.thought_id]
    records = contacts._records(context, intent.thought.evidence.sources, now)
    task_id = DIAGNOSTIC_TASK_PREFIX + "simulation-" + digest([context.key, attempt_id])
    request_hash = digest([context.key, attempt_id, candidate, "simulation"])
    presentation = ReplySettings(contacts.configuration).capture(context, now)["settings"]
    deadline = min(opportunity.expires_at, now + timedelta(seconds=180))
    job = ContactJob(context, intent, candidate["role_instruction"], request_hash,
        preferences.snapshot.resolution_token, tuple(record.content_hash for record in records),
        selected.feasible_count, selected.weight_used,
        presentation_settings={key: presentation[key] for key in TEXT_DEFAULTS},
        segmentation_settings={key: presentation[key] for key in _SEGMENTATION_SETTINGS_KEYS if key in presentation},
        deadline_at=deadline, administrator_test=True,
        diagnostic_source_backed=(candidate.get("diagnostic_origin", {}).get("_diagnostic", {}).get("route_id") != "synthetic_event"),
        diagnostic_origin=candidate.get("diagnostic_origin"))
    result, completion_call = None, None

    async def generate(run):
        nonlocal result, completion_call
        current = contacts._policy(context, intent.purpose, contacts.tasks.now(),
            token=job.preference_token, administrator_test=True)
        source_records = contacts._records(context, intent.thought.evidence.sources, contacts.tasks.now(),
                                           hashes=job.source_hashes)
        # Render the production task, not the former force-send test prompt.
        render_job = replace(job, administrator_test=False,
            automatic_origin={key: value for key, value in (job.diagnostic_origin or {}).items()
                              if key not in ("_diagnostic", "synthetic_event")} or None)
        history, dialogue, last_reply = (), (), None
        if contacts.history is not None:
            snapshot = contacts.history.capture(context, now, action="proactive.contact",
                limit=current.values["generation_history_limit"], max_chars=current.values["history_max_chars"])
            history = contacts.history.read(snapshot, context, now, action="proactive.contact", round_metadata=True)
            last_reply = contacts.history.latest_reply(context, now,
                within_minutes=current.values["dedup_last_message_window_minutes"], action="proactive.contact")
            dialogue_snapshot = contacts.history.conversation.capture(context, now,
                limit=current.values["generation_history_limit"], max_chars=current.values["history_max_chars"])
            dialogue = contacts.history.conversation.read(dialogue_snapshot, context, now, round_metadata=True)
            render_job = replace(render_job, interaction=contacts.history.interaction(context, now,
                                                                                     action="proactive.contact"))
        policy = text_policy(job.presentation_settings or {})
        segmentation = _segmentation_policy(job.segmentation_settings)
        memory = _memory_preview(contacts, render_job, source_records)
        role_state = role_state_for_prompt(contacts._role_state_summary(context, now), intent.purpose)
        recent_life = contacts._recent_life_summary(context, now)
        request = request_for(render_job, source_records, current, history, last_reply=last_reply,
            dialogue=dialogue, memory=memory, role_state=role_state, recent_life=recent_life,
            rhythm_snapshot=contacts.budget.rhythm_snapshot(context, current, now),
            reactions_available=False, voice_available=False, poke_available=False)
        extra = presentation_instructions(current, segmentation, policy,
                                           news=intent.purpose == "news")
        if extra:
            request = replace(request, instruction=request.instruction + "\n" + extra)
        if contacts.request_context is not None:
            request = await prepare_role_context(contacts.request_context, request, context)
        completion = await contacts.gateway.generate(run, request)
        completion_call = completion.call_id
        if completion.draft is None:
            raise TaskError("simulation_output_missing", "the model did not return a message decision")
        alert_records = [record for record in source_records if record.reference.owner == ALERT_OWNER]
        alert = json.loads(alert_records[0].payload) if len(alert_records) == 1 else None
        try:
            decision, body, reaction = parse_contact_output(completion.draft.text,
                mode=request.proactive_mode, official_alert=alert)
        except (TypeError, ValueError, KeyError) as error:
            raise TaskError("simulation_output_invalid", "the model response does not match the active message schema") from error
        parts, reason = (), None
        if body is not None:
            body = strip_voice_markup(strip_internal_expression_protocol(body))
            if float(decision.get("decision_confidence", 1.0)) < current.values["silence_min_confidence"]:
                body, reason = None, "low_confidence"
        if body is not None:
            body, leaked = policy.framework_guard(body)
            statements = (body,) if alert else observed_statements(completion.draft)
            if leaked:
                body, reason = None, "framework_guard"
            else:
                body = policy.replace_text(body, statements=statements)
                host_parts = contacts.output_segments(context, body) if contacts.output_segments else None
                raw_parts = segmentation.parts(body) if host_parts is None else host_parts
                parts = tuple(trim_blank_edges(part) for part in policy.clean_parts(raw_parts, statements=statements)
                              if part.strip())
                if alert and parts:
                    parts = (*parts[:-1], parts[-1] + attribution_suffix(alert["attributions"]))
                if policy.forbidden_word_guard("".join(parts), scope_kind=context.scope.kind.value) is not None:
                    parts, reason = (), "forbidden_word"
        run.validate()
        result = {"simulation": True, "state": "passed" if parts else "skipped",
            "decision": "send" if parts else "skip", "parts": list(parts),
            "raw_response": completion.draft.text, "mode": request.proactive_mode,
            "task_id": task_id, "call_id": completion.call_id,
            "skip_reason": reason or (decision.get("skip_reason") if not parts else None),
            "reaction_intent": reaction, "diagnostic_image": bool(diagnostic_image),
            "generated_at": stamp(contacts.tasks.now()),
            "stages": [{"stage": stage, "state": "passed"} for stage in
                       ("model_generation", "output_parsing", "segmentation")]}
        key = RESULT_PREFIX + context.key + ":" + attempt_id
        operation_id = "simulation-result-" + digest([context.key, attempt_id])
        operation = OperationRef(operation_id, "proactive.simulation", context.scope,
                                 VersionRef(operation_id, 1), operation_id)
        operations = OperationRepository(app.runtime)
        with app.runtime.transaction() as db:
            run.begin_effect()
            require(operations.reserve(operation, parameter_hash=digest(result), observed_at=contacts.tasks.now()))
            db.execute("INSERT INTO runtime_metadata(key,value) VALUES (?,?)", (key, encode(result)))
            stale = db.execute("SELECT key FROM runtime_metadata WHERE key LIKE ? ORDER BY rowid DESC LIMIT -1 OFFSET 20",
                               (RESULT_PREFIX + context.key + ":%",)).fetchall()
            for row in stale:
                db.execute("DELETE FROM runtime_metadata WHERE key=?", (row["key"],))
            require(operations.transition(operation_id, expected_state=OperationState.ACCEPTED,
                next_state=OperationState.COMPLETED, observed_at=contacts.tasks.now()))
            run.record_operation(operation_id)
        return TaskResult("succeeded", effect="committed", result_ref=key,
                          operation_id=operation_id, receipt_ref=operation_id)

    runner = ExactTaskRunner(contacts.tasks, frozenset({CONTACT_KIND}), generate)
    invocation = asyncio.current_task()
    contacts._pending[task_id] = job
    contacts._invocations.add(invocation)
    try:
        require(contacts.tasks.submit(TaskSpec(task_id, context, CONTACT_KIND, request_hash, task_id,
                                             opportunity.opened_at, deadline)))
        executed = require(await runner.run(context, task_id))
        if executed.stop_code:
            raise TaskError(executed.stop_code, "message simulation did not complete")
        if result is None:
            raise TaskError("simulation_result_missing", "message simulation produced no result")
        return result
    except BaseException:
        if completion_call is not None:
            contacts.gateway.ledger.discard(completion_call)
        raise
    finally:
        await runner.close()
        contacts._pending.pop(task_id, None)
        contacts._invocations.discard(invocation)
