"""Manual expression comparison against operator-approved dialogue."""

import json
import re
import unicodedata
from dataclasses import dataclass

from companion_v2.execution.models import TaskError, digest, encode, stamp
from companion_v2.expression import learning, rule_details
from companion_v2.expression.configuration import read_settings
from companion_v2.expression.style_evidence import ExpressionSampleCatalog

SOURCE_KIND = "preset_comparison"
CURSOR_PREFIX = "role.daily.expression.cursor.v1:"
MAX_COMPARISON_RULES = 3
INSTRUCTION = '表达对照学习：\napproved_dialogue 是人工认可的完整预设对话，代表希望你模仿的风格。只学习 assistant 一方，user 发言用于理解语境。unreviewed_dialogue 是你生成的对话，它们没有被人工选入预设，这不代表它们一定是完全错误的，但绝非你要模仿的风格，提供这些对话给你是为了方便你了解自己以往是怎么生成的，方便你对照、发现和纠正其中潜在的错误表达。\n在相似情境中比较具体说法和接话方式，归纳贴近认可示例的表达规则；不从未认可内容单独建立偏好。每条候选至少引用一条 preset: 开头的认可 assistant 证据，可同时引用 unreviewed: 证据，说明规则适用的情境，保持认可示例中的自然变化，不把偶尔用法强制为每次使用。结果放在 expression 字段，使用本轮提供的规则 Schema；规则仅进入待审核列表。已经整理过的历史只作其他任务背景，不再作为本轮新的表达学习证据。每条候选填写 expression：kind=style 是具体情境表达，pattern 保留有辨识度的短说法或带占位的说法，例如在彼此已有共识、不需解释时用“懂的都懂”；kind=grammar 是可复用句法，pattern 写出结构，例如“先用两三个字确认，再省略主语接一句补充”。situation 说明何时合适，avoid 说明何时别用，proposal 说明角色怎样自然化用。\n不要把“更自然、简短口语化、温柔一些”这样的泛泛评价当作学到的表达。短而有辨识度的原话可以保留，长句、私人经历和身份信息不照搬；一次出现可成为局部情境候选，但不要宣称是长期习惯。样本、情境、说法、用法须对应，不能编造证据。对照 existing_rules 的情境与模板：同一说法、同一用法即使 observation 或 proposal 有新措辞也不要另提候选；已拒绝或已停用的规则也不要擅自复活。同一情境下不同的说法，以及相同说法在真正不同情境中的用法可以共存。\napplicability.channels 使用 private/proactive；relationship_stages 和 emotion_gates 无明确限制时填写 ["any"]。any 单独使用，不与其他值并列；keywords 可为空数组。'
COMPARISON_INSTRUCTION = (
    '本轮同时考虑正向表达与负面约束，模仿目标始终是 approved_dialogue 中的 assistant。'
    'expression.polarity=positive 表示建议采用，polarity=negative 表示建议避免。'
    '对照相似情境下的认可表达与本轮未认可回复，分析未认可回复在措辞或接话方式上有哪些不贴切之处，'
    '将有具体对照依据的问题提出为待审核的负面约束；仅在未认可回复中出现这一点本身不作为质量差的理由。'
    '负面规则的 observation 说明两边的语境与表达差异，pattern 写应避免的具体说法或句式，'
    'proposal 写清应避免什么及更接近认可示例的表达方向，situation 写约束适用的情境，avoid 写例外情境。'
    '正向规则的 pattern 与 proposal 继续描述值得借鉴的表达，avoid 写不适用情境。'
    '两类规则都先进入待审核列表。'
)
INSTRUCTION += '\n' + COMPARISON_INSTRUCTION
INSTRUCTION = INSTRUCTION.replace('已经整理过的历史只作其他任务背景，不再作为本轮新的表达学习证据。',
    '表达学习的语料范围以本轮 approved_dialogue 和 unreviewed_dialogue 为准。')
CONTRACT_INSTRUCTION = '输出约定：每次最多提出 3 条候选规则，正向表达与负面约束合计计算，优先提出有依据且与已有规则不同的候选。evidence_handles 只填写本轮输入已有的消息编号，程序会根据编号取回原文，模型无须复述引用文本。正向规则至少引用一个 preset: 编号，只从认可 assistant 的表达归纳可参考的说法；负面规则同时引用 preset: 与 unreviewed: 编号，对比相似情境下的表达差异，说明应避免什么。pattern 和 proposal 可以概括表达习惯或句式模板，无须与原句逐字一致；具体正向示范以认可示例为参考，未认可内容用于对照与规避分析。applicability.intent 可填单个意图或意图数组，多项表示任一意图适用，any 单独使用。'
PLATFORM_MARKERS = ("[回复]", "[图片]", "[语音]", "[视频]", "[文件]", "[分享卡片]", "[QQ表情:")


class ExpressionCandidateError(TaskError):
    """A single rejected candidate with bounded supporting diagnostics."""

    def __init__(self, code, reason, examples=()):
        super().__init__(code, reason)
        values, seen = [], set()
        for text in examples:
            if not isinstance(text, str) or not text:
                continue
            key = "".join(unicodedata.normalize("NFKC", text).split())
            if key not in seen:
                seen.add(key)
                values.append(text[:160])
                if len(values) == 8:
                    break
        self.unsupported_examples = tuple(values)


@dataclass(frozen=True)
class ComparisonAssessment:
    summary: str
    signals: tuple[learning.StyleSignal, ...]
    rejections: tuple[dict, ...]


def schema():
    """Reference source messages without making the model reproduce quotations."""
    result = learning.schema(MAX_COMPARISON_RULES)
    references = result["properties"]["signals"]["items"]["properties"]["evidence_handles"]
    references.update(minItems=1, maxItems=16, uniqueItems=True,
        items={"type": "string", "pattern": r"^(preset|unreviewed):[0-9]+$"})
    return result


def enabled(app, context, *, automatic=True):
    policy = read_settings(app.runtime, context)
    hook = getattr(app.gateway, "request_context", None)
    return (policy.enabled and not automatic
            and callable(getattr(hook, "expression_learning_source", None)))


def cursor_key(context):
    return CURSOR_PREFIX + digest([ExpressionSampleCatalog.role_owner(context), context.scope])


async def capture(service, context, *, automatic=True):
    """Capture approved examples and the configured incremental or full reply window.

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
    skip_processed = read_settings(service.runtime, context).skip_processed_replies
    after = cursor["sequence"] if skip_processed else 0
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
            "skip_processed_replies": skip_processed,
            "cursor_before": previous, "handles": handles, "existing_rules": existing_rules,
            "captured_at": stamp(service.tasks.now())}


def quoted_examples(text):
    """Read illustrative phrases in common prose/code quotation forms."""
    matches = re.findall(r'“([^”]*)”|「([^」]*)」|『([^』]*)』|‘([^’]*)’|"([^"\n]*)"|\'([^\'\n]*)\'|`([^`\n]*)`', text)
    return [value for parts in matches for value in parts if value]


def parse(raw, snapshot):
    value = json.loads(encode(raw))
    if isinstance(value, dict) and isinstance(value.get("signals"), list) and len(value["signals"]) > MAX_COMPARISON_RULES:
        raise TaskError("expression_rule_limit", "a comparison may propose at most three rules")
    approved = {key: item["content"] for key, item in snapshot["handles"].items()
                if key.startswith("preset:") and item.get("quality") == "approved" and item.get("role") == "assistant"}
    comparison = {key: item["content"] for key, item in snapshot["handles"].items()
                  if key.startswith("unreviewed:") and item.get("quality") == "unreviewed" and item.get("role") == "assistant"}
    allowed = set(approved) | set(comparison)
    for signal in value.get("signals", []):
        references = signal.get("evidence_handles")
        if (type(references) is not list or not references or len(references) > 16
                or any(type(handle) is not str or handle not in allowed for handle in references)):
            raise TaskError("expression_approved_evidence_required", "rule references must name original assistant messages")
        targets = list(dict.fromkeys(handle for handle in references if handle in approved))
        if not targets:
            raise TaskError("expression_approved_evidence_required", "rule evidence needs approved assistant sources")
        form = signal.get("expression", {})
        polarity = form.get("polarity", "positive")
        contrasts = list(dict.fromkeys(handle for handle in references if handle in comparison))
        if polarity == "negative" and not contrasts:
            raise TaskError("expression_counterexample_required", "a negative rule needs an unreviewed source message")
        # Older saved responses may include copied text; message IDs remain
        # authoritative, and these optional fields never rebind their sources.
        signal.pop("approved_quotes", None)
        signal.pop("counterexamples", None)
        signal["evidence_handles"] = targets + (contrasts if polarity == "negative" else [])
        pattern, proposal = form.get("pattern", ""), signal.get("proposal", "")
        examples = [pattern, *quoted_examples(pattern), *quoted_examples(proposal)]
        markers = [marker for marker in PLATFORM_MARKERS if any(marker in text for text in examples)]
        if markers:
            raise ExpressionCandidateError("expression_platform_marker",
                "QQ record placeholders do not describe a speaking style", markers)
        if polarity == "positive":
            canonical = lambda text: "".join(unicodedata.normalize("NFKC", text).split())
            positives = [canonical(text) for text in approved.values()]
            originals = list(comparison.values())
            unreviewed = [canonical(text) for text in originals]
            unsupported = [text for text in examples if len(canonical(text)) >= 2
                           and any(canonical(text) in source for source in unreviewed)
                           and not any(canonical(text) in source for source in positives)]
            advice = canonical(pattern + "\n" + proposal)
            unsupported.extend(text for text in originals if len(canonical(text)) >= 4
                               and canonical(text) in advice
                               and not any(canonical(text) in source for source in positives))
            if unsupported:
                raise ExpressionCandidateError("expression_unapproved_pattern",
                    "an unreviewed-only expression cannot become a positive rule example", unsupported)
    return learning.parse_output(encode(value), allowed_evidence=allowed,
                                 default_channels=("private", "proactive"))


def assess(raw, snapshot):
    """Validate independent candidates while retaining the exact rejection reasons."""
    value = json.loads(encode(raw))
    if (type(value) is not dict or set(value) != {"summary", "signals"}
            or type(value["signals"]) is not list):
        raise TaskError("expression_output_invalid", "expression output needs a summary and bounded candidate list")
    if len(value["signals"]) > MAX_COMPARISON_RULES:
        raise TaskError("expression_rule_limit", "a comparison may propose at most three rules")
    approved = {key for key, item in snapshot["handles"].items()
                if item.get("quality") == "approved" and item.get("role") == "assistant"}
    try:
        base = learning.parse_output(encode({"summary": value["summary"], "signals": []}),
                                     allowed_evidence=approved)
    except (ValueError, TypeError, KeyError) as error:
        raise TaskError("expression_output_invalid", "expression summary is invalid") from error
    accepted, rejections = [], []
    for index, candidate in enumerate(value["signals"], 1):
        try:
            result = parse({"summary": base.summary, "signals": [candidate]}, snapshot)
            accepted.extend(result.signals)
        except (TaskError, ValueError, TypeError, KeyError, AttributeError) as error:
            form = candidate.get("expression") if isinstance(candidate, dict) else None
            pattern = form.get("pattern", "") if isinstance(form, dict) else ""
            code = getattr(error, "code", None) or {
                "invalid_applicability": "expression_applicability_invalid",
                "invalid_expression_form": "expression_form_invalid",
            }.get(str(error), "expression_candidate_invalid")
            rejections.append({"index": index, "code": code,
                "pattern": pattern[:200] if isinstance(pattern, str) else "",
                "unsupported_examples": list(getattr(error, "unsupported_examples", ()))})
    return ComparisonAssessment(base.summary, tuple(accepted), tuple(rejections))


def validate(runtime, context, snapshot):
    row = runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (cursor_key(context),))
    current = row["value"] if row else None
    if current != snapshot["cursor_before"]:
        raise TaskError("expression_review_stale", "another review has already processed this source window")


def evidence(window, handles, *, polarity="positive"):
    snapshot = window.get("comparison", {})
    values = snapshot.get("handles", {})
    allowed = {"approved", "unreviewed"} if polarity == "negative" else {"approved"}
    if (polarity not in ("positive", "negative") or not handles
            or any(handle not in values or values[handle].get("quality") not in allowed
                   or values[handle].get("role") != "assistant" for handle in handles)
            or not any(values[handle]["quality"] == "approved" for handle in handles)
            or polarity == "negative" and not any(values[handle]["quality"] == "unreviewed" for handle in handles)):
        raise TaskError("expression_sample_changed", "comparison evidence is unavailable")
    return [("认可示例" if values[handle]["quality"] == "approved" else "对照反例")
            + "：[" + handle + "] " + values[handle]["content"] for handle in handles]


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
    rejections = list(getattr(result, "rejections", ()))
    receipt = {"run_id": None, "pending_rules": 0, "after_sequence": snapshot["after_sequence"],
        "through_sequence": snapshot["through_sequence"], "cursor_advanced": not rejections,
        "rejected_rules": len(rejections), "rejections": rejections}
    if rejections and not result.signals:
        return receipt
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
    if not rejections:
        previous_sequence = json.loads(snapshot["cursor_before"])["sequence"] if snapshot["cursor_before"] else 0
        connection.execute("INSERT INTO runtime_metadata(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (cursor_key(context), encode({"sequence": max(previous_sequence, snapshot["through_sequence"]), "source_key": snapshot["source_key"],
                                        "reviewed_at": at, "run_id": run_id})))
    return {**receipt, "run_id": identifier, "pending_rules": len(accepted)}
