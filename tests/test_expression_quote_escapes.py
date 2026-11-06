"""Legacy copied text cannot override authoritative source message numbers."""

from copy import deepcopy
import unittest

from companion_v2.execution.models import TaskError, encode
from companion_v2.role_work.daily import expression
from tests import test_daily_expression_comparison as comparison_tests


class LegacyQuotationCompatibilityTests(unittest.TestCase):
    setUp = comparison_tests.ApprovedTargetParserTests.setUp

    def test_copied_text_format_never_changes_the_numbered_source(self):
        source = "第一句\n\n第二句"
        self.snapshot["handles"]["preset:1"]["content"] = source
        for quote in (source, r"第一句\n\n第二句", "第一句 第二句", "与编号原文无关的旧文本"):
            with self.subTest(quote=quote):
                value = deepcopy(self.value)
                value["signals"][0]["approved_quotes"] = [{"evidence_handle": "preset:1", "quote": quote}]
                original = encode(value)
                parsed = expression.parse(value, self.snapshot)
                self.assertEqual(parsed.signals[0].evidence_handles, ("preset:1",))
                self.assertEqual(encode(value), original)

    def test_a_valid_legacy_quote_does_not_repair_an_unknown_message_number(self):
        self.value["signals"][0]["evidence_handles"] = ["preset:9999"]
        with self.assertRaises(TaskError) as caught:
            expression.parse(self.value, self.snapshot)
        self.assertEqual(caught.exception.code, "expression_approved_evidence_required")

    def test_negative_rules_use_both_numbered_sources_without_copying_quotations(self):
        signal = self.value["signals"][0]
        signal["expression"].update(polarity="negative", kind="grammar", pattern="用动作替代对问题的回答")
        signal["evidence_handles"] = ["preset:1", "unreviewed:2"]
        signal.pop("approved_quotes", None)
        parsed = expression.parse(self.value, self.snapshot)
        self.assertEqual(parsed.signals[0].evidence_handles, ("preset:1", "unreviewed:2"))
        self.assertEqual(parsed.signals[0].expression["polarity"], "negative")

    def test_generalized_style_template_needs_no_verbatim_duplicate(self):
        signal = self.value["signals"][0]
        self.snapshot["handles"]["preset:1"]["content"] = "明明是在努力学习"
        signal["expression"]["pattern"] = "明明是在…"
        signal["proposal"] = "轻松接话时简短补充不同的解释。"
        signal.pop("approved_quotes", None)
        parsed = expression.parse(self.value, self.snapshot)
        self.assertEqual(parsed.signals[0].expression["pattern"], "明明是在…")
