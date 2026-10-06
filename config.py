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

# Phase-3A genetic grid search + ML surrogate pre-screening
# (set GENETIC_ENABLED=0 to skip)
GENETIC_ENABLED = os.environ.get("GENETIC_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", "off")
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"
CODES_FILE = DATA_DIR / "codes.json"

# Phase-3C voice input - offline STT via faster-whisper (set VOICE_ENABLED=0
# to skip; text path works without the optional engine installed)
VOICE_ENABLED = os.environ.get("VOICE_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", "off")
# whisper model size (tiny.en is fastest, base.en the default trade-off)
VOICE_MODEL = os.environ.get("VOICE_MODEL", "base.en").strip() or "base.en"
# empty = auto-detect spoken language (transcripts still parse as English)
VOICE_LANGUAGE = os.environ.get("VOICE_LANGUAGE", "").strip() or None
_MODEL_DIR_OVERRIDE = os.environ.get("VOICE_MODEL_DIR", "").strip()
# weights download once here (gitignored under output/ - copy them in by
# hand for fully offline machines)
VOICE_MODEL_DIR = (Path(_MODEL_DIR_OVERRIDE) if _MODEL_DIR_OVERRIDE
                   else OUTPUT_DIR / "voice")

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


# parser .title() spellings that differ from the rate-table keys
_RATE_ALIASES = {"bangalore": "bengaluru", "gurgaon": "gurugram",
                 "bombay": "mumbai", "calcutta": "kolkata",
                 "madras": "chennai"}


@lru_cache(maxsize=1)
def _rates_lower() -> dict[str, float]:
    out: dict[str, float] = {}
    for k, v in get_codes()["cost_inr_per_sqft"].items():
        try:
            out[k.lower()] = float(v)
        except (TypeError, ValueError):
            continue        # explanatory comment entries, not city rates
    return out


def city_rate_inr_sqft(city: str) -> float:
    """Rate lookup, case-insensitive with known aliases (req.city is
    title-cased by the parser, so raw .title() lookup missed aliases like
    "Bangalore" vs the table's "Bengaluru" and fell back to default)."""
    rates = _rates_lower()
    key = city.strip().lower()
    return float(rates.get(_RATE_ALIASES.get(key, key), rates["default"]))
