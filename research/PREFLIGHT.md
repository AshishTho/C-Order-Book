# Walk-forward preflight

`research.preflight.validate_preflight` validates the full schedule before any evaluation. `research.stress_walk_forward.evaluate_walk_forward` calls it once (all folds, all delays) before `feature_series`/`calibrate`/`predictive_metrics`/`simulate`; any failure raises `ValueError` before the first fold.

## API

```python
evaluate_walk_forward(rows, config, folds, final_holdout_start, ..., timestamps=None, feature_data=None)
```

- `timestamps`: optional nonnegative strictly increasing ints, one per row; selects `clock='timestamp'`. Equal timestamps are rejected before evaluation. Unequal gaps are allowed; delay/horizon still count observations, not seconds.
- `feature_data`: `{'clock': 'seq'|'timestamp', 'signals': {name: {'values': [...], 'available_at': [...]}}}` for every name in `research.lab.SIGNALS`; arrays of length `max(test_stop)`, finite non-bool numbers, `available_at[i] <= marker[i]`.

```python
fd = {'clock': 'seq', 'signals': {s: {'values': vals, 'available_at': avail} for s in SIGNALS}}
report = evaluate_walk_forward(rows, Config(), folds, 80, feature_data=fd)
```

Each fold uses only the `[:test_stop]` prefix of supplied features. Default runs (no options) produce byte-identical outputs and digests; custom runs add clock, marker prefix, values and availability to the fold digest (later-fold data excluded).

## Limits

- `available_at` is a caller declaration checked against row markers, not verified provenance.
- `1e150` is a conservative input cap; downstream arithmetic is not certified finite.
- CLI/CSV format unchanged; the CLI gains default preflight only.
