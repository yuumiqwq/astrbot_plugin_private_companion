"""Approved examples guide incremental, reviewable expression learning."""

import json
import unittest
from copy import deepcopy
from unittest.mock import patch

from companion_v2.configuration.management import ExpressionLearningManagement, RoleProfileManagement
from companion_v2.execution.models import TaskError, encode
from companion_v2.model_gateway.failures import ModelFailure
from companion_v2.model_gateway.models import Amount, ProviderResponse
from companion_v2.model_gateway.response_diagnostics import ResponseDiagnostics
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
    async def test_balance_failure_preserves_the_comparison_cursor_and_rules(self):
        app, context = await self.comparison_ready(diary=False, plan=False, memory=False)

        async def insufficient(_envelope):
            return ProviderResponse("not_started", usage=Amount(0, 0), receipt_ref="balance:402",
                failure=ModelFailure.TRANSPORT, diagnostics=ResponseDiagnostics(status_code=402))

        self.model_behavior = insufficient
        result = await app.daily_review.run(context, "原角色人格", expression_only=True,
                                             operation_key="balance-failure")

        self.assertEqual(result["state"], "failed", result)
        self.assertEqual(result["receipts"]["code"], "model_insufficient_balance")
        self.assertIsNone(app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?",
                                              (expression.cursor_key(context),)))
        self.assertEqual(app.runtime.read_one("SELECT COUNT(*) n FROM expression_style_signals", ())["n"], 0)

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

    async def mixed_comparison_reply(self, envelope):
        response = await self.comparison_reply(envelope)
        value = json.loads(response.draft.text)
        good = value["expression"]["signals"][0]
        unapproved = deepcopy(good)
        unapproved["proposal"] = "可以补充“先读题目。”作为自己的表达。"
        marker = deepcopy(good)
        marker["expression"].update(kind="style", pattern="[回复]")
        value["expression"]["signals"] = [unapproved, marker, good]
        return answered(encode(value))

    async def negative_comparison_reply(self, envelope):
        response = await self.comparison_reply(envelope)
        value = json.loads(response.draft.text)
        negative = deepcopy(value["expression"]["signals"][0])
        negative.update(
            observation="同样回应共同观看的邀请，认可示例直接答应；普通回复用郑重承诺增加不必要的负担。",
            proposal="回应普通邀请时避免“我将无条件陪伴你”，用认可示例中的“可以呀。”直接答应。",
            evidence_handles=["preset:1", "unreviewed:12"],
            counterexamples=[{"evidence_handle": "unreviewed:12", "quote": "我将无条件陪伴你"}],
        )
        negative["expression"].update(polarity="negative", kind="style",
            pattern="我将无条件陪伴你", situation="回应普通共同活动邀请时", avoid="双方明确讨论郑重承诺时")
        value["expression"]["signals"].append(negative)
        return answered(encode(value))

    async def test_positive_and_negative_rules_share_manual_review_and_keep_separate_evidence(self):
        app, context = await self.comparison_ready(diary=False, plan=False, memory=False)
        self.source.dialogue[1]["content"] = "一起看看吗？"
        self.source.dialogue[2]["content"] = "我将无条件陪伴你"
        self.model_behavior = self.negative_comparison_reply
        result = await app.daily_review.run(context, "原角色人格", expression_only=True,
                                             operation_key="two-polarities")
        self.assertEqual(result["state"], "complete", result)
        self.assertEqual(result["receipts"]["expression"]["pending_rules"], 2)
        self.assertTrue(result["receipts"]["expression"]["cursor_advanced"])
        self.assertEqual(len(self.daily_calls()), 1)
        manager = ExpressionStyleManagement(app)
        pending = manager.list(operator=self.core, context=context).unwrap()["items"]
        negative = next(item for item in pending if item["expression"]["polarity"] == "negative")
        self.assertTrue(any(text.startswith("认可示例：") for text in negative["evidence"]))
        self.assertTrue(any(text.startswith("对照反例：") for text in negative["evidence"]))
        policy = read_settings(app.runtime, context)
        self.assertEqual(app.expression_style_context.capture(context, self.clock.now(), policy=policy)["references"], [])
        manager.command(operator=self.core, context=context, signal_id=negative["signal_id"], action="adopt",
            view_token=negative["view_token"], operation_key="adopt-negative").unwrap()
        captured = app.expression_style_context.capture(context, self.clock.now(), policy=policy)
        contributions = app.expression_style_context.contributions(captured, context, self.clock.now(), policy=policy)
        payload = next(json.loads(item.payload) for item in contributions if item.purpose == "expression_style_rule")
        self.assertEqual(payload["expression"]["polarity"], "negative")
        self.assertIn("避免", payload["proposal"])
        adopted = manager.list(operator=self.core, context=context, state="adopted").unwrap()["items"][0]
        manager.command(operator=self.core, context=context, signal_id=adopted["signal_id"], action="retire",
            view_token=adopted["view_token"], operation_key="retire-negative").unwrap()
        self.assertEqual(app.expression_style_context.capture(context, self.clock.now(), policy=policy)["references"], [])

    async def test_manual_negative_prompt_and_schema_are_available_without_enabling_daily_learning(self):
        app, context = await self.comparison_ready(diary=False, plan=False, memory=False)
        self.assertIsNone(await expression.capture(app.daily_review, context, automatic=True))
        result = await app.daily_review.run(context, "原角色人格", expression_only=True,
                                             operation_key="negative-contract")
        self.assertEqual(result["state"], "complete", result)
        request = self.daily_calls()[0].original
        self.assertIn("expression.polarity", request.instruction)
        self.assertIn("负面约束", request.instruction)
        schema = expression.schema()["properties"]["signals"]["items"]
        self.assertIn("counterexamples", schema["required"])
        self.assertEqual(schema["properties"]["expression"]["properties"]["polarity"]["enum"], ["positive", "negative"])

    async def test_mixed_candidates_save_only_approved_pending_rules_and_keep_the_window(self):
        app, context = await self.comparison_ready(diary=False, plan=False, memory=False)
        self.model_behavior = self.mixed_comparison_reply
        result = await app.daily_review.run(context, "原角色人格", expression_only=True,
                                             operation_key="mixed-candidates")
        self.assertEqual(result["state"], "partial", result)
        receipt = result["receipts"]["expression"]
        self.assertEqual(receipt["pending_rules"], 1)
        self.assertFalse(receipt["cursor_advanced"])
        self.assertEqual(receipt["rejected_rules"], 2)
        self.assertEqual([item["index"] for item in receipt["rejections"]], [1, 2])
        self.assertEqual([item["code"] for item in receipt["rejections"]],
                         ["expression_unapproved_pattern", "expression_platform_marker"])
        self.assertEqual(receipt["rejections"][0]["unsupported_examples"], ["先读题目。"])
        rows = app.runtime.read_all("SELECT state,proposal,evidence_json FROM expression_style_signals", ())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "pending")
        self.assertEqual(json.loads(rows[0]["evidence_json"]), ["preset:1"])
        self.assertNotIn("先读题目", rows[0]["proposal"])
        self.assertIsNone(app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?",
                                              (expression.cursor_key(context),)))
        next_window = await expression.capture(app.daily_review, context, automatic=False)
        self.assertEqual([item["sequence"] for item in next_window["unreviewed_dialogue"]], [11, 12])
        self.assertEqual(len(next_window["existing_rules"]), 1)
        current = app.expression_style_context.capture(context, self.clock.now(), policy=read_settings(app.runtime, context))
        self.assertEqual(current["references"], [])
        repeated = await app.daily_review.run(context, "原角色人格", expression_only=True,
                                               operation_key="mixed-candidates")
        self.assertEqual(repeated, result)
        self.assertEqual(len(self.daily_calls()), 1)

        self.model_behavior = self.comparison_reply
        completed = await app.daily_review.run(context, "原角色人格", expression_only=True,
                                                operation_key="all-valid-later")
        self.assertEqual(completed["state"], "complete", completed)
        self.assertTrue(completed["receipts"]["expression"]["cursor_advanced"])
        self.assertEqual(completed["receipts"]["expression"]["pending_rules"], 0)
        self.assertIsNone(await expression.capture(app.daily_review, context, automatic=False))
        self.assertEqual(app.runtime.read_one("SELECT COUNT(*) n FROM expression_style_signals", ())["n"], 1)

    async def test_partial_preview_reports_rejections_without_writing_or_advancing(self):
        app, context = await self.comparison_ready(diary=False, plan=False, memory=False)
        self.model_behavior = self.mixed_comparison_reply
        result = await simulate_daily_review(app, context, "原角色人格",
            attempt_id="partial-comparison-preview", expression_only=True)
        self.assertEqual(result["state"], "partial", result)
        self.assertEqual(result["expression_validation"]["accepted_rules"], 1)
        self.assertEqual(result["expression_validation"]["rejected_rules"], 2)
        self.assertEqual(len(result["result"]["expression"]["signals"]), 1)
        self.assertEqual(result["stages"][-1], {"stage": "output_validation", "state": "partial"})
        self.assertIsNone(app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?",
                                              (expression.cursor_key(context),)))
        self.assertEqual(app.runtime.read_one("SELECT COUNT(*) n FROM expression_style_signals", ())["n"], 0)
        self.assertEqual(app.runtime.read_one("SELECT COUNT(*) n FROM expression_style_runs", ())["n"], 0)

    async def test_partial_rule_save_rolls_back_and_recovers_the_same_output_without_a_second_call(self):
        app, context = await self.comparison_ready(diary=False, plan=False, memory=False)
        self.model_behavior = self.mixed_comparison_reply
        with patch.object(expression.rule_details, "write", side_effect=RuntimeError("synthetic storage failure")):
            result = await app.daily_review.run(context, "原角色人格", expression_only=True,
                                                 operation_key="partial-recovery")
        self.assertEqual(result["state"], "received", result)
        self.assertEqual(app.runtime.read_one("SELECT COUNT(*) n FROM expression_style_signals", ())["n"], 0)
        self.assertEqual(app.runtime.read_one("SELECT COUNT(*) n FROM expression_style_runs", ())["n"], 0)
        recovered = await app.daily_review.run(context, "原角色人格", expression_only=True,
                                                operation_key="partial-recovery")
        self.assertEqual(recovered["state"], "partial", recovered)
        self.assertEqual(recovered["receipts"]["expression"]["pending_rules"], 1)
        self.assertEqual(len(self.daily_calls()), 1)
        self.assertIsNone(app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?",
                                              (expression.cursor_key(context),)))

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

    async def test_exact_approved_text_repairs_a_misnumbered_reference_before_commit(self):
        app, context = await self.comparison_ready(diary=False, plan=False, memory=False)
        self.source.approved.extend([{"role": "user", "content": "明天见？"},
                                     {"role": "assistant", "content": "明天见。"}])
        original = self.comparison_reply

        async def misnumbered(envelope):
            response = await original(envelope)
            value = json.loads(response.draft.text)
            signal = value["expression"]["signals"][0]
            signal["evidence_handles"] = ["preset:3"]
            signal["approved_quotes"][0]["evidence_handle"] = "preset:3"
            return answered(encode(value))

        self.model_behavior = misnumbered
        result = await app.daily_review.run(context, "原角色人格", expression_only=True,
                                             operation_key="repair-reference")
        self.assertEqual(result["state"], "complete", result)
        self.assertEqual(result["receipts"]["expression"]["pending_rules"], 1)
        row = app.runtime.read_one("SELECT evidence_json,state FROM expression_style_signals", ())
        self.assertEqual(json.loads(row["evidence_json"]), ["preset:1"])
        self.assertEqual(row["state"], "pending")
        self.assertIsNotNone(app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?",
                                                (expression.cursor_key(context),)))
        repeated = await app.daily_review.run(context, "原角色人格", expression_only=True,
                                               operation_key="repair-reference")
        self.assertEqual(repeated["state"], "complete")
        self.assertEqual(len(self.daily_calls()), 1)

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
        self.assertEqual(result["receipts"]["expression"]["rejected_rules"], 1)
        self.assertEqual(result["receipts"]["code"], "expression_approved_evidence_required")
        self.assertIsNone(app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (expression.cursor_key(context),)))
        self.assertEqual(app.runtime.read_one("SELECT COUNT(*) n FROM expression_style_signals", ())["n"], 0)
        self.assertEqual(app.runtime.read_one("SELECT COUNT(*) n FROM expression_style_runs", ())["n"], 0)
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

    def test_qq_placeholder_is_not_a_style_even_when_it_occurs_in_approved_records(self):
        self.snapshot["handles"]["preset:1"]["content"] = "[回复]我可以学呀！哼 我明明有一直在学（委屈"
        self.value["signals"][0]["expression"]["pattern"] = "[回复]"
        self.value["signals"][0]["approved_quotes"][0]["quote"] = "[回复]"
        with self.assertRaises(TaskError) as raised:
            expression.parse(self.value, self.snapshot)
        self.assertEqual(raised.exception.code, "expression_platform_marker")
        self.assertEqual(raised.exception.unsupported_examples, ("[回复]",))

    def test_context_cues_can_be_described_without_becoming_style_examples(self):
        self.value["signals"][0]["expression"]["situation"] = "对方提到“（缩被窝）”时"
        parsed = expression.parse(self.value, self.snapshot)
        self.assertEqual(len(parsed.signals), 1)

    def test_one_invalid_candidate_does_not_discard_an_approved_candidate(self):
        bad = deepcopy(self.value["signals"][0])
        bad["proposal"] = "可以补充“（缩被窝）”表达。"
        self.value["signals"].insert(0, bad)
        assessed = expression.assess(self.value, self.snapshot)
        self.assertEqual(len(assessed.signals), 1)
        self.assertEqual(len(assessed.rejections), 1)
        self.assertEqual(assessed.rejections[0]["index"], 1)
        self.assertEqual(assessed.rejections[0]["unsupported_examples"], ["（缩被窝）"])

    def test_invalid_candidate_shape_has_its_own_rejection(self):
        self.value["signals"].insert(0, [])
        assessed = expression.assess(self.value, self.snapshot)
        self.assertEqual(len(assessed.signals), 1)
        self.assertEqual(assessed.rejections[0]["code"], "expression_candidate_invalid")

    def test_invalid_batch_shape_still_fails_before_any_candidate_is_processed(self):
        self.value["signals"] = {"not": "a list"}
        with self.assertRaises(TaskError) as raised:
            expression.assess(self.value, self.snapshot)
        self.assertEqual(raised.exception.code, "expression_output_invalid")

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

    def test_unapproved_examples_are_rejected_in_both_advice_fields_and_quote_forms(self):
        self.value["signals"][0]["expression"].update(kind="grammar", pattern="先回应，再补充下一步")
        self.snapshot["unreviewed_dialogue"].append({"role": "assistant", "content": "先读题目。"})
        for field in ("proposal", "pattern"):
            for pair in (("“", "”"), ('"', '"'), ("‘", "’"), ("'", "'"), ("`", "`"), ("", "")):
                with self.subTest(field=field, quotes=pair):
                    value = deepcopy(self.value)
                    advice = "先简短答应，再说" + pair[0] + "先读题目。" + pair[1]
                    if field == "proposal":
                        value["signals"][0][field] = advice
                    else:
                        value["signals"][0]["expression"][field] = advice
                    assessed = expression.assess(value, self.snapshot)
                    self.assertEqual(len(assessed.signals), 0)
                    self.assertEqual(assessed.rejections[0]["code"], "expression_unapproved_pattern")

    def test_a_quote_must_come_from_the_cited_approved_message(self):
        self.value["signals"][0]["approved_quotes"][0]["quote"] = "（缩被窝)"
        with self.assertRaisesRegex(TaskError, "exact quotations"):
            expression.parse(self.value, self.snapshot)

    def test_unique_exact_approved_quote_corrects_the_reference_without_mutating_input(self):
        self.snapshot["handles"]["preset:3"] = {
            "role": "assistant", "quality": "approved", "content": "明天见。"}
        self.value["signals"][0]["evidence_handles"] = ["preset:3"]
        self.value["signals"][0]["approved_quotes"][0]["evidence_handle"] = "preset:3"
        before = encode(self.value)
        result = expression.parse(self.value, self.snapshot)
        self.assertEqual(result.signals[0].evidence_handles, ("preset:1",))
        self.assertEqual(encode(self.value), before)

    def test_misnumbered_quote_with_multiple_approved_matches_is_rejected(self):
        self.snapshot["handles"].update({
            "preset:3": {"role": "assistant", "quality": "approved", "content": "明天见。"},
            "preset:5": {"role": "assistant", "quality": "approved", "content": "哼（委屈"},
        })
        self.value["signals"][0]["evidence_handles"] = ["preset:3"]
        self.value["signals"][0]["approved_quotes"][0]["evidence_handle"] = "preset:3"
        with self.assertRaisesRegex(TaskError, "exact quotations"):
            expression.parse(self.value, self.snapshot)

    def test_an_unreviewed_handle_cannot_be_rebound_to_an_approved_message(self):
        self.value["signals"][0]["evidence_handles"].append("unreviewed:2")
        self.value["signals"][0]["approved_quotes"][0]["evidence_handle"] = "unreviewed:2"
        with self.assertRaisesRegex(TaskError, "exact quotations"):
            expression.parse(self.value, self.snapshot)

    def test_slash_variants_each_need_exact_approved_quotation_support(self):
        self.snapshot["handles"]["preset:3"] = {
            "role": "assistant", "quality": "approved", "content": "不知道诶\n\n不知道耶"}
        signal = self.value["signals"][0]
        signal["evidence_handles"] = ["preset:3"]
        signal["approved_quotes"] = [{"evidence_handle": "preset:3", "quote": text}
                                     for text in ("不知道诶", "不知道耶")]
        signal["expression"]["pattern"] = "不知道诶/不知道耶"
        result = expression.parse(self.value, self.snapshot)
        self.assertEqual(result.signals[0].expression["pattern"], "不知道诶/不知道耶")
        signal["expression"]["pattern"] = "不知道诶/（缩被窝)"
        with self.assertRaisesRegex(TaskError, "approved quotation"):
            expression.parse(self.value, self.snapshot)


class NegativeConstraintParserTests(unittest.TestCase):
    setUp = ApprovedTargetParserTests.setUp

    def negative_rule(self):
        value = deepcopy(self.value)
        signal = value["signals"][0]
        signal["evidence_handles"].append("unreviewed:2")
        signal["counterexamples"] = [{"evidence_handle": "unreviewed:2", "quote": "（缩被窝)"}]
        signal["expression"].update(polarity="negative", pattern="（缩被窝)",
            situation="正在讨论具体事情，却用动作回避回应时", avoid="双方正在共同描写场景时")
        signal["observation"] = "认可示例回应对方正在说的事，未认可回复用单独动作取代回应。"
        signal["proposal"] = "在需要回应具体内容时避免只说“（缩被窝)”，先表达与当前话题有关的意思。"
        return value

    def test_negative_example_can_be_preserved_as_an_explicit_constraint(self):
        value = self.negative_rule()
        original = encode(value)
        result = expression.parse(value, self.snapshot)
        self.assertEqual(result.signals[0].expression["polarity"], "negative")
        self.assertEqual(result.signals[0].evidence_handles, ("preset:1", "unreviewed:2"))
        self.assertEqual(encode(value), original)
        self.assertNotIn("counterexamples", result.signals[0].expression)

    def test_negative_grammar_can_describe_the_problem_without_copying_the_phrase_as_a_template(self):
        value = self.negative_rule()
        value["signals"][0]["expression"].update(kind="grammar", pattern="用括号动作代替对当前问题的回应")
        parsed = expression.parse(value, self.snapshot)
        self.assertEqual(parsed.signals[0].expression["kind"], "grammar")

    def test_negative_rules_need_both_approved_target_and_exact_counterexample(self):
        for missing in ("approved_quotes", "counterexamples"):
            with self.subTest(missing=missing):
                value = self.negative_rule()
                value["signals"][0].pop(missing)
                assessed = expression.assess(value, self.snapshot)
                self.assertFalse(assessed.signals)
                self.assertEqual(assessed.rejections[0]["code"],
                    "expression_approved_quote_required" if missing == "approved_quotes" else "expression_counterexample_required")

    def test_negative_counterexample_does_not_accept_wrong_source_roles_or_fabricated_quotes(self):
        for handle, quote in (("preset:1", "（委屈"), ("unreviewed:2", "凭空编造的反例")):
            with self.subTest(handle=handle, quote=quote):
                value = self.negative_rule()
                value["signals"][0]["counterexamples"] = [{"evidence_handle": handle, "quote": quote}]
                assessed = expression.assess(value, self.snapshot)
                self.assertFalse(assessed.signals)
                self.assertEqual(assessed.rejections[0]["code"], "expression_counterexample_invalid")
        self.snapshot["handles"]["unreviewed:2"]["role"] = "user"
        assessed = expression.assess(self.negative_rule(), self.snapshot)
        self.assertFalse(assessed.signals)
        self.assertEqual(assessed.rejections[0]["code"], "expression_counterexample_invalid")

    def test_negative_concrete_pattern_requires_its_counterexample(self):
        value = self.negative_rule()
        value["signals"][0]["expression"]["pattern"] = "另一个未曾出现的说法"
        assessed = expression.assess(value, self.snapshot)
        self.assertFalse(assessed.signals)
        self.assertEqual(assessed.rejections[0]["code"], "expression_counterexample_invalid")

    def test_positive_rules_still_reject_negative_examples(self):
        value = self.negative_rule()
        value["signals"][0]["expression"]["polarity"] = "positive"
        assessed = expression.assess(value, self.snapshot)
        self.assertFalse(assessed.signals)
        self.assertEqual(assessed.rejections[0]["code"], "expression_counterexample_invalid")

    def test_negative_rules_do_not_authorize_unrelated_unreviewed_examples(self):
        value = self.negative_rule()
        self.snapshot["unreviewed_dialogue"].append({"role": "assistant", "content": "先读题目。"})
        value["signals"][0]["proposal"] += "或者说“先读题目。”。"
        assessed = expression.assess(value, self.snapshot)
        self.assertFalse(assessed.signals)
        self.assertEqual(assessed.rejections[0]["code"], "expression_unapproved_pattern")

    def test_qq_markers_do_not_become_negative_style_rules(self):
        value = self.negative_rule()
        self.snapshot["handles"]["unreviewed:2"]["content"] = "[回复]"
        value["signals"][0]["counterexamples"][0]["quote"] = "[回复]"
        value["signals"][0]["expression"]["pattern"] = "[回复]"
        assessed = expression.assess(value, self.snapshot)
        self.assertFalse(assessed.signals)
        self.assertEqual(assessed.rejections[0]["code"], "expression_platform_marker")

    def test_review_evidence_labels_counterexamples_only_for_negative_rules(self):
        window = {"comparison": self.snapshot}
        handles = ["preset:1", "unreviewed:2"]
        evidence = expression.evidence(window, handles, polarity="negative")
        self.assertTrue(evidence[0].startswith("认可示例："))
        self.assertTrue(evidence[1].startswith("对照反例："))
        with self.assertRaises(TaskError):
            expression.evidence(window, handles)
        with self.assertRaises(TaskError):
            expression.evidence(window, ["unreviewed:2"], polarity="negative")

    def test_two_polarities_remain_independent_candidates_in_the_same_response(self):
        value = self.negative_rule()
        value["signals"].insert(0, deepcopy(self.value["signals"][0]))
        assessed = expression.assess(value, self.snapshot)
        self.assertEqual(len(assessed.signals), 2)
        self.assertFalse(assessed.rejections)
