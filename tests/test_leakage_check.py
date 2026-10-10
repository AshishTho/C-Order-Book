"""Bounded syntax-review behavior, explicit blind spots, and offline execution safety."""

from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from research.leakage_check import MAX_BYTES, inspect_source, main, scan_file

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "leakage"


class LeakageCheckTests(unittest.TestCase):
    def test_broken_and_fixed_features(self):
        broken = scan_file(FIXTURES / "future_feature.py")
        fixed = scan_file(FIXTURES / "past_feature.py")
        self.assertEqual([(f["rule"], f["line"], f["periods"]) for f in broken["findings"]], [("LL001", 5, -1)])
        self.assertEqual(fixed["findings"], [])
        self.assertEqual(fixed["unassessed"], [])
        self.assertEqual(fixed["shift_calls"], 1)

    def test_literal_arguments_multiline_and_utf8_byte_location(self):
        result = inspect_source("é = prices.shift(periods=-2)\nx = prices.shift(\n -3, None, 'index')\ny = prices.shift(-4, axis=0)\n")
        self.assertEqual([f["periods"] for f in result["findings"]], [-2, -3, -4])
        self.assertEqual([f["line"] for f in result["findings"]], [1, 2, 4])
        self.assertEqual(result["findings"][0]["column_utf8_byte"], 6)

    def test_comments_strings_nonnegative_and_other_methods_do_not_match(self):
        result = inspect_source("# prices.shift(-1)\ns = 'prices.shift(-2)'\na = prices.shift()\nb = prices.shift(-0)\nc = prices.shift(+2)\nd = prices.other(-3)\n")
        self.assertEqual(result["shift_calls"], 3)
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["unassessed"], [])

    def test_dynamic_column_frequency_and_iterable_calls_are_explicitly_unassessed(self):
        calls = ["shift(-lag)", "shift([-1, 1])", "shift(-1, axis=1)", "shift(-1, None, 'columns')",
                 "shift(-1, freq='D')", "shift(-1, frequency)", "shift(-1, axis=axis)", "shift(**kwargs)",
                 "shift(*args)", "shift(-1, periods=-2)", "shift(periods=-True)", "shift(-1, strange=True)"]
        result = inspect_source("\n".join("prices." + call for call in calls))
        self.assertEqual(result["findings"], [])
        self.assertEqual(len(result["unassessed"]), len(calls))
        self.assertTrue(all(r["reason"] for r in result["unassessed"]))

    def test_future_labels_and_unrelated_shift_methods_remain_review_candidates(self):
        result = inspect_source("future_target = close.shift(-1)\nunrelated_object.shift(-2)\n")
        self.assertEqual(len(result["findings"]), 2)
        self.assertTrue(all(f["severity"] == "review" and "future target" in f["review"] for f in result["findings"]))

    def test_centered_and_trailing_feature_fixtures(self):
        broken = scan_file(FIXTURES / "centered_feature.py")
        fixed = scan_file(FIXTURES / "trailing_feature.py")
        self.assertEqual([(f["rule"], f["line"], f["window"]) for f in broken["findings"]], [("LL002", 5, 3)])
        self.assertEqual(broken["unassessed"], [])
        self.assertEqual(fixed["findings"], [])
        self.assertEqual(fixed["unassessed"], [])
        self.assertEqual(fixed["rolling_calls"], 1)
        self.assertEqual(fixed["shift_calls"], 0)

    def test_rolling_literal_forms_and_minimum_future_window(self):
        cases = [("rolling(3, center=True)", 3), ("rolling(window=4, center=True)", 4),
                 ("rolling(+5, None, True)", 5), ("rolling(3, 0, True, closed='right')", 3),
                 ("rolling(3, 3, True, axis='index', closed=None)", 3),
                 ("rolling(3, center=True, axis=0)", 3), ("rolling(1, center=True)", None),
                 ("rolling(2, center=True)", None), ("rolling(3)", None),
                 ("rolling(3, center=False)", None)]
        for expression, window in cases:
            with self.subTest(expression=expression):
                result = inspect_source("close." + expression)
                self.assertEqual(result["rolling_calls"], 1)
                self.assertEqual(result["unassessed"], [])
                self.assertEqual([f["window"] for f in result["findings"]], [] if window is None else [window])

    def test_unsupported_rolling_forms_are_explicit(self):
        groups = {
            "ambiguous_or_unsupported_rolling_arguments": [
                "rolling(3, None, True, None)", "rolling(3, center=True, window=4)",
                "rolling(3, center=True, center=False)", "rolling(*args)", "rolling(3, **kwargs)",
                "rolling(3, center=True, on='time')", "rolling(3, center=True, win_type='gaussian')",
                "rolling(3, center=True, step=2)", "rolling(3, center=True, method='single')"],
            "column_or_dynamic_rolling_axis": ["rolling(3, center=True, axis=1)",
                "rolling(3, center=True, axis='columns')", "rolling(3, center=True, axis=axis)",
                "rolling(3, center=True, axis=None)", "rolling(3, center=True, axis=False)"],
            "nondefault_or_dynamic_rolling_closure": ["rolling(3, center=True, closed='left')",
                "rolling(3, center=True, closed='both')", "rolling(3, center=True, closed='neither')",
                "rolling(3, center=True, closed=closure)"],
            "nonpositive_noninteger_or_dynamic_rolling_window": ["rolling(center=True)",
                "rolling(size, center=True)", "rolling('3D', center=True)",
                "rolling(indexer, center=True)", "rolling(0, center=True)", "rolling(-3, center=True)",
                "rolling(True, center=True)", "rolling(3.0, center=True)", "rolling(1 + 2, center=True)"],
            "nonliteral_boolean_rolling_center": ["rolling(3, center=flag)", "rolling(3, center=1)",
                "rolling(3, center=None)", "rolling(3, center=not False)"],
            "unsupported_rolling_min_periods": ["rolling(3, -1, True)", "rolling(3, 4, True)",
                "rolling(3, True, True)", "rolling(3, minimum, True)"]}
        for reason, calls in groups.items():
            for call in calls:
                with self.subTest(call=call):
                    result = inspect_source("close." + call)
                    self.assertEqual(result["findings"], [])
                    self.assertEqual(result["unassessed"],
                                     [{"rule": "LL002", "line": 1, "column_utf8_byte": 1, "reason": reason}])

    def test_mixed_rules_have_stable_source_locations_and_counts(self):
        result = inspect_source("é = close.rolling(\n 3, None, True).mean()\nx = close.shift(-1)\n"
                                "y = close.rolling(4, center=True).mean().shift(-2)\n"
                                "a = close.rolling(width, center=True)\nb = close.shift(-lag)\n")
        self.assertEqual(result["rolling_calls"], 3)
        self.assertEqual(result["shift_calls"], 3)
        self.assertEqual([(f["rule"], f["line"], f["column_utf8_byte"]) for f in result["findings"]],
                         [("LL002", 1, 6), ("LL001", 3, 5), ("LL001", 4, 5), ("LL002", 4, 5)])
        self.assertEqual([u["line"] for u in result["unassessed"]], [5, 6])

    def test_rolling_false_positive_examples_still_need_review(self):
        result = inspect_source("plot = close.rolling(3, center=True).mean()\n"
                                "lagged = close.rolling(3, center=True).mean().shift(1)\n"
                                "unrelated.rolling(3, center=True)\n"
                                "unused = close.rolling(3, center=True)\n")
        self.assertEqual(len(result["findings"]), 4)
        self.assertTrue(all(f["severity"] == "review" and "delay" in f["review"] for f in result["findings"]))

    def test_rolling_comments_strings_and_aliases_are_not_detected(self):
        result = inspect_source("# close.rolling(3, center=True)\ntext = 'close.rolling(3, center=True)'\n"
                                "alias = close.rolling\nalias(3, center=True)\nrolling(3, center=True)\n")
        self.assertEqual(result["rolling_calls"], 0)
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["unassessed"], [])

    def test_source_is_hashed_but_never_imported_executed_or_echoed(self):
        with tempfile.TemporaryDirectory() as folder:
            path, marker = Path(folder) / "untrusted.py", Path(folder) / "executed.txt"
            raw = ("from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('executed')\n"
                   "secret = 'PRIVATE_TEST_SENTINEL'\nresult = close.shift(-1)\ncentered = close.rolling(3, center=True).mean()\n").encode("utf-8")
            path.write_bytes(raw)
            result = scan_file(path)
            self.assertEqual(result["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(path.read_bytes(), raw)
            self.assertFalse(marker.exists())
            self.assertEqual([f["rule"] for f in result["findings"]], ["LL001", "LL002"])
            self.assertNotIn("PRIVATE_TEST_SENTINEL", json.dumps(result))

    def test_read_decode_parse_and_size_errors_remain_incomplete(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name, raw, expected in [("bad.py", b"secret = 'PRIVATE_TEST_SENTINEL", "invalid_python"),
                                        ("encoding.py", b"\xff", "input_must_be_utf8"),
                                        ("large.py", b" " * (MAX_BYTES + 1), "file_exceeds_1_MiB_limit")]:
                path = root / name
                path.write_bytes(raw)
                result = scan_file(path)
                self.assertEqual(result["error"], expected)
                self.assertNotIn("PRIVATE_TEST_SENTINEL", json.dumps(result))
            self.assertEqual(scan_file(root / "missing.py")["error"], "file_not_readable")
            self.assertEqual(scan_file(root)["error"], "file_not_readable")

    def test_cli_exit_codes_and_non_certification_report(self):
        for name, code, count in [("future_feature.py", 1, 1), ("past_feature.py", 0, 0),
                                  ("centered_feature.py", 1, 1), ("trailing_feature.py", 0, 0), ("missing.py", 2, 0)]:
            with self.subTest(name=name):
                run = subprocess.run([sys.executable, "-m", "research.leakage_check", str(FIXTURES / name)],
                                     cwd=ROOT, capture_output=True, text=True, check=False)
                self.assertEqual(run.returncode, code, run.stderr)
                report = json.loads(run.stdout)
                self.assertFalse(report["certifies_leakage_free"])
                self.assertEqual(report["summary"]["review_candidates"], count)
                self.assertTrue(any("execution-timing" in text for text in report["limitations"]))

    def test_unassessed_calls_are_visible_even_without_findings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "dynamic.py"
            path.write_text("feature = close.shift(-horizon)\ncentered = close.rolling(width, center=True)\n", encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                code = main([str(path)])
            report = json.loads(output.getvalue())
            self.assertEqual(code, 0)  # Means no supported candidates, never "safe".
            self.assertEqual(report["summary"], {"review_candidates": 0, "unassessed_calls": 2, "file_errors": 0})
            self.assertFalse(report["certifies_leakage_free"])


if __name__ == "__main__":
    unittest.main()
