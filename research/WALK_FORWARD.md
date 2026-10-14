# Expanding-history walk-forward evaluation

This optional protocol reuses the existing signals, calibration, delayed crossing
fills, depth caps and replay inspector. It fixes the fold schedule and every
hyperparameter before evaluation. It learns only each signal's threshold and
orientation from prior observations; it does not select winning strategies or
parameters using future scores.

```sh
python -m research.walk_forward --input build/study/bbo.csv --output build/walk-forward.json --data-kind synthetic --initial-train 1000 --test-size 1000 --horizon 10 --delay 1 --fee-ticks 0.1 --html build/walk-forward-fold-0.html --html-fold 0
python -m unittest discover -s tests -p test_walk_forward.py -v
```

Use fresh output paths. Input bytes are read and hashed once, JSON and optional
HTML are exclusively created, and existing sources/results are never overwritten.
As with the original report, these outputs are not an atomic pair. The JSON
contains all folds; `--html-fold` selects one zero-based fold for the existing
HTML/replay renderer. No new viewer or network dependency is introduced.

For fold `k`, with `T = initial_train`, `B = test_size`, and
`M = delay + horizon`:

- Test observations are `[T + k*B, T + (k+1)*B)` with no overlapping test blocks.
- Training decisions are `[0, test_start - M)`. Their last label ends at
  `test_start - 1`; calibration is passed only the pre-test history.
- Test decisions stop at `test_end - M`, exclusive. Every entry, label and exit
  stays within the current block; partial exits may leave terminal inventory.
- Both the initial history and test size must leave at least 20 eligible
  decisions. Only complete blocks are evaluated. An unused tail is explicitly
  reported and is never silently scored as a shorter block.

Earlier test observations may enter later training once their labels are
observable. Thus every block is unseen by its own calibration, not forever
excluded from later fits. The seed, horizon, delay, threshold quantile, fees,
slippage, order size and schedule stay fixed. The single-holdout `train_fraction`
is unused by this API and absent from the walk-forward report configuration.
Causal features are recomputed from the global beginning of each prefix, keeping
OFI context and the seeded random stream consistent. This prioritizes auditable
prefix isolation over optimization for very large numbers of folds.

Each block is an **independent flat-start experiment**. Cash and inventory reset
at its start. Any ending inventory and partial exits stay in that fold's report;
there is no fabricated liquidation, cross-fold carry, stitched equity curve or
aggregate tradable PnL. A continuous portfolio would need a separately specified
boundary and rebalancing policy. No fee/delay sensitivity panels are run here.

Synthetic constant-market tests retain negative random-control PnL after crossing
costs and the flat-zero control. Mutating future rows cannot change calibration
or earlier complete folds; appending rows cannot revise completed folds. These
invariants test this implementation, not whether an arbitrary strategy is free
of leakage or profitable. Overlapping labels and shared training history are not
independent statistical trials. Choosing inputs after inspecting these reports
turns them into development data; reserve fresh observations for final validation.

The existing [methodology](METHODOLOGY.md) still governs units, unavailable-liquidity
assumptions, omitted queue/impact/funding effects, and synthetic-data limitations.
The previously reported losses after costs are unchanged and must not be tuned
away or hidden.
