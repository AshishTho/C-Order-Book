import unittest

from research.preflight import SIGNALS, validate_preflight

from research.lab import Config, Snapshot

N, HOLD, FOLD = 100, 80, {'test_start': 40, 'test_stop': 60}


def rows(seqs=None):
    return [Snapshot(s, 100, 101, 5, 5) for s in (seqs if seqs is not None else range(N))]


def feats(clock='seq', n=60, shift=0):
    sig = {k: {'values': [float(i) for i in range(n)],
               'available_at': [i + shift for i in range(n)]} for k in SIGNALS}
    return {'clock': clock, 'signals': sig}


def run(r=None, folds=None, delays=(2,), **kw):
    return validate_preflight(r or rows(), Config(), folds or [dict(FOLD)], HOLD, delays, **kw)


class PreflightTest(unittest.TestCase):
    def test_clean_default(self):
        out = run()
        self.assertEqual(set(out), {'folds', 'clock', 'markers', 'features', 'availability'})
        self.assertEqual(out['clock'], 'seq')
        self.assertEqual(out['markers'], tuple(range(N)))
        self.assertIsNone(out['features'])
        self.assertEqual(out['folds'], ({'test_start': 40, 'test_stop': 60},))

    def test_supplied_values_copied_unchanged(self):
        fd = feats()
        out = run(feature_data=fd)
        self.assertEqual(out['features']['ofi'], tuple(fd['signals']['ofi']['values']))
        fd['signals']['ofi']['values'][0] = 99.0
        self.assertEqual(out['features']['ofi'][0], 0.0)
        ts = list(range(0, 10 * N, 10))
        out = run(timestamps=ts, feature_data=feats('timestamp'))
        self.assertEqual(out['clock'], 'timestamp')

    def test_bad_rows_and_timestamps(self):
        cases = {'dup': dict(r=rows([0, 0] + list(range(2, N)))),
                 'rev': dict(r=rows(range(N, 0, -1))),
                 'neg': dict(r=rows(range(-1, N - 1))),
                 'ts_dup': dict(timestamps=[0] * N),
                 'ts_bool': dict(timestamps=[True] + list(range(1, N))),
                 'ts_len': dict(timestamps=list(range(N - 1))),
                 'delay_bool': dict(delays=(True,)),
                 'delay_empty': dict(delays=())}
        for name, kw in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                run(**kw)
        with self.assertRaises(ValueError):
            validate_preflight(rows(), Config(), [FOLD], True, (2,))
        with self.assertRaises(ValueError):
            validate_preflight(rows(), Config(delay=0), [FOLD], HOLD, (2,))

    def test_bad_features(self):
        def mut(fn):
            fd = feats()
            fn(fd)
            return fd
        cases = {'future': feats(shift=1),
                 'clock': feats('timestamp'),
                 'extra_key': mut(lambda d: d.update(x=1)),
                 'missing_sig': mut(lambda d: d['signals'].pop('flat')),
                 'missing_avail': mut(lambda d: d['signals']['ofi'].pop('available_at')),
                 'length': feats(n=N),
                 'nan': mut(lambda d: d['signals']['ofi']['values'].__setitem__(3, float('nan'))),
                 'bool': mut(lambda d: d['signals']['ofi']['values'].__setitem__(3, True)),
                 'huge': mut(lambda d: d['signals']['ofi']['values'].__setitem__(3, 10 ** 400))}
        for name, fd in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                run(feature_data=fd)

    def test_bad_folds(self):
        cases = {'overlap': [FOLD, {'test_start': 55, 'test_stop': 75}],
                 'later_window': [FOLD, {'test_start': 60, 'test_stop': 70}],
                 'short_train': [{'test_start': 30, 'test_stop': 60}],
                 'past_holdout': [{'test_start': 40, 'test_stop': 81}],
                 'override': [dict(FOLD, train_stop=40)],
                 'bool': [{'test_start': 40, 'test_stop': True}],
                 'empty': []}
        for name, folds in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                validate_preflight(rows(), Config(), folds, HOLD, (5,))


from unittest import mock

from research import stress_walk_forward as swf

SPIED = ('feature_series', 'calibrate', 'predictive_metrics', 'simulate')


def ev(r=None, folds=None, **kw):
    return swf.evaluate_walk_forward(r or rows(), Config(), folds or [dict(FOLD)], HOLD,
                                     delay_grid=(2,), **kw)


def digests(obj):
    if isinstance(obj, dict):
        return [v for k, v in obj.items() if k == 'reproduction_sha256'] + \
            [d for v in obj.values() for d in digests(v)]
    if isinstance(obj, (list, tuple)):
        return [d for v in obj for d in digests(v)]
    return []


class StressIntegration(unittest.TestCase):
    def assert_no_eval(self, **kw):
        mocks = {n: mock.patch.object(swf, n, wraps=getattr(swf, n)) for n in SPIED}
        spies = {n: m.start() for n, m in mocks.items()}
        try:
            with self.assertRaises(ValueError):
                ev(**kw)
        finally:
            mock.patch.stopall()
        for n, s in spies.items():
            self.assertEqual(s.call_count, 0, n)

    def test_invalid_seq_stops_before_eval(self):
        self.assert_no_eval(r=rows([0] + list(range(N - 1))))

    def test_invalid_later_fold_stops_before_eval(self):
        self.assert_no_eval(folds=[dict(FOLD), {'test_start': 60, 'test_stop': 70}])

    def test_future_values_truthful_future_availability(self):
        f = feats()
        for s in f['signals'].values():
            s['values'] = [float(i + 1) for i in range(60)]
            s['available_at'] = [i + 1 for i in range(60)]
        self.assert_no_eval(feature_data=f)

    def test_invalid_timestamps(self):
        ts = list(range(N))
        ts[5] = -1
        self.assert_no_eval(timestamps=ts)

    def test_supplied_features_reach_calibrate(self):
        with mock.patch.object(swf, 'calibrate', wraps=swf.calibrate) as c:
            ev()
            default_args = c.call_args_list[:]
        with mock.patch.object(swf, 'calibrate', wraps=swf.calibrate) as c:
            ev(feature_data=feats())
            custom_args = c.call_args_list[:]
        self.assertTrue(custom_args)
        self.assertNotEqual(repr(default_args), repr(custom_args))

    def test_fingerprints_distinguish_values_and_timing(self):
        a = feats()
        b = feats()
        for s in b['signals'].values():
            s['values'] = [v + 1.0 for v in s['values']]
        c = feats()
        for s in c['signals'].values():
            s['available_at'] = [max(i - 1, 0) for i in range(60)]
        da, db, dc = (digests(ev(feature_data=x)) for x in (a, b, c))
        self.assertEqual(da, digests(ev(feature_data=feats())))
        self.assertNotEqual(da, db)
        self.assertNotEqual(da, dc)
        self.assertNotEqual(da, digests(ev()))


if __name__ == '__main__':
    unittest.main()
