"""Preview/export boundaries and intentionally limited possible-secret warnings."""

from contextlib import redirect_stdout
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from research.repro_capsule import MAX_BYTES, main

FIXTURE = Path(__file__).parent / "fixtures" / "capsule" / "manual-example.json"


class CapsuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = self.root / "input.json"
        self.output = self.root / "capsule.json"
        self.data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.save()

    def save(self, data=None):
        self.manifest.write_text(json.dumps(self.data if data is None else data), encoding="utf-8")

    def run_cli(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([str(self.manifest), *map(str, args)])
        return code, json.loads(output.getvalue())

    def test_preview_lists_exact_inclusions_without_echo_or_writes(self):
        original = self.manifest.read_bytes()
        code, report = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(report["mode"], "preview")
        self.assertEqual(report["manifest_sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual([entry["kind"] for entry in report["included_entries"]], ["command", "log", "diff"])
        self.assertEqual(report["included_entries"][0]["fields"], ["exit_code", "kind", "label", "text"])
        self.assertNotIn(self.data["title"], json.dumps(report))
        self.assertNotIn(self.data["entries"][0]["text"], json.dumps(report))
        self.assertFalse(report["certifies_secret_free"])
        self.assertFalse(self.output.exists())
        self.assertEqual(self.manifest.read_bytes(), original)

    def test_confirmed_export_preserves_exact_manual_evidence_and_marks_it_unverified(self):
        _, preview = self.run_cli()
        code, report = self.run_cli("--write", self.output, "--confirm-sha256", preview["manifest_sha256"])
        self.assertEqual(code, 0)
        self.assertEqual(report["mode"], "written")
        capsule = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(capsule["declared_evidence"], self.data)
        self.assertEqual(capsule["source_manifest_sha256"], preview["manifest_sha256"])
        self.assertEqual(capsule["evidence_status"], "manually_supplied_unverified")

    def test_missing_or_stale_confirmation_never_writes(self):
        self.assertEqual(self.run_cli("--write", self.output)[0], 2)
        _, preview = self.run_cli()
        self.data["entries"][0]["text"] += " --changed"
        self.save()
        code, report = self.run_cli("--write", self.output, "--confirm-sha256", preview["manifest_sha256"])
        self.assertEqual(code, 2)
        self.assertEqual(report["error"], "preview_confirmation_missing_or_input_changed")
        self.assertFalse(self.output.exists())

    def test_existing_files_and_source_are_never_overwritten(self):
        _, preview = self.run_cli()
        self.output.write_text("preserve", encoding="utf-8")
        before = self.manifest.read_bytes()
        for target in (self.output, self.manifest):
            self.assertEqual(self.run_cli("--write", target, "--confirm-sha256", preview["manifest_sha256"]), (2, {"error": "destination_exists"}))
        self.assertEqual(self.output.read_text(encoding="utf-8"), "preserve")
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_possible_secrets_are_flagged_without_values_and_block_export(self):
        examples = ["password = SYNTHETIC_CREDENTIAL", "Bearer SYNTHETIC_ACCESS_VALUE", "-----BEGIN PRIVATE KEY-----",
                    "https://demo:SYNTHETIC_PASSWORD@example.invalid", "ghp_" + "X" * 20]
        for value in examples:
            with self.subTest(value_type=examples.index(value)):
                self.data["entries"][1]["text"] = "first line\n" + value
                self.save()
                code, preview = self.run_cli()
                self.assertEqual(code, 1)
                self.assertTrue(preview["possible_secret_flags"])
                self.assertEqual(preview["possible_secret_flags"][0]["line"], 2)
                self.assertNotIn(value, json.dumps(preview))
                self.assertEqual(self.run_cli("--write", self.output, "--confirm-sha256", preview["manifest_sha256"])[0], 1)
                self.assertFalse(self.output.exists())

    def test_title_and_labels_are_checked_and_false_positives_are_not_certifications(self):
        self.data["title"] = "password=example-placeholder"
        self.data["entries"][0]["label"] = "token=example-placeholder"
        self.save()
        code, report = self.run_cli()
        self.assertEqual(code, 1)
        self.assertEqual({flag["location"] for flag in report["possible_secret_flags"]}, {"title", "entries[0].label"})
        self.assertTrue(any("false positives" in limit for limit in report["limitations"]))

    def test_commands_and_path_shaped_text_are_inert(self):
        marker = self.root / "executed.txt"
        self.data["entries"][0]["text"] = "python -c \"open(" + repr(str(marker)) + ",'w').write('executed')\""
        self.data["entries"][1]["text"] = str(self.root / "unreadable-file.log")
        self.save()
        _, preview = self.run_cli()
        self.assertEqual(self.run_cli("--write", self.output, "--confirm-sha256", preview["manifest_sha256"])[0], 0)
        self.assertFalse(marker.exists())
        self.assertEqual(json.loads(self.output.read_text(encoding="utf-8"))["declared_evidence"], self.data)

    def test_invalid_inputs_use_fixed_errors_without_echo(self):
        bad = copy.deepcopy(self.data)
        bad["entries"][0]["file"] = "PRIVATE_SENTINEL"
        self.save(bad)
        self.assertEqual(self.run_cli()[0], 2)
        for raw in (b'{"title":"PRIVATE_SENTINEL', b'\xff', b'{"title":"a","title":"b","entries":[]}', b' ' * (MAX_BYTES + 1)):
            self.manifest.write_bytes(raw)
            code, report = self.run_cli()
            self.assertEqual(code, 2)
            self.assertNotIn("PRIVATE_SENTINEL", json.dumps(report))
        self.manifest.unlink()
        self.assertEqual(self.run_cli(), (2, {"error": "local_file_access_failed"}))


if __name__ == "__main__":
    unittest.main()
