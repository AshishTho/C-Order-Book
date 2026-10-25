"""Chronology, frozen calibration, bounded fills, and report preservation."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research.lab import Config, SIGNALS, Snapshot, calibrate
from research import walk_forward
from research.data_quality import DataQualityError
from research.replay_viewer import replay_payload, render_replay
from research.walk_forward import evaluate_walk_forward, main


def market(count=180):
    return [Snapshot(i + 1, 100 + i // 3, 102 + i // 3, 20 + i % 11, 10 + i % 7)
            for i in range(count)]


class WalkForwardTests(unittest.TestCase):
    config = Config(horizon=5, delay=2)

    def evaluate(self, rows):
        return evaluate_walk_forward(rows, self.config, initial_train=60, test_size=40)

    def test_exact_boundaries_and_training_input_is_only_history(self):
        with patch("research.walk_forward.calibrate", wraps=calibrate) as spy:
            result = self.evaluate(market(187))
        self.assertEqual(len(result["folds"]), 3)
        self.assertEqual(result["unused_tail"], {"start": 180, "stop_exclusive": 187, "observations": 7})
        for number, fold in enumerate(result["folds"]):
            split = fold["split"]
            start, end = 60 + number * 40, 100 + number * 40
            self.assertEqual(split["test_start"], start)
            self.assertEqual(split["test_stop_exclusive"], end)
            self.assertEqual(split["latest_train_label_index"], start - 1)
            self.assertEqual(split["train_decision_stop_exclusive"], start - 7)
            self.assertEqual([row['index'] for row in fold['book_snapshots']], list(range(start, end)))
            self.assertEqual(replay_payload(fold)['invariants']['issues'], [])
            for call in spy.call_args_list[number * len(SIGNALS):(number + 1) * len(SIGNALS)]:
                self.assertEqual(len(call.args[1]), start)
                self.assertEqual(len(call.args[0]), start)
            for item in fold["strategies"].values():
                sim = item["execution"]
                self.assertEqual(item["test_predictive"]["decision_observations"], 33)
                self.assertEqual([p["index"] for p in sim["equity_curve"]], list(range(start, end)))
                trades = sim["trades"] + ([sim["open_trade"]] if sim["open_trade"] else [])
                for trade in trades:
                    self.assertGreaterEqual(trade["decision_index"], start)
                    self.assertLess(trade["decision_index"] + 7, end)
                    self.assertEqual(trade["entry_index"], trade["decision_index"] + 2)
                    self.assertTrue(all(fill["index"] < end for fill in trade["exit_fills"]))

    def test_current_and_later_future_cannot_change_calibration(self):
        rows = market()
        baseline = self.evaluate(rows)
        for number, fold in enumerate(baseline["folds"]):
            start = fold["split"]["test_start"]
            changed = rows[:start] + [replace(r, bid_price=r.bid_price + 500,
                                               ask_price=r.ask_price + 1000, bid_qty=1, ask_qty=500)
                                      for r in rows[start:]]
            mutated = self.evaluate(changed)
            for name in SIGNALS:
                self.assertEqual(fold["strategies"][name]["train_calibration"],
                                 mutated["folds"][number]["strategies"][name]["train_calibration"])
            self.assertEqual(baseline["folds"][:number], mutated["folds"][:number])

    def test_mutating_or_appending_after_fold_end_cannot_change_that_fold(self):
        rows = market()
        baseline = self.evaluate(rows)
        changed = rows[:100] + [replace(r, bid_qty=999, ask_price=r.ask_price + 100) for r in rows[100:]]
        self.assertEqual(baseline["folds"][0], self.evaluate(changed)["folds"][0])
        self.assertEqual(baseline["folds"], self.evaluate(market(220))["folds"][:3])
        self.assertEqual(baseline, self.evaluate(rows))

    def test_constant_market_retains_losing_random_and_flat_zero_controls(self):
        rows = [Snapshot(i + 1, 100, 102, 100, 100) for i in range(180)]
        report = self.evaluate(rows)
        for fold in report["folds"]:
            self.assertLess(fold["strategies"]["random"]["execution"]["net_pnl_ticks_lots"], 0)
            flat = fold["strategies"]["flat"]["execution"]
            self.assertEqual((flat["net_pnl_ticks_lots"], flat["turnover_lots"], flat["terminal_inventory_lots"]), (0, 0, 0))
        self.assertNotIn("aggregate_pnl", report)

    def test_partial_terminal_inventory_is_retained_and_replay_is_reused(self):
        rows = [Snapshot(i + 1, 100, 102, 1 if i >= 90 else 100, 1 if i >= 90 else 100) for i in range(100)]
        report = evaluate_walk_forward(rows, Config(horizon=5, delay=1, threshold_quantile=0, order_size=50),
                                       initial_train=60, test_size=40)
        fold = report["folds"][0]
        sim = fold["strategies"]["random"]["execution"]
        self.assertNotEqual(sim["terminal_inventory_lots"], 0)
        self.assertIsNotNone(sim["open_trade"])
        self.assertNotIn("exit_index", sim["open_trade"])
        payload = replay_payload(fold)["strategies"]["random"]
        self.assertEqual(payload["terminalInventory"], str(sim["terminal_inventory_lots"]))
        self.assertTrue(all(fill[0] < 100 for fill in payload["fills"]))
        self.assertIn('id="replayData"', render_replay(fold))

    def test_rejects_invalid_schedule_or_insufficient_rows(self):
        for initial, size in [(True, 40), (60, 40.0), (26, 40), (60, 26), (-1, 40), (160, 40)]:
            with self.subTest(initial=initial, size=size), self.assertRaises(ValueError):
                evaluate_walk_forward(market(), self.config, initial_train=initial, test_size=size)
        with self.assertRaises(ValueError):
            evaluate_walk_forward(market(), replace(self.config, delay=0), initial_train=60, test_size=40)

    def test_public_evaluation_rejects_invalid_snapshots_before_helpers(self):
        cases = ({'bid_price': 1000}, {'ask_qty': 0}, {'bid_qty': True},
                 {'ask_price': 1000.0}, {'bid_price': 2 ** 63},
                 {'seq': 1}, {'seq': True}, {'seq': 2 ** 64}, None)
        for mutation in cases:
            with self.subTest(mutation=mutation), ExitStack() as stack:
                data = market()
                data[-1] = replace(data[-1], **mutation) if mutation is not None else object()
                spies = [stack.enter_context(patch.object(walk_forward, name, wraps=getattr(walk_forward, name)))
                         for name in ('feature_series', 'calibrate', 'predictive_metrics', 'simulate')]
                with self.assertRaises(ValueError):
                    self.evaluate(data)
                for spy in spies:
                    spy.assert_not_called()

    def test_quality_policy_preserves_book_errors_and_rejects_malformed_fields(self):
        policy = {'max_gap_seconds': 5, 'max_staleness_seconds': 1000,
                  'on_gap': 'mark', 'forward_fill_limit_seconds': 0}
        cases = (({'bid_price': 1000}, DataQualityError), ({'bid_qty': 0}, DataQualityError),
                 ({'ask_qty': -1}, DataQualityError), ({'ask_qty': True}, ValueError),
                 ({'ask_price': 1000.0}, ValueError), ({'bid_price': 2 ** 63}, ValueError),
                 ({'seq': 1}, ValueError), (None, ValueError))
        for mutation, error in cases:
            with self.subTest(mutation=mutation), ExitStack() as stack:
                data = market()
                data[70] = replace(data[70], **mutation) if mutation is not None else object()
                spies = [stack.enter_context(patch.object(walk_forward, name, wraps=getattr(walk_forward, name)))
                         for name in ('feature_series', 'calibrate', 'predictive_metrics', 'simulate')]
                with self.assertRaises(error) as raised:
                    evaluate_walk_forward(data, self.config, initial_train=60, test_size=40,
                                          quality_policy=policy, timestamps=list(range(len(data))))
                if error is DataQualityError:
                    self.assertIn('index 70', str(raised.exception))
                for spy in spies:
                    spy.assert_not_called()

    def test_cli_hash_outputs_and_existing_replay_preserve_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, output, page = root / "bbo.csv", root / "report.json", root / "fold.html"
            raw = ("seq,bid_price,ask_price,bid_qty,ask_qty\r\n" + "".join(
                f"{r.seq},{r.bid_price},{r.ask_price},{r.bid_qty},{r.ask_qty}\r\n" for r in market())).encode("utf-8-sig")
            source.write_bytes(raw)
            args = ["--input", str(source), "--output", str(output), "--html", str(page),
                    "--html-fold", "1", "--data-kind", "synthetic", "--initial-train", "60", "--test-size", "40"]
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 0)
            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(result["source"]["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(result["source"]["data_kind"], "synthetic")
            page_text = page.read_text(encoding="utf-8")
            self.assertIn('id="replayData"', page_text)
            self.assertIn("Walk-forward fold 1 (independent): test indices [100, 140)", page_text)
            self.assertIn("No fee/delay sensitivity scenarios were computed", page_text)
            self.assertNotIn("JSON includes delay scenarios", page_text)
            saved = output.read_bytes(), page.read_bytes()
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(args)
            self.assertEqual((output.read_bytes(), page.read_bytes()), saved)
            self.assertEqual(source.read_bytes(), raw)
            args[args.index("--output") + 1] = str(source)
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(args)
            self.assertEqual(source.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
