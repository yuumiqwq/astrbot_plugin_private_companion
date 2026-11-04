"""Ordinary inbound messages never schedule expression-target learning."""

import asyncio
import unittest

from tests.multi_host_support import MultiFixture, multi_config


class ExpressionLearningScheduleTests(MultiFixture, unittest.IsolatedAsyncioTestCase):
    async def test_private_input_does_not_register_or_enqueue_legacy_learning(self):
        self.controller = self.make_controller(multi_config(delivery_mode="onebot_private"))
        app = await self.start_host()
        await self.accepted(text="我不喝咖啡")
        context, _ = await self.accepted(identifier="event-2", text="我更喜欢喝茶")

        service = app.expression_style
        service.next_checks.clear()
        service.cursor = 0
        service._next_discovery_at = None
        service.advance()
        await asyncio.sleep(0)
        self.assertEqual(service.workers, {})
        self.assertNotIn("role.expression.learn", app.tasks.registrations)
        self.assertNotIn("role.expression.learn", app.gateway.router.profiles)
        status = service.status(context)
        self.assertTrue(status["learning_enabled"])
        self.assertFalse(status["due"])
        self.assertFalse(status["worker"])
        self.assertEqual(status["limit"], 60)
        self.assertEqual(status["state"], "manual_only")
        self.assertEqual(status["source_kind"], "preset_comparison")


if __name__ == "__main__":
    unittest.main()
