"""Multiple applicability intents survive review and match as alternatives."""

import copy
import json
from types import SimpleNamespace
import unittest

from companion_v2.conversation.foreground import (
    PromptMaterial, PromptRequest, SealedForegroundContext,
)
from companion_v2.conversation.generation import _filter_expression_style_materials
from companion_v2.conversation.style_context import EXPRESSION_STYLE_INSTRUCTION
from companion_v2.execution.models import TaskContext
from companion_v2.expression import learning
from companion_v2.expression.sharing import build_pack, normalize_pack
from companion_v2.expression.style_management import ExpressionStyleManagement
from companion_v2.foundation.refs import AudienceRef
from tests.test_host_application import HostFixture
from tests.test_management_access import SCOPE


FORM = {
    "kind": "grammar", "polarity": "positive", "situation": "回应提问或安慰对方时",
    "pattern": "先简短回应，再补充与当前话题相关的话", "avoid": "需要明确边界时",
}
RULE = {
    "dimension": "sentence_rhythm", "observation": "先回应对方，再补充当前话题。",
    "proposal": "结合当前问题或情绪，用短句回应后再作补充。", "confidence": 0.7,
    "expression": FORM,
    "applicability": {
        "channels": ["private"], "relationship_stages": ["any"],
        "emotion_gates": ["any"], "intent": ["question", "comfort"], "keywords": [],
    },
}


class IntentChoiceContractTests(unittest.TestCase):
    def test_legacy_strings_and_missing_intent_keep_their_meaning(self):
        self.assertEqual(learning.normalize_applicability()["intent"], "any")
        for intent in learning.INTENTS:
            with self.subTest(intent=intent):
                self.assertEqual(learning.normalize_applicability({"intent": intent})["intent"], intent)

    def test_lists_are_canonical_without_mutating_the_input(self):
        raw = {"intent": ["question", "comfort"]}
        before = copy.deepcopy(raw)
        self.assertEqual(learning.normalize_applicability(raw)["intent"], ["comfort", "question"])
        self.assertEqual(raw, before)
        for intent in learning.INTENTS:
            with self.subTest(intent=intent):
                self.assertEqual(learning.normalize_applicability({"intent": [intent]})["intent"], intent)

    def test_invalid_intents_retain_a_precise_reason_and_common_field_code(self):
        cases = [
            ([], "empty"), ("", "empty"), ("  ", "empty"),
            ("unknown", "unknown"), (["question", "unknown"], "unknown"),
            (["question", "question"], "duplicate"), (["any", "question"], "any_mixed"),
            (["question", None], "type"), (None, "type"), (True, "type"),
            (("question", "comfort"), "type"),
            (["question"] * (len(learning.INTENTS) + 1), "limit"),
        ]
        for value, reason in cases:
            with self.subTest(value=value), self.assertRaises(learning.ApplicabilityIntentError) as caught:
                learning.normalize_applicability({"intent": value})
            error = caught.exception
            self.assertIsInstance(error, ValueError)
            self.assertEqual(error.code, "expression_applicability_invalid")
            self.assertEqual(error.field, "intent")
            self.assertEqual(error.reason, reason)

    def test_parser_keeps_all_intents_and_does_not_replace_intent_errors(self):
        item = copy.deepcopy(RULE)
        item["evidence_handles"] = ["preset:1"]
        output = learning.parse_output({"summary": "形式观察", "signals": [item]},
                                       allowed_evidence={"preset:1"})
        self.assertEqual(output.signals[0].applicability["intent"], ["comfort", "question"])
        item["applicability"]["intent"] = ["question", "question"]
        with self.assertRaises(learning.ApplicabilityIntentError) as caught:
            learning.parse_output({"summary": "形式观察", "signals": [item]},
                                  allowed_evidence={"preset:1"})
        self.assertEqual(caught.exception.reason, "duplicate")

    def test_schema_exposes_legacy_strings_and_exclusive_any_arrays(self):
        schema = learning.schema()["properties"]["signals"]["items"]
        choices = schema["properties"]["applicability"]["properties"]["intent"]["oneOf"]
        string, multiple, any_only = choices
        self.assertEqual(set(string["enum"]), learning.INTENTS)
        self.assertEqual(multiple["type"], "array")
        self.assertTrue(multiple["uniqueItems"])
        self.assertEqual(multiple["minItems"], 1)
        self.assertEqual(multiple["maxItems"], len(learning.INTENTS) - 1)
        self.assertEqual(set(multiple["items"]["enum"]), learning.INTENTS - {"any"})
        self.assertEqual((any_only["minItems"], any_only["maxItems"]), (1, 1))
        self.assertEqual(any_only["items"]["enum"], ["any"])

    def test_each_intent_is_an_alternative_in_laya_matching(self):
        applicability = {"intent": ["comfort", "question"]}
        self.assertTrue(learning.laya_intent_matches(applicability,
            {"reply_intent": "answer", "reply_style": "normal"}))
        self.assertTrue(learning.laya_intent_matches(applicability,
            {"reply_intent": "comfort", "reply_style": "normal"}))
        self.assertFalse(learning.laya_intent_matches(applicability,
            {"reply_intent": "explain", "reply_style": "normal"}))
        self.assertFalse(learning.laya_intent_matches(applicability, {}))
        self.assertTrue(learning.laya_intent_matches({"intent": ["play", "casual"]},
            {"reply_intent": "acknowledge", "reply_style": "playful"}))
        self.assertFalse(learning.laya_intent_matches({"intent": ["play", "casual"]},
            {"reply_intent": "acknowledge", "reply_style": "normal"}))
        self.assertTrue(learning.laya_intent_matches({"intent": ["any"]}, None))

    def test_signatures_compare_canonical_choices_without_losing_intents(self):
        def signature(intent):
            return learning.candidate_signature(RULE["dimension"], RULE["observation"],
                RULE["proposal"], {**RULE["applicability"], "intent": intent}, FORM)

        self.assertEqual(signature(["question", "comfort"]), signature(["comfort", "question"]))
        self.assertEqual(signature(["question"]), signature("question"))
        self.assertNotEqual(signature(["question", "comfort"]), signature("question"))
        self.assertNotEqual(signature(["question", "comfort"]), signature("any"))

    def test_shared_pack_round_trip_preserves_the_choices_and_old_strings(self):
        for intent, expected in [(["question", "comfort"], ["comfort", "question"]),
                                 ("question", "question"), (["question"], "question")]:
            with self.subTest(intent=intent):
                rule = {**RULE, "applicability": {**RULE["applicability"], "intent": intent}}
                package = build_pack([rule])
                self.assertEqual(package["rule_groups"][0]["rules"][0]["intent"], expected)
                normalized = normalize_pack(package)
                self.assertEqual(normalized["signals"][0]["applicability"]["intent"], expected)
                self.assertEqual(normalized["rejected"], [])


class IntentChoiceReviewTests(HostFixture, unittest.IsolatedAsyncioTestCase):
    def selected_context(self):
        binding = self.identity.resolve_role(SCOPE, observed_at=self.clock.now()).unwrap()
        return TaskContext(self.important, binding, AudienceRef(SCOPE, (self.important.subject,)))

    async def test_review_storage_application_and_export_preserve_or_semantics(self):
        host = await self.start()
        app = host.application
        manager = ExpressionStyleManagement(app)
        context = self.selected_context()
        package = build_pack([RULE])
        preview = manager.preview_import(operator=self.core, context=context, raw_pack=package).unwrap()
        manager.import_pack(operator=self.core, context=context, raw_pack=package,
            preview_token=preview["preview_token"], operation_key="intent-choice-import").unwrap()
        pending = manager.list(operator=self.core, context=context, state="pending").unwrap()["items"][0]
        self.assertEqual(pending["applicability"]["intent"], ["comfort", "question"])
        manager.command(operator=self.core, context=context, signal_id=pending["signal_id"],
            action="adopt", view_token=pending["view_token"], operation_key="intent-choice-adopt").unwrap()

        snapshot = app.expression_style_context.capture(context, self.clock.now())
        contributions = app.expression_style_context.contributions(snapshot, context, self.clock.now())
        materials = tuple(PromptMaterial(item.owner, item.purpose, item.payload) for item in contributions)
        payload = next(json.loads(item.payload) for item in materials if item.purpose == "expression_style_rule")
        self.assertEqual(payload["applicability"]["intent"], ["comfort", "question"])
        event = SimpleNamespace(scope=context.scope, runtime_scope=None)
        request = PromptRequest(event, "", SealedForegroundContext(materials, (), ()),
            "Reply\n" + EXPRESSION_STYLE_INSTRUCTION, context.role_binding)
        for reply_intent in ("answer", "comfort"):
            with self.subTest(reply_intent=reply_intent):
                filtered = _filter_expression_style_materials(request,
                    {"reply_intent": reply_intent, "reply_style": "normal"})
                matched = [json.loads(item.payload) for item in filtered.context.materials
                           if item.purpose == "expression_style_rule"]
                self.assertEqual(len(matched), 1)
                self.assertEqual(matched[0]["applicability"]["intent"], ["comfort", "question"])
        filtered = _filter_expression_style_materials(request,
            {"reply_intent": "explain", "reply_style": "normal"})
        self.assertFalse(any(item.purpose == "expression_style_rule" for item in filtered.context.materials))
        exported = manager.export_pack(operator=self.core, context=context).unwrap()["package"]
        self.assertEqual(normalize_pack(exported)["signals"][0]["applicability"]["intent"],
                         ["comfort", "question"])
        self.assertEqual(self.called, [])


if __name__ == "__main__":
    unittest.main()
