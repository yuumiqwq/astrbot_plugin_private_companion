"""First-run progress migrates without retaining a runtime-mode choice."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from astrbot_host.setup_state import SCHEMA_VERSION, SETUP_STEPS, SetupStateStore, SetupStepsIncomplete


class SetupStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="companion-v2-setup-state-")
        self.store = SetupStateStore(self.temporary.name)
        self.store.root.mkdir(parents=True)

    def tearDown(self):
        self.temporary.cleanup()

    def write_legacy(self, document):
        self.store.path.write_text(json.dumps(document, ensure_ascii=False) + "\n", encoding="utf-8")

    def test_schema_two_drops_single_target_selection_and_keeps_progress(self):
        self.write_legacy({"schema_version": 2, "mode": {"accounts": "single", "personas": "single"},
            "completed": ["model"], "skipped": ["knowledge"], "proactive_test": None, "ready": False})

        state = self.store.read()
        self.assertEqual(state["schema_version"], SCHEMA_VERSION)
        self.assertNotIn("mode", state)
        self.assertEqual(state["completed"], ["model"])
        self.assertEqual(state["skipped"], ["knowledge"])

        state = self.store.complete("relationships")
        persisted = json.loads(self.store.path.read_text(encoding="utf-8"))
        self.assertEqual(state["completed"], ["model", "relationships"])
        self.assertNotIn("mode", persisted)

    def test_schema_one_receipt_is_kept_without_its_mode_field(self):
        tested_at = "2026-10-05T00:00:00+00:00"
        self.write_legacy({"schema_version": 1, "mode": {"accounts": "single", "personas": "single"},
            "completed": ["model", "knowledge", "relationships"],
            "proactive_test": {"state": "delivered", "tested_at": tested_at}, "ready": True})

        state = self.store.read()

        self.assertEqual(state["schema_version"], SCHEMA_VERSION)
        self.assertNotIn("mode", state)
        self.assertEqual(state["proactive_test"]["state"], "delivered")
        self.assertEqual(state["proactive_test"]["finished_at"], tested_at)
        self.assertTrue(state["ready"])

    def test_setup_progress_does_not_require_mode_selection(self):
        for step in ("model", "knowledge", "relationships", "proactive", "life", "group"):
            self.store.complete(step)

        state = self.store.begin_proactive_test(attempt_id="attempt-1", started_at="2026-10-05T00:00:00+00:00")

        self.assertEqual(state["proactive_test"]["attempt_id"], "attempt-1")
        self.assertNotIn("mode", state)

    def test_every_guided_step_must_be_done_or_skipped_before_the_proactive_test(self):
        for step in ("model", "knowledge", "relationships", "proactive", "life"):
            self.store.complete(step)
        with self.assertRaises(ValueError):
            self.store.begin_proactive_test(attempt_id="attempt-2", started_at="2026-10-05T00:00:00+00:00")
        state = self.store.complete("group", skipped=True)
        self.assertEqual(state["skipped"], ["group"])
        state = self.store.begin_proactive_test(attempt_id="attempt-3", started_at="2026-10-05T00:00:00+00:00")
        self.assertEqual(state["proactive_test"]["attempt_id"], "attempt-3")

    def test_unfinished_guide_and_unreplayable_attempt_are_different_errors(self):
        # Same store, two unrelated refusals. The host route has to tell them
        # apart, so they must not be the same exception class.
        self.store.complete("model")
        with self.assertRaises(SetupStepsIncomplete) as unfinished:
            self.store.begin_proactive_test(attempt_id="attempt-a", started_at="2026-10-05T00:00:00+00:00")
        self.assertIsInstance(unfinished.exception, ValueError)
        self.assertIn("guided setup steps", str(unfinished.exception))

        for step in ("knowledge", "relationships", "proactive", "life", "group"):
            self.store.complete(step, skipped=True)
        self.store.begin_proactive_test(attempt_id="attempt-b", started_at="2026-10-05T00:00:00+00:00")
        with self.assertRaises(ValueError) as replay:
            self.store.begin_proactive_test(attempt_id="attempt-c", started_at="2026-10-05T00:00:00+00:00")
        self.assertNotIsInstance(replay.exception, SetupStepsIncomplete)
        self.assertIn("cannot be replayed", str(replay.exception))

    def simulation_result(self, *, decision="send"):
        return {"state": "passed" if decision == "send" else "skipped", "simulation": True,
            "decision": decision, "attempt_id": "preview-attempt", "task_id": "preview-task",
            "call_id": "actual-model-call", "started_at": "2026-11-01T00:00:00Z",
            "finished_at": "2026-11-01T00:00:01Z", "stages": [
                {"stage": stage, "state": "passed"} for stage in
                ("model_generation", "output_parsing", "segmentation")]}

    def test_successful_simulation_completes_setup_and_keeps_the_legacy_receipt(self):
        for step in SETUP_STEPS:
            self.store.complete(step, skipped=step != "model")
        self.store.begin_proactive_test(attempt_id="old-test", started_at="2026-10-01T00:00:00Z")
        old = self.store.finish_proactive_test(outcome="failed", finished_at="2026-10-01T00:00:01Z",
                                              error_code="contact_diagnostic_source_required")["proactive_test"]
        state = self.store.complete_simulation(self.simulation_result())
        self.assertTrue(state["ready"])
        self.assertEqual(state["proactive_test"]["state"], "simulated")
        self.assertEqual(state["proactive_test"]["previous_receipt"], old)
        self.assertEqual(state["proactive_test"]["delivery_state"], "not_requested")
        self.assertEqual(self.store.read(), state)

    def test_valid_skip_is_successful_schema_validation_without_delivery(self):
        for step in SETUP_STEPS:
            self.store.complete(step)
        state = self.store.complete_simulation(self.simulation_result(decision="skip"))
        self.assertTrue(state["ready"])
        self.assertEqual(state["proactive_test"]["decision"], "skip")
        self.assertEqual(self.store.read(), state)

    def test_preview_does_not_skip_unfinished_setup_sections(self):
        self.store.complete("model")
        before = self.store.read()
        self.assertEqual(self.store.complete_simulation(self.simulation_result()), before)
        self.assertEqual(self.store.read(), before)

    def test_failed_or_unverified_simulation_cannot_complete_setup(self):
        for step in SETUP_STEPS:
            self.store.complete(step)
        for patch in ({"state": "failed"}, {"simulation": False}, {"call_id": ""}, {"stages": []}):
            with self.subTest(patch=patch):
                result = {**self.simulation_result(), **patch}
                self.assertFalse(self.store.complete_simulation(result)["ready"])
                self.assertFalse(self.store.read()["ready"])

    def test_existing_completed_receipt_is_not_replaced_by_later_simulations(self):
        for step in SETUP_STEPS:
            self.store.complete(step)
        self.store.begin_proactive_test(attempt_id="old-test", started_at="2026-10-01T00:00:00Z")
        before = self.store.finish_proactive_test(outcome="delivered", finished_at="2026-10-01T00:00:01Z")
        self.assertEqual(self.store.complete_simulation(self.simulation_result()), before)
        self.assertEqual(self.store.read(), before)



if __name__ == "__main__":
    unittest.main()
