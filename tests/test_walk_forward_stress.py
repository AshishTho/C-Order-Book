import copy
import dataclasses
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from research import stress_walk_forward as wf
from research.lab import Config, SIGNALS, Snapshot

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('run_walk_forward', ROOT / 'scripts' / 'run_walk_forward.py')
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

FOLDS = [{'test_start': 60, 'test_stop': 100}, {'test_start': 100, 'test_stop': 140}]
HOLDOUT = 160
GRIDS = dict(fee_grid=(0, 0.5, 1.0), delay_grid=(1, 2), spread_grid=(0, 1), depth_grid=(1.0, 0.25))
NET = 'net_pnl_ticks_lots'


def market():
    return [Snapshot(i + 1, 100 + i // 3, 102 + i // 3, 20 + i % 11, 10 + i % 7) for i in range(200)]


def constant_book():
    return [Snapshot(i + 1, 100, 102, 20, 10) for i in range(200)]


def run(rows=None, config=None, folds=FOLDS, holdout=HOLDOUT, **kw):
    return wf.evaluate_walk_forward(market() if rows is None else rows, config or Config(),
                                    copy.deepcopy(folds), holdout, **{**GRIDS, **kw})


class WalkForwardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = run()

    def test_later_and_reserved_mutations_preserve_earlier_fold(self):
        rows = market()
        rows[120] = dataclasses.replace(rows[120], bid_qty=999, ask_price=rows[120].ask_price + 7)
        rows[180] = dataclasses.replace(rows[180], bid_price=1, ask_qty=1)
        self.assertEqual(run(rows)['folds'][0], self.base['folds'][0])
        only_suffix = market()
        only_suffix[190] = dataclasses.replace(only_suffix[190], ask_qty=500)
        self.assertEqual(run(only_suffix), self.base)

    def test_malformed_folds_and_grids_rejected(self):
        bad_folds = [[], [{'test_start': 60}], [{'test_start': 60, 'test_stop': 100, 'x': 1}],
                     [{'test_start': True, 'test_stop': 100}], [{'test_start': 80, 'test_stop': 80}],
                     [{'test_start': 60, 'test_stop': 100}, {'test_start': 90, 'test_stop': 130}],
                     [{'test_start': 100, 'test_stop': 170}], [{'test_start': 25, 'test_stop': 60}]]
        for folds in bad_folds:
            with self.subTest(folds=folds), self.assertRaises(ValueError):
                run(folds=folds)
        bad_grids = [dict(fee_grid=(-1,)), dict(fee_grid=(float('nan'),)), dict(delay_grid=(0,)),
                     dict(delay_grid=(1.5,)), dict(spread_grid=(-1,)), dict(depth_grid=(0.0,)),
                     dict(depth_grid=(1.5,)), dict(fee_grid=())]
        for grid in bad_grids:
            with self.subTest(grid=grid), self.assertRaises(ValueError):
                run(**grid)
        for holdout in (0, 200, True):
            with self.subTest(holdout=holdout), self.assertRaises(ValueError):
                run(holdout=holdout)

    def test_reproducible_seeded_prefix_and_input_immutability(self):
        rows, folds = market(), copy.deepcopy(FOLDS)
        before = (list(rows), copy.deepcopy(folds))
        again = wf.evaluate_walk_forward(rows, Config(), folds, HOLDOUT, **GRIDS)
        self.assertEqual((rows, folds), before)
        self.assertEqual(again, self.base)
        reseeded = run(config=Config(seed=7))
        self.assertNotEqual(reseeded['folds'][0]['reproduction_sha256'],
                            self.base['folds'][0]['reproduction_sha256'])
        early = market()
        early[5] = dataclasses.replace(early[5], bid_qty=77)
        self.assertNotEqual(run(early)['folds'][0]['reproduction_sha256'],
                            self.base['folds'][0]['reproduction_sha256'])

    def test_all_signals_and_every_stress_case_retained(self):
        expected = [('base', None)] + [(d, v) for d, g in (
            ('fee_ticks', GRIDS['fee_grid']), ('delay_observations', GRIDS['delay_grid']),
            ('spread_widening_ticks', GRIDS['spread_grid']), ('depth_fraction', GRIDS['depth_grid'])) for v in g]
        for fold in self.base['folds']:
            self.assertEqual(set(fold['signals']), set(SIGNALS))
            self.assertTrue({'imbalance', 'microprice', 'ofi', 'random', 'flat'} <= set(SIGNALS))
            for sig in fold['signals'].values():
                got = [(c['dimension'], c['value']) for c in sig['stress_cases']]
                self.assertEqual(got, expected)
                for c in sig['stress_cases']:
                    self.assertEqual(set(c['summary']), set(wf._SUMMARY_KEYS))

    def test_frozen_calibration_shared_across_real_simulator_calls(self):
        events = []

        def cal(*a, **k):
            out = real_cal(*a, **k)
            events.append(('cal', out))
            return out

        def sim(*a, **k):
            events.append(('sim', a[3]))
            return real_sim(*a, **k)

        real_cal, real_sim = wf.calibrate, wf.simulate
        with mock.patch.object(wf, 'calibrate', side_effect=cal), mock.patch.object(wf, 'simulate', side_effect=sim):
            run()
        per_signal = 1 + sum(len(g) for g in GRIDS.values())
        self.assertEqual(sum(e[0] == 'cal' for e in events), len(FOLDS) * len(SIGNALS))
        current = None
        for kind, obj in events:
            if kind == 'cal':
                current, count = obj, 0
            else:
                self.assertIs(obj, current)
                count += 1
                self.assertLessEqual(count, per_signal)

    def test_training_labels_end_before_test_start(self):
        cfg = Config()
        for fold in self.base['folds']:
            self.assertLess(fold['latest_train_label_index'], fold['test_start'])
            self.assertEqual(fold['train_stop'], fold['test_start'] - cfg.delay - cfg.horizon)
            self.assertLessEqual(fold['predictive_decision_stop'], fold['test_stop'])

    def test_constant_book_costs_monotonic(self):
        res = run(constant_book(), fee_grid=(0, 0.5, 1.0, 2.0), spread_grid=(0, 1, 2))
        for fold in res['folds']:
            for sig in fold['signals'].values():
                for dim in ('fee_ticks', 'spread_widening_ticks'):
                    pnl = [c['summary'][NET] for c in sig['stress_cases'] if c['dimension'] == dim]
                    if None not in pnl:
                        self.assertEqual(pnl, sorted(pnl, reverse=True))
                        self.assertLessEqual(pnl[0], 0)

    def test_shallow_depth_reports_terminal_inventory_without_forced_flat(self):
        rows = [Snapshot(i + 1, 100, 102, 40, 40) for i in range(26)]
        rows[24] = Snapshot(25, 100, 102, 10, 10)
        cfg = Config(horizon=1, delay=1, order_size=10, threshold_quantile=0,
                     fee_ticks=0, seed=1)
        result = run(rows, cfg, folds=[{'test_start': 22, 'test_stop': 25}], holdout=25,
                     fee_grid=(0,), delay_grid=(1,), spread_grid=(0,), depth_grid=(1, .25))
        sig = result['folds'][0]['signals']['random']
        self.assertEqual(sig['base_simulation']['terminal_inventory_lots'], 0)
        stress = next(c['summary'] for c in sig['stress_cases']
                      if c['dimension'] == 'depth_fraction' and c['value'] == .25)
        self.assertEqual(stress['terminal_inventory_lots'], 8)
        self.assertEqual(stress['completed_round_trips'], 0)
        self.assertEqual(stress['turnover_lots'], 12)
        self.assertEqual(stress['net_pnl_ticks_lots'], -12)
        self.assertEqual(stress['terminal_liquidation_mark_ticks_lots'], -20)
        self.assertIsNotNone(stress['open_trade'])

    def test_large_integer_depth_is_never_rounded_up(self):
        qty = 9007199254740995
        rows = [Snapshot(1, 100, 102, qty, qty)]
        for widening in (0, 1):
            stressed = wf._stress_rows(rows, 0, 1, widen=widening, depth=1.0)[0]
            self.assertEqual((stressed.bid_qty, stressed.ask_qty), (qty, qty))
        half = wf._stress_rows(rows, 0, 1, depth=0.5)[0]
        self.assertEqual((half.bid_qty, half.ask_qty), (qty // 2, qty // 2))


    def test_decimal_depth_floor_min_one_and_prefix_unchanged(self):
        prefix = Snapshot(1, 100, 102, 7, 7)
        target = Snapshot(2, 100, 102, 10, 1)
        tail = Snapshot(3, 100, 102, 5, 5)
        rows = [prefix, target, tail]
        original = list(rows)
        out = wf._stress_rows(rows, 1, 2, widen=1, depth=.3)
        self.assertEqual(len(out), 2)
        self.assertIs(out[0], prefix)
        self.assertEqual(out[0], Snapshot(1, 100, 102, 7, 7))
        stressed = out[1]
        self.assertEqual((stressed.bid_price, stressed.ask_price), (99, 103))
        self.assertEqual(stressed.bid_qty, 3)
        self.assertEqual(stressed.ask_qty, 1)
        self.assertIsInstance(stressed.bid_qty, int)
        self.assertEqual(rows, original)
        self.assertEqual(target, Snapshot(2, 100, 102, 10, 1))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.plan = self.dir / 'plan.json'
        self.out = self.dir / 'out.json'
        self.write_plan({'config': {}, 'folds': FOLDS[:1], 'final_holdout_start': HOLDOUT,
                         'fee_grid': [0], 'delay_grid': [1], 'spread_grid': [0], 'depth_grid': [1.0]})
        self.reader = mock.patch.object(cli, 'read_snapshot_source', return_value=(market(), 'ab' * 32))
        self.reader.start()

    def tearDown(self):
        self.reader.stop()
        self.tmp.cleanup()

    def write_plan(self, plan):
        self.plan.write_text(json.dumps(plan), encoding='utf-8')

    def main(self):
        with mock.patch('sys.stderr'):
            return cli.main(['--input', str(self.dir / 'in.csv'), '--plan', str(self.plan), '--output', str(self.out)])

    def test_success_writes_strict_json_with_sha(self):
        self.assertEqual(self.main(), 0)
        data = json.loads(self.out.read_text(encoding='utf-8'), parse_constant=self.fail)
        self.assertEqual(data['input_sha256'], 'ab' * 32)
        self.assertEqual(data['schema_version'], wf.SCHEMA_VERSION)

    def test_existing_output_preserved(self):
        self.out.write_bytes(b'keep me')
        self.assertEqual(self.main(), 2)
        self.assertEqual(self.out.read_bytes(), b'keep me')

    def test_malformed_plans_rejected(self):
        for plan in ({'config': {}, 'folds': FOLDS, 'final_holdout_start': HOLDOUT, 'extra': 1},
                     {'config': {}, 'folds': FOLDS}, {'config': {'bogus': 1}, 'folds': FOLDS, 'final_holdout_start': HOLDOUT},
                     {'config': [], 'folds': FOLDS, 'final_holdout_start': HOLDOUT},
                     {'config': {}, 'folds': FOLDS, 'final_holdout_start': HOLDOUT, 'fee_grid': 1}, []):
            with self.subTest(plan=plan):
                self.write_plan(plan)
                self.assertEqual(self.main(), 2)
                self.assertFalse(self.out.exists())


if __name__ == '__main__':
    unittest.main()
