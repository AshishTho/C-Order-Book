"""Declared gap/staleness/forward-fill policy and its walk-forward gate (synthetic data only)."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research import data_quality, walk_forward
from research.data_quality import DataQualityError, apply_policy, check_series, require_evaluable
from research.lab import Config, Snapshot

POLICY = {"max_gap_seconds": 10, "max_staleness_seconds": 30, "on_gap": "reject",
          "forward_fill_limit_seconds": 20}


def policy(**changes):
    return {**POLICY, **changes}


def moving(count):
    """Every row changes size, so nothing is stale."""
    return [Snapshot(i + 1, 100, 102, 10 + i, 10) for i in range(count)]


def market(count=180):
    return [Snapshot(i + 1, 100 + i // 3, 102 + i // 3, 20 + i % 11, 10 + i % 7) for i in range(count)]


def kinds(findings):
    return [(item["kind"], item["index"]) for item in findings]


class CheckSeriesTests(unittest.TestCase):
    def test_clean_series_passes_unchanged(self):
        rows, stamps = moving(6), [0, 10, 20, 30, 40, 50]
        self.assertEqual(check_series(rows, POLICY, timestamps=stamps), [])
        for action in ("reject", "mark"):
            outcome = apply_policy(rows, policy(on_gap=action), timestamps=stamps)
            self.assertEqual(outcome["rows"], tuple(rows))
            self.assertEqual(outcome["timestamps"], tuple(stamps))
            self.assertEqual(outcome["flags"], ((),) * 6)
            self.assertEqual((outcome["rejected"], outcome["marked"], outcome["forward_fills"]), ([], [], []))
            self.assertIs(require_evaluable(outcome), outcome)

    def test_gap_detection_at_boundary(self):
        findings = check_series(moving(4), POLICY, timestamps=[0, 10, 21, 31])
        self.assertEqual(kinds(findings), [("gap", 2)])
        self.assertEqual({k: findings[0][k] for k in ("seq", "timestamp", "previous_timestamp", "gap_seconds")},
                         {"seq": 3, "timestamp": 21, "previous_timestamp": 10, "gap_seconds": 11})
        # Exact rational comparison: one ulp above the limit is a gap, the limit itself is not.
        self.assertEqual(check_series(moving(2), policy(max_gap_seconds=0.5), timestamps=[1.0, 1.5]), [])
        self.assertEqual(kinds(check_series(moving(2), policy(max_gap_seconds=0.5),
                                            timestamps=[1.0, 1.5000000000000002])), [("gap", 1)])

    def test_stale_quote_detection_at_boundary(self):
        rows = [Snapshot(i + 1, 100, 102, 10, 10) for i in range(5)] + [Snapshot(6, 100, 102, 11, 10)]
        findings = check_series(rows, POLICY, timestamps=[0, 10, 20, 30, 31, 40])
        self.assertEqual(kinds(findings), [("stale", 4)])
        self.assertEqual((findings[0]["timestamp"], findings[0]["unchanged_since"], findings[0]["stale_seconds"]),
                         (31, 0, 31))
        # Any change in price or size restarts the unchanged run.
        rows[2] = Snapshot(3, 100, 103, 10, 10)
        self.assertEqual(check_series(rows, POLICY, timestamps=[0, 10, 20, 30, 31, 40]), [])

    def test_crossed_locked_and_empty_books_detected(self):
        rows = [Snapshot(1, 100, 102, 5, 5), Snapshot(2, 103, 102, 5, 5), Snapshot(3, 102, 102, 5, 6),
                Snapshot(4, 100, 102, 0, 5), Snapshot(5, 100, 102, 5, 7)]
        findings = check_series(rows, POLICY, timestamps=[0, 1, 2, 3, 4])
        self.assertEqual(kinds(findings), [("crossed", 1), ("crossed", 2), ("empty", 3)])
        self.assertEqual(findings[0]["detail"], "crossed: bid > ask")
        self.assertEqual(findings[1]["detail"], "locked: bid == ask")
        self.assertEqual(findings[0]["timestamp"], 1)
        # Book errors are rejected even under 'mark'.
        outcome = apply_policy(rows, policy(on_gap="mark"), timestamps=[0, 1, 2, 3, 4])
        self.assertEqual([item["index"] for item in outcome["rejected"]], [1, 2, 3])
        self.assertEqual(outcome["marked"], [])
        with self.assertRaisesRegex(DataQualityError, "evaluation stopped"):
            require_evaluable(outcome)

    def test_invalid_policy_and_timestamps(self):
        bad = [{k: v for k, v in POLICY.items() if k != "on_gap"}, policy(extra=1), policy(on_gap="drop"),
               policy(max_gap_seconds=0), policy(max_gap_seconds=True), policy(max_staleness_seconds=float("inf")),
               policy(forward_fill_limit_seconds=-1), policy(max_gap_seconds="10")]
        for item in bad:
            with self.subTest(item), self.assertRaises(ValueError):
                check_series(moving(2), item, timestamps=[0, 1])
        for stamps in ([0, 0], [1, 0], [0], [0, float("nan")], [-1, 0], [0, True]):
            with self.subTest(stamps), self.assertRaises(ValueError):
                check_series(moving(2), POLICY, timestamps=stamps)


class ForwardFillTests(unittest.TestCase):
    def test_fill_stops_at_limit_and_never_continues_indefinitely(self):
        stamps = [0, 15, 35, 56, 1_000_056]
        outcome = apply_policy(moving(5), policy(on_gap="mark"), timestamps=stamps)
        fills = {item["index"]: item for item in outcome["forward_fills"]}
        self.assertEqual(sorted(fills), [1, 2, 3, 4])
        # A gap within the limit is bridged completely; the row is accepted unflagged.
        self.assertEqual((fills[1]["filled_seconds"], fills[1]["uncovered_seconds"]), (15, 0))
        # Exactly at the limit is still bridged; one second beyond is not.
        self.assertEqual((fills[2]["filled_seconds"], fills[2]["uncovered_seconds"]), (20, 0))
        self.assertEqual((fills[3]["filled_seconds"], fills[3]["uncovered_seconds"]), (20, 1))
        # A huge gap is filled for the limit only, never up to the next observation.
        self.assertEqual((fills[4]["from_timestamp"], fills[4]["filled_until"], fills[4]["filled_seconds"],
                          fills[4]["uncovered_seconds"]), (56, 76, 20, 1_000_000 - 20))
        self.assertEqual(outcome["flags"], ((), (), (), ("gap_beyond_fill_limit",), ("gap_beyond_fill_limit",)))
        self.assertEqual([item["index"] for item in outcome["marked"]], [3, 4])
        self.assertTrue(all(item["filled_seconds"] <= 20 for item in outcome["forward_fills"]))

    def test_zero_limit_disables_forward_fill(self):
        outcome = apply_policy(moving(3), policy(forward_fill_limit_seconds=0), timestamps=[0, 11, 12])
        self.assertEqual(outcome["forward_fills"][0]["filled_seconds"], 0)
        self.assertEqual([(item["index"], item["reasons"]) for item in outcome["rejected"]],
                         [(1, ["gap_beyond_fill_limit"])])

    def test_reject_removes_rows_and_stops_evaluation_explicitly(self):
        rows = moving(4)
        outcome = apply_policy(rows, POLICY, timestamps=[0, 5, 40, 45])
        self.assertEqual(outcome["rows"], (rows[0], rows[1], rows[3]))
        self.assertEqual(outcome["rejected"], [{"index": 2, "seq": 3, "timestamp": 40,
                                                "reasons": ["gap_beyond_fill_limit"]}])
        with self.assertRaisesRegex(DataQualityError, r"rejected 1 row\(s\); evaluation stopped: index 2 t=40"):
            require_evaluable(outcome)

    def test_mark_keeps_rows_but_flags_them(self):
        rows = [Snapshot(i + 1, 100, 102, 10, 10) for i in range(5)]
        outcome = apply_policy(rows, policy(on_gap="mark"), timestamps=[0, 10, 20, 30, 100])
        self.assertEqual(outcome["rows"], tuple(rows))
        self.assertEqual(outcome["rejected"], [])
        self.assertEqual(outcome["flags"][4], ("gap_beyond_fill_limit", "stale"))
        self.assertEqual([(item["index"], item["reasons"]) for item in outcome["marked"]],
                         [(4, ["gap_beyond_fill_limit", "stale"])])
        self.assertIs(require_evaluable(outcome), outcome)


class WalkForwardIntegrationTests(unittest.TestCase):
    config = Config(horizon=5, delay=2)

    def evaluate(self, rows, **kwargs):
        return walk_forward.evaluate_walk_forward(rows, self.config, initial_train=60, test_size=40, **kwargs)

    def test_clean_policy_leaves_folds_unchanged(self):
        rows = market()
        baseline = self.evaluate(rows)
        checked = self.evaluate(rows, timestamps=list(range(180)), quality_policy=POLICY)
        self.assertEqual(checked.pop("data_quality")["finding_counts"], {})
        for fold in checked["folds"]:
            self.assertEqual(fold.pop("data_quality_marked_indices"), [])
        self.assertEqual(checked, baseline)
        self.assertNotIn("data_quality", baseline)

    def test_rejected_rows_stop_evaluation_before_calibration(self):
        stamps = list(range(120)) + [t + 500 for t in range(120, 180)]
        with patch.object(walk_forward, "calibrate", wraps=walk_forward.calibrate) as spy:
            with self.assertRaisesRegex(DataQualityError, "index 120"):
                self.evaluate(market(), timestamps=stamps, quality_policy=POLICY)
        self.assertEqual(spy.call_count, 0)
        with self.assertRaises(ValueError):
            self.evaluate(market(), timestamps=stamps)

    def test_marked_rows_are_evaluated_and_disclosed(self):
        rows = market()
        stamps = list(range(120)) + [t + 500 for t in range(120, 180)]
        report = self.evaluate(rows, timestamps=stamps, quality_policy=policy(on_gap="mark"))
        self.assertEqual([item["index"] for item in report["data_quality"]["marked"]], [120])
        # Each fold reads rows[:test_stop], so a marked row stays disclosed in every later fold.
        self.assertEqual([fold["data_quality_marked_indices"] for fold in report["folds"]], [[], [120], [120]])
        self.assertEqual(report["folds"][2]["strategies"], self.evaluate(rows)["folds"][2]["strategies"])
        json.dumps(report, allow_nan=False)

    def test_cli_policy_flag(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            policy_path = root / "policy.json"
            policy_path.write_text(json.dumps(POLICY), encoding="utf-8")

            def write(name, stamps, rows=None):
                path = root / name
                path.write_text("seq,bid_price,ask_price,bid_qty,ask_qty,timestamp\n" + "".join(
                    f"{r.seq},{r.bid_price},{r.ask_price},{r.bid_qty},{r.ask_qty},{t}\n"
                    for r, t in zip(rows or market(), stamps)), encoding="utf-8")
                return path

            def run(source, output):
                args = ["--input", str(source), "--output", str(output), "--data-kind", "synthetic",
                        "--initial-train", "60", "--test-size", "40", "--horizon", "5", "--delay", "2",
                        "--data-quality", str(policy_path)]
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                    try:
                        return walk_forward.main(args), err.getvalue()
                    except SystemExit as exit_:
                        return exit_.code, err.getvalue()

            clean = write("clean.csv", [f"{t}.25" for t in range(180)])
            self.assertEqual(run(clean, root / "clean.json")[0], 0)
            report = json.loads((root / "clean.json").read_text(encoding="utf-8"))
            self.assertEqual(report["data_quality"]["policy"], POLICY)
            gapped = write("gapped.csv", list(range(120)) + [t + 500 for t in range(120, 180)])
            code, err = run(gapped, root / "gapped.json")
            self.assertEqual(code, 2)
            self.assertIn("evaluation stopped", err)
            self.assertFalse((root / "gapped.json").exists())
            rows = market()
            rows[150] = Snapshot(rows[150].seq, 300, 200, 5, 5)
            crossed = write("crossed.csv", range(180), rows)
            code, err = run(crossed, root / "crossed.json")
            self.assertEqual(code, 2)
            self.assertIn("index 150 t=150: crossed", err)
            (root / "plain.csv").write_text("seq,bid_price,ask_price,bid_qty,ask_qty\n1,1,2,1,1\n", encoding="utf-8")
            code, err = run(root / "plain.csv", root / "plain.json")
            self.assertEqual(code, 2)
            self.assertIn("timestamp", err)

    def test_reader_hashes_same_bytes_and_keeps_bad_books_for_reporting(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bbo.csv"
            raw = b"seq,bid_price,ask_price,bid_qty,ask_qty,timestamp\n1,100,101,5,5,0\n2,102,101,5,5,1.5\n"
            path.write_bytes(raw)
            rows, stamps, digest = data_quality.read_timed_snapshot_source(path)
            self.assertEqual(stamps, [0, 1.5])
            self.assertEqual(rows[1], Snapshot(2, 102, 101, 5, 5))
            self.assertEqual(digest, hashlib.sha256(raw).hexdigest())
            for bad in (b"3,100,101,5,5,x\n", b"1,100,101,5,5,3\n", b"3,100,101,5,5,nan\n", b"3,100,101,5,5\n"):
                path.write_bytes(raw + bad)
                with self.subTest(bad), self.assertRaises(ValueError):
                    data_quality.read_timed_snapshot_source(path)

    def test_policy_reader_hashes_exact_parsed_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "policy.json"
            raw = b"\xef\xbb\xbf" + json.dumps(POLICY, indent=2).encode("utf-8") + b"\r\n"
            path.write_bytes(raw)
            original_read = Path.read_bytes

            def read_then_change(source):
                payload = original_read(source)
                source.write_text(json.dumps(policy(on_gap="mark")), encoding="utf-8")
                return payload

            with patch.object(Path, "read_bytes", read_then_change):
                parsed, digest = data_quality.read_policy_source(path)
            self.assertEqual(parsed, POLICY)
            self.assertEqual(digest, hashlib.sha256(raw).hexdigest())
            self.assertEqual(data_quality.load_policy(path), policy(on_gap="mark"))

    def test_policy_reader_retains_strict_json_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "policy.json"
            for raw in (json.dumps(POLICY)[:-1] + ', "on_gap": "mark"}',
                        json.dumps(POLICY).replace('"max_gap_seconds": 10', '"max_gap_seconds": NaN')):
                path.write_text(raw, encoding="utf-8")
                for reader in (data_quality.read_policy_source, data_quality.load_policy):
                    with self.subTest(raw=raw, reader=reader.__name__), self.assertRaises(ValueError):
                        reader(path)


if __name__ == "__main__":
    unittest.main()
