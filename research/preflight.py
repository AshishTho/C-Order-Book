"""Pure preflight validation for walk-forward evaluation inputs (stdlib only, no I/O)."""
import math

from research.lab import SIGNALS, Snapshot

MIN_TRAIN = 20
MAX_ABS_VALUE = 1e150  # conservative input magnitude cap; does not certify downstream arithmetic
_FOLD_KEYS = frozenset(('test_start', 'test_stop'))
_SIGNAL_KEYS = frozenset(('values', 'available_at'))


def _is_int(x):
    return type(x) is int


def _seq_of(x, name):
    if not isinstance(x, (list, tuple)):
        raise ValueError(f'{name} must be a list or tuple')
    return tuple(x)


def _increasing_ints(values, name, upper=None):
    prev = -1
    for i, v in enumerate(values):
        if not _is_int(v) or v < 0 or (upper is not None and v >= upper) or v <= prev:
            raise ValueError(f'{name}[{i}] must be a nonnegative int, strictly increasing')
        prev = v


def _check_folds(folds, holdout, config, delays):
    folds = _seq_of(folds, 'folds')
    if not folds:
        raise ValueError('folds must be nonempty')
    lag = config.delay + config.horizon
    all_delays = {config.delay, *delays}
    errors, out, prev_stop = [], [], 0
    for i, f in enumerate(folds):
        if type(f) is not dict or set(f) != _FOLD_KEYS:
            errors.append(f'fold {i}: must be a dict with exactly test_start, test_stop')
            continue
        start, stop = f['test_start'], f['test_stop']
        if not (_is_int(start) and _is_int(stop)):
            errors.append(f'fold {i}: bounds must be ints')
            continue
        if not 0 <= start < stop <= holdout:
            errors.append(f'fold {i}: require 0 <= start < stop <= holdout')
        if start < prev_stop:
            errors.append(f'fold {i}: overlaps or precedes previous fold')
        if start - lag < MIN_TRAIN:
            errors.append(f'fold {i}: fewer than {MIN_TRAIN} training rows before label lag')
        if (start - lag - 1) + lag >= start:
            errors.append(f'fold {i}: latest training label reaches test window')
        for d in sorted(all_delays):
            if stop - d - config.horizon <= start:
                errors.append(f'fold {i}: no test decision for delay {d}')
        prev_stop = max(prev_stop, stop)
        out.append({'test_start': start, 'test_stop': stop})
    if errors:
        raise ValueError('; '.join(errors))
    return tuple(out)


def _check_value(v, where):
    if type(v) not in (int, float):
        raise ValueError(f'{where}: value must be int or float')
    try:
        fv = float(v)
    except OverflowError:
        raise ValueError(f'{where}: value too large') from None
    if not math.isfinite(fv) or abs(fv) > MAX_ABS_VALUE:
        raise ValueError(f'{where}: value must be finite and bounded')


def _check_features(feature_data, clock, markers, need):
    if type(feature_data) is not dict or set(feature_data) != {'clock', 'signals'}:
        raise ValueError('feature_data must have exactly clock, signals')
    if feature_data['clock'] != clock:
        raise ValueError(f'feature_data clock must be {clock!r}')
    signals = feature_data['signals']
    if type(signals) is not dict or set(signals) != set(SIGNALS):
        raise ValueError(f'signals must have exactly {SIGNALS}')
    features, availability = {}, {}
    for name in SIGNALS:
        entry = signals[name]
        if type(entry) is not dict or set(entry) != _SIGNAL_KEYS:
            raise ValueError(f'{name}: must have exactly values, available_at')
        values = _seq_of(entry['values'], f'{name}.values')
        avail = _seq_of(entry['available_at'], f'{name}.available_at')
        if len(values) != need or len(avail) != need:
            raise ValueError(f'{name}: sequences must have length {need}')
        for i in range(need):
            _check_value(values[i], f'{name}[{i}]')
            a = avail[i]
            if not _is_int(a) or a < 0 or a > markers[i]:
                raise ValueError(f'{name}[{i}]: available_at must be int in [0, marker]')
        features[name], availability[name] = values, avail
    return features, availability


def validate_preflight(rows, config, folds, holdout, delays, *, timestamps=None, feature_data=None):
    """Validate evaluation inputs; return normalized copies or raise ValueError.

    Checks shape/causality declarations only; cannot prove external provenance.
    """
    try:
        config.validate()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f'invalid config: {exc}') from None
    rows = _seq_of(rows, 'rows')
    if not all(isinstance(r, Snapshot) for r in rows):
        raise ValueError('rows must be Snapshot instances')
    seqs = tuple(r.seq for r in rows)
    _increasing_ints(seqs, 'seq', upper=2 ** 64)
    if not _is_int(holdout) or not 0 < holdout < len(rows):
        raise ValueError('holdout must be an int with 0 < holdout < len(rows)')
    delays = _seq_of(delays, 'delays')
    if not delays or any(not _is_int(d) or d < 1 for d in delays):
        raise ValueError('delays must be a nonempty sequence of positive ints')
    norm_folds = _check_folds(folds, holdout, config, delays)
    if timestamps is None:
        clock, markers = 'seq', seqs
    else:
        markers = _seq_of(timestamps, 'timestamps')
        if len(markers) != len(rows):
            raise ValueError('timestamps must match rows length')
        _increasing_ints(markers, 'timestamps')
        clock = 'timestamp'
    features = availability = None
    if feature_data is not None:
        need = max(f['test_stop'] for f in norm_folds)
        features, availability = _check_features(feature_data, clock, markers, need)
    return {'folds': norm_folds, 'clock': clock, 'markers': markers,
            'features': features, 'availability': availability}
