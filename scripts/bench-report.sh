#!/usr/bin/env bash
# =============================================================================
# bench-report.sh -- build, run every bench_* target, and write a JSON + CSV
# summary together with the provenance needed to reproduce it.
#
# Nothing here is estimated or hard-coded: every number in the report is
# parsed verbatim from what the benchmark binaries print on THIS machine, and
# every provenance field (commit, compiler, flags, CPU, governor, ...) is read
# from the machine at run time ("n/a" when it cannot be read).
#
# Usage:
#   scripts/bench-report.sh [options]
#
#   -b, --build-dir DIR   CMake build dir (default: <repo>/build/release)
#   -o, --out-dir DIR     output dir (default: <build-dir>/bench-results/<utc>-<sha>)
#   -n, --repeats N       measured runs per benchmark (default: 1)
#   -w, --warmup N        discarded warm-up runs per benchmark (default: 0)
#       --only A,B        run only these benches (names, with or without bench_)
#       --skip-build      do not (re)configure/build; use binaries in build-dir
#       --skip-tests      do not run test_correctness / test_properties first
#       --keep-data       keep the generated feed/journal files (~110 MB)
#   -h, --help            this text
#
# Environment: CXX (compiler for a fresh configure), BENCH_TIMEOUT (seconds per
# run, default 600).
#
# Exit status: 0 only if the tests passed, every benchmark exited 0, and no
# benchmark printed a failed self-check ([BROKEN!] / [MISMATCH!] / [DIVERGED!]).
#
# The script only OBSERVES the machine; it never changes governors, affinity or
# kernel settings. See bench/README.md for how to prepare the machine.
# =============================================================================
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$REPO/build/release"
OUT_DIR=""
REPEATS=1
WARMUP=0
ONLY=""
SKIP_BUILD=0
SKIP_TESTS=0
KEEP_DATA=0
TIMEOUT_S="${BENCH_TIMEOUT:-600}"

usage() { sed -n '2,/^# =====/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; }
die()   { echo "bench-report: $*" >&2; exit 2; }
warn()  { echo "bench-report: WARNING: $*" >&2; WARNINGS+=("$*"); }

WARNINGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        -b|--build-dir) BUILD_DIR="$(realpath -m "${2:?missing value}")"; shift 2 ;;
        -o|--out-dir)   OUT_DIR="$(realpath -m "${2:?missing value}")"; shift 2 ;;
        -n|--repeats)   REPEATS="${2:?missing value}"; shift 2 ;;
        -w|--warmup)    WARMUP="${2:?missing value}"; shift 2 ;;
        --only)         ONLY="${2:?missing value}"; shift 2 ;;
        --skip-build)   SKIP_BUILD=1; shift ;;
        --skip-tests)   SKIP_TESTS=1; shift ;;
        --keep-data)    KEEP_DATA=1; shift ;;
        -h|--help)      usage; exit 0 ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
done
[[ "$REPEATS" =~ ^[1-9][0-9]*$ ]] || die "--repeats must be a positive integer"
[[ "$WARMUP"  =~ ^[0-9]+$      ]] || die "--warmup must be a non-negative integer"
command -v python3 >/dev/null    || die "python3 is required (report generation)"
command -v cmake   >/dev/null    || die "cmake is required"

ncpu() { nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 1; }

# --- benchmark list: one target per bench/bench_*.cpp ------------------------
ALL_BENCHES=()
for f in "$REPO"/bench/bench_*.cpp; do
    b="$(basename "$f" .cpp)"; ALL_BENCHES+=("$b")
done
[[ ${#ALL_BENCHES[@]} -gt 0 ]] || die "no bench/bench_*.cpp found"

BENCHES=()
if [[ -n "$ONLY" ]]; then
    IFS=',' read -r -a want <<<"$ONLY"
    for w in "${want[@]}"; do
        w="bench_${w#bench_}"
        found=0
        for b in "${ALL_BENCHES[@]}"; do [[ "$b" == "$w" ]] && found=1; done
        [[ $found -eq 1 ]] || die "unknown benchmark: $w (have: ${ALL_BENCHES[*]})"
        BENCHES+=("$w")
    done
else
    BENCHES=("${ALL_BENCHES[@]}")
fi

# --- provenance --------------------------------------------------------------
GIT_SHA="n/a"; GIT_SHORT="nogit"; GIT_BRANCH="n/a"; GIT_DIRTY="n/a"
if git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
    GIT_SHA="$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo n/a)"
    GIT_SHORT="$(git -C "$REPO" rev-parse --short=10 HEAD 2>/dev/null || echo nogit)"
    GIT_BRANCH="$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || echo n/a)"
    if [[ -n "$(git -C "$REPO" status --porcelain 2>/dev/null)" ]]; then GIT_DIRTY="true"; else GIT_DIRTY="false"; fi
fi
[[ "$GIT_DIRTY" == "true" ]] && warn "working tree has uncommitted changes; the commit hash does not fully identify this build"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
[[ -n "$OUT_DIR" ]] || OUT_DIR="$BUILD_DIR/bench-results/$STAMP-$GIT_SHORT"
RAW_DIR="$OUT_DIR/raw"
WORK_DIR="$OUT_DIR/work"          # cwd for the benches: they use ./build/*.bin
mkdir -p "$RAW_DIR" "$WORK_DIR/build"
ENV_FILE="$OUT_DIR/env.txt"; : >"$ENV_FILE"
RUNS_TSV="$OUT_DIR/runs.tsv"; : >"$RUNS_TSV"

kv() { # kv key value   (single line, tabs/newlines flattened)
    local v="${2//$'\n'/ }"; v="${v//$'\t'/ }"
    printf '%s=%s\n' "$1" "$v" >>"$ENV_FILE"
}
read_file() { [[ -r "$1" ]] && tr -d '\n' <"$1" || echo "n/a"; }

# --- build -------------------------------------------------------------------
if [[ $SKIP_BUILD -eq 0 ]]; then
    echo "== configure + build (Release) in $BUILD_DIR"
    cmake -S "$REPO" -B "$BUILD_DIR" -DCMAKE_BUILD_TYPE=Release -DLOB_SANITIZE=OFF \
          -DCMAKE_EXPORT_COMPILE_COMMANDS=ON ${CXX:+-DCMAKE_CXX_COMPILER="$CXX"} >/dev/null
    cmake --build "$BUILD_DIR" --parallel "$(ncpu)" \
          --target test_correctness test_properties "${BENCHES[@]}" >"$OUT_DIR/build.log" 2>&1 \
        || { tail -30 "$OUT_DIR/build.log" >&2; die "build failed (full log: $OUT_DIR/build.log)"; }
fi
for b in "${BENCHES[@]}"; do
    [[ -x "$BUILD_DIR/$b" ]] || die "missing binary $BUILD_DIR/$b (drop --skip-build?)"
done

CACHE="$BUILD_DIR/CMakeCache.txt"
cache_val() { grep -m1 "^$1:" "$CACHE" 2>/dev/null | cut -d= -f2- || true; }
CXX_PATH="$(cache_val CMAKE_CXX_COMPILER)"; [[ -n "$CXX_PATH" ]] || CXX_PATH="n/a"
CXX_VERSION="n/a"
[[ "$CXX_PATH" != "n/a" ]] && CXX_VERSION="$("$CXX_PATH" --version 2>&1 | head -1 || echo n/a)"

# --- machine description (read, never written) -------------------------------
cpu_model="n/a"
if command -v lscpu >/dev/null 2>&1; then
    cpu_model="$(LC_ALL=C lscpu | sed -n 's/^Model name:[[:space:]]*//p' | head -1)"
fi
if [[ -z "$cpu_model" || "$cpu_model" == "n/a" ]] && [[ -r /proc/cpuinfo ]]; then
    cpu_model="$(sed -n 's/^model name[[:space:]]*:[[:space:]]*//p' /proc/cpuinfo | head -1)"
fi
if [[ -z "$cpu_model" || "$cpu_model" == "n/a" ]] && command -v sysctl >/dev/null 2>&1; then
    cpu_model="$(sysctl -n machdep.cpu.brand_string 2>/dev/null || true)"
fi
[[ -n "$cpu_model" ]] || cpu_model="n/a"

CPU_FLAGS="$(sed -n 's/^flags[[:space:]]*:[[:space:]]*//p' /proc/cpuinfo 2>/dev/null | head -1 || true)"
has_flag() { [[ " $CPU_FLAGS " == *" $1 "* ]] && echo yes || echo no; }
NCPU="$(ncpu)"
GOV="$(read_file /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor)"
NO_TURBO="$(read_file /sys/devices/system/cpu/intel_pstate/no_turbo)"
BOOST="$(read_file /sys/devices/system/cpu/cpufreq/boost)"
SMT="$(read_file /sys/devices/system/cpu/smt/active)"
CMDLINE="n/a"; [[ -r /proc/cmdline ]] && CMDLINE="$(cat /proc/cmdline)"
ISOLATED="$(read_file /sys/devices/system/cpu/isolated)"
THP="$(read_file /sys/kernel/mm/transparent_hugepage/enabled)"
CLOCKSRC="$(read_file /sys/devices/system/clocksource/clocksource0/current_clocksource)"
OS_NAME="$(. /etc/os-release 2>/dev/null && echo "${PRETTY_NAME:-n/a}" || uname -s)"
VIRT="$([[ "$(has_flag hypervisor)" == yes ]] && echo yes || echo no/unknown)"

kv generated_utc       "$STAMP"
kv git_commit          "$GIT_SHA"
kv git_branch          "$GIT_BRANCH"
kv git_dirty           "$GIT_DIRTY"
kv compiler_path       "$CXX_PATH"
kv compiler_version    "$CXX_VERSION"
kv cmake_version       "$(cmake --version | head -1)"
kv cmake_build_type    "$(cache_val CMAKE_BUILD_TYPE)"
kv cmake_cxx_flags     "$(cache_val CMAKE_CXX_FLAGS) $(cache_val CMAKE_CXX_FLAGS_RELEASE)"
kv compile_commands    "$BUILD_DIR/compile_commands.json"
kv cpu_model           "$cpu_model"
kv logical_cpus        "$NCPU"
kv os                  "$OS_NAME"
kv kernel              "$(uname -sr)"
kv arch                "$(uname -m)"
kv virtualized         "$VIRT"
kv cpu_governor        "$GOV"
kv intel_no_turbo      "$NO_TURBO"
kv cpufreq_boost       "$BOOST"
kv smt_active          "$SMT"
kv isolated_cpus       "$ISOLATED"
kv kernel_cmdline      "$CMDLINE"
kv thp                 "$THP"
kv clocksource         "$CLOCKSRC"
kv tsc_constant        "$(has_flag constant_tsc)"
kv tsc_nonstop         "$(has_flag nonstop_tsc)"
kv rdtscp              "$(has_flag rdtscp)"
kv repeats             "$REPEATS"
kv warmup_runs         "$WARMUP"
kv timeout_seconds     "$TIMEOUT_S"

# --- advisory checks (the script reports; it does not "fix") -----------------
[[ "$GOV" != "n/a" && "$GOV" != "performance" ]] && warn "cpu governor is '$GOV', not 'performance': clocks will vary run to run"
[[ "$NO_TURBO" == "0" ]] && warn "turbo boost is enabled: frequency (and thus latency) will drift with load/thermals"
[[ "$GOV" == "n/a" ]] && warn "cpufreq governor not readable here (VM/container/non-Linux): frequency scaling state is unknown"
[[ "$VIRT" == "yes" ]] && warn "running under a hypervisor: TSC, scheduling and interrupts are not under your control"
[[ "$NCPU" -lt 5 ]] && warn "only $NCPU logical CPUs: the benches pin threads to cores 0, 2 and 4; pins to missing cores silently fail and threads will share cores, so cross-core numbers are not meaningful"
[[ "$(has_flag constant_tsc)" == no || "$(has_flag nonstop_tsc)" == no ]] && [[ -n "$CPU_FLAGS" ]] && warn "CPU does not advertise constant_tsc+nonstop_tsc: RDTSC may not be a wall-clock proxy"
[[ "$ISOLATED" == "n/a" || -z "$ISOLATED" ]] && warn "no isolated CPUs (isolcpus): scheduler ticks and other tasks can preempt the pinned benchmark threads"

# --- tests first: timings of a wrong engine are meaningless ------------------
TESTS_STATUS="skipped"
if [[ $SKIP_TESTS -eq 0 ]]; then
    echo "== tests"
    TESTS_STATUS="pass"
    for t in test_correctness test_properties; do
        if ! "$BUILD_DIR/$t" >"$RAW_DIR/$t.txt" 2>&1; then
            TESTS_STATUS="FAIL"; echo "bench-report: $t FAILED (see $RAW_DIR/$t.txt)" >&2
        fi
    done
fi

# --- run ---------------------------------------------------------------------
TIMEOUT_CMD=()
if   command -v timeout  >/dev/null 2>&1; then TIMEOUT_CMD=(timeout "$TIMEOUT_S")
elif command -v gtimeout >/dev/null 2>&1; then TIMEOUT_CMD=(gtimeout "$TIMEOUT_S"); fi

now() { if [[ -n "${EPOCHREALTIME:-}" ]]; then echo "${EPOCHREALTIME/,/.}"; else date +%s; fi; }

ANY_FAIL=0
[[ "$TESTS_STATUS" == "FAIL" ]] && ANY_FAIL=1
if [[ "$TESTS_STATUS" != "FAIL" ]]; then
    for b in "${BENCHES[@]}"; do
        for ((w = 1; w <= WARMUP; w++)); do
            echo "== $b (warm-up $w/$WARMUP, discarded)"
            (cd "$WORK_DIR" && "${TIMEOUT_CMD[@]}" "$BUILD_DIR/$b" >/dev/null 2>&1) || true
        done
        for ((k = 1; k <= REPEATS; k++)); do
            echo "== $b (run $k/$REPEATS)"
            raw="$RAW_DIR/$b.run$k.txt"
            t0="$(now)"
            set +e
            (cd "$WORK_DIR" && "${TIMEOUT_CMD[@]}" "$BUILD_DIR/$b") >"$raw" 2>&1
            rc=$?
            set -e
            t1="$(now)"
            secs="$(python3 -I -c 'import sys; print(f"{float(sys.argv[2])-float(sys.argv[1]):.3f}")' "$t0" "$t1")"
            printf '%s\t%s\t%s\t%s\t%s\n' "$b" "$k" "$rc" "$secs" "$raw" >>"$RUNS_TSV"
            [[ $rc -ne 0 ]] && { ANY_FAIL=1; echo "bench-report: $b run $k exited $rc" >&2; }
            [[ $KEEP_DATA -eq 0 ]] && rm -f "$WORK_DIR"/build/*.bin
        done
    done
fi
[[ $KEEP_DATA -eq 0 ]] && rm -rf "$WORK_DIR"

# --- summarise (parse only what was printed) ---------------------------------
printf '%s\n' "${WARNINGS[@]+"${WARNINGS[@]}"}" >"$OUT_DIR/warnings.txt"
python3 -I - "$OUT_DIR" "$TESTS_STATUS" <<'PY' || ANY_FAIL=1
import csv, json, re, statistics, sys
from pathlib import Path

out = Path(sys.argv[1])
tests_status = sys.argv[2]

env = {}
for line in (out / "env.txt").read_text().splitlines():
    k, _, v = line.partition("=")
    env[k] = v

# Exact compile command of a bench translation unit (flags actually used).
flags = "n/a"
try:
    cc = json.loads(Path(env["compile_commands"]).read_text())
    for e in cc:
        if e["file"].endswith("bench/bench_rdtsc.cpp"):
            flags = e.get("command") or " ".join(e.get("arguments", []))
            break
except Exception:
    pass
env["bench_compile_command"] = flags

SECTION = re.compile(r"^==\s*(.+?)\s*==\s*$")
SAMPLES = re.compile(r"^\s+(.+?)\s*\((\d+) samples[^)]*\):\s*$")
ROW     = re.compile(r"^\s+(P50|P90|P99\.99|P99\.9|P99|max)(?=\s)")
NUM_NS  = re.compile(r"(\d+(?:\.\d+)?)\s*ns")
NUM_CYC = re.compile(r"(\d+)\s*cyc")
TSC     = re.compile(r"TSC calibration:\s*([0-9.]+)\s*GHz")
BAD     = re.compile(r"\[(BROKEN|MISMATCH|DIVERGED)!\]")
METRIC  = re.compile(r"ns/op|ns/msg|ns/order|cycles/order|ops/sec|msgs/sec|msgs/s\b|per \d+-byte frame")

def parse(path):
    heading, name = "", ""
    pct, metrics = [], []
    tsc = None
    verified = determ = meets = misses = 0
    failures = []
    for line in Path(path).read_text(errors="replace").splitlines():
        if (m := TSC.search(line)) and tsc is None:
            tsc = float(m.group(1))
        if (m := SECTION.match(line)):
            heading, name = m.group(1), ""
            continue
        if (m := SAMPLES.match(line)):
            name = m.group(1)
            continue
        if (m := ROW.match(line)) and name:
            ns, cyc = NUM_NS.findall(line), NUM_CYC.findall(line)
            if ns:   # last value on the row = harness-overhead-subtracted where two are printed
                pct.append({"section": f"{heading} :: {name}", "stat": m.group(1),
                            "cycles": int(cyc[-1]) if cyc else None, "ns": float(ns[-1])})
            continue
        if METRIC.search(line):
            metrics.append(line.strip())
        verified += line.count("[VERIFIED]")
        determ   += line.count("[DETERMINISTIC]")
        meets    += line.count("MEETS")
        misses   += line.count("MISSES")
        if BAD.search(line):
            failures.append(line.strip())
    return {"tsc_ghz": tsc, "percentiles": pct, "metric_lines": metrics,
            "self_checks": {"verified": verified, "deterministic": determ,
                            "budget_meets": meets, "budget_misses": misses,
                            "failed": failures}}

benchmarks, any_bad = {}, False
for line in (out / "runs.tsv").read_text().splitlines():
    bench, run, rc, secs, raw = line.split("\t")
    r = {"run": int(run), "exit_code": int(rc), "wall_seconds": float(secs),
         "raw_output": str(Path(raw).relative_to(out))}
    r.update(parse(raw))
    any_bad |= r["exit_code"] != 0 or bool(r["self_checks"]["failed"])
    benchmarks.setdefault(bench, {"runs": []})["runs"].append(r)

for bench, d in benchmarks.items():
    groups = {}
    for r in d["runs"]:
        for p in r["percentiles"]:
            groups.setdefault((p["section"], p["stat"]), []).append(p["ns"])
    d["aggregate_ns"] = [
        {"section": s, "stat": st, "n": len(v), "median": statistics.median(v),
         "min": min(v), "max": max(v)} for (s, st), v in groups.items()]
    ghz = [r["tsc_ghz"] for r in d["runs"] if r["tsc_ghz"]]
    d["tsc_ghz_spread_pct"] = (100.0 * (max(ghz) - min(ghz)) / min(ghz)) if len(ghz) > 1 else None

warnings = [w for w in (out / "warnings.txt").read_text().splitlines() if w]
for bench, d in benchmarks.items():
    sp = d["tsc_ghz_spread_pct"]
    if sp is not None and sp > 0.5:
        warnings.append(f"{bench}: TSC calibration varied by {sp:.2f}% across runs; latencies in ns are less trustworthy")

report = {
    "schema": 1,
    "note": ("Every figure is parsed verbatim from benchmark stdout on the machine "
             "described in 'environment'. Nothing is estimated. Compare only reports "
             "from the same hardware/commit/flags; regenerate on the target machine."),
    "environment": env,
    "warnings": warnings,
    "tests": tests_status,
    "benchmarks": benchmarks,
}
(out / "report.json").write_text(json.dumps(report, indent=2) + "\n")

with open(out / "report.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["git_commit", "git_dirty", "compiler", "cpu_model", "bench", "run",
                "section", "stat", "cycles", "ns"])
    for bench, d in benchmarks.items():
        for r in d["runs"]:
            for p in r["percentiles"]:
                w.writerow([env.get("git_commit"), env.get("git_dirty"),
                            env.get("compiler_version"), env.get("cpu_model"), bench,
                            r["run"], p["section"], p["stat"],
                            "" if p["cycles"] is None else p["cycles"], p["ns"]])

n_rows = sum(len(r["percentiles"]) for d in benchmarks.values() for r in d["runs"])
print(f"\nreport: {out/'report.json'}\n        {out/'report.csv'}  ({n_rows} percentile rows)")
print(f"tests: {tests_status}   benchmarks: {len(benchmarks)}   "
      f"self-check failures or non-zero exits: {'YES' if any_bad else 'none'}")
for w in warnings:
    print("  warning:", w)
sys.exit(1 if any_bad or tests_status == "FAIL" else 0)
PY
exit "$ANY_FAIL"
