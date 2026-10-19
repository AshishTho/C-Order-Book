# Expanding-window walk-forward evaluation with a reserved final suffix.
# Offline research diagnostics only: stdlib, deterministic, no live trading.
import dataclasses
import hashlib
import json
import math
from numbers import Real

from research.lab import SIGNALS, Snapshot, calibrate, feature_series, predictive_metrics, simulate
from research.preflight import validate_preflight

SCHEMA_VERSION = 'walk_forward.v1'
MIN_TRAIN_DECISIONS = 20
_FOLD_KEYS = frozenset(('test_start', 'test_stop'))
_SUMMARY_KEYS = (
    'net_pnl_ticks_lots', 'realized_round_trip_pnl_ticks_lots', 'fees_ticks_lots',
    'slippage_ticks_lots', 'turnover_lots', 'completed_round_trips', 'max_drawdown_ticks_lots',
    'terminal_inventory_lots', 'terminal_liquidation_mark_ticks_lots', 'open_trade',
)


def _index(name, value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f'{name} must be an int, got {value!r}')
    return value


def _real(name, value):
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        raise ValueError(f'{name} must be a finite real number, got {value!r}')
    return float(value)


def _json_safe(obj):
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        obj = dataclasses.asdict(obj)
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def _grids(fee_grid, delay_grid, spread_grid, depth_grid):
    grids = {
        'fee_ticks': tuple(_real('fee', f) for f in fee_grid),
        'delay_observations': tuple(_index('delay', d) for d in delay_grid),
        'spread_widening_ticks': tuple(_index('spread widening', s) for s in spread_grid),
        'depth_fraction': tuple(_real('depth fraction', f) for f in depth_grid),
    }
    if not all(grids.values()):
        raise ValueError('stress grids must be non-empty')
    if any(f < 0 for f in grids['fee_ticks']):
        raise ValueError('fees must be nonnegative')
    if any(d <= 0 for d in grids['delay_observations']):
        raise ValueError('delays must be positive')
    if any(s < 0 for s in grids['spread_widening_ticks']):
        raise ValueError('spread widening must be nonnegative')
    if any(not 0 < f <= 1 for f in grids['depth_fraction']):
        raise ValueError('depth fractions must satisfy 0 < f <= 1')
    return grids


def _validate_folds(folds, holdout):
    if not isinstance(folds, (list, tuple)) or not folds:
        raise ValueError('folds must be a non-empty list')
    out, prev_stop = [], None
    for k, fold in enumerate(folds):
        if not isinstance(fold, dict) or set(fold) != _FOLD_KEYS:
            raise ValueError(f'fold {k} must have exactly keys test_start, test_stop')
        start = _index(f'fold {k} test_start', fold['test_start'])
        stop = _index(f'fold {k} test_stop', fold['test_stop'])
        if not 0 <= start < stop:
            raise ValueError(f'fold {k} is empty or negative')
        if prev_stop is not None and start < prev_stop:
            raise ValueError(f'fold {k} overlaps or is out of order')
        if stop > holdout:
            raise ValueError(f'fold {k} enters reserved suffix at {holdout}')
        out.append({'test_start': start, 'test_stop': stop})
        prev_stop = stop
    return out


def _widened_quotes(row, widen, index):
    bid, ask = row.bid_price - widen, row.ask_price + widen
    if (type(bid) is not int or type(ask) is not int
            or not 0 < bid < ask < 2 ** 63):
        raise ValueError(f'spread widening {widen} requires positive int64 bid < ask at row {index}')
    return bid, ask


def _stress_rows(rows, start, stop, widen=0, depth=1.0):
    from fractions import Fraction
    # Exact rational from the shortest decimal repr, e.g. 0.3 -> 3/10, 1.0 -> 1.
    scale = Fraction(str(depth))
    num, den = scale.numerator, scale.denominator
    out = list(rows[:start])
    for i in range(start, stop):
        r = rows[i]
        bid, ask = _widened_quotes(r, widen, i)
        out.append(dataclasses.replace(
            r, bid_price=bid, ask_price=ask,
            bid_qty=max(1, r.bid_qty * num // den),
            ask_qty=max(1, r.ask_qty * num // den)))
    return out


def _summary(sim):
    return {k: sim.get(k) for k in _SUMMARY_KEYS}


def _digest(prefix, config, fold, grids, inputs=None):
    payload = {'rows': [dataclasses.asdict(r) for r in prefix], 'config': dataclasses.asdict(config),
               'fold': fold, 'scenarios': grids}
    if inputs is not None:  # only custom runs; keeps default digests byte-identical
        payload['inputs'] = inputs
    text = json.dumps(_json_safe(payload), sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def evaluate_walk_forward(rows, config, folds, final_holdout_start, *, fee_grid=(0, 0.5),
                          delay_grid=(1, 2, 5), spread_grid=(0, 1), depth_grid=(1.0, 0.25),
                          timestamps=None, feature_data=None):
    rows = tuple(rows)
    if not all(isinstance(r, Snapshot) for r in rows):
        raise ValueError('rows must be Snapshot instances')
    config.validate()
    _index('config.delay', config.delay)
    _index('config.horizon', config.horizon)
    n = len(rows)
    holdout = _index('final_holdout_start', final_holdout_start)
    if not 0 < holdout < n:
        raise ValueError('final_holdout_start must leave at least one reserved row')
    grids = _grids(fee_grid, delay_grid, spread_grid, depth_grid)
    folds = _validate_folds(folds, holdout)
    lag = config.delay + config.horizon
    pre = validate_preflight(rows, config, folds, holdout,
                             tuple(sorted({config.delay, *grids['delay_observations']})),
                             timestamps=timestamps, feature_data=feature_data)
    # Widenings are nonnegative: the largest checks both bounds for every scenario.
    # Only test windows are transformed; validate them all before any evaluation.
    widest = max(grids['spread_widening_ticks'])
    for fold in folds:
        for index in range(fold['test_start'], fold['test_stop']):
            _widened_quotes(rows[index], widest, index)
    custom = timestamps is not None or feature_data is not None
    results = []
    for k, fold in enumerate(folds):
        start, stop = fold['test_start'], fold['test_stop']
        train_stop = start - lag
        if train_stop < MIN_TRAIN_DECISIONS:
            raise ValueError(f'fold {k}: {max(train_stop, 0)} training decisions < {MIN_TRAIN_DECISIONS}')
        for d in sorted({config.delay, *grids['delay_observations']}):
            if stop - d - config.horizon <= start:
                raise ValueError(f'fold {k}: no test decisions for delay {d}')
        prefix = list(rows[:stop])
        if pre['features'] is None:
            features = feature_series(prefix, seed=config.seed)
        else:
            features = {s: list(v[:stop]) for s, v in pre['features'].items()}
        inputs = None
        if custom:
            inputs = {'clock': pre['clock'], 'markers': list(pre['markers'][:stop]),
                      'features': None if pre['features'] is None else features,
                      'availability': None if pre['availability'] is None else
                      {s: list(v[:stop]) for s, v in pre['availability'].items()}}
        spread_rows = {w: _stress_rows(prefix, start, stop, widen=w) for w in grids['spread_widening_ticks']}
        depth_rows = {f: _stress_rows(prefix, start, stop, depth=f) for f in grids['depth_fraction']}
        signals = {}
        for name in SIGNALS:
            values = list(features[name][:stop])
            cal = calibrate(values, prefix, train_stop, config, name)
            pred = predictive_metrics(values, prefix, start, stop - lag, cal, config)
            base = simulate(prefix, values, start, cal, config)
            cases = [{'dimension': 'base', 'value': None, 'summary': _summary(base)}]
            for f in grids['fee_ticks']:
                cfg = dataclasses.replace(config, fee_ticks=f)
                cfg.validate()
                cases.append({'dimension': 'fee_ticks', 'value': f,
                              'summary': _summary(simulate(prefix, values, start, cal, cfg))})
            for d in grids['delay_observations']:
                cfg = dataclasses.replace(config, delay=d)
                cfg.validate()
                cases.append({'dimension': 'delay_observations', 'value': d,
                              'summary': _summary(simulate(prefix, values, start, cal, cfg))})
            for w, srows in spread_rows.items():
                cases.append({'dimension': 'spread_widening_ticks', 'value': w,
                              'summary': _summary(simulate(srows, values, start, cal, config))})
            for f, drows in depth_rows.items():
                cases.append({'dimension': 'depth_fraction', 'value': f,
                              'summary': _summary(simulate(drows, values, start, cal, config))})
            signals[name] = {'calibration': cal, 'predictive': pred, 'base_simulation': base,
                             'stress_cases': cases}
        results.append({'fold_index': k, 'test_start': start, 'test_stop': stop,
                        'train_start': 0, 'train_stop': train_stop,
                        'latest_train_label_index': train_stop - 1 + lag,
                        'predictive_decision_stop': stop - lag,
                        'reproduction_sha256': _digest(prefix, config, fold, grids, inputs),
                        'signals': signals})
    return _json_safe({
        'schema_version': SCHEMA_VERSION,
        'configuration': {'config': config, 'stress_grids': grids, 'row_count': n,
                          'min_train_decisions': MIN_TRAIN_DECISIONS, 'train_fraction_used': False},
        'boundaries': {'folds': folds, 'final_holdout': {'start': holdout, 'stop': n}},
        'units': {'pnl': 'ticks x lots', 'fee': 'ticks per lot per side',
                  'delay': 'BBO observations (not milliseconds)',
                  'spread_widening': 'integer stored ticks per side, test rows only',
                  'depth_fraction': 'scale of displayed top qty, floored, min 1, test rows only'},
        'calibration_policy': 'frozen per fold from base config; stress cases reuse base features '
                              'and calibration, including delay stress; labels only from base',
        'assumptions': [
            'each fold trains on decisions [0, test_start - delay - horizon) and starts flat',
            'features, calibration and simulation see only rows[:test_stop]',
            *(() if feature_data is None else (
                'supplied features used; available_at is a caller declaration checked <= row marker, '
                'provenance not externally verified',)),
            *(() if timestamps is None else ('caller timestamps validated nonnegative and ordered',)),
            'folds are independent diagnostics; PnL is not chained or summed across folds',
            'stress dimensions vary independently against the base config, not a Cartesian sweep',
            'trade and entry indices are global row indices',
            'non-finite numeric values are reported as None',
        ],
        'warnings': [
            'config.train_fraction is ignored; explicit fold boundaries define training',
            'reserved suffix [final_holdout_start, len(rows)) is never read for features, calibration, metrics or stress',
            'open inventory at test_stop is reported as-is; terminal_liquidation_mark is hypothetical, not executable',
            'no boundary auto-selection or winner selection is performed',
        ],
        'folds': results,
    })
