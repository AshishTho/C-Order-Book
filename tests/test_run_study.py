"""Study orchestration evidence with mocked steps; no research subprocesses run."""
import base64
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
import run_study  # noqa: E402


class StudyEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="study-evidence-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "study"
        self.binary = self.root / "fake-replay"
        self.binary.write_bytes(b"fixture binary; never executed")

    def invoke(self, *, timeout_at=None, timeout_stdout=None, timeout_stderr=None, fail_at=None):
        steps, git_calls = [], []

        def run(command, **kwargs):
            if command == ["git", "rev-parse", "HEAD"]:
                git_calls.append(command)
                return subprocess.CompletedProcess(command, 0, "fixture-commit\n", "")
            index = len(steps)
            steps.append(command)
            self.assertEqual(kwargs["timeout"], 120)
            self.assertTrue(kwargs["capture_output"])
            self.assertTrue(kwargs["text"])
            if index == timeout_at:
                raise subprocess.TimeoutExpired(command, kwargs["timeout"], output=timeout_stdout,
                                                stderr=timeout_stderr)
            target = Path(command[command.index("--output") + 1])
            target.write_text(f"synthetic step {index} artifact\n", encoding="utf-8")
            if "--html" in command:
                Path(command[command.index("--html") + 1]).write_text("synthetic HTML fixture", encoding="utf-8")
            return subprocess.CompletedProcess(command, 7 if index == fail_at else 0,
                                                f"step {index} stdout\n", f"step {index} stderr\n")

        args = ["run_study.py", "--build-dir", str(self.root), "--output-dir", str(self.output),
                "--events", "10", "--seed", "7"]
        stdout, stderr = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch.object(sys, "argv", args), \
                mock.patch.object(run_study, "executable", return_value=self.binary), \
                mock.patch.object(run_study, "source_hash", return_value="a" * 64), \
                mock.patch.object(run_study.platform, "platform", return_value="fixture platform"), \
                mock.patch.object(run_study.platform, "python_version", return_value="fixture Python"), \
                mock.patch.object(run_study.subprocess, "run", side_effect=run), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                run_study.main()
            except SystemExit as error:
                code = error.code
        return code, stdout.getvalue(), stderr.getvalue(), steps, git_calls

    def failure(self):
        path = self.output / "failure.json"
        self.assertTrue(path.exists(), "Failure evidence must be persisted before exiting")
        return json.loads(path.read_text(encoding="utf-8"))

    def test_first_timeout_retains_partial_bytes_and_unknown_returncode(self):
        partial_stdout, partial_stderr = b"partial progress\r\n\xe2\x82", b"partial diagnostic\n\xff"
        code, _, error, steps, git_calls = self.invoke(
            timeout_at=0, timeout_stdout=partial_stdout, timeout_stderr=partial_stderr)
        self.assertEqual((code, len(steps), len(git_calls)), (2, 1, 0))
        self.assertIn("timed out after 120 seconds", error)
        records = self.failure()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["command"], steps[0])
        self.assertIsNone(record["returncode"])
        self.assertIs(record["timed_out"], True)
        self.assertEqual(record["timeout_seconds"], 120)
        for stream, captured in (("stdout", partial_stdout), ("stderr", partial_stderr)):
            self.assertEqual(base64.b64decode(record[stream + "_base64"], validate=True), captured)
            self.assertEqual(record[stream], captured.decode("utf-8", errors="replace"))
        self.assertFalse((self.output / "manifest.json").exists())

    def test_later_timeout_retains_every_prior_step_and_stops_the_pipeline(self):
        for timeout_at in (1, 2):
            with self.subTest(timeout_at=timeout_at):
                self.output = self.root / f"later-{timeout_at}"
                code, _, error, steps, git_calls = self.invoke(
                    timeout_at=timeout_at, timeout_stdout=b"later partial", timeout_stderr=b"later warning")
                self.assertEqual((code, len(steps), len(git_calls)), (2, timeout_at + 1, 0))
                self.assertIn("timed out after 120 seconds", error)
                records = self.failure()
                self.assertEqual(len(records), timeout_at + 1)
                for index, record in enumerate(records[:-1]):
                    self.assertEqual(record, {"command": steps[index], "returncode": 0,
                                              "stdout": f"step {index} stdout\n",
                                              "stderr": f"step {index} stderr\n"})
                self.assertEqual(records[-1]["command"], steps[-1])
                self.assertEqual(records[-1]["stdout"], "later partial")
                self.assertEqual(base64.b64decode(records[-1]["stderr_base64"]), b"later warning")
                self.assertIsNone(records[-1]["returncode"])
                self.assertTrue((self.output / "events.csv").exists())
                self.assertFalse((self.output / "manifest.json").exists())

    def test_timeout_distinguishes_absent_empty_and_already_decoded_output(self):
        cases = ((None, None, None), (b"", "", ""), ("decoded text", "decoded text", None))
        for index, (captured, text, encoded) in enumerate(cases):
            with self.subTest(captured=captured):
                self.output = self.root / f"capture-{index}"
                code, _, _, steps, _ = self.invoke(timeout_at=0, timeout_stdout=captured, timeout_stderr=captured)
                self.assertEqual((code, len(steps)), (2, 1))
                record = self.failure()[0]
                for stream in ("stdout", "stderr"):
                    self.assertEqual(record[stream], text)
                    self.assertEqual(record[stream + "_base64"], encoded)

    def test_nonzero_exit_keeps_existing_failure_format_and_prior_logs(self):
        for fail_at in (0, 1, 2):
            with self.subTest(fail_at=fail_at):
                self.output = self.root / f"nonzero-{fail_at}"
                code, _, error, steps, git_calls = self.invoke(fail_at=fail_at)
                self.assertEqual((code, len(steps), len(git_calls)), (2, fail_at + 1, 0))
                self.assertIn(f"step {fail_at} stderr", error)
                self.assertEqual(self.failure(), [
                    {"command": command, "returncode": 7 if index == fail_at else 0,
                     "stdout": f"step {index} stdout\n", "stderr": f"step {index} stderr\n"}
                    for index, command in enumerate(steps)])
                self.assertFalse((self.output / "manifest.json").exists())

    def test_success_keeps_manifest_and_does_not_create_failure_evidence(self):
        code, _, error, steps, git_calls = self.invoke()
        self.assertEqual((code, error, len(steps), len(git_calls)), (0, "", 3, 1))
        manifest = json.loads((self.output / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual((manifest["seed"], manifest["events"], manifest["git_base_commit"]),
                         (7, 10, "fixture-commit"))
        self.assertEqual(manifest["source_sha256"], "a" * 64)
        self.assertEqual(manifest["binary_sha256"], hashlib.sha256(self.binary.read_bytes()).hexdigest())
        self.assertEqual([step["command"] for step in manifest["steps"]], steps)
        self.assertEqual(set(manifest["files_sha256"]), {"events.csv", "bbo.csv", "report.json", "report.html"})
        self.assertFalse((self.output / "failure.json").exists())

    def test_existing_output_directory_is_untouched(self):
        self.output.mkdir()
        sentinel = self.output / "prior-evidence"
        sentinel.write_bytes(b"retain original evidence")
        code, _, error, steps, git_calls = self.invoke()
        self.assertEqual((code, len(steps), len(git_calls)), (2, 0, 0))
        self.assertIn("already exists", error)
        self.assertEqual(sentinel.read_bytes(), b"retain original evidence")
        self.assertEqual(sorted(path.name for path in self.output.iterdir()), ["prior-evidence"])


if __name__ == "__main__":
    unittest.main()
