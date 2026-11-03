"""Approved examples guide incremental, reviewable expression learning."""

import json

from companion_v2.execution.models import TaskError, encode
from companion_v2.expression.configuration import (
    GROUP, ExpressionConfigurationOwner, configuration_fields, read_settings, target_for,
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
            unreviewed = next(key for key in snapshot["handles"] if key.startswith("unreviewed:"))
            result["expression"] = {"summary": "参考已认可示例的接话方式。", "signals": [{
                "dimension": "sentence_rhythm", "observation": "先回应邀请，再补充下一步。",
                "proposal": "回应共同活动时，先用简短应答接住，再接一句具体行动。",
                "confidence": 0.7, "evidence_handles": [approved, unreviewed],
                "expression": {"kind": "grammar", "situation": "回应共同活动邀请时",
                    "pattern": "先简短答应，再接一句具体行动", "avoid": "需要拒绝邀请时"},
                "applicability": {"channels": ["private", "proactive"], "relationship_stages": ["any"],
                    "emotion_gates": ["any"], "intent": "any", "keywords": []},
            }]}
        return answered(encode(result))

    async def test_comparison_joins_one_daily_call_and_waits_for_adoption(self):
        app, context = await self.comparison_ready()
        result = await self.run_daily()
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
        self.assertEqual(scheduled["state"], "complete", scheduled)
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
        result = await simulate_daily_review(app, context, "原角色人格", attempt_id="comparison-preview")
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
        old = await expression.capture(app.daily_review, context)
        result = await app.daily_review.run(context, "原角色人格", expression_only=True, operation_key="before-restart")
        self.assertEqual(result["state"], "complete", result)
        with self.assertRaisesRegex(TaskError, "already processed"):
            expression.validate(app.runtime, context, old)
        await app.close()
        restarted = await self.start(self.host(runtime=self.runtime), task_generation=1, model_generation=1)
        self.app = restarted.application
        self.app.gateway.request_context = self.source
        self.app.state_maintenance.bind(context, "原角色人格")
        self.assertIsNone(await expression.capture(self.app.daily_review, context))
