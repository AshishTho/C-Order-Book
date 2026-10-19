"""Side-by-side comparison of walk-forward reports with explicit comparability checks.

Every run is checked against one baseline (the first run unless named) for the
same input data, cost model and cost parameters, metric definitions and units,
and split scheme. Each mismatch is a named reason. Incompatible runs and runs
that are worse than the baseline stay in the table, visibly labelled; nothing is
filtered out, ranked or declared a winner. Differences are descriptive only: no
significance test is computed.

Input identity checks each report's source SHA-256 as well as the declared
SHA-256 set of its provenance manifest. The manifest body hash is checked,
but inputs are not re-hashed here; use `python -m research.provenance verify`
for that. Declared row count, window, data-quality policy and fold anchors are
checked even when manifests match. Without a usable manifest or report source
checksum, an otherwise matching sample is flagged unverified-sample.

Usage (exit 0 for any completed comparison, 2 for unreadable input):
  python -m research.compare base.json other.json [--baseline NAME]
      [--format text|json|markdown] [--manifest NAME=PATH]
A sibling REPORT.manifest.json (from walk_forward --write-manifest) is used
automatically; run names are report file stems.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from research import provenance

SCHEMA_VERSION = 1
KIND = "research_run_comparison"
MAX_REPORT_BYTES = 256 * 1024 * 1024
REASONS = ("different-sample", "different-costs", "different-metrics", "different-splits", "unverified-sample")
COST_MODEL = ("research.lab.simulate: delayed entries and horizon exits cross the displayed top quote, "
              "depth-capped; fee and slippage in ticks per lot per side")
COST_KEYS = ("fee_ticks", "slippage_ticks")
HIGHER_IS_BETTER = frozenset(("net_pnl_ticks_lots", "realized_round_trip_pnl_ticks_lots", "win_rate",
                              "mean_trade_pnl_ticks_lots", "profit_factor", "terminal_liquidation_mark_ticks_lots",
                              "mean_signed_markout_ticks", "nonzero_markout_direction_accuracy"))
LOWER_IS_BETTER = frozenset(("max_drawdown_ticks_lots", "fees_ticks_lots", "slippage_ticks_lots"))
TRADE_BASED = frozenset(("realized_round_trip_pnl_ticks_lots", "win_rate", "mean_trade_pnl_ticks_lots",
                         "profit_factor"))
ACTIVE_BASED = frozenset(("mean_signed_markout_ticks", "nonzero_markout_direction_accuracy"))
NOTES = [
    "No ranking or winner is declared. Incompatible runs keep their values but get no deltas or labels.",
    "Runs worse than the baseline are kept and labelled 'worse'; nothing is filtered out.",
    "Deltas are run minus baseline per fold, strategy and metric; they are descriptive, not significance-tested.",
    "n is the per-fold sample named by sample_basis; overlapping labels and shared history make it "
    "not independent evidence.",
    "Folds are independent experiments; no PnL is summed or chained across folds.",
    "Input identity compares declared manifest and report source checksums; no input files are re-hashed here.",
]


def _number(value) -> bool:
    return type(value) in (int, float)


def _collect(metrics: dict, prefix: str, sections: dict, observations: int) -> None:
    for section, values in sections.items():
        for field, value in values.items():
            if field == "open_trade" or not (value is None or _number(value)):
                continue
            if section == "execution":
                basis, n = (("round_trips", values["completed_round_trips"]) if field in TRADE_BASED
                            else ("test_observations", observations))
            else:
                basis, n = (("active_decisions", values["active_decisions"]) if field in ACTIVE_BASED
                            else ("decision_observations", values["decision_observations"]))
            metrics[f"{prefix}.{section}.{field}"] = {"value": value, "n": n, "basis": basis}


def _metric_definitions(name: str, units: dict, metrics: dict) -> dict:
    fields = sorted({key.split(".", 2)[2] for key in metrics})
    return {"format": name, "units": units, "fields": fields}


def _describe_walk_forward(report: dict) -> dict:
    config, folds = report["config"], report["folds"]
    source = report.get("source") or {}
    metrics: dict = {}
    for fold in folds:
        split = fold["split"]
        for name, item in fold["strategies"].items():
            _collect(metrics, f"fold{fold['fold']}.{name}",
                     {"execution": item["execution"], "test_predictive": item["test_predictive"],
                      "train_predictive": item["train_predictive"]},
                     split["test_stop_exclusive"] - split["test_start"])
    quality = report.get("data_quality")
    return {"format": "walk_forward",
            "sample": {"row_count": report["total_observations"],
                       "window": {"start": 0, "stop_exclusive": report["total_observations"]},
                       "source_sha256": source.get("sha256"), "data_kind": source.get("data_kind"),
                       "fold_test_start_seqs": [fold["split"]["test_start_seq"] for fold in folds],
                       "data_quality_policy": quality["policy"] if quality else None},
            "costs": {"model": COST_MODEL, **{key: config[key] for key in COST_KEYS}},
            "metric_definitions": _metric_definitions("research.walk_forward schema 1", folds[0]["units"], metrics),
            "split": {"protocol": report["protocol"], "initial_train": report["initial_train"],
                      "test_size": report["test_size"]},
            "parameters": {key: value for key, value in config.items() if key not in COST_KEYS},
            "metrics": metrics}


def _describe_stress(report: dict) -> dict:
    configuration, folds = report["configuration"], report["folds"]
    config = configuration["config"]
    metrics: dict = {}
    for fold in folds:
        for name, item in fold["signals"].items():
            _collect(metrics, f"fold{fold['fold_index']}.{name}",
                     {"execution": item["base_simulation"], "test_predictive": item["predictive"]},
                     fold["test_stop"] - fold["test_start"])
    rows = configuration["row_count"]
    return {"format": "stress_walk_forward",
            "sample": {"row_count": rows, "window": {"start": 0, "stop_exclusive": rows},
                       "source_sha256": report.get("input_sha256"), "data_kind": None,
                       "fold_test_start_seqs": None, "data_quality_policy": None},
            "costs": {"model": COST_MODEL, **{key: config[key] for key in COST_KEYS}},
            "metric_definitions": _metric_definitions(f"research.stress_walk_forward {report['schema_version']}",
                                                      report["units"], metrics),
            "split": {"protocol": "fixed_folds_with_reserved_suffix", "folds": report["boundaries"]["folds"],
                      "final_holdout_start": report["boundaries"]["final_holdout"]["start"]},
            "parameters": {**{key: value for key, value in config.items()
                              if key not in COST_KEYS and key != "train_fraction"},
                           "stress_grids": configuration["stress_grids"]},
            "metrics": metrics}


def describe_report(report) -> dict:
    """Normalize one walk-forward report; raise ValueError if it is not one."""
    if type(report) is not dict:
        raise ValueError("report must be a JSON object")
    try:
        if report.get("schema_version") == 1 and report.get("protocol") == "expanding_history_independent_folds":
            described = _describe_walk_forward(report)
        elif report.get("schema_version") == "walk_forward.v1":
            described = _describe_stress(report)
        else:
            raise ValueError("not a recognised walk-forward report (research.walk_forward or stress_walk_forward)")
    except (KeyError, TypeError, IndexError, AttributeError) as exc:
        raise ValueError(f"malformed walk-forward report: {type(exc).__name__}: {exc}") from None
    if not described["metrics"]:
        raise ValueError("walk-forward report contains no folds or metrics")
    return described


def _provenance(value, source_sha256) -> tuple[dict, list[str]]:
    if value is None:
        return {"status": "absent", "input_sha256": None}, ["no-provenance"]
    manifest = value if type(value) is dict else provenance.load_json(value)
    provenance.validate_manifest(manifest)
    declared = sorted({item["sha256"] for item in manifest["inputs"]})
    record = {"status": "declared", "input_sha256": declared, "manifest_sha256": manifest["manifest_sha256"]}
    if provenance.manifest_digest(manifest) != manifest["manifest_sha256"]:
        return {**record, "status": "manifest-hash-mismatch", "input_sha256": None}, ["provenance-invalid"]
    if source_sha256 is None:
        return record, ["provenance-not-cross-checked-with-report"]
    if source_sha256 not in declared:
        return ({**record, "status": "does-not-cover-report-source", "input_sha256": None},
                ["provenance-does-not-cover-report-source"])
    return record, []


def _run(item, index: int) -> dict:
    if type(item) is dict and "report" in item:
        unknown = set(item) - {"report", "name", "provenance"}
        if unknown:
            raise ValueError(f"run {index}: unknown keys {sorted(unknown)}")
        report, name, manifest = item["report"], item.get("name"), item.get("provenance")
    else:
        report, name, manifest = item, None, None
    if type(report) is not dict:
        report = load_report(report)
    name = f"run{index + 1}" if name is None else name
    if type(name) is not str or not name:
        raise ValueError(f"run {index}: name must be a non-empty string")
    run = describe_report(report)
    run["provenance"], run["flags"] = _provenance(manifest, run["sample"]["source_sha256"])
    return {"name": name, **run}


def _differences(run: dict, base: dict) -> dict:
    """Named reasons (in REASONS order) with the baseline and run values behind each."""
    found = {}
    a, b = run["provenance"]["input_sha256"], base["provenance"]["input_sha256"]
    mine, theirs = run["sample"], base["sample"]
    keys = ["row_count", "window", "data_quality_policy"]
    sources_known = mine["source_sha256"] is not None and theirs["source_sha256"] is not None
    if sources_known:
        keys.append("source_sha256")  # a shared manifest may cover distinct report inputs
    if (mine["fold_test_start_seqs"] is not None and theirs["fold_test_start_seqs"] is not None
            and run["split"] == base["split"]):
        keys.append("fold_test_start_seqs")
    changed = [key for key in keys if mine[key] != theirs[key]]
    if a is not None and b is not None:
        if a != b or changed:
            found["different-sample"] = {"baseline": {"input_sha256": b, "row_count": theirs["row_count"],
                                                      **{key: theirs[key] for key in changed}},
                                         "run": {"input_sha256": a, "row_count": mine["row_count"],
                                                 **{key: mine[key] for key in changed}}}
    elif changed:
        found["different-sample"] = {"baseline": {key: theirs[key] for key in changed},
                                     "run": {key: mine[key] for key in changed}}
    for reason, key in (("different-costs", "costs"), ("different-metrics", "metric_definitions"),
                        ("different-splits", "split")):
        if run[key] != base[key]:
            found[reason] = {"baseline": base[key], "run": run[key]}
    if "different-sample" not in found and (a is None or b is None or not sources_known):
        found["unverified-sample"] = {"baseline": base["provenance"]["status"], "run": run["provenance"]["status"],
                                      "checked": "declared row count, window, data-quality policy and any "
                                                 "declared source checksum or fold anchors"}
    return found


def _status(reasons) -> str:
    if any(reason.startswith("different-") for reason in reasons):
        return "incompatible"
    return "unverified" if reasons else "compatible"


def _cell(run: dict, base: dict, key: str, direction) -> dict:
    cell, reference = run["metrics"].get(key), base["metrics"].get(key)
    out = {"value": None if cell is None else cell["value"], "n": None if cell is None else cell["n"],
           "delta": None}
    if cell is None:
        out["label"] = "missing"
    elif run is base:
        out["label"] = "baseline"
    elif run["status"] == "incompatible":
        out["label"] = "not-comparable"
    elif reference is None or cell["value"] is None or reference["value"] is None:
        out["label"] = "not-available"
    else:
        out["delta"] = delta = cell["value"] - reference["value"]
        if delta == 0:
            out["label"] = "equal"
        elif direction is None:
            out["label"] = "differs"
        else:
            out["label"] = "better" if (delta > 0) == (direction == "higher") else "worse"
    return out


def compare_runs(reports, baseline: str | None = None) -> dict:
    """Compare walk-forward reports side by side against one baseline run.

    Each item is a report dict, a report path, or {"report": dict|path,
    "name": str, "provenance": manifest dict|path}. Nothing is dropped or ranked.
    """
    runs = [_run(item, index) for index, item in enumerate(reports)]
    if len(runs) < 2:
        raise ValueError("need at least two runs to compare")
    names = [run["name"] for run in runs]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate run names: {names}")
    if baseline is None:
        base = runs[0]
    elif baseline in names:
        base = runs[names.index(baseline)]
    else:
        raise ValueError(f"unknown baseline {baseline!r}; runs are {names}")
    for run in runs:
        run["details"] = {} if run is base else _differences(run, base)
        run["reasons"] = list(run["details"])
        run["status"] = "baseline" if run is base else _status(run["reasons"])
        run["parameter_changes"] = {key: {"baseline": base["parameters"].get(key), "run": run["parameters"].get(key)}
                                    for key in sorted(set(run["parameters"]) | set(base["parameters"]))
                                    if run["parameters"].get(key) != base["parameters"].get(key)}
    order = list(dict.fromkeys(key for run in [base, *runs] for key in run["metrics"]))
    rows = []
    for key in order:
        field = key.rsplit(".", 1)[1]
        direction = "higher" if field in HIGHER_IS_BETTER else "lower" if field in LOWER_IS_BETTER else None
        basis = next(run["metrics"][key]["basis"] for run in [base, *runs] if key in run["metrics"])
        rows.append({"metric": key, "direction": direction, "sample_basis": basis,
                     "cells": {run["name"]: _cell(run, base, key, direction) for run in runs}})
    for run in runs:
        counts = {}
        for row in rows:
            label = row["cells"][run["name"]]["label"]
            counts[label] = counts.get(label, 0) + 1
        run["label_counts"] = dict(sorted(counts.items()))
        if counts.get("worse"):
            run["flags"].append("worse-than-baseline")
        if run["status"] == "incompatible":
            run["flags"].append("not-comparable")
    public = ("name", "format", "status", "reasons", "flags", "details", "provenance", "sample", "costs",
              "metric_definitions", "split", "parameters", "parameter_changes", "label_counts")
    pairwise = [{"runs": [first["name"], second["name"]], "reasons": list(_differences(second, first))}
                for i, first in enumerate(runs) for second in runs[i + 1:]]
    return {"schema_version": SCHEMA_VERSION, "kind": KIND, "baseline": base["name"],
            "runs": [{key: run[key] for key in public} for run in runs],
            "pairwise": pairwise, "metrics": rows, "notes": list(NOTES)}


def _fmt(value) -> str:
    if value is None:
        return "-"
    if type(value) is float:
        return f"{value:.6g}"
    return str(value)


def _cell_text(cell: dict) -> str:
    text = f"{_fmt(cell['value'])} n={_fmt(cell['n'])}"
    if cell["delta"] is not None:
        sign = "+" if cell["delta"] > 0 else ""
        return f"{text} d={sign}{_fmt(cell['delta'])} {cell['label']}"
    return text if cell["label"] == "baseline" else f"{text} {cell['label']}"


def _run_lines(result: dict) -> list[tuple[str, str]]:
    lines = []
    for run in result["runs"]:
        reasons = ", ".join(run["reasons"]) or "none"
        flags = ", ".join(run["flags"]) or "none"
        changes = "; ".join(f"{key}: {_fmt(item['baseline'])} -> {_fmt(item['run'])}"
                            for key, item in run["parameter_changes"].items()) or "none"
        counts = ", ".join(f"{label}={count}" for label, count in run["label_counts"].items())
        lines.append((run["name"], f"status={run['status']}; reasons: {reasons}; flags: {flags}; "
                                   f"provenance={run['provenance']['status']}; parameter changes: {changes}; "
                                   f"metric labels: {counts}"))
    return lines


def _table(result: dict) -> tuple[list[str], list[list[str]]]:
    names = [run["name"] for run in result["runs"]]
    header = ["metric", "better", "n basis",
              *(f"{name} (baseline)" if name == result["baseline"] else name for name in names)]
    body = [[row["metric"], row["direction"] or "-", row["sample_basis"],
             *(_cell_text(row["cells"][name]) for name in names)] for row in result["metrics"]]
    return header, body


def render_text(result: dict) -> str:
    lines = [f"Research run comparison; baseline: {result['baseline']}", "", "Runs:"]
    lines += [f"  {name}: {text}" for name, text in _run_lines(result)]
    lines += ["", "Pairwise comparability:"]
    lines += [f"  {a} vs {b}: {', '.join(item['reasons']) or 'compatible'}"
              for item in result["pairwise"] for a, b in [item["runs"]]]
    header, body = _table(result)
    widths = [max(len(row[i]) for row in [header, *body]) for i in range(len(header))]
    lines += ["", "Metrics (value n=sample d=delta-vs-baseline label):"]
    for row in [header, ["-" * width for width in widths], *body]:
        lines.append("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())
    lines += ["", "Notes:", *(f"  - {note}" for note in result["notes"])]
    return "\n".join(lines) + "\n"


def _md(text: str) -> str:
    return text.replace("\\", "\\\\").replace("|", "\\|")


def render_markdown(result: dict) -> str:
    lines = ["# Research run comparison", "", f"Baseline: `{_md(result['baseline'])}`", "", "## Runs", "",
             "| run | status | reasons | flags | provenance | parameter changes |",
             "| --- | --- | --- | --- | --- | --- |"]
    for run in result["runs"]:
        changes = "; ".join(f"{key}: {_fmt(item['baseline'])} -> {_fmt(item['run'])}"
                            for key, item in run["parameter_changes"].items()) or "none"
        lines.append(f"| {_md(run['name'])} | {run['status']} | {', '.join(run['reasons']) or 'none'} | "
                     f"{', '.join(run['flags']) or 'none'} | {run['provenance']['status']} | {_md(changes)} |")
    lines += ["", "## Pairwise comparability", ""]
    lines += [f"- {_md(a)} vs {_md(b)}: {', '.join(item['reasons']) or 'compatible'}"
              for item in result["pairwise"] for a, b in [item["runs"]]]
    header, body = _table(result)
    lines += ["", "## Metrics", "", "Cells: value, n=sample, d=delta vs baseline, label.", "",
              "| " + " | ".join(_md(cell) for cell in header) + " |", "|" + " --- |" * len(header)]
    lines += ["| " + " | ".join(_md(cell) for cell in row) + " |" for row in body]
    lines += ["", "## Notes", "", *(f"- {note}" for note in result["notes"])]
    return "\n".join(lines) + "\n"


def render(result: dict, fmt: str = "text") -> str:
    if fmt == "json":
        return json.dumps(result, indent=2, allow_nan=False) + "\n"
    if fmt == "markdown":
        return render_markdown(result)
    if fmt == "text":
        return render_text(result)
    raise ValueError(f"unknown format {fmt!r}")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(name):
    raise ValueError(f"non-finite JSON constant {name}")


def load_report(path) -> dict:
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_REPORT_BYTES + 1)
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError(f"{path}: report exceeds size limit")
    try:
        report = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                            parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"{path}: invalid UTF-8 JSON: {exc}") from None
    if type(report) is not dict:
        raise ValueError(f"{path}: report must be a JSON object")
    return report


def _cli_runs(paths: list[Path], manifests: list[str]) -> list[dict]:
    stems = [path.stem for path in paths]
    names = stems if len(set(stems)) == len(stems) else [str(path) for path in paths]
    chosen = {}
    for spec in manifests:
        name, sep, target = spec.partition("=")
        if not sep or name not in names or not target:
            raise ValueError(f"--manifest {spec!r}: expected NAME=PATH with NAME one of {names}")
        chosen[name] = Path(target)
    runs = []
    for name, path in zip(names, paths):
        sidecar = path.with_name(path.name + ".manifest.json")
        manifest = chosen.get(name, sidecar if sidecar.is_file() else None)
        try:
            runs.append({"name": name, "report": load_report(path),
                         "provenance": None if manifest is None else provenance.load_json(manifest)})
        except ValueError as exc:
            raise ValueError(f"run {name}: {exc}") from None
    return runs


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m research.compare", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("reports", nargs="+", type=Path, help="walk-forward JSON reports (two or more)")
    parser.add_argument("--baseline", help="run name (report file stem) to compare against; default: first")
    parser.add_argument("--format", choices=("text", "json", "markdown"), default="text")
    parser.add_argument("--manifest", action="append", default=[], metavar="NAME=PATH",
                        help="provenance manifest for a run; overrides REPORT.manifest.json")
    args = parser.parse_args(argv)
    try:
        result = compare_runs(_cli_runs(args.reports, args.manifest), baseline=args.baseline)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(render(result, args.format))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
