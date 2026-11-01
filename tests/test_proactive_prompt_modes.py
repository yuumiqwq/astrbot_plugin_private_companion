import json
from types import SimpleNamespace
import unittest

from companion_v2.adapters.astrbot.model_wire import build_wire
from companion_v2.conversation.segmentation import SegmentationPolicy
from companion_v2.conversation.text_processing import ReplyTextPolicy
from companion_v2.model_gateway.management import ModelManagement
from companion_v2.proactive.morning_rhythm import NUDGE_INSTRUCTION
from companion_v2.proactive.prompt_catalog import presentation_instructions
from companion_v2.proactive.prompts import INSTRUCTION, request_for
from companion_v2.proactive.simple_prompts import parse_contact_output
from tests.proactive_support import ContactFixture
from tests.test_role_tool_loop import answered


class ProactivePromptModeTests(ContactFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.app = await self.enabled()
        self.context = self.contact_context()
        self.manager = ModelManagement(self.app, self.context)

    def task(self, manager=None):
        return next(item for item in (manager or self.manager).status(self.core, layer="window").unwrap()["tasks"]
                    if item["kind"] == "proactive.compose")

    def patch_mode(self, mode, *, token=None, operation="switch-mode", reset=False):
        return self.manager.configure(self.core, {"kind": "proactive.compose", "section": "mode", "layer": "window",
            "operation_key": operation, "edit_token": token or self.task()["prompt_assembly"]["mode"]["edit_token"],
            "changes": {} if reset else {"mode": mode}, "reset": ["mode"] if reset else []})

    def patch_prompt(self, key, prompt):
        section = next(item for item in self.task()["prompt_sections"] if item["key"] == key)
        return self.manager.configure(self.core, {"kind": "proactive.compose", "section": key, "layer": "window",
            "operation_key": "save-" + key, "edit_token": section["edit_token"],
            "changes": {"prompt": prompt}, "reset": []})

    async def test_mode_api_preserves_both_sets_and_rejects_stale_edits(self):
        self.assertEqual(self.task()["prompt_assembly"]["current_mode"], "companion")
        self.patch_prompt("simple", "Short task {{current_time}} {{unanswered_count}}").unwrap()
        self.patch_prompt("business", "Saved companion rules").unwrap()
        token = self.task()["prompt_assembly"]["mode"]["edit_token"]
        result = self.patch_mode("simple", token=token).unwrap()
        self.assertEqual(result["task"]["prompt_assembly"]["current_mode"], "simple")
        self.assertNotIn("purpose.chat", str(result["task"]["prompt_assembly"]["order"]))
        self.assertTrue(self.patch_mode("simple", token=token).unwrap()["replayed"])
        stale = self.patch_mode("companion", token=token, operation="stale-mode")
        self.assertEqual(stale.failure.code, "configuration_revision_conflict")
        other = ModelManagement(self.app, self.other_window_context())
        self.assertEqual(self.task(other)["prompt_assembly"]["current_mode"], "companion")
        self.patch_mode("companion", operation="switch-back").unwrap()
        sections = {item["key"]: item["prompt"] for item in self.task()["prompt_sections"]}
        self.assertEqual(sections["simple"], "Short task {{current_time}} {{unanswered_count}}")
        self.assertEqual(sections["business"], "Saved companion rules")
        self.patch_mode("simple", operation="switch-again").unwrap()
        self.patch_mode(None, reset=True, operation="inherit-mode").unwrap()
        self.assertEqual(self.task()["prompt_assembly"]["current_mode"], "companion")

    async def test_simple_uses_real_placeholders_and_omits_candidate_instructions(self):
        self.patch_mode("simple").unwrap()
        self.patch_prompt("simple", "Time {{current_time}}; unanswered={{unanswered_count}}").unwrap()
        record = SimpleNamespace(reference=self.source(self.app), payload="Verified source fact")
        job = SimpleNamespace(intent=self.intent(self.opportunity(), purpose="greeting", summary=NUDGE_INSTRUCTION),
            context=self.context, automatic_origin=None, interaction={"contacts_since_last_input": 3},
            role_instruction="Current persona", administrator_test=True,
            diagnostic_origin={"_diagnostic": {"route_id": "synthetic_event"}})
        request = request_for(job, (record,), self.preferences(self.app),
            role_state={"mood": "calm"}, recent_life="A saved life event")
        self.assertEqual(request.proactive_mode, "simple")
        self.assertEqual(request.response_format, "json_object")
        self.assertIn("unanswered=3", request.instruction)
        self.assertNotIn("{{", request.instruction)
        self.assertNotIn(INSTRUCTION, request.instruction)
        payload = "\n".join(item.payload for item in request.context.materials)
        self.assertNotIn(NUDGE_INSTRUCTION, payload)
        self.assertNotIn("合成情境演练", request.instruction + payload)
        self.assertIn("Verified source fact", payload)
        self.assertEqual(presentation_instructions(self.preferences(self.app),
            SegmentationPolicy(llm_controlled=True), ReplyTextPolicy(enable_humanized_expression=True), news=True), "")

    async def test_simple_request_reaches_provider_without_companion_task_prompt(self):
        self.patch_prompt("business", "Companion-only rules").unwrap()
        self.patch_prompt("simple", "Our short prompt {{current_time}}").unwrap()
        self.patch_mode("simple").unwrap()

        async def simple_response(envelope):
            return answered('{"send":true,"message":"Simple message"}')

        self.model_behavior = simple_response
        result = (await self.contact(self.app)).unwrap()
        self.assertEqual(result.task.state, "succeeded")
        await self.delivery_state(self.app, result.task.task_id, "delivered")
        wire = build_wire(self.called[-1])
        self.assertEqual(self.called[-1].original.response_format, "json_object")
        system = wire.messages[0]["content"]
        self.assertIn("这是原角色人格，保持自然。", system)
        self.assertIn("Our short prompt", system)
        self.assertNotIn("Companion-only rules", system)
        self.assertNotIn(self.called[-1].task_prompt, system)
        self.assertNotIn("decision", system)
        self.assertEqual([item.text for item in self.sent], ["Simple message"])

    async def test_companion_request_also_uses_provider_json_mode(self):
        result = (await self.contact(self.app)).unwrap()
        self.assertEqual(result.task.state, "succeeded")
        self.assertEqual(self.called[-1].original.proactive_mode, "companion")
        self.assertEqual(self.called[-1].original.response_format, "json_object")

    async def test_simple_skip_does_not_send_an_empty_message(self):
        self.patch_mode("simple").unwrap()

        async def skip(envelope):
            return answered('{"send":false,"message":""}')

        self.model_behavior = skip
        result = (await self.contact(self.app)).unwrap()
        self.assertEqual(result.task.state, "succeeded")
        self.assertEqual(self.sent, [])
        report = self.app.proactive.observe(self.context, result.task.task_id).unwrap()
        self.assertTrue(report["skipped"])


class SimpleContactSchemaTests(unittest.TestCase):
    def test_legacy_schema_is_strict_and_newlines_remain_unchanged(self):
        text = "First line\n\nSecond line"
        data, body, reaction = parse_contact_output(json.dumps({"send": True, "message": text}), mode="simple")
        self.assertEqual((data["decision"], body, reaction), ("send", text, None))
        self.assertEqual(parse_contact_output('{"send":false,"message":""}', mode="simple")[1:], (None, None))
        for raw in ('{"send":"false","message":""}', '{"send":false,"message":"x"}',
                    '{"send":true,"message":" "}', '{"send":true,"message":"x","text":"x"}',
                    '{"send":false,"send":true,"message":"x"}', '{"decision":"skip","text":""}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_contact_output(raw, mode="simple")
