# Running the benchmarks reproducibly

The benchmarks are RDTSC-instrumented, core-pinned, and spin-wait on each
other, so their output is only as trustworthy as the machine they run on. This
page says how to prepare a machine, what the benchmarks already do for you, and
how `scripts/bench-report.sh` records a run so another person can tell what was
measured and on what.

**No benchmark results are checked in to this repository.** The figures in the
top-level [README](../README.md) (section 4) were measured on a different
machine (Windows x64, GCC 16.1, invariant TSC at 1.498 GHz). They are left
exactly as published, and they **must be regenerated with
`scripts/bench-report.sh` on the target machine** before being quoted or
compared against anything.

## Quick start

```bash
# from the repository root; builds Release, runs the tests, then every bench
scripts/bench-report.sh --warmup 1 --repeats 5

# results land in build/release/bench-results/<utc>-<commit>/
#   report.json   environment + per-run results + per-statistic min/median/max
#   report.csv    one row per (bench, run, section, statistic)
#   raw/          verbatim stdout of every run (the source of every number)
```

Useful options: `--only rdtsc,risk` (subset), `--skip-build`, `--keep-data`,
`-b DIR` (build directory), `-o DIR` (output directory); `CXX=clang++` selects
the compiler for a fresh configure. `scripts/bench-report.sh --help` has the
full list.

The script first runs `test_correctness` and `test_properties` and refuses to
time anything if they fail (latency of a wrong engine is meaningless). It then
runs each `bench_*` binary from a scratch directory (the benches write their
~110 MB feed/journal files to `./build/*.bin`; these are deleted afterwards
unless `--keep-data`). It exits non-zero if a test fails, a benchmark exits
non-zero, or a benchmark prints a failed self-check (`[BROKEN!]`,
`[MISMATCH!]`, `[DIVERGED!]`).

The script only **observes** the machine. It never changes the governor, CPU
affinity or kernel settings; it records them and prints warnings when they look
wrong (governor not `performance`, turbo on, fewer than 5 logical CPUs, running
under a hypervisor, no isolated CPUs, TSC not advertised as constant/nonstop,
TSC calibration drifting between runs).

## What the report records

Everything is read from the machine at run time (`n/a` if unreadable) or parsed
verbatim from benchmark output. Nothing is estimated or filled in.

| Field | Source |
|-------|--------|
| `git_commit`, `git_branch`, `git_dirty` | `git rev-parse`, `git status --porcelain` |
| `compiler_path`, `compiler_version` | `CMakeCache.txt`, `<compiler> --version` |
| `bench_compile_command` | the exact compile command for `bench_rdtsc.cpp` from `compile_commands.json` (flags actually used) |
| `cpu_model`, `logical_cpus`, `arch` | `lscpu` / `/proc/cpuinfo` / `sysctl`, `uname` |
| `cpu_governor`, `intel_no_turbo`, `cpufreq_boost`, `smt_active` | `/sys/devices/system/cpu/...` |
| `isolated_cpus`, `kernel_cmdline`, `thp`, `clocksource` | `/sys/devices/system/cpu/isolated`, `/proc/cmdline`, `/sys/kernel/mm/transparent_hugepage`, `/sys/devices/system/clocksource` |
| `tsc_constant`, `tsc_nonstop`, `rdtscp`, `virtualized` | CPU flags in `/proc/cpuinfo` |
| per run: `exit_code`, `wall_seconds`, `tsc_ghz` | exit status, wall clock, the benchmark's own `TSC calibration:` line |
| per run: `percentiles` | rows labelled `P50 P90 P99 P99.9 P99.99 max` under a `(N samples)` header; both cycles and ns |
| per run: `metric_lines` | lines reporting `ns/op`, `ns/msg`, `ops/sec`, ... copied verbatim |
| per run: `self_checks` | counts of `[VERIFIED]`, `[DETERMINISTIC]`, budget `MEETS`/`MISSES`, and any failed-check lines |

When a row prints two nanosecond values (`bench_rdtsc` prints "raw" and "minus
harness"), the report stores the **last** one, i.e. the value with the
measurement overhead subtracted, which is the figure the top-level README
quotes. The raw output files remain the authority if there is any doubt.

## Preparing the machine (Linux)

Do these on the machine whose numbers you intend to publish. Most need root.
Record what you did next to the report.

1. **Use real hardware, at least 5 logical CPUs.** The benchmarks pin threads to
   logical CPUs 0 (feed/producer), 2 (engine) and 4 (strategy/signals). A pin
   to a CPU that does not exist fails silently and the threads then share
   cores, which produces numbers that look plausible and are wrong. Choose
   CPUs that are **separate physical cores**, not SMT siblings: compare
   `lscpu -e=CPU,CORE,SOCKET` or
   `cat /sys/devices/system/cpu/cpu*/topology/thread_siblings_list`. If 0/2/4
   are siblings on your topology, either disable SMT (below) or change the
   `pin_thread(...)` constants locally and say so.
2. **Fixed clock.** `sudo cpupower frequency-set -g performance` (or write
   `performance` to every `scaling_governor`). Turn turbo off for repeatable
   frequency: `echo 1 | sudo tee /sys/devices/system/cpu/intel_pstate/no_turbo`
   (Intel pstate) or `echo 0 | sudo tee /sys/devices/system/cpu/cpufreq/boost`
   (AMD / acpi-cpufreq). Whether to disable turbo is a judgement call: it
   makes runs repeatable but understates what the part can do. Record the
   choice (the report does).
3. **Isolate the cores.** On the kernel command line:
   `isolcpus=2,4 nohz_full=2,4 rcu_nocbs=2,4` (and 0 as well if you can spare
   it), reboot, and confirm with `cat /sys/devices/system/cpu/isolated`. Move
   IRQs away: `irqaffinity=` on the command line, and stop `irqbalance`
   (`sudo systemctl stop irqbalance`) so it does not re-spread interrupts onto
   the benchmark cores.
4. **Quiesce.** Close browsers/IDEs, stop indexers, timers and cron jobs, keep
   the machine on AC power and cool (thermal throttling shows up as a slow
   drift over a long session). No other benchmark or build running.
5. **SMT / hugepages (optional).** `echo off | sudo tee
   /sys/devices/system/cpu/smt/control` removes sibling interference.
   Transparent hugepages can add compaction stalls; `never` is the
   conservative choice for latency work.
6. **Check the clock source.** `RDTSC` is only a wall-clock proxy on an
   *invariant* TSC: `grep -o -w -e constant_tsc -e nonstop_tsc /proc/cpuinfo |
   sort -u` should list both. In a VM, the TSC can be emulated or scaled and
   guest scheduling is outside your control; treat VM numbers as smoke tests.

On Windows (the platform of the published figures) the equivalent steps are the
High-performance power plan, no other foreground work, and running from an
elevated shell so `pin_thread` can raise the process priority class (it
silently falls back to HIGH otherwise). `scripts/bench-report.sh` is a bash
script for Linux/macOS (Git Bash/WSL are untested); on Windows run the binaries
directly and keep the same provenance by hand.

## What the benchmarks already do

- **Deterministic inputs.** Workloads are generated from fixed seeds
  (`bench_rdtsc`: `0xDEADBEEF` warm-up, `0xC0FFEE` measured; the ITCH feed:
  `0x17C4`), so two runs process the same messages. The `[VERIFIED]` gates
  compare fill counts and traded lots against the generator's ground truth.
- **Pre-touching.** The order arena, ladder, hash map, ring buffers, sample
  vectors and journal mapping are provisioned and touched before measurement,
  so soft page faults do not land inside timed regions.
- **Warm-up inside a run.** `bench_rdtsc` replays a 100,000-op warm-up
  workload to populate the book, fault pages and train branch predictors
  before the 2,000,000 measured ops; the ping-pong benchmark discards its first
  10% of rounds. The `--warmup N` option of the script adds *whole-process*
  warm-up runs (discarded) on top, which settles frequency, page cache and the
  feed file; use at least 1 for published numbers.
- **Measurement overhead.** Samples are `LFENCE; RDTSC` ... `RDTSCP; LFENCE`
  pairs. The pair's own cost is measured at startup as the minimum over 10,000
  empty pairs (`measure_tsc_overhead()`) and subtracted from each sample; when
  an operation is smaller than the clock read itself, the benchmarks time
  batches and divide.

### RDTSC calibration

`lob::calibrate_tsc_ghz()` (`include/lob/rdtsc.hpp`) converts cycles to
nanoseconds. It reads the TSC and `std::chrono::steady_clock` together, sleeps
200 ms, reads both again, and reports `delta_ticks / delta_ns` (ticks per
nanosecond, i.e. GHz). Each benchmark prints the result on its first line
(`TSC calibration: 2.800 GHz`). Things to check:

- On an invariant TSC the value is the **reference/base** frequency, not the
  current core clock, so it should match the nominal base frequency of the CPU
  (a 2.8 GHz part reports about 2.800 even if the core is boosting). A value
  that wanders is a sign of a non-invariant TSC, a VM, or a loaded machine.
- It must be stable across runs. The report stores `tsc_ghz` for every run and
  warns when the spread across repeats exceeds 0.5%.
- Calibration error scales every ns figure by the same factor. It does not
  affect the cycle counts, which are also stored (`cycles`) and are the safer
  basis for comparing two builds on one machine.
- Only the TSC-to-ns conversion is calibrated. Core-clock cycles per
  operation still depend on the frequency the core actually ran at, which is
  why the governor/turbo state is part of the report.

## Reading the results

- **Use `cycles` and ns together, and compare like with like:** same machine,
  same governor/isolation, same compiler and flags (the build uses
  `-O3 -march=native`, so binaries are tied to the CPU they were built on),
  same commit (`git_dirty` should be `false`).
- **Repeat.** With `--repeats 5` the report gives min/median/max of each
  statistic. A median with a wide min-max spread means the machine was noisy,
  not that the code is variable.
- **Believe percentiles up to about P99.9.** Beyond that, spikes are usually
  timer interrupts and preemption (see the tail note in the top-level README);
  isolation and IRQ steering move them, code changes do not.
- **A/B delta lines need isolated cores.** Lines such as `journal cost at the
  percentiles (ON - OFF, ns)` or `sparse-id translation cost (B - A, ns)`
  subtract one run from another. On an unisolated or virtualised machine these
  can come out negative or enormous (the two runs saw different scheduling),
  which means the comparison is invalid, not that one variant is faster.
- **Self-checks are part of the result.** A run with a failed `[VERIFIED]` /
  `[DETERMINISTIC]` gate is discarded regardless of how fast it was.

## What CI does (and does not) do with benchmarks

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) **builds** every
`bench_*` target with GCC and Clang so they cannot rot, but never runs them:
shared CI runners are virtualised, share cores and have no stable clock, so
timings from them would be noise. The functional guarantees (matching, journal
replay determinism, ITCH round trip, ring boundaries) are covered by
`ctest`, which does run in CI, in Release and under AddressSanitizer +
UBSan.
