"""Offline syntax review for explicit future-data patterns; never imports input."""

import argparse
import ast
import hashlib
import json
from pathlib import Path

MAX_BYTES = 1024 * 1024
UNKNOWN = object()
LIMITATIONS = [
    "Two syntax heuristics, not a leakage-free certification or a backtest audit.",
    "Assumes pandas-like shift/rolling semantics and rows ordered from past to future; neither is verified.",
    "Future labels and retrospective smoothing may legitimately use future rows. A candidate is not proof of leakage.",
    "No type, alias, dataflow, split, full-sample preprocessing or execution-timing analysis.",
    "Dynamic arguments, frequency/index shifts and column shifts are not assessed by LL001.",
    "LL002 only assesses literal fixed row windows with literal boolean centering, default/right closure, "
    "and default/row axis; other rolling options, aliases and downstream delays are not assessed.",
]


def literal(node):
    """Read only a small literal form, without evaluation or arbitrary traversal."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.operand, ast.Constant):
        value = node.operand.value
        if type(value) is int:
            if isinstance(node.op, ast.USub):
                return -value
            if isinstance(node.op, ast.UAdd):
                return value
    return UNKNOWN


def inspect_rolling(call):
    """LL002: a deliberately small subset of pandas-like rolling call syntax."""
    location = {"line": call.lineno, "column_utf8_byte": call.col_offset + 1, "rule": "LL002"}

    def unassessed(reason):
        return None, {**location, "reason": reason}

    positional = ["window", "min_periods", "center"]
    names = [k.arg for k in call.keywords]
    if (None in names or len(set(names)) != len(names)
            or any(isinstance(a, ast.Starred) for a in call.args)
            or len(call.args) > len(positional)
            or any(k not in {*positional, "axis", "closed"} for k in names)
            or any(name in names for name in positional[:len(call.args)])):
        return unassessed("ambiguous_or_unsupported_rolling_arguments")
    arguments = dict(zip(positional, call.args))
    arguments.update({k.arg: k.value for k in call.keywords})
    values = {name: literal(node) for name, node in arguments.items()}
    window = values.get("window", UNKNOWN)
    center = values.get("center", False)
    axis = values.get("axis", 0)
    closed = values.get("closed", None)
    minimum = values.get("min_periods", None)
    if not (type(axis) is int and axis == 0 or type(axis) is str and axis == "index"):
        return unassessed("column_or_dynamic_rolling_axis")
    if not (closed is None or type(closed) is str and closed == "right"):
        return unassessed("nondefault_or_dynamic_rolling_closure")
    if type(window) is not int or window < 1:
        return unassessed("nonpositive_noninteger_or_dynamic_rolling_window")
    if type(center) is not bool:
        return unassessed("nonliteral_boolean_rolling_center")
    if not (minimum is None or type(minimum) is int and 0 <= minimum <= window):
        return unassessed("unsupported_rolling_min_periods")
    # Under default/right closure, widths 1 and 2 have no later row even when centered.
    if center and window >= 3:
        return {**location, "severity": "review", "window": window,
                "message": "Centered row window can include later observations at an earlier row.",
                "review": "If a rolling result enters a feature or decision, check when every row in the window "
                          "became available. Retrospective smoothing or a sufficient downstream delay may be "
                          "legitimate; neither dataflow nor delay is verified."}, None
    return None, None


def inspect_source(source):
    tree = ast.parse(source)
    findings, unassessed = [], []
    calls = sorted((n for n in ast.walk(tree)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "shift"), key=lambda n: (n.lineno, n.col_offset))
    for call in calls:
        # AST columns are UTF-8 byte offsets; expose that convention explicitly.
        location = {"line": call.lineno, "column_utf8_byte": call.col_offset + 1}
        names = [k.arg for k in call.keywords]
        if (None in names or len(set(names)) != len(names)
                or any(isinstance(a, ast.Starred) for a in call.args)
                or len(call.args) > 5
                or any(k not in {"periods", "freq", "axis", "fill_value", "suffix"} for k in names)
                or any(name in names for name in ["periods", "freq", "axis", "fill_value", "suffix"][:len(call.args)])):
            unassessed.append({**location, "reason": "ambiguous_or_unsupported_arguments"})
            continue
        keywords = {k.arg: k.value for k in call.keywords}

        def argument(name, index, default):
            node = keywords.get(name, call.args[index] if len(call.args) > index else None)
            return literal(node) if node is not None else default

        periods, frequency, axis = argument("periods", 0, 1), argument("freq", 1, None), argument("axis", 2, 0)
        if frequency is not None:
            reason = "frequency_or_dynamic_index_shift"
        elif not (type(axis) is int and axis == 0 or type(axis) is str and axis == "index" or axis is None):
            reason = "column_or_dynamic_axis_shift"
        elif type(periods) is not int:
            reason = "non_scalar_or_dynamic_periods"
        else:
            reason = None
        if reason:
            unassessed.append({**location, "reason": reason})
        elif periods < 0:
            findings.append({**location, "rule": "LL001", "severity": "review",
                             "periods": periods,
                             "message": "Negative row shift can move later observations into earlier rows.",
                             "review": "If this value enters a feature or decision, check when it became available. "
                                       "If it is a future target, keep it out of features and review label overlap at splits."})
    rolling_calls = [n for n in ast.walk(tree)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                     and n.func.attr == "rolling"]
    for call in rolling_calls:
        finding, unsupported = inspect_rolling(call)
        if finding:
            findings.append(finding)
        if unsupported:
            unassessed.append(unsupported)
    order = lambda item: (item["line"], item["column_utf8_byte"])
    return {"shift_calls": len(calls), "rolling_calls": len(rolling_calls),
            "findings": sorted(findings, key=order), "unassessed": sorted(unassessed, key=order)}


def scan_file(path):
    result = {"path": str(path)}
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return {**result, "error": "file_exceeds_1_MiB_limit"}
        result["sha256"] = hashlib.sha256(raw).hexdigest()
        result.update(inspect_source(raw.decode("utf-8-sig")))
    except SyntaxError as error:
        result["error"] = "invalid_python"
        result["line"] = error.lineno
    except UnicodeError:
        result["error"] = "input_must_be_utf8"
    except (RecursionError, MemoryError):
        result["error"] = "parser_resource_limit"
    except OSError:
        result["error"] = "file_not_readable"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline LL001/LL002 review candidates; no certification or source execution.")
    parser.add_argument("files", nargs="+", help="1-20 explicitly selected UTF-8 Python files, at most 1 MiB each")
    args = parser.parse_args(argv)
    if len(args.files) > 20:
        parser.error("Select at most 20 files; directory recursion is not supported.")
    files = [scan_file(path) for path in args.files]
    errors = sum("error" in f for f in files)
    candidates = sum(len(f.get("findings", [])) for f in files)
    unassessed = sum(len(f.get("unassessed", [])) for f in files)
    report = {"schema_version": 1, "certifies_leakage_free": False,
              "summary": {"review_candidates": candidates, "unassessed_calls": unassessed, "file_errors": errors},
              "limitations": LIMITATIONS, "files": files}
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return 2 if errors else 1 if candidates else 0


if __name__ == "__main__":
    raise SystemExit(main())
