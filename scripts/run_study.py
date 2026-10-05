"""Reproduce the synthetic end-to-end study; no downloads or Python dependencies."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def executable(build: Path, name: str, config: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    for path in (build / (name + suffix), build / config / (name + suffix)):
        if path.is_file():
            return path.resolve()
    raise ValueError(f"Missing {name} in {build}; run cmake --build first")


def source_hash() -> str:
    digest = hashlib.sha256()
    paths = [ROOT / "CMakeLists.txt"]
    for folder in ("include", "tools", "research", "scripts", "bench"):
        paths.extend(path for path in (ROOT / folder).rglob("*")
                     if path.suffix in (".hpp", ".cpp", ".py"))
    for path in sorted(paths):
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, default=Path("build"))
    parser.add_argument("--config", default="Release")
    parser.add_argument("--output-dir", type=Path, default=Path("build/study"))
    parser.add_argument("--events", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    try:
        binary = executable(args.build_dir.resolve(), "replay_events", args.config)
        output = args.output_dir.resolve()
        if output.exists():
            raise ValueError(f"Output directory already exists: {output}; use a new directory")
        output.mkdir(parents=True)
        events, bbo = output / "events.csv", output / "bbo.csv"
        commands = [
            [sys.executable, "-m", "research.generate", "--output", str(events),
             "--events", str(args.events), "--seed", str(args.seed)],
            [str(binary), "--input", str(events), "--output", str(bbo)],
            [sys.executable, "-m", "research.lab", "--input", str(bbo),
             "--output", str(output / "report.json"), "--html", str(output / "report.html"),
             "--data-kind", "synthetic", "--horizon", "10", "--delay", "1",
             "--fee-ticks", "0.1", "--seed", str(args.seed)],
        ]
        logs = []
        for command in commands:
            result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=120)
            logs.append({"command": command, "returncode": result.returncode,
                         "stdout": result.stdout, "stderr": result.stderr})
            print(result.stdout, end="")
            if result.returncode:
                (output / "failure.json").write_text(json.dumps(logs, indent=2), encoding="utf-8")
                raise ValueError(result.stderr or f"Command failed: {command}")
        git = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
        manifest = {"data_kind": "synthetic", "seed": args.seed, "events": args.events,
                    "git_base_commit": git.stdout.strip(), "source_sha256": source_hash(),
                    "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                    "platform": platform.platform(), "python": platform.python_version(),
                    "files_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                     for p in sorted(output.iterdir()) if p.is_file()},
                    "steps": logs}
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        print(f"Open {output / 'report.html'}; provenance: {output / 'manifest.json'}")
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
