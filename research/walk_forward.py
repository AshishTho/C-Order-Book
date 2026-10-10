"""Expanding-history, fixed-parameter research folds; no strategy selection or live execution."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
from typing import Sequence

from research.lab import (Config, SIGNALS, Snapshot, calibrate, feature_series,
                          predictive_metrics, read_snapshot_source, simulate,
                          validate_output_paths, write_html)


def evaluate_walk_forward(rows: Sequence[Snapshot], config: Config, *,
                          initial_train: int, test_size: int) -> dict:
    """Freeze each calibration before its non-overlapping future test block.

    Folds are independent flat-start simulations. Prior test rows become eligible
    history for later folds, only after their labels are observable. No scores
    choose hyperparameters, signals, costs, or the fold schedule.
    """
    config.validate()
    margin = config.delay + config.horizon
    if any(type(x) is not int or x < 20 + margin for x in (initial_train, test_size)):
        raise ValueError("initial_train and test_size must allow 20 decisions after delay+horizon")
    if len(rows) < initial_train + test_size:
        raise ValueError("need at least one complete future test block")
    parameters = asdict(config)
    parameters.pop("train_fraction")  # Explicit observation counts replace the single-holdout fraction.
    result = {"schema_version": 1, "protocol": "expanding_history_independent_folds",
              "config": parameters, "initial_train": initial_train, "test_size": test_size,
              "total_observations": len(rows), "folds": [],
              "limitations": [
                  "Fixed inputs only; no hyperparameter or strategy selection from future periods.",
                  "Earlier test blocks enter later training history only once their labels are observable.",
                  "Independent flat-start folds; terminal inventory is retained, not liquidated or carried.",
                  "No stitched portfolio PnL or equity: each fold is a separate bounded experiment.",
                  "Only complete blocks are evaluated; unused trailing rows are explicitly reported.",
                  "Overlapping labels and reused training histories are not independent statistical evidence.",
                  "Repeated inspection changes a holdout into development data; reserve new data for final validation.",
                  "Synthetic fixtures are software checks, not evidence of profitable trading."]}
    for start in range(initial_train, len(rows) - test_size + 1, test_size):
        end = start + test_size
        train_stop = start - margin
        # Calibration cannot access the current test block or any later observation.
        history = rows[:start]
        training_features = feature_series(history, config.seed)
        prefix = rows[:end]
        features = feature_series(prefix, config.seed)
        fold = {"schema_version": 1, "fold": len(result["folds"]), "config": parameters.copy(),
                "units": {"price": "stored integer ticks", "quantity": "lots",
                          "pnl": "ticks * lots", "time": "valid two-sided BBO observations"},
                "split": {"total_observations": end, "train_decision_start": 0,
                          "train_decision_stop_exclusive": train_stop,
                          "latest_train_label_index": train_stop - 1 + margin,
                          "embargo_observations": margin, "test_start": start,
                          "test_start_seq": rows[start].seq, "test_stop_exclusive": end,
                          "test_decision_stop_exclusive": end - margin},
                "assumptions": [*result["limitations"],
                    "Threshold and correlation orientation use training observations only.",
                    "Delayed entries and exits cross displayed quotes; top depth caps fills.",
                    "Partial exits stop at the fold boundary; any remaining inventory stays disclosed.",
                    "Midpoint equity and terminal liquidation marks are valuations, never invented fills.",
                    "Displayed liquidity is assumed available; no impact, queues, funding or hidden depth model.",
                    "Event delay counts BBO observations, not clock time; fee/slippage inputs are ticks per lot per side.",
                    "No fee/delay sensitivity panels are computed in this walk-forward increment."],
                "strategies": {}}
        for name in SIGNALS:
            calibration = calibrate(training_features[name], history, train_stop, config, name)
            fold["strategies"][name] = {
                "train_calibration": asdict(calibration),
                "train_predictive": predictive_metrics(training_features[name], history, 0, train_stop, calibration, config),
                "test_predictive": predictive_metrics(features[name], prefix, start, end - margin, calibration, config),
                "execution": simulate(prefix, features[name], start, calibration, config),
                "fee_sensitivity": [], "delay_sensitivity": []}
        result["folds"].append(fold)
    consumed = result["folds"][-1]["split"]["test_stop_exclusive"]
    result["unused_tail"] = {"start": consumed, "stop_exclusive": len(rows), "observations": len(rows) - consumed}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new JSON report; never overwritten")
    parser.add_argument("--data-kind", choices=("synthetic", "historical"), required=True)
    parser.add_argument("--initial-train", type=int, required=True)
    parser.add_argument("--test-size", type=int, required=True)
    parser.add_argument("--html", type=Path, help="optional existing replay/report renderer for one fold")
    parser.add_argument("--html-fold", type=int, default=0, help="zero-based fold for --html")
    parser.add_argument("--horizon", type=int, default=10)
    parser.add_argument("--delay", type=int, default=1)
    parser.add_argument("--threshold-quantile", type=float, default=0.75)
    parser.add_argument("--fee-ticks", type=float, default=0.1)
    parser.add_argument("--slippage-ticks", type=float, default=0.0)
    parser.add_argument("--order-size", type=int, default=1)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)
    config = Config(**{key: getattr(args, key) for key in Config.__dataclass_fields__ if key != "train_fraction"})
    try:
        validate_output_paths(args.input, args.output, args.html)
        rows, source_hash = read_snapshot_source(args.input)
        report = evaluate_walk_forward(rows, config, initial_train=args.initial_train, test_size=args.test_size)
        if not 0 <= args.html_fold < len(report["folds"]):
            raise ValueError("html-fold must name an evaluated fold")
        report["source"] = {"path": str(args.input), "sha256": source_hash, "data_kind": args.data_kind}
        report["runtime"] = {"python": sys.version.split()[0], "walk_forward_version": 1}
        for fold in report["folds"]:
            fold["source"] = report["source"].copy()
        document = json.dumps(report, indent=2, allow_nan=False) + "\n"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(document)
        if args.html:
            write_html(report["folds"][args.html_fold], args.html)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(f"Wrote {len(report['folds'])} independent folds; unused tail: {report['unused_tail']['observations']} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
