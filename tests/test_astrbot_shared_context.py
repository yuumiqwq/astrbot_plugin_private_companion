from dataclasses import dataclass, replace
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from astrbot_host.shared_context import AstrBotSharedContext, native_newline_reply_instruction
from companion_v2.conversation.foreground import PromptMaterial, SealedForegroundContext
from companion_v2.foundation.refs import ScopeKind
from tests.proactive_support import ContactFixture
from tests.test_role_tool_loop import answered


@dataclass(frozen=True)
class Request:
    event: object
    role_binding: object
    context: object
    role_instruction: str = "previous persona"
    role_guard_instruction: str | None = None
    instruction: str = "Return the contact decision."
    response_schema_json: str = '{"type":"object"}'
    output_format: str = ""
    proactive_mode: str = "companion"
    host_provider_id: str | None = None


class SharedHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.history = [
            {
                "role": "user" if i % 2 == 0 else "assistant",
                "content": str(i) + "x" * 200,
            }
            for i in range(100)
        ]
        self.manager = NS(
            shared_history=NS(settings=Mock(return_value={})),
            get_curr_conversation_id=AsyncMock(return_value="current"),
            get_conversation=AsyncMock(
                return_value=NS(history=json.dumps(self.history))
            ),
        )
        self.native = NS(
            conversation_manager=self.manager,
            get_provider_by_id=Mock(return_value=object()),
            prepare_session_request=AsyncMock(),
        )
        self.bridge = NS(
            verified_persona=AsyncMock(
                return_value={"prompt": "current persona and examples"}
            )
        )
        self.loader = AstrBotSharedContext(self.native, self.bridge, "chat")
        self.request = Request(
            NS(scope=NS(kind=ScopeKind.PRIVATE, platform="qq", native_scope_id="peer")),
            object(),
            SealedForegroundContext(
                (
                    PromptMaterial(
                        "conversation.private_history",
                        "recent_private_dialogue",
                        "old plugin history",
                    ),
                    PromptMaterial(
                        "delivery.history", "dedup_candidate", "sent candidate"
                    ),
                    PromptMaterial("proactive.contact", "contact_intent", "a thought"),
                ),
                (),
                (),
            ),
        )

    def test_host_paragraphs_are_shared_without_splitting_short_sentences(self):
        settings = {
            "enable": True,
            "split_mode": "regex",
            "regex": r"\S[\s\S]*?(?=(?:\r?\n[^\S\r\n]*){2,}|\Z)",
        }
        self.native.get_config = Mock(
            return_value={"platform_settings": {"segmented_reply": settings}}
        )
        self.assertEqual(
            self.loader.split_reply(self.request.event, "First. Second.\n\nThird."),
            ("First. Second.", "Third."),
        )
        settings["enable"] = False
        self.assertEqual(
            self.loader.split_reply(self.request.event, "First.\n\nSecond."),
            ("First.\n\nSecond.",),
        )

    def test_host_newline_rule_separates_single_and_blank_lines(self):
        self.native.get_config = Mock(
            return_value={
                "platform_settings": {
                    "segmented_reply": {
                        "enable": True,
                        "split_mode": "regex",
                        "regex": r"[^\r\n]+",
                    }
                }
            }
        )
        self.assertEqual(
            self.loader.split_reply(
                self.request.event, "First. Second.\nThird.\r\n\n  \nFourth."
            ),
            ("First. Second.", "Third.", "Fourth."),
        )

    async def test_proactive_json_uses_native_newlines_and_skip_has_no_body(self):
        from companion_v2.proactive.prompts import contact_output

        settings = {"enable": True, "split_mode": "regex", "regex": r"[^\r\n]+"}
        self.native.get_config = Mock(return_value={"platform_settings": {"segmented_reply": settings}})
        request = replace(self.request, output_format="proactive_contact")
        result = await self.loader(request)
        self.assertIn("每个非空行", result.instruction)
        self.assertIn("\\n", result.instruction)
        self.assertNotIn("PRIVATE_COMPANION_SPLIT", result.instruction)
        self.assertIn(result.instruction, self.native.prepare_session_request.await_args.kwargs["response_instruction"])
        body, _ = contact_output(json.dumps({"decision": "send", "text": "第一条\n\n第二条。短句仍在同一条。"}))
        self.assertEqual(self.loader.split_reply(request.event, body), ("第一条", "第二条。短句仍在同一条。"))
        self.assertEqual(contact_output('{"decision":"skip","text":""}'), (None, None))
        settings["enable"] = False
        self.assertEqual(native_newline_reply_instruction(self.native, "qq:FriendMessage:peer"), "")
        settings["enable"] = True
        settings["regex"] = r".+"
        self.assertEqual(native_newline_reply_instruction(self.native, "qq:FriendMessage:peer"), "")

    async def test_background_preparation_uses_the_current_native_chat_provider(self):
        self.native.get_current_chat_provider_id = AsyncMock(return_value="current-chat-provider")
        provider = object()
        self.native.get_provider_by_id = Mock(return_value=provider)
        result = await self.loader(self.request)
        self.native.get_current_chat_provider_id.assert_awaited_once_with("qq:FriendMessage:peer")
        self.native.get_provider_by_id.assert_called_once_with("current-chat-provider")
        self.assertEqual(result.host_provider_id, "current-chat-provider")
        self.assertIs(self.native.prepare_session_request.await_args.args[1], provider)

    async def test_simple_json_keeps_shared_history_and_uses_message_send_schema(self):
        from companion_v2.proactive.simple_prompts import parse_contact_output

        settings = {"enable": True, "split_mode": "regex", "regex": r"[^\r\n]+"}
        self.native.get_config = Mock(return_value={"platform_settings": {"segmented_reply": settings}})
        request = replace(self.request, output_format="proactive_contact", proactive_mode="simple",
                          instruction="Short contact task.")
        result = await self.loader(request)
        self.assertEqual(result.instruction, request.instruction)
        self.assertEqual(result.role_instruction, "current persona and examples")
        self.assertEqual(len(result.context.materials), 1)
        prepared = self.native.prepare_session_request.await_args
        self.assertEqual(prepared.args[2].prompt, request.instruction)
        self.assertEqual(prepared.kwargs["response_instruction"], "")
        history = next(item for item in result.context.materials if item.owner == "astrbot.conversation")
        self.assertEqual(json.loads(history.payload), self.history)
        data, body, _ = parse_contact_output('{"send":true,"message":"First\\n\\nSecond"}', mode="simple")
        self.assertEqual(self.loader.split_reply(request.event, body), ("First", "Second"))
        self.assertEqual(data["decision"], "send")

    async def test_all_background_modes_render_real_qq_time_without_dating_examples(self):
        from astrbot.core.session_context import prepare_request_context

        examples = [{"role": "user", "content": "Preset question", "_no_save": True},
                    {"role": "assistant", "content": "Preset answer", "_no_save": True}]
        history = [{"role": "assistant", "content": "晚安姐姐", "_qq_time": 1793481687, "_qq_sequence": 672}]
        self.manager.get_conversation.return_value = NS(history=json.dumps(history))
        self.bridge.verified_persona.return_value = {
            "prompt": "OvO", "system_prompt": "OvO", "begin_dialogs": examples}
        self.native.get_config = Mock(return_value={"timezone": "Asia/Shanghai"})
        provider = NS(provider_config={"max_context_tokens": 128000}, get_model=lambda: "offline-test")
        self.native.get_provider_by_id.return_value = provider
        async def prepare(*args, **kwargs):
            await prepare_request_context(self.native, *args, **kwargs)
        self.native.prepare_session_request.side_effect = prepare
        for output, mode in (("proactive_contact", "simple"), ("proactive_contact", "companion"),
                             ("role_daily_review", "companion")):
            with self.subTest(output=output, mode=mode):
                result = await self.loader(replace(self.request, output_format=output, proactive_mode=mode))
                messages = json.loads(result.context.materials[-1].payload)
                dialogue = [m for m in messages if m["role"] in {"user", "assistant"}]
                self.assertEqual([m["content"] for m in dialogue[:2]], ["Preset question", "Preset answer"])
                self.assertEqual(messages[-1]["content"],
                    "[QQ消息发送时间：2026-11-01 05:21:27]\n晚安姐姐")
                self.assertEqual(sum(m["content"].count("Asia/Shanghai") for m in messages), 1)
                self.assertEqual(json.loads(self.manager.get_conversation.return_value.history), history)

    async def test_simple_does_not_collect_companion_materials_and_budgets_native_presets(self):
        examples = [{"role": "user", "content": "Preset question", "_no_save": True},
                    {"role": "assistant", "content": "Preset answer", "_no_save": True}]
        self.bridge.verified_persona.return_value = {
            "prompt": "persona with examples", "system_prompt": "exact persona",
            "begin_dialogs": examples}
        self.loader.application = object()
        request = replace(self.request, output_format="proactive_contact", proactive_mode="simple",
                          instruction="Saved prompt unchanged.")
        with patch("astrbot_host.shared_materials.common_materials") as collect:
            result = await self.loader.for_task(request, object())
        collect.assert_not_called()
        prepared = self.native.prepare_session_request.await_args
        self.assertEqual(prepared.args[2].system_prompt, "exact persona")
        self.assertEqual(prepared.args[2].contexts, examples + self.history)
        self.assertEqual(prepared.args[2].prompt, request.instruction)
        self.assertEqual(prepared.kwargs["response_instruction"], "")
        self.assertEqual(result.instruction, request.instruction)
        self.assertEqual(len(result.context.materials), 1)
        expected = [{"role": item["role"], "content": item["content"]} for item in examples]
        self.assertEqual(json.loads(result.context.materials[0].payload), expected + self.history)

    async def test_host_history_has_no_plugin_round_or_character_cap(self):
        result = await self.loader(self.request)
        history = next(
            item
            for item in result.context.materials
            if item.owner == "astrbot.conversation"
        )
        self.assertEqual(json.loads(history.payload), self.history)
        self.assertGreater(len(history.payload), 8192)
        self.manager.get_conversation.assert_awaited_once_with(
            "qq:FriendMessage:peer", "current"
        )
        self.assertEqual(result.role_instruction, "current persona and examples")
        self.assertEqual(result.role_guard_instruction, result.role_instruction)
        self.assertEqual(result.host_provider_id, "chat")
        self.assertFalse(
            any(
                item.owner == "conversation.private_history"
                for item in result.context.materials
            )
        )
        self.assertTrue(
            any(item.purpose == "dedup_candidate" for item in result.context.materials)
        )
        call = self.native.prepare_session_request.await_args
        self.assertIn(self.request.instruction, call.kwargs["response_instruction"])

    async def test_native_compaction_result_is_used(self):
        async def compact(_umo, _provider, request, **_kwargs):
            request.contexts = [
                {"role": "system", "content": "older summary"},
                self.history[-1],
                {"role": "checkpoint", "content": "internal"},
            ]

        self.native.prepare_session_request.side_effect = compact
        result = await self.loader(self.request)
        material = result.context.materials[-1]
        self.assertEqual(
            json.loads(material.payload),
            [{"role": "system", "content": "older summary"}, self.history[-1]],
        )

    async def test_unconfigured_session_keeps_original_request(self):
        self.manager.shared_history.settings.return_value = None
        self.assertIs(await self.loader(self.request), self.request)
        self.manager.get_conversation.assert_not_awaited()
        self.bridge.verified_persona.assert_not_awaited()

    async def test_presets_use_native_roles_and_share_the_history_budget(self):
        examples = [
            {"role": "user", "content": "Example question", "_no_save": True},
            {"role": "assistant", "content": "Example answer", "_no_save": True},
        ]
        self.bridge.verified_persona.return_value = {
            "prompt": "persona with serialized examples",
            "system_prompt": "exact persona",
            "begin_dialogs": examples,
        }
        result = await self.loader(self.request)
        request = self.native.prepare_session_request.await_args.args[2]
        self.assertEqual(request.system_prompt, "exact persona")
        self.assertEqual(request.contexts, examples + self.history)
        self.assertEqual(result.role_instruction, "exact persona")
        self.assertEqual(result.role_guard_instruction, "persona with serialized examples")
        messages = json.loads(result.context.materials[-1].payload)
        self.assertEqual(messages[:2], [{"role": item["role"], "content": item["content"]}
                                        for item in examples])
        self.assertEqual(messages[2:], self.history)

    async def test_repreparing_replaces_the_previous_shared_history(self):
        prepared = await self.loader(self.request)
        refreshed = await self.loader(prepared)
        self.assertEqual(sum(item.owner == "astrbot.conversation"
                             for item in refreshed.context.materials), 1)

    async def test_native_preset_guidance_is_shared_once_in_all_background_modes(self):
        examples = [{"role": "system", "content": "Use assistant examples for style.", "_no_save": True},
                    {"role": "user", "content": "Preset question", "_no_save": True},
                    {"role": "assistant", "content": "Preset answer", "_no_save": True}]
        self.bridge.verified_persona.return_value = {
            "prompt": "persona and examples", "system_prompt": "exact persona", "begin_dialogs": examples}
        for output, mode in (("proactive_contact", "simple"), ("proactive_contact", "companion"),
                             ("role_daily_review", "companion")):
            with self.subTest(output=output, mode=mode):
                request = replace(self.request, output_format=output, proactive_mode=mode)
                result = await self.loader(await self.loader(request))
                budgeted = self.native.prepare_session_request.await_args.args[2]
                self.assertEqual(budgeted.contexts, examples + self.history)
                self.assertEqual(result.role_instruction, "exact persona")
                history = json.loads(result.context.materials[-1].payload)
                self.assertEqual(history[:3], [{"role": d["role"], "content": d["content"]} for d in examples])
                self.assertEqual(sum(m["content"] == examples[0]["content"] for m in history), 1)

    async def test_new_native_session_uses_empty_history_with_the_current_persona(self):
        self.manager.get_curr_conversation_id.return_value = None
        result = await self.loader(self.request)
        self.assertEqual(result.role_instruction, "current persona and examples")
        self.assertEqual(json.loads(result.context.materials[-1].payload), [])
        self.manager.get_conversation.assert_not_awaited()
        self.native.prepare_session_request.assert_awaited_once()

    async def test_history_failure_is_reported_without_using_stale_plugin_history(self):
        self.manager.get_conversation.side_effect = RuntimeError("history unavailable")
        with self.assertRaisesRegex(RuntimeError, "history unavailable"):
            await self.loader(self.request)


class ProactiveSharedContextTests(ContactFixture, unittest.IsolatedAsyncioTestCase):
    async def test_host_paragraphs_become_distinct_delivery_parts(self):
        app = await self.enabled()
        app.proactive.output_segments = lambda context, body: tuple(body.split("\n\n"))

        async def generate(_envelope):
            return answered('{"decision":"send","text":"First. Second.\\n\\nThird."}')

        self.model_behavior = generate
        task = (await self.contact(app)).unwrap().task
        observation = app.proactive.observe(
            self.contact_context(), task.task_id
        ).unwrap()
        self.assertEqual(observation["stage_metrics"]["delivery"]["expected_parts"], 2)

    async def test_prepared_host_context_reaches_model_and_skip_sends_nothing(self):
        app = await self.enabled()
        seen = []

        async def prepare(request):
            seen.append(request)
            material = PromptMaterial(
                "astrbot.conversation", "recent_private_dialogue", "host history"
            )
            return replace(
                request,
                context=replace(
                    request.context, materials=(*request.context.materials, material)
                ),
            )

        async def generate(envelope):
            self.assertTrue(
                any(
                    m.owner == "astrbot.conversation"
                    for m in envelope.original.context.materials
                )
            )
            return answered('{"decision":"skip","text":"","skip_reason":"repetitive"}')

        app.proactive.request_context = prepare
        self.model_behavior = generate
        task = (await self.contact(app)).unwrap().task
        observation = app.proactive.observe(
            self.contact_context(), task.task_id
        ).unwrap()
        self.assertEqual(len(seen), 1)
        self.assertTrue(observation["skipped"])
        self.assertEqual(observation["stage_metrics"]["delivery"]["expected_parts"], 0)

    async def test_administrator_diagnostic_uses_the_configured_host_context(self):
        app = await self.enabled()
        app.proactive.request_context = AsyncMock(
            side_effect=lambda request: replace(
                request, role_instruction="verified host persona"
            )
        )
        result = await self.contact(app, administrator_test=True)
        self.assertIsNone(result.failure)
        app.proactive.request_context.assert_awaited_once()
        self.assertEqual(
            self.called[-1].original.role_instruction, "verified host persona"
        )
