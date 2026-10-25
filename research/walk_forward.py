"""Expanding-history, fixed-parameter research folds; no strategy selection or live execution."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
from typing import Sequence

from research import data_quality, provenance
from research.lab import (Config, SIGNALS, Snapshot, calibrate, feature_series,
                          predictive_metrics, read_snapshot_source, simulate,
                          validate_output_paths, validate_snapshot_rows, write_html)


def evaluate_walk_forward(rows: Sequence[Snapshot], config: Config, *,
                          initial_train: int, test_size: int,
                          timestamps: Sequence | None = None, quality_policy: dict | None = None) -> dict:
    """Freeze each calibration before its non-overlapping future test block.

    Folds are independent flat-start simulations. Prior test rows become eligible
    history for later folds, only after their labels are observable. No scores
    choose hyperparameters, signals, costs, or the fold schedule.

    Optional quality_policy (with timestamps) applies research.data_quality first;
    any rejected row stops evaluation with DataQualityError, marked rows are kept
    unchanged and disclosed under "data_quality".
    """
    config.validate()
    validate_snapshot_rows(rows, allow_invalid_books=quality_policy is not None)
    margin = config.delay + config.horizon
    if any(type(x) is not int or x < 20 + margin for x in (initial_train, test_size)):
        raise ValueError("initial_train and test_size must allow 20 decisions after delay+horizon")
    if len(rows) < initial_train + test_size:
        raise ValueError("need at least one complete future test block")
    quality = None
    if quality_policy is not None or timestamps is not None:
        if quality_policy is None or timestamps is None:
            raise ValueError("quality_policy and timestamps must be supplied together")
        quality = data_quality.require_evaluable(
            data_quality.apply_policy(rows, quality_policy, timestamps=timestamps))
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
                "strategies": {},
                "book_snapshots": [{"index": index, **asdict(rows[index])}
                                   for index in range(start, end)]}
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
    if quality is not None:
        result["data_quality"] = data_quality.summarize(quality)
        for fold in result["folds"]:
            stop = fold["split"]["test_stop_exclusive"]
            fold["data_quality_marked_indices"] = [item["index"] for item in quality["marked"]
                                                   if item["index"] < stop]
    return result


def _manifest_path(input_path: Path, root: Path) -> str:
    relative = os.path.relpath(input_path.resolve(), root.resolve()).replace(os.sep, "/")
    return provenance.check_relative_path(relative, "input path relative to manifest")


def _require_verified_inputs(manifest_path: Path, input_paths: dict[str, Path]) -> dict:
    """Re-hash every manifest input and require each consumed file to be listed."""
    manifest = provenance.load_json(manifest_path)
    report = provenance.verify_manifest(manifest, manifest_path.parent)
    if not report["valid"]:
        raise ValueError(f"manifest verification failed; refusing to run: changed={report['changed']} "
                         f"missing={report['missing']} manifest_hash_ok={report['manifest_hash_ok']} "
                         f"affected_experiments={report['affected_experiments']}")
    declarations = {item["path"]: item for item in manifest["inputs"]}
    declared = {}
    for argument, input_path in input_paths.items():
        wanted = _manifest_path(input_path, manifest_path.parent)
        if wanted not in declarations:
            raise ValueError(f"--{argument} {input_path} is not an input of manifest {manifest_path}")
        declared[argument] = declarations[wanted]
    return declared


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
    parser.add_argument("--manifest", type=Path,
                        help="existing provenance manifest; refuse to run unless every input re-hashes "
                             "and --input plus any --data-quality policy are listed "
                             "(paths relative to the manifest's directory)")
    parser.add_argument("--write-manifest", action="store_true",
                        help="also create OUTPUT.manifest.json recording inputs, steps and parameters")
    parser.add_argument("--input-source", help="free-text input origin for --write-manifest")
    parser.add_argument("--input-retrieved-at", help="ISO 8601 time with offset for --write-manifest")
    parser.add_argument("--experiment-id", default="walk_forward", help="experiment id for --write-manifest")
    parser.add_argument("--data-quality", type=Path,
                        help="JSON policy; requires a 'timestamp' seconds column and stops on rejected rows")
    args = parser.parse_args(argv)
    config = Config(**{key: getattr(args, key) for key in Config.__dataclass_fields__ if key != "train_fraction"})
    manifest_out = args.output.with_name(args.output.name + ".manifest.json")
    try:
        if args.write_manifest and not (args.input_source and args.input_retrieved_at):
            raise ValueError("--write-manifest requires --input-source and --input-retrieved-at")
        validate_output_paths(args.input, args.output, args.html)
        if args.write_manifest and (manifest_out.exists() or manifest_out.is_symlink()):
            raise ValueError(f"Output already exists: {manifest_out}; use a new path")
        input_paths = {"input": args.input}
        if args.data_quality:
            input_paths["data-quality"] = args.data_quality
        declared = _require_verified_inputs(args.manifest, input_paths) if args.manifest else None
        policy = timestamps = policy_hash = None
        if args.data_quality:
            policy, policy_hash = data_quality.read_policy_source(args.data_quality)
            rows, timestamps, source_hash = data_quality.read_timed_snapshot_source(args.input)
        else:
            rows, source_hash = read_snapshot_source(args.input)
        if declared is not None:
            if declared["input"]["sha256"] != source_hash:
                raise ValueError("input changed after manifest verification; refusing to run")
            if policy is not None and declared["data-quality"]["sha256"] != policy_hash:
                raise ValueError("policy changed after manifest verification; refusing to run")
        report = evaluate_walk_forward(rows, config, initial_train=args.initial_train, test_size=args.test_size,
                                       timestamps=timestamps, quality_policy=policy)
        if not 0 <= args.html_fold < len(report["folds"]):
            raise ValueError("html-fold must name an evaluated fold")
        report["source"] = {"path": str(args.input), "sha256": source_hash, "data_kind": args.data_kind}
        report["runtime"] = {"python": sys.version.split()[0], "walk_forward_version": 1}
        if args.manifest:
            report["provenance"] = {"verified_manifest": str(args.manifest), "status": "inputs_rehashed_and_matched"}
        for fold in report["folds"]:
            fold["source"] = report["source"].copy()
        manifest = None
        if args.write_manifest:
            root = manifest_out.parent
            root.mkdir(parents=True, exist_ok=True)  # POSIX cannot traverse '..' from a missing directory.
            inputs = [{"path": _manifest_path(args.input, root), "source": args.input_source,
                       "retrieved_at": args.input_retrieved_at}]
            steps = [{"name": "research.lab.read_snapshot_source", "parameters": {}}]
            if policy is not None:
                inputs.append({"path": _manifest_path(args.data_quality, root), "source": "data-quality policy file",
                               "retrieved_at": args.input_retrieved_at})
                steps = [{"name": "research.data_quality.read_timed_snapshot_source", "parameters": {}},
                         {"name": "research.data_quality.apply_policy", "parameters": policy}]
            parameters = {**report["config"], "initial_train": args.initial_train,
                          "test_size": args.test_size, "data_kind": args.data_kind}
            steps.append({"name": "research.walk_forward.evaluate_walk_forward", "parameters": parameters})
            manifest = provenance.build_manifest(inputs, steps, {"id": args.experiment_id, "parameters": parameters},
                                                 root=root)
            if manifest["inputs"][0]["sha256"] != source_hash:
                raise ValueError("input changed while building the manifest; nothing written")
            if policy is not None and manifest["inputs"][1]["sha256"] != policy_hash:
                raise ValueError("policy changed while building the manifest; nothing written")
        document = json.dumps(report, indent=2, allow_nan=False) + "\n"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(document)
        if args.html:
            write_html(report["folds"][args.html_fold], args.html)
        if manifest is not None:
            provenance.write_manifest(manifest, manifest_out)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(f"Wrote {len(report['folds'])} independent folds; unused tail: {report['unused_tail']['observations']} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
