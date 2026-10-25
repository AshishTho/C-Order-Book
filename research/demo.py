"""Reproducible SYNTHETIC demo: data, provenance, data quality, two walk-forward runs, comparison.

Usage (standard library only; no C++ build, network or credentials needed):
  python scripts/demo.py [--output-dir DIR] [--seed N] [--events N] [--force]
  python -m research.demo ...

Everything is written inside the output directory (a new temporary directory
when omitted). A non-empty directory is refused unless --force is given; --force
replaces only the files this demo writes and leaves any other file alone.
Windows refuses replacement over a directory symlink, preserving that link and
its referent; earlier outputs may already have been published before a failure.
For a given seed, event count and Python version every JSON output is
byte-identical; wall times appear only in DEMO_RESULTS.md. The BBO is rebuilt by
a simple Python aggregate book, so the C++ matching engine is not exercised.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import sys
import tempfile
import time

from research import compare, data_quality, provenance
from research.generate import EVENT_SCHEMA, generate_events
from research.lab import SCHEMA, Config, Snapshot
from research.walk_forward import evaluate_walk_forward

DEFAULT_SEED = 42
DEFAULT_EVENTS = 2000
INITIAL_TRAIN, TEST_SIZE = 600, 400
MIN_EVENTS, MAX_EVENTS = INITIAL_TRAIN + TEST_SIZE + 1, 10000  # one BBO row per event after the first
# Fixed placeholder so manifests stay byte-identical; the data is regenerated on every run.
RETRIEVED_AT = "1970-01-01T00:00:00+00:00"
POLICY = {"max_gap_seconds": 5, "max_staleness_seconds": 300, "on_gap": "reject",
          "forward_fill_limit_seconds": 0}
RUNS = (("baseline", {"fee_ticks": 0.1, "slippage_ticks": 0.0}),
        ("high_costs", {"fee_ticks": 0.5, "slippage_ticks": 0.25}))
OUTPUTS = ("events.csv", "bbo.csv", "data_quality_policy.json", "manifest.json", "data_quality.json",
           "baseline.json", "high_costs.json", "comparison.json", "comparison.md", "DEMO_RESULTS.md")
SELECTED_METRICS = ("execution.net_pnl_ticks_lots", "execution.fees_ticks_lots",
                    "execution.slippage_ticks_lots", "execution.completed_round_trips")
LIMITATIONS = (
    "Synthetic data only: a seeded toy event generator, not calibrated to any venue or instrument.",
    "No transaction-level market impact: simulated orders never change the book; displayed depth is "
    "assumed available and there is no queue position model.",
    "Results are not evidence of profitability; the folds are descriptive and not significance-tested.",
    "Not live-trading ready: no order gateway, exchange connectivity, risk controls or real clock.",
    "The C++ matching engine is not exercised by this demo (it needs cmake); the BBO is rebuilt by a "
    "simple Python aggregate book. Use scripts/run_study.py after a cmake build for the engine path.",
    "Timestamps are a synthetic clock (one second per event), so the data-quality check is a plumbing test.",
)


def reconstruct_bbo(events) -> list[Snapshot]:
    """Top of book after each event, one row whenever both sides are displayed.

    A deliberately simple aggregate reference book standing in for
    build/replay_events: it rejects crossing adds instead of matching them.
    """
    orders: dict[int, list] = {}
    levels: dict[str, dict[int, int]] = {"B": {}, "S": {}}
    rows = []
    for event in events:
        seq, kind, ref = event["seq"], event["type"], event["order_ref"]
        if kind == "A":
            side, price, quantity = event["side"], event["price"], event["qty"]
            if ref in orders or side not in levels or price <= 0 or quantity <= 0:
                raise ValueError(f"seq {seq}: invalid add")
            opposite = levels["S" if side == "B" else "B"]
            if opposite and (price >= min(opposite) if side == "B" else price <= max(opposite)):
                raise ValueError(f"seq {seq}: crossing add; this reference book does not match orders")
            orders[ref] = [side, price, quantity]
            levels[side][price] = levels[side].get(price, 0) + quantity
        else:
            if ref not in orders:
                raise ValueError(f"seq {seq}: unknown order_ref {ref}")
            side, price, remaining = orders[ref]
            quantity = remaining if kind == "D" else event["qty"]
            if kind not in ("D", "E", "X") or not 0 < quantity <= remaining:
                raise ValueError(f"seq {seq}: invalid {kind} event")
            levels[side][price] -= quantity
            if not levels[side][price]:
                del levels[side][price]
            orders[ref][2] -= quantity
            if not orders[ref][2]:
                del orders[ref]
        if levels["B"] and levels["S"]:
            bid, ask = max(levels["B"]), min(levels["S"])
            rows.append(Snapshot(seq, bid, ask, levels["B"][bid], levels["S"][ask]))
    return rows


def prepare_output(path: Path | None, force: bool) -> Path:
    """Return the output directory; refuse a non-empty one unless force."""
    if path is None:
        return Path(tempfile.mkdtemp(prefix="cob-demo-")).resolve()
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ValueError(f"output path exists and is not a directory: {path}")
    if path.is_dir() and any(path.iterdir()) and not force:
        raise ValueError(f"output directory is not empty: {path}; use a new directory or pass --force")
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


@contextmanager
def _target(root: Path, name: str, *, force: bool = False):
    """Stage one complete output, then publish without following the destination."""
    if name not in OUTPUTS:
        raise ValueError(f"not a demo output: {name}")
    path = root / name
    if path.is_dir() and not path.is_symlink():
        raise ValueError(f"{path} is a directory; refusing to replace it")
    with tempfile.TemporaryDirectory(prefix=".cob-demo-", dir=root) as folder:
        staged = Path(folder) / name
        yield staged
        if force:
            # Atomically replace supported files/symlinks. Directories and Windows
            # directory symlinks fail closed, retaining the destination and referent.
            os.replace(staged, path)
        else:
            # Unlike open("x") on Windows, a hard link also refuses a dangling destination symlink.
            try:
                os.link(staged, path)
            except FileExistsError as exc:
                raise FileExistsError(f"output already exists: {path}; use a new directory or pass --force") from exc
            except OSError as exc:
                raise OSError(f"cannot publish {path} without overwriting via a hard link: {exc}") from exc


def _write_text(root: Path, name: str, text: str, *, force: bool = False) -> None:
    with _target(root, name, force=force) as path:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)


def _write_json(root: Path, name: str, value, *, force: bool = False) -> None:
    _write_text(root, name, json.dumps(value, indent=2, allow_nan=False) + "\n", force=force)


@contextmanager
def _timed(timings: list, name: str):
    start = time.perf_counter()
    yield
    timings.append((name, time.perf_counter() - start))


def _parameters(config: Config) -> dict:
    parameters = asdict(config)
    parameters.pop("train_fraction")
    return {**parameters, "initial_train": INITIAL_TRAIN, "test_size": TEST_SIZE, "data_kind": "synthetic"}


def _walk_forward(rows, stamps, digest: str, config: Config, *, policy: dict) -> dict:
    report = evaluate_walk_forward(rows, config, initial_train=INITIAL_TRAIN, test_size=TEST_SIZE,
                                   timestamps=stamps, quality_policy=policy)
    # Relative paths keep reports byte-identical across output directories.
    report["source"] = {"path": "bbo.csv", "sha256": digest, "data_kind": "synthetic"}
    report["runtime"] = {"python": platform.python_version(), "walk_forward_version": 1}
    report["provenance"] = {"verified_manifest": "manifest.json", "status": "inputs_rehashed_and_matched"}
    for fold in report["folds"]:
        fold["source"] = report["source"].copy()
    return report


def _verified_sha(root: Path, manifest: dict, path: str) -> str:
    """Re-hash every manifest input before evaluation, as walk_forward --manifest does."""
    check = provenance.verify_manifest(manifest, root)
    if not check["valid"]:
        raise ValueError(f"manifest verification failed: changed={check['changed']} missing={check['missing']}")
    return next(item["sha256"] for item in manifest["inputs"] if item["path"] == path)


def run_demo(output_dir: Path | None, *, seed: int = DEFAULT_SEED, events: int = DEFAULT_EVENTS,
             force: bool = False, command: str = "") -> dict:
    if type(seed) is not int or type(events) is not int or not MIN_EVENTS <= events <= MAX_EVENTS:
        raise ValueError(f"seed must be an int and events an int from {MIN_EVENTS} to {MAX_EVENTS}")
    policy = data_quality.validate_policy(dict(POLICY))
    policy_text = json.dumps(policy, indent=2, allow_nan=False) + "\n"
    policy_bytes = policy_text.encode("utf-8")
    policy_sha256 = hashlib.sha256(policy_bytes).hexdigest()
    root = prepare_output(output_dir, force)
    timings: list[tuple[str, float]] = []
    total_start = time.perf_counter()
    with _timed(timings, "generate synthetic L3 events"):
        event_list = list(generate_events(events, seed))
        with _target(root, "events.csv", force=force) as path:
            with path.open("x", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=EVENT_SCHEMA, lineterminator="\n")
                writer.writeheader()
                writer.writerows(event_list)
    with _timed(timings, "rebuild timed BBO (Python reference book)"):
        snapshots = reconstruct_bbo(event_list)
        lines = [",".join((*SCHEMA, data_quality.TIMESTAMP_COLUMN))]
        lines += [f"{r.seq},{r.bid_price},{r.ask_price},{r.bid_qty},{r.ask_qty},{r.seq}" for r in snapshots]
        _write_text(root, "bbo.csv", "\n".join(lines) + "\n", force=force)
    configs = {name: Config(seed=seed, **costs) for name, costs in RUNS}
    with _timed(timings, "write provenance manifest"):
        _write_text(root, "data_quality_policy.json", policy_text, force=force)
        generated = f"synthetic: research.generate.generate_events(count={events}, seed={seed}); regenerated by research.demo"
        inputs = [{"path": "events.csv", "source": generated, "retrieved_at": RETRIEVED_AT},
                  {"path": "bbo.csv", "source": "synthetic: rebuilt from events.csv by research.demo.reconstruct_bbo; "
                                                "timestamp column is the event seq in seconds",
                   "retrieved_at": RETRIEVED_AT},
                  {"path": "data_quality_policy.json", "source": "data-quality policy declared by research.demo",
                   "retrieved_at": RETRIEVED_AT}]
        steps = [{"name": "research.generate.generate_events", "parameters": {"count": events, "seed": seed}},
                 {"name": "research.demo.reconstruct_bbo",
                  "parameters": {"book": "python aggregate reference book; C++ replay not used",
                                 "timestamp": "event seq as seconds"}},
                 {"name": "research.data_quality.apply_policy", "parameters": policy.copy()},
                 {"name": "research.walk_forward.evaluate_walk_forward",
                  "parameters": {"initial_train": INITIAL_TRAIN, "test_size": TEST_SIZE,
                                 "runs": [name for name, _ in RUNS]}}]
        experiments = [{"id": name, "parameters": _parameters(config),
                        "inputs": ["bbo.csv", "data_quality_policy.json"]} for name, config in configs.items()]
        manifest = provenance.build_manifest(inputs, steps, experiments, root=root)
        declared_policy = next(item["sha256"] for item in manifest["inputs"]
                               if item["path"] == "data_quality_policy.json")
        if declared_policy != policy_sha256:
            raise ValueError("data_quality_policy.json changed before manifest creation; refusing to run")
        with _target(root, "manifest.json", force=force) as path:
            provenance.write_manifest(manifest, path)
    with _timed(timings, "verify manifest and run data-quality check"):
        declared = _verified_sha(root, manifest, "bbo.csv")
        # Match one byte capture to the validated snapshot; do not reopen and parse a later file.
        if (root / "data_quality_policy.json").read_bytes() != policy_bytes:
            raise ValueError("data_quality_policy.json changed after manifest verification; refusing to run")
        rows, stamps, digest = data_quality.read_timed_snapshot_source(root / "bbo.csv")
        if digest != declared:
            raise ValueError("bbo.csv changed after manifest verification; refusing to run")
        outcome = data_quality.require_evaluable(data_quality.apply_policy(rows, policy.copy(), timestamps=stamps))
        _write_json(root, "data_quality.json", {"input": "bbo.csv", "sha256": digest, "rows": len(rows),
                                                "accepted_rows": len(outcome["rows"]),
                                                **data_quality.summarize(outcome)}, force=force)
    reports = {}
    for name, config in configs.items():
        with _timed(timings, f"walk-forward: {name}"):
            reports[name] = _walk_forward(rows, stamps, digest, config, policy=policy.copy())
            _write_json(root, f"{name}.json", reports[name], force=force)
    with _timed(timings, "compare runs"):
        result = compare.compare_runs([{"name": name, "report": report, "provenance": manifest}
                                       for name, report in reports.items()])
        _write_text(root, "comparison.json", compare.render(result, "json"), force=force)
        _write_text(root, "comparison.md", compare.render(result, "markdown"), force=force)
    total = time.perf_counter() - total_start
    summary = {"output_dir": root, "seed": seed, "events": events, "observations": len(rows),
               "bbo_sha256": digest, "folds": len(reports["baseline"]["folds"]),
               "unused_tail": reports["baseline"]["unused_tail"]["observations"],
               "timings": timings, "total_seconds": total, "comparison": result}
    _write_text(root, "DEMO_RESULTS.md", render_results(summary, command), force=force)
    return summary


def _runs_table(result: dict) -> list[str]:
    lines = ["| run | status | reasons | flags | provenance | parameter changes |",
             "| --- | --- | --- | --- | --- | --- |"]
    for run in result["runs"]:
        changes = "; ".join(f"{key}: {item['baseline']} -> {item['run']}"
                            for key, item in run["parameter_changes"].items()) or "none"
        lines.append(f"| {run['name']} | {run['status']} | {', '.join(run['reasons']) or 'none'} | "
                     f"{', '.join(run['flags']) or 'none'} | {run['provenance']['status']} | {changes} |")
    return lines


def render_results(summary: dict, command: str) -> str:
    result = summary["comparison"]
    names = [run["name"] for run in result["runs"]]
    costs = "; ".join(f"`{name}`: fee {c['fee_ticks']}, slippage {c['slippage_ticks']}" for name, c in RUNS)
    lines = ["# Synthetic demo results", "",
             "Produced by `research.demo` on SYNTHETIC data. Read the Limitations section before the numbers.", "",
             "## Run", "",
             "Command:", "", "```sh", command or "(not recorded)", "```", "",
             f"- Python: {platform.python_version()} ({platform.python_implementation()})",
             f"- Seed: {summary['seed']} (generator and random control)",
             f"- Dataset: {summary['events']} synthetic L3 events -> {summary['observations']} timed BBO observations",
             f"- bbo.csv SHA-256: `{summary['bbo_sha256']}`",
             f"- Walk-forward: initial_train={INITIAL_TRAIN}, test_size={TEST_SIZE}, {summary['folds']} folds, "
             f"unused tail {summary['unused_tail']} rows",
             f"- Costs in ticks per lot per side: {costs}",
             f"- Output directory: `{summary['output_dir']}`", "",
             "## Wall time per step", "", "| step | seconds |", "| --- | ---: |"]
    lines += [f"| {name} | {seconds:.3f} |" for name, seconds in summary["timings"]]
    lines += [f"| total | {summary['total_seconds']:.3f} |", "",
              "Wall times vary by machine and run; they are excluded from every JSON file.", "",
              "## Comparison", "", f"Baseline: `{result['baseline']}`. Comparability flags per run:", ""]
    lines += _runs_table(result)
    lines += ["", "Pairwise comparability:", ""]
    lines += [f"- {a} vs {b}: {', '.join(item['reasons']) or 'compatible'}"
              for item in result["pairwise"] for a, b in [item["runs"]]]
    lines += ["", "Selected execution metrics per fold and strategy (cells: value, n=sample, label). "
              "Incompatible runs keep their values but get no deltas. All metrics: `comparison.md`.", "",
              "| metric | better | n basis | " + " | ".join(
                  f"{name} (baseline)" if name == result["baseline"] else name for name in names) + " |",
              "|" + " --- |" * (3 + len(names))]
    for row in result["metrics"]:
        if row["metric"].split(".", 2)[2] in SELECTED_METRICS:
            cells = " | ".join(compare._cell_text(row["cells"][name]) for name in names)
            lines.append(f"| {row['metric']} | {row['direction'] or '-'} | {row['sample_basis']} | {cells} |")
    lines += ["", "Comparison notes:", "", *(f"- {note}" for note in result["notes"]), "",
              "## Limitations", "", *(f"- {item}" for item in LIMITATIONS), "",
              "## Files", "",
              "- `events.csv`: synthetic L3 events; `bbo.csv`: timed top of book rebuilt from them",
              "- `data_quality_policy.json`, `data_quality.json`: declared policy and its findings",
              "- `manifest.json`: provenance manifest (input SHA-256, transformations, both experiments)",
              "- `baseline.json`, `high_costs.json`: walk-forward reports",
              "- `comparison.json`, `comparison.md`: full run comparison", "",
              "Re-check from the output directory:", "", "```sh",
              "python -m research.provenance verify manifest.json",
              "python -m research.compare baseline.json high_costs.json --manifest baseline=manifest.json \\",
              "  --manifest high_costs=manifest.json --format markdown", "```"]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python scripts/demo.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output-dir", type=Path, help="new or empty directory (default: a new temporary directory)")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--events", type=int, default=DEFAULT_EVENTS,
                        help=f"synthetic L3 events, {MIN_EVENTS} to {MAX_EVENTS}")
    parser.add_argument("--force", action="store_true",
                        help="write into a non-empty directory, replacing only this demo's own files")
    args = parser.parse_args(argv)
    if argv is None:
        command = shlex.join(getattr(sys, "orig_argv", [sys.executable, *sys.argv]))
    else:
        command = f"research.demo.main({list(argv)!r})  # in-process call"
    try:
        summary = run_demo(args.output_dir, seed=args.seed, events=args.events, force=args.force, command=command)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"Wrote SYNTHETIC demo to {summary['output_dir']} in {summary['total_seconds']:.1f}s; "
          f"see {summary['output_dir'] / 'DEMO_RESULTS.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
