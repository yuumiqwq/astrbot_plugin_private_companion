"""Operator-adopted expression rules exercised through real reply boundaries.

All providers, users and platform receipts are synthetic. No V1 data is read.
"""

import asyncio
from dataclasses import replace
from datetime import datetime
import json
import unittest
from unittest.mock import patch

from astrbot.api.web import PluginRequest, bind_request_context
from starlette.requests import Request

from companion_v2.configuration.registry import FieldRegistry
from companion_v2.conversation.flow import ConversationPolicy
from companion_v2.conversation.foreground import ClaimKind, DraftClaim
from companion_v2.execution.models import TaskContext
from companion_v2.execution.runner import RunResult
from companion_v2.expression.configuration import (GROUP, PRIVATE_GROUP, private_target_for,
    target_for)
from companion_v2.expression.review import (ExpressionDraftReview, REVIEW_BUDGET_SECONDS,
    REVIEW_PROVIDER_TIMEOUT_SECONDS, REVIEW_WAIT_SECONDS, expression_review_profile, reviewed_text)
from companion_v2.expression.sharing import build_pack
from companion_v2.expression.style_management import ExpressionStyleManagement
from companion_v2.conversation.style_context import EXPRESSION_STYLE_INSTRUCTION
from companion_v2.foundation.refs import (AudienceRef)
from companion_v2.foundation.outcome import Outcome
from companion_v2.role_profile.configuration import (GROUP as ROLE_PROFILE_GROUP,
    RoleProfileConfigurationOwner, configuration_fields as role_profile_fields,
    target_for as role_profile_target_for)
from types import SimpleNamespace
from companion_v2.model_gateway.routing import configuration_fields
from tests.proactive_support import ContactFixture
from tests.group_host_support import GroupFixture
from tests.staging_bootstrap_support import wait_for_batch
from tests.test_host_application import HostFixture
from tests.test_management_access import SCOPE
from tests.test_role_tool_loop import answered


RULE = {"dimension": "sentence_rhythm", "observation": "回应计划时常用短句收束",
        "proposal": "回应计划时先给简短回应，再用短句自然收束", "confidence": 0.8,
        "expression": {"kind": "grammar", "situation": "确认计划或约定时",
            "pattern": "先用短句确认，再用短句自然收束", "avoid": "需要交代条件或细节时"}}
ORIGINAL = "嗯嗯，收到。\n晚点再聊。"
REVISED = "收到啦。\n晚点再聊。"


def materials(envelope, purpose):
    return tuple(item for item in envelope.original.context.materials if item.purpose == purpose)


def wire_materials(payload, purpose):
    return tuple(item for message in payload["messages"] if message["role"] == "user"
                 for item in json.loads(message["content"]).get("materials", []) if item["purpose"] == purpose)


class ExpressionFixture:
    def expression_profiles(self):
        self.profiles += (expression_review_profile("primary", max_output_tokens=100),
                          expression_review_profile("primary", max_output_tokens=100, contact=True))
        self.registry = FieldRegistry(version="expression-pipeline.v1", fields=configuration_fields(self.profiles))

    def configure_expression(self, app, changes, context=None):
        context = context or self.selected_context()
        shared = {key: value for key, value in changes.items() if key != "private_learning_enabled"}
        local = {key: value for key, value in changes.items() if key == "private_learning_enabled"}
        receipt = None
        if shared:
            target = target_for(installation_id=app.runtime.installation_id)
            snapshot = app.configuration.read(self.core, target=target, group_id=GROUP,
                                              observed_at=self.clock.now()).unwrap()
            receipt = app.configuration.patch(self.core, target=target, group_id=GROUP,
                expected_edit_token=snapshot.edit_token,
                changes={GROUP + "." + key: value for key, value in shared.items()},
                operation=self.operation(target.authorization_scope, owner=GROUP),
                observed_at=self.clock.now()).unwrap()
        if local:
            target = private_target_for(context)
            snapshot = app.configuration.read(self.core, target=target, group_id=PRIVATE_GROUP,
                                              observed_at=self.clock.now()).unwrap()
            receipt = app.configuration.patch(self.core, target=target, group_id=PRIVATE_GROUP,
                expected_edit_token=snapshot.edit_token,
                changes={PRIVATE_GROUP + "." + key: value for key, value in local.items()},
                operation=self.operation(target.authorization_scope, owner=PRIVATE_GROUP),
                observed_at=self.clock.now()).unwrap()
        return receipt

    def selected_context(self):
        binding = self.identity.resolve_role(SCOPE, observed_at=self.clock.now()).unwrap()
        return TaskContext(self.important, binding, AudienceRef(SCOPE, (self.important.subject,)))

    def adopt_pack(self, app, context=None):
        context = context or self.selected_context()
        manager = ExpressionStyleManagement(app)
        pack = build_pack([RULE], title="合成的短句表达")
        preview = manager.preview_import(operator=self.core, context=context, raw_pack=pack).unwrap()
        manager.import_pack(operator=self.core, context=context, raw_pack=pack,
            preview_token=preview["preview_token"], operation_key="pipeline-import").unwrap()
        row = manager.list(operator=self.core, context=context, state="pending").unwrap()["items"][0]
        manager.command(operator=self.core, context=context, signal_id=row["signal_id"],
            action="adopt", view_token=row["view_token"], operation_key="pipeline-adopt").unwrap()
        return row["signal_id"]

    async def expression_model(self, envelope):
        request = envelope.original
        if request.output_format == "role_expression_review":
            return answered(json.dumps({"decision": "rewrite", "text": REVISED}, ensure_ascii=False))
        return answered(ORIGINAL)



class ExpressionReplyPipelineTests(ExpressionFixture, HostFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.expression_profiles()
        self.model_behavior = self.expression_model

    async def seed(self, host, *, count=6):
        app = host.application
        context = self.selected_context()
        owner = RoleProfileConfigurationOwner(app.runtime)
        target = role_profile_target_for(context)
        profile = owner.read(target, role_profile_fields())
        owner.write(target, role_profile_fields(), changes={ROLE_PROFILE_GROUP + ".main_companion_subject":
            self.important.subject.subject_id}, reset=(), expected_revision=profile.revision,
            observed_at=self.clock.now())
        self.configure_expression(app, {"private_learning_enabled": False}, context)
        batch = None
        for index in range(count):
            batch = host.receive(self.event("seed-" + str(index), text="嗯哼，那就这么定啦。" + str(index))).unwrap()
        self.advance(app)
        await self.done(app, batch)
        return self.context(app, batch)

    async def reply(self, host, identifier="after-learning"):
        batch = host.receive(self.event(identifier, text="晚点再聊，好不好？")).unwrap()
        self.advance(host.application)
        row = await self.done(host.application, batch)
        self.assertEqual(row["state"], "complete", row["reason"])
        return batch

    async def test_operator_adopted_rule_is_applied_to_reply(self):
        host = await self.start()
        app = host.application
        context = await self.seed(host)
        self.adopt_pack(app, context)
        before = len(self.sent)
        await self.reply(host)
        envelope = next(item for item in reversed(self.called) if item.original.output_format == "conversation")
        self.assertIn(RULE["proposal"], materials(envelope, "expression_style_rule")[0].payload)
        self.assertIn(EXPRESSION_STYLE_INSTRUCTION, envelope.original.instruction)
        self.assertEqual([item.text.strip() for item in self.sent[before:]], REVISED.splitlines())
        adopted = ExpressionStyleManagement(app).list(operator=self.core, context=context, state="adopted").unwrap()["items"][0]
        self.assertEqual(adopted["injections"], 1)



    async def test_paused_rule_and_application_switch_stop_injection_without_blocking_reply(self):
        host = await self.start()
        app = host.application
        context = await self.seed(host)
        self.configure_expression(app, {"style_review_enabled": False})
        self.adopt_pack(app, context)
        manager = ExpressionStyleManagement(app)
        await self.reply(host, "with-rule")
        self.assertTrue(materials(self.called[-1], "expression_style_rule"))
        old = manager.list(operator=self.core, context=context, state="adopted").unwrap()["items"][0]
        manager.command(operator=self.core, context=context, signal_id=old["signal_id"], action="retire",
            view_token=old["view_token"], operation_key="retire-operator-rule").unwrap()
        await self.reply(host, "paused-rule")
        self.assertFalse(materials(self.called[-1], "expression_style_rule"))
        row = manager.list(operator=self.core, context=context, state="retired").unwrap()["items"][0]
        manager.command(operator=self.core, context=context, signal_id=row["signal_id"], action="restore",
            view_token=row["view_token"], operation_key="restore-operator-rule").unwrap()
        await self.reply(host, "restored-rule")
        self.assertTrue(materials(self.called[-1], "expression_style_rule"))
        self.configure_expression(app, {"private_application_enabled": False})
        await self.reply(host, "disabled-application")
        self.assertFalse(materials(self.called[-1], "expression_style_rule"))

    async def test_review_failure_timeout_and_cancel_keep_boundaries_and_clean_children(self):
        host = await self.start(self.host(policy=ConversationPolicy(0, 0, 180, 0)))
        app = host.application
        self.configure_expression(app, {"private_learning_enabled": False})
        self.adopt_pack(app)
        for failure in ("json", "exception", "timeout"):
            with self.subTest(failure=failure):
                entered, stopped = asyncio.Event(), asyncio.Event()
                async def model(envelope):
                    if envelope.original.output_format != "role_expression_review":
                        return answered(ORIGINAL)
                    entered.set()
                    if failure == "json":
                        return answered("{invalid")
                    if failure == "exception":
                        raise RuntimeError("synthetic provider failure")
                    try:
                        await asyncio.Event().wait()
                    finally:
                        stopped.set()
                self.model_behavior = model
                before = len(self.sent)
                with patch("companion_v2.expression.review.REVIEW_WAIT_SECONDS", 0.1):
                    await self.reply(host, "failed-" + failure)
                self.assertTrue(entered.is_set())
                self.assertEqual([item.text.strip() for item in self.sent[before:]], ORIGINAL.splitlines())
                self.assertFalse(app.expression_draft_review.active_handles)
                self.assertFalse(app.expression_draft_review.pending)
                if failure == "timeout":
                    self.assertTrue(stopped.is_set())
        entered = asyncio.Event()
        async def held_review(envelope):
            if envelope.original.output_format == "role_expression_review":
                entered.set()
                await asyncio.Event().wait()
            return answered(ORIGINAL)
        self.model_behavior = held_review
        before = len(self.sent)
        batch = host.receive(self.event("cancel-review")).unwrap()
        await asyncio.wait_for(entered.wait(), 5)
        app.cancel(self.context(app, batch), batch).unwrap()
        await self.until(lambda: not app.expression_draft_review.pending)
        self.assertEqual(len(self.sent), before, "a cancelled parent never sends its draft")
        self.assertFalse(app.expression_draft_review.active_handles)
        self.assertEqual(app.runtime.read_one("SELECT COUNT(*) n FROM execution_tasks WHERE kind = 'role.expression.review' AND state IN ('created', 'running', 'retry_scheduled')", ())["n"], 0)

    async def test_review_budget_exhaustion_keeps_original_and_is_logged(self):
        host = await self.start()
        app = host.application
        self.configure_expression(app, {"private_learning_enabled": False})
        self.adopt_pack(app)
        entered = asyncio.Event()
        async def held_review(envelope):
            if envelope.original.output_format == "role_expression_review":
                entered.set()
                await asyncio.Event().wait()
            return answered(ORIGINAL)
        self.model_behavior = held_review
        before = len(self.sent)
        with patch("companion_v2.expression.review.REVIEW_WAIT_SECONDS", 0.1), \
                self.assertLogs("companion_v2.expression.review", level="INFO") as captured:
            await self.reply(host, "budget-exhausted")
        self.assertTrue(entered.is_set())
        self.assertEqual([item.text.strip() for item in self.sent[before:]], ORIGINAL.splitlines())
        skipped = [record for record in captured.records
                   if "expression_review_budget_exhausted" in record.getMessage()]
        self.assertEqual([record.levelname for record in skipped], ["INFO"])
        self.assertIn("kind=role.expression.review", skipped[0].getMessage())
        row = app.runtime.read_one("SELECT created_at, deadline_at FROM execution_tasks "
                                   "WHERE kind = 'role.expression.review' ORDER BY created_at DESC LIMIT 1", ())
        self.assertAlmostEqual((datetime.fromisoformat(row["deadline_at"])
                                - datetime.fromisoformat(row["created_at"])).total_seconds(),
                               REVIEW_BUDGET_SECONDS, delta=1)
        self.assertFalse(app.expression_draft_review.active_handles)
        self.assertFalse(app.expression_draft_review.pending)

    async def test_optional_expression_read_failure_does_not_block_reply(self):
        host = await self.start()
        app = host.application
        self.configure_expression(app, {"private_learning_enabled": False})
        self.adopt_pack(app)
        with patch.object(app.expression_style_context, "_material", side_effect=RuntimeError("storage unavailable")):
            await self.reply(host)
        self.assertEqual(app.state, "running")

    async def test_unsuccessful_review_result_keeps_original(self):
        host = await self.start()
        app = host.application
        self.configure_expression(app, {"private_learning_enabled": False})
        self.adopt_pack(app)
        async def failed(context, task_id):
            app.expression_draft_review.results[task_id] = "不可采用的复核正文"
            return Outcome.success(RunResult(SimpleNamespace(state="failed")))
        before = len(self.sent)
        with patch.object(app.expression_draft_review.runner, "run", side_effect=failed):
            await self.reply(host, "unsuccessful-review")
        self.assertEqual([item.text.strip() for item in self.sent[before:]], ORIGINAL.splitlines())
        self.assertFalse(app.expression_draft_review.results)
        self.assertFalse(app.expression_draft_review.pending)


class ExpressionReviewContractTests(unittest.TestCase):
    def test_claim_statements_and_official_alert_body_are_preserved(self):
        claim = DraftClaim("question", ClaimKind.QUESTION, "明天 8 点见面？")
        draft = "嗯嗯，明天 8 点见面？"
        rewrite = json.dumps({"decision": "rewrite", "text": "明天 9 点见面？"}, ensure_ascii=False)
        self.assertEqual(reviewed_text(rewrite, draft, (claim,)), draft)
        original = "气象部门发布暴雨预警，请留意。"
        rewrite = json.dumps({"decision": "rewrite", "text": "可能下雨。"}, ensure_ascii=False)
        self.assertEqual(reviewed_text(rewrite, original, (), (original,)), original)
        safe = json.dumps({"decision": "rewrite", "text": "好呀，明天 8 点见面？"}, ensure_ascii=False)
        self.assertEqual(reviewed_text(safe, draft, (claim,)), "好呀，明天 8 点见面？")

    def test_every_review_budget_derives_from_one_ceiling(self):
        self.assertLess(REVIEW_PROVIDER_TIMEOUT_SECONDS, REVIEW_BUDGET_SECONDS)
        self.assertLess(REVIEW_BUDGET_SECONDS, REVIEW_WAIT_SECONDS)
        for contact in (False, True):
            profile = expression_review_profile("primary", max_output_tokens=100, contact=contact)
            self.assertEqual(profile.timeout_seconds, REVIEW_PROVIDER_TIMEOUT_SECONDS)
        registrations = ExpressionDraftReview(None).registrations
        self.assertEqual({registration.definition.timeout_seconds for registration in registrations},
                         {REVIEW_BUDGET_SECONDS})


class ExpressionGroupPipelineTests(GroupFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.controller.register_routes()
        self.routes = {(path, methods[0]): handler for path, handler, methods, _ in self.native.registered_web_apis}
        self.sequence = 0

    async def request(self, suffix, data=None, *, query=""):
        from companion_v2.execution.models import digest
        path = "/astrbot_plugin_private_companion/host/targets/group-a/roles/" + digest("role-a") + "/reply" + suffix
        body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else b""
        method = "POST" if data is not None else "GET"
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}
        native = Request({"type": "http", "method": method, "path": path, "headers": [],
                          "query_string": query.encode("utf-8")}, receive=receive)
        with bind_request_context(PluginRequest(native, username="admin")):
            result = await self.routes[(path, method)]()
        self.assertEqual(result["status"], "ok", result)
        return result["data"]


    async def input(self, sender="200"):
        self.sequence += 1
        event = self.group_event(sender=sender, identifier="expression-input-" + str(self.sequence),
            text="嗯哼，那就这么定啦。" + str(self.sequence))
        result = await self.controller.receive_claimed(event)
        self.assertTrue(result.is_success, result.failure)
        await wait_for_batch(self.controller.session, result.unwrap())
        app = self.controller.session.application
        return TaskContext.restore(json.loads(app.messages.batch(result.unwrap())["context_json"]))


    async def test_group_omits_sampling_controls_while_ordinary_reply_still_works(self):
        app = await self.start_host()
        for suffix, method in (("/expression", "GET"), ("/expression/configure", "POST"),
                ("/style/samples", "GET"), ("/style/samples/command", "POST"), ("/style/learn", "POST")):
            self.assertFalse(any(path.endswith(suffix) and verb == method for path, verb in self.routes))
        await self.input("200")
        context = await self.input("201")
        self.assertEqual(app.expression_style.status(context)["state"], "manual_only")
        self.assertEqual(app.expression_style.workers, {})
        self.assertNotIn("role.expression.learn", app.tasks.registrations)
        self.assertEqual(app.state, "running")

    async def test_revoking_optional_style_permission_does_not_block_ordinary_group_reply(self):
        app = await self.start_host()
        context = await self.input()
        row = app.runtime.read_one("SELECT grant_id FROM identity_grants WHERE capability = 'role.expression.read' AND json_extract(payload_json, '$.revoked_at') IS NULL LIMIT 1", ())
        self.assertIsNotNone(row, "native groups grant scoped expression access")
        app.tasks.identity.revoke_grant(row["grant_id"], expected_revision=app.tasks.identity.revision,
                                        revoked_at=self.clock.now())
        self.assertEqual((await self.input()).role_binding, context.role_binding)
        self.assertEqual(app.state, "running")


class ExpressionContactPipelineTests(ExpressionFixture, ContactFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.expression_profiles()

    async def contact_model(self, envelope):
        if envelope.original.output_format == "role_expression_review":
            self.assertFalse(self.sent, "wording review finishes before the first part is sent")
            return answered(json.dumps({"decision": "rewrite", "text": REVISED}, ensure_ascii=False))
        return answered(json.dumps({"decision": "send", "text": ORIGINAL}, ensure_ascii=False))

    async def test_proactive_uses_rules_review_and_first_confirmed_receipt(self):
        self.model_behavior = self.contact_model
        app = await self.enabled(limits=replace(self.limits, background_slots=2))
        self.configure_expression(app, {"private_learning_enabled": False})
        identifier = self.adopt_pack(app)
        task = (await self.contact(app)).unwrap().task
        await self.delivery_state(app, task.task_id, "delivered")
        contact = next(item for item in self.called if item.original.output_format != "role_expression_review")
        self.assertIn(RULE["proposal"], materials(contact, "expression_style_rule")[0].payload)
        self.assertTrue(any(item.original.output_format == "role_expression_review" for item in self.called))
        self.assertEqual("".join(item.text for item in self.sent).strip(), REVISED)
        manager = ExpressionStyleManagement(app)
        rows = manager.list(operator=self.core, context=self.selected_context(), state="adopted").unwrap()["items"]
        self.assertEqual(next(item["injections"] for item in rows if item["signal_id"] == identifier), 1)
        self.assertFalse(app.expression_draft_review.active_handles)

    async def test_single_background_slot_can_review_a_proactive_message(self):
        self.model_behavior = self.contact_model
        app = await self.enabled()
        self.configure_expression(app, {"private_learning_enabled": False})
        self.adopt_pack(app)
        task = (await self.contact(app)).unwrap().task
        await self.delivery_state(app, task.task_id, "delivered")
        self.assertEqual("".join(item.text for item in self.sent).strip(), REVISED)
        self.assertFalse(app.expression_draft_review.active_handles)
        self.assertFalse(app.expression_draft_review.pending)
        self.assertEqual(app.runtime.read_one("SELECT COUNT(*) n FROM execution_tasks WHERE kind = 'role.expression.review' AND state = 'created'", ())["n"], 0)

    async def test_administrator_test_never_reads_expression_or_memory(self):
        self.model_behavior = self.contact_model
        app = await self.enabled()
        self.adopt_pack(app)
        with patch.object(app.expression_style_context, "capture", side_effect=AssertionError("diagnostic must not read styles")), \
             patch.object(app.proactive.memory_context, "contact_capture", side_effect=AssertionError("diagnostic must not read memory")):
            task = (await self.contact(app, administrator_test=True)).unwrap().task
        await self.delivery_state(app, task.task_id, "delivered")
        self.assertFalse(any(materials(item, "expression_style_rule") for item in self.called))
        self.assertFalse(any(item.original.output_format == "role_expression_review" for item in self.called))


if __name__ == "__main__":
    unittest.main()
