"""Offline CLI: run walk-forward evaluation from a JSON plan. Research diagnostics only."""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.lab import Config, read_snapshot_source  # noqa: E402
from research.stress_walk_forward import evaluate_walk_forward  # noqa: E402

REQUIRED = frozenset(('config', 'folds', 'final_holdout_start'))
GRID_KEYS = ('fee_grid', 'delay_grid', 'spread_grid', 'depth_grid')


def _reject_constant(name):
    raise ValueError(f'non-finite constant {name} not allowed in plan')


def load_plan(path):
    with open(path, encoding='utf-8') as fh:
        plan = json.load(fh, parse_constant=_reject_constant)
    if not isinstance(plan, dict):
        raise ValueError('plan must be a JSON object')
    unknown = set(plan) - REQUIRED - set(GRID_KEYS)
    missing = REQUIRED - set(plan)
    if unknown or missing:
        raise ValueError(f'plan keys: unknown {sorted(unknown)}, missing {sorted(missing)}')
    if not isinstance(plan['config'], dict):
        raise ValueError('config must be a JSON object')
    for key in GRID_KEYS:
        if key in plan and not isinstance(plan[key], list):
            raise ValueError(f'{key} must be a JSON list')
    return plan


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        plan = load_plan(args.plan)
        config = Config(**plan['config'])
        rows, sha = read_snapshot_source(args.input)
        grids = {k: tuple(plan[k]) for k in GRID_KEYS if k in plan}
        result = evaluate_walk_forward(rows, config, plan['folds'], plan['final_holdout_start'], **grids)
        result['input_sha256'] = sha
        text = json.dumps(result, sort_keys=True, indent=2, allow_nan=False) + '\n'
        # A partial write must never appear at the final report path. Link a
        # closed sibling file without replacing any competing destination entry.
        with tempfile.TemporaryDirectory(prefix='.stress-report-', dir=args.output.parent) as temp:
            staged = Path(temp) / 'report.json'
            with open(staged, 'x', encoding='utf-8') as fh:
                fh.write(text)
            os.link(staged, args.output)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
