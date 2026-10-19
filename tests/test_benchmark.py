"""Publication regressions using fake subprocess results, never real benchmarks."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import benchmark  # noqa: E402


class BenchmarkCaptureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="benchmark-capture-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "report.json"
        self.binary = self.root / "fake-benchmark"
        self.binary.write_bytes(b"fixture binary; never executed")
        (self.root / "CMakeCache.txt").write_text("fixture build metadata")
        self.evidence = b"another writer's evidence\x00\xff\n"

    def invoke(self, *, returncode=0, repeats=1, during_run=None):
        def run(argv, **kwargs):
            if during_run:
                during_run()
            return subprocess.CompletedProcess(argv, returncode, "raw stdout\n", "raw stderr\n")

        argv = ["benchmark.py", "--build-dir", str(self.root), "--config", "Debug",
                "--output", str(self.output), "--targets", "risk", "--repeats", str(repeats)]
        stdout, stderr = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(benchmark, "executable", return_value=self.binary), \
                mock.patch.object(benchmark, "source_hash", return_value="a" * 64), \
                mock.patch.object(benchmark.platform, "platform", return_value="fixture platform"), \
                mock.patch.object(benchmark.platform, "processor", return_value="fixture processor"), \
                mock.patch.object(benchmark.subprocess, "run", side_effect=run) as process, \
                redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                benchmark.main()
            except SystemExit as error:
                code = error.code
        return code, stdout.getvalue(), stderr.getvalue(), process.call_count

    def make_symlink(self, target):
        try:
            self.output.symlink_to(target)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"Symlink creation unavailable: {error}")

    def test_existing_file_refused_before_running(self):
        self.output.write_bytes(self.evidence)
        code, _, error, calls = self.invoke()
        self.assertEqual((code, calls), (2, 0))
        self.assertIn("preserve previous evidence", error)
        self.assertEqual(self.output.read_bytes(), self.evidence)

    def test_existing_directory_refused_before_running(self):
        self.output.mkdir()
        code, _, _, calls = self.invoke()
        self.assertEqual((code, calls), (2, 0))
        self.assertTrue(self.output.is_dir())

    def test_existing_dangling_symlink_refused_before_running(self):
        target = self.root / "missing-target"
        self.make_symlink(target)
        code, _, _, calls = self.invoke()
        self.assertEqual((code, calls), (2, 0))
        self.assertTrue(self.output.is_symlink())
        self.assertFalse(target.exists())

    def test_concurrent_file_preserved_after_success_or_failure(self):
        for child_code in (0, 7):
            with self.subTest(child_code=child_code):
                code, _, error, calls = self.invoke(
                    returncode=child_code,
                    during_run=lambda: self.output.write_bytes(self.evidence))
                self.assertEqual((code, calls), (2, 1))
                self.assertIn("preserve previous evidence", error)
                self.assertEqual(self.output.read_bytes(), self.evidence)
                self.output.unlink()

    def test_concurrent_symlink_does_not_create_target(self):
        target = self.root / "missing-target"
        code, _, error, calls = self.invoke(during_run=lambda: self.make_symlink(target))
        self.assertEqual((code, calls), (2, 1))
        self.assertIn("preserve previous evidence", error)
        self.assertTrue(self.output.is_symlink())
        self.assertFalse(target.exists())

    def test_fresh_report_preserves_raw_evidence_and_metadata(self):
        code, _, error, calls = self.invoke(repeats=2)
        self.assertEqual((code, calls, error), (0, 2, ""))
        report = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(report["source_sha256"], "a" * 64)
        self.assertEqual(report["requested_config"], "Debug")
        self.assertEqual(report["cmake_cache"], "fixture build metadata")
        self.assertEqual(len(report["runs"]), 2)
        for repeat, run in enumerate(report["runs"]):
            self.assertEqual(run, {"target": "risk", "repeat": repeat, "returncode": 0,
                                   "binary_sha256": hashlib.sha256(self.binary.read_bytes()).hexdigest(),
                                   "stdout": "raw stdout\n", "stderr": "raw stderr\n"})

    def test_failed_benchmark_still_saves_fresh_report(self):
        code, _, error, calls = self.invoke(returncode=7, repeats=2)
        self.assertEqual((code, calls), (2, 1))
        self.assertIn("risk failed; inspect saved evidence", error)
        report = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(report["error"], "risk failed; inspect saved evidence")
        self.assertEqual(report["runs"][0]["returncode"], 7)
        self.assertEqual(report["runs"][0]["stderr"], "raw stderr\n")

    def test_publication_error_is_reported_without_success(self):
        real_open = Path.open

        def deny_publication(path, mode="r", *args, **kwargs):
            if path == self.output and mode == "x":
                raise PermissionError("fixture write denied")
            return real_open(path, mode, *args, **kwargs)

        with mock.patch.object(Path, "open", deny_publication):
            code, stdout, error, calls = self.invoke()
        self.assertEqual((code, calls), (2, 1))
        self.assertIn("Could not save benchmark evidence", error)
        self.assertNotIn(str(self.output), stdout)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
