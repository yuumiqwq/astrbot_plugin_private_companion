"""Positive and negative rules retain distinct evidence and review semantics."""

import json
import unittest
from types import SimpleNamespace

from companion_v2.conversation.style_context import EXPRESSION_STYLE_INSTRUCTION
from companion_v2.execution.models import TaskContext, encode, stamp
from companion_v2.expression import learning, rule_details
from companion_v2.expression.sharing import build_pack, normalize_pack
from companion_v2.expression.style_management import ExpressionStyleManagement
from companion_v2.foundation.refs import AudienceRef
from tests.test_host_application import HostFixture
from tests.test_management_access import SCOPE


FORM = {"kind": "style", "situation": "回应共同活动邀请时", "pattern": "先读题目。", "avoid": ""}
RULE = {"dimension": "diction", "observation": "结合邀请语境判断表达方式。",
        "proposal": "先回应邀请，再衔接适合当前话题的下一步。", "confidence": 0.7,
        "applicability": {"channels": ["private", "proactive"]}}


class PolarityContractTests(unittest.TestCase):
    def test_old_forms_default_positive_and_new_schema_requires_polarity(self):
        self.assertEqual(rule_details.normalize(FORM), {**FORM, "polarity": "positive"})
        self.assertIn("polarity", rule_details.FORM_SCHEMA["required"])
        for value in (None, True, "other"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                rule_details.normalize({**FORM, "polarity": value})

    def test_legacy_read_normalizes_without_changing_the_saved_record(self):
        saved = encode({"form": FORM, "revision": 2})
        runtime = SimpleNamespace(read_one=lambda *_: {"value": saved})
        detail = rule_details.read(runtime, "legacy-rule")
        self.assertEqual(detail["form"]["polarity"], "positive")
        self.assertEqual(detail["revision"], 2)
        self.assertEqual(rule_details.form_for(runtime, {"signal_id": "legacy-rule"}), detail["form"])
        self.assertNotIn("polarity", json.loads(saved)["form"])

    def test_identity_keeps_opposite_rules_distinct_and_old_positive_equivalent(self):
        positive = {**FORM, "polarity": "positive"}
        negative = {**FORM, "polarity": "negative"}
        self.assertEqual(learning.form_identity(FORM), learning.form_identity(positive))
        self.assertNotEqual(learning.form_identity(positive), learning.form_identity(negative))
        arguments = (RULE["dimension"], RULE["observation"], RULE["proposal"], RULE["applicability"])
        self.assertEqual(learning.candidate_signature(*arguments, FORM),
                         learning.candidate_signature(*arguments, positive))
        self.assertNotEqual(learning.candidate_signature(*arguments, positive),
                            learning.candidate_signature(*arguments, negative))

    def test_pack_round_trip_keeps_both_polarities(self):
        rules = [{**RULE, "expression": {**FORM, "polarity": value}}
                 for value in ("positive", "negative")]
        package = build_pack(rules)
        self.assertEqual([group["rules"][0]["polarity"] for group in package["rule_groups"]],
                         ["positive", "negative"])
        imported = normalize_pack(package)
        self.assertEqual([row["expression"]["polarity"] for row in imported["signals"]],
                         ["positive", "negative"])
        self.assertEqual(imported["rejected"], [])

    def test_legacy_pack_defaults_positive_and_conflicting_polarities_are_rejected(self):
        package = build_pack([{**RULE, "expression": FORM}])
        rule = package["rule_groups"][0]["rules"][0]
        rule.pop("polarity")
        rule["expression"].pop("polarity")
        self.assertEqual(normalize_pack(package)["signals"][0]["expression"]["polarity"], "positive")
        rule["polarity"] = "negative"
        self.assertEqual(normalize_pack(package)["signals"][0]["expression"]["polarity"], "negative")
        rule["expression"]["polarity"] = "positive"
        with self.assertRaisesRegex(ValueError, "empty_pack"):
            normalize_pack(package)


class PolarityReviewTests(HostFixture, unittest.IsolatedAsyncioTestCase):
    def selected_context(self):
        binding = self.identity.resolve_role(SCOPE, observed_at=self.clock.now()).unwrap()
        return TaskContext(self.important, binding, AudienceRef(SCOPE, (self.important.subject,)))

    async def pending_negative(self):
        host = await self.start()
        app = host.application
        context = self.selected_context()
        manager = ExpressionStyleManagement(app)
        package = build_pack([{**RULE, "expression": {**FORM, "polarity": "negative"}}])
        preview = manager.preview_import(operator=self.core, context=context, raw_pack=package).unwrap()
        manager.import_pack(operator=self.core, context=context, raw_pack=package,
            preview_token=preview["preview_token"], operation_key="negative-pack").unwrap()
        item = manager.list(operator=self.core, context=context).unwrap()["items"][0]
        return app, context, manager, item

    async def test_negative_comparison_requires_adoption_and_retains_both_evidence_roles(self):
        app, context, manager, item = await self.pending_negative()
        handles = {"preset:1": {"quality": "approved", "role": "assistant", "content": "可以呀。"},
                   "unreviewed:2": {"quality": "unreviewed", "role": "assistant", "content": FORM["pattern"]}}
        window = {"opened_at": stamp(self.clock.now()), "closed_at": stamp(self.clock.now()),
                  "handles": list(handles), "characters": 10, "source_kind": "preset_comparison",
                  "context_key": context.key, "comparison": {"handles": handles}}
        with app.runtime.transaction() as connection:
            connection.execute("UPDATE expression_style_runs SET sample_json=? WHERE run_id=?",
                               (encode(window), item["run_id"]))
            connection.execute("UPDATE expression_style_signals SET evidence_json=? WHERE signal_id=?",
                               (encode(list(handles)), item["signal_id"]))
        item = manager.list(operator=self.core, context=context).unwrap()["items"][0]
        self.assertIsNone(item["unavailable_reason"])
        self.assertEqual(item["evidence"], ["认可示例：[preset:1] 可以呀。", "对照反例：[unreviewed:2] " + FORM["pattern"]])
        self.assertEqual(item["expression"]["polarity"], "negative")
        snapshot = app.expression_style_context.capture(context, self.clock.now())
        self.assertEqual(snapshot["references"], [])
        manager.command(operator=self.core, context=context, signal_id=item["signal_id"],
            action="adopt", view_token=item["view_token"], operation_key="adopt-negative").unwrap()
        snapshot = app.expression_style_context.capture(context, self.clock.now())
        materials = app.expression_style_context.contributions(snapshot, context, self.clock.now())
        payload = next(json.loads(value.payload) for value in materials
                       if value.purpose == "expression_style_rule")
        self.assertEqual(payload["expression"]["polarity"], "negative")
        self.assertIn("negative 表示应避免", EXPRESSION_STYLE_INSTRUCTION)
        exported = manager.export_pack(operator=self.core, context=context).unwrap()["package"]
        self.assertEqual(exported["rule_groups"][0]["rules"][0]["expression"]["polarity"], "negative")
        self.assertEqual(self.called, [])

    async def test_revision_cannot_flip_polarity_and_legacy_edits_preserve_negative(self):
        app, context, manager, item = await self.pending_negative()
        changes = {"dimension": item["dimension"], "expression": {**FORM, "polarity": "positive"},
                   "proposal": "回应邀请时先确认对方的意思。", "applicability": item["applicability"]}
        args = {"operator": self.core, "context": context, "signal_id": item["signal_id"],
                "view_token": item["view_token"], "operation_key": "flip-polarity", "changes": changes}
        self.assertEqual(manager.revise(**args).failure.code, "expression_revision_invalid")
        self.assertEqual(rule_details.form_for(app.runtime, item)["polarity"], "negative")
        changes["expression"].pop("polarity")
        args["operation_key"] = "legacy-negative-edit"
        manager.revise(**args).unwrap()
        self.assertTrue(manager.revise(**args).unwrap()["replayed"])
        after = manager.list(operator=self.core, context=context).unwrap()["items"][0]
        self.assertEqual(after["expression"]["polarity"], "negative")
        self.assertEqual(after["state"], "pending")
        self.assertEqual(self.called, [])

    async def test_negative_type_remains_visible_when_its_evidence_is_unavailable(self):
        app, context, manager, item = await self.pending_negative()
        window = {"opened_at": stamp(self.clock.now()), "closed_at": stamp(self.clock.now()),
                  "handles": ["preset:1", "unreviewed:2"], "characters": 10,
                  "source_kind": "preset_comparison", "context_key": context.key,
                  "comparison": {"handles": {
                      "preset:1": {"quality": "approved", "role": "assistant", "content": "可以呀。"}}}}
        with app.runtime.transaction() as connection:
            connection.execute("UPDATE expression_style_runs SET sample_json=? WHERE run_id=?",
                               (encode(window), item["run_id"]))
            connection.execute("UPDATE expression_style_signals SET evidence_json=? WHERE signal_id=?",
                               (encode(window["handles"]), item["signal_id"]))
        unavailable = manager.list(operator=self.core, context=context).unwrap()["items"][0]
        self.assertEqual(unavailable["polarity"], "negative")
        self.assertEqual(unavailable["unavailable_reason"], "expression_sample_changed")
        self.assertIsNone(unavailable["expression"])
        self.assertIsNone(unavailable["proposal"])
