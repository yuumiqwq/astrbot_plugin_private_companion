"""Existing conversations survive removal of obsolete learning owners."""

import json
import unittest

from astrbot_host.configuration_api import ConfigurationWebAPI
from astrbot_host.growth_api import GrowthWebAPI
from companion_v2.conversation.participation import GroupParticipation
from tests.test_conversation_messages import MessageFixture
from tests.test_host_application import HostFixture
from tests.test_management_access import NOW


class RetiredReviewContextTests(MessageFixture, unittest.IsolatedAsyncioTestCase):
    async def test_saved_review_snapshot_does_not_restore_removed_guidance(self):
        batch = self.accept()
        self.messages.seal(self.context, batch, deadline_at=self.deadline).unwrap()
        row = self.messages.batch(batch)
        saved = json.loads(row["contributions_json"])
        saved["role_self_review"] = {"references": [{"item_id": "old", "run_id": "old"}]}
        with self.runtime.transaction() as connection:
            connection.execute("UPDATE conversation_batches SET contributions_json=? WHERE batch_id=?",
                               (json.dumps(saved), batch))
        request = self.messages._request(self.messages.batch(batch), self.context, NOW)
        self.assertTrue(request.context.materials)
        self.assertFalse(any(material.owner == "role.self_review" for material in request.context.materials))


class RetiredLearningHostTests(HostFixture, unittest.IsolatedAsyncioTestCase):
    async def test_host_keeps_daily_work_and_growth_without_removed_workers(self):
        host = await self.start()
        app = host.application
        self.assertFalse(hasattr(app, "self_review"))
        self.assertFalse(hasattr(app.messages, "role_self_review_context"))
        self.assertFalse(hasattr(app.tools.group_participation, "behavior"))
        self.assertIsNotNone(app.daily_review)
        self.assertIsNotNone(app.growth_progress)

    def test_removed_management_routes_are_unavailable(self):
        growth_routes = [route[0] for route in GrowthWebAPI(None).routes()]
        self.assertFalse(any("self-review" in path for path in growth_routes))
        self.assertTrue(any(path.endswith("/progress/command") for path in growth_routes))
        group_routes = [route[0] for route in ConfigurationWebAPI(None).routes()]
        self.assertFalse(any("/behavior" in path for path in group_routes))
        self.assertEqual(GroupParticipation.actions, frozenset({"group.participation.choose"}))
