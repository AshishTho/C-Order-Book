"""Manual-input local evidence report. No collection, execution, or uploading."""

import argparse
import hashlib
import json
import math
from pathlib import Path
import re

MAX_BYTES = 1024 * 1024
PATTERNS = [
    ("private_key", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
    ("credential_assignment", re.compile(r'''(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|client[_-]?secret|secret|token)["']?\s*[:=]\s*["']?[^\s,;"']{4,}''')),
    ("authorization_header", re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9_+/=.-]{8,}")),
    ("token_prefix", re.compile(r"\b(?:ghp_|github_pat_|sk-)[A-Za-z0-9_-]{12,}")),
    ("url_credentials", re.compile(r"(?i)https?://[^\s/:@]+:[^\s/@]+@")),
]
LIMITATIONS = [
    "Manual claims only: commands were not run and outcomes were not verified.",
    "Possible-secret heuristics have false positives and miss secrets, personal data and novel formats.",
    "Metadata preview omits supplied text and labels; manually inspect the input before confirming its hash.",
    "No automatic redaction, file collection, environment capture, upload or reproducibility certification.",
]
EXPERIMENT_LIMITATION = ("Declared evidence SHA-256 hashes have not been checked against any files; "
                         "labels are inert text and named paths were not read.")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
SHA256_HEX = re.compile(r"[0-9a-f]{64}")


class CapsuleError(ValueError):
    pass


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CapsuleError("duplicate_json_key")
        result[key] = value
    return result


def reject_constant(_name):
    raise CapsuleError("nonfinite_json_constant")


def finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise CapsuleError("nonfinite_json_number")
    return value


def canonical_parameters(parameters):
    return json.dumps(parameters, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def validate_experiment(experiment):
    if not isinstance(experiment, dict) or set(experiment) != {"parameters", "evidence"}:
        raise CapsuleError("experiment_fields_must_be_parameters_and_evidence")
    parameters = experiment["parameters"]
    if not isinstance(parameters, dict) or not 1 <= len(parameters) <= 50:
        raise CapsuleError("expected_1_to_50_parameters")
    for key, value in parameters.items():
        if not IDENTIFIER.fullmatch(key):
            raise CapsuleError("parameter_name_must_be_identifier")
        if value is None or type(value) in (bool, int):
            continue
        if type(value) is float and math.isfinite(value):
            continue
        if type(value) is str and len(value) <= 1000:
            continue
        raise CapsuleError("parameter_value_must_be_finite_scalar_or_string_up_to_1000")
    evidence = experiment["evidence"]
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 25:
        raise CapsuleError("expected_1_to_25_evidence_items")
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {"label", "sha256"}:
            raise CapsuleError("evidence_fields_must_be_label_and_sha256")
        if not isinstance(item["label"], str) or not 1 <= len(item["label"]) <= 200:
            raise CapsuleError("evidence_label_must_be_1_to_200_characters")
        if not isinstance(item["sha256"], str) or not SHA256_HEX.fullmatch(item["sha256"]):
            raise CapsuleError("evidence_sha256_must_be_64_lowercase_hex")


def load_manifest(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise CapsuleError("manifest_exceeds_1_MiB_limit")
    try:
        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_object,
                          parse_constant=reject_constant, parse_float=finite_float)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise CapsuleError("invalid_utf8_json") from None
    if not isinstance(data, dict) or not {"title", "entries"} <= set(data) or set(data) - {"title", "entries", "experiment"}:
        raise CapsuleError("expected_title_and_entries_only")
    if not isinstance(data["title"], str) or not 1 <= len(data["title"]) <= 200:
        raise CapsuleError("title_must_be_1_to_200_characters")
    entries = data["entries"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 25:
        raise CapsuleError("expected_1_to_25_entries")
    for entry in entries:
        if not isinstance(entry, dict) or not {"kind", "label", "text"} <= set(entry) or set(entry) - {"kind", "label", "text", "exit_code"}:
            raise CapsuleError("entry_fields_must_be_kind_label_text_and_optional_exit_code")
        if entry["kind"] not in ("command", "log", "diff"):
            raise CapsuleError("unsupported_entry_kind")
        if not isinstance(entry["label"], str) or not 1 <= len(entry["label"]) <= 200:
            raise CapsuleError("label_must_be_1_to_200_characters")
        if not isinstance(entry["text"], str) or not 1 <= len(entry["text"]) <= 262144:
            raise CapsuleError("text_must_be_1_to_262144_characters")
        if "exit_code" in entry and (entry["kind"] != "command" or type(entry["exit_code"]) is not int or not -2147483648 <= entry["exit_code"] <= 2147483647):
            raise CapsuleError("exit_code_requires_command_and_32_bit_integer")
    if "experiment" in data:
        validate_experiment(data["experiment"])
    return data, digest(raw)


def schema_version(data):
    return 2 if "experiment" in data else 1


def limitations(data):
    return LIMITATIONS + [EXPERIMENT_LIMITATION] if "experiment" in data else LIMITATIONS


def preview(data, source_hash):
    flags, included = [], []

    def inspect(text, location):
        for name, pattern in PATTERNS:
            match = pattern.search(text)
            if match:
                flags.append({"location": location, "line": text.count("\n", 0, match.start()) + 1, "kind": name})

    inspect(data["title"], "title")
    for index, entry in enumerate(data["entries"]):
        for field in ("label", "text"):
            inspect(entry[field], f"entries[{index}].{field}")
        included.append({"index": index, "kind": entry["kind"], "fields": sorted(entry),
                         "text_bytes": len(entry["text"].encode("utf-8")),
                         "text_sha256": digest(entry["text"].encode("utf-8"))})
    report = {"schema_version": schema_version(data), "mode": "preview", "manifest_sha256": source_hash,
              "included_top_level_fields": ["title", "entries"], "included_entries": included}
    if "experiment" in data:
        experiment = data["experiment"]
        canonical = canonical_parameters(experiment["parameters"])
        inspect(canonical, "experiment.parameters")
        for index, item in enumerate(experiment["evidence"]):
            inspect(item["label"], f"experiment.evidence[{index}].label")
        report["included_top_level_fields"].append("experiment")
        # Names, values and labels are deliberately omitted; only counts and hashes are shown.
        report["experiment"] = {"parameter_count": len(experiment["parameters"]),
                                "parameters_canonical_sha256": digest(canonical.encode("utf-8")),
                                "declared_evidence": [{"index": index, "declared_sha256": item["sha256"]}
                                                      for index, item in enumerate(experiment["evidence"])],
                                "evidence_hashes_verified": False}
    report.update({"possible_secret_flags": flags, "can_export": not flags,
                   "certifies_secret_free": False, "limitations": limitations(data)})
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Preview manually supplied evidence; export only after reviewing the input hash.")
    parser.add_argument("manifest", help="UTF-8 JSON containing title, manual command/log/diff entries and an optional declared experiment")
    parser.add_argument("--write", help="Create a new local JSON report; existing files are never overwritten")
    parser.add_argument("--confirm-sha256", help="Exact manifest hash from a prior preview")
    args = parser.parse_args(argv)
    try:
        data, source_hash = load_manifest(args.manifest)
        report = preview(data, source_hash)
        if args.confirm_sha256 and not args.write:
            raise CapsuleError("confirmation_requires_write_destination")
        if args.write:
            if args.confirm_sha256 != source_hash:
                raise CapsuleError("preview_confirmation_missing_or_input_changed")
            if report["possible_secret_flags"]:
                print(json.dumps(report, indent=2))
                return 1
            capsule = {"schema_version": schema_version(data), "source_manifest_sha256": source_hash,
                       "evidence_status": "manually_supplied_unverified", "limitations": limitations(data),
                       "declared_evidence": data}
            if "experiment" in data:
                capsule["experiment_evidence_hash_status"] = "declared_not_checked_against_files"
            # Exclusive creation also refuses an existing symlink or the source file.
            with Path(args.write).open("x", encoding="utf-8", newline="\n") as output:
                json.dump(capsule, output, indent=2, ensure_ascii=True, allow_nan=False)
                output.write("\n")
            report["mode"] = "written"
        print(json.dumps(report, indent=2, ensure_ascii=True))
        return 1 if report["possible_secret_flags"] else 0
    except FileExistsError:
        error = "destination_exists"
    except OSError:
        error = "local_file_access_failed"
    except CapsuleError as failure:
        error = str(failure)
    except (ValueError, TypeError, RecursionError):
        # Parser/encoding exceptions may contain input; emit only a fixed code.
        error = "invalid_manifest"
    print(json.dumps({"error": error}))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
