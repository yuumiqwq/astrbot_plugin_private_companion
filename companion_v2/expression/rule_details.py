"""Concrete expression forms, kept beside the existing review and evidence ledger."""

import json

from companion_v2.execution.models import digest, encode, stamp


PREFIX = "expression.rule-form:"
LIMITS = {"situation": 240, "pattern": 500, "avoid": 400}


def normalize(raw):
    if (type(raw) is not dict or not {"kind", *LIMITS} <= set(raw)
            or set(raw) - {"kind", "polarity", *LIMITS}
            or raw["kind"] not in ("style", "grammar")
            or raw.get("polarity", "positive") not in ("positive", "negative")):
        raise ValueError("invalid_expression_form")
    result = {"kind": raw["kind"], "polarity": raw.get("polarity", "positive")}
    for name, maximum in LIMITS.items():
        value = raw[name]
        if type(value) is not str or len(value) > maximum or (name != "avoid" and not value.strip()):
            raise ValueError("invalid_expression_form")
        result[name] = value.strip()
    return result


def read(runtime, signal_id):
    row = runtime.read_one("SELECT value FROM runtime_metadata WHERE key = ?", (PREFIX + digest(signal_id),))
    if row is None:
        return None
    record = json.loads(row["value"])
    record["form"] = normalize(record["form"])
    return record


def write(runtime, signal_id, form, *, at, actor, operation_key, legacy=None):
    form = normalize(form)
    previous = read(runtime, signal_id)
    value = {"signal_id": signal_id, "form": form, "revision": (previous or {}).get("revision", 0) + 1,
        "updated_at": stamp(at), "updated_by": actor, "operation_key": operation_key,
        "legacy": (previous or {}).get("legacy", legacy or {})}
    with runtime.transaction() as connection:
        connection.execute("INSERT INTO runtime_metadata(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (PREFIX + digest(signal_id), encode(value)))
    return value


def form_for(runtime, row):
    record = read(runtime, row["signal_id"])
    return record["form"] if record else None


def legacy_counts(record):
    result = {}
    for name in ("use_count", "positive_feedback", "negative_feedback"):
        value = (record or {}).get("legacy", {}).get(name, 0)
        result[name] = max(0, value) if type(value) is int else 0
    return result


FORM_SCHEMA = {"type": "object", "properties": {
    "kind": {"type": "string", "enum": ["style", "grammar"]},
    "polarity": {"type": "string", "enum": ["positive", "negative"]},
    **{name: {"type": "string", "maxLength": maximum} for name, maximum in LIMITS.items()},
}, "required": ["kind", "polarity", *LIMITS], "additionalProperties": False}
