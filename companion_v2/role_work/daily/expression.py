"""Incremental expression comparison against operator-approved dialogue."""

import json
import re
import unicodedata

from companion_v2.execution.models import TaskError, digest, encode, stamp
from companion_v2.expression import learning, rule_details
from companion_v2.expression.configuration import read_settings
from companion_v2.expression.style_evidence import ExpressionSampleCatalog

SOURCE_KIND = "preset_comparison"
CURSOR_PREFIX = "role.daily.expression.cursor.v1:"
INSTRUCTION = '表达对照学习：\napproved_dialogue 是人工认可的完整预设对话，代表希望你模仿的风格。只学习 assistant 一方，user 发言用于理解语境。unreviewed_dialogue 是你生成的对话，它们没有被人工选入预设，这不代表它们一定是完全错误的，但绝非你要模仿的风格，提供这些对话给你是为了方便你了解自己以往是怎么生成的，方便你对照、发现和纠正其中潜在的错误表达。\n在相似情境中比较具体说法和接话方式，归纳贴近认可示例的表达规则；不从未认可内容单独建立偏好。每条候选至少引用一条 preset: 开头的认可 assistant 证据，可同时引用 unreviewed: 证据，说明规则适用的情境，保持认可示例中的自然变化，不把偶尔用法强制为每次使用。结果放在 expression 字段，使用本轮提供的规则 Schema；规则仅进入待审核列表。已经整理过的历史只作其他任务背景，不再作为本轮新的表达学习证据。每条候选填写 expression：kind=style 是具体情境表达，pattern 保留有辨识度的短说法或带占位的说法，例如在彼此已有共识、不需解释时用“懂的都懂”；kind=grammar 是可复用句法，pattern 写出结构，例如“先用两三个字确认，再省略主语接一句补充”。situation 说明何时合适，avoid 说明何时别用，proposal 说明角色怎样自然化用。\n不要把“更自然、简短口语化、温柔一些”这样的泛泛评价当作学到的表达。短而有辨识度的原话可以保留，长句、私人经历和身份信息不照搬；一次出现可成为局部情境候选，但不要宣称是长期习惯。样本、情境、说法、用法须对应，不能编造证据。对照 existing_rules 的情境与模板：同一说法、同一用法即使 observation 或 proposal 有新措辞也不要另提候选；已拒绝或已停用的规则也不要擅自复活。同一情境下不同的说法，以及相同说法在真正不同情境中的用法可以共存。\napplicability.channels 使用 private/proactive；relationship_stages 和 emotion_gates 无明确限制时填写 ["any"]。any 单独使用，不与其他值并列；keywords 可为空数组。'
CONTRACT_INSTRUCTION = '输出约定：approved_quotes 每项填写 evidence_handle 和该认可 assistant 中连续、准确的 quote 原文。规则的学习依据只取这些认可原文；evidence_handles 中的 unreviewed 引用仅作对照，不会成为正面依据。kind=style 的 pattern 从认可摘录中直接取一个连续短语，句法结构归入 kind=grammar。observation 和 proposal 中用于示范的具体例句也来自认可原文。'


def schema():
    """Describe approved-only evidence and verbatim supporting quotations."""
    result = learning.schema(3)
    signal = result["properties"]["signals"]["items"]
    signal["properties"]["approved_quotes"] = {
        "type": "array", "minItems": 1, "maxItems": 8,
        "items": {"type": "object", "additionalProperties": False,
                  "required": ["evidence_handle", "quote"], "properties": {
                      "evidence_handle": {"type": "string", "pattern": "^preset:"},
                      "quote": {"type": "string", "minLength": 1}}}}
    signal["required"].append("approved_quotes")
    return result


def enabled(app, context, *, automatic=True):
    policy = read_settings(app.runtime, context)
    hook = getattr(app.gateway, "request_context", None)
    return (policy.enabled and not automatic
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
        s.applicability_json,s.state FROM expression_style_signals s
        JOIN expression_style_runs r ON r.run_id=s.run_id WHERE s.owner_key=? AND s.scope_key=?
        AND (json_extract(r.sample_json,'$.source_kind') IS NOT 'preset_comparison'
             OR json_extract(r.sample_json,'$.comparison.schema_version')=2)
        ORDER BY s.created_at DESC LIMIT 120""",
        (digest(ExpressionSampleCatalog.role_owner(context)), digest(context.scope)))
    existing_rules = [{"dimension": item["dimension"], "proposal": item["proposal"],
                       "expression": rule_details.form_for(service.runtime, item), "state": item["state"]}
                      for item in existing]
    return {"source_kind": SOURCE_KIND, "schema_version": 2, "source_key": source["source_key"],
            "approved_dialogue": approved, "unreviewed_dialogue": unreviewed,
            "after_sequence": after, "through_sequence": through,
            "cursor_before": previous, "handles": handles, "existing_rules": existing_rules,
            "captured_at": stamp(service.tasks.now())}


def parse(raw, snapshot):
    value = json.loads(encode(raw))
    approved = {key: item["content"] for key, item in snapshot["handles"].items()
                if item.get("quality") == "approved" and item.get("role") == "assistant"}
    for signal in value.get("signals", []):
        references = signal.get("evidence_handles", [])
        if (not references or any(type(handle) is not str or handle not in snapshot["handles"] for handle in references)):
            raise TaskError("expression_approved_evidence_required", "rule references must name original sources")
        handles = [handle for handle in references if handle in approved]
        if not handles:
            raise TaskError("expression_approved_evidence_required", "rule evidence needs approved assistant sources")
        signal["evidence_handles"] = handles
        quotes = signal.pop("approved_quotes", None)
        if (not isinstance(quotes, list) or not quotes or len(quotes) > 8
                or any(type(item) is not dict or set(item) != {"evidence_handle", "quote"}
                       or item["evidence_handle"] not in handles or type(item["quote"]) is not str
                       or not item["quote"].strip() or item["quote"] not in approved[item["evidence_handle"]]
                       for item in quotes)):
            raise TaskError("expression_approved_quote_required", "each rule needs exact quotations from its approved evidence")
        form = signal.get("expression", {})
        if form.get("kind") == "style" and not any(form.get("pattern", "") in item["quote"] for item in quotes):
            raise TaskError("expression_unapproved_pattern", "a concrete expression must occur in its approved quotation")
        canonical = lambda text: "".join(unicodedata.normalize("NFKC", text).split())
        examples = [form.get("pattern", ""), *re.findall(r"[“「『](.*?)[”」』]", signal.get("proposal", ""))]
        positives = [canonical(text) for text in approved.values()]
        unreviewed = [canonical(item["content"]) for item in snapshot["unreviewed_dialogue"] if item["role"] == "assistant"]
        if any(len(canonical(text)) >= 2 and any(canonical(text) in source for source in unreviewed)
               and not any(canonical(text) in source for source in positives) for text in examples):
            raise TaskError("expression_unapproved_pattern", "an unreviewed-only expression cannot become a rule example")
    result = learning.parse_output(encode(value), allowed_evidence=set(approved),
                                   default_channels=("private", "proactive"))
    return result


def validate(runtime, context, snapshot):
    row = runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (cursor_key(context),))
    current = row["value"] if row else None
    if current != snapshot["cursor_before"]:
        raise TaskError("expression_review_stale", "another review has already processed this source window")


def evidence(window, handles):
    snapshot = window.get("comparison", {})
    values = snapshot.get("handles", {})
    if any(handle not in values or values[handle].get("quality") != "approved" for handle in handles):
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
    rows = connection.execute("""SELECT s.dimension,s.observation,s.proposal,s.applicability_json,s.signal_id
        FROM expression_style_signals s JOIN expression_style_runs r ON r.run_id=s.run_id
        WHERE s.owner_key=? AND s.scope_key=? AND (json_extract(r.sample_json,'$.source_kind') IS NOT 'preset_comparison'
        OR json_extract(r.sample_json,'$.comparison.schema_version')=2)""", (owner_key, scope_key)).fetchall()
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
