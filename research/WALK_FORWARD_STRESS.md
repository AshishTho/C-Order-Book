# Explicit-fold stress diagnostics

`research.stress_walk_forward.evaluate_walk_forward` accepts explicit,
non-overlapping test folds and a reserved final suffix. It complements the
existing `research.walk_forward` schedule API; that API and its tests remain
unchanged. The stress CLI is `scripts/run_walk_forward.py`.

Run the small checked-in synthetic fixture from the repository root, choosing a
new output path:

```sh
python scripts/run_walk_forward.py --input tests/fixtures/walk-forward-stress/terminal-depth.csv --plan tests/fixtures/walk-forward-stress/plan.json --output local-stress-report.json
python -m unittest discover -s tests -p test_walk_forward_stress.py -v
```

The plan requires `config`, `folds` (`test_start`, `test_stop`), and
`final_holdout_start`. Optional nonempty grids are `fee_grid`, `delay_grid`,
`spread_grid`, and `depth_grid`. The CSV requires integer `seq`, `bid_price`,
`ask_price`, `bid_qty`, `ask_qty` columns with strictly increasing sequence and
valid positive quotes/depth. Input bytes are read and hashed together. Output is
strict JSON and exclusively created; previous files are never overwritten.

Training decisions end at `test_start - delay - horizon`; their labels end
before the test begins. Each fold computes features and execution only through
`test_stop`, starts flat, and reports unclosed inventory. The reserved suffix is
never used by evaluation, although the CSV reader validates and fingerprints the
whole input. `train_fraction` does not choose folds here. At least twenty eligible
training decisions and one eligible test decision are required for every delay.

All five signals, including flat/random controls and losing scenarios, are
retained. Costs, observation delays, spread widening, and displayed-depth
fractions vary independently against frozen base calibration and features;
this is not a Cartesian search or parameter selection. Depth scaling uses exact
decimal fractions before flooring, with minimum one displayed lot. Quantities
above binary64's exact-integer range are never converted to float for this step.

The recorded [synthetic CLI result](../examples/walk_forward_stress_synthetic.json)
is a 26-row plumbing test, not market evidence. The seeded random control enters
ten lots: base depth closes the position; depth 0.25 exits two lots and retains
eight. The stressed midpoint-marked result is **−12 ticks × lots** and the
hypothetical liquidation mark is **−20**. Neither mark is an additional fill.
No cross-fold PnL is summed, forced liquidation performed, winner selected, or
alpha claim made. Keep this loss and existing research results visible.
