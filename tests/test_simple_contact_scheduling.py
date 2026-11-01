"""The compact mode has one periodic decision and no category producers."""

from datetime import timedelta
from contextlib import ExitStack
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from companion_v2.execution.models import TaskError
from companion_v2.proactive.configuration import PURPOSES
from tests.contact_schedule_support import ContactScheduleFixture
from tests.test_role_tool_loop import answered


class SimpleContactSchedulingTests(ContactScheduleFixture, unittest.IsolatedAsyncioTestCase):
    async def ready(self):
        host, _ = await self.scheduled(automatic=False)
        app = host.application
        self.settings(app, {"prompt_mode": "simple", "automatic_enabled": True,
                           "proactive_intensity_preset": "off"}).unwrap()
        self.clock.advance(timedelta(seconds=60))
        schedule = app.contact_schedule
        schedule._closed = True  # Exercise the chosen plan without the polling driver racing it.
        context = self.contact_context()
        return app, schedule, context, schedule.latest_conversation(context)

    async def test_one_periodic_candidate_ignores_all_category_controls_and_sources(self):
        app, schedule, context, row = await self.ready()
        changes = {purpose + ".enabled": False for purpose in PURPOSES}
        changes.update({"chat.pause_until": (self.clock.now() + timedelta(days=1)).isoformat(),
                        "chat.weight": 0.01, "proactive_hour_activity_enabled": True})
        self.settings(app, changes).unwrap()
        forbidden = Mock(side_effect=AssertionError("category source must not run"))
        with ExitStack() as stack:
            for name in ("calendar_events", "wake_events", "greeting_events", "health_events", "events",
                         "journal_events", "growth_events", "date_events", "meal_events", "screen_events",
                         "official_alert_events", "balance_events", "news_events", "weather_events",
                         "weather_change_events", "relationship_events", "reminiscence_events"):
                stack.enter_context(patch.object(schedule, name, NS(select=forbidden, select_transitions=forbidden,
                    prompt_candidates=forbidden, adjusted_curve=forbidden, peek_due=forbidden)))
            stack.enter_context(patch.object(schedule, "screen_peek", NS(ready=forbidden, request=forbidden)))
            self.assertFalse(schedule._peek(row, self.clock.now()))
            plan = schedule._plan(row, self.clock.now())
            self.assertIsNotNone(plan)
            self.assertTrue(plan[1].opportunity_id.startswith("simple-periodic-"))
            self.assertEqual(len(plan[2]), 1)
            self.assertEqual(plan[2][0].thought.weight, 1.0)
            self.assertEqual(set(plan[4]), {"batch_id", "event_key", "event_hash", "role_hash"})
        forbidden.assert_not_called()

    async def test_global_switches_pause_quiet_unanswered_and_budget_still_apply(self):
        app, schedule, context, row = await self.ready()
        now = self.clock.now()
        for changes, reset in (
            ({"enabled": False}, {"enabled": True}),
            ({"automatic_enabled": False}, {"automatic_enabled": True}),
            ({"pause_until": (now + timedelta(hours=1)).isoformat()}, {"pause_until": ""}),
            ({"quiet_enabled": True, "quiet_start": "00:00", "quiet_end": "00:00"}, {"quiet_enabled": False}),
        ):
            self.settings(app, changes).unwrap()
            self.assertIsNone(schedule._plan(row, now), changes)
            self.settings(app, reset).unwrap()
        self.settings(app, {"unanswered_pause_after": 3}).unwrap()
        interaction = app.proactive.history.interaction(context, now, action="proactive.contact")
        with patch.object(app.proactive.history, "interaction", return_value={**interaction, "contacts_since_last_input": 3}):
            self.assertIsNone(schedule._plan(row, now))
        with patch.object(app.proactive.budget, "contacts", side_effect=TaskError("contact_delivery_budget", "full")):
            with self.assertRaises(TaskError) as error:
                schedule._plan(row, now)
            self.assertEqual(error.exception.code, "contact_delivery_budget")
        self.assertIsNotNone(schedule._plan(row, now))

    async def test_periodic_skip_uses_one_call_and_keeps_interval_without_judges(self):
        app, schedule, context, row = await self.ready()
        self.settings(app, {"enable_llm_proactive_persona_judge": True,
                           "chat.enabled": False}).unwrap()
        self.assertEqual(app.proactive._judge_plan(None, "A candidate", self.preferences(app), self.clock.now()),
                         (None, None))
        async def generate(_):
            return answered('{"send":false,"message":""}')
        self.model_behavior = generate
        plan = schedule._plan(row, self.clock.now())
        result = (await schedule._run(plan)).unwrap()
        self.assertEqual(result.task.state, "succeeded")
        self.assertEqual(len(self.contact_calls()), 1)
        self.assertEqual(self.contact_calls()[0].original.proactive_mode, "simple")
        self.assertIsNone(schedule._plan(row, self.clock.now()))
        self.clock.advance(timedelta(seconds=59))
        self.assertIsNone(schedule._plan(row, self.clock.now()))
        self.clock.advance(timedelta(seconds=1))
        self.assertIsNotNone(schedule._plan(row, self.clock.now()))
        self.assertEqual(app.proactive.history.interaction(context, self.clock.now(),
                         action="proactive.contact")["contacts_since_last_input"], 0)
        self.assertIsNone(app.runtime.read_one("SELECT 1 FROM execution_tasks WHERE kind='proactive.judge'", ()))

    async def test_direct_event_contacts_are_not_an_alternate_simple_trigger(self):
        app, _, _, _ = await self.ready()
        opportunity = self.opportunity("external-event")
        result = await self.contact(app, opportunity, (self.intent(opportunity, purpose="environment"),))
        self.assertEqual(result.failure.code, "contact_simple_periodic_only")
        self.assertEqual(self.contact_calls(), [])
