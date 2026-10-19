"""Publication regressions using fake subprocess results, never real benchmarks."""
from contextlib import redirect_stderr, redirect_stdout
import base64
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

    def invoke(self, *, returncode=0, repeats=1, during_run=None, timeout_at=None,
               timeout_stdout=None, timeout_stderr=None):
        attempt = 0

        def run(argv, **kwargs):
            nonlocal attempt
            index = attempt
            attempt += 1
            self.assertEqual(kwargs["timeout"], 180)
            self.assertTrue(kwargs["text"])
            if during_run:
                during_run()
            if index == timeout_at:
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output=timeout_stdout,
                                                stderr=timeout_stderr)
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

    def test_concurrent_symlink_preserves_existing_or_missing_target(self):
        # Check capability outside a subTest so an unavailable privilege skips
        # the whole test before benchmark.main's final publication can run.
        self.make_symlink(self.root / "capability-probe")
        self.output.unlink()
        for child_code in (0, 7):
            for target_exists in (False, True):
                with self.subTest(child_code=child_code, target_exists=target_exists):
                    target = self.root / f"target-{child_code}-{target_exists}"
                    if target_exists:
                        target.write_bytes(self.evidence)
                    code, _, error, calls = self.invoke(
                        returncode=child_code, during_run=lambda: self.make_symlink(target))
                    self.assertEqual((code, calls), (2, 1))
                    self.assertIn("preserve previous evidence", error)
                    self.assertTrue(self.output.is_symlink())
                    self.assertEqual(target.exists(), target_exists)
                    if target_exists:
                        self.assertEqual(target.read_bytes(), self.evidence)
                    self.output.unlink()
                    self.assertEqual(list(self.root.glob(".benchmark-report-*")), [])

    def test_competing_writer_at_publication_boundary_is_preserved(self):
        real_link = benchmark.os.link

        def claim_output(source, destination):
            self.assertEqual(json.loads(Path(source).read_text())["runs"][0]["stdout"], "raw stdout\n")
            self.output.write_bytes(self.evidence)
            return real_link(source, destination)

        for child_code in (0, 7):
            with self.subTest(child_code=child_code), mock.patch.object(benchmark.os, "link", claim_output):
                code, _, error, calls = self.invoke(returncode=child_code)
                self.assertEqual((code, calls), (2, 1))
                self.assertIn("preserve previous evidence", error)
                self.assertEqual(self.output.read_bytes(), self.evidence)
                self.assertEqual(list(self.root.glob(".benchmark-report-*")), [])
                self.output.unlink()

    def test_concurrent_directory_is_preserved(self):
        code, _, error, calls = self.invoke(during_run=self.output.mkdir)
        self.assertEqual((code, calls), (2, 1))
        self.assertTrue(self.output.is_dir())
        self.assertTrue(error)
        self.assertEqual(list(self.root.glob(".benchmark-report-*")), [])

    def test_fresh_report_preserves_raw_evidence_and_metadata(self):
        code, _, error, calls = self.invoke(repeats=2)
        self.assertEqual((code, calls, error), (0, 2, ""))
        report = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(report["source_sha256"], "a" * 64)
        self.assertEqual(report["requested_config"], "Debug")
        self.assertEqual(report["cmake_cache"], "fixture build metadata")
        self.assertEqual(len(report["runs"]), 2)
        self.assertEqual(list(self.root.glob(".benchmark-report-*")), [])
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

    def test_first_timeout_preserves_partial_bytes_and_attempt_metadata(self):
        observed_hash = hashlib.sha256(self.binary.read_bytes()).hexdigest()
        partial_stdout = b"partial stdout\r\n\xe2\x82"
        partial_stderr = b"affinity warning\n\xff"
        code, _, error, calls = self.invoke(
            repeats=3, timeout_at=0, timeout_stdout=partial_stdout, timeout_stderr=partial_stderr,
            during_run=lambda: self.binary.write_bytes(b"binary replaced after launch"))
        self.assertEqual((code, calls), (2, 1))
        self.assertIn("timed out after 180 seconds", error)
        report = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(len(report["runs"]), 1)
        run = report["runs"][0]
        self.assertEqual((run["target"], run["repeat"], run["binary_sha256"]), ("risk", 0, observed_hash))
        self.assertIsNone(run["returncode"])
        self.assertIs(run["timed_out"], True)
        self.assertEqual(run["timeout_seconds"], 180)
        for stream, raw in (("stdout", partial_stdout), ("stderr", partial_stderr)):
            self.assertEqual(base64.b64decode(run[stream + "_base64"], validate=True), raw)
            self.assertEqual(run[stream], raw.decode("utf-8", errors="replace"))
        self.assertEqual(report["source_sha256"], "a" * 64)
        self.assertEqual(report["requested_config"], "Debug")
        self.assertEqual(report["cmake_cache"], "fixture build metadata")

    def test_later_timeout_keeps_prior_run_and_stops_remaining_repeats(self):
        code, _, error, calls = self.invoke(repeats=4, timeout_at=1, timeout_stdout=b"second partial")
        self.assertEqual((code, calls), (2, 2))
        self.assertIn("timed out after 180 seconds", error)
        runs = json.loads(self.output.read_text(encoding="utf-8"))["runs"]
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0], {"target": "risk", "repeat": 0, "returncode": 0,
                                   "binary_sha256": hashlib.sha256(self.binary.read_bytes()).hexdigest(),
                                   "stdout": "raw stdout\n", "stderr": "raw stderr\n"})
        self.assertEqual((runs[1]["target"], runs[1]["repeat"], runs[1]["stdout"]),
                         ("risk", 1, "second partial"))
        self.assertIsNone(runs[1]["returncode"])
        self.assertTrue(runs[1]["timed_out"])

    def test_timeout_absent_empty_and_text_output_are_distinguished(self):
        cases = ((None, None, None), (b"", "", ""), ("already decoded", "already decoded", None))
        for index, (captured, text, encoded) in enumerate(cases):
            with self.subTest(captured=captured):
                self.output = self.root / f"report-{index}.json"
                code, _, _, calls = self.invoke(timeout_at=0, timeout_stdout=captured, timeout_stderr=captured)
                self.assertEqual((code, calls), (2, 1))
                run = json.loads(self.output.read_text(encoding="utf-8"))["runs"][0]
                for stream in ("stdout", "stderr"):
                    self.assertEqual(run[stream], text)
                    self.assertEqual(run[stream + "_base64"], encoded)

    def test_timeout_does_not_overwrite_competing_output(self):
        code, _, error, calls = self.invoke(
            repeats=3, timeout_at=0, timeout_stdout=b"partial progress",
            during_run=lambda: self.output.write_bytes(self.evidence))
        self.assertEqual((code, calls), (2, 1))
        self.assertIn("preserve previous evidence", error)
        self.assertEqual(self.output.read_bytes(), self.evidence)
        self.assertEqual(list(self.root.glob(".benchmark-report-*")), [])

    def test_publication_error_is_reported_without_success(self):
        with mock.patch.object(benchmark.os, "link", side_effect=PermissionError("fixture link denied")):
            code, stdout, error, calls = self.invoke()
        self.assertEqual((code, calls), (2, 1))
        self.assertIn("Could not save benchmark evidence", error)
        self.assertNotIn(str(self.output), stdout)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".benchmark-report-*")), [])


if __name__ == "__main__":
    unittest.main()
