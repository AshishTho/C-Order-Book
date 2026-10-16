"""Synthetic demo: every output, plain limitations, byte-identical reruns, overwrite refusal, confined writes."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from research import compare, demo, provenance

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


if __name__ == "__main__":
    unittest.main()
