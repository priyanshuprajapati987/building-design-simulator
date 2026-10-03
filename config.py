"""Central configuration: paths, versions, code-data loader."""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Phase-2 OpenSees FEA verification (set FEA_ENABLED=0 to skip)
FEA_ENABLED = os.environ.get("FEA_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", "off")
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"
CODES_FILE = DATA_DIR / "codes.json"

APP_NAME = "Building Design Simulator"
VERSION = "0.1.0"
DISCLAIMER = (
    "PRELIMINARY DESIGN ONLY - code-based approximations for early-stage "
    "comparison. Not for construction. Final design must be checked and "
    "signed by a licensed structural engineer."
)


@lru_cache(maxsize=1)
def get_codes() -> dict:
    with open(CODES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def city_info(city: str) -> dict | None:
    """Lookup city record (case-insensitive, unknown -> None)."""
    return get_codes()["cities"].get(city.strip().lower())


def city_rate_inr_sqft(city: str) -> float:
    rates = get_codes()["cost_inr_per_sqft"]
    return float(rates.get(city.strip().title(), rates["default"]))
