import json
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock

from companion_v2.conversation.foreground import PromptMaterial
from companion_v2.execution.models import TaskError
from companion_v2.proactive.simulation import RESULT_PREFIX, simulate_contact
from tests.proactive_support import ContactFixture
from tests.test_role_tool_loop import answered


class ProactiveSimulationTests(ContactFixture, unittest.IsolatedAsyncioTestCase):
    def candidate(self, identifier="preview"):
        opportunity = self.opportunity(identifier)
        return {"opportunity": opportunity, "intents": (self.intent(opportunity),),
                "role_instruction": "这是当前人格。", "route": {"id": "synthetic_event"}}

    def protected_counts(self, app):
        tables = ("proactive_contacts", "background_deliveries", "background_delivery_receipts",
                  "conversation_inputs", "conversation_batches", "memory_atoms", "memory_views",
                  "memory_sessions", "memory_handles")
        return {table: app.runtime.read_one("SELECT COUNT(*) n FROM " + table, ())["n"] for table in tables}

    async def preview(self, app, candidate=None, **kwargs):
        return await simulate_contact(app, self.contact_context(), candidate or self.candidate(),
                                      attempt_id="preview-attempt", **kwargs)

    async def test_real_model_schema_and_host_parts_without_production_records(self):
        app = await self.enabled()
        before = self.protected_counts(app)
        async def model(_):
            return answered(json.dumps({"decision": "send", "text": "第一句\n第二句"}, ensure_ascii=False))
        self.model_behavior = model
        app.proactive.output_segments = lambda context, text: tuple(text.splitlines())
        result = await self.preview(app)
        self.assertEqual((result["state"], result["decision"], result["parts"]),
                         ("passed", "send", ["第一句", "第二句"]))
        self.assertEqual(len(self.called), 1)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.protected_counts(app), before)
        self.assertIsNotNone(app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key LIKE ?",
                                                   (RESULT_PREFIX + "%",)))
        self.assertFalse(app.proactive._pending)

    async def test_shared_context_and_current_persona_reach_model(self):
        app = await self.enabled()
        async def shared(request):
            return replace(request, role_instruction="当前 AstrBot 人格",
                context=replace(request.context, materials=(*request.context.materials,
                    PromptMaterial("astrbot.conversation", "recent_private_dialogue",
                                   json.dumps([{"role":"user","content":"已保存的预设与历史"}],ensure_ascii=False)))))
        app.proactive.request_context = AsyncMock(side_effect=shared)
        result = await self.preview(app)
        self.assertEqual(result["state"], "passed")
        request = self.called[0].original
        self.assertEqual(request.role_instruction, "当前 AstrBot 人格")
        self.assertTrue(any(item.owner == "astrbot.conversation" for item in request.context.materials))
        app.proactive.request_context.assert_awaited_once()
        self.assertEqual(self.sent, [])

    async def check_simple_schema(self, send):
        app = await self.enabled()
        self.settings(app, {"prompt_mode": "simple"}).unwrap()
        async def model(_):
            return answered(json.dumps({"send":send,"message":"一\n二" if send else ""},ensure_ascii=False))
        self.model_behavior = model
        app.proactive.output_segments = lambda context,text:tuple(text.splitlines())
        result = await self.preview(app, self.candidate("simple-" + str(send)))
        self.assertEqual(result["mode"], "simple")
        self.assertEqual(result["parts"], ["一","二"] if send else [])
        self.assertEqual(result["decision"], "send" if send else "skip")
        self.assertEqual(self.sent, [])

    async def test_simple_send_uses_active_schema_and_host_parts(self):
        await self.check_simple_schema(True)

    async def test_simple_skip_creates_no_empty_part(self):
        await self.check_simple_schema(False)

    async def test_invalid_output_never_becomes_a_message(self):
        app = await self.enabled()
        self.settings(app, {"prompt_mode":"simple"}).unwrap()
        before = self.protected_counts(app)
        async def model(_):
            return answered('{"send":false,"message":"unexpected text"}')
        self.model_behavior = model
        with self.assertRaises(TaskError) as caught:
            await self.preview(app)
        self.assertEqual(caught.exception.code, "simulation_output_invalid")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.protected_counts(app), before)
        self.assertFalse(app.proactive._pending)

    async def test_model_failure_has_no_delivery_fallback(self):
        app = await self.enabled()
        before = self.protected_counts(app)
        async def model(_):
            raise RuntimeError("synthetic provider failure")
        self.model_behavior = model
        with self.assertRaises(TaskError):
            await self.preview(app)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.protected_counts(app), before)
        self.assertFalse(app.proactive._pending)

    async def test_paused_contact_can_preview_without_changing_switch(self):
        app = await self.enabled()
        self.settings(app, {"enabled":False}).unwrap()
        result = await self.preview(app)
        self.assertEqual(result["state"], "passed")
        self.assertFalse(self.preferences(app).values["enabled"])
        self.assertEqual(self.sent, [])

    async def test_old_delivered_diagnostics_are_not_recalled_as_background_chat(self):
        app = await self.enabled()
        task = (await self.contact(app, administrator_test=True)).unwrap().task
        await self.delivery_state(app, task.task_id, "delivered")
        self.assertTrue(self.sent)  # The fixture represents a saved legacy send.
        history = app.proactive.history.capture(self.contact_context(), self.clock.now(),
                                               action="proactive.contact")
        self.assertEqual(history["references"], [])
        self.assertEqual(app.runtime.read_one("SELECT COUNT(*) n FROM background_deliveries", ())["n"], 1)
