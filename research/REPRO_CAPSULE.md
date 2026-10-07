# Repro Capsule: manual local evidence

This small tool packages only text that you explicitly place in a JSON manifest.
It does not run a reproduction, validate claimed results, collect referenced
files, inspect Git or the environment, read credentials, or upload anything.

The manifest must contain a `title` and 1-25 `entries`. Each entry has `kind`
(`command`, `log` or `diff`), `label` and `text`. Commands may also include a
manually reported integer `exit_code`. Unknown fields are rejected. See the
[synthetic example](../tests/fixtures/capsule/manual-example.json); its contents
are illustrative, not evidence that those commands were run.

First edit and inspect your manifest, then preview exactly which entries and
fields will be included:

```sh
python -m research.repro_capsule tests/fixtures/capsule/manual-example.json
```

The preview lists entry indexes, kinds, included fields, text byte counts and
hashes, plus the hash of the exact input bytes. It intentionally does not echo
titles, labels, commands, logs, diffs or matched credential values. Refer to
your input file for the content associated with each index.

After reviewing both input and preview, explicitly confirm that input hash:

```sh
python -m research.repro_capsule my-input.json --write capsule.json --confirm-sha256 HASH_FROM_PREVIEW
```

Export refuses a missing/stale hash, an existing destination (including the
source file), or possible-secret flags. Edit/remove flagged text yourself and
preview again. Nothing is automatically redacted. Output is ordinary local
JSON containing the exact supplied evidence, marked `manually_supplied_unverified`.
It is not encrypted and is not automatically safe to share.

Possible-secret checks cover a few credential assignments, recognizable token
prefixes, authorization strings, private-key headers and credential-bearing
HTTP URLs. They have **false positives and false negatives**; personal data and
other secret formats can pass unnoticed. `can_export` only describes this
tool's flag gate, not a safety guarantee. Every preview explicitly declines to
certify that the input is secret-free. No account or external service is used.

Limits: UTF-8 JSON at most 1 MiB, title/labels at most 200 characters, each text
at most 262,144 characters. Exit 0 means preview/export completed, 1 means
possible-secret flags require review, and 2 means a validation or file error.
The command writes only when both `--write` and the matching confirmation hash
are supplied; it never overwrites an existing file.

Tests cover inclusion preview, unchanged-input confirmation, exact evidence,
non-execution, metadata/content flags, fixed error messages and overwrite refusal:

```sh
python -m unittest discover -s tests -p test_repro_capsule.py -v
```
