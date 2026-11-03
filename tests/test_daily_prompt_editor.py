"""Daily prompt editing uses the real configuration and simulation boundaries."""

import unittest
from dataclasses import replace

from companion_v2.adapters.astrbot.model_wire import build_wire
from companion_v2.conversation.foreground import PromptMaterial
from companion_v2.configuration.models import ConfigLayer
from companion_v2.configuration.registry import FieldRegistry
from companion_v2.execution.models import encode
from companion_v2.model_gateway.management import ModelManagement
from companion_v2.model_gateway.routing import configuration_fields
from companion_v2.role_work.daily.inputs import DAILY_SCHEMA
from companion_v2.role_work.daily.service import KIND, daily_profile
from companion_v2.role_work.daily import prompts
from companion_v2.role_work.daily.simulation import simulate_daily_review
from tests.test_role_state import StateFixture
from tests import test_role_daily_review as support


class DailyPromptEditorTests(StateFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.profiles += (daily_profile("primary", max_output_tokens=100),)
        self.registry = FieldRegistry(version="daily.editor.tests", fields=configuration_fields(self.profiles))
        self.operations = 0

    ready = support.DailyReviewTests.ready
    switches = support.DailyReviewTests.switches
    reply = support.DailyReviewTests.reply
    daily_calls = support.DailyReviewTests.daily_calls

    def projection(self, layer="window"):
        manager = ModelManagement(self.app, self.task_context)
        return manager._project(self.core, self.app.router.profiles[KIND], ConfigLayer(layer))

    def patch(self, key, text=None, *, layer="window", token=None, reset=False):
        task = self.projection(layer)
        section = next(item for item in task["prompt_sections"] if item["key"] == key)
        self.operations += 1
        return ModelManagement(self.app, self.task_context).configure(self.core, {
            "kind": KIND, "section": key, "layer": layer, "operation_key": "daily-editor-" + str(self.operations),
            "edit_token": token or section.get("edit_token", task["edit_token"]),
            "changes": {} if reset else {"prompt": text}, "reset": ["prompt"] if reset else []})

    async def test_projection_contains_effective_sources_and_readonly_schema(self):
        await self.ready()
        task = self.projection()
        sections = {item["key"]: item for item in task["prompt_sections"]}
        self.assertEqual({key: sections[key]["prompt"] for key in prompts.DEFAULTS}, prompts.DEFAULTS)
        self.assertTrue(all(sections[key]["editable"] for key in (*prompts.DEFAULTS, "diary_style")))
        self.assertFalse(sections["output_contract"]["editable"])
        self.assertIn(encode(DAILY_SCHEMA), sections["output_contract"]["prompt"])
        self.assertEqual(self.patch("output_contract", "changed").failure.code, "model_prompt_section_readonly")
        self.assertEqual(task["prompt_assembly"]["order"][2]["sections"],
                         ["diary", "diary_style", "day_plan", "memory", "expression"])
        self.assertEqual(self.daily_calls(), [])

    async def test_edits_apply_to_preview_and_scheduled_call_without_schema_changes(self):
        app, context = await self.ready()
        for key in prompts.DEFAULTS:
            self.patch(key, "自定义每日任务：" + key).unwrap()
        self.patch("diary_style", "使用轻快的私人笔记口吻。").unwrap()
        app.life_runtime.for_context(context).refresh()
        preview = await simulate_daily_review(app, context, "原角色人格", attempt_id="daily-editor-preview")
        self.assertEqual(preview["state"], "passed", preview)
        preview_request = self.daily_calls()[-1].original
        formal = await app.daily_review.run(context, "原角色人格")
        self.assertEqual(formal["state"], "complete", formal)
        formal_request = self.daily_calls()[-1].original
        self.assertEqual(preview_request.instruction, formal_request.instruction)
        for key in prompts.DEFAULTS:
            if key == "expression":
                self.assertNotIn("自定义每日任务：" + key, formal_request.instruction)
            else:
                self.assertIn("自定义每日任务：" + key, formal_request.instruction)
        self.assertIn("使用轻快的私人笔记口吻。", formal_request.instruction)
        self.assertEqual(formal_request.response_schema_json, encode(DAILY_SCHEMA))
        self.assertIn(encode(DAILY_SCHEMA), formal_request.instruction)
        self.assertEqual(len(self.daily_calls()), 2)
        self.assertEqual(self.sent, [])

    async def test_revision_conflict_and_role_inheritance_do_not_lose_saved_prompts(self):
        await self.ready()
        self.patch("global", "角色默认", layer="role").unwrap()
        task = self.projection()
        section = next(item for item in task["prompt_sections"] if item["key"] == "global")
        self.assertEqual((section["prompt"], section["field"]["source"]), ("角色默认", "role"))
        old_token = section["edit_token"]
        self.patch("global", "窗口版本").unwrap()
        conflict = self.patch("diary", "过期草稿", token=old_token)
        self.assertEqual(conflict.failure.code, "configuration_revision_conflict")
        self.assertEqual(prompts.read(self.app, self.task_context)["global"], "窗口版本")
        self.assertEqual(prompts.read(self.app, self.task_context)["diary"], prompts.DIARY_TASK)
        self.patch("global", reset=True).unwrap()
        self.assertEqual(prompts.read(self.app, self.task_context)["global"], "角色默认")

    async def test_disabled_task_omits_its_custom_prompt(self):
        app, context = await self.ready(diary=False, memory=False)
        self.patch("diary", "不应使用的日记指令").unwrap()
        self.patch("memory", "不应使用的记忆指令").unwrap()
        result = await app.daily_review.run(context, "原角色人格")
        self.assertEqual(result["state"], "complete", result)
        request = self.daily_calls()[-1].original
        self.assertNotIn("不应使用", request.instruction)
        self.assertIn(prompts.PLAN_TASK, request.instruction)
        self.assertEqual(self.sent, [])

    async def test_default_assembly_retains_previous_instruction_text(self):
        app, context = await self.ready()
        await app.daily_review.run(context, "原角色人格")
        expected = (prompts.GLOBAL + "日记部分：\n" + prompts.DIARY_TASK
            + "\n\n次日日程部分：\n" + prompts.PLAN_TASK
            + "\n\n记忆整理部分：\n" + prompts.MEMORY_TASK
            + "\n" + prompts.output_contract())
        self.assertEqual(self.daily_calls()[-1].original.instruction, expected)
        envelope = self.daily_calls()[-1]
        # Supply the same separate history material as the AstrBot shared hook,
        # without accessing live conversations or making any external request.
        history = PromptMaterial("astrbot.conversation", "recent_private_dialogue",
            '[{"role":"user","content":"preset fixture"},{"role":"assistant","content":"history fixture"}]')
        request = replace(envelope.original, context=replace(envelope.original.context,
            materials=(*envelope.original.context.materials, history)))
        wire = build_wire(replace(envelope, original=request))
        sections = {item.key: item.default_prompt for item in prompts.sections()}
        system = wire.messages[0]["content"]
        pieces = (request.role_instruction, request.instruction, envelope.task_prompt,
                  sections["wire_contract"], sections["terminal_prompt_guard"])
        offsets = [system.index(piece) for piece in pieces]
        self.assertEqual(offsets, sorted(offsets))
        self.assertTrue(system.endswith(sections["terminal_prompt_guard"]))
        self.assertEqual([item["content"] for item in wire.messages[1:3]], ["preset fixture", "history fixture"])
        self.assertIn('"daily_parts"', wire.messages[3]["content"])
        self.assertNotIn(sections["automatic_context"], system)
        self.assertEqual(len(wire.messages), 4)

