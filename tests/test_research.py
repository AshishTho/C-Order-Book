"""Economic and leakage invariants, not golden-file performance targets."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import hashlib
import json
from unittest.mock import patch
from pathlib import Path
import tempfile
import unittest

from research.generate import generate_events
from research.lab import (Calibration, Config, Snapshot, calibrate, evaluate,
                          feature_series, main, markout, read_snapshots, simulate, split_indices)


def market(count=200):
    return [Snapshot(i + 1, 100 + i // 3, 102 + i // 3,
                     20 + i % 11, 10 + i % 7) for i in range(count)]


class ResearchTests(unittest.TestCase):
    def test_constant_market_pays_spread_and_both_side_costs(self):
        rows = [Snapshot(i, 100, 102, 100, 100) for i in range(8)]
        calibration = Calibration(0, 1, 0, 0, 0, None)
        config = Config(horizon=2, delay=1, fee_ticks=0.25, slippage_ticks=0.5)
        values = [1] + [0] * 7
        result = simulate(rows, values, 0, calibration, config)
        self.assertEqual(result["completed_round_trips"], 1)
        self.assertAlmostEqual(result["net_pnl_ticks_lots"], -3.5)
        self.assertAlmostEqual(result["fees_ticks_lots"], 0.5)
        self.assertAlmostEqual(result["slippage_ticks_lots"], 1.0)
        self.assertEqual(result["trades"][0]["entry_index"], 1)
        self.assertEqual(result["trades"][0]["exit_index"], 3)

    def test_delayed_fill_uses_later_ask_not_signal_mid(self):
        rows = [Snapshot(i, 100 + 10 * i, 102 + 10 * i, 100, 100) for i in range(6)]
        config = Config(horizon=1, delay=2, fee_ticks=0)
        result = simulate(rows, [1, 0, 0, 0, 0, 0], 0,
                          Calibration(0, 1, 0, 0, 0, None), config)
        trade = result["trades"][0]
        self.assertEqual(trade["entry_price_ticks"], 122)
        self.assertEqual(trade["exit_fills"][0]["price_ticks"], 130)
        self.assertEqual(result["net_pnl_ticks_lots"], 8)

    def test_short_fills_bid_then_ask(self):
        rows = [Snapshot(i, 100 - 5 * i, 102 - 5 * i, 100, 100) for i in range(6)]
        result = simulate(rows, [-1, 0, 0, 0, 0, 0], 0,
                          Calibration(0, 1, 0, 0, 0, None),
                          Config(horizon=1, delay=1, fee_ticks=0))
        self.assertEqual(result["trades"][0]["entry_price_ticks"], 95)
        self.assertEqual(result["trades"][0]["exit_fills"][0]["price_ticks"], 92)
        self.assertEqual(result["net_pnl_ticks_lots"], 3)

    def test_large_absolute_price_preserves_small_pnl(self):
        base = 2**60
        rows = [Snapshot(i, base + 10 * i, base + 10 * i + 2, 100, 100) for i in range(6)]
        config = Config(horizon=1, delay=1, fee_ticks=0.1)
        self.assertEqual(markout(rows, 0, config), 10)
        result = simulate(rows, [1, 0, 0, 0, 0, 0], 0,
                          Calibration(0, 1, 0, 0, 0, None), config)
        self.assertAlmostEqual(result["net_pnl_ticks_lots"], 7.8)
        self.assertAlmostEqual(result["trades"][0]["net_pnl_ticks_lots"], 7.8)

    def test_depth_caps_entry_and_partial_exit_carries_forward(self):
        rows = [Snapshot(i, 100, 102, 1, 3) for i in range(8)]
        result = simulate(rows, [1] + [0] * 7, 0,
                          Calibration(0, 1, 0, 0, 0, None),
                          Config(horizon=1, delay=1, fee_ticks=0, order_size=10))
        trade = result["trades"][0]
        self.assertEqual(trade["quantity"], 3)
        self.assertEqual([x["index"] for x in trade["exit_fills"]], [2, 3, 4])
        self.assertEqual(result["maximum_absolute_inventory_lots"], 3)
        self.assertEqual(result["net_pnl_ticks_lots"], -6)

    def test_unliquidated_inventory_is_reported_not_forged_as_filled(self):
        rows = [Snapshot(i, 100, 102, 1, 10) for i in range(4)]
        result = simulate(rows, [1, 0, 0, 0], 0,
                          Calibration(0, 1, 0, 0, 0, None),
                          Config(horizon=1, delay=1, fee_ticks=0, order_size=10))
        self.assertEqual(result["terminal_inventory_lots"], 8)
        self.assertEqual(result["completed_round_trips"], 0)
        self.assertEqual(result["turnover_lots"], 12)
        self.assertEqual(result["open_trade"]["quantity"], 10)
        self.assertEqual(len(result["open_trade"]["exit_fills"]), 2)

    def test_training_labels_do_not_reach_test(self):
        rows = market()
        config = Config(horizon=5, delay=2)
        stop, split = split_indices(len(rows), config)
        self.assertEqual(stop - 1 + config.delay + config.horizon, split - 1)
        features = feature_series(rows)["imbalance"]
        original = calibrate(features, rows, stop, config, "imbalance")
        altered = rows[:split] + [replace(row, bid_price=row.bid_price + 5000,
                                           ask_price=row.ask_price + 5000,
                                           bid_qty=99999, ask_qty=1) for row in rows[split:]]
        changed = calibrate(feature_series(altered)["imbalance"], altered, stop, config, "imbalance")
        self.assertEqual(original, changed)

    def test_future_changes_cannot_change_past_features(self):
        rows = market()
        altered = rows[:100] + [replace(row, bid_qty=999999) for row in rows[100:]]
        before, after = feature_series(rows), feature_series(altered)
        for name in before:
            self.assertEqual(before[name][:100], after[name][:100])

    def test_ofi_matches_hand_calculation(self):
        rows = [Snapshot(1, 100, 102, 10, 20), Snapshot(2, 100, 102, 15, 18),
                Snapshot(3, 101, 103, 12, 14)]
        values = feature_series(rows)
        self.assertAlmostEqual(values["ofi"][1], 7 / 33)
        self.assertAlmostEqual(values["ofi"][2], 30 / 26)
        self.assertAlmostEqual(values["microprice"][1], values["imbalance"][1])

    def test_cost_sensitivity_preserves_fills_and_accounts_exactly(self):
        rows = market()
        calibration = Calibration(0, 1, 0, 0, 0, None)
        config = Config(horizon=3, delay=1, fee_ticks=0)
        zero = simulate(rows, [1] * len(rows), 100, calibration, config)
        costly = simulate(rows, [1] * len(rows), 100, calibration, replace(config, fee_ticks=0.3))
        self.assertEqual(zero["turnover_lots"], costly["turnover_lots"])
        self.assertEqual(zero["completed_round_trips"], costly["completed_round_trips"])
        self.assertAlmostEqual(zero["net_pnl_ticks_lots"] - costly["net_pnl_ticks_lots"],
                               costly["turnover_lots"] * 0.3)

    def test_flat_null_is_zero_and_evaluation_is_deterministic(self):
        config = Config(horizon=3, delay=1)
        report = evaluate(market(), config)
        self.assertEqual(report, evaluate(market(), config))
        flat = report["strategies"]["flat"]["execution"]
        for key in ("net_pnl_ticks_lots", "turnover_lots", "max_drawdown_ticks_lots"):
            self.assertEqual(flat[key], 0)

    def test_rejects_bad_input(self):
        invalid = ("1,100,99,1,1", "1,100,101,0,1", "1,100.5,101,1,1",
                   "1,100,101,1,1\n1,100,101,1,1", "1,nan,101,1,1")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.csv"
            for data in invalid:
                with self.subTest(data=data):
                    path.write_text("seq,bid_price,ask_price,bid_qty,ask_qty\n" + data, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        read_snapshots(path)

    def test_rejects_zero_delay_and_nonfinite_costs(self):
        for config in (Config(delay=0), Config(fee_ticks=float("nan")),
                       Config(slippage_ticks=-1), Config(horizon=0)):
            with self.subTest(config=config), self.assertRaises(ValueError):
                config.validate()

    def test_generator_lifecycle_and_reproducibility(self):
        events = list(generate_events(2000, 9))
        self.assertEqual(events, list(generate_events(2000, 9)))
        self.assertNotEqual(events, list(generate_events(2000, 10)))
        live, seen = {}, set()
        for index, event in enumerate(events):
            self.assertEqual(event["seq"], index + 1)
            reference, quantity = event["order_ref"], event["qty"]
            if event["type"] == "A":
                self.assertNotIn(reference, seen)
                seen.add(reference)
                live[reference] = dict(event)
            elif event["type"] == "D":
                self.assertEqual((event["side"], event["price"], event["qty"]), ("-", 0, 0))
                self.assertIn(reference, live)
                del live[reference]
            else:
                self.assertEqual((event["side"], event["price"]), ("-", 0))
                self.assertIn(reference, live)
                self.assertGreater(quantity, 0)
                self.assertLessEqual(quantity, live[reference]["qty"])
                live[reference]["qty"] -= quantity
                if live[reference]["qty"] == 0:
                    del live[reference]
            if index:
                bids = [x["price"] for x in live.values() if x["side"] == "B"]
                asks = [x["price"] for x in live.values() if x["side"] == "S"]
                self.assertLess(max(bids), min(asks))


class ResearchOutputTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "bbo.csv"
        self.source.write_text("seq,bid_price,ask_price,bid_qty,ask_qty\n" + "".join(
            f"{row.seq},{row.bid_price},{row.ask_price},{row.bid_qty},{row.ask_qty}\n"
            for row in market()), encoding="utf-8")
        self.source_bytes = self.source.read_bytes()

    def run_cli(self, output, html=None):
        args = ["--input", str(self.source), "--output", str(output), "--data-kind", "synthetic"]
        if html is not None:
            args.extend(["--html", str(html)])
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return main(args)

    def test_source_hash_describes_analyzed_bytes_when_input_changes_during_evaluation(self):
        output = self.root / "report.json"

        def replace_source(rows, config, **kwargs):
            self.assertEqual(rows, market())
            self.source.write_bytes(self.source_bytes.replace(b",20,10", b",30,10"))
            return evaluate(rows, config, **kwargs)

        with patch("research.lab.evaluate", side_effect=replace_source):
            self.assertEqual(self.run_cli(output), 0)
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["source"]["sha256"], hashlib.sha256(self.source_bytes).hexdigest())
        self.assertNotEqual(report["source"]["sha256"], hashlib.sha256(self.source.read_bytes()).hexdigest())

    def test_source_hash_preserves_original_bom_and_line_endings(self):
        payload = b"\xef\xbb\xbf" + self.source_bytes.replace(b"\n", b"\r\n")
        self.source.write_bytes(payload)
        output = self.root / "report.json"
        self.assertEqual(self.run_cli(output), 0)
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["source"]["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(report["split"]["total_observations"], len(market()))
        self.assertEqual(report["strategies"], evaluate(market(), Config())["strategies"])

    def test_aliasing_input_or_outputs_is_rejected_before_writing(self):
        output = self.root / "report.json"
        for target, html in [(self.source, None), (output, self.source), (output, output),
                             (self.root / "." / "bbo.csv", None)]:
            with self.subTest(target=target, html=html), self.assertRaises(SystemExit) as caught:
                self.run_cli(target, html)
            self.assertEqual(caught.exception.code, 2)
            self.assertEqual(self.source.read_bytes(), self.source_bytes)
            self.assertFalse(output.exists())

    def test_existing_json_or_html_preserves_all_evidence(self):
        for existing_name in ("json", "html"):
            with self.subTest(existing=existing_name):
                output = self.root / (existing_name + ".json")
                html = self.root / (existing_name + ".html")
                existing = output if existing_name == "json" else html
                untouched = html if existing_name == "json" else output
                existing.write_bytes(b"previous evidence")
                with self.assertRaises(SystemExit):
                    self.run_cli(output, html)
                self.assertEqual(existing.read_bytes(), b"previous evidence")
                self.assertFalse(untouched.exists())
                self.assertEqual(self.source.read_bytes(), self.source_bytes)

    def test_fresh_reports_are_written_and_a_repeat_is_rejected(self):
        output, html = self.root / "new" / "report.json", self.root / "new" / "report.html"
        self.assertEqual(self.run_cli(output, html), 0)
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["split"]["total_observations"], 200)
        self.assertIn("<html", html.read_text(encoding="utf-8"))
        before = output.read_bytes(), html.read_bytes()
        with self.assertRaises(SystemExit):
            self.run_cli(output, html)
        self.assertEqual((output.read_bytes(), html.read_bytes()), before)
        self.assertEqual(self.source.read_bytes(), self.source_bytes)

    def test_file_created_during_evaluation_is_not_overwritten(self):
        output = self.root / "report.json"
        def competing_writer(*args, **kwargs):
            output.write_bytes(b"concurrent evidence")
            return evaluate(*args, **kwargs)
        with patch("research.lab.evaluate", side_effect=competing_writer), self.assertRaises(SystemExit):
            self.run_cli(output)
        self.assertEqual(output.read_bytes(), b"concurrent evidence")
        self.assertEqual(self.source.read_bytes(), self.source_bytes)


if __name__ == "__main__":
    unittest.main()
