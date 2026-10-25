"""Saved-result inspection never recalculates evidence or overwrites an artifact."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from research import lab, replay_viewer, walk_forward
from research.lab import Config, Snapshot


class SavedReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rows = [Snapshot(i + 1, 100 + i // 3, 102 + i // 3, 20 + i % 11, 10 + i % 7)
                for i in range(200)]
        cls.report = lab.evaluate(rows, Config(), fee_grid=[], delay_grid=[])
        cls.report['source'] = {'path': 'original-file-does-not-exist.csv',
                                'sha256': 'a' * 64, 'data_kind': 'synthetic'}
        cls.report['provenance'] = {'status': 'original declaration, not checked by this exporter'}
        cls.folds = walk_forward.evaluate_walk_forward(rows, Config(), initial_train=80, test_size=40)
        cls.folds['source'] = cls.report['source'].copy()

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.source, self.output = self.root / 'saved.json', self.root / 'replay.html'

    def save(self, document):
        raw = b'\xef\xbb\xbf' + json.dumps(document, indent=2).replace('\n', '\r\n').encode()
        self.source.write_bytes(raw)
        return raw

    def payload(self):
        text = self.output.read_text(encoding='utf-8')
        encoded = re.search(r'<script id="replayData" type="application/json">(.*?)</script>', text, re.S)
        return json.loads(encoded.group(1)), text

    def test_saved_holdout_needs_no_csv_and_preserves_bytes_config_and_recorded_results(self):
        raw = self.save(self.report)
        with ExitStack() as stack:
            for name in ('evaluate', 'simulate', 'calibrate', 'read_snapshot_source'):
                stack.enter_context(patch.object(lab, name, side_effect=AssertionError('must not rerun')))
            stack.enter_context(patch.object(walk_forward, 'evaluate_walk_forward', side_effect=AssertionError('must not rerun')))
            evidence = replay_viewer.export_saved_report(self.source, self.output)
        payload, text = self.payload()
        self.assertEqual(payload, replay_viewer.replay_payload(self.report))
        self.assertEqual(evidence['saved_report_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(evidence['recorded_metadata']['provenance'], self.report['provenance'])
        self.assertIn('have not been reverified', text)
        self.assertEqual(self.source.read_bytes(), raw)
        self.assertFalse((self.root / self.report['source']['path']).exists())

    def test_explicit_saved_fold_selection_keeps_correct_boundary_and_parent_source(self):
        raw = self.save(self.folds)
        replay_viewer.export_saved_report(self.source, self.output, fold=1)
        payload, text = self.payload()
        self.assertEqual(payload['split'], self.folds['folds'][1]['split'])
        self.assertEqual(payload['source'], self.folds['source'])
        self.assertEqual(payload['strategies'], replay_viewer.replay_payload(self.folds['folds'][1])['strategies'])
        self.assertIn('Saved walk-forward fold 1', text)
        self.assertEqual(self.source.read_bytes(), raw)

    def test_invalid_fold_and_unsupported_structure_publish_nothing(self):
        for document, fold in ((self.folds, None), (self.folds, -1), (self.folds, 99),
                               (self.report, 0), ({'schema_version': 99}, None),
                               ({'schema_version': 1, 'strategies': {'broken': {}}}, None)):
            with self.subTest(fold=fold):
                raw = self.save(document)
                with self.assertRaises(ValueError):
                    replay_viewer.export_saved_report(self.source, self.output, fold=fold)
                self.assertEqual(self.source.read_bytes(), raw)
                self.assertFalse(self.output.exists())

    def test_legacy_reports_and_untrusted_labels_remain_inert(self):
        report = copy.deepcopy(self.report)
        del report['book_snapshots']
        name = '</script><script>window.injected=true</script>'
        report['strategies'][name] = report['strategies'].pop('imbalance')
        report['provenance']['path'] = name
        self.save(report)
        replay_viewer.export_saved_report(self.source, self.output)
        payload, text = self.payload()
        self.assertFalse(payload['invariants']['bookRecorded'])
        self.assertIn(name, payload['strategies'])
        self.assertNotIn('<script>window.injected', text)
        self.assertIn('Legacy reports without book snapshots', text)

    def test_existing_output_and_input_alias_are_preserved(self):
        raw = self.save(self.report)
        self.output.write_bytes(b'previous evidence')
        for destination in (self.source, self.output):
            with self.assertRaises(ValueError):
                replay_viewer.export_saved_report(self.source, destination)
        self.assertEqual(self.source.read_bytes(), raw)
        self.assertEqual(self.output.read_bytes(), b'previous evidence')

    def test_numeric_looking_costs_and_quantities_are_rejected_before_arithmetic(self):
        for field in ('fee_ticks', 'slippage_ticks', 'entry_quantity', 'exit_quantity'):
            invalid = ('2', True, -1, None) if field.endswith('ticks') else ('2', True, 0, -1, 1.5, 2**63)
            for value in invalid:
                with self.subTest(field=field, value=value):
                    report = copy.deepcopy(self.report)
                    if field.endswith('ticks'):
                        report['config'][field] = value
                    else:
                        trade = next(item['execution']['trades'][0] for item in report['strategies'].values()
                                     if item['execution']['trades'])
                        target = trade if field == 'entry_quantity' else trade['exit_fills'][0]
                        target['quantity'] = value
                    raw = self.save(report)
                    with self.assertRaises(ValueError), patch.object(replay_viewer, 'render_replay',
                                                                     side_effect=AssertionError('validation must run first')):
                        replay_viewer.export_saved_report(self.source, self.output)
                    self.assertEqual(self.source.read_bytes(), raw)
                    self.assertFalse(self.output.exists())

    def test_competing_writer_and_failed_serialization_leave_no_partial_output(self):
        self.save(self.report)
        link = os.link
        def compete(staged, final):
            self.assertIn('</html>', Path(staged).read_text(encoding='utf-8'))
            Path(final).write_bytes(b'competitor')
            return link(staged, final)
        with patch('os.link', side_effect=compete), self.assertRaises(FileExistsError):
            replay_viewer.export_saved_report(self.source, self.output)
        self.assertEqual(self.output.read_bytes(), b'competitor')
        failed = self.root / 'failed.html'
        write = Path.write_text
        def interrupted(target, *args, **kwargs):
            write(target, 'partial', encoding='utf-8')
            raise OSError('synthetic interrupted write')
        with patch.object(Path, 'write_text', interrupted), self.assertRaises(OSError):
            replay_viewer.export_saved_report(self.source, failed)
        self.assertFalse(failed.exists())
        self.assertEqual(list(self.root.glob('.saved-replay-*')), [])

    def test_ambiguous_or_nonfinite_json_and_oversize_input_are_rejected(self):
        for raw in (b'{"schema_version":1,"schema_version":1}', b'{"value":NaN}', b'[]'):
            self.source.write_bytes(raw)
            with self.assertRaises(ValueError):
                replay_viewer.export_saved_report(self.source, self.output)
        self.save(self.report)
        with patch.object(replay_viewer, 'MAX_SAVED_REPORT_BYTES', 32), self.assertRaises(ValueError):
            replay_viewer.export_saved_report(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_cli_exports_a_saved_report_and_reports_bad_input_without_traceback(self):
        self.save(self.report)
        result = subprocess.run([sys.executable, '-m', 'research.replay_viewer', '--input', str(self.source),
                                 '--output', str(self.output)], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('recorded results only', result.stdout)
        self.source.write_text('{bad json', encoding='utf-8')
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            code = replay_viewer.main(['--input', str(self.source), '--output', str(self.root / 'bad.html')])
        self.assertEqual(code, 2)
        self.assertNotIn('Traceback', errors.getvalue())
        self.assertFalse((self.root / 'bad.html').exists())


if __name__ == '__main__':
    unittest.main()
