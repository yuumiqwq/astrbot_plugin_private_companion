"""Incremental expression comparison against operator-approved dialogue."""

import json
import re

from companion_v2.execution.models import TaskError, digest, encode, stamp
from companion_v2.expression import learning, rule_details
from companion_v2.expression.configuration import read_settings
from companion_v2.expression.style_evidence import ExpressionSampleCatalog

SOURCE_KIND = "preset_comparison"
CURSOR_PREFIX = "role.daily.expression.cursor.v1:"
INSTRUCTION = (
    "表达对照学习：approved_dialogue 是人工认可的完整预设对话，代表希望模仿的风格。"
    "只学习 assistant 一方，user 发言用于理解语境。unreviewed_dialogue 是上次成功整理后新增的对话，"
    "其中 assistant 回复尚未被人工选入预设；这不表示错误、低质量或需要纠正，也不把出现频率当作认可。"
    "在相似情境中比较具体说法和接话方式，归纳贴近认可示例的表达规则；不从未认可内容单独建立偏好。"
    "每条候选至少引用一条 preset: 开头的认可 assistant 证据，可同时引用 unreviewed: 证据，"
    "说明规则适用的情境，保持认可示例中的自然变化，不把偶尔用法强制为每次使用。"
    "结果放在 expression 字段，使用本轮提供的规则 Schema；规则仅进入待审核列表。"
    "已经整理过的历史只作其他任务背景，不再作为本轮新的表达学习证据。"
    + learning.FORM_INSTRUCTION
)


def enabled(app, context, *, automatic=True):
    policy = read_settings(app.runtime, context)
    hook = getattr(app.gateway, "request_context", None)
    return (policy.enabled and (not automatic or policy.daily_comparison_enabled)
            and callable(getattr(hook, "expression_learning_source", None)))


def cursor_key(context):
    return CURSOR_PREFIX + digest([ExpressionSampleCatalog.role_owner(context), context.scope])


async def capture(service, context, *, automatic=True):
    """Capture the full approved corpus and only unprocessed source replies.

    Args:
        service: Daily review coordinator.
        context: Exact private role context.
        automatic: Whether the daily automation switch applies.

    Returns:
        A durable snapshot, or None when this review has no comparison work.
    """
    if not enabled(service.app, context, automatic=automatic):
        return None
    source = await service.gateway.request_context.expression_learning_source(context)
    row = service.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (cursor_key(context),))
    previous = row["value"] if row else None
    cursor = json.loads(previous) if previous else {"sequence": 0, "source_key": source["source_key"]}
    if cursor["source_key"] != source["source_key"]:
        raise TaskError("expression_source_changed", "the comparison source changed")
    approved, handles, recognized = [], {}, set()
    for index, message in enumerate(source["approved_dialogue"]):
        if message.get("role") not in ("system", "user", "assistant") or not isinstance(message.get("content"), str):
            continue
        item = {"role": message["role"], "content": message["content"]}
        if item["role"] == "assistant":
            item["handle"] = "preset:" + str(index)
            handles[item["handle"]] = {**item, "quality": "approved"}
            recognized.add(item["content"].strip())
            recognized.update(part.strip() for part in re.split(r"\n\s*\n", item["content"]) if part.strip())
        approved.append(item)
    if not handles:
        return None
    after = cursor["sequence"]
    unreviewed, through = [], after
    for message in source["dialogue"]:
        sequence = message.get("_qq_sequence")
        if type(sequence) is not int or sequence <= after or message.get("_no_save"):
            continue
        through = max(through, sequence)
        role, content = message.get("role"), message.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
            continue
        if role == "assistant" and content.strip() in recognized:
            continue
        item = {"role": role, "content": content, "sequence": sequence, "sent_at": message.get("_qq_time")}
        if role == "assistant":
            item["handle"] = "unreviewed:" + str(sequence)
            handles[item["handle"]] = {**item, "quality": "unreviewed"}
        unreviewed.append(item)
    if not any(item["role"] == "assistant" for item in unreviewed):
        return None
    existing = service.runtime.read_all("""SELECT s.signal_id,s.dimension,s.observation,s.proposal,
        s.applicability_json,s.state FROM expression_style_signals s WHERE owner_key=? AND scope_key=?
        ORDER BY created_at DESC LIMIT 120""",
        (digest(ExpressionSampleCatalog.role_owner(context)), digest(context.scope)))
    existing_rules = [{"dimension": item["dimension"], "proposal": item["proposal"],
                       "expression": rule_details.form_for(service.runtime, item), "state": item["state"]}
                      for item in existing]
    return {"source_kind": SOURCE_KIND, "source_key": source["source_key"],
            "approved_dialogue": approved, "unreviewed_dialogue": unreviewed,
            "after_sequence": after, "through_sequence": through,
            "cursor_before": previous, "handles": handles, "existing_rules": existing_rules,
            "captured_at": stamp(service.tasks.now())}


def parse(raw, snapshot):
    result = learning.parse_output(encode(raw), allowed_evidence=set(snapshot["handles"]),
                                   default_channels=("private", "proactive"))
    if any(not any(handle.startswith("preset:") for handle in item.evidence_handles)
           for item in result.signals):
        raise TaskError("expression_approved_evidence_required", "each rule needs approved assistant evidence")
    return result


def validate(runtime, context, snapshot):
    row = runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (cursor_key(context),))
    current = row["value"] if row else None
    if current != snapshot["cursor_before"]:
        raise TaskError("expression_review_stale", "another review has already processed this source window")


def evidence(window, handles):
    snapshot = window.get("comparison", {})
    values = snapshot.get("handles", {})
    if any(handle not in values for handle in handles):
        raise TaskError("expression_sample_changed", "comparison evidence is unavailable")
    return [("认可示例" if values[handle]["quality"] == "approved" else "未选入预设的回复")
            + "：" + values[handle]["content"] for handle in handles]


def commit(service, context, snapshot, result, run_id, connection):
    """Save reviewable candidates and their source cursor in the daily transaction.

    Args:
        service: Daily review coordinator.
        context: Exact private role context.
        snapshot: Captured approved corpus and incremental source window.
        result: Validated expression candidates.
        run_id: Idempotent daily or manual operation identity.
        connection: Existing daily transaction.

    Returns:
        The source window and number of newly pending rules.
    """
    validate(service.runtime, context, snapshot)
    owner_key = digest(ExpressionSampleCatalog.role_owner(context))
    scope_key = digest(context.scope)
    rows = connection.execute("""SELECT dimension,observation,proposal,applicability_json,signal_id
        FROM expression_style_signals WHERE owner_key=? AND scope_key=?""", (owner_key, scope_key)).fetchall()
    signatures = {learning.candidate_signature(row["dimension"], row["observation"], row["proposal"],
        json.loads(row["applicability_json"]), rule_details.form_for(service.runtime, row)) for row in rows}
    forms = {learning.form_identity(rule_details.form_for(service.runtime, row)) for row in rows}
    capacity = read_settings(service.runtime, context).active_signal_limit
    active = connection.execute("""SELECT COUNT(*) n FROM expression_style_signals
        WHERE owner_key=? AND scope_key=? AND state IN ('pending','adopted')""", (owner_key, scope_key)).fetchone()["n"]
    accepted = []
    for item in result.signals:
        signature = learning.candidate_signature(item.dimension, item.observation, item.proposal,
                                                 item.applicability, item.expression)
        identity = learning.form_identity(item.expression)
        if (signature not in signatures and identity not in forms
                and len(accepted) < max(0, capacity - active)):
            accepted.append(item)
            signatures.add(signature)
            forms.add(identity)
    identifier = "daily-expression:" + run_id
    at = stamp(service.tasks.now())
    window = {"opened_at": snapshot["captured_at"], "closed_at": snapshot["captured_at"],
              "handles": sorted(snapshot["handles"]),
              "characters": sum(len(item["content"]) for item in snapshot["handles"].values()),
              "source_kind": SOURCE_KIND, "context_key": context.key, "comparison": snapshot}
    connection.execute("INSERT INTO expression_style_runs VALUES (?,?,?,?,?,?,'pending',?,NULL)",
        (identifier, owner_key, scope_key, encode(window), digest(snapshot), result.summary, at))
    for index, item in enumerate(accepted):
        signal = identifier + ":signal:" + str(index)
        connection.execute("""INSERT INTO expression_style_signals
            (signal_id,run_id,owner_key,scope_key,dimension,observation,proposal,applicability_json,
             confidence,evidence_json,state,created_at,decided_at,decided_by,decision_key,ordinal)
             VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?,NULL,NULL,NULL,?)""",
            (signal, identifier, owner_key, scope_key, item.dimension, item.observation, item.proposal,
             encode(item.applicability), item.confidence, encode(sorted(item.evidence_handles)), at, index))
        if item.expression is not None:
            rule_details.write(service.runtime, signal, item.expression, at=service.tasks.now(),
                               actor="learning_model", operation_key=identifier)
    connection.execute("INSERT INTO runtime_metadata(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (cursor_key(context), encode({"sequence": snapshot["through_sequence"], "source_key": snapshot["source_key"],
                                    "reviewed_at": at, "run_id": run_id})))
    return {"run_id": identifier, "pending_rules": len(accepted), "after_sequence": snapshot["after_sequence"],
            "through_sequence": snapshot["through_sequence"]}
