"""Reproducible SYNTHETIC demo from a clean checkout; see research/demo.py. Python only, no cmake."""
import sys
from pathlib import Path

# Keep every write inside the output directory: no __pycache__ in the checkout.
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.demo import main  # noqa: E402

if __name__ == '__main__':
    sys.exit(main())
