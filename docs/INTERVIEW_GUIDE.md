# Demonstrating the project

Use this project to connect systems correctness, experiment design, and trading
judgment. Describe only work you understand and have personally verified;
adapt the wording to your actual contribution and background.

## Five-minute walkthrough

1. Run the build and CTest. Explain why a simple reference book is useful:
   it is slower but independent of the optimized data structures, so it can
   expose state, fill, and priority disagreements after every operation.
2. Run `python scripts/run_study.py --output-dir build/interview-demo`.
   Trace one order add, execution, or cancellation through the C++ replay
   into its BBO observation and research signal.
3. Open the HTML report. Explain the train/test boundary, why the label horizon
   and entry delay require an embargo, and why direction/threshold calibration
   must use training observations only.
4. Explain an actual fill: decision at observation i, entry at i+delay,
   buy at ask/sell at bid, fees on each side, and depth-capped exit. Contrast
   a midpoint accounting mark with an executable fill price when asked.
5. Show the negative synthetic result. Explain why a simulator passing its
   accounting tests does not imply a profitable strategy. Outline the real-data
   experiment that could falsify the trading hypothesis.

## Role-specific discussion

For **quant development**, discuss cache layout, preallocation, bitmap versus
tree tradeoffs, generation IDs, ownership, parser bounds, and why matching
still scales with fills. Show a bug regression and its randomized oracle.

For **quant research**, discuss OFI versus imbalance, the algebraic relationship
between imbalance and weighted midpoint, chronological leakage, multiple
comparisons, dependent observations, and cost/delay sensitivity. Correlation is
a diagnostic; there is no confidence interval or validated alpha claim here.

For **quant trading**, discuss turnover, spread capture versus spread payment,
liquidity at the intended size, inventory at the end of a test, and why displayed
depth is an optimistic capacity bound. Queue position and passive fill modeling
are absent; quote generation alone does not establish market-making profitability.

## Defensible project bullets

Use these only after you can explain the implementation and your contribution:

- Developed and evaluated a C++20 order-book research workflow with deterministic
  L3 replay, fixed-point matching, OFI/imbalance features, and chronological
  holdout experiments using delayed bid/ask execution and transaction costs.
- Validated matching behavior against a seeded reference model over 36,000
  operations; added malformed-feed, numeric-boundary, reserve-priority, risk,
  and replay regression tests with cross-platform CI and sanitizer coverage.
- Built reproducible experiment artifacts with input/source/binary hashes,
  fill-level audit data, drawdown, inventory, and fee/delay sensitivity reports.

Do not claim production HFT deployment, verified live profitability, regulatory
compliance, or a nanosecond latency number without corresponding evidence.

## Next experiment to own

Obtain licensed order-level data for several instruments and independent days.
Document source sequencing, corrections, session resets, tick/lot conventions,
and fees. Freeze features and thresholds before the last holdout. Evaluate
separate spread/depth regimes, a dependence-aware uncertainty procedure, and
an execution model appropriate to the strategy. Keep failed hypotheses and
negative results in the research record. This is where personal research
judgment can add more value than another synthetic speed headline.
