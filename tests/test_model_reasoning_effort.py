"""Task-scoped reasoning survives routing, including a shared native model."""

from dataclasses import replace
import unittest

from companion_v2.execution.models import TaskError
from tests.test_model_gateway import ModelFixture


class ModelReasoningEffortTests(ModelFixture, unittest.IsolatedAsyncioTestCase):
    async def test_shared_model_uses_current_task_effort_without_changing_other_tasks(self):
        self.patch_model(reasoning_effort="max", max_output_tokens=80)
        request = replace(self.request(), host_provider_id="primary")
        plan = self.router.resolve(self.model_run(), request)
        self.assertEqual((plan.envelope.reasoning_effort, plan.envelope.max_output_tokens), ("max", 80))
        self.assertEqual(plan.audit["reasoning_effort"], "max")
        other = self.router.resolve(self.model_run("other", kind="fixture.background"), request)
        self.assertEqual((other.envelope.reasoning_effort, other.envelope.max_output_tokens), ("", 10))

    async def test_host_provider_override_does_not_bypass_effort_validation(self):
        self.patch_model(reasoning_effort="invalid")
        with self.assertRaises(TaskError) as error:
            self.router.resolve(self.model_run(), replace(self.request(), host_provider_id="primary"))
        self.assertEqual(error.exception.code, "model_configuration_invalid")

    async def test_default_routing_follows_provider_effort(self):
        plan = self.router.resolve(self.model_run(), self.request())
        self.assertEqual(plan.envelope.reasoning_effort, "")
        self.assertEqual(plan.audit["reasoning_effort"], "")
