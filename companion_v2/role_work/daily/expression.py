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
CONTRACT_INSTRUCTION = (
    '输出约定：每次最多提出 3 条候选规则，正向表达与负面约束合计计算；优先提出最有依据且与已有规则不同的候选。'
    '每条规则填写 expression.polarity。approved_quotes 每项填写 evidence_handle 和该编号对应的认可 '
    'assistant 中连续、准确的 quote 原文，直接复制编号与原文；两类规则都提供认可原文作为参照。'
    '引用中的换行按正常 JSON 字符串转义，解码后与原文一致，也可选择不跨行的连续短摘录。'
    'positive 的 counterexamples 填 []，kind=style 的 pattern 从认可摘录中取连续短语，'
    '多个变体可用 / 分隔且分别提供认可原文，句法结构归入 kind=grammar。'
    'negative 的 counterexamples 至少填写一项 unreviewed: 编号及对应 assistant 的准确 quote，'
    'kind=style 的 pattern 取自这些反例，kind=grammar 可概括反例中的句式问题。'
    'evidence_handles 包含本规则使用的认可及反例编号，反例只说明应避免的表达。'
    '对方的触发话语写在 expression.situation，对照分析放在 observation；'
    'proposal 中推荐的例句取自认可原文，负面规则引用反例时明确其为应避免的说法。'
)
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
    """Describe approved targets and explicitly separated negative examples."""
    result = learning.schema(MAX_COMPARISON_RULES)
    signal = result["properties"]["signals"]["items"]
    signal["properties"]["approved_quotes"] = {
        "type": "array", "minItems": 1, "maxItems": 8,
        "items": {"type": "object", "additionalProperties": False,
                  "required": ["evidence_handle", "quote"], "properties": {
                      "evidence_handle": {"type": "string", "pattern": "^preset:"},
                      "quote": {"type": "string", "minLength": 1}}}}
    signal["required"].append("approved_quotes")
    signal["properties"]["counterexamples"] = {
        "type": "array", "maxItems": 8,
        "items": {"type": "object", "additionalProperties": False,
                  "required": ["evidence_handle", "quote"], "properties": {
                      "evidence_handle": {"type": "string", "pattern": "^unreviewed:"},
                      "quote": {"type": "string", "minLength": 1}}}}
    signal["required"].append("counterexamples")
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


def source_quotation(handle, quote, originals):
    """Resolve exact text, accepting one extra serialization of line endings only."""
    decoded = quote.replace("\\r\\n", "\r\n").replace("\\n", "\n").replace("\\r", "\r")
    variants = dict.fromkeys((quote, decoded, decoded.replace("\r\n", "\n").replace("\r", "\n")))
    for text in variants:
        if not text.strip():
            continue
        if text in originals[handle]:
            return {"evidence_handle": handle, "quote": text}
        matches = [key for key, content in originals.items() if text in content]
        if len(matches) == 1:
            return {"evidence_handle": matches[0], "quote": text}
        if matches:
            return None
    return None


def approved_quotations(quotes, handles, approved):
    """Resolve a misnumbered citation only through its unique exact approved text."""
    if not isinstance(quotes, list) or not quotes or len(quotes) > 8:
        raise TaskError("expression_approved_quote_required", "each rule needs exact quotations from its approved evidence")
    resolved = []
    for item in quotes:
        if (type(item) is not dict or set(item) != {"evidence_handle", "quote"}
                or type(item["evidence_handle"]) is not str or item["evidence_handle"] not in handles
                or type(item["quote"]) is not str or not item["quote"].strip()):
            raise TaskError("expression_approved_quote_required", "each rule needs exact quotations from its approved evidence")
        reference = source_quotation(item["evidence_handle"], item["quote"], approved)
        if reference is None:
            raise ExpressionCandidateError("expression_approved_quote_required",
                "each rule needs exact quotations from its approved evidence", (item["quote"],))
        resolved.append(reference)
    return resolved


def approved_pattern(pattern, quotes):
    """Require exact approved support for a phrase or every slash-separated variant."""
    if type(pattern) is not str or not pattern.strip():
        return False
    if any(pattern in item["quote"] for item in quotes):
        return True
    variants = [item.strip() for item in re.split(r"[/／]", pattern)]
    return (len(variants) > 1 and all(variants)
            and all(any(variant in item["quote"] for item in quotes) for variant in variants))


def quoted_examples(text):
    """Read illustrative phrases in common prose/code quotation forms."""
    matches = re.findall(r'“([^”]*)”|「([^」]*)」|『([^』]*)』|‘([^’]*)’|"([^"\n]*)"|\'([^\'\n]*)\'|`([^`\n]*)`', text)
    return [value for parts in matches for value in parts if value]


def counterexample_quotations(quotes, handles, unreviewed):
    """Resolve exact counterexamples without treating them as approved targets."""
    if not isinstance(quotes, list) or not quotes:
        raise TaskError("expression_counterexample_required", "a negative rule needs an exact unreviewed counterexample")
    if len(quotes) > 8:
        raise TaskError("expression_counterexample_invalid", "counterexamples must quote their unreviewed assistant sources")
    resolved = []
    for item in quotes:
        if (type(item) is not dict or set(item) != {"evidence_handle", "quote"}
                or type(item["evidence_handle"]) is not str or item["evidence_handle"] not in handles
                or type(item["quote"]) is not str or not item["quote"].strip()):
            raise TaskError("expression_counterexample_invalid", "counterexamples must quote their unreviewed assistant sources")
        reference = source_quotation(item["evidence_handle"], item["quote"], unreviewed)
        if reference is None:
            raise ExpressionCandidateError("expression_counterexample_invalid",
                "counterexamples must quote their unreviewed assistant sources", (item["quote"],))
        resolved.append(reference)
    return resolved


def parse(raw, snapshot):
    value = json.loads(encode(raw))
    if isinstance(value, dict) and isinstance(value.get("signals"), list) and len(value["signals"]) > MAX_COMPARISON_RULES:
        raise TaskError("expression_rule_limit", "a comparison may propose at most three rules")
    approved = {key: item["content"] for key, item in snapshot["handles"].items()
                if item.get("quality") == "approved" and item.get("role") == "assistant"}
    comparison = {key: item["content"] for key, item in snapshot["handles"].items()
                  if item.get("quality") == "unreviewed" and item.get("role") == "assistant"}
    for signal in value.get("signals", []):
        references = signal.get("evidence_handles", [])
        if (not references or any(type(handle) is not str or handle not in snapshot["handles"] for handle in references)):
            raise TaskError("expression_approved_evidence_required", "rule references must name original sources")
        handles = [handle for handle in references if handle in approved]
        if not handles:
            raise TaskError("expression_approved_evidence_required", "rule evidence needs approved assistant sources")
        quotes = approved_quotations(signal.pop("approved_quotes", None), handles, approved)
        form = signal.get("expression", {})
        polarity = form.get("polarity", "positive")
        counterexamples = signal.pop("counterexamples", [])
        if not isinstance(counterexamples, list) or len(counterexamples) > 8:
            raise TaskError("expression_counterexample_invalid", "counterexamples must be a bounded list")
        if polarity == "negative":
            comparison_handles = [handle for handle in references if handle in comparison]
            counterexamples = counterexample_quotations(counterexamples, comparison_handles, comparison)
        elif counterexamples:
            raise TaskError("expression_counterexample_invalid", "positive rules do not carry negative examples")
        signal["evidence_handles"] = list(dict.fromkeys(item["evidence_handle"] for item in [*quotes, *counterexamples]))
        pattern, proposal = form.get("pattern", ""), signal.get("proposal", "")
        examples = [pattern, *quoted_examples(pattern), *quoted_examples(proposal)]
        markers = [marker for marker in PLATFORM_MARKERS if any(marker in text for text in examples)]
        if markers:
            raise ExpressionCandidateError("expression_platform_marker",
                "QQ record placeholders do not describe a speaking style", markers)
        if form.get("kind") == "style":
            basis = counterexamples if polarity == "negative" else quotes
            if not approved_pattern(form.get("pattern"), basis):
                code = "expression_counterexample_invalid" if polarity == "negative" else "expression_unapproved_pattern"
                raise ExpressionCandidateError(code,
                    "a concrete expression must occur in its " + ("counterexample" if polarity == "negative" else "approved quotation"),
                    (form.get("pattern", ""),))
        canonical = lambda text: "".join(unicodedata.normalize("NFKC", text).split())
        supported = [canonical(text) for text in approved.values()]
        supported.extend(canonical(item["quote"]) for item in counterexamples)
        originals = [item["content"] for item in snapshot["unreviewed_dialogue"] if item["role"] == "assistant"]
        unreviewed = [canonical(text) for text in originals]
        unsupported = [text for text in examples if len(canonical(text)) >= 2
                       and any(canonical(text) in source for source in unreviewed)
                       and not any(canonical(text) in source for source in supported)]
        advice = canonical(pattern + "\n" + proposal)
        unsupported.extend(text for text in originals if len(canonical(text)) >= 4
                           and canonical(text) in advice
                           and not any(canonical(text) in source for source in supported))
        if unsupported:
            raise ExpressionCandidateError("expression_unapproved_pattern",
                "an unreviewed-only expression cannot become a rule example", unsupported)
    result = learning.parse_output(encode(value), allowed_evidence=set(approved) | set(comparison),
                                   default_channels=("private", "proactive"))
    return result


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
            rejections.append({"index": index, "code": getattr(error, "code", "expression_candidate_invalid"),
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
