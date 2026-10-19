"""Synthetic demo: every output, plain limitations, byte-identical reruns, overwrite refusal, confined writes."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from research import compare, data_quality, demo, provenance

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {"events.csv", "bbo.csv", "data_quality_policy.json", "manifest.json", "data_quality.json",
            "baseline.json", "high_costs.json", "comparison.json", "comparison.md", "DEMO_RESULTS.md"}
DETERMINISTIC = sorted(EXPECTED - {"DEMO_RESULTS.md"})
LIMITATION_PHRASES = ("Synthetic data only", "No transaction-level market impact",
                      "Results are not evidence of profitability", "Not live-trading ready",
                      "The C++ matching engine is not exercised by this demo (it needs cmake)")
CLAIM_WORDS = ("profitable", "guaranteed", "guarantee", "live-ready")


def run(*args):
    with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
        code = demo.main([str(arg) for arg in args])
    return code, out.getvalue(), err.getvalue()


def tree(folder: Path) -> dict:
    return {path.relative_to(folder).as_posix(): path.read_bytes() for path in folder.rglob("*") if path.is_file()}


def checkout_state() -> dict:
    return {path.relative_to(ROOT).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in ROOT.rglob("*")
            if path.is_file() and not {".git", "__pycache__"}.intersection(path.relative_to(ROOT).parts)}


class DemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._folder = tempfile.TemporaryDirectory()
        cls.root = Path(cls._folder.name)
        for name, seed in (("first", 7), ("second", 7), ("other", 8)):
            code, _, err = run("--output-dir", cls.root / name, "--seed", seed)
            assert code == 0, err

    @classmethod
    def tearDownClass(cls):
        cls._folder.cleanup()

    def load(self, run, name):
        return json.loads((self.root / run / name).read_text(encoding="utf-8"))

    def entry(self, run, name="baseline"):
        return {"name": f"{run}-{name}", "report": self.load(run, f"{name}.json"),
                "provenance": self.load(run, "manifest.json")}

    def test_demo_produces_every_file_with_consistent_evidence(self):
        folder = self.root / "first"
        self.assertEqual(set(tree(folder)), EXPECTED)
        manifest = self.load("first", "manifest.json")
        self.assertTrue(provenance.verify_manifest(manifest, folder)["valid"])
        declared = {item["path"]: item["sha256"] for item in manifest["inputs"]}
        self.assertEqual({e["id"] for e in manifest["experiments"]}, {"baseline", "high_costs"})
        quality = self.load("first", "data_quality.json")
        self.assertEqual((quality["rejected"], quality["sha256"]), ([], declared["bbo.csv"]))
        self.assertEqual(quality["rows"], 1999)
        for name, fee in (("baseline", 0.1), ("high_costs", 0.5)):
            report = self.load("first", f"{name}.json")
            self.assertEqual(report["source"], {"path": "bbo.csv", "sha256": declared["bbo.csv"],
                                                "data_kind": "synthetic"})
            self.assertEqual(report["config"]["fee_ticks"], fee)
            self.assertEqual(report["config"]["seed"], 7)
            self.assertEqual(report["data_quality"]["policy"], demo.POLICY)
            self.assertGreaterEqual(len(report["folds"]), 2)
        result = self.load("first", "comparison.json")
        runs = {run["name"]: run for run in result["runs"]}
        self.assertEqual(runs["baseline"]["status"], "baseline")
        self.assertEqual((runs["high_costs"]["status"], runs["high_costs"]["reasons"]),
                         ("incompatible", ["different-costs"]))
        self.assertEqual({run["provenance"]["status"] for run in runs.values()}, {"declared"})
        # The same comparison is reproducible from the files alone through the CLI.
        out = io.StringIO()
        with redirect_stdout(out):
            code = compare.main([str(folder / "baseline.json"), str(folder / "high_costs.json"), "--format", "json",
                                 "--manifest", f"baseline={folder / 'manifest.json'}",
                                 "--manifest", f"high_costs={folder / 'manifest.json'}"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue()), result)

    def test_results_document_states_run_facts_and_limitations_without_claims(self):
        text = (self.root / "first" / "DEMO_RESULTS.md").read_text(encoding="utf-8")
        self.assertIn("research.demo.main(['--output-dir',", text)
        self.assertIn("- Python: " + sys.version.split()[0], text)
        self.assertIn("- Seed: 7", text)
        self.assertIn("2000 synthetic L3 events -> 1999 timed BBO observations", text)
        for step in ("generate synthetic L3 events", "write provenance manifest",
                     "verify manifest and run data-quality check", "walk-forward: baseline",
                     "walk-forward: high_costs", "compare runs", "total"):
            self.assertRegex(text, rf"\| {step} \| \d+\.\d{{3}} \|")
        self.assertRegex(text, r"\| high_costs \| incompatible \| different-costs \| not-comparable \| declared \|")
        self.assertIn("- baseline vs high_costs: different-costs", text)
        self.assertIn("fold0.imbalance.execution.net_pnl_ticks_lots", text)
        limitations = text.split("## Limitations", 1)[1].split("\n## ", 1)[0]
        for phrase in LIMITATION_PHRASES:
            self.assertIn(phrase, limitations)
        lowered = text.lower()
        for word in CLAIM_WORDS:
            self.assertNotIn(word, lowered)

    def test_same_seed_gives_byte_identical_reports(self):
        first, second = tree(self.root / "first"), tree(self.root / "second")
        for name in DETERMINISTIC:
            self.assertEqual(first[name], second[name], name)
        result = compare.compare_runs([self.entry("first"), self.entry("second")])
        self.assertEqual((result["runs"][1]["status"], result["runs"][1]["reasons"]), ("compatible", []))

    def test_different_seed_changes_data_hash_and_is_flagged_different_sample(self):
        first, other = self.load("first", "baseline.json"), self.load("other", "baseline.json")
        self.assertNotEqual(first["source"]["sha256"], other["source"]["sha256"])
        self.assertEqual(first["total_observations"], other["total_observations"])
        result = compare.compare_runs([self.entry("first"), self.entry("other")])
        run = result["runs"][1]
        self.assertEqual(run["status"], "incompatible")
        self.assertIn("different-sample", run["reasons"])
        self.assertIn("not-comparable", run["flags"])
        self.assertEqual(run["parameter_changes"], {"seed": {"baseline": 7, "run": 8}})

    def test_refuses_to_overwrite_without_force(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "out"
            target.mkdir()
            (target / "keep.txt").write_text("earlier evidence", encoding="utf-8")
            (target / "baseline.json").write_text("earlier report", encoding="utf-8")
            before = tree(Path(folder))
            code, out, err = run("--output-dir", target, "--seed", 7)
            self.assertEqual((code, out), (2, ""))
            self.assertIn("--force", err)
            self.assertEqual(tree(Path(folder)), before)
            not_a_dir = Path(folder) / "file.txt"
            not_a_dir.write_text("x", encoding="utf-8")
            self.assertEqual(run("--output-dir", not_a_dir, "--force")[0], 2)
            self.assertEqual(not_a_dir.read_text(encoding="utf-8"), "x")
            # --force replaces only the demo's own files and leaves anything else untouched.
            code, _, err = run("--output-dir", target, "--seed", 7, "--force")
            self.assertEqual(code, 0, err)
            after = tree(target)
            self.assertEqual(set(after), EXPECTED | {"keep.txt"})
            self.assertEqual(after["keep.txt"], b"earlier evidence")
            reference = tree(self.root / "first")
            for name in DETERMINISTIC:
                self.assertEqual(after[name], reference[name], name)

    def test_script_from_clean_cwd_writes_nothing_outside_output_directory(self):
        before = checkout_state()
        with tempfile.TemporaryDirectory() as folder:
            cwd = Path(folder)
            start = time.perf_counter()
            result = subprocess.run([sys.executable, str(ROOT / "scripts" / "demo.py"), "--output-dir", "out",
                                     "--seed", "7"], cwd=cwd, capture_output=True, text=True, timeout=120)
            elapsed = time.perf_counter() - start
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLess(elapsed, 60)
            produced = tree(cwd)
            self.assertEqual(set(produced), {f"out/{name}" for name in EXPECTED})
            reference = tree(self.root / "first")
            for name in DETERMINISTIC:  # identical across a fresh process and another directory
                self.assertEqual(produced[f"out/{name}"], reference[name], name)
            self.assertIn("scripts", produced["out/DEMO_RESULTS.md"].decode("utf-8"))
            self.assertIn("--output-dir out --seed 7", produced["out/DEMO_RESULTS.md"].decode("utf-8"))
        self.assertEqual(checkout_state(), before)

    def test_rejects_out_of_range_event_count_before_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "out"
            self.assertEqual(run("--output-dir", target, "--events", 50)[0], 2)
            self.assertFalse(target.exists())


class DemoPolicyTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())) / "out"
        # A tiny real synthetic fold; no benchmark binaries or external services.
        for name, value in (("INITIAL_TRAIN", 31), ("TEST_SIZE", 31), ("MIN_EVENTS", 63)):
            self.stack.enter_context(patch.object(demo, name, value))
        self.original = dict(demo.POLICY)
        self.replacement = {**self.original, "max_gap_seconds": 0.5, "on_gap": "mark"}

    def run_demo(self):
        return run("--output-dir", self.root, "--events", 63)

    def reject_replacement(self, window, replacement):
        build, verify = provenance.build_manifest, provenance.verify_manifest

        def replace_policy():
            (self.root / "data_quality_policy.json").write_bytes(replacement)

        def replace_then_build(*args, **kwargs):
            replace_policy()
            return build(*args, **kwargs)

        def verify_then_replace(*args, **kwargs):
            result = verify(*args, **kwargs)
            self.assertTrue(result["valid"])
            replace_policy()
            return result

        with patch.object(provenance, "build_manifest" if window == "before_hash" else "verify_manifest",
                          side_effect=replace_then_build if window == "before_hash" else verify_then_replace), \
                patch.object(data_quality, "apply_policy", wraps=data_quality.apply_policy) as quality, \
                patch.object(demo, "evaluate_walk_forward", wraps=demo.evaluate_walk_forward) as evaluate:
            code, _, err = self.run_demo()
        self.assertEqual(code, 2, err)
        self.assertIn("data_quality_policy.json", err)
        quality.assert_not_called()
        evaluate.assert_not_called()
        reports = EXPECTED - {"events.csv", "bbo.csv", "data_quality_policy.json", "manifest.json"}
        self.assertFalse(reports.intersection(p.name for p in self.root.iterdir()))
        self.assertEqual((self.root / "data_quality_policy.json").read_bytes(), replacement)

    def test_policy_substitution_before_manifest_hash_is_rejected(self):
        self.reject_replacement("before_hash", json.dumps(self.replacement).encode("utf-8"))

    def test_policy_substitution_after_verification_is_rejected(self):
        self.reject_replacement("after_verify", json.dumps(self.replacement).encode("utf-8"))

    def test_policy_reformatting_before_manifest_hash_is_rejected(self):
        self.reject_replacement("before_hash", json.dumps(self.original).encode("utf-8"))

    def test_policy_reformatting_after_verification_is_rejected(self):
        self.reject_replacement("after_verify", json.dumps(self.original).encode("utf-8"))

    def assert_policy_consistency(self):
        for name in ("data_quality.json", "baseline.json", "high_costs.json"):
            report = json.loads((self.root / name).read_text(encoding="utf-8"))
            quality = report if name == "data_quality.json" else report["data_quality"]
            self.assertEqual(quality["policy"], self.original)
            self.assertEqual(quality["marked"], [])
        manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        step = next(s for s in manifest["transformations"] if s["name"] == "research.data_quality.apply_policy")
        self.assertEqual(step["parameters"], self.original)

    def test_all_consumers_use_published_snapshot_when_global_policy_changes(self):
        verify, apply_policy = provenance.verify_manifest, data_quality.apply_policy
        consumed = []

        def verify_then_change_global(*args, **kwargs):
            result = verify(*args, **kwargs)
            demo.POLICY.update(self.replacement)
            return result

        def record_policy(rows, policy, **kwargs):
            consumed.append(dict(policy))
            return apply_policy(rows, policy, **kwargs)

        with patch.dict(demo.POLICY), \
                patch.object(provenance, "verify_manifest", side_effect=verify_then_change_global), \
                patch.object(data_quality, "apply_policy", side_effect=record_policy):
            code, _, err = self.run_demo()
        self.assertEqual(code, 0, err)
        self.assertEqual(consumed, [self.original] * 3)
        self.assert_policy_consistency()

    def test_replacement_after_byte_capture_cannot_change_consumed_policy(self):
        read_bytes = Path.read_bytes
        captures = []

        def capture_then_replace(path):
            payload = read_bytes(path)
            if path == self.root / "data_quality_policy.json":
                captures.append(payload)
                path.write_bytes(json.dumps(self.replacement).encode("utf-8"))
            return payload

        with patch.object(Path, "read_bytes", capture_then_replace):
            code, _, err = self.run_demo()
        self.assertEqual(code, 0, err)
        self.assertEqual(len(captures), 1)
        self.assertEqual(json.loads(captures[0]), self.original)
        self.assert_policy_consistency()


class DemoCollisionTests(unittest.TestCase):
    SENTINEL = b"other writer's evidence\x00\xff"

    def assert_no_staging(self, root):
        self.assertLessEqual({p.name for p in root.iterdir()}, EXPECTED)

    def make_competitor(self, root, name, kind, outside):
        target = root / name
        if kind == "file":
            target.write_bytes(self.SENTINEL)
        elif kind == "directory":
            target.mkdir()
            (target / "keep.bin").write_bytes(self.SENTINEL)
        else:
            if kind == "symlink":
                outside.write_bytes(self.SENTINEL)
            elif kind == "directory_symlink":
                outside.mkdir()
                (outside / "keep.bin").write_bytes(self.SENTINEL)
            try:
                target.symlink_to(outside, target_is_directory=kind == "directory_symlink")
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")
        return target

    def assert_referent(self, kind, outside):
        if kind == "symlink":
            self.assertEqual(outside.read_bytes(), self.SENTINEL)
        elif kind == "directory_symlink":
            self.assertEqual((outside / "keep.bin").read_bytes(), self.SENTINEL)
        elif kind == "dangling":
            self.assertFalse(outside.exists())

    def assert_competitor(self, target, kind, outside):
        if kind == "file":
            self.assertEqual(target.read_bytes(), self.SENTINEL)
        elif kind == "directory":
            self.assertEqual((target / "keep.bin").read_bytes(), self.SENTINEL)
        else:
            self.assertTrue(target.is_symlink())
            self.assertEqual(target.readlink(), outside)
            self.assert_referent(kind, outside)

    def collision_after_prepare(self, name, kind, force=False):
        with tempfile.TemporaryDirectory() as folder:
            root, outside = Path(folder) / "out", Path(folder) / "external.bin"
            prepare = demo.prepare_output

            def admit_then_compete(path, requested_force):
                result = prepare(path, requested_force)
                self.make_competitor(result, name, kind, outside)
                return result

            with patch.object(demo, "prepare_output", side_effect=admit_then_compete):
                code, _, err = run("--output-dir", root, "--events", demo.MIN_EVENTS,
                                   *(["--force"] if force else []))
            self.assertEqual(code, 2, err)
            self.assertIn(name, err)
            self.assert_competitor(root / name, kind, outside)
            self.assert_no_staging(root)

    def test_late_regular_file_at_each_output_is_preserved_without_force(self):
        for name in sorted(EXPECTED):
            with self.subTest(name=name):
                self.collision_after_prepare(name, "file")

    def test_late_directory_is_preserved_even_with_force(self):
        for force in (False, True):
            with self.subTest(force=force):
                self.collision_after_prepare("events.csv", "directory", force)

    def test_late_existing_symlink_is_preserved_without_force(self):
        self.collision_after_prepare("events.csv", "symlink")

    def test_late_dangling_symlink_is_preserved_without_force(self):
        self.collision_after_prepare("events.csv", "dangling")

    def test_late_directory_symlink_is_preserved_without_force(self):
        self.collision_after_prepare("events.csv", "directory_symlink")

    def collision_at_publication(self, kind, force=False):
        with tempfile.TemporaryDirectory() as folder:
            root, outside = Path(folder) / "out", Path(folder) / "external.bin"
            publish_file = os.replace if force else os.link

            def compete_then_publish(source, destination):
                if Path(destination) == root / "events.csv":
                    self.assertTrue(Path(source).read_bytes().startswith(b"seq,type,"))
                    self.make_competitor(root, "events.csv", kind, outside)
                return publish_file(source, destination)

            with patch("os.replace" if force else "os.link", side_effect=compete_then_publish) as publish:
                code, _, err = run("--output-dir", root, "--events", demo.MIN_EVENTS,
                                   *(["--force"] if force else []))
            publish.assert_called()
            if force and kind != "directory":
                self.assertEqual(code, 0, err)
                self.assertFalse((root / "events.csv").is_symlink())
                self.assertTrue((root / "events.csv").read_bytes().startswith(b"seq,type,"))
                self.assert_referent(kind, outside)
            else:
                self.assertEqual(code, 2, err)
                self.assertIn("events.csv", err)
                self.assert_competitor(root / "events.csv", kind, outside)
            self.assert_no_staging(root)

    def test_late_regular_file_at_publication_is_preserved(self):
        self.collision_at_publication("file")

    def test_late_directory_at_publication_is_preserved(self):
        self.collision_at_publication("directory")

    def test_late_existing_symlink_at_publication_is_preserved(self):
        self.collision_at_publication("symlink")

    def test_late_dangling_symlink_at_publication_is_preserved(self):
        self.collision_at_publication("dangling")

    def test_late_directory_symlink_at_publication_is_preserved(self):
        self.collision_at_publication("directory_symlink")

    def test_force_replaces_late_regular_file_at_publication(self):
        self.collision_at_publication("file", force=True)

    def test_force_preserves_late_directory_at_publication(self):
        self.collision_at_publication("directory", force=True)

    def test_force_replaces_late_existing_symlink_at_publication(self):
        self.collision_at_publication("symlink", force=True)

    def test_force_replaces_late_dangling_symlink_at_publication(self):
        self.collision_at_publication("dangling", force=True)

    def test_force_replaces_late_directory_symlink_at_publication(self):
        self.collision_at_publication("directory_symlink", force=True)

    def test_failed_force_publication_preserves_existing_file_and_cleans_staging(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / "events.csv"
            target.write_bytes(self.SENTINEL)
            with patch("os.replace", side_effect=PermissionError("synthetic replace failure")):
                code, _, err = run("--output-dir", root, "--events", demo.MIN_EVENTS, "--force")
            self.assertEqual(code, 2, err)
            self.assertIn("synthetic replace failure", err)
            self.assertEqual(target.read_bytes(), self.SENTINEL)
            self.assertEqual({p.name for p in root.iterdir()}, {"events.csv"})

    def test_unsupported_hard_links_fail_without_publishing_or_staging(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "out"
            with patch("os.link", side_effect=OSError("hard links unsupported")):
                code, _, err = run("--output-dir", root, "--events", demo.MIN_EVENTS)
            self.assertEqual(code, 2, err)
            self.assertIn("hard link", err)
            self.assertEqual(list(root.iterdir()), [])

    def test_failed_serialization_preserves_force_target_and_cleans_staging(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / "manifest.json"
            target.write_bytes(self.SENTINEL)

            def partial_manifest(_manifest, path):
                Path(path).write_text("partial", encoding="utf-8")
                raise OSError("synthetic writer failure")

            with patch.object(provenance, "write_manifest", side_effect=partial_manifest):
                code, _, err = run("--output-dir", root, "--events", demo.MIN_EVENTS, "--force")
            self.assertEqual(code, 2, err)
            self.assertIn("synthetic writer failure", err)
            self.assertEqual(target.read_bytes(), self.SENTINEL)
            self.assert_no_staging(root)

    def test_force_replaces_late_regular_file_at_every_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "out"
            prepare = demo.prepare_output

            def admit_then_compete(path, requested_force):
                result = prepare(path, requested_force)
                for name in EXPECTED:
                    (result / name).write_bytes(self.SENTINEL)
                return result

            with patch.object(demo, "prepare_output", side_effect=admit_then_compete):
                code, _, err = run("--output-dir", root, "--events", demo.MIN_EVENTS, "--force")
            self.assertEqual(code, 0, err)
            for name in EXPECTED:
                self.assertNotEqual((root / name).read_bytes(), self.SENTINEL)
            self.assertEqual({p.name for p in root.iterdir()}, EXPECTED)

    def force_replaces_symlink(self, kind):
        with tempfile.TemporaryDirectory() as folder:
            root, outside = Path(folder) / "out", Path(folder) / "external.bin"
            root.mkdir()
            target = self.make_competitor(root, "events.csv", kind, outside)
            code, _, err = run("--output-dir", root, "--events", demo.MIN_EVENTS, "--force")
            self.assertEqual(code, 0, err)
            self.assertFalse(target.is_symlink())
            self.assertTrue(target.read_bytes().startswith(b"seq,type,"))
            self.assert_referent(kind, outside)
            self.assert_no_staging(root)

    def test_force_replaces_existing_symlink_without_touching_referent(self):
        self.force_replaces_symlink("symlink")

    def test_force_replaces_dangling_symlink_without_creating_referent(self):
        self.force_replaces_symlink("dangling")

    def test_force_replaces_directory_symlink_without_touching_referent(self):
        self.force_replaces_symlink("directory_symlink")


if __name__ == "__main__":
    unittest.main()
