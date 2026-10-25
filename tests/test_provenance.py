"""Provenance manifests: re-hashing, change attribution, CLI exit codes, walk-forward gate."""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from research import data_quality, provenance, walk_forward
from research.lab import Snapshot
from research.provenance import ManifestError, build_manifest, verify_manifest
from research.walk_forward import evaluate_walk_forward, main as walk_forward_main

STAMP = "2026-10-16T09:00:00+02:00"


def market(count=180):
    return [Snapshot(i + 1, 100 + i // 3, 102 + i // 3, 20 + i % 11, 10 + i % 7) for i in range(count)]


def bbo_bytes(rows):
    return ("seq,bid_price,ask_price,bid_qty,ask_qty\n" + "".join(
        f"{r.seq},{r.bid_price},{r.ask_price},{r.bid_qty},{r.ask_qty}\n" for r in rows)).encode("utf-8")


def quiet(function, *args):
    with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
        code = function(*args)
    return code, out.getvalue()


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.root = Path(self._folder.name)
        (self.root / "a.csv").write_bytes(b"seq,value\n1,10\n2,20\n")
        (self.root / "data").mkdir()
        (self.root / "data" / "b.csv").write_bytes(b"seq,value\n1,30\n")
        self.inputs = [{"path": "a.csv", "source": "synthetic:generate.py seed=7", "retrieved_at": STAMP},
                       {"path": "data/b.csv", "source": "synthetic:generate.py seed=8",
                        "retrieved_at": "2026-10-16T07:00:00Z"}]
        self.steps = [{"name": "replay", "parameters": {"z": 1, "a": [1, 2]}},
                      {"name": "walk_forward", "parameters": {"horizon": 5, "delay": 2}}]
        self.experiments = [{"id": "exp-a", "parameters": {"seed": 7}, "inputs": ["a.csv"]},
                            {"id": "exp-b", "parameters": {"seed": 8}, "inputs": ["data/b.csv"]},
                            {"id": "exp-both", "parameters": {}}]

    def tearDown(self):
        self._folder.cleanup()

    def build(self):
        return build_manifest(self.inputs, self.steps, self.experiments, root=self.root)

    def test_build_records_hashes_steps_and_verifies(self):
        manifest = self.build()
        first = manifest["inputs"][0]
        self.assertEqual(first["bytes"], len((self.root / "a.csv").read_bytes()))
        self.assertEqual(first["sha256"], provenance.file_digest(self.root / "a.csv")[0])
        self.assertEqual([s["name"] for s in manifest["transformations"]], ["replay", "walk_forward"])
        self.assertEqual([s["step"] for s in manifest["transformations"]], [0, 1])
        self.assertEqual(manifest["experiments"][2]["inputs"], ["a.csv", "data/b.csv"])
        self.assertEqual(manifest["manifest_sha256"], provenance.manifest_digest(manifest))
        # Parameter key order cannot change the manifest hash.
        self.steps[0]["parameters"] = {"a": [1, 2], "z": 1}
        self.assertEqual(self.build()["manifest_sha256"], manifest["manifest_sha256"])
        self.assertEqual(verify_manifest(manifest, self.root),
                         {"valid": True, "manifest_hash_ok": True, "changed": [], "missing": [],
                          "affected_experiments": []})

    def test_one_changed_byte_invalidates_only_experiments_using_it(self):
        manifest = self.build()
        raw = bytearray((self.root / "a.csv").read_bytes())
        raw[-2] ^= 1
        (self.root / "a.csv").write_bytes(bytes(raw))
        report = verify_manifest(manifest, self.root)
        self.assertFalse(report["valid"])
        self.assertTrue(report["manifest_hash_ok"])
        self.assertEqual(report["changed"], ["a.csv"])
        self.assertEqual(report["missing"], [])
        self.assertEqual(report["affected_experiments"], ["exp-a", "exp-both"])

    def test_missing_input_is_reported(self):
        manifest = self.build()
        (self.root / "data" / "b.csv").unlink()
        report = verify_manifest(manifest, self.root)
        self.assertFalse(report["valid"])
        self.assertEqual((report["changed"], report["missing"]), ([], ["data/b.csv"]))
        self.assertEqual(report["affected_experiments"], ["exp-b", "exp-both"])
        (self.root / "data" / "b.csv").mkdir()  # a directory is not the declared file
        self.assertEqual(verify_manifest(manifest, self.root)["missing"], ["data/b.csv"])

    def test_tampered_declared_hash_is_caught_by_rehashing(self):
        manifest = self.build()
        forged = copy.deepcopy(manifest)
        forged["inputs"][1]["sha256"] = "0" * 64
        report = verify_manifest(forged, self.root)
        self.assertFalse(report["valid"])
        self.assertEqual(report["changed"], ["data/b.csv"])
        # Even with a recomputed manifest hash, re-hashing the file exposes the forgery.
        forged["manifest_sha256"] = provenance.manifest_digest(forged)
        report = verify_manifest(forged, self.root)
        self.assertTrue(report["manifest_hash_ok"])
        self.assertEqual((report["valid"], report["changed"]), (False, ["data/b.csv"]))
        # Every input is re-read on each verification; declared hashes are never trusted.
        with patch.object(provenance, "file_digest", wraps=provenance.file_digest) as spy:
            verify_manifest(manifest, self.root)
        self.assertEqual(spy.call_count, 2)

    def test_edited_body_invalidates_all_experiments(self):
        manifest = self.build()
        manifest["experiments"][0]["parameters"]["seed"] = 99
        report = verify_manifest(manifest, self.root)
        self.assertEqual((report["valid"], report["manifest_hash_ok"]), (False, False))
        self.assertEqual(report["affected_experiments"], ["exp-a", "exp-b", "exp-both"])

    def test_rejects_malformed_declarations(self):
        bad_inputs = [{"path": "/abs.csv"}, {"path": "C:/x.csv"}, {"path": "a\\b.csv"}, {"path": "./a.csv"},
                      {"path": "x/../a.csv"}, {"retrieved_at": "2026-10-16T09:00:00"},
                      {"retrieved_at": "yesterday"}, {"source": ""}, {"path": "nope.csv"}]
        for change in bad_inputs:
            with self.subTest(change), self.assertRaises(ManifestError):
                build_manifest([{**self.inputs[0], **change}], self.steps, {"id": "e", "parameters": {}},
                               root=self.root)
        for steps, experiment in [([{"name": "x", "parameters": {"v": float("nan")}}], {"id": "e", "parameters": {}}),
                                  ([{"name": "x"}], {"id": "e", "parameters": {}}),
                                  (self.steps, {"id": "bad id", "parameters": {}}),
                                  (self.steps, [{"id": "e", "parameters": {}}, {"id": "e", "parameters": {}}]),
                                  (self.steps, {"id": "e", "parameters": {}, "inputs": ["other.csv"]})]:
            with self.subTest(steps=steps, experiment=experiment), self.assertRaises(ManifestError):
                build_manifest([self.inputs[0]], steps, experiment, root=self.root)
        manifest = self.build()
        for mutate in (lambda m: m.pop("limitations"), lambda m: m["inputs"][0].update(bytes=-1),
                       lambda m: m["inputs"][0].update(sha256="ABC"),
                       lambda m: m["inputs"][0].update(path="../../escape.csv/..")):
            broken = copy.deepcopy(manifest)
            mutate(broken)
            with self.assertRaises(ManifestError):
                verify_manifest(broken, self.root)

    def test_cli_rejects_expanded_manifest_before_creating_output(self):
        for name, parameters in (("unicode", {"note": "\u00e9" * 256}),
                                 ("pretty", {"values": list(range(64))})):
            with self.subTest(name=name):
                self.experiments[0]["parameters"] = parameters
                spec = self.root / f"{name}-spec.json"
                spec.write_text(json.dumps({"inputs": self.inputs, "transformations": self.steps,
                                            "experiment": self.experiments}, ensure_ascii=False,
                                           separators=(",", ":")), encoding="utf-8")
                manifest = self.build()
                emitted = (json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True,
                                      allow_nan=False) + "\n").encode("utf-8")
                limit = len(emitted) - 1
                self.assertLess(spec.stat().st_size, limit)
                target = self.root / name / "manifest.json"
                with patch.object(provenance, "MAX_MANIFEST_BYTES", limit), \
                        redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                    code = provenance.main(["build", "--spec", str(spec), "--output", str(target),
                                            "--root", str(self.root)])
                self.assertEqual(code, 2)
                self.assertIn("size limit", err.getvalue())
                self.assertEqual(out.getvalue(), "")
                self.assertFalse(target.parent.exists())

    def test_writer_rejects_one_byte_over_limit_without_creating_directories(self):
        manifest = self.build()
        emitted = (json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True,
                              allow_nan=False) + "\n").encode("utf-8")
        target = self.root / "absent" / "nested" / "manifest.json"
        with patch.object(provenance, "MAX_MANIFEST_BYTES", len(emitted) - 1):
            with self.assertRaisesRegex(ManifestError, "size limit"):
                provenance.write_manifest(manifest, target)
        self.assertFalse((self.root / "absent").exists())

    def test_exact_size_limit_preserves_format_and_cli_roundtrip(self):
        self.experiments[0]["parameters"]["note"] = "\u00e9\n"
        manifest = self.build()
        emitted = (json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True,
                              allow_nan=False) + "\n").encode("utf-8")
        target = self.root / "exact.json"
        with patch.object(provenance, "MAX_MANIFEST_BYTES", len(emitted)):
            provenance.write_manifest(manifest, target)
            self.assertEqual(target.read_bytes(), emitted)
            code, out = quiet(provenance.main, ["verify", str(target)])
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(out)["valid"])
            with self.assertRaises(FileExistsError):
                provenance.write_manifest(manifest, target)
            self.assertEqual(target.read_bytes(), emitted)

    def test_cli_build_and_verify_exit_codes(self):
        spec = self.root / "spec.json"
        spec.write_text(json.dumps({"inputs": self.inputs, "transformations": self.steps,
                                    "experiment": self.experiments}), encoding="utf-8")
        target = self.root / "manifest.json"
        code, _ = quiet(provenance.main, ["build", "--spec", str(spec), "--output", str(target)])
        self.assertEqual(code, 0)
        saved = target.read_bytes()
        code, out = quiet(provenance.main, ["verify", str(target)])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["valid"])
        # Never overwrite an existing manifest.
        self.assertEqual(quiet(provenance.main, ["build", "--spec", str(spec), "--output", str(target)])[0], 2)
        self.assertEqual(target.read_bytes(), saved)
        (self.root / "a.csv").write_bytes(b"changed")
        code, out = quiet(provenance.main, ["verify", str(target)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["changed"], ["a.csv"])
        (self.root / "bad.json").write_text('{"kind": 1, "kind": 2}', encoding="utf-8")
        self.assertEqual(quiet(provenance.main, ["verify", str(self.root / "bad.json")])[0], 2)
        self.assertEqual(quiet(provenance.main, ["verify", str(self.root / "absent.json")])[0], 2)
        elsewhere = self.root / "data" / "manifest.json"
        self.assertEqual(quiet(provenance.main, ["build", "--spec", str(spec), "--output", str(elsewhere)])[0], 2)
        self.assertEqual(quiet(provenance.main, ["build", "--spec", str(spec), "--output", str(elsewhere),
                                                 "--root", str(self.root)])[0], 0)
        self.assertEqual(quiet(provenance.main, ["verify", str(elsewhere), "--root", str(self.root)])[0], 0)
        code, out = quiet(provenance.main, ["verify", str(elsewhere)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["missing"], ["a.csv", "data/b.csv"])


class ManifestPublicationTests(unittest.TestCase):
    SENTINEL = b"existing evidence\x00\xff"

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        (self.root / "input.csv").write_bytes(b"synthetic input\n")
        self.spec = self.root / "spec.json"
        self.spec.write_text(json.dumps({
            "inputs": [{"path": "input.csv", "source": "synthetic", "retrieved_at": STAMP}],
            "transformations": [], "experiment": {"id": "e", "parameters": {}}}), encoding="utf-8")
        self.output = self.root / "out" / "manifest.json"
        self.output.parent.mkdir()
        self.referent = self.root / "referent.bin"

    def run_cli(self):
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            code = provenance.main(["build", "--spec", str(self.spec), "--output", str(self.output),
                                    "--root", str(self.root)])
        return code, out.getvalue(), err.getvalue()

    def assert_no_staging(self):
        self.assertEqual(list(self.output.parent.glob(".provenance-manifest-*")), [])

    def make_competitor(self, kind):
        if kind == "file":
            self.output.write_bytes(self.SENTINEL)
        elif kind == "directory":
            self.output.mkdir()
            (self.output / "keep.bin").write_bytes(self.SENTINEL)
        else:
            if kind == "symlink":
                self.referent.write_bytes(self.SENTINEL)
            try:
                self.output.symlink_to(self.referent)
                self.link_target_before = self.output.readlink()
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")

    def assert_competitor(self, kind):
        if kind == "file":
            self.assertEqual(self.output.read_bytes(), self.SENTINEL)
        elif kind == "directory":
            self.assertEqual((self.output / "keep.bin").read_bytes(), self.SENTINEL)
        else:
            self.assertTrue(self.output.is_symlink())
            self.assertEqual(self.output.readlink(), self.link_target_before)
            if kind == "symlink":
                self.assertEqual(self.referent.read_bytes(), self.SENTINEL)
            else:
                self.assertFalse(self.referent.exists())

    def existing_collision(self, kind):
        self.make_competitor(kind)
        code, out, err = self.run_cli()
        self.assertEqual(code, 2, err)
        self.assertEqual(out, "")
        self.assertTrue(err)
        self.assert_competitor(kind)
        self.assert_no_staging()

    def late_collision(self, kind):
        real_link = os.link

        def compete_then_link(source, destination):
            # A complete, valid artifact must be staged before publication.
            manifest = provenance.load_json(source)
            self.assertTrue(verify_manifest(manifest, self.root)["valid"])
            self.make_competitor(kind)
            return real_link(source, destination)

        with patch.object(os, "link", side_effect=compete_then_link) as publish:
            code, out, err = self.run_cli()
        publish.assert_called_once()
        self.assertEqual(code, 2, err)
        self.assertEqual(out, "")
        self.assertTrue(err)
        self.assert_competitor(kind)
        self.assert_no_staging()

    def test_existing_file_is_preserved(self):
        self.existing_collision("file")

    def test_existing_directory_is_preserved(self):
        self.existing_collision("directory")

    def test_existing_symlink_and_referent_are_preserved(self):
        self.existing_collision("symlink")

    def test_existing_dangling_symlink_does_not_create_referent(self):
        self.existing_collision("dangling")

    def test_late_file_is_preserved(self):
        self.late_collision("file")

    def test_late_directory_is_preserved(self):
        self.late_collision("directory")

    def test_late_symlink_and_referent_are_preserved(self):
        self.late_collision("symlink")

    def test_late_dangling_symlink_does_not_create_referent(self):
        self.late_collision("dangling")

    def test_interrupted_write_does_not_leave_partial_manifest(self):
        real_open, attempts = Path.open, []

        @contextmanager
        def interrupted_stream(path, *args, **kwargs):
            with real_open(path, *args, **kwargs) as stream:
                def write(text):
                    attempts.append(path)
                    stream.write(text[:7])
                    raise OSError("synthetic interrupted write")
                yield SimpleNamespace(write=write)

        def open_stream(path, *args, **kwargs):
            if path.name == "manifest.json" and args and args[0] == "x":
                return interrupted_stream(path, *args, **kwargs)
            return real_open(path, *args, **kwargs)

        with patch.object(Path, "open", open_stream):
            code, out, err = self.run_cli()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("synthetic interrupted write", err)
        self.assertFalse(self.output.exists())
        self.assert_no_staging()

    def test_link_failure_leaves_no_output_or_staging(self):
        with patch.object(os, "link", side_effect=OSError("synthetic unsupported hard link")) as publish:
            code, out, err = self.run_cli()
        publish.assert_called_once()
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("synthetic unsupported hard link", err)
        self.assertFalse(self.output.exists())
        self.assert_no_staging()

    def test_fresh_manifest_roundtrip_cleans_staging(self):
        code, out, err = self.run_cli()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["written"], str(self.output))
        code, report = quiet(provenance.main, ["verify", str(self.output), "--root", str(self.root)])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(report)["valid"])
        self.assert_no_staging()


class WalkForwardManifestTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.root = Path(self._folder.name)
        (self.root / "data").mkdir()
        self.source = self.root / "data" / "bbo.csv"
        self.raw = bbo_bytes(market())
        self.source.write_bytes(self.raw)
        self.base = ["--input", str(self.source), "--data-kind", "synthetic",
                     "--initial-train", "60", "--test-size", "40", "--horizon", "5", "--delay", "2"]

    def tearDown(self):
        self._folder.cleanup()

    def run_main(self, *extra):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            try:
                return walk_forward_main([*self.base, *extra]), err.getvalue()
            except SystemExit as exit_:
                return exit_.code, err.getvalue()

    def policy_fixture(self, action):
        policy = {"max_gap_seconds": 10, "max_staleness_seconds": 30, "on_gap": action,
                  "forward_fill_limit_seconds": 20}
        policy_path = self.root / "policy.json"
        policy_path.write_text(json.dumps(policy), encoding="utf-8")
        self.source.write_text("seq,bid_price,ask_price,bid_qty,ask_qty,timestamp\n" + "".join(
            f"{r.seq},{r.bid_price},{r.ask_price},{r.bid_qty},{r.ask_qty},{i + (500 if i >= 120 else 0)}\n"
            for i, r in enumerate(market())), encoding="utf-8")
        return policy_path, policy

    def test_policy_changed_after_manifest_verification_refuses_before_evaluation(self):
        policy_path, policy = self.policy_fixture("reject")
        manifest = build_manifest(
            [{"path": path, "source": "synthetic fixture", "retrieved_at": STAMP}
             for path in ("data/bbo.csv", "policy.json")], [], {"id": "e", "parameters": {}}, root=self.root)
        manifest_path = self.root / "m.json"
        provenance.write_manifest(manifest, manifest_path)
        original_verify = provenance.verify_manifest

        def verify_then_change(*args, **kwargs):
            result = original_verify(*args, **kwargs)
            policy_path.write_text(json.dumps({**policy, "on_gap": "mark"}), encoding="utf-8")
            return result

        output = self.root / "out.json"
        with patch.object(provenance, "verify_manifest", side_effect=verify_then_change), \
                patch.object(walk_forward, "evaluate_walk_forward", wraps=evaluate_walk_forward) as evaluate:
            code, error = self.run_main("--output", str(output), "--manifest", str(manifest_path),
                                        "--data-quality", str(policy_path))
        self.assertEqual(code, 2)
        self.assertIn("policy changed after manifest verification", error)
        self.assertEqual(evaluate.call_count, 0)
        self.assertFalse(output.exists())

    def test_policy_changed_while_building_manifest_writes_nothing(self):
        policy_path, policy = self.policy_fixture("mark")

        def evaluate_then_change(*args, **kwargs):
            result = evaluate_walk_forward(*args, **kwargs)
            policy_path.write_text(json.dumps({**policy, "on_gap": "reject"}), encoding="utf-8")
            return result

        output = self.root / "out.json"
        with patch.object(walk_forward, "evaluate_walk_forward", side_effect=evaluate_then_change):
            code, error = self.run_main("--output", str(output), "--write-manifest", "--input-source",
                                        "synthetic fixture", "--input-retrieved-at", STAMP,
                                        "--data-quality", str(policy_path))
        self.assertEqual(code, 2)
        self.assertIn("policy changed while building the manifest", error)
        self.assertFalse(output.exists())
        self.assertFalse(output.with_name(output.name + ".manifest.json").exists())

    def test_unchanged_policy_manifest_writes_and_verifies_a_rerun(self):
        policy_path, policy = self.policy_fixture("mark")
        output = self.root / "first.json"
        code, error = self.run_main("--output", str(output), "--write-manifest", "--input-source",
                                    "synthetic fixture", "--input-retrieved-at", STAMP,
                                    "--data-quality", str(policy_path))
        self.assertEqual((code, error), (0, ""))
        manifest_path = output.with_name(output.name + ".manifest.json")
        manifest = provenance.load_json(manifest_path)
        self.assertTrue(verify_manifest(manifest, self.root)["valid"])
        self.assertEqual(manifest["inputs"][1]["sha256"], data_quality.read_policy_source(policy_path)[1])
        rerun = self.root / "second.json"
        code, error = self.run_main("--output", str(rerun), "--manifest", str(manifest_path),
                                    "--data-quality", str(policy_path))
        self.assertEqual((code, error), (0, ""))
        report = json.loads(rerun.read_text(encoding="utf-8"))
        self.assertEqual(report["data_quality"]["policy"], policy)
        self.assertEqual(report["provenance"]["status"], "inputs_rehashed_and_matched")

    def test_used_policy_must_be_listed_in_manifest(self):
        policy_path, _ = self.policy_fixture("mark")
        manifest = build_manifest([{"path": "data/bbo.csv", "source": "synthetic", "retrieved_at": STAMP}],
                                  [], {"id": "e", "parameters": {}}, root=self.root)
        manifest_path = self.root / "m.json"
        provenance.write_manifest(manifest, manifest_path)
        output = self.root / "out.json"
        with patch.object(walk_forward, "evaluate_walk_forward", wraps=evaluate_walk_forward) as evaluate:
            code, error = self.run_main("--output", str(output), "--manifest", str(manifest_path),
                                        "--data-quality", str(policy_path))
        self.assertEqual(code, 2)
        self.assertIn("--data-quality", error)
        self.assertIn("is not an input of manifest", error)
        self.assertEqual(evaluate.call_count, 0)
        self.assertFalse(output.exists())

    def test_written_manifest_verifies_and_gates_a_rerun(self):
        output = self.root / "reports" / "run1.json"
        code, _ = self.run_main("--output", str(output), "--write-manifest", "--input-source",
                                "synthetic:test market()", "--input-retrieved-at", STAMP, "--experiment-id", "wf-1")
        self.assertEqual(code, 0)
        manifest_path = self.root / "reports" / "run1.json.manifest.json"
        manifest = provenance.load_json(manifest_path)
        self.assertEqual(manifest["inputs"][0]["path"], "../data/bbo.csv")
        self.assertEqual(manifest["experiments"][0]["id"], "wf-1")
        self.assertEqual(manifest["experiments"][0]["parameters"]["horizon"], 5)
        self.assertEqual(manifest["transformations"][-1]["name"], "research.walk_forward.evaluate_walk_forward")
        self.assertTrue(verify_manifest(manifest, manifest_path.parent)["valid"])
        rerun = self.root / "reports" / "run2.json"
        code, _ = self.run_main("--output", str(rerun), "--manifest", str(manifest_path))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(rerun.read_text(encoding="utf-8"))["provenance"]["status"],
                         "inputs_rehashed_and_matched")
        # An existing manifest is never overwritten, even if its report was removed.
        saved = manifest_path.read_bytes()
        output.unlink()
        code, err = self.run_main("--output", str(output), "--write-manifest", "--input-source", "x",
                                  "--input-retrieved-at", STAMP)
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)
        self.assertEqual(manifest_path.read_bytes(), saved)
        self.assertFalse(output.exists())

    def test_walk_forward_refuses_to_run_with_invalid_manifest(self):
        output = self.root / "first.json"
        self.assertEqual(self.run_main("--output", str(output), "--write-manifest", "--input-source",
                                       "synthetic:test", "--input-retrieved-at", STAMP)[0], 0)
        manifest_path = self.root / "first.json.manifest.json"
        changed = bytearray(self.raw)
        changed[-3] = ord("9") if changed[-3] != ord("9") else ord("8")
        self.source.write_bytes(bytes(changed))
        refused = self.root / "refused.json"
        with patch("research.walk_forward.evaluate_walk_forward", wraps=evaluate_walk_forward) as spy:
            code, err = self.run_main("--output", str(refused), "--manifest", str(manifest_path))
        self.assertEqual(code, 2)
        self.assertIn("manifest verification failed", err)
        self.assertIn("data/bbo.csv", err)
        self.assertEqual(spy.call_count, 0)
        self.assertFalse(refused.exists())
        self.source.unlink()
        code, err = self.run_main("--output", str(refused), "--manifest", str(manifest_path))
        self.assertEqual(code, 2)
        self.assertIn("missing=['data/bbo.csv']", err)
        self.assertFalse(refused.exists())

    def test_input_must_be_listed_in_manifest(self):
        other = self.root / "data" / "other.csv"
        other.write_bytes(self.raw)
        manifest = build_manifest([{"path": "data/other.csv", "source": "synthetic", "retrieved_at": STAMP}],
                                  [], {"id": "e", "parameters": {}}, root=self.root)
        provenance.write_manifest(manifest, self.root / "m.json")
        code, err = self.run_main("--output", str(self.root / "out.json"), "--manifest", str(self.root / "m.json"))
        self.assertEqual(code, 2)
        self.assertIn("is not an input of manifest", err)
        self.assertFalse((self.root / "out.json").exists())

    def test_write_manifest_requires_declared_source(self):
        code, err = self.run_main("--output", str(self.root / "out.json"), "--write-manifest")
        self.assertEqual(code, 2)
        self.assertIn("--input-source", err)
        self.assertFalse((self.root / "out.json").exists())


if __name__ == "__main__":
    unittest.main()
