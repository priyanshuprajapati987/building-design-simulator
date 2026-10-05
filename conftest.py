"""Root conftest: make the project root importable for pytest (modules/, config)."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Hermetic suite: Phase-3A genetic search stays ON in production
# (config.GENETIC_ENABLED default) but is disabled here - the rest of the
# suite would otherwise pay the GA + FEA re-verification cost on every
# pipeline.run (tall buildings: +145s per heavy test). The search itself is
# covered explicitly by tests/test_genetic.py (genetic=True / evolve()).
os.environ["GENETIC_ENABLED"] = "0"
