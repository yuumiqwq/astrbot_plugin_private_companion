"""Serialized newlines retain exact assistant evidence without fuzzy matching."""

from copy import deepcopy
import unittest

from companion_v2.execution.models import TaskError, encode
from companion_v2.role_work.daily import expression
from tests import test_daily_expression_comparison as comparison_tests


class QuotationEscapeTests(unittest.TestCase):
    setUp = comparison_tests.ApprovedTargetParserTests.setUp

    def test_extra_newline_serialization_is_resolved_to_the_actual_approved_text(self):
        source = "诶？！\n\n我也有关注这个诶！"
        self.snapshot["handles"]["preset:1"]["content"] = source
        signal = self.value["signals"][0]
        signal["approved_quotes"][0]["quote"] = r"诶？！\n\n我也有关注这个诶！"
        signal["expression"]["pattern"] = "诶？！ / 我也有关注这个诶！"
        before = encode(self.value)
        parsed = expression.parse(self.value, self.snapshot)
        self.assertEqual(parsed.signals[0].evidence_handles, ("preset:1",))
        resolved = expression.approved_quotations(signal["approved_quotes"], ["preset:1"], {"preset:1": source})
        self.assertEqual(resolved[0]["quote"], source)
        self.assertEqual(encode(self.value), before)

    def test_crlf_and_lf_serialization_are_checked_against_real_source_spelling(self):
        for ending in ("\r\n", "\n"):
            with self.subTest(ending=repr(ending)):
                source = "第一句" + ending + "第二句"
                resolved = expression.approved_quotations(
                    [{"evidence_handle": "preset:1", "quote": r"第一句\r\n第二句"}], ["preset:1"], {"preset:1": source})
                self.assertEqual(resolved[0]["quote"], source)

    def test_real_literal_backslashes_are_preferred_over_a_decoded_alternative(self):
        source = r"第一句\n第二句"
        originals = {"preset:1": source, "preset:3": "第一句\n第二句"}
        resolved = expression.approved_quotations(
            [{"evidence_handle": "preset:1", "quote": source}], ["preset:1"], originals)
        self.assertEqual(resolved, [{"evidence_handle": "preset:1", "quote": source}])

    def test_unique_serialized_quote_can_repair_an_approved_handle(self):
        originals = {"preset:1": "另一个认可示例", "preset:3": "第一句\n\n第二句"}
        resolved = expression.approved_quotations(
            [{"evidence_handle": "preset:1", "quote": r"第一句\n\n第二句"}], ["preset:1"], originals)
        self.assertEqual(resolved, [{"evidence_handle": "preset:3", "quote": "第一句\n\n第二句"}])

    def test_ambiguous_serialized_quotes_stay_rejected(self):
        originals = {"preset:1": "另一个认可示例", "preset:3": "第一句\n第二句", "preset:5": "第一句\n第二句"}
        with self.assertRaises(TaskError) as caught:
            expression.approved_quotations(
                [{"evidence_handle": "preset:1", "quote": r"第一句\n第二句"}], ["preset:1"], originals)
        self.assertEqual(caught.exception.code, "expression_approved_quote_required")
        self.assertEqual(caught.exception.unsupported_examples, (r"第一句\n第二句",))

    def test_punctuation_whitespace_and_other_escape_sequences_are_not_rewritten(self):
        originals = {"preset:1": "你好！\n谢谢你。\n中文\n第一 句\n第二句"}
        for quote in (r"你好!\n谢谢你。", r"中\u6587", r"你好！\t谢谢你。", r"第一句\n第二句", r"\n"):
            with self.subTest(quote=quote), self.assertRaises(TaskError):
                expression.approved_quotations([{"evidence_handle": "preset:1", "quote": quote}], ["preset:1"], originals)

    def test_unreviewed_and_user_sources_do_not_become_approved_evidence(self):
        for role, quality in (("assistant", "unreviewed"), ("user", "approved")):
            with self.subTest(role=role, quality=quality):
                snapshot = deepcopy(self.snapshot)
                snapshot["handles"]["preset:1"].update(role=role, quality=quality, content="第一句\n第二句")
                value = deepcopy(self.value)
                value["signals"][0]["approved_quotes"][0]["quote"] = r"第一句\n第二句"
                with self.assertRaises(TaskError):
                    expression.parse(value, snapshot)

    def test_negative_counterexample_serialization_keeps_its_negative_source(self):
        source = "第一句\n第二句"
        self.snapshot["handles"]["unreviewed:2"]["content"] = source
        self.snapshot["unreviewed_dialogue"][0]["content"] = source
        signal = self.value["signals"][0]
        signal["expression"].update(polarity="negative", kind="grammar", pattern="把两个无关的句子硬接在一起")
        signal["evidence_handles"].append("unreviewed:2")
        signal["counterexamples"] = [{"evidence_handle": "unreviewed:2", "quote": r"第一句\n第二句"}]
        parsed = expression.parse(self.value, self.snapshot)
        self.assertEqual(parsed.signals[0].expression["polarity"], "negative")
        self.assertEqual(parsed.signals[0].evidence_handles, ("preset:1", "unreviewed:2"))
        resolved = expression.counterexample_quotations(signal["counterexamples"], ["unreviewed:2"], {"unreviewed:2": source})
        self.assertEqual(resolved[0]["quote"], source)

    def test_fabricated_multiline_quote_reports_the_offending_text(self):
        self.value["signals"][0]["approved_quotes"][0]["quote"] = r"原文里没有\n这句话"
        assessed = expression.assess(self.value, self.snapshot)
        self.assertFalse(assessed.signals)
        self.assertEqual(assessed.rejections[0]["unsupported_examples"], [r"原文里没有\n这句话"])
