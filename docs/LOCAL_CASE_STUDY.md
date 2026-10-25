# A reproducible synthetic order-book study

The question was deliberately narrow: on one fixed synthetic sequence, would
imbalance decisions lose less than a seeded random control after crossing costs?
The locally predeclared hypothesis, seed, bounds and metric are in
[`LOCAL_STUDY_PLAN.json`](../research/LOCAL_STUDY_PLAN.json). No parameter or seed
search followed the result. This is a software and research-method demonstration,
not evidence about a venue, instrument or profitable strategy.

## Method and measured result

The C++ replay processed 5,000 generated order events into 4,999 BBO observations.
The first 60% calibrated thresholds and orientation. Test observations began at
index2,999; the final training label ended at2,998. Delay1 plus horizon10 required
an 11-observation purge. Orders crossed displayed bid/ask with one-lot size,
0.1 tick fee per lot per side and zero additional slippage. The units are stored
integer ticks×lots, not dollars. No millisecond delay is inferred.

| Fixed signal | Held-out net ticks×lots | Completed round trips | Terminal lots |
| --- | ---: | ---: | ---: |
| Imbalance | -2,422.4 | 142 | 0 |
| Microprice displacement | -2,159.0 | 160 | 0 |
| OFI | -3,957.2 | 131 | 0 |
| Seeded random | -4,106.0 | 140 | 0 |
| Flat control | 0.0 | 0 | 0 |

The predeclared imbalance-minus-random metric was **+1,683.6 ticks×lots**.
Imbalance still lost money in the simulation, and the flat control did better.
Microprice is shown for completeness, not selected as a new winner. Removing
fees left imbalance at -2,394.0; raising fees to1 tick per side made it -2,678.0.
Frozen-calibration delays2 and5 produced -5,231.6 and -3,510.4 respectively.

Two separate development folds tested fee0/0.1/0.5, delay1/2/5, spread widening
0/1/3 ticks per side and depth fractions1/0.25/0.1. Each dimension varied alone,
not as a search. All active cases remained negative. The one-lot order and
minimum-one-lot depth scaling made the depth cases identical in these folds;
this does not establish robustness to unavailable liquidity. The separate
terminal-depth fixture retained eight unclosed lots with a -12 midpoint mark
and a hypothetical -20 liquidation mark. Neither mark was booked as a fill.

## Systems evidence and tradeoffs

Fresh native tests covered price/time priority, cancellation, partial fills,
numeric boundaries, a 36,000-operation vector reference model and feed/replay
validation. Two added tests check that current test-window data cannot alter
training calibration and that irregular clocks preserve observation-based fills.
The pre-publication-fix Python run passed194 tests with18 Windows symlink skips.
Independent release checks found a partial-final-file defect in stress output.
Staged no-clobber publication fixed it; six focused checks passed, including
disk failure, keyboard interruption and a competing writer. The final aggregate
and clean-checkout results are recorded separately; do not infer them from these
earlier counts.

Three same-build streaming benchmark repeats measured63.9,75.3 and74.1 million
operations/second (mean71.1, sample standard deviation6.26). They are uncontrolled
desktop microbenchmarks, not a latency SLA or cross-version improvement. The
[reproduction guide](LOCAL_REPRODUCTION.md) records the warmup, hardware identifier,
compiler flags and important harness differences. Fixed ladders and preallocation
make bounded single-writer mutation efficient, at the cost of explicit price and
capacity limits. The simple vector oracle favors clarity over matching-engine speed.

## Reproduction and limits

Follow the [one-command study after native build](LOCAL_REPRODUCTION.md).
Input BBO SHA-256:
`6dbd74fa881d6fda152fad0ef7aee12ed303495d7835632d72a258d05e58fe8f`.
The two runs produced identical event/BBO bytes and identical reports except for
their absolute input path. Each saved manifest's file hashes were verified.
The local receipt pack sits beside the candidate: `research-summary.json`,
`fixed-holdout/manifest.json`, `stress-expanded-report.json`, `benchmark-report.json`.
Original run base was`1c85ec863c3f182c814044425ab7caef8e65825a`; the recorded source
hash binds the exact code despite then-uncommitted documentation/test additions.

The generator has artificial anchor quotes. There is no market impact, queue
position, funding, short availability, real clock, broker or exchange connection.
Overlapping labels are dependent, and this single holdout supports no independent
sample significance claim. Current CMake/CTest and sanitizers were not run here.
Measurements were performed locally; publication is tracked separately from
validation. No merge, installation, or live financial action occurred.

## Three-minute walkthrough

1. Show the fixed plan and chronological purge before the result table.
2. Explain the flat ladder/FIFO/arena design and one oracle failure boundary.
3. Open the generated report, compare imbalance with random and flat, then inspect
   recorded entry/exit fills and fees. Keep the negative result visible.
4. Reproduce with the existing command, compare hashes, and explain the simulation
   and benchmark limits. Project claims should stay tied to this measured evidence.
