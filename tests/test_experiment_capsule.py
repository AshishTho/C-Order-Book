import hashlib
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research import repro_capsule  # noqa: E402

HASH_A = "a" * 64
HASH_B = "0123456789abcdef" * 4
ENTRIES = [{"kind": "log", "label": "run log", "text": "ok"}]


def manifest(**experiment):
    data = {"title": "Capsule", "entries": ENTRIES}
    if experiment:
        data["experiment"] = experiment
    return data


class ExperimentCapsuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write_input(self, content, name="input.json"):
        path = self.root / name
        raw = content if isinstance(content, bytes) else (content if isinstance(content, str) else json.dumps(content)).encode("utf-8")
        path.write_bytes(raw)
        return path

    def run_cli(self, *argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = repro_capsule.main([str(arg) for arg in argv])
        return code, buffer.getvalue()

    def preview_hash(self, path):
        code, out = self.run_cli(path)
        return code, json.loads(out)

    def test_legacy_manifest_unchanged(self):
        path = self.write_input(manifest())
        code, report = self.preview_hash(path)
        self.assertEqual(code, 0)
        self.assertEqual(report["schema_version"], 1)
        self.assertNotIn("experiment", report)
        self.assertEqual(report["included_top_level_fields"], ["title", "entries"])
        self.assertEqual(report["limitations"], repro_capsule.LIMITATIONS)
        dest = self.root / "out.json"
        code, _ = self.run_cli(path, "--write", dest, "--confirm-sha256", report["manifest_sha256"])
        self.assertEqual(code, 0)
        capsule = json.loads(dest.read_text(encoding="utf-8"))
        self.assertEqual(capsule["schema_version"], 1)
        self.assertEqual(capsule["declared_evidence"], manifest())

    def test_experiment_roundtrip_and_preview_does_not_echo(self):
        parameters = {"zqLearningRate": 0.25, "zqSeedName": "zqValueMarker", "zqFlag": True,
                      "zqNothing": None, "zqEpochs": 12}
        evidence = [{"label": "zqLabelMarker", "sha256": HASH_A}, {"label": "other", "sha256": HASH_B}]
        data = manifest(parameters=parameters, evidence=evidence)
        path = self.write_input(data)
        code, out = self.run_cli(path)
        self.assertEqual(code, 0)
        self.assertNotIn("zq", out)
        report = json.loads(out)
        self.assertEqual(report["schema_version"], 2)
        canonical = json.dumps(parameters, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        self.assertEqual(report["experiment"]["parameter_count"], 5)
        self.assertEqual(report["experiment"]["parameters_canonical_sha256"], hashlib.sha256(canonical.encode()).hexdigest())
        self.assertEqual(report["experiment"]["declared_evidence"],
                         [{"index": 0, "declared_sha256": HASH_A}, {"index": 1, "declared_sha256": HASH_B}])
        self.assertFalse(report["experiment"]["evidence_hashes_verified"])
        self.assertIn(repro_capsule.EXPERIMENT_LIMITATION, report["limitations"])
        dest = self.root / "out.json"
        code, _ = self.run_cli(path, "--write", dest, "--confirm-sha256", report["manifest_sha256"])
        self.assertEqual(code, 0)
        capsule = json.loads(dest.read_text(encoding="utf-8"))
        self.assertEqual(capsule["schema_version"], 2)
        self.assertEqual(capsule["declared_evidence"]["experiment"], data["experiment"])
        self.assertIn(repro_capsule.EXPERIMENT_LIMITATION, capsule["limitations"])

    def test_invalid_inputs_rejected(self):
        good_evidence = [{"label": "x", "sha256": HASH_A}]
        bad = [
            manifest(parameters={"a": {"nested": 1}}, evidence=good_evidence),
            manifest(parameters={"a": [1]}, evidence=good_evidence),
            manifest(parameters={}, evidence=good_evidence),
            manifest(parameters={"1bad": 1}, evidence=good_evidence),
            manifest(parameters={"a": "x" * 1001}, evidence=good_evidence),
            manifest(parameters={f"p{i}": i for i in range(51)}, evidence=good_evidence),
            manifest(parameters={"a": 1}, evidence=[]),
            manifest(parameters={"a": 1}, evidence=[{"label": "x", "sha256": "A" * 64}]),
            manifest(parameters={"a": 1}, evidence=[{"label": "", "sha256": HASH_A}]),
            manifest(parameters={"a": 1}, evidence=[{"label": "x", "sha256": HASH_A, "path": "p"}]),
            manifest(parameters={"a": 1}, evidence=good_evidence, extra=1),
            dict(manifest(), unknown=1),
        ]
        raw_bad = [
            '{"title":"t","entries":[{"kind":"log","label":"l","text":"x"}],"experiment":{"parameters":{"a":NaN},"evidence":[{"label":"x","sha256":"%s"}]}}' % HASH_A,
            '{"title":"t","entries":[{"kind":"log","label":"l","text":"x"}],"experiment":{"parameters":{"a":-Infinity},"evidence":[{"label":"x","sha256":"%s"}]}}' % HASH_A,
            '{"title":"t","entries":[{"kind":"log","label":"l","text":"x"}],"experiment":{"parameters":{"a":1e999},"evidence":[{"label":"x","sha256":"%s"}]}}' % HASH_A,
            '{"title":"t","entries":[{"kind":"command","label":"l","text":"x","exit_code":Infinity}]}',
            '{"title":"t","entries":[{"kind":"log","label":"l","text":"x"}],"experiment":{"parameters":{"a":1,"a":2},"evidence":[{"label":"x","sha256":"%s"}]}}' % HASH_A,
            b"{" + b" " * (1024 * 1024) + b"}",
        ]
        for index, content in enumerate(bad + raw_bad):
            with self.subTest(index=index):
                path = self.write_input(content, f"bad{index}.json")
                code, out = self.run_cli(path)
                self.assertEqual(code, 2)
                self.assertIn("error", json.loads(out))

    def test_secret_parameters_and_labels_block_write(self):
        cases = [
            manifest(parameters={"password": "fakeHunter2Value"}, evidence=[{"label": "x", "sha256": HASH_A}]),
            manifest(parameters={"a": 1}, evidence=[{"label": "token=FAKEabcd1234", "sha256": HASH_A}]),
        ]
        for index, data in enumerate(cases):
            with self.subTest(index=index):
                path = self.write_input(data, f"secret{index}.json")
                code, report = self.preview_hash(path)
                self.assertEqual(code, 1)
                self.assertTrue(report["possible_secret_flags"])
                self.assertTrue(report["possible_secret_flags"][0]["location"].startswith("experiment."))
                self.assertFalse(report["can_export"])
                dest = self.root / f"secret_out{index}.json"
                code, out = self.run_cli(path, "--write", dest, "--confirm-sha256", report["manifest_sha256"])
                self.assertEqual(code, 1)
                self.assertNotIn("fakeHunter2Value", out)
                self.assertFalse(dest.exists())

    def test_changed_parameters_invalidate_confirmation(self):
        evidence = [{"label": "x", "sha256": HASH_A}]
        path = self.write_input(manifest(parameters={"rate": 1}, evidence=evidence))
        _, report = self.preview_hash(path)
        self.write_input(manifest(parameters={"rate": 2}, evidence=evidence))
        dest = self.root / "out.json"
        code, out = self.run_cli(path, "--write", dest, "--confirm-sha256", report["manifest_sha256"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out), {"error": "preview_confirmation_missing_or_input_changed"})
        self.assertFalse(dest.exists())

    def test_path_and_command_labels_are_inert(self):
        target = self.root / "real_file.txt"
        target.write_text("content whose hash differs", encoding="utf-8")
        marker = self.root / "marker"
        evidence = [{"label": str(target), "sha256": "0" * 64},
                    {"label": f"touch {marker}", "sha256": HASH_A}]
        path = self.write_input(manifest(parameters={"cmd": f"echo hi > {marker}"}, evidence=evidence))
        boom = mock.Mock(side_effect=AssertionError("no execution allowed"))
        with mock.patch("subprocess.run", boom), mock.patch("subprocess.Popen", boom), mock.patch.object(os, "system", boom):
            code, report = self.preview_hash(path)
            dest = self.root / "out.json"
            write_code, _ = self.run_cli(path, "--write", dest, "--confirm-sha256", report["manifest_sha256"])
        self.assertEqual(code, 0)
        self.assertEqual(write_code, 0)
        boom.assert_not_called()
        self.assertFalse(marker.exists())
        self.assertEqual(report["experiment"]["declared_evidence"][0]["declared_sha256"], "0" * 64)
        self.assertFalse(report["experiment"]["evidence_hashes_verified"])

    def test_existing_destination_preserved(self):
        path = self.write_input(manifest(parameters={"a": 1}, evidence=[{"label": "x", "sha256": HASH_A}]))
        _, report = self.preview_hash(path)
        dest = self.root / "out.json"
        dest.write_text("keep me", encoding="utf-8")
        code, out = self.run_cli(path, "--write", dest, "--confirm-sha256", report["manifest_sha256"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out), {"error": "destination_exists"})
        self.assertEqual(dest.read_text(encoding="utf-8"), "keep me")


if __name__ == "__main__":
    unittest.main()
