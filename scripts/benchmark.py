"""Capture raw benchmark evidence with binary/source hashes and build metadata."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import tempfile
from run_study import ROOT, executable, source_hash


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, default=Path("build"))
    parser.add_argument("--config", default="Release")
    parser.add_argument("--output", type=Path, default=Path("build/benchmark.json"))
    parser.add_argument("--targets", nargs="+", default=["signals", "risk", "advanced_orders"],
                        choices=["rdtsc", "itch_pipeline", "zero_copy", "trading_loop", "risk",
                                 "journal", "flat_hash", "signals", "advanced_orders"])
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 100:
        parser.error("--repeats must be 1..100")
    if args.output.exists():
        parser.error("Output exists; choose a new path to preserve previous evidence")
    build = args.build_dir.resolve()
    cache = build / "CMakeCache.txt"
    runs = []
    report = {"recorded_at_utc": datetime.now(timezone.utc).isoformat(),
              "platform": platform.platform(), "processor": platform.processor(),
              "source_sha256": source_hash(), "requested_config": args.config,
              "cmake_cache": cache.read_text() if cache.exists() else None,
              "interpretation": "Machine-specific synthetic microbenchmarks; no latency SLA. "
                                "Retain failed affinity warnings and compare repeated runs on the same host/build.",
              "runs": runs}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        for name in args.targets:
            binary = executable(build, "bench_" + name, args.config)
            for repeat in range(args.repeats):
                # Legacy benchmarks write build/*.bin relative to their cwd.
                # Isolate each run from earlier captures and support custom builds.
                with tempfile.TemporaryDirectory(prefix="lob-benchmark-", dir=build) as temp:
                    (Path(temp) / "build").mkdir()
                    result = subprocess.run([str(binary)], cwd=temp, capture_output=True, text=True, timeout=180)
                runs.append({"target": name, "repeat": repeat, "returncode": result.returncode,
                             "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                             "stdout": result.stdout, "stderr": result.stderr})
                print(f"{name} run {repeat + 1}: exit {result.returncode}")
                if result.returncode:
                    raise ValueError(f"{name} failed; inspect saved evidence")
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        report["error"] = str(error)
    finally:
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if "error" in report:
        parser.error(report["error"])
    print(args.output)


if __name__ == "__main__":
    main()
