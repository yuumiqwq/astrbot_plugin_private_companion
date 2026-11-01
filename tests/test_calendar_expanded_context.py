"""Expanded chat schedules retain past and future plans across calendar months."""

from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch

from companion_v2.agenda.calendar_prompt import expanded_schedule_window
from companion_v2.agenda.models import Schedule
from companion_v2.execution.models import TaskError, encode
from tests.test_management_access import NOW
from tests.test_role_state import StateFixture, calendar_data, state_data


class ExpandedScheduleWindowTests(unittest.TestCase):
    def test_calendar_month_handles_month_end_leap_year_and_local_date(self):
        cases = (
            (datetime(2026, 1, 31, 12, tzinfo=timezone.utc), "2026-01-24", "2026-03-01"),
            (datetime(2028, 1, 31, 12, tzinfo=timezone.utc), "2028-01-24", "2028-03-01"),
            (datetime(2026, 12, 30, 20, tzinfo=timezone.utc), "2026-12-24", "2027-02-01"),
        )
        for at, first_day, exclusive_end in cases:
            with self.subTest(at=at):
                start, end = expanded_schedule_window(at, "Asia/Shanghai")
                self.assertEqual(start.date().isoformat(), first_day)
                self.assertEqual(end.date().isoformat(), exclusive_end)
                self.assertEqual(start.hour, 0)
                self.assertEqual(end.hour, 0)
                self.assertEqual(start.utcoffset(), timedelta(hours=8))


class ExpandedScheduleContextTests(StateFixture, unittest.IsolatedAsyncioTestCase):
    def create_plan(self, app, context, identifier, at, source, **changes):
        app.calendar.command(context, identifier=identifier, action="create", expected_revision=0,
            operation_key=identifier, sources=(source,), data=calendar_data(title=identifier,
                schedule=json.loads(encode(Schedule(at, "Asia/Shanghai"))), **changes))

    async def test_past_and_month_end_survive_more_than_64_entries_with_source_checks(self):
        _, app, context, manager = self.setup_state()
        self.configure(manager, calendar_enabled=True, schedule_simulation_mode="expanded")
        consumer = app.messages.role_state_context
        shared_source = self.source(app, context, "expanded-source")
        final_source = self.source(app, context, "expanded-final-source")
        start, end = expanded_schedule_window(NOW, "Asia/Shanghai")
        for index in range(72):
            self.create_plan(app, context, f"plan-{index:02d}", start + timedelta(hours=2 + index * 12), shared_source)
        self.create_plan(app, context, "month-end-plan", end - timedelta(hours=1), final_source)
        self.create_plan(app, context, "outside-old", start - timedelta(hours=2), shared_source)
        self.create_plan(app, context, "outside-future", end, shared_source)
        self.create_plan(app, context, "user-plan", NOW, shared_source, kind="user_report")

        # A plan spanning two weekly pages is emitted only once.
        self.create_plan(app, context, "week-boundary", start + timedelta(days=7, minutes=-30), shared_source,
                         duration_minutes=120)
        with patch.object(app.calendar, "occurrences", wraps=app.calendar.occurrences) as occurrences:
            snapshot = consumer.capture(context, NOW)
        self.assertEqual(snapshot["schedule_simulation_mode"], "expanded")
        self.assertEqual(len(snapshot["calendar"]["items"]), 74)
        self.assertFalse(snapshot["calendar"]["partial"])
        self.assertEqual(len(occurrences.call_args_list), 6)
        for call in occurrences.call_args_list:
            self.assertLessEqual(call.kwargs["end"] - call.kwargs["start"], timedelta(days=7))
            self.assertEqual(call.kwargs["kinds"], ("role_plan",))

        materials = consumer.contributions(snapshot, context, NOW)
        instruction = consumer.instruction(materials)
        self.assertIn("过去 7 天至未来 1 个月", instruction)
        self.assertIn("plan-00", instruction)
        self.assertIn("month-end-plan", instruction)
        self.assertNotIn("outside-old", instruction)
        self.assertNotIn("outside-future", instruction)
        self.assertNotIn("user-plan", instruction)
        self.assertEqual(instruction.count("week-boundary"), 1)
        self.assertEqual(len(json.loads(materials[0].payload)["calendar"]["items"]), 74)

        self.revoke_source(app, final_source)
        with self.assertRaises(TaskError):
            consumer.contributions(snapshot, context, NOW)

    async def test_expanded_calendar_also_survives_the_full_role_state_projection(self):
        _, app, context, manager = self.setup_state()
        self.configure(manager, calendar_enabled=True, schedule_simulation_mode="expanded", enabled=True)
        app.role_state.write(context, data=state_data(energy=60), expected_revision=0, operation_key="seed-expanded-state")
        source = self.source(app, context, "expanded-state-source")
        self.create_plan(app, context, "next-month-exam", NOW + timedelta(days=25), source)
        consumer = app.messages.role_state_context
        snapshot = consumer.capture(context, NOW)
        self.assertEqual(snapshot["projection_version"], 3)
        self.assertIn("next-month-exam", consumer.instruction(consumer.contributions(snapshot, context, NOW)))

    async def test_partial_week_remains_marked_in_the_projected_material(self):
        _, app, context, manager = self.setup_state()
        self.configure(manager, calendar_enabled=True, schedule_simulation_mode="expanded")
        source = self.source(app, context, "expanded-partial-source")
        self.create_plan(app, context, "visible-plan", NOW, source)
        original = app.calendar.occurrences

        def partial_week(*args, **kwargs):
            result = original(*args, **kwargs)
            return {**result, "partial": True, "skipped": 1}

        consumer = app.messages.role_state_context
        with patch.object(app.calendar, "occurrences", side_effect=partial_week):
            snapshot = consumer.capture(context, NOW)
        self.assertTrue(snapshot["calendar"]["partial"])
        self.assertEqual(snapshot["calendar"]["skipped"], 6)
        instruction = consumer.instruction(consumer.contributions(snapshot, context, NOW))
        self.assertIn("材料可能不完整", instruction)
