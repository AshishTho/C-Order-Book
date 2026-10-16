"""Run comparison: comparability reasons, retained losing/incompatible runs, renderers, CLI exit codes."""
from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest

from research import compare, provenance
from research.compare import compare_runs, render
from research.lab import Config, Snapshot
from research.stress_walk_forward import evaluate_walk_forward as stress_walk_forward
from research.walk_forward import main as walk_forward_main

STAMP = "2026-10-16T09:00:00+00:00"


def market(count=180):
    return [Snapshot(i + 1, 100 + i // 3, 102 + i // 3, 20 + i % 11, 10 + i % 7) for i in range(count)]


def bbo_bytes(rows):
    return ("seq,bid_price,ask_price,bid_qty,ask_qty\n" + "".join(
        f"{r.seq},{r.bid_price},{r.ask_price},{r.bid_qty},{r.ask_qty}\n" for r in rows)).encode("utf-8")


def quiet(function, *args):
    with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
        code = function(*args)
    return code, out.getvalue(), err.getvalue()


class CompareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._folder = tempfile.TemporaryDirectory()
        cls.root = Path(cls._folder.name)
        data = cls.root / "data"
        data.mkdir()
        rows = market()
        (data / "bbo.csv").write_bytes(bbo_bytes(rows))
        # Same row count and shape, one quantity changed: only the checksum tells them apart.
        changed = rows[:150] + [Snapshot(r.seq, r.bid_price, r.ask_price, r.bid_qty + 3, r.ask_qty) for r in rows[150:]]
        (data / "bbo_changed.csv").write_bytes(bbo_bytes(changed))
        reports = cls.root / "reports"
        cls.reports = reports
        base = ["--data-kind", "synthetic", "--initial-train", "60", "--test-size", "40",
                "--horizon", "5", "--delay", "2"]
        manifest = ["--write-manifest", "--input-source", "synthetic:test market()", "--input-retrieved-at", STAMP]
        plans = {"base": ("bbo.csv", manifest, []), "same": ("bbo.csv", manifest, []),
                 "costly": ("bbo.csv", manifest, ["--fee-ticks", "0.5"]),
                 "changed": ("bbo_changed.csv", manifest, []),
                 "bare": ("bbo.csv", [], []),
                 "slower": ("bbo.csv", manifest, ["--delay", "4"]),
                 "resplit": ("bbo.csv", manifest, ["--test-size", "50"])}
        for name, (source, extra, override) in plans.items():
            code, _, err = quiet(walk_forward_main, ["--input", str(data / source),
                                                     "--output", str(reports / f"{name}.json"),
                                                     *base, *extra, *override])
            assert code == 0, err

    @classmethod
    def tearDownClass(cls):
        cls._folder.cleanup()

    def path(self, name):
        return self.reports / f"{name}.json"

    def run_entry(self, name, manifest=True):
        sidecar = self.reports / f"{name}.json.manifest.json"
        return {"name": name, "report": compare.load_report(self.path(name)),
                "provenance": sidecar if manifest and sidecar.exists() else None}

    def compare(self, *names, baseline=None):
        return compare_runs([self.run_entry(name) for name in names], baseline=baseline)

    @staticmethod
    def run_of(result, name):
        return next(run for run in result["runs"] if run["name"] == name)

    def test_identical_inputs_and_costs_are_compatible_with_deltas(self):
        result = self.compare("base", "same", "slower")
        self.assertEqual(result["baseline"], "base")
        for name in ("same", "slower"):
            run = self.run_of(result, name)
            self.assertEqual((run["status"], run["reasons"]), ("compatible", []), name)
            self.assertEqual(run["provenance"]["status"], "declared")
        self.assertEqual(self.run_of(result, "slower")["parameter_changes"], {"delay": {"baseline": 2, "run": 4}})
        nonzero = 0
        for row in result["metrics"]:
            base, same, slower = (row["cells"][name] for name in ("base", "same", "slower"))
            self.assertEqual(base["label"], "baseline")
            self.assertIsNotNone(base["n"])
            if base["value"] is not None and same["value"] is not None:
                self.assertEqual((same["delta"], same["label"]), (0, "equal"))
            if base["value"] is not None and slower["value"] is not None:
                self.assertEqual(slower["delta"], slower["value"] - base["value"])
                self.assertIsNotNone(slower["n"])
                nonzero += slower["delta"] != 0
        self.assertGreater(nonzero, 0)
        net = next(row for row in result["metrics"] if row["metric"] == "fold0.imbalance.execution.net_pnl_ticks_lots")
        self.assertEqual((net["direction"], net["sample_basis"]), ("higher", "test_observations"))
        self.assertEqual(net["cells"]["base"]["n"], 40)
        self.assertEqual([item["reasons"] for item in result["pairwise"]], [[], [], []])

    def test_changed_costs_are_flagged_and_never_ranked(self):
        result = self.compare("base", "costly")
        run = self.run_of(result, "costly")
        self.assertEqual((run["status"], run["reasons"]), ("incompatible", ["different-costs"]))
        self.assertEqual(run["details"]["different-costs"]["run"]["fee_ticks"], 0.5)
        self.assertIn("not-comparable", run["flags"])
        self.assertNotIn("worse-than-baseline", run["flags"])
        self.assertEqual(set(run["label_counts"]), {"not-comparable"})
        for row in result["metrics"]:
            cell = row["cells"]["costly"]
            self.assertEqual((cell["delta"], cell["label"]), (None, "not-comparable"))
        # Values are still shown side by side; nothing is dropped.
        self.assertTrue(any(row["cells"]["costly"]["value"] is not None for row in result["metrics"]))
        self.assertEqual(len(result["metrics"]), len(self.compare("base", "same")["metrics"]))
        for fmt in ("text", "json", "markdown"):
            output = render(result, fmt).lower()
            for word in ("winner", "best", "rank:", "ranking:"):
                self.assertNotIn(word, output.replace("no ranking or winner is declared", ""))

    def test_changed_input_checksum_is_different_sample(self):
        result = self.compare("base", "changed")
        run = self.run_of(result, "changed")
        self.assertEqual((run["status"], run["reasons"]), ("incompatible", ["different-sample"]))
        detail = run["details"]["different-sample"]
        self.assertNotEqual(detail["run"]["input_sha256"], detail["baseline"]["input_sha256"])
        self.assertEqual(detail["run"]["row_count"], detail["baseline"]["row_count"])

    def test_missing_provenance_is_unverified_sample(self):
        result = self.compare("base", "bare")
        run = self.run_of(result, "bare")
        self.assertEqual((run["status"], run["reasons"]), ("unverified", ["unverified-sample"]))
        self.assertEqual(run["provenance"]["status"], "absent")
        self.assertIn("no-provenance", run["flags"])
        self.assertTrue(all(row["cells"]["bare"]["label"] == "equal" for row in result["metrics"]
                            if row["cells"]["bare"]["value"] is not None))
        # Without provenance on either side, a different declared source checksum is still a different sample.
        bare = compare.load_report(self.path("bare"))
        other = copy.deepcopy(bare)
        other["source"]["sha256"] = "0" * 64
        result = compare_runs([bare, other])
        self.assertEqual(self.run_of(result, "run2")["reasons"], ["different-sample"])
        # A manifest whose body was edited, or which does not list the report's input, is not trusted.
        manifest = provenance.load_json(self.reports / "base.json.manifest.json")
        edited = copy.deepcopy(manifest)
        edited["experiments"][0]["parameters"]["delay"] = 9
        foreign = copy.deepcopy(manifest)
        foreign["inputs"][0]["sha256"] = "f" * 64
        foreign["manifest_sha256"] = provenance.manifest_digest(foreign)
        for bad, flag in ((edited, "provenance-invalid"), (foreign, "provenance-does-not-cover-report-source")):
            result = compare_runs([self.run_entry("base"),
                                   {"name": "x", "report": compare.load_report(self.path("same")), "provenance": bad}])
            run = self.run_of(result, "x")
            self.assertEqual(run["reasons"], ["unverified-sample"])
            self.assertIn(flag, run["flags"])

    def test_split_and_metric_definition_changes_are_named(self):
        result = self.compare("base", "resplit")
        self.assertEqual(self.run_of(result, "resplit")["reasons"], ["different-splits"])
        stress = stress_walk_forward(market(), Config(horizon=5, delay=2),
                                     [{"test_start": 60, "test_stop": 100}], 140)
        result = compare_runs([self.run_entry("base"), {"name": "stress", "report": stress}])
        run = self.run_of(result, "stress")
        self.assertEqual(run["format"], "stress_walk_forward")
        self.assertEqual(run["reasons"], ["different-metrics", "different-splits", "unverified-sample"])
        self.assertEqual(run["status"], "incompatible")

    def test_losing_run_is_retained_and_labelled(self):
        result = self.compare("slower", "base")
        run = self.run_of(result, "base")
        self.assertEqual(run["status"], "compatible")
        self.assertIn("worse-than-baseline", run["flags"])
        self.assertGreater(run["label_counts"]["worse"], 0)
        worse = 0
        for row in result["metrics"]:
            cell, reference = row["cells"]["base"], row["cells"]["slower"]
            if row["direction"] and cell["value"] is not None and reference["value"] is not None:
                lower = cell["value"] < reference["value"]
                expected = "equal" if cell["value"] == reference["value"] else (
                    "worse" if lower == (row["direction"] == "higher") else "better")
                self.assertEqual(cell["label"], expected, row["metric"])
                worse += expected == "worse"
        self.assertEqual(worse, run["label_counts"]["worse"])
        # The same runs with the other baseline keep everything too.
        self.assertEqual(len(self.compare("base", "slower")["metrics"]), len(result["metrics"]))
        self.assertEqual(self.compare("slower", "base", baseline="base")["baseline"], "base")

    def test_every_format_contains_every_run(self):
        names = ("base", "same", "costly", "changed", "bare", "slower", "resplit")
        result = self.compare(*names)
        self.assertEqual([run["name"] for run in result["runs"]], list(names))
        parsed = json.loads(render(result, "json"))
        self.assertEqual([run["name"] for run in parsed["runs"]], list(names))
        self.assertTrue(all(set(row["cells"]) == set(names) for row in parsed["metrics"]))
        for fmt in ("text", "markdown"):
            output = render(result, fmt)
            header = next(line for line in output.splitlines() if "metric" in line and "(baseline)" in line)
            for name in names:
                self.assertIn(name, header, fmt)
            for reason in ("different-costs", "different-sample", "different-splits", "unverified-sample",
                           "worse-than-baseline"):
                self.assertIn(reason, output, fmt)
        with self.assertRaises(ValueError):
            compare_runs([self.run_entry("base")])
        with self.assertRaises(ValueError):
            self.compare("base", "same", baseline="absent")

    def test_cli_exit_codes(self):
        code, out, _ = quiet(compare.main, [str(self.path("base")), str(self.path("costly")),
                                            str(self.path("bare")), "--format", "json"])
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual([run["status"] for run in result["runs"]], ["baseline", "incompatible", "unverified"])
        self.assertEqual(result["runs"][0]["provenance"]["status"], "declared")  # sidecar picked up
        code, out, _ = quiet(compare.main, [str(self.path("base")), str(self.path("slower")),
                                            "--baseline", "slower", "--format", "markdown"])
        self.assertEqual(code, 0)
        self.assertIn("Baseline: `slower`", out)
        code, out, _ = quiet(compare.main, [str(self.path("base")), str(self.path("bare")),
                                            "--manifest", f"bare={self.reports / 'base.json.manifest.json'}"])
        self.assertEqual(code, 0)
        self.assertIn("bare: status=compatible", out)
        garbage, other = self.root / "garbage.json", self.root / "other.json"
        garbage.write_text("{not json", encoding="utf-8")
        other.write_text('{"schema_version": 1, "folds": []}', encoding="utf-8")
        for args in ([str(self.path("base")), str(self.root / "absent.json")],
                     [str(self.path("base")), str(garbage)],
                     [str(self.path("base")), str(other)],
                     [str(self.path("base"))],
                     [str(self.path("base")), str(self.path("same")), "--baseline", "nobody"],
                     [str(self.path("base")), str(self.path("same")), "--manifest", "same=" + str(garbage)]):
            with self.subTest(args=args):
                code, out, err = quiet(compare.main, args)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("error:", err)


if __name__ == "__main__":
    unittest.main()
