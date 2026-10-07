"""Manual-input local evidence report. No collection, execution, or uploading."""

import argparse
import hashlib
import json
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


def load_manifest(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise CapsuleError("manifest_exceeds_1_MiB_limit")
    try:
        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise CapsuleError("invalid_utf8_json") from None
    if not isinstance(data, dict) or set(data) != {"title", "entries"}:
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
    return data, digest(raw)


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
    return {"schema_version": 1, "mode": "preview", "manifest_sha256": source_hash,
            "included_top_level_fields": ["title", "entries"], "included_entries": included,
            "possible_secret_flags": flags, "can_export": not flags,
            "certifies_secret_free": False, "limitations": LIMITATIONS}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Preview manually supplied evidence; export only after reviewing the input hash.")
    parser.add_argument("manifest", help="UTF-8 JSON containing title and manual command/log/diff entries")
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
            capsule = {"schema_version": 1, "source_manifest_sha256": source_hash,
                       "evidence_status": "manually_supplied_unverified", "limitations": LIMITATIONS,
                       "declared_evidence": data}
            # Exclusive creation also refuses an existing symlink or the source file.
            with Path(args.write).open("x", encoding="utf-8", newline="\n") as output:
                json.dump(capsule, output, indent=2, ensure_ascii=True)
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
