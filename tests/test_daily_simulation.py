"""Daily previews exercise the production request and validators without domain writes."""

import unittest
from unittest.mock import patch

from companion_v2.configuration.registry import FieldRegistry
from companion_v2.execution.models import TaskError, encode
from companion_v2.model_gateway.routing import configuration_fields
from companion_v2.role_work.daily.service import daily_profile
from companion_v2.role_work.daily.simulation import RESULT_PREFIX, simulate_daily_review
from tests import test_role_daily_review as daily_support
from tests.test_role_state import StateFixture
from tests.test_role_tool_loop import answered


class DailySimulationTests(StateFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.profiles += (daily_profile("primary", max_output_tokens=100),)
        self.registry = FieldRegistry(version="daily.preview.tests", fields=configuration_fields(self.profiles))

    switches = daily_support.DailyReviewTests.switches
    reply = daily_support.DailyReviewTests.reply
    daily_calls = daily_support.DailyReviewTests.daily_calls

    async def ready(self, **parts):
        app, context = await daily_support.DailyReviewTests.ready(self, **parts)
        # Finish the fixture's normal settings activation before measuring the
        # preview. The real host has already registered its diary schedule.
        app.life_runtime.for_context(context).refresh()
        return app, context

    def protected(self, app):
        prefixes = ("source", "memory_", "role_journal", "journal_", "calendar", "agenda", "role_daily",
                    "conversation_", "background_", "proactive_", "role_life", "role_activity")
        tables = [row[0] for row in app.runtime.read_all("SELECT name FROM sqlite_master WHERE type='table'", ())
                  if row[0].startswith(prefixes)]
        return {table: [tuple(row) for row in app.runtime.read_all('SELECT * FROM "' + table + '" ORDER BY rowid', ())]
                for table in tables}

    async def preview(self, app, context):
        return await simulate_daily_review(app, context, "原角色人格", attempt_id="test-daily-preview")

    async def test_one_call_validates_all_parts_without_formal_records(self):
        app, context = await self.ready(plan=False)
        before = self.protected(app)
        with patch("companion_v2.role_work.daily.output.commit", side_effect=AssertionError("no domain commit")):
            result = await self.preview(app, context)
        self.assertEqual(result["state"], "passed", result)
        self.assertTrue(all(result["parts_enabled"].values()))
        self.assertEqual(set(result["result"]), {"diary", "day_plan", "memory"})
        self.assertEqual(len(self.daily_calls()), 1)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.protected(app), before)
        self.assertFalse(app.role_state.settings(context)[0]["planning_enabled"])
        self.assertFalse(app.daily_review.pending)
        self.assertIsNotNone(app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key LIKE ?",
                                                 (RESULT_PREFIX + "%",)))

    async def test_invalid_schema_never_commits_other_parts(self):
        app, context = await self.ready()
        before = self.protected(app)
        async def invalid(_):
            return answered(encode({"diary": {"body": "无效日记"}, "day_plan": {"items": []},
                "memory": {"summary": "", "proposals": [], "pending_decisions": [], "anniversaries": []}}))
        self.model_behavior = invalid
        with self.assertRaises(TaskError):
            await self.preview(app, context)
        self.assertEqual(len(self.daily_calls()), 1)
        self.assertEqual(self.protected(app), before)
        self.assertEqual(self.sent, [])
        self.assertFalse(app.daily_review.pending)

    async def test_provider_failure_leaves_temporary_sources_and_selections_rolled_back(self):
        app, context = await self.ready()
        before = self.protected(app)
        async def unavailable(_):
            raise RuntimeError("synthetic provider failure")
        self.model_behavior = unavailable
        with self.assertRaises(TaskError):
            await self.preview(app, context)
        self.assertEqual(self.protected(app), before)
        self.assertEqual(self.sent, [])
        self.assertFalse(app.daily_review.pending)

    async def test_disabled_parts_do_not_call_model_or_change_switches(self):
        app, context = await self.ready(diary=False, plan=False, memory=False)
        self.configure(self.state_manager, calendar_enabled=False)
        before = self.protected(app)
        result = await self.preview(app, context)
        self.assertEqual(result["state"], "skipped")
        self.assertFalse(any(result["parts_enabled"].values()))
        self.assertEqual(self.daily_calls(), [])
        self.assertEqual(self.protected(app), before)
        self.assertEqual(self.sent, [])
