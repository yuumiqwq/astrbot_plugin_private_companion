"""Expression comparisons cite snapshot message IDs without repeating their text."""

from copy import deepcopy
import unittest

from companion_v2.execution.models import encode
from companion_v2.role_work.daily import expression


class MessageReferenceTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {
            "handles": {
                "preset:1": {"role": "assistant", "quality": "approved", "content": "好呀，等你。"},
                "preset:3": {"role": "user", "quality": "approved", "content": "你会来吗？"},
                "unreviewed:2": {
                    "role": "assistant", "quality": "unreviewed", "content": "我将永远无条件陪伴你。"},
                "unreviewed:4": {"role": "user", "quality": "unreviewed", "content": "一起看吧。"},
            },
            "unreviewed_dialogue": [
                {"role": "assistant", "content": "我将永远无条件陪伴你。"},
                {"role": "user", "content": "一起看吧。"},
            ],
        }
        self.value = {"summary": "比较认可的简短回应和未认可的郑重承诺。", "signals": [{
            "dimension": "diction", "observation": "认可示例针对具体邀请作简短回应。",
            "proposal": "先回应当前邀请，再按情境补充下一步。", "confidence": 0.8,
            "evidence_handles": ["preset:1"],
            "expression": {
                "kind": "style", "polarity": "positive", "situation": "回应共同活动的邀请时",
                "pattern": "好呀，那就约好了", "avoid": "需要拒绝或澄清时",
            },
            "applicability": {
                "channels": ["private", "proactive"], "relationship_stages": ["any"],
                "emotion_gates": ["any"], "intent": ["casual", "play"], "keywords": [],
            },
        }]}

    def negative(self):
        value = deepcopy(self.value)
        signal = value["signals"][0]
        signal["evidence_handles"] = ["preset:1", "unreviewed:2"]
        signal["expression"].update(
            polarity="negative", pattern="避免用郑重承诺替代对具体邀请的回应")
        signal["proposal"] = "避免使用“我将永远无条件陪伴你。”回应普通邀约，直接回应当次活动。"
        return value

    def assert_rejected(self, value, code="expression_approved_evidence_required", snapshot=None):
        assessed = expression.assess(value, snapshot or self.snapshot)
        self.assertEqual(assessed.signals, ())
        self.assertEqual(len(assessed.rejections), 1)
        self.assertEqual(assessed.rejections[0]["code"], code)

    def test_schema_requires_bounded_unique_message_ids_without_quote_fields(self):
        rule = expression.schema()["properties"]["signals"]["items"]
        self.assertIn("evidence_handles", rule["required"])
        for legacy in ("approved_quotes", "counterexamples"):
            self.assertNotIn(legacy, rule["properties"])
            self.assertNotIn(legacy, rule["required"])
        references = rule["properties"]["evidence_handles"]
        self.assertEqual((references["minItems"], references["maxItems"]), (1, 16))
        self.assertTrue(references["uniqueItems"])
        self.assertIn("pattern", references["items"])

    def test_positive_id_only_accepts_generalized_style_and_keeps_all_intents(self):
        self.assertNotIn(self.value["signals"][0]["expression"]["pattern"],
                         self.snapshot["handles"]["preset:1"]["content"])
        result = expression.parse(self.value, self.snapshot)
        self.assertEqual(result.signals[0].evidence_handles, ("preset:1",))
        self.assertEqual(result.signals[0].applicability["intent"], ["casual", "play"])
        self.assertEqual(result.signals[0].expression["pattern"], "好呀，那就约好了")

    def test_message_id_without_quote_is_sufficient_for_both_polarities(self):
        for value, expected in ((self.value, ("preset:1",)),
                                (self.negative(), ("preset:1", "unreviewed:2"))):
            with self.subTest(polarity=value["signals"][0]["expression"]["polarity"]):
                before = encode({"value": value, "snapshot": self.snapshot})
                result = expression.parse(value, self.snapshot)
                self.assertEqual(result.signals[0].evidence_handles, expected)
                self.assertEqual(encode({"value": value, "snapshot": self.snapshot}), before)

    def test_legacy_quote_fields_are_ignored_even_when_their_shape_or_text_is_invalid(self):
        for quotes, counterexamples in ((None, "legacy payload"), ([], {}),
                ([{"evidence_handle": "preset:unknown", "quote": "不存在的原文"}],
                 [{"evidence_handle": "unreviewed:unknown", "quote": "不存在的反例"}])):
            for candidate in (self.value, self.negative()):
                value = deepcopy(candidate)
                value["signals"][0].update(approved_quotes=quotes, counterexamples=counterexamples)
                with self.subTest(quotes=quotes, polarity=value["signals"][0]["expression"]["polarity"]):
                    assessed = expression.assess(value, self.snapshot)
                    self.assertEqual(len(assessed.signals), 1)
                    self.assertEqual(assessed.rejections, ())

    def test_unknown_or_foreign_message_ids_are_not_repaired_from_quote_text(self):
        for reference in ("preset:999", "unreviewed:999"):
            with self.subTest(reference=reference):
                value = deepcopy(self.value)
                value["signals"][0]["evidence_handles"] = ["preset:1", reference]
                value["signals"][0]["approved_quotes"] = [{
                    "evidence_handle": reference, "quote": "好呀，等你。"}]
                self.assert_rejected(value)

    def test_non_assistant_sources_cannot_supply_rule_evidence(self):
        for role in ("user", "system", "tool"):
            for reference in ("preset:1", "unreviewed:2"):
                with self.subTest(role=role, reference=reference):
                    snapshot = deepcopy(self.snapshot)
                    snapshot["handles"][reference]["role"] = role
                    value = self.negative()
                    self.assert_rejected(value, snapshot=snapshot)

    def test_numbered_user_messages_are_not_valid_assistant_references(self):
        for references in (["preset:3"], ["preset:1", "unreviewed:4"]):
            with self.subTest(references=references):
                value = self.negative()
                value["signals"][0]["evidence_handles"] = references
                self.assert_rejected(value)

    def test_message_prefix_must_agree_with_its_snapshot_quality(self):
        for reference, quality in (("preset:1", "unreviewed"), ("unreviewed:2", "approved")):
            with self.subTest(reference=reference):
                snapshot = deepcopy(self.snapshot)
                snapshot["handles"][reference]["quality"] = quality
                self.assert_rejected(self.negative(), snapshot=snapshot)
        snapshot = deepcopy(self.snapshot)
        snapshot["handles"]["foreign:1"] = deepcopy(snapshot["handles"]["preset:1"])
        value = deepcopy(self.value)
        value["signals"][0]["evidence_handles"] = ["foreign:1"]
        self.assert_rejected(value, snapshot=snapshot)

    def test_empty_non_string_and_oversized_references_are_rejected(self):
        for references in ([], ["preset:1", None], "preset:1", ["preset:1"] * 17):
            with self.subTest(references=references):
                value = deepcopy(self.value)
                value["signals"][0]["evidence_handles"] = references
                assessed = expression.assess(value, self.snapshot)
                self.assertEqual(assessed.signals, ())
                self.assertEqual(len(assessed.rejections), 1)

    def test_duplicate_message_ids_normalize_without_losing_either_evidence_quality(self):
        for value, references, expected in (
                (deepcopy(self.value), ["preset:1", "preset:1"], ("preset:1",)),
                (self.negative(), ["unreviewed:2", "preset:1", "unreviewed:2", "preset:1"],
                 ("preset:1", "unreviewed:2"))):
            value["signals"][0]["evidence_handles"] = references
            with self.subTest(references=references):
                parsed = expression.parse(value, self.snapshot)
                self.assertEqual(parsed.signals[0].evidence_handles, expected)

    def test_negative_rules_need_both_source_quality_classes(self):
        for references, code in ((["preset:1"], "expression_counterexample_required"),
                                  (["unreviewed:2"], "expression_approved_evidence_required")):
            with self.subTest(references=references):
                value = self.negative()
                value["signals"][0]["evidence_handles"] = references
                self.assert_rejected(value, code)

    def test_positive_comparison_keeps_only_approved_ids_for_later_application(self):
        self.value["signals"][0]["evidence_handles"].append("unreviewed:2")
        parsed = expression.parse(self.value, self.snapshot)
        self.assertEqual(parsed.signals[0].evidence_handles, ("preset:1",))
        self.assertEqual(expression.evidence({"comparison": self.snapshot},
                         parsed.signals[0].evidence_handles), ["认可示例：[preset:1] 好呀，等你。"])

    def test_positive_rules_cannot_promote_known_unreviewed_only_text(self):
        for field in ("pattern", "proposal"):
            value = deepcopy(self.value)
            if field == "pattern":
                value["signals"][0]["expression"][field] = "我将永远无条件陪伴你。"
            else:
                value["signals"][0][field] = "可以说“我将永远无条件陪伴你。”作为回应。"
            with self.subTest(field=field):
                self.assert_rejected(value, "expression_unapproved_pattern")

    def test_shared_approved_and_unreviewed_phrase_remains_a_valid_positive_target(self):
        self.snapshot["handles"]["preset:1"]["content"] = "我将永远无条件陪伴你。"
        self.value["signals"][0]["expression"]["pattern"] = "我将永远无条件陪伴你。"
        parsed = expression.parse(self.value, self.snapshot)
        self.assertEqual(len(parsed.signals), 1)

    def test_negative_rule_can_describe_what_to_avoid_without_positive_pattern_matching(self):
        value = self.negative()
        result = expression.parse(value, self.snapshot)
        self.assertEqual(result.signals[0].expression["polarity"], "negative")
        self.assertEqual(result.signals[0].evidence_handles, ("preset:1", "unreviewed:2"))
        self.assertIn("我将永远无条件陪伴你。", result.signals[0].proposal)


if __name__ == "__main__":
    unittest.main()
