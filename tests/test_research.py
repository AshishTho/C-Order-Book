"""Economic and leakage invariants, not golden-file performance targets."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, replace
import io
import hashlib
import json
import re
from unittest.mock import patch
from pathlib import Path
import tempfile
import unittest

from research.generate import generate_events
from research.lab import (Calibration, Config, Snapshot, calibrate, evaluate,
                          feature_series, main, markout, read_snapshots, simulate, split_indices)
from research.replay_viewer import replay_payload, render_replay


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


class ReplayViewerTests(unittest.TestCase):
    def report(self, rows=None, config=None):
        rows = rows or [Snapshot(i, 100, 102, 100, 100) for i in range(8)]
        config = config or Config(horizon=2, delay=1, fee_ticks=0.25, slippage_ticks=0.5)
        execution = simulate(rows, [1] + [0] * (len(rows) - 1), 0,
                             Calibration(0, 1, 0, 0, 0, None), config)
        return {"config": asdict(config), "split": {"test_start": 0},
                "units": {"pnl": "ticks * lots"},
                "source": {"path": "synthetic.csv", "sha256": "a" * 64, "data_kind": "synthetic"},
                "strategies": {"constant": {"execution": execution}}}

    def test_recorded_fills_costs_and_negative_outcome_reconcile_without_mutation(self):
        report = self.report()
        before = json.dumps(report, sort_keys=True)
        payload = replay_payload(report)
        strategy = payload["strategies"]["constant"]
        self.assertEqual([(f[0], f[2], f[3], f[4], f[5]) for f in strategy["fills"]],
                         [(1, "Entry", "Buy", "102", "1"), (3, "Exit", "Sell", "100", "1")])
        self.assertEqual(sum(f[6] for f in strategy["fills"]), strategy["totalFees"])
        self.assertEqual(sum(f[7] for f in strategy["fills"]), strategy["totalSlippage"])
        self.assertEqual(strategy["fills"][0][9], None)  # No future trade outcome at entry.
        self.assertEqual(strategy["fills"][-1][9], -3.5)
        self.assertEqual(strategy["realizedPnl"], -3.5)
        self.assertEqual(strategy["points"][-1][2], -3.5)
        self.assertEqual(json.dumps(report, sort_keys=True), before)

    def test_partial_terminal_position_is_visible_without_inventing_a_closing_fill(self):
        rows = [Snapshot(i, 100, 102, 1, 5) for i in range(5)]
        report = self.report(rows, Config(horizon=2, delay=1, order_size=5, fee_ticks=0.25))
        execution = report["strategies"]["constant"]["execution"]
        self.assertIsNotNone(execution["open_trade"])
        strategy = replay_payload(report)["strategies"]["constant"]
        self.assertEqual(strategy["terminalInventory"], "3")
        self.assertEqual([f[5] for f in strategy["fills"]], ["5", "1", "1"])
        self.assertEqual(sum(f[6] for f in strategy["fills"]), execution["fees_ticks_lots"])
        self.assertTrue(all(f[9] is None for f in strategy["fills"]))
        self.assertEqual(len(strategy["fills"]), 1 + len(execution["open_trade"]["exit_fills"]))

    def test_large_integer_prices_and_sequences_survive_browser_json_exactly(self):
        offset = 10 ** 18
        report = self.report([Snapshot(offset + i, offset + 100, offset + 102, 100, 100)
                              for i in range(8)])
        strategy = replay_payload(report)["strategies"]["constant"]
        self.assertEqual(strategy["fills"][0][4], str(offset + 102))
        self.assertEqual(strategy["fills"][0][1], str(offset + 1))
        self.assertEqual(strategy["points"][0][1], str(offset))

    def test_embedded_provenance_cannot_escape_json_script_and_flat_control_is_retained(self):
        report = self.report()
        report["source"]["path"] = '</script><script>window.injected=true</script>&'
        fragment = render_replay(report)
        encoded = re.search(r'<script id="replayData" type="application/json">(.*?)</script>',
                            fragment, re.S).group(1)
        self.assertNotIn("<", encoded)
        self.assertEqual(json.loads(encoded)["source"], report["source"])
        self.assertNotIn("<script>window.injected", fragment)
        flat = evaluate(market(), Config())["strategies"]["flat"]
        report["strategies"]["flat"] = flat
        self.assertEqual(replay_payload(report)["strategies"]["flat"]["fills"], [])
        self.assertEqual(replay_payload(report)["strategies"]["flat"]["realizedPnl"], 0)


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
        self.assertIn('id="replayData"', html.read_text(encoding="utf-8"))
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
