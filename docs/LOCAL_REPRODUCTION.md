# Reproduce the local candidate without installing tools

This Windows path uses the existing GCC 13.1.0 and Python 3.10.11 toolchain.
Run from this repository root in PowerShell. A new output directory is required;
the study command refuses an existing one. It uses only generated synthetic data.

```powershell
New-Item -ItemType Directory -Path build -Force | Out-Null
& 'C:\msys64\ucrt64\bin\g++.exe' -std=c++20 -O2 -Wall -Wextra -pthread -static -Iinclude tests/test_correctness.cpp -o build/test_correctness.exe
& 'C:\msys64\ucrt64\bin\g++.exe' -std=c++20 -O2 -Wall -Wextra -pthread -static -Iinclude tests/test_feed.cpp -o build/test_feed.exe
& 'C:\msys64\ucrt64\bin\g++.exe' -std=c++20 -O2 -Wall -Wextra -pthread -static -Iinclude tools/replay_events.cpp -o build/replay_events.exe
& .\build\test_correctness.exe
& .\build\test_feed.exe
& 'C:\msys64\ucrt64\bin\python.exe' tests/test_replay.py build/replay_events.exe
& 'C:\msys64\ucrt64\bin\python.exe' -m unittest tests.test_benchmark tests.test_compare tests.test_data_quality tests.test_demo tests.test_experiment_capsule tests.test_leakage_check tests.test_preflight tests.test_provenance tests.test_repro_capsule tests.test_research tests.test_run_study tests.test_walk_forward tests.test_walk_forward_stress
```

After a successful build, one existing command reproduces the end-to-end study:

```powershell
& 'C:\msys64\ucrt64\bin\python.exe' scripts/run_study.py --build-dir build --output-dir build/study-20261025 --events 5000 --seed 20261025
```

Open `build/study-20261025/report.html`. The JSON stores exact configuration,
training/test boundaries, recorded fills, fees, sensitivity cases and inventory.
The manifest records revision, source/binary hashes, seed, commands and hashes
of generated inputs and complete reports. The report hash also binds its exact
configuration. A failure or timeout has no success manifest; preserve partial
output and choose a fresh directory for a retry. Do not use broad Python test
discovery: `test_replay.py` needs an explicit native executable.

The October25 repeat produced byte-identical events/BBO. Reports matched after
removing only their differing absolute input path. This is a reproducibility
check of one fixed experiment, not another seed search. Recorded outputs use
base revision `1c85ec863c3f182c814044425ab7caef8e65825a` plus their source hash;
documentation and test additions were uncommitted at that original run.

The external local evidence directory is `../` relative to this candidate:
`fixed-holdout/manifest.json`, `research-summary.json`, and the command receipts.
These files were not uploaded. CMake/CTest and sanitizer execution remain
unavailable here; direct compiler checks must not be reported as those runs.

## Benchmark interpretation

The existing `bench_rdtsc` harness uses two million deterministic operations,
100,000 warmup operations and per-run TSC calibration. Three October25 repeats
on Windows10, Intel64 Family6 Model158 Stepping10, using the same GCC build,
reported streaming rates 63.9, 75.3 and 74.1 million operations/second.
The commercial CPU name lookup was denied by the local OS; the available CPU
identifier, binary/source fingerprints and compiler flags are retained.

These are machine-specific microbenchmarks with uncontrolled background load.
Serialized latency samples and streaming timing are different measurements.
Streaming uses a different seed and the book after the latency phase; trade
totals include warmup. The SPSC pipeline is unwarmed and starts its producer
before the timer. Ping-pong is an unfenced half-RTT estimate. None establishes
exchange latency, a latency SLA, or a speedup over another implementation.
Console/file IO is outside the timed engine region; pipeline measurements are
separate in the raw capture. Keep all three raw outputs and their variation.
