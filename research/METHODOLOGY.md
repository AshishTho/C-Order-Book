# Research protocol

This lab connects the matching/reconstruction engine to a falsifiable research
question: **does a book signal predict a later price move large enough to pay for
crossing the spread, fees, and execution delay?** A predictive correlation and an
executable profit are different outcomes. The report exposes both.

The included generator is a **synthetic pipeline fixture**. Permanent deep
quotes, asynchronous cancellation/replacement, and random near-quote sizes make
it deliberately easy to exercise book transitions. These choices also create
artificial patterns and wide spreads. Its results demonstrate software behavior,
not alpha, market realism, or expected investment performance.

## Reproduce

Python 3.10+; standard library only. Run from the repository root after building
the `replay_events` target:

```sh
python -m research.generate --output build/events.csv --events 10000 --seed 42
./build/replay_events --input build/events.csv --output build/bbo.csv
python -m research.lab --input build/bbo.csv --output build/report.json --html build/report.html --data-kind synthetic
python -m unittest discover -s tests -p test_research.py -v
```

On Windows the replay executable is `build/replay_events.exe` (or inside the
configuration subdirectory for a multi-configuration CMake generator). The
generator also writes `events.csv.meta.json` containing the seed, version and
SHA-256. The research JSON includes the BBO input hash, parameters, Python
version, split indices, training calibration, fills, full equity paths, and
sensitivity results. HTML is a self-contained summary of that JSON.

The CLI parses and hashes the same input bytes in one read before evaluation.
A later replacement of the CSV cannot change the recorded hash. The hash
includes the original BOM and line endings; it describes the analyzed bytes,
not a second reading of the path. Retain that input separately for reproduction.

The report CLI requires distinct input, JSON and HTML paths, and refuses to
overwrite existing outputs. Use new report filenames or a fresh run directory
for each evaluation so source data and prior evidence remain intact. Exclusive
file creation also rejects outputs created after the initial path check. The
JSON and HTML are separate files, not an atomic pair: a later I/O failure can
leave a newly created report; inspect it and use fresh paths for a retry.

For your own data, export one instrument and one continuous session to:

```csv
seq,bid_price,ask_price,bid_qty,ask_qty
1,10000,10002,90,60
2,10000,10002,80,65
```

Declare `--data-kind historical` only when the input is historical. That label is
a user declaration, not independent provenance verification. Keep licensing,
venue, instrument, session, adjustments, and feed-gap policy with the source
data. The loader rejects crossed/locked books, nonpositive quantities, malformed
integers, and nonincreasing sequences. It cannot detect missing source events
from a BBO CSV alone.

## Units and information set

Prices are integer **stored ticks**, matching the C++ replay interface. A stored
tick is not automatically the exchange's minimum quote increment. Fees and
additional adverse slippage are expressed in stored ticks **per lot per side**.
PnL is ticks × lots. Map these to the correct venue price/lot multiplier before
interpreting currency results. There is no implicit dollar or basis-point
conversion.

`--delay 1` means one later exported BBO observation. `--horizon 10` means ten
observations after the entry fill. Observations need not have equally spaced
timestamps; sequence gaps are not durations. The lab does not equate C++ measured
latency with this event-delay parameter. Do not concatenate sessions without an
explicit session/reset policy.

## Features

Let `b,a` be the best bid and ask; `qb,qa` their displayed quantities.

- **Imbalance:** `(qb - qa) / (qb + qa)`.
- **Microprice proxy:** `(b*qa + a*qb)/(qb+qa) - (a+b)/2`. This is a
  weighted-mid displacement, algebraically `(a-b) * imbalance / 2`. At a fixed
  spread, it carries exactly the same information as imbalance. It is not an
  independently calibrated Markov-chain micro-price estimator; see
  [Stoikov, *The micro-price: a high-frequency estimator of future prices*](https://www.tandfonline.com/doi/abs/10.1080/14697688.2018.1489139).
- **OFI:** the current event contribution
  `1[b>=b_prev]*qb - 1[b<=b_prev]*qb_prev - 1[a<=a_prev]*qa + 1[a>=a_prev]*qa_prev`,
  divided by the current total displayed depth. The contribution follows
  [Cont, Kukanov and Stoikov, *The Price Impact of Order Book Events*](https://arxiv.org/abs/1011.6402).
  Here it is a single normalized observation feature, not their time-bucket
  aggregate or a reproduction of their empirical study.
- **Random control:** a reproducible uniform random feature, with fixed positive
  orientation. Its activation threshold comes from its training values.
- **Flat control:** zero positions, turnover, and PnL.

All features use current/past observations only. The first OFI contribution is
zero because there is no preceding observation. The first test OFI can use the
immediately preceding historical observation, which would be available live.

## Frozen chronological holdout

With `N` observations, the test starts at `floor(N * train_fraction)`. The final
`delay + horizon` candidate training observations are purged. Thus the latest
training label ends at the observation immediately before test starts. This
pre-test embargo is documented in every report.

The training label for a decision at `t` is
`mid[t + delay + horizon] - mid[t + delay]`. Training chooses only:

1. The sign of the feature/label correlation, used as signal orientation.
2. The configured quantile of absolute feature values, used as activation
   threshold. Nonzero feature values at or above the threshold activate.

Zero/undefined training correlation produces no trades for a learned signal.
Threshold ties can cause activation rates to exceed the nominal upper-tail
fraction. The random control always uses positive orientation, and the flat
control never activates.

The default 75th percentile, horizon, train fraction and costs are specified
before evaluation; the code does not optimize them on test PnL. All fee and
delay sensitivity scenarios freeze the base training calibration. They are
diagnostics, not new untouched holdouts. Selecting a strategy or scenario from
these panels requires another unseen dataset before reporting generalization.

The default lab is a **single holdout**. An optional [walk-forward protocol](WALK_FORWARD.md)
adds fixed-schedule independent future folds without strategy selection. The default remains a small,
auditable starting protocol. Labels overlap, so the lab does not attach IID
p-values to correlation or report an annualized Sharpe without clock-time and
capital assumptions. A single seeded random path is a sanity check, not a
statistical null distribution.

## Executable accounting

A signal decided at `t` enters at `t + delay`. Long entries buy at that later
ask; shorts sell at that later bid. Exit begins `horizon` observations after the
fill, on the opposite side of the spread. The simulator allows one pending
entry or inventory position. It may decide a new entry after the prior exit,
but that order still waits for its own delay.

Entry quantity is capped by the then-displayed contra-side depth. Unfilled entry
quantity is discarded. Exit fills are also depth-capped; an unfilled remainder
persists into later observations. The report retains completed round trips,
all their exit fills, and any terminal open trade with its partial exits. It
does not invent a closing fill when the data ends.

Fees and slippage are charged at every fill, on both sides of a round trip.
Gross cash and price differences use integer arithmetic before conversion to
floating point for research statistics/costs. This avoids losing a small price
move solely because the absolute price has a large offset. Midpoint equity and
drawdown are accounting marks, not execution prices. A separate hypothetical
terminal liquidation mark is disclosed; available depth may not support it.

Displayed liquidity is assumed available when a simulated market order arrives.
The historical book is exogenous: simulated trading does not consume future
historical depth. There is no market impact, queue-position model, exchange
latency distribution, short-borrow/funding model, or maker-fill simulation. This
is an intentionally explicit small-order crossing benchmark, not a production
execution simulator. No return percentage is reported without a capital model.

## Inspecting the recorded replay

The self-contained HTML report includes an observation replay inspector. Choose
a strategy, scrub held-out observations, or jump between recorded fills. It shows
midpoint equity, inventory, drawdown, per-fill fees/slippage, completed round-trip
PnL, and terminal open inventory without adding a hypothetical closing fill.
Cost rows are derived from the recorded quantities and saved cost configuration.
Large integer prices and sequence identifiers are embedded as decimal strings to
avoid browser rounding. Losing trades and the flat control remain in the report.

The provenance panel exposes the analyzed input SHA-256, source declaration,
configuration, units and chronological split. These controls only inspect saved
simulation results; they do not rerun models, fetch data or place orders. The JSON
report remains the full audit artifact, and existing output files are still never
overwritten. The interactive section includes every held-out point and recorded
fill, so HTML size grows with the experiment; the overview plot stays sampled.

## Next empirical experiment

Before changing the strategy, obtain a licensed, gap-audited multi-session feed;
preserve timestamps and session boundaries; select horizons/costs using training
sessions only; and reserve a final set of sessions that remains unseen during
development. Compare against an activity-matched distribution of seeded random
controls, estimate uncertainty with session/block resampling, and separate
selection from final validation. If crossing costs dominate, the result is a
valid negative finding. A maker strategy would require a new queue-aware fill
model and adverse-selection analysis rather than replacing asks/bids with
midpoints in this one.
