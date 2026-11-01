"""Retry only completed, accounted blank proactive responses, once and unchanged."""

from dataclasses import replace
import unittest

from companion_v2.adapters.astrbot.model_wire import build_wire
from companion_v2.conversation.foreground import ModelDraft
from companion_v2.execution.models import TaskError
from companion_v2.model_gateway.failures import ModelFailure
from companion_v2.model_gateway.models import Amount, CapturedModelOutput, ModelOutputError, ProviderResponse
from companion_v2.model_gateway.response_diagnostics import ResponseDiagnostics, ResponseTextSummary
from tests.test_model_gateway import ModelFixture


class ProactiveBlankRetryTests(ModelFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.patch_model(fallback="")
        self.wires = []

    def proactive_request(self):
        return replace(self.request(), output_format="proactive_contact", proactive_mode="simple",
                       response_format="json_object", response_schema_json='{"type":"object"}')

    def rejected(self, envelope, *, text="       ", finish="stop", known_usage=True, diagnostics=True):
        self.wires.append(build_wire(envelope).messages)
        if envelope.record_rejected_output is not None:
            envelope.record_rejected_output(CapturedModelOutput(text, finish), "invalid JSON", "JSONDecodeError")
        return ProviderResponse("unknown", failure=ModelFailure.WIRE_DECODE,
            usage=Amount(17, 40, input_tokens=10, output_tokens=7) if known_usage else None,
            receipt_ref="rejected-" + str(len(self.called)) if known_usage else None,
            diagnostics=ResponseDiagnostics(completion_present=True, finish_reason=finish,
                parse_state="contract_rejected", raw=ResponseTextSummary.from_text(text),
                failure=ModelFailure.WIRE_DECODE) if diagnostics else None)

    async def test_blank_then_success_preserves_exact_wire_and_accounts_for_both_calls(self):
        async def respond(envelope):
            if len(self.called) == 1:
                return self.rejected(envelope)
            self.wires.append(build_wire(envelope).messages)
            return ProviderResponse("generated", ModelDraft('{"send":true,"message":"hello"}', ()),
                Amount(25, 60, input_tokens=10, output_tokens=15), "success")
        self.responses["primary"] = respond
        run = self.model_run()
        request = self.proactive_request()
        result = await self.gateway.generate(run, request)
        self.assertEqual(result.draft.text, '{"send":true,"message":"hello"}')
        self.assertEqual(len(self.called), 2)
        self.assertEqual(self.wires[0], self.wires[1])
        self.assertIs(self.called[0][1].original, self.called[1][1].original)
        self.assertEqual(self.called[0][1].max_output_tokens, self.called[1][1].max_output_tokens)
        self.assertEqual(self.called[0][1].task_prompt, self.called[1][1].task_prompt)
        calls = self.call_rows()
        self.assertEqual([c["position"] for c in calls], [0, 2])
        self.assertEqual([c["actual_tokens"] for c in calls], [17, 25])
        self.assertEqual([c["output_tokens"] for c in calls], [7, 15])
        self.assertEqual(result.call_id, calls[1]["call_id"])
        rounds = self.runtime.read_all("SELECT * FROM model_rounds ORDER BY round_no", ())
        self.assertEqual([r["call_id"] for r in rounds], [c["call_id"] for c in calls])
        self.assertEqual([r["state"] for r in rounds], ["final", "final"])
        self.assertEqual(rounds[0]["request_hash"], rounds[1]["request_hash"])
        self.assertEqual(self.runtime.read_one("SELECT count(*) AS n FROM model_requests", ())["n"], 1)
        self.assertTrue(all((a["spent_tokens"], a["held_tokens"]) == (42, 0) for a in self.accounts()))
        observed = self.gateway.observe(self.context, run.task.task_id).unwrap()["calls"]
        self.assertEqual(observed[0]["failure"]["code"], ModelFailure.WIRE_DECODE.value)
        self.assertEqual(observed[0]["response_diagnostics"]["finish_reason"], "stop")

    async def test_second_blank_is_final_and_keeps_both_receipts(self):
        async def respond(envelope):
            return self.rejected(envelope)
        self.responses["primary"] = respond
        with self.assertRaises(ModelOutputError) as raised:
            await self.gateway.generate(self.model_run(), self.proactive_request())
        calls = self.call_rows()
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(self.called), 2)
        self.assertEqual(self.wires[0], self.wires[1])
        self.assertEqual(raised.exception.call_id, calls[-1]["call_id"])
        self.assertEqual([c["actual_tokens"] for c in calls], [17, 17])
        self.assertTrue(all(a["spent_tokens"] == 34 and a["held_tokens"] == 0 for a in self.accounts()))
        rounds = self.runtime.read_all("SELECT call_id FROM model_rounds ORDER BY round_no", ())
        self.assertEqual([r["call_id"] for r in rounds], [c["call_id"] for c in calls])

    async def assert_single_failure(self, *, expected="model_output_invalid", **options):
        async def respond(envelope):
            return self.rejected(envelope, **options)
        self.responses["primary"] = respond
        with self.assertRaises(TaskError) as raised:
            await self.gateway.generate(self.model_run(), self.proactive_request())
        self.assertEqual(raised.exception.code, expected)
        self.assertEqual(len(self.called), 1)

    async def test_nonblank_invalid_json_is_not_retried(self):
        await self.assert_single_failure(text='{"send":')

    async def test_blank_without_confirmed_usage_is_not_retried(self):
        await self.assert_single_failure(known_usage=False, expected="model_usage_unknown")

    async def test_blank_without_verified_finish_metadata_is_not_retried(self):
        await self.assert_single_failure(diagnostics=False)

    async def test_length_ending_is_not_retried(self):
        self.responses["primary"] = ProviderResponse("generated",
            captured_output=CapturedModelOutput(" ", "length"), usage=Amount(17, 40), receipt_ref="truncated")
        with self.assertRaises(TaskError) as raised:
            await self.gateway.generate(self.model_run(), self.proactive_request())
        self.assertEqual(raised.exception.code, "model_output_truncated")
        self.assertEqual(len(self.called), 1)

    async def test_timeout_is_not_retried(self):
        async def respond(_):
            raise TimeoutError()
        self.responses["primary"] = respond
        with self.assertRaises(TaskError) as raised:
            await self.gateway.generate(self.model_run(), self.proactive_request())
        self.assertEqual(raised.exception.code, "model_timeout")
        self.assertEqual(len(self.called), 1)

    async def test_existing_fallback_chain_has_no_extra_blank_retry(self):
        self.patch_model(fallback="fallback")
        async def respond(envelope):
            return self.rejected(envelope)
        self.responses.update(primary=respond, fallback=respond)
        with self.assertRaises(ModelOutputError):
            await self.gateway.generate(self.model_run(), self.proactive_request())
        self.assertEqual([name for name, _ in self.called], ["primary", "fallback"])
        self.assertEqual([c["position"] for c in self.call_rows()], [0, 1])

    async def test_other_output_formats_are_not_retried(self):
        async def respond(envelope):
            return self.rejected(envelope)
        self.responses["primary"] = respond
        with self.assertRaises(ModelOutputError):
            await self.gateway.generate(self.model_run(), self.request())
        self.assertEqual(len(self.called), 1)
