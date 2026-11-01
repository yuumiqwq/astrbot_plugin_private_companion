"""The prompt editor changes the templates consumed by the original contact path."""

from datetime import datetime, timezone
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest

from companion_v2.configuration.models import ConfigLayer, ConfigTarget
from companion_v2.model_gateway.management import ModelManagement
from companion_v2.adapters.astrbot.model_wire import build_wire
from companion_v2.conversation.segmentation import SegmentationPolicy
from companion_v2.conversation.text_processing import ReplyTextPolicy
from companion_v2.proactive.prompt_catalog import presentation_instruction, segmentation_instruction
from companion_v2.proactive.configuration import GROUP
from companion_v2.proactive.preferences import ContactPreferences
from companion_v2.proactive.prompts import INSTRUCTION, contact_output, request_for
from companion_v2.proactive.prompt_templates import PROMPT_DEFAULTS
from tests.proactive_support import ContactFixture


class PromptTemplateTests(ContactFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.app = await self.enabled()
        self.context = self.contact_context()
        self.manager = ModelManagement(self.app, self.context)

    def task(self, manager=None):
        return next(task for task in (manager or self.manager).status(self.core, layer="window").unwrap()["tasks"]
                    if task["kind"] == "proactive.compose")

    def section(self, key, task=None):
        return next(section for section in (task or self.task())["prompt_sections"] if section["key"] == key)

    def patch(self, key, *, prompt=None, reset=False, token=None, operation="edit-template", operator=None):
        data = {"kind": "proactive.compose", "section": key, "operation_key": operation,
                "edit_token": token or self.section(key)["edit_token"],
                "changes": {} if reset else {"prompt": prompt}, "reset": ["prompt"] if reset else []}
        return self.manager.configure(operator or self.core, data)

    def composed(self, purpose="chat", *, dialogue=(), history=(), last_reply=None,
                 automatic_origin=None, preferences=None):
        opportunity = self.opportunity()
        job = SimpleNamespace(intent=self.intent(opportunity, purpose=purpose), context=self.context,
            automatic_origin=automatic_origin, interaction=None, role_instruction="角色自己的设定")
        return request_for(job, (), preferences or self.preferences(self.app), history,
                           dialogue=dialogue, last_reply=last_reply)

    async def test_edit_replaces_business_template_and_reaches_the_provider(self):
        original = self.section("expression")["prompt"]
        text = "自然展开一个有内容的感想，按转折分段。"
        self.patch("expression", prompt=text).unwrap()
        task = (await self.contact(self.app)).unwrap().task
        self.assertEqual(task.state, "succeeded")
        request = self.called[0].original
        self.assertIn(text, request.instruction)
        self.assertNotIn(original, request.instruction)
        self.assertIn(INSTRUCTION, request.instruction)
        self.assertEqual(request.role_instruction, "这是原角色人格，保持自然。")

    async def test_news_replaces_only_news_templates_and_expands_length(self):
        self.settings(self.app, {"proactive_intensity_body_chars": 280}).unwrap()
        self.patch("news_sharing", prompt="遇到新鲜事后自然分享亮点，再说具体感想。", operation="news-style").unwrap()
        self.patch("news_length", prompt="新闻全部正文 {chars} 字以内，保留自然段落。", operation="news-length").unwrap()
        news = self.composed("news").instruction
        chat = self.composed().instruction
        self.assertIn("新闻全部正文 280 字以内", news)
        self.assertNotIn(PROMPT_DEFAULTS["news_sharing_prompt"], news)
        self.assertNotIn("遇到新鲜事后自然分享亮点", chat)
        self.assertIn("无需压成一句", chat)
        self.assertNotIn("一句话能说完就只说一句", chat + news)

    async def test_empty_optional_template_keeps_the_selected_news_length(self):
        self.settings(self.app, {"proactive_intensity_body_chars": 280}).unwrap()
        self.patch("news_sharing", prompt="", operation="empty-news").unwrap()
        self.patch("news_length", prompt="News limit {chars}", operation="news-limit").unwrap()
        request = self.composed("news")
        self.assertIn("News limit 280", request.instruction)
        self.assertNotIn(PROMPT_DEFAULTS["news_sharing_prompt"], request.instruction)

    async def test_native_newline_output_preserves_send_and_skip_contract(self):
        request = self.composed()
        for key in ("output_contract", "terminal_prompt_guard"):
            instruction = dict(request.prompt_overrides)[key]
            self.assertIn(r"转义换行 \n", instruction)
            self.assertIn("每个非空行", instruction)
            self.assertNotIn("{{split_marker}}", instruction)
        text = "突然想到个事\n你写代码的时候会认真起名吗"
        self.assertEqual(contact_output(json.dumps({"decision": "send", "text": text})), (text, None))
        self.assertEqual(contact_output(json.dumps({"decision": "skip", "text": "",
            "skip_reason": "no_natural_opening"})), (None, None))

    async def test_recent_meal_answer_and_unanswered_contact_are_explicit_continuity_cues(self):
        now = self.clock.now()
        dialogue = ({"role": "user", "text": "吃了啦", "received_at": now.isoformat()},)
        request = self.composed("meal", dialogue=dialogue, automatic_origin={"meal_event": {"kind": "noon"}})
        self.assertIn("用户已答过同一餐次时不要再换说法确认", request.instruction)
        self.assertIn("contacts_since_last_input 只统计用户最近一次真实发言后已确认送达的主动联系数量", request.instruction)
        self.assertIn("角色自己的小幅状态变化也不逐条播报", request.instruction)
        self.assertIn("饭点只表示角色自己的时间背景，不证明对方时区、是否吃过或正在用餐", request.instruction)
        recent = [material for material in request.context.materials
                  if material.purpose == "recent_private_dialogue"]
        self.assertEqual(len(recent), 1)
        self.assertIn("吃了啦", recent[0].payload)

    async def test_full_exchange_is_shared_across_contact_purposes_without_inferred_labels(self):
        base = self.preferences(self.app)
        observed_at = datetime(2026, 9, 21, 5, 27, tzinfo=timezone.utc)
        preferences = ContactPreferences(base.snapshot, base.values, observed_at)
        dialogue = ({"role": "assistant", "text": "午饭吃得怎么样？", "delivered_at":
                     "2026-09-21T04:30:20+00:00", "_history_round": "lunch"},
                    {"role": "user", "text": "碗都洗好啦", "received_at":
                     "2026-09-21T04:30:54+00:00", "_history_round": "lunch"},
                    {"role": "assistant", "text": "那就好，下午慢慢来", "delivered_at":
                     "2026-09-21T04:31:29+00:00", "_history_round": "lunch"})

        for purpose in ("meal", "greeting", "care", "chat"):
            with self.subTest(purpose=purpose):
                request = self.composed(purpose, dialogue=dialogue, preferences=preferences)
                intent = next(item for item in request.context.materials if item.purpose == "contact_intent")
                self.assertNotIn("continuity_signal", json.loads(intent.payload))
                recent = [json.loads(item.payload) for item in request.context.materials
                          if item.purpose == "recent_private_dialogue"]
                self.assertEqual([item["text"] for item in recent[0]["messages"]],
                                 [item["text"] for item in dialogue])
                self.assertIn("这一连续性判断适用于所有主动目的", request.instruction)
                self.assertIn("独立于话题去重开关和时间窗", request.instruction)
                self.assertIn("后来的明确纠正、另一餐或新的情境则要重新理解", request.instruction)

    async def test_ambiguous_statements_keep_their_original_meaning_and_date(self):
        base = self.preferences(self.app)
        observed_at = datetime(2026, 9, 21, 5, 27, tzinfo=timezone.utc)
        preferences = ContactPreferences(base.snapshot, base.values, observed_at)
        for text in ("你吃饱了吗？", "还没吃", "我没吃饱", "他吃了，我还没顾上", "吃了？并没有哦",
                     "嗯，刚刚那份已经解决啦", "晚饭吃了", "昨天吃饱啦", "先去躺一会儿，不一定睡",
                     "任务都交了，等明天反馈", "我还没吃饭这几个字怎么翻译？"):
            with self.subTest(text=text):
                request = self.composed("chat", dialogue=({"role": "user", "text": text,
                    "received_at": "2026-09-21T04:30:54+00:00"},), preferences=preferences)
                self.assertNotIn("continuity_signal", {item.purpose for item in request.context.materials})
                recent = next(item for item in request.context.materials if item.purpose == "recent_private_dialogue")
                self.assertEqual(json.loads(recent.payload)["text"], text)

        yesterday = self.composed("chat", dialogue=({"role": "user", "text": "吃饱啦",
            "received_at": "2026-09-20T04:30:54+00:00"},), preferences=preferences)
        self.assertNotIn("continuity_signal", {item.purpose for item in yesterday.context.materials})
        recent = next(item for item in yesterday.context.materials if item.purpose == "recent_private_dialogue")
        self.assertEqual(json.loads(recent.payload)["received_at"], "2026-09-20T04:30:54+00:00")

    async def test_later_correction_follows_the_old_reply_and_proactive_message(self):
        request = self.composed("care", dialogue=(
            {"role": "user", "text": "刚才说的是昨晚，今天还没顾上", "received_at": "2026-09-21T05:05:00+00:00"},
            {"role": "assistant", "text": "午饭吃了吗？", "delivered_at": "2026-09-21T12:30:20+08:00",
             "_history_round": "lunch"},
            {"role": "user", "text": "吃饱啦", "received_at": "2026-09-21T04:30:54+00:00",
             "_history_round": "lunch"}),
            history=({"owner": GROUP, "text": "手边这本书真有趣", "delivered_at": "2026-09-21T04:55:00+00:00"},),
            last_reply={"text": "吃饱了就好呀", "delivered_at": "2026-09-21T04:31:29+00:00"})
        past = [json.loads(item.payload) for item in request.context.materials
                if item.purpose in ("recent_private_dialogue", "last_confirmed_reply", "dedup_candidate",
                                    "confirmed_background_message", "confirmed_reply_context")]
        self.assertEqual([item.get("text", [msg["text"] for msg in item.get("messages", ())]) for item in past],
                         [["午饭吃了吗？", "吃饱啦"], "吃饱了就好呀", "手边这本书真有趣", "刚才说的是昨晚，今天还没顾上"])

    async def test_relationship_inheritance_reset_and_window_isolation(self):
        target = ConfigTarget(self.context.scope.installation_id, ConfigLayer.RELATIONSHIP,
            self.context.role_binding.role, bot_id=self.context.scope.bot_id, subject=self.context.actor.subject)
        self.settings(self.app, {"news_sharing_prompt": "关系层的新闻表达"}, target=target, actor=self.core).unwrap()
        self.assertEqual(self.section("news_sharing")["field"]["source"], "relationship")
        self.patch("news_sharing", prompt="这个窗口的新闻表达", operation="window-news").unwrap()
        other = ModelManagement(self.app, self.other_window_context())
        self.assertEqual(self.section("news_sharing", self.task(other))["prompt"], "关系层的新闻表达")
        reset = self.patch("news_sharing", reset=True, operation="restore-news").unwrap()
        field = self.section("news_sharing", reset["task"])["field"]
        self.assertEqual((field["value"], field["source"], field["explicit"]), ("关系层的新闻表达", "relationship", False))

    async def test_conflicts_replay_validation_and_authorization(self):
        token = self.section("expression")["edit_token"]
        first = self.patch("expression", prompt="窗口表达", token=token).unwrap()
        replay = self.patch("expression", prompt="窗口表达", token=token).unwrap()
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["operation_id"], replay["operation_id"])
        conflict = self.patch("expression", prompt="过期草稿", token=token, operation="stale-template")
        self.assertEqual(conflict.failure.code, "configuration_revision_conflict")
        for value in ("删了变量", "{chars.__class__}", "{chars!r}", "{chars:10000}", "{unknown}", "{"):
            with self.subTest(value=value):
                invalid = self.patch("length", prompt=value, operation="invalid-template")
                self.assertEqual(invalid.failure.code, "invalid_contact_preference")
        forbidden = self.patch("expression", prompt="非管理员表达", operator=self.important, operation="forbidden")
        self.assertEqual(forbidden.failure.code, "core_access_required")
        self.patch("business", prompt="修改主动联系规则", operation="business").unwrap()
        self.assertIn("修改主动联系规则", self.composed().instruction)

    async def test_catalog_shows_complete_fixed_templates_without_private_messages(self):
        sections = self.task()["prompt_sections"]
        self.assertNotIn("coherence", {section["key"] for section in sections})
        self.assertNotIn("消息正文组织", str(sections))
        self.assertTrue(all(section["editable"] for section in sections))
        self.assertEqual(self.task()["prompt_assembly"]["purpose_count"], 15)
        self.assertEqual(self.section("business")["prompt"], INSTRUCTION)
        self.assertIn("准确来源、原日期和链接留到对方追问时再给", self.section("news_sharing")["prompt"])
        self.assertNotIn("角色自己的设定", str(sections))
        self.assertNotIn("用户先前说过", str(sections))

    async def test_global_purpose_task_and_wire_overrides_reach_the_original_provider(self):
        for key in ("business", "topic_continuity", "purpose.chat", "output", "terminal", "capability"):
            self.patch(key, prompt="edited-" + key, operation="edit-" + key).unwrap()
        task = self.task()
        self.manager.configure(self.core, {"kind": "proactive.compose", "layer": "window",
            "operation_key": "task-prompt", "edit_token": task["edit_token"],
            "changes": {"prompt": "edited-task"}, "reset": []}).unwrap()
        self.assertEqual((await self.contact(self.app)).unwrap().task.state, "succeeded")
        wire = build_wire(self.called[0])
        system = wire.messages[0]["content"]
        for key in ("business", "topic_continuity", "purpose.chat", "output", "terminal", "capability", "task"):
            self.assertIn("edited-" + key, system)
        self.assertNotIn(INSTRUCTION, system)
        calendar = replace(self.called[0], original=replace(self.called[0].original,
            output_format="calendar_planning", prompt_overrides=()))
        self.assertIn("末尾约束", build_wire(calendar).messages[0]["content"])
        self.assertNotIn("edited-terminal", build_wire(calendar).messages[0]["content"])

    async def test_role_and_window_edits_have_distinct_effective_scopes(self):
        role_task = next(task for task in self.manager.status(self.core, layer="role").unwrap()["tasks"]
                         if task["kind"] == "proactive.compose")
        section = self.section("purpose.chat", role_task)
        self.assertEqual(section["configuration_layer"], "role")
        self.manager.configure(self.core, {"kind": "proactive.compose", "section": "purpose.chat", "layer": "role",
            "operation_key": "role-purpose", "edit_token": section["edit_token"],
            "changes": {"prompt": "Role-wide chat prompt"}, "reset": []}).unwrap()
        self.patch("purpose.chat", prompt="Window chat prompt", operation="window-purpose").unwrap()
        other = ModelManagement(self.app, self.other_window_context())
        self.assertEqual(self.section("purpose.chat", self.task(other))["prompt"], "Role-wide chat prompt")
        self.assertEqual(self.section("purpose.chat")["prompt"], "Window chat prompt")
        self.patch("purpose.chat", reset=True, operation="inherit-role-purpose").unwrap()
        self.assertEqual(self.section("purpose.chat")["prompt"], "Role-wide chat prompt")

    async def test_production_location_source_uses_the_edited_origin_prompt(self):
        self.patch("origin.reality_location_event", prompt="Use the reported place only", operation="location-origin").unwrap()
        reference = replace(self.source(self.app), owner="reality.location")
        record = SimpleNamespace(reference=reference, payload='{"place":"authorized place"}')
        job = SimpleNamespace(intent=self.intent(self.opportunity(), purpose="environment"), context=self.context,
            automatic_origin=None, interaction=None, role_instruction="Current persona")
        request = request_for(job, (record,), self.preferences(self.app))
        self.assertIn("Use the reported place only", request.instruction)
        self.assertNotIn("Use the reported place only", self.composed("environment").instruction)

    async def test_diagnostic_variants_use_their_saved_templates(self):
        for key in ("diagnostic_synthetic", "diagnostic_source", "diagnostic_plain", "diagnostic_image", "diagnostic_presentation"):
            self.patch(key, prompt="edited-" + key, operation=key).unwrap()
        for mode in ("synthetic", "source", "plain"):
            job = SimpleNamespace(intent=self.intent(self.opportunity()), context=self.context,
                automatic_origin=None, interaction=None, role_instruction="Current persona", administrator_test=True,
                diagnostic_origin={"_diagnostic": {"route_id": "synthetic_event" if mode == "synthetic" else "relationship_event"}},
                diagnostic_source_backed=mode == "source", diagnostic_image_intent="happy image")
            request = request_for(job, (), self.preferences(self.app))
            for key in ("diagnostic_" + mode, "diagnostic_image", "diagnostic_presentation"):
                self.assertIn("edited-" + key, request.instruction)

    async def test_presentation_defaults_and_proactive_overrides_preserve_switches(self):
        preferences = self.preferences(self.app)
        for mode in ("free", "constraint", "guided"):
            for enabled in (False, True):
                policy = ReplyTextPolicy(prompt_style_mode=mode, enable_humanized_expression=enabled,
                    humanized_expression_prompt="Existing humanized prompt", style_prompt="Existing style")
                self.assertEqual(presentation_instruction(preferences, policy), policy.instruction)
        policy = SegmentationPolicy(llm_controlled=True, prompt="Existing segmentation")
        self.assertEqual(segmentation_instruction(preferences, policy), policy.instruction)
        self.patch("segmentation", prompt="New proactive segmentation", operation="segmentation").unwrap()
        self.assertEqual(segmentation_instruction(self.preferences(self.app), policy), "New proactive segmentation")
        self.assertEqual(segmentation_instruction(self.preferences(self.app), replace(policy, enabled=False)), "")
        self.patch("humanized", prompt="New proactive expression", operation="humanized").unwrap()
        policy = ReplyTextPolicy(enable_humanized_expression=True)
        self.assertIn("New proactive expression", presentation_instruction(self.preferences(self.app), policy))

    async def test_editor_projects_inherited_presentation_text_used_by_production(self):
        group = "conversation.presentation"
        target = ConfigTarget(self.context.scope.installation_id, ConfigLayer.ROLE, self.context.role_binding.role,
                              bot_id=self.context.scope.bot_id)
        snapshot = self.app.configuration.read(self.core, target=target, group_id=group,
            observed_at=self.clock.now()).unwrap()
        self.app.configuration.patch(self.core, target=target, group_id=group,
            expected_edit_token=snapshot.edit_token,
            changes={group + ".prompt": "Shared custom segmentation",
                     group + ".humanized_expression_prompt": "Shared custom expression"},
            operation=self.operation(target.authorization_scope, owner=group), observed_at=self.clock.now()).unwrap()
        self.assertEqual(self.section("segmentation")["prompt"], "Shared custom segmentation")
        self.assertEqual(self.section("humanized")["prompt"], "Shared custom expression")
        self.assertEqual(self.section("humanized")["field"]["source"], "presentation")
        self.assertEqual(segmentation_instruction(self.preferences(self.app),
            SegmentationPolicy(llm_controlled=True, prompt="Shared custom segmentation")),
            self.section("segmentation")["prompt"])
        self.patch("humanized", prompt="Proactive-only expression", operation="local-humanized").unwrap()
        self.patch("humanized", reset=True, operation="reset-humanized").unwrap()
        self.assertEqual(self.section("humanized")["prompt"], "Shared custom expression")
