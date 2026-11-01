"""The daily batch uses real domain validators and a synthetic text provider."""

import asyncio
from datetime import datetime, timedelta
import itertools
import json
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from companion_v2.configuration.registry import FieldRegistry
from companion_v2.execution.models import encode
from companion_v2.memory.management import MemoryManagement
from companion_v2.model_gateway.routing import configuration_fields
from companion_v2.adapters.astrbot.native import NativeConversationInput
from companion_v2.conversation.native_observations import NativeConversationObservations
from companion_v2.identity_policy.personas import ConversationRoute
from companion_v2.role_work.daily.service import KIND, daily_profile
from companion_v2.role_work.journal.models import JournalKind, JournalState
from companion_v2.role_work.life.management import RoleLifeManagement
from tests.test_role_state import StateFixture
from tests.test_role_tool_loop import answered
from tests.test_management_access import NOW, SCOPE


class SharedContext:
    def supports(self, scope):
        return True

    async def __call__(self, request):
        return request


class DailyReviewTests(StateFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.profiles += (daily_profile("primary", max_output_tokens=100),)
        self.registry = FieldRegistry(version="daily.tests", fields=configuration_fields(self.profiles))

    async def ready(self, *, diary=True, plan=True, memory=True):
        host, app, context, manager = self.setup_state()
        self.grant(self.important, SCOPE, "role.journal.write")
        self.configure(manager, calendar_enabled=True, planning_enabled=plan)
        await self.start(host)
        app.gateway.request_context = SharedContext()
        app.state_maintenance.bind(context, "原角色人格")
        app.life_runtime.bind(context, "原角色人格")
        self.app, self.task_context, self.state_manager = app, context, manager
        self.life_manager = RoleLifeManagement(app, context)
        await self.switches(diary=diary, plan=plan, memory=memory)
        self.model_behavior = self.reply
        return app, context

    async def switches(self, *, diary, plan, memory):
        self.configure(self.state_manager, calendar_enabled=True, planning_enabled=plan)
        before = self.life_manager.status(self.core)
        self.counter += 1
        (await self.life_manager.command(self.core, dict(action="configure", operation_key="life-" + str(self.counter),
            view_token=before["view_token"], edit_token=before["edit_token"],
            changes={"enabled": True, "diary_enabled": diary, "sleep_enabled": False,
                     "reading_enabled": False, "diary_time": "23:50", "timezone": "Asia/Shanghai"}))).unwrap()
        memory_manager = MemoryManagement(self.app.memory, self.core)
        settings = memory_manager.settings(self.task_context).unwrap()
        memory_manager.patch_settings(self.task_context, operation_key="memory-" + str(self.counter),
            expected_edit_token=settings["edit_token"], changes={"memory.enabled": True,
                "memory.reflection_enabled": memory}).unwrap()

    async def reply(self, envelope):
        if envelope.original.output_format != "role_daily_review":
            return answered("知道了。")
        materials = envelope.original.context.materials
        info = json.loads(next(item.payload for item in materials if item.purpose == "daily_parts"))
        enabled = info["enabled"]
        result = {name: None for name in enabled}
        if enabled["diary"]:
            candidates = json.loads(next(item.payload for item in materials if item.purpose == "writing_materials"))
            result["diary"] = {"body": "今天安静地记下了一点感受。", "form": "随记", "recall": None,
                               "afterglow": "", "material_ids": [item["id"] for item in candidates[:64]]}
        if enabled["day_plan"]:
            plan = json.loads(next(item.payload for item in materials if item.purpose == "planning_material"))
            start = datetime.fromisoformat(plan["start"]) + timedelta(hours=10)
            result["day_plan"] = {"items": [{"title": "读书", "start": start.isoformat(), "minutes": 30,
                                             "detail": "明天继续读几页。"}]}
        if enabled["memory"]:
            result["memory"] = {"summary": "没有新增的长期事实。", "proposals": [], "pending_decisions": [], "anniversaries": []}
        return answered(encode(result))

    async def run_daily(self):
        return await self.app.daily_review.run(self.task_context, "原角色人格")

    def daily_calls(self):
        return [call for call in self.called if call.original.output_format == "role_daily_review"]

    def target(self):
        day = self.clock.now().astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        return self.app.journal_triggers.target(self.task_context, JournalKind.DIARY, day, "Asia/Shanghai")

    async def test_one_model_call_commits_three_parts_and_repeat_is_idempotent(self):
        app, context = await self.ready()
        result = await self.run_daily()
        self.assertEqual(result["state"], "complete", result)
        self.assertEqual(len(self.daily_calls()), 1)
        self.assertEqual(app.role_journal.head(self.target().key).state, JournalState.ACTIVE)
        self.assertEqual(len(result["receipts"]["day_plan"]), 1)
        self.assertEqual(result["receipts"]["memory"]["state"], "complete")
        self.assertEqual((await self.run_daily())["receipts"], result["receipts"])
        self.assertEqual(len(self.daily_calls()), 1)
        self.assertEqual(self.sent, [])

    async def test_each_switch_combination_controls_the_same_call(self):
        await self.ready(diary=False, plan=False, memory=False)
        for index, (diary, plan, memory) in enumerate(itertools.product((False, True), repeat=3)):
            self.clock.advance(timedelta(days=1))
            await self.switches(diary=diary, plan=plan, memory=memory)
            before = len(self.daily_calls())
            result = await self.run_daily()
            self.assertEqual(result["state"], "complete" if any((diary, plan, memory)) else "disabled", result)
            self.assertEqual(len(self.daily_calls()) - before, int(any((diary, plan, memory))))
            if result["state"] == "complete":
                self.assertEqual(result["receipts"]["diary"] is not None, diary)
                self.assertEqual(result["receipts"]["day_plan"] is not None, plan)
                self.assertEqual(result["receipts"]["memory"] is not None, memory)

    async def test_failed_domain_save_rolls_back_every_part_and_recovers_saved_output(self):
        app, context = await self.ready()
        original = app.calendar.command
        with patch.object(app.calendar, "command", side_effect=RuntimeError("synthetic storage failure")):
            result = await self.run_daily()
        self.assertEqual(result["state"], "received", result)
        self.assertEqual(app.role_journal.head(self.target().key).state, JournalState.ABSENT)
        self.assertEqual(len(self.daily_calls()), 1)
        app.calendar.command = original
        recovered = await self.run_daily()
        self.assertEqual(recovered["state"], "complete", recovered)
        self.assertEqual(len(self.daily_calls()), 1)
        self.assertEqual(app.role_journal.head(self.target().key).state, JournalState.ACTIVE)

    async def test_invalid_part_saves_no_other_domain_result_or_second_call(self):
        app, context = await self.ready()
        good = self.reply
        async def invalid(envelope):
            response = await good(envelope)
            if envelope.original.output_format == "role_daily_review":
                return answered(encode({"diary": {"body": "bad"}, "day_plan": {"items": []},
                    "memory": {"summary": "", "proposals": [], "pending_decisions": [], "anniversaries": []}}))
            return response
        self.model_behavior = invalid
        result = await self.run_daily()
        self.assertEqual(result["state"], "failed", result)
        self.assertEqual(app.role_journal.head(self.target().key).state, JournalState.ABSENT)
        self.assertEqual(len(self.daily_calls()), 1)
        self.assertEqual((await self.run_daily())["state"], "failed")
        self.assertEqual(len(self.daily_calls()), 1)

    async def test_legacy_daily_planning_is_replaced_but_horizon_remains(self):
        app, context = await self.ready()
        settings, _ = app.role_state.settings(context)
        self.assertEqual(app.calendar_planning.opportunity(context, settings, self.clock.now())[0], "horizon")
        self.assertTrue(app.journal_schedule.delegated(context))
        self.assertTrue(app.memory_schedule.delegated(context))
        due = app.daily_review.next_wake().astimezone(ZoneInfo("Asia/Shanghai"))
        self.assertEqual((due.hour, due.minute), (23, 50))

    async def test_daily_wakeup_and_restarted_driver_keep_date_identity_and_late_memory_window(self):
        app, context = await self.ready()
        due = app.daily_review.next_wake()
        self.clock.set(due - timedelta(seconds=1))
        app.daily_review.advance()
        self.assertFalse(app.daily_review.active_handles)
        self.clock.advance(timedelta(seconds=1))
        app.daily_review.advance()
        identifier = app.daily_review.identifier(context, due.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat(), "Asia/Shanghai")
        await self.until(lambda: app.daily_review._row(identifier) is not None
                         and app.daily_review._row(identifier)["state"] == "complete")
        self.assertEqual(len(self.daily_calls()), 1)
        await app.close()
        restarted = await self.start(self.host(runtime=self.runtime), task_generation=1, model_generation=1)
        app = self.app = restarted.application
        app.gateway.request_context = SharedContext()
        app.state_maintenance.bind(context, "原角色人格")
        app.life_runtime.bind(context, "原角色人格")
        app.daily_review.advance()
        await asyncio.sleep(0)
        self.assertEqual(len(self.daily_calls()), 1)
        self.clock.advance(timedelta(days=1))
        result = await self.run_daily()
        self.assertEqual(result["state"], "complete", result)
        stored = json.loads(app.daily_review._row(result["run_id"])["inputs_json"])
        self.assertEqual(datetime.fromisoformat(stored["memory_start"]), due)
        plan = stored["plan"]
        self.assertEqual(datetime.fromisoformat(plan["start"]).astimezone(ZoneInfo("Asia/Shanghai")).day,
                         (self.clock.now().astimezone(ZoneInfo("Asia/Shanghai")) + timedelta(days=1)).day)
        self.assertEqual(len(self.daily_calls()), 2)

    async def test_real_memory_proposal_uses_original_source_handles(self):
        app, context = await self.ready(diary=False, plan=False)
        original = NativeConversationInput("coffee", context.actor.principal,
            ConversationRoute(context.scope, user=context.actor.subject), context.audience,
            "我平时不喝咖啡", self.clock.now(), context.role_binding.role, "原角色人格")
        NativeConversationObservations(app.messages).capture_input(context, original, turn_id="coffee")
        self.clock.advance(timedelta(seconds=1))
        async def remember(envelope):
            source = json.loads(next(item.payload for item in envelope.original.context.materials
                                     if item.purpose == "reflection_source"))
            return answered(encode({"diary": None, "day_plan": None, "memory": {
                "summary": "对方自述不喝咖啡。", "proposals": [{"item_id": "coffee", "operation": "add",
                "evidence_handles": [source["handle"]], "predicate": "饮用偏好", "object": "不喝咖啡",
                "representation": "用户自述平时不喝咖啡。", "target": None,
                "details": [{"field": "single_value", "value": True}]}], "pending_decisions": [], "anniversaries": []}}))
        self.model_behavior = remember
        result = await self.run_daily()
        self.assertEqual(result["state"], "complete", result)
        batch = result["receipts"]["memory"]["batch_id"]
        memory = app.memory_reflection.status(context, batch).unwrap()
        self.assertEqual(memory["receipts"]["coffee"]["state"], "persisted")
        self.assertEqual(len(self.daily_calls()), 1)

    async def test_day_plan_rejects_a_time_before_tomorrow(self):
        app, context = await self.ready(diary=False, memory=False)
        async def wrong_day(envelope):
            return answered(encode({"diary": None, "memory": None, "day_plan": {"items": [{
                "title": "过去一天的安排", "start": (self.clock.now() + timedelta(hours=1)).isoformat(),
                "minutes": 20, "detail": "不能保存到今天"}]}}))
        self.model_behavior = wrong_day
        result = await self.run_daily()
        self.assertEqual(result["state"], "failed", result)
        self.assertEqual(app.calendar.inventory(context)["items"], [])

    async def test_submitted_but_unstarted_day_resumes_after_midnight_without_second_task(self):
        app, context = await self.ready()
        self.clock.set(app.daily_review.next_wake() + timedelta(minutes=9))
        day = self.target().key.local_date
        with patch.object(app.daily_review.runner, "run", side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await self.run_daily()
        identifier = app.daily_review.identifier(context, day, "Asia/Shanghai")
        self.assertEqual(app.daily_review._row(identifier)["state"], "prepared")
        self.assertEqual(self.daily_calls(), [])
        self.clock.advance(timedelta(minutes=2))
        result = await app.daily_review.run(context, "原角色人格", local_date=day)
        self.assertEqual(result["state"], "complete", result)
        self.assertEqual(len(self.daily_calls()), 1)
        count = app.runtime.read_one("SELECT COUNT(*) n FROM execution_tasks WHERE kind=?", (KIND,))["n"]
        self.assertEqual(count, 1)

    async def test_expired_saved_plan_stops_recovery_without_recalling_model(self):
        app, context = await self.ready()
        day = self.target().key.local_date
        with patch.object(app.calendar, "command", side_effect=RuntimeError("synthetic write interruption")):
            first = await self.run_daily()
        self.assertEqual(first["state"], "received", first)
        self.clock.advance(timedelta(days=3))
        result = await app.daily_review.run(context, "原角色人格", local_date=day)
        self.assertEqual(result["state"], "failed", result)
        self.assertEqual(result["receipts"]["code"], "planning_window_expired")
        self.assertEqual(len(self.daily_calls()), 1)
