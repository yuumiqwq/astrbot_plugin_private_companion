import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from astrbot_host.core_reply_context import build_core_reply_prompt
from astrbot_host.shared_context import AstrBotSharedContext
from astrbot_host.shared_materials import OWNER, common_materials, merge_common_materials
from companion_v2.conversation.foreground import ContextContribution, ContributionState, PromptMaterial
from companion_v2.execution.models import TaskError
from tests.test_model_gateway import ModelFixture


class SharedRoleMaterialTests(ModelFixture, unittest.IsolatedAsyncioTestCase):
    def application(self):
        def owner(name, purpose, payload):
            contribution = ContextContribution(purpose, name, purpose, ContributionState.READY,
                                                json.dumps(payload))
            return NS(capture=Mock(return_value={}), contributions=Mock(return_value=(contribution,)),
                      instruction=Mock(return_value="Use the saved role state."))

        messages = NS(
            acquaintance=NS(card_contribution=Mock(return_value=None)), knowledge=None,
            role_profile_context=None, relationship_profile_context=None, relationship_context=None,
            role_growth_context=None, role_self_review_context=None, open_items_context=None,
            proactive_inspirations=None, memory_context=object(), expression_style_context=None,
            life_context=owner("role.life", "role_life_context", {"diary": "Yesterday's walk"}),
            role_state_context=owner("role.state", "role_state", {
                "mood": "calm", "role_schedule": {"mode": "expanded", "items": ["Tomorrow's exam"]}}),
            cross_window_context=NS(contribution=Mock(return_value=ContextContribution(
                "elsewhere", "conversation.awareness", "other_window", ContributionState.READY,
                '{"text":"Other window only"}'))),
        )
        settings = {"guidance": "", "calendar_timezone": "Asia/Shanghai", "calendar_enabled": False,
                    "platform_enabled": True, "model_route_enabled": True, "configured_modalities_enabled": True}
        return NS(messages=messages, runtime=self.runtime, memory=None,
                  perception_settings=NS(capture=Mock(return_value={"settings": settings})),
                  reply_settings=NS(snapshot=Mock(side_effect=AssertionError("reply-only settings"))),
                  router=NS(profiles={}))

    @staticmethod
    def memory():
        return (ContextContribution("memory-test", "memory", "current_memory", ContributionState.READY,
                                    '{"text":"Remembered examination"}'),)

    async def test_common_materials_match_normal_reply_and_enter_the_native_history_budget(self):
        app = self.application()
        request = self.request()
        history = [{"role": "user", "content": "Tomorrow's exam"},
                   {"role": "assistant", "content": "Good luck"}]
        native_input = NS(text="Tomorrow's exam", event_id=request.event.event_id,
                          received_at=request.event.received_at)
        with patch("astrbot_host.core_reply_context._memory_materials", return_value=self.memory()) as memory:
            ordinary = build_core_reply_prompt(app, self.context, native_input, at=request.event.received_at)
            ordinary_blocks = dict(ordinary.context_blocks)
            app.reply_settings.snapshot.reset_mock()
            app.messages.cross_window_context.contribution.reset_mock()
            manager = NS(shared_history=NS(settings=Mock(return_value={})),
                get_curr_conversation_id=AsyncMock(return_value="current"),
                get_conversation=AsyncMock(return_value=NS(history=json.dumps(history))))

            async def compact(_umo, _provider, native_request, **_kwargs):
                materials = json.loads(native_request.prompt)
                common = {item["purpose"]: item["payload"] for item in materials if item["owner"] == OWNER}
                for section in ("memory", "role_state", "life"):
                    self.assertEqual(common[section], ordinary_blocks[section])
                self.assertEqual(json.loads(common["perception"])["context"],
                                 json.loads(ordinary.perception_context))
                self.assertNotIn("cross_window", common)
                self.assertNotIn("sent_messages", common)
                self.assertNotIn("task_prompt", common)
                self.assertNotIn("expression_preferences", common)
                self.assertEqual(native_request.contexts, history)
                native_request.contexts = history[-1:]

            native = NS(conversation_manager=manager, get_provider_by_id=Mock(return_value=object()),
                        prepare_session_request=AsyncMock(side_effect=compact))
            bridge = NS(verified_persona=AsyncMock(return_value={"prompt": "OvO"}))
            adapter = AstrBotSharedContext(native, bridge, "primary", application=app)
            self.gateway.request_context = adapter
            run = self.model_run(kind="fixture.background")
            result = await self.gateway.prepare_request(run, request)
            again = await self.gateway.prepare_request(run, result)

        self.assertIs(again, result)
        native.prepare_session_request.assert_awaited_once()
        manager.get_conversation.assert_awaited_once()
        app.reply_settings.snapshot.assert_not_called()
        app.messages.cross_window_context.contribution.assert_not_called()
        self.assertEqual(memory.call_args.args[1], self.context)
        self.assertEqual(memory.call_args.args[2].text, "Tomorrow's exam")
        saved = [item for item in result.context.materials if item.owner == "astrbot.conversation"]
        self.assertEqual(len(saved), 1)
        self.assertEqual(json.loads(saved[0].payload), history[-1:])
        self.assertEqual(result.role_instruction, "OvO")

    def test_common_capture_does_not_leave_query_side_effects(self):
        app, request = self.application(), self.request()
        with self.runtime.transaction() as db:
            db.execute("CREATE TEMP TABLE shared_material_probe (value TEXT)")

        def memory(*_args):
            with self.runtime.transaction() as db:
                db.execute("INSERT INTO shared_material_probe VALUES ('temporary recall')")
            return self.memory()

        with patch("astrbot_host.core_reply_context._memory_materials", side_effect=memory):
            material = common_materials(app, self.context, request,
                                        [{"role": "user", "content": "Examination"}])
        self.assertIn("memory", {item.purpose for item in material})
        self.assertEqual(self.runtime.read_one("SELECT COUNT(*) AS n FROM shared_material_probe", ())["n"], 0)

    def test_other_window_is_rejected_before_any_owner_capture(self):
        app, request = self.application(), self.request()
        other = NS(scope=object(), role_binding=self.context.role_binding)
        with self.assertRaises(TaskError) as caught:
            common_materials(app, other, request, [])
        self.assertEqual(caught.exception.code, "shared_context_changed")
        app.messages.life_context.capture.assert_not_called()
        app.messages.role_state_context.capture.assert_not_called()

    def test_only_duplicate_optional_previews_are_replaced(self):
        materials = (
            PromptMaterial("memory", "current_memory", "old recall"),
            PromptMaterial("memory", "reflection_source", "daily input handle"),
            PromptMaterial("delivery.history", "dedup_candidate", "sent candidate"),
            PromptMaterial("proactive.contact", "contact_intent", json.dumps({
                "thought": "Contact opportunity", "role_state": {"old": True}, "recent_life": {"old": True}})),
        )
        common = tuple(PromptMaterial(OWNER, name, name) for name in ("memory", "life", "role_state"))
        merged = merge_common_materials(materials, common)
        self.assertNotIn("old recall", [item.payload for item in merged])
        self.assertIn(materials[1], merged)
        self.assertIn(materials[2], merged)
        intent = next(item for item in merged if item.purpose == "contact_intent")
        self.assertEqual(json.loads(intent.payload), {"thought": "Contact opportunity"})
        self.assertEqual(merge_common_materials(merged, common), merged)
