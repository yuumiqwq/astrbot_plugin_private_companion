"""Approved presets are the sole source of new expression-learning targets."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from companion_v2.execution.models import TaskError
from companion_v2.expression.style_service import ExpressionStyleService
from companion_v2.foundation.refs import ScopeKind


class RetiredInboundLearningTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_entry_points_do_not_read_sources_or_call_a_model(self):
        runtime = Mock()
        service = ExpressionStyleService(runtime)
        service.samples = Mock()
        service.gateway = Mock()
        service.tasks = Mock()
        self.assertEqual(service.registrations, ())
        for kind in (ScopeKind.PRIVATE, ScopeKind.GROUP):
            with self.subTest(kind=kind):
                context = SimpleNamespace(key=kind.value, scope=SimpleNamespace(kind=kind))
                self.assertIsNone(service.prepare(context, "persona", None))
                service.advance()
                with self.assertRaises(TaskError) as start:
                    service.start(context, "persona")
                self.assertEqual(start.exception.code, "expression_preset_learning_required")
                with self.assertRaises(TaskError) as run:
                    await service.run(context, "persona")
                self.assertEqual(run.exception.code, "expression_preset_learning_required")
        with self.assertRaises(TaskError) as generate:
            await service.generate(Mock())
        self.assertEqual(generate.exception.code, "expression_preset_learning_required")
        self.assertEqual(service.validate(Mock(), None).failure.code,
                         "expression_preset_learning_required")
        self.assertEqual(service.workers, {})
        self.assertFalse(runtime.mock_calls)
        self.assertFalse(service.samples.mock_calls)
        self.assertFalse(service.gateway.mock_calls)

if __name__ == "__main__":
    unittest.main()
