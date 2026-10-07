# Leakage Lab: explicit future-shift review

This first increment implements **one static Python syntax heuristic**, LL001.
It identifies calls such as `close.shift(-1)` or `close.shift(periods=-2)` that
could put later row values into earlier rows if used as features or decisions.
It is an offline review aid, not a leakage detector with comprehensive coverage.

```sh
python -m research.leakage_check tests/fixtures/leakage/future_feature.py
python -m research.leakage_check tests/fixtures/leakage/past_feature.py
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

**Not implemented:** aliases or dataflow, indirect future indexing, full-sample
preprocessing, train/test contamination, feature availability, label embargoes,
same-bar fills or other execution-timing checks. A report with no findings cannot
rule out any of these. Existing economic tests and the methodology remain
separate evidence, not a substitute for reviewing an actual experiment.
