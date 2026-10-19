"""Declared missing/stale market-data policy for timestamped BBO snapshots (stdlib only).

Policy keys (all required, no others):
  max_gap_seconds            gap when consecutive timestamps differ by MORE than this
  max_staleness_seconds      stale when an identical top of book persists MORE than this
  on_gap                     'reject' or 'mark' rows hit by timing findings
  forward_fill_limit_seconds longest span the last observed quote may be carried across a gap

Forward fill carries the last observed quote across a gap as the assumed book
state; no synthetic rows are inserted. Coverage stops at the limit, so a row
after a longer gap is affected (rejected or marked). Stale quotes are affected the
same way. Crossed, locked or empty books are always rejected: they cannot be
traded against or used as features. Evaluation must stop on any rejected row.
Comparisons use exact rational arithmetic on the supplied int/float seconds.
"""

from __future__ import annotations

import csv
from fractions import Fraction
import hashlib
import io
import json
import math
from pathlib import Path
from typing import Sequence

from research.lab import SCHEMA, Snapshot

TIMESTAMP_COLUMN = "timestamp"
POLICY_KEYS = frozenset(("max_gap_seconds", "max_staleness_seconds", "on_gap", "forward_fill_limit_seconds"))
ACTIONS = ("reject", "mark")
TIMING_KINDS = frozenset(("gap_beyond_fill_limit", "stale"))
BOOK_KINDS = frozenset(("crossed", "empty"))


class DataQualityError(ValueError):
    """Raised when evaluation receives rows that the declared policy rejected."""


def _seconds(value, name: str, *, positive: bool) -> int | float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError(f"{name} must be a finite {'positive' if positive else 'nonnegative'} int or float")
    return value


def validate_policy(policy) -> dict:
    if type(policy) is not dict or set(policy) != POLICY_KEYS:
        raise ValueError("policy must have exactly " + ", ".join(sorted(POLICY_KEYS)))
    if policy["on_gap"] not in ACTIONS:
        raise ValueError("on_gap must be 'reject' or 'mark'")
    return {"max_gap_seconds": _seconds(policy["max_gap_seconds"], "max_gap_seconds", positive=True),
            "max_staleness_seconds": _seconds(policy["max_staleness_seconds"], "max_staleness_seconds", positive=True),
            "on_gap": policy["on_gap"],
            "forward_fill_limit_seconds": _seconds(policy["forward_fill_limit_seconds"],
                                                   "forward_fill_limit_seconds", positive=False)}


def _plain(value: Fraction) -> int | float:
    return value.numerator if value.denominator == 1 else float(value)


def _check_inputs(snapshots, timestamps) -> tuple[tuple[Snapshot, ...], tuple]:
    rows = tuple(snapshots)
    if not all(isinstance(row, Snapshot) for row in rows):
        raise ValueError("snapshots must be Snapshot instances")
    stamps = tuple(timestamps)
    if len(stamps) != len(rows):
        raise ValueError("timestamps must match snapshots length")
    previous = None
    for index, stamp in enumerate(stamps):
        if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp < 0:
            raise ValueError(f"timestamps[{index}] must be a finite nonnegative int or float (seconds)")
        if previous is not None and stamp <= previous:
            raise ValueError(f"timestamps[{index}] must strictly increase")
        previous = stamp
    return rows, stamps


def _quote(row: Snapshot) -> tuple[int, int, int, int]:
    return row.bid_price, row.ask_price, row.bid_qty, row.ask_qty


def check_series(snapshots: Sequence[Snapshot], policy: dict, *, timestamps: Sequence) -> list[dict]:
    """Return gap, stale, crossed and empty findings ordered by row; [] when clean."""
    policy = validate_policy(policy)
    rows, stamps = _check_inputs(snapshots, timestamps)
    max_gap = Fraction(policy["max_gap_seconds"])
    max_stale = Fraction(policy["max_staleness_seconds"])
    findings: list[dict] = []
    run_start = 0
    for index, row in enumerate(rows):
        base = {"index": index, "seq": row.seq, "timestamp": stamps[index]}
        if index:
            gap = Fraction(stamps[index]) - Fraction(stamps[index - 1])
            if gap > max_gap:
                findings.append({"kind": "gap", **base, "previous_timestamp": stamps[index - 1],
                                 "gap_seconds": _plain(gap)})
            if _quote(row) != _quote(rows[index - 1]):
                run_start = index
            age = Fraction(stamps[index]) - Fraction(stamps[run_start])
            if age > max_stale:
                findings.append({"kind": "stale", **base, "unchanged_since": stamps[run_start],
                                 "stale_seconds": _plain(age)})
        if min(row.bid_price, row.ask_price, row.bid_qty, row.ask_qty) <= 0:
            findings.append({"kind": "empty", **base, "detail": "nonpositive price or displayed quantity"})
        if row.bid_price >= row.ask_price:
            findings.append({"kind": "crossed", **base,
                             "detail": "locked: bid == ask" if row.bid_price == row.ask_price else "crossed: bid > ask"})
    return findings


def apply_policy(snapshots: Sequence[Snapshot], policy: dict, *, timestamps: Sequence) -> dict:
    """Apply the declared policy without inventing data.

    Gaps no longer than forward_fill_limit_seconds are bridged by carrying the
    previous quote; the bridge never extends past the limit. Rows after a longer
    gap and stale rows follow on_gap; crossed/empty rows are always rejected.
    'rows' holds only accepted rows (marked rows included), in original order.
    """
    policy = validate_policy(policy)
    rows, stamps = _check_inputs(snapshots, timestamps)
    findings = check_series(rows, policy, timestamps=stamps)
    limit = Fraction(policy["forward_fill_limit_seconds"])
    reasons: dict[int, list[str]] = {}
    forward_fills = []
    for finding in findings:
        index = finding["index"]
        if finding["kind"] == "gap":
            gap = Fraction(stamps[index]) - Fraction(stamps[index - 1])
            filled = min(gap, limit)
            forward_fills.append({"index": index, "seq": finding["seq"], "timestamp": stamps[index],
                                  "from_timestamp": stamps[index - 1],
                                  "filled_until": _plain(Fraction(stamps[index - 1]) + filled),
                                  "filled_seconds": _plain(filled),
                                  "uncovered_seconds": _plain(gap - filled)})
            if gap > limit:
                reasons.setdefault(index, []).append("gap_beyond_fill_limit")
        else:
            reasons.setdefault(index, []).append(finding["kind"])
    rejected, marked, kept, kept_stamps, flags = [], [], [], [], []
    for index, row in enumerate(rows):
        why = reasons.get(index, [])
        entry = {"index": index, "seq": row.seq, "timestamp": stamps[index], "reasons": why}
        if BOOK_KINDS.intersection(why) or (why and policy["on_gap"] == "reject"):
            rejected.append(entry)
            continue
        if why:
            marked.append(entry)
        kept.append(row)
        kept_stamps.append(stamps[index])
        flags.append(tuple(why))
    return {"policy": policy, "findings": findings, "forward_fills": forward_fills,
            "rejected": rejected, "marked": marked, "rows": tuple(kept),
            "timestamps": tuple(kept_stamps), "flags": tuple(flags)}


def require_evaluable(outcome: dict) -> dict:
    """Stop explicitly rather than evaluate a series with rejected rows."""
    if outcome["rejected"]:
        shown = "; ".join(f"index {item['index']} t={item['timestamp']}: {','.join(item['reasons'])}"
                          for item in outcome["rejected"][:10])
        more = len(outcome["rejected"]) - 10
        raise DataQualityError(f"data-quality policy rejected {len(outcome['rejected'])} row(s); "
                               f"evaluation stopped: {shown}" + (f"; and {more} more" if more > 0 else ""))
    return outcome


def summarize(outcome: dict) -> dict:
    """JSON-ready audit record; omits the row payload itself."""
    counts: dict[str, int] = {}
    for finding in outcome["findings"]:
        counts[finding["kind"]] = counts.get(finding["kind"], 0) + 1
    return {"policy": dict(outcome["policy"]), "finding_counts": dict(sorted(counts.items())),
            "findings": [dict(item) for item in outcome["findings"]],
            "forward_fills": [dict(item) for item in outcome["forward_fills"]],
            "marked": [dict(item, reasons=list(item["reasons"])) for item in outcome["marked"]],
            "rejected": [dict(item, reasons=list(item["reasons"])) for item in outcome["rejected"]],
            "limitations": ["Forward fill is an assumed carried quote; no synthetic rows are inserted.",
                            "Marked rows are evaluated unchanged; flags disclose them, they do not repair them.",
                            "Timestamps are caller-supplied seconds; clock accuracy is not verified."]}


def _parse_timestamp(text: str | None, line: int) -> int | float:
    if text is None:
        raise ValueError(f"line {line}: missing {TIMESTAMP_COLUMN} field")
    try:
        value = int(text)
    except ValueError:
        try:
            value = float(text)
        except ValueError:
            raise ValueError(f"line {line}: {TIMESTAMP_COLUMN} must be a number of seconds") from None
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"line {line}: {TIMESTAMP_COLUMN} must be finite and nonnegative")
    return value


def read_timed_snapshot_source(path: Path) -> tuple[list[Snapshot], list, str]:
    """Parse BBO rows plus a timestamp column (seconds) from the same hashed bytes.

    Book sanity (crossed/empty) is left to check_series so it is reported with
    timestamps instead of aborting the parse; integer fields and seq order are strict.
    """
    payload = Path(path).read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig"), newline=""))
    required = (*SCHEMA, TIMESTAMP_COLUMN)
    if (reader.fieldnames is None or any(name not in reader.fieldnames for name in required)
            or len(set(reader.fieldnames)) != len(reader.fieldnames)):
        raise ValueError("timed BBO CSV requires columns: " + ",".join(required))
    rows, stamps = [], []
    for record in reader:
        if None in record:
            raise ValueError(f"line {reader.line_num}: too many CSV fields")
        try:
            row = Snapshot(*(int(record[name]) for name in SCHEMA))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"line {reader.line_num}: all BBO fields must be integers") from exc
        if not 0 <= row.seq < 2**64 or (rows and row.seq <= rows[-1].seq):
            raise ValueError(f"line {reader.line_num}: seq must strictly increase")
        if max(abs(row.bid_price), abs(row.ask_price), abs(row.bid_qty), abs(row.ask_qty)) >= 2**63:
            raise ValueError(f"line {reader.line_num}: BBO fields must fit int64")
        rows.append(row)
        stamps.append(_parse_timestamp(record[TIMESTAMP_COLUMN], reader.line_num))
    if not rows:
        raise ValueError("BBO CSV contains no observations")
    return rows, stamps, digest


def read_policy_source(path: Path) -> tuple[dict, str]:
    """Validate a policy and hash the exact bytes parsed in the same read."""
    def reject_constant(name):
        raise ValueError(f"non-finite constant {name} not allowed in policy")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate policy key {key}")
            result[key] = value
        return result

    payload = Path(path).read_bytes()
    policy = validate_policy(json.loads(payload.decode("utf-8-sig"), parse_constant=reject_constant,
                                        object_pairs_hook=unique))
    return policy, hashlib.sha256(payload).hexdigest()


def load_policy(path: Path) -> dict:
    return read_policy_source(path)[0]
