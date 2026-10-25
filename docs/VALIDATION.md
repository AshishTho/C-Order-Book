# Recorded validation

## Current local candidate, 25 October 2026

Base: `1c85ec863c3f182c814044425ab7caef8e65825a`, independently cloned locally
from the combined candidate. The base incorporates twelve recorded source
commits on portfolio base `6862e13d019c0324cc1110242fd78a3efa6af99e`:
the benchmark repair and the changes associated with PR14–24. It reuses matching,
walk-forward, stress, provenance and existing replay-viewer code. No sibling
Serephine work belongs in this candidate.

Fresh base validation: 210 Python tests collected, 192 passed and 18 Windows
symlink tests skipped. The matching executable was freshly compiled with GCC
13.1.0, C++20, `-O2 -Wall -Wextra -pthread -static`, and passed all fourteen
groups, including 36,000 seeded oracle operations and numeric boundaries.
The local evidence pack records exact commands, log hashes and later results.
CMake registration now includes the previously omitted compare, data-quality,
demo and provenance suites. CMake itself is unavailable in this environment;
its registration change is inspected, not claimed as an executed CTest run.

Exclusions: historical ancestry is incomplete (missing ancestor
`f7acb5499089ef87ec02d65836bfe126f07f5f18`); there is no combined remote CI,
current sanitizer run, live-market validation, publication, merge or installation.
Exact working-tree contents remain available. Old evidence below is historical
and must not be counted as current candidate validation.

## Historical validation retained from the earlier portfolio snapshot

Local validation on Windows x64, GCC 13.1.0 (MSYS2 UCRT64), CMake 3.31.10,
Python 3.10.11, Release build; native CPU tuning disabled.

- All twelve CMake targets built successfully.
- All four CTest suites passed: correctness, feed, research, replay.
- Core tests include 36,000 seeded reference-book operations and 4,000,000
  SPSC transfers; replay includes a separate 3,000-event Python depth oracle.
- Fourteen research unit tests and six replay unit tests passed.
- The seed-42 study replayed 10,000 events into 9,999 BBO observations;
  all active synthetic strategies lost after spread/fees, and all ended flat.
- Signals, risk, advanced orders, and journal benchmarks each completed three
  runs successfully, including their embedded functional checks.

[Synthetic summary](../examples/synthetic_summary.json) contains the configuration,
holdout boundaries, metrics, and source/input fingerprints.
[Benchmark evidence](../examples/benchmark_windows_gcc13.json) preserves all
twelve raw stdout/stderr logs, binary/source hashes, and portable build settings.
These desktop measurements are illustrative: load and scheduling are not controlled.
The current checked-in source fingerprint is the relevant identifier for these
records; no timing comparison with the previous engine has been established.

GitHub Actions independently runs GCC Release, Clang Debug with ASan/UBSan,
and Windows MSVC Release. Inspect the exact commit's workflow result for CI
status; defining a job is not evidence that it passed. MinGW here has no
`libubsan`, so sanitizer validation relies on the Linux job.

Tests do not establish exchange compatibility, regulatory controls, durable
crash recovery, live profitability, or absence of all bugs. See the explicit
contracts and remaining limitations in [DESIGN.md](DESIGN.md).
