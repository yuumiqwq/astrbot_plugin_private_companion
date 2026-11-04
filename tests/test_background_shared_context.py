from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from companion_v2.conversation.foreground import PromptMaterial
from companion_v2.execution.models import Lane
from companion_v2.model_gateway.models import ModelTaskProfile
from companion_v2.model_gateway.shared_role_context import shares_role_context
from tests.test_model_gateway import ModelFixture


class SharedBackgroundGatewayTests(ModelFixture, unittest.IsolatedAsyncioTestCase):
    async def test_shared_background_prepares_before_routing_and_uses_the_chat_provider(self):
        self.patch_model(kind="fixture.background", provider="primary", fallback="primary")
        history = PromptMaterial("astrbot.conversation", "recent_private_dialogue",
                                 '[{"role":"user","content":"Shared dialogue"}]')

        async def prepare(request):
            return replace(request, role_instruction="Shared persona and presets", host_provider_id="fallback",
                           context=replace(request.context, materials=(*request.context.materials, history)))

        self.gateway.request_context = AsyncMock(side_effect=prepare)
        run = self.model_run(kind="fixture.background")
        completion = await self.gateway.generate(run, self.request())
        self.assertIsNotNone(completion.draft)
        self.gateway.request_context.assert_awaited_once()
        provider, envelope = self.called[0]
        self.assertEqual(provider, "fallback")
        self.assertEqual(envelope.original.role_instruction, "Shared persona and presets")
        self.assertIn(history, envelope.original.context.materials)
        self.assertEqual(envelope.original.instruction, "Original reply instruction")
        self.assertEqual(self.gateway.observe(self.context, run.task.task_id).unwrap()["plan"]["provider_override"],
                         "fallback")

    async def test_existing_shared_context_is_not_fetched_twice(self):
        self.gateway.request_context = AsyncMock()
        original = self.request()
        request = replace(original, context=replace(original.context,
            materials=(*original.context.materials, PromptMaterial("astrbot.conversation", "recent_private_dialogue", "[]"))))
        await self.gateway.generate(self.model_run(kind="fixture.background"), request)
        self.gateway.request_context.assert_not_awaited()

    async def test_foreground_chat_keeps_its_own_original_preparation(self):
        self.gateway.request_context = AsyncMock()
        await self.gateway.generate(self.model_run(), self.request())
        self.gateway.request_context.assert_not_awaited()


class SharedBackgroundSelectionTests(unittest.TestCase):
    def test_all_role_text_tasks_share_context_and_media_tasks_keep_their_input(self):
        kinds = ("calendar.plan", "memory.reflect", "role.journal.diary", "role.journal.dream",
                 "role.journal.derive", "role.journal.select", "role.growth.progress",
                 "role.expression.learn", "role.state.maintain", "role.reading.consume", "proactive.compose",
                 "proactive.judge", "role.daily_review")
        for kind in kinds:
            with self.subTest(kind=kind):
                self.assertTrue(shares_role_context(ModelTaskProfile(kind, Lane.BACKGROUND, "chat", "Task")))
        for kind in ("conversation.poke_reply", "role.expression.review", "content.authoring.draft"):
            self.assertTrue(shares_role_context(ModelTaskProfile(kind, Lane.FOREGROUND, "chat", "Task")))
        for kind in ("screen.interpretation", "conversation.image_narration", "conversation.video_narration",
                     "conversation.context_image_caption", "conversation.image_review"):
            self.assertFalse(shares_role_context(ModelTaskProfile(kind, Lane.BACKGROUND, "visual", "Task")))
        self.assertFalse(shares_role_context(ModelTaskProfile("audio.transcribe", Lane.BACKGROUND, "audio", "Task",
            capabilities=("audio",))))
        self.assertFalse(shares_role_context(ModelTaskProfile("role.reader", Lane.BACKGROUND, "chat", "Task"),
            SimpleNamespace(images=(object(),), audios=(), videos=())))
