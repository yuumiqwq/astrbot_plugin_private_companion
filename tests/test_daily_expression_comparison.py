"""Approved examples guide incremental, reviewable expression learning."""

import json
import unittest

from companion_v2.configuration.management import ExpressionLearningManagement, RoleProfileManagement
from companion_v2.execution.models import TaskError, encode
from companion_v2.expression.configuration import (
    GROUP, PUBLIC_FIELDS, RETIRED_PROFILE_SOURCE_FIELDS, ExpressionConfigurationOwner,
    configuration_fields, read_settings, target_for,
)
from companion_v2.expression.style_management import ExpressionStyleManagement
from companion_v2.role_work.daily import expression
from companion_v2.role_work.daily.simulation import simulate_daily_review
from tests import test_role_daily_review as daily_tests
from tests.test_role_tool_loop import answered


class ComparisonSource(daily_tests.SharedContext):
    def __init__(self):
        self.approved = [{"role": "user", "content": "一起看看吗？"},
                         {"role": "assistant", "content": "可以呀。\n\n我们慢慢看。"}]
        self.dialogue = [
            {"role": "assistant", "content": "我们慢慢看。", "_qq_sequence": 10},
            {"role": "user", "content": "这题呢？", "_qq_sequence": 11},
            {"role": "assistant", "content": "先读题目。", "_qq_sequence": 12},
        ]

    async def expression_learning_source(self, context):
        return {"source_key": "onebot:FriendMessage:owner", "conversation_id": "conversation",
                "approved_dialogue": self.approved, "dialogue": self.dialogue}


class DailyExpressionTests(daily_tests.DailyReviewTests):
    async def comparison_ready(self, **parts):
        app, context = await self.ready(**parts)
        self.source = ComparisonSource()
        app.gateway.request_context = self.source
        owner, fields, target = ExpressionConfigurationOwner(app.runtime), configuration_fields(), target_for(context)
        current = owner.read(target, fields)
        owner.write(target, fields, changes={GROUP + ".enabled": True,
            GROUP + ".daily_comparison_enabled": True, GROUP + ".private_learning_enabled": False},
            reset=(), expected_revision=current.revision, observed_at=self.clock.now())
        self.model_behavior = self.comparison_reply
        return app, context

    async def comparison_reply(self, envelope):
        response = await daily_tests.DailyReviewTests.reply(self, envelope)
        if envelope.original.output_format != "role_daily_review":
            return response
        result = json.loads(response.draft.text)
        material = next((item for item in envelope.original.context.materials
                         if item.purpose == "expression_comparison"), None)
        result["expression"] = None
        if material is not None:
            snapshot = json.loads(material.payload)
            approved = next(key for key in snapshot["handles"] if key.startswith("preset:"))
            result["expression"] = {"summary": "参考已认可示例的接话方式。", "signals": [{
                "dimension": "sentence_rhythm", "observation": "先回应邀请，再补充下一步。",
                "proposal": "回应共同活动时，先用简短应答接住，再接一句具体行动。",
                "confidence": 0.7, "evidence_handles": [approved],
                "approved_quotes": [{"evidence_handle": approved, "quote": "可以呀。"}],
                "expression": {"kind": "grammar", "situation": "回应共同活动邀请时",
                    "pattern": "先简短答应，再接一句具体行动", "avoid": "需要拒绝邀请时"},
                "applicability": {"channels": ["private", "proactive"], "relationship_stages": ["any"],
                    "emotion_gates": ["any"], "intent": "any", "keywords": []},
            }]}
        return answered(encode(result))

    async def test_comparison_joins_one_daily_call_and_waits_for_adoption(self):
        app, context = await self.comparison_ready()
        result = await app.daily_review.run(context, "原角色人格", expression_only=True, operation_key="manual-adoption")
        self.assertEqual(result["state"], "complete", result)
        self.assertEqual(len(self.daily_calls()), 1)
        self.assertEqual(result["receipts"]["expression"]["pending_rules"], 1)
        manager = ExpressionStyleManagement(app)
        pending = manager.list(operator=self.core, context=context).unwrap()["items"]
        self.assertEqual(len(pending), 1, pending)
        self.assertEqual(pending[0]["source_kind"], "preset_comparison")
        self.assertIn("adopt", pending[0]["actions"])
        policy = read_settings(app.runtime, context)
        before = app.expression_style_context.capture(context, self.clock.now(), policy=policy)
        self.assertEqual(before["references"], [])
        manager.command(operator=self.core, context=context, signal_id=pending[0]["signal_id"], action="adopt",
                        view_token=pending[0]["view_token"], operation_key="adopt-comparison").unwrap()
        after = app.expression_style_context.capture(context, self.clock.now(), policy=policy)
        self.assertEqual(len(after["references"]), 1)
        self.assertTrue(app.expression_style_context.contributions(after, context, self.clock.now(), policy=policy))

    async def test_manual_and_scheduled_runs_share_cursor_and_retain_all_approved_examples(self):
        app, context = await self.comparison_ready(diary=False, plan=False, memory=False)
        first = await app.daily_review.run(context, "原角色人格", expression_only=True, operation_key="manual-first")
        self.assertEqual(first["state"], "complete", first)
        self.assertEqual(len(self.daily_calls()), 1)
        scheduled = await self.run_daily()
        self.assertEqual(scheduled["state"], "disabled", scheduled)
        self.assertEqual(len(self.daily_calls()), 1)
        repeated = await app.daily_review.run(context, "原角色人格", expression_only=True, operation_key="manual-again")
        self.assertEqual(repeated["state"], "complete", repeated)
        self.assertEqual(len(self.daily_calls()), 1)
        self.source.dialogue.append({"role": "assistant", "content": "我们先看第一步吧。", "_qq_sequence": 13})
        self.source.approved.append({"role": "assistant", "content": "好呀，一起看。"})
        second = await app.daily_review.run(context, "原角色人格", expression_only=True, operation_key="manual-new")
        self.assertEqual(second["state"], "complete", second)
        self.assertEqual(len(self.daily_calls()), 2)
        snapshot = json.loads(next(item.payload for item in self.daily_calls()[-1].original.context.materials
                                   if item.purpose == "expression_comparison"))
        self.assertEqual([item["sequence"] for item in snapshot["unreviewed_dialogue"]], [13])
        self.assertEqual(len(snapshot["approved_dialogue"]), 3)
        self.assertEqual(snapshot["handles"]["unreviewed:13"]["quality"], "unreviewed")
        self.assertEqual(app.runtime.read_one("SELECT COUNT(*) n FROM expression_style_signals", ())["n"], 1)

    async def test_preview_does_not_consume_progress_or_write_rules(self):
        app, context = await self.comparison_ready(diary=False, plan=False, memory=False)
        before = app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (expression.cursor_key(context),))
        result = await simulate_daily_review(app, context, "原角色人格", attempt_id="comparison-preview", expression_only=True)
        self.assertEqual(result["state"], "passed", result)
        self.assertIsNone(before)
        self.assertIsNone(app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (expression.cursor_key(context),)))
        self.assertEqual(app.runtime.read_one("SELECT COUNT(*) n FROM expression_style_signals", ())["n"], 0)

    async def test_unapproved_only_evidence_is_rejected_without_advancing(self):
        app, context = await self.comparison_ready(diary=False, plan=False, memory=False)
        good = self.comparison_reply
        async def ungrounded(envelope):
            result = await good(envelope)
            value = json.loads(result.draft.text)
            value["expression"]["signals"][0]["evidence_handles"] = ["unreviewed:12"]
            return answered(encode(value))
        self.model_behavior = ungrounded
        result = await app.daily_review.run(context, "原角色人格", expression_only=True, operation_key="invalid")
        self.assertEqual(result["state"], "failed", result)
        self.assertIsNone(app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (expression.cursor_key(context),)))
        self.assertEqual(app.runtime.read_one("SELECT COUNT(*) n FROM expression_style_signals", ())["n"], 0)
        self.model_behavior = good
        result = await app.daily_review.run(context, "原角色人格", expression_only=True, operation_key="corrected")
        self.assertEqual(result["state"], "complete", result)

    async def test_source_cursor_survives_restart_and_stale_commit_is_rejected(self):
        app, context = await self.comparison_ready(diary=False, plan=False, memory=False)
        old = await expression.capture(app.daily_review, context, automatic=False)
        result = await app.daily_review.run(context, "原角色人格", expression_only=True, operation_key="before-restart")
        self.assertEqual(result["state"], "complete", result)
        with self.assertRaisesRegex(TaskError, "already processed"):
            expression.validate(app.runtime, context, old)
        await app.close()
        restarted = await self.start(self.host(runtime=self.runtime), task_generation=1, model_generation=1)
        self.app = restarted.application
        self.app.gateway.request_context = self.source
        self.app.state_maintenance.bind(context, "原角色人格")
        self.assertIsNone(await expression.capture(self.app.daily_review, context, automatic=False))

    async def test_daily_never_learns_even_with_the_old_automatic_switch_enabled(self):
        app, context = await self.comparison_ready()
        result = await self.run_daily()
        self.assertEqual(result["state"], "complete", result)
        self.assertIsNone(result["receipts"]["expression"])
        self.assertFalse(any(item.purpose == "expression_comparison"
                             for item in self.daily_calls()[-1].original.context.materials))
        self.assertIsNone(await expression.capture(app.daily_review, context))


    async def test_dashboard_omits_legacy_material_controls_and_rejects_stale_writes(self):
        app, context = await self.comparison_ready(diary=False, plan=False, memory=False)
        manager = ExpressionLearningManagement(app, context)
        status = manager.status(self.core).unwrap()
        for name in ("settings", "fields", "provenance"):
            self.assertEqual(set(status[name]), PUBLIC_FIELDS)
        result = manager.configure(self.core, {"operation_key": "old-material-opt-in",
            "edit_token": status["edit_token"], "changes": {"private_learning_enabled": True}})
        self.assertTrue(result.failure)
        self.assertEqual(result.failure.code, "expression_learning_configuration_invalid")

        profile = RoleProfileManagement(app, context)
        settings = profile.status(self.core).unwrap()
        for name in ("settings", "fields", "provenance"):
            self.assertFalse(RETIRED_PROFILE_SOURCE_FIELDS.intersection(settings[name]))
        changed = profile.configure(self.core, {"operation_key": "old-source-selection",
            "edit_token": settings["edit_token"],
            "changes": {"expression_private_source_mode": "all"}})
        self.assertTrue(changed.failure)


class ApprovedTargetParserTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {"handles": {
            "preset:1": {"role": "assistant", "quality": "approved", "content": "我可以学呀！哼 我明明有一直在学（委屈"},
            "unreviewed:2": {"role": "assistant", "quality": "unreviewed", "content": "（缩被窝)"}},
            "unreviewed_dialogue": [{"role": "assistant", "content": "（缩被窝)"}]}
        self.value = {"summary": "认可原文中的具体表达。", "signals": [{
            "dimension": "formatting", "observation": "认可示例用未闭合括号表达委屈。",
            "proposal": "在类似情境中自然使用认可示例里的括号表达。", "confidence": 0.7,
            "evidence_handles": ["preset:1"],
            "approved_quotes": [{"evidence_handle": "preset:1", "quote": "（委屈"}],
            "expression": {"kind": "style", "situation": "表示委屈时", "pattern": "（委屈", "avoid": "正式说明时"},
            "applicability": {"channels": ["private"], "relationship_stages": ["any"],
                "emotion_gates": ["any"], "intent": "any", "keywords": []}}]}

    def test_approved_exact_expression_is_accepted(self):
        result = expression.parse(self.value, self.snapshot)
        self.assertEqual(result.signals[0].evidence_handles, ("preset:1",))

    def test_comparison_references_never_become_positive_learning_evidence(self):
        self.value["signals"][0]["evidence_handles"].append("unreviewed:2")
        result = expression.parse(self.value, self.snapshot)
        self.assertEqual(result.signals[0].evidence_handles, ("preset:1",))

    def test_unapproved_only_pattern_cannot_borrow_an_approved_reference(self):
        self.value["signals"][0]["expression"]["pattern"] = "（缩被窝）"
        with self.assertRaisesRegex(TaskError, "approved quotation"):
            expression.parse(self.value, self.snapshot)

    def test_unapproved_example_is_rejected_inside_a_grammar_rule(self):
        self.value["signals"][0]["expression"].update(kind="grammar", pattern="在句末使用括号表达情绪")
        self.value["signals"][0]["proposal"] = "句末可以补充“（缩被窝）”表达。"
        with self.assertRaisesRegex(TaskError, "unreviewed-only expression"):
            expression.parse(self.value, self.snapshot)

    def test_a_quote_must_come_from_the_cited_approved_message(self):
        self.value["signals"][0]["approved_quotes"][0]["quote"] = "（缩被窝)"
        with self.assertRaisesRegex(TaskError, "exact quotations"):
            expression.parse(self.value, self.snapshot)
