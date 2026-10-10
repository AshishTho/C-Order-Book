# Leakage Lab: explicit future-row review

This tool implements **two bounded static Python syntax heuristics**. LL001
reviews literal negative row shifts; LL002 reviews centered fixed row windows.
Both patterns can put later observations into earlier features or decisions.
It is an offline review aid, not a leakage detector with comprehensive coverage.

```sh
python -m research.leakage_check tests/fixtures/leakage/future_feature.py
python -m research.leakage_check tests/fixtures/leakage/past_feature.py
python -m research.leakage_check tests/fixtures/leakage/centered_feature.py
python -m research.leakage_check tests/fixtures/leakage/trailing_feature.py
python -m unittest discover -s tests -p test_leakage_check.py -v
```

Select 1-20 UTF-8 Python files explicitly, at most 1 MiB each. The command reads
them once, hashes the exact bytes, parses syntax and emits JSON to stdout. It
never imports or executes the selected code, scans directories, contacts a
service or changes a file. Reports contain paths and line/UTF-8-byte column
locations, but omit source snippets. Treat paths as potentially private when
choosing to share a report.

Exit codes: **1** means review candidates were found; **2** means an input or
argument error; **0** means no supported candidates were found. Zero is never a
safe-backtest verdict. Every report explicitly says `certifies_leakage_free:
false`, lists limitations, and counts unassessed calls and file errors.

## LL001: negative row shifts

LL001 assumes pandas-like `shift` semantics and rows ordered from past to future.
It does not resolve object types or inspect the data. A different method named
`shift`, descending row order, or a legitimate future target can produce a false
positive. Review whether the value enters a decision before it is observable.
Keep future labels out of features and review overlap at chronological splits;
do not mechanically replace every negative shift with a positive one.

Only literal scalar integer periods, the row axis (default, `0`, `"index"` or
`None`), and an absent/`None` frequency are assessed. Dynamic arguments, iterable
periods, argument expansion, column shifts and frequency/index shifts are
reported as unassessed. The distinction follows the
[pandas `DataFrame.shift` API](https://pandas.pydata.org/docs/reference/api/pandas.DataFrame.shift.html).

The broken fixture uses the next close in a decision. The corrected fixture
uses a prior close for this particular feature; the current close must still be
available before deciding. Neither fixture establishes executable fill timing.
Tests also retain the expected warning for a legitimate future-label example,
and prove that scanned source is not run or echoed into reports.

## LL002: centered fixed row windows

`close.rolling(3, center=True).mean()` places the window's result at its center.
With chronological rows and pandas semantics, the three-row mean at row `t`
uses rows `t-1`, `t`, and `t+1`. If used at `t`, that feature can see a later
close. LL002 warns on the `rolling` call itself, without resolving its type,
aggregation or eventual use. The rule follows the
[pandas `DataFrame.rolling` API](https://pandas.pydata.org/docs/reference/api/pandas.DataFrame.rolling.html).

The supported subset is deliberately small:

- The first three positional arguments are `window`, `min_periods`, `center`;
  these may also be supplied as keywords without duplicates.
- `window` must be a literal positive integer. `center` must be a literal
  boolean or omitted (default `False`). Only `center=True` and widths of at
  least **3** warn. With default/right closure, widths 1 and 2 include no later
  row even when centered. This is not a claim about other leakage risks.
- `min_periods` is omitted, `None`, or a literal integer between 0 and `window`.
- `closed` is omitted, `None`, or `"right"`. The axis is omitted, `0`, or
  `"index"` (the last two cover older pandas row-axis syntax; newer pandas
  versions removed that parameter).

Dynamic expressions, time-based windows, custom indexers, non-default closure,
column/dynamic axes, invalid or dynamic minimum periods, argument expansion,
duplicate arguments, more than three positional arguments, and **all other
keywords** are recorded as unassessed. This includes `on`, `win_type`, `step`
and `method`, even if their supplied values happen to be defaults. No arbitrary
expression is evaluated. Method aliases and indirect calls are not recognized
or counted, so unassessed counts are not an inventory of every unsupported use.

False positives include retrospective plots, future targets, unrelated methods
named `rolling`, unused rolling objects, non-chronological data, and a centered
result delayed by a sufficient positive shift before use. No downstream delay is
verified. Do not mechanically remove centering from every warning.

The deliberately broken `centered_feature.py` fixture uses the centered mean
in a decision. `trailing_feature.py` changes just that window to a trailing
mean, which uses rows `t-2`, `t-1`, and `t` for width 3. The current close must
still be available before deciding; warm-up handling and executable fill timing
remain separate concerns. The checker only parses these fixtures; it never
imports or executes them.

Reports retain schema version 1 and LL001 records, with an additive
`rolling_calls` count per parsed file. LL002 candidates and unassessed records
include `rule: "LL002"`; both rules contribute to the existing summary and exit
codes. Findings and unassessed records are sorted by source location. Tied
locations (for example chained calls) list LL001 first.

## Coverage limits

**Not implemented:** aliases or dataflow, indirect future indexing, full-sample
preprocessing, train/test contamination, feature availability, label embargoes,
same-bar fills or other execution-timing checks. A report with no findings cannot
rule out any of these. Existing economic tests and the methodology remain
separate evidence, not a substitute for reviewing an actual experiment.
