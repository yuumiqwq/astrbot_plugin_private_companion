"""Exercise the installed AstrBot _query without making a network request."""

from copy import deepcopy
from dataclasses import replace
import inspect
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from openai.resources.chat.completions import AsyncCompletions
from openai.types.chat import ChatCompletion

from companion_v2.conversation.foreground import PromptMaterial
from companion_v2.model_gateway.failures import ModelFailure
from companion_v2.proactive.response_schema import contact_schema, final_task_prompt
from tests.native_adapter_support import FakeProvider, NativeFixture


class SdkClient:
    def __init__(self, state, max_retries=0):
        self.state, self.max_retries = state, max_retries
        self.chat = NS(completions=NS(create=self.create))

    def with_options(self, *, max_retries):
        return SdkClient(self.state, max_retries)

    async def create(self, **payload):
        self.state.calls.append(deepcopy(payload))
        return ChatCompletion(id="local-sdk-fixture", created=1, model="fixture", object="chat.completion",
            choices=[{"index": 0, "finish_reason": "stop",
                      "message": {"role": "assistant", "content": self.state.text}}],
            usage={"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40})


class ProactiveNativeContractTests(NativeFixture, unittest.IsolatedAsyncioTestCase):
    async def test_task_effort_and_output_budget_reach_sdk_without_changing_native_defaults(self):
        from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial

        state = NS(calls=[], text='{"decision":"skip","text":"","skip_reason":"no_natural_opening"}')
        self.native.provider.provider_config.update(provider="deepseek", custom_extra_body={
            "temperature": 0.7, "thinking": {"type": "disabled"}, "reasoning_effort": "low"})
        self.native.provider.client = SdkClient(state)
        self.native.provider.default_params = inspect.signature(AsyncCompletions.create).parameters.keys()
        native_defaults = deepcopy(self.native.provider.provider_config)
        self.native_model = self.make_model()
        self.native_model.maximum_output_tokens = 8192
        envelope = await self.envelope(reasoning_effort="max", max_output_tokens=8192)
        request = replace(envelope.original, output_format="proactive_contact", response_format="json_object",
            response_schema_json=json.dumps(contact_schema("companion")))
        with patch.object(FakeProvider, "_query", ProviderOpenAIOfficial._query), \
             patch.object(FakeProvider, "_apply_provider_specific_request_overrides",
                          ProviderOpenAIOfficial._apply_provider_specific_request_overrides, create=True), \
             patch.object(FakeProvider, "_sanitize_assistant_messages",
                          staticmethod(ProviderOpenAIOfficial._sanitize_assistant_messages), create=True):
            result = await self.native_model.generate(replace(envelope, original=request))
            self.assertEqual(result.status, "generated")
            selected = state.calls[-1]
            self.assertEqual(selected["max_tokens"], 8192)
            self.assertEqual(selected["reasoning_effort"], "max")
            self.assertEqual(selected["extra_body"]["reasoning_effort"], "max")
            self.assertEqual(selected["extra_body"]["thinking"], {"type": "enabled"})
            self.assertEqual(selected["response_format"], {"type": "json_object"})
            self.assertEqual(self.native.provider.provider_config, native_defaults)
            inherited = await self.native_model.generate(replace(envelope, original=request, reasoning_effort=""))
            self.assertEqual(inherited.status, "generated")
            self.assertNotIn("reasoning_effort", state.calls[-1])
            self.assertEqual(state.calls[-1]["extra_body"], native_defaults["custom_extra_body"])
            self.assertEqual(self.native.provider.provider_config, native_defaults)

    async def test_final_sdk_payload_keeps_json_mode_and_schema_after_shared_history(self):
        from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial

        state = NS(calls=[], text="")
        self.native.provider.client = SdkClient(state)
        self.native.provider.default_params = inspect.signature(AsyncCompletions.create).parameters.keys()
        self.native_model = self.make_model()
        envelope = await self.envelope()
        history = [{"role": "user", "content": "Shared question"},
                   {"role": "assistant", "content": "Shared answer"}]
        for mode in ("companion", "simple"):
            with self.subTest(mode=mode):
                schema = contact_schema(mode)
                state.text = ('{"send":false,"message":""}' if mode == "simple" else
                              '{"decision":"skip","text":"","skip_reason":"no_natural_opening"}')
                request = replace(envelope.original, output_format="proactive_contact", proactive_mode=mode,
                    response_format="json_object", response_schema_json=json.dumps(schema),
                    prompt_overrides=(("output_contract", "Companion output sentinel"),
                                      ("terminal_prompt_guard", "Companion terminal sentinel")),
                    context=replace(envelope.original.context, materials=(
                        PromptMaterial("role.state", "state", "Companion material sentinel"),
                        PromptMaterial("astrbot.conversation", "recent_private_dialogue", json.dumps(history)),)))
                with patch.object(FakeProvider, "_query", ProviderOpenAIOfficial._query), \
                     patch.object(FakeProvider, "_apply_provider_specific_request_overrides",
                                  ProviderOpenAIOfficial._apply_provider_specific_request_overrides, create=True), \
                     patch.object(FakeProvider, "_sanitize_assistant_messages",
                                  staticmethod(ProviderOpenAIOfficial._sanitize_assistant_messages), create=True):
                    result = await self.native_model.generate(replace(envelope, original=request))
                self.assertEqual(result.status, "generated")
                sent = state.calls[-1]
                self.assertEqual(sent["response_format"], {"type": "json_object"})
                self.assertNotIn("response_format", sent["extra_body"])
                if mode == "simple":
                    self.assertEqual(sent["messages"], [
                        {"role": "system", "content": request.role_instruction}, *history,
                        {"role": "user", "content": request.instruction}])
                else:
                    self.assertEqual(sent["messages"][-4:-2], history)
                    self.assertIn("materials", json.loads(sent["messages"][-2]["content"]))
                    task = sent["messages"][-1]
                    self.assertEqual(task, {"role": "user", "content": final_task_prompt(mode, schema)})
                    self.assertIn(state.text, task["content"])

    async def test_blank_provider_output_stays_rejected_and_reaches_diagnostic_sink(self):
        envelope = await self.envelope()
        raw = " \n\t" * 7
        from tests.native_adapter_support import completion
        async def respond(*_):
            return completion(raw)
        self.native.provider.client.state.behavior = respond
        rejected = []
        request = replace(envelope.original, output_format="proactive_contact", response_format="json_object")
        result = await self.native_model.generate(replace(envelope, original=request,
            record_rejected_output=lambda *details: rejected.append(details)))
        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.failure, ModelFailure.WIRE_DECODE)
        self.assertIsNone(result.draft)
        self.assertEqual(rejected[0][0].text, raw)
        self.assertIn("only whitespace", rejected[0][1])
        self.assertEqual(rejected[0][2], "JSONDecodeError")
