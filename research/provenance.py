"""Local data provenance manifests: re-hash declared inputs before trusting them.

A manifest records each input file's SHA-256 and byte size, the ordered
transformation list, the experiments that consume the inputs, and a hash of the
canonical manifest body. Verification always re-reads and re-hashes every input;
declared hashes are never accepted on their own. Checksums detect change, not
correctness, authenticity or the truth of the free-text source declaration.

Usage (exit code 0 only when the build succeeds or verification is valid):
  python -m research.provenance build --spec spec.json --output manifest.json [--root DIR]
  python -m research.provenance verify manifest.json [--root DIR]
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import sys
import tempfile

SCHEMA_VERSION = 1
KIND = "research_provenance_manifest"
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_DEPTH = 20
CHUNK = 1024 * 1024
EXPERIMENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
SHA256_HEX = re.compile(r"[0-9a-f]{64}")
LIMITATIONS = [
    "Checksums detect change, not correctness: a matching hash says nothing about data quality.",
    "Source and retrieved_at are free-text declarations; they are recorded, not verified.",
    "An editor who rewrites an input, its declared hash and the manifest hash together is not detected.",
    "Paths are relative to a caller-chosen root; moving files reports them as missing.",
]
_MANIFEST_KEYS = {"schema_version", "kind", "inputs", "transformations", "experiments",
                  "limitations", "manifest_sha256"}
_INPUT_KEYS = {"path", "source", "retrieved_at", "sha256", "bytes"}


class ManifestError(ValueError):
    pass


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def file_digest(path: Path) -> tuple[str, int]:
    """Stream the file; return (sha256 hex, byte count) of the bytes actually read."""
    hasher, size = hashlib.sha256(), 0
    with Path(path).open("rb") as stream:
        while chunk := stream.read(CHUNK):
            hasher.update(chunk)
            size += len(chunk)
    return hasher.hexdigest(), size


def _check_json_value(value, where: str, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise ManifestError(f"{where}: nesting deeper than {MAX_DEPTH}")
    if value is None or type(value) in (bool, int, str):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ManifestError(f"{where}: non-finite number")
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _check_json_value(item, f"{where}[{index}]", depth + 1)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ManifestError(f"{where}: object keys must be strings")
            _check_json_value(item, f"{where}.{key}", depth + 1)
        return
    raise ManifestError(f"{where}: unsupported value type {type(value).__name__}")


def _parameters(value, where: str) -> dict:
    if type(value) is not dict:
        raise ManifestError(f"{where}: parameters must be a JSON object")
    _check_json_value(value, where)
    # Round-trip through sorted canonical JSON so key order never matters.
    return json.loads(canonical_json(value))


def _text(value, where: str, limit: int) -> str:
    if type(value) is not str or not 1 <= len(value) <= limit or "\x00" in value:
        raise ManifestError(f"{where}: must be 1 to {limit} characters")
    return value


def check_relative_path(value, where: str = "path") -> str:
    """Normalized POSIX path relative to the manifest root (leading '..' allowed)."""
    _text(value, where, 1024)
    if ("\\" in value or ":" in value or value.startswith("/")
            or posixpath.normpath(value) != value or value in (".", "..")
            or PurePosixPath(value).name in ("", "..")):
        raise ManifestError(f"{where}: must be a normalized relative POSIX file path")
    return value


def _timestamp(value, where: str) -> str:
    _text(value, where, 64)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        raise ManifestError(f"{where}: must be ISO 8601") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ManifestError(f"{where}: must include a UTC offset")
    return value


def _transformations(items) -> list[dict]:
    if type(items) is not list:
        raise ManifestError("transformations must be a list")
    out = []
    for step, item in enumerate(items):
        if type(item) is not dict or set(item) - {"step"} != {"name", "parameters"}:
            raise ManifestError(f"transformations[{step}]: expected name and parameters")
        if "step" in item and item["step"] != step:
            raise ManifestError(f"transformations[{step}]: step must equal its position")
        out.append({"step": step, "name": _text(item["name"], f"transformations[{step}].name", 200),
                    "parameters": _parameters(item["parameters"], f"transformations[{step}].parameters")})
    return out


def _experiments(value, paths: list[str]) -> list[dict]:
    items = [value] if type(value) is dict else value
    if type(items) is not list or not items:
        raise ManifestError("experiment must be an object or a non-empty list of objects")
    out, seen = [], set()
    for index, item in enumerate(items):
        where = f"experiments[{index}]"
        if type(item) is not dict or not {"id", "parameters"} <= set(item) or set(item) - {"id", "parameters", "inputs"}:
            raise ManifestError(f"{where}: expected id, parameters and optional inputs")
        if type(item["id"]) is not str or not EXPERIMENT_ID.fullmatch(item["id"]):
            raise ManifestError(f"{where}.id: must match {EXPERIMENT_ID.pattern}")
        if item["id"] in seen:
            raise ManifestError(f"{where}.id: duplicate experiment id")
        seen.add(item["id"])
        used = item.get("inputs", paths)
        if type(used) is not list or not used or len(set(used)) != len(used) or any(p not in paths for p in used):
            raise ManifestError(f"{where}.inputs: must be a non-empty list of distinct declared input paths")
        out.append({"id": item["id"], "parameters": _parameters(item["parameters"], f"{where}.parameters"),
                    "inputs": list(used)})
    return out


def _input_declaration(item, index: int) -> dict:
    where = f"inputs[{index}]"
    if type(item) is not dict:
        raise ManifestError(f"{where}: must be an object")
    return {"path": check_relative_path(item.get("path"), f"{where}.path"),
            "source": _text(item.get("source"), f"{where}.source", 1000),
            "retrieved_at": _timestamp(item.get("retrieved_at"), f"{where}.retrieved_at")}


def manifest_digest(manifest: dict) -> str:
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    return hashlib.sha256(canonical_json(body).encode("ascii")).hexdigest()


def build_manifest(inputs, transformations, experiment, *, root: Path | str = ".") -> dict:
    """Hash each declared local input under root and return a self-hashed manifest."""
    if type(inputs) is not list or not inputs:
        raise ManifestError("inputs must be a non-empty list")
    declared = []
    for index, item in enumerate(inputs):
        if type(item) is not dict or set(item) != {"path", "source", "retrieved_at"}:
            raise ManifestError(f"inputs[{index}]: expected exactly path, source and retrieved_at")
        declared.append(_input_declaration(item, index))
    paths = [item["path"] for item in declared]
    if len(set(paths)) != len(paths):
        raise ManifestError("inputs: duplicate path")
    root = Path(root)
    for item in declared:
        target = root / PurePosixPath(item["path"])
        if not target.is_file():
            raise ManifestError(f"input not found: {item['path']}")
        item["sha256"], item["bytes"] = file_digest(target)
    manifest = {"schema_version": SCHEMA_VERSION, "kind": KIND, "inputs": declared,
                "transformations": _transformations(transformations),
                "experiments": _experiments(experiment, paths), "limitations": list(LIMITATIONS)}
    manifest["manifest_sha256"] = manifest_digest(manifest)
    return manifest


def validate_manifest(manifest) -> None:
    """Structural checks only; says nothing about whether inputs still match."""
    if type(manifest) is not dict or set(manifest) != _MANIFEST_KEYS:
        raise ManifestError("manifest must have exactly " + ", ".join(sorted(_MANIFEST_KEYS)))
    if manifest["schema_version"] != SCHEMA_VERSION or manifest["kind"] != KIND:
        raise ManifestError("unsupported manifest schema or kind")
    inputs = manifest["inputs"]
    if type(inputs) is not list or not inputs:
        raise ManifestError("inputs must be a non-empty list")
    for index, item in enumerate(inputs):
        if type(item) is not dict or set(item) != _INPUT_KEYS:
            raise ManifestError(f"inputs[{index}]: expected exactly " + ", ".join(sorted(_INPUT_KEYS)))
        _input_declaration(item, index)
        if type(item["sha256"]) is not str or not SHA256_HEX.fullmatch(item["sha256"]):
            raise ManifestError(f"inputs[{index}].sha256: must be 64 lowercase hex characters")
        if type(item["bytes"]) is not int or item["bytes"] < 0:
            raise ManifestError(f"inputs[{index}].bytes: must be a nonnegative int")
    paths = [item["path"] for item in inputs]
    if len(set(paths)) != len(paths):
        raise ManifestError("inputs: duplicate path")
    _transformations(manifest["transformations"])
    if type(manifest["experiments"]) is not list:
        raise ManifestError("experiments must be a list")
    for index, item in enumerate(manifest["experiments"]):
        if type(item) is not dict or set(item) != {"id", "parameters", "inputs"}:
            raise ManifestError(f"experiments[{index}]: expected exactly id, parameters and inputs")
    _experiments(manifest["experiments"], paths)
    if type(manifest["limitations"]) is not list or not all(type(x) is str for x in manifest["limitations"]):
        raise ManifestError("limitations must be a list of strings")
    if type(manifest["manifest_sha256"]) is not str or not SHA256_HEX.fullmatch(manifest["manifest_sha256"]):
        raise ManifestError("manifest_sha256: must be 64 lowercase hex characters")


def verify_manifest(manifest, root: Path | str) -> dict:
    """Re-hash every input under root; any change, absence or body edit is invalid."""
    validate_manifest(manifest)
    root = Path(root)
    changed, missing = [], []
    for item in manifest["inputs"]:
        target = root / PurePosixPath(item["path"])
        try:
            if not target.is_file():
                raise FileNotFoundError(item["path"])
            actual, size = file_digest(target)
        except OSError:
            missing.append(item["path"])
            continue
        if actual != item["sha256"] or size != item["bytes"]:
            changed.append(item["path"])
    body_ok = manifest_digest(manifest) == manifest["manifest_sha256"]
    bad = set(changed) | set(missing)
    # An edited body cannot be attributed to one experiment, so it affects all of them.
    affected = [item["id"] for item in manifest["experiments"]
                if not body_ok or bad.intersection(item["inputs"])]
    return {"valid": body_ok and not bad, "manifest_hash_ok": body_ok,
            "changed": changed, "missing": missing, "affected_experiments": affected}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(name):
    raise ManifestError(f"non-finite JSON constant {name}")


def load_json(path: Path | str):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ManifestError("JSON document exceeds size limit")
    try:
        return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                          parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ManifestError(f"invalid UTF-8 JSON: {exc}") from None


def write_manifest(manifest: dict, path: Path | str) -> None:
    """Publish a complete manifest without replacing any destination entry."""
    serialized = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n"
    if len(serialized.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise ManifestError("serialized manifest exceeds size limit")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Windows open("x") can follow a dangling symlink. A hard link refuses
    # existing destination entries; staging beside the output keeps one volume.
    with tempfile.TemporaryDirectory(prefix=".provenance-manifest-", dir=path.parent) as folder:
        staged = Path(folder) / "manifest.json"
        with staged.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(serialized)
        os.link(staged, path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m research.provenance", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="hash inputs from a JSON spec and write a new manifest")
    build.add_argument("--spec", required=True, type=Path,
                       help="JSON object with inputs, transformations and experiment")
    build.add_argument("--output", required=True, type=Path, help="new manifest path (never overwritten)")
    build.add_argument("--root", type=Path, help="directory input paths are relative to (default: output's directory)")
    verify = commands.add_parser("verify", help="re-hash every input; exit 0 only when valid")
    verify.add_argument("manifest", type=Path)
    verify.add_argument("--root", type=Path, help="directory input paths are relative to (default: manifest's directory)")
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            spec = load_json(args.spec)
            if type(spec) is not dict or set(spec) != {"inputs", "transformations", "experiment"}:
                raise ManifestError("spec must have exactly inputs, transformations and experiment")
            root = args.root if args.root is not None else args.output.parent
            manifest = build_manifest(spec["inputs"], spec["transformations"], spec["experiment"], root=root)
            write_manifest(manifest, args.output)
            print(json.dumps({"written": str(args.output), "manifest_sha256": manifest["manifest_sha256"]}))
            return 0
        root = args.root if args.root is not None else args.manifest.parent
        report = verify_manifest(load_json(args.manifest), root)
        print(json.dumps(report, indent=2))
        return 0 if report["valid"] else 1
    except FileExistsError:
        print("error: output already exists; use a new path", file=sys.stderr)
    except (ManifestError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
