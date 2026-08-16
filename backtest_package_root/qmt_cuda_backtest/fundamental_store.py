"""Point-in-time financial event cache and rule evaluation."""

from __future__ import annotations

import operator
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np


SCHEMA_VERSION = 1
_RULE_RE = re.compile(
    r"^\s*(?P<field>[A-Za-z0-9_]+\.[A-Za-z0-9_]+)\s*"
    r"(?P<op><=|>=|==|!=|<|>)\s*"
    r"(?P<value>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*$"
)
_OPS = {
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
    "==": operator.eq,
    "!=": operator.ne,
}


@dataclass(frozen=True)
class FundamentalRule:
    field: str
    op: str
    value: float

    def __str__(self) -> str:
        return f"{self.field}{self.op}{self.value:g}"


@dataclass(frozen=True)
class FundamentalData:
    codes: list[str]
    fields: list[str]
    event_code: np.ndarray
    event_field: np.ndarray
    report_date: np.ndarray
    announce_date: np.ndarray
    available_date: np.ndarray
    value: np.ndarray


def parse_rule(text: str) -> FundamentalRule:
    match = _RULE_RE.match(text)
    if not match:
        raise ValueError(
            "invalid fundamental rule; expected TABLE.field>=number, got " + repr(text)
        )
    return FundamentalRule(
        match.group("field").upper().split(".", 1)[0]
        + "."
        + match.group("field").split(".", 1)[1],
        match.group("op"),
        float(match.group("value")),
    )


def load_fundamental_cache(path: Path) -> FundamentalData:
    with np.load(path, allow_pickle=False) as data:
        required = {
            "schema_version", "codes", "fields", "event_code", "event_field",
            "report_date", "announce_date", "available_date", "value",
        }
        missing = sorted(required - set(data.files))
        if missing:
            raise ValueError(f"fundamental cache is missing fields: {missing}")
        version = int(np.asarray(data["schema_version"]).item())
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported fundamental cache schema {version}; expected {SCHEMA_VERSION}"
            )
        result = FundamentalData(
            data["codes"].tolist(),
            data["fields"].tolist(),
            data["event_code"].astype(np.int32, copy=True),
            data["event_field"].astype(np.int16, copy=True),
            data["report_date"].astype(np.int32, copy=True),
            data["announce_date"].astype(np.int32, copy=True),
            data["available_date"].astype(np.int32, copy=True),
            data["value"].astype(np.float64, copy=True),
        )
    lengths = {
        len(result.event_code), len(result.event_field), len(result.report_date),
        len(result.announce_date), len(result.available_date), len(result.value),
    }
    if len(lengths) != 1:
        raise ValueError("fundamental event arrays do not have equal lengths")
    if len(result.available_date) and np.any(result.available_date[1:] < result.available_date[:-1]):
        raise ValueError("fundamental events are not sorted by available_date")
    return result


def build_rule_mask(
    calendar: np.ndarray,
    target_codes: list[str],
    data: FundamentalData,
    rules: list[FundamentalRule],
    *,
    missing: str = "exclude",
) -> tuple[np.ndarray, dict]:
    """Build a daily eligibility mask without exposing future announcements.

    Events are applied once ``available_date <= calendar day``.  A revision of
    an older report never replaces a newer report already known on that day;
    a revision of the same report period does replace its older value.
    """
    if missing not in {"exclude", "allow"}:
        raise ValueError("missing must be 'exclude' or 'allow'")
    field_lookup = {name: i for i, name in enumerate(data.fields)}
    unknown = sorted({rule.field for rule in rules} - set(field_lookup))
    if unknown:
        raise KeyError(f"rules reference fields absent from fundamental cache: {unknown}")

    target_lookup = {code: i for i, code in enumerate(target_codes)}
    source_to_target = np.array(
        [target_lookup.get(code, -1) for code in data.codes], dtype=np.int32
    )
    mapped_code = source_to_target[data.event_code]
    keep = mapped_code >= 0
    event_indices = np.flatnonzero(keep)

    n_codes, n_fields = len(target_codes), len(data.fields)
    current = np.full((n_codes, n_fields), np.nan, dtype=np.float64)
    current_report = np.zeros((n_codes, n_fields), dtype=np.int32)
    current_announce = np.zeros((n_codes, n_fields), dtype=np.int32)
    masks = np.ones((len(calendar), n_codes), dtype=bool)
    pointer = 0

    for day_i, raw_day in enumerate(calendar):
        day = int(raw_day)
        while pointer < len(event_indices):
            event_i = int(event_indices[pointer])
            if int(data.available_date[event_i]) > day:
                break
            code_i = int(mapped_code[event_i])
            field_i = int(data.event_field[event_i])
            report = int(data.report_date[event_i])
            announce = int(data.announce_date[event_i])
            if (report > current_report[code_i, field_i] or
                    (report == current_report[code_i, field_i] and
                     announce >= current_announce[code_i, field_i])):
                current[code_i, field_i] = data.value[event_i]
                current_report[code_i, field_i] = report
                current_announce[code_i, field_i] = announce
            pointer += 1

        eligible = np.ones(n_codes, dtype=bool)
        for rule in rules:
            values = current[:, field_lookup[rule.field]]
            finite = np.isfinite(values)
            passed = _OPS[rule.op](values, rule.value)
            eligible &= np.where(finite, passed, missing == "allow")
        masks[day_i] = eligible

    field_coverage = {}
    for rule in rules:
        j = field_lookup[rule.field]
        field_coverage[rule.field] = int(np.sum(np.isfinite(current[:, j])))
    eligible_counts = np.sum(masks, axis=1)
    stats = {
        "rules": [str(rule) for rule in rules],
        "missing": missing,
        "cache_codes": len(data.codes),
        "target_codes": len(target_codes),
        "mapped_codes": int(np.sum(source_to_target >= 0)),
        "event_count": int(len(event_indices)),
        "final_field_coverage": field_coverage,
        "eligible_count_start": int(eligible_counts[0]) if len(eligible_counts) else 0,
        "eligible_count_end": int(eligible_counts[-1]) if len(eligible_counts) else 0,
        "eligible_count_min": int(np.min(eligible_counts)) if len(eligible_counts) else 0,
        "eligible_count_max": int(np.max(eligible_counts)) if len(eligible_counts) else 0,
        "eligible_count_median": (float(np.median(eligible_counts))
                                  if len(eligible_counts) else 0.0),
    }
    return masks, stats
