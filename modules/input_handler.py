"""Input handling: natural-language text and structured dict -> requirement fields.

Voice input in a later phase feeds transcripts into parse_text(), so the text
parser is the single front door for both typed and spoken requirements.
"""
from __future__ import annotations

import re
from typing import Any

from .models import Requirements

# ---------------------------------------------------------------------------
# keyword tables
# ---------------------------------------------------------------------------

_TYPE_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    # priority order: first match wins
    ("hospital", ("hospital", "clinic", "medical", "healthcare", "nursing")),
    ("school", ("school", "college", "university", "academy", "campus", "institute")),
    ("hotel", ("hotel", "lodge", "guest house", "guesthouse", "resort")),
    ("warehouse", ("warehouse", "godown", "storage", "depot")),
    ("industrial", ("factory", "industrial", "manufacturing", "plant", "workshop")),
    ("retail", ("shop", "retail", "showroom", "store", "boutique", "mall")),
    ("office", ("office", "workplace", "co-working", "coworking", "it park", "itpark")),
    ("residential", ("residential", "apartment", "apartments", "flat", "flats",
                     "bhk", "housing", "villa", "home", "dwelling", "tower residential")),
]

_SPECIAL_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("parking", ("parking", "stilt", "garage", "car park")),
    ("green_roof", ("green roof", "roof garden", "terrace garden")),
    ("solar_panels", ("solar panel", "solar", "photovoltaic", "pv panel")),
    ("rainwater_harvesting", ("rainwater", "rain water", "water harvesting")),
    ("swimming_pool", ("swimming pool", "pool")),
    ("earthquake_resistant", ("earthquake", "seismic", "quake")),
    ("ev_charging", ("ev charging", "ev charger", "charging point")),
]

_SOIL_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("III", ("soft clay", "soft soil", "marshy", "liquefiable", "black cotton")),
    ("I", ("hard rock", "rocky", "bedrock", "rock outcrop")),
    ("II", ("stiff clay", "dense sand", "medium soil", "sandy soil", "stiff soil")),
]

_TERRAIN_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    (4, ("city centre", "city center", "downtown", "dense urban", "high rise district")),
    (1, ("sea face", "beachfront", "coastal", "sea coast", "waterfront", "open sea")),
    (2, ("rural", "village", "open field", "farmland", "countryside")),
    (3, ("suburb", "residential area", "town")),
]

_ZONE_WORDS = {"2": "II", "3": "III", "4": "IV", "5": "V",
               "ii": "II", "iii": "III", "iv": "IV", "v": "V"}

_UNIT_AREA_KEYS = ("1rk", "1bhk", "2bhk", "3bhk")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _find_keyword(text: str, table: list[tuple[Any, tuple[str, ...]]]) -> Any | None:
    for value, words in table:
        if any(w in text for w in words):
            return value
    return None


def _to_number(s: str) -> float:
    return float(s.replace(",", "").replace("_", ""))


# ---------------------------------------------------------------------------
# text parser
# ---------------------------------------------------------------------------

def parse_text(text: str) -> dict[str, Any]:
    """Best-effort extraction of building requirements from free text (or a
    voice transcript). Missing fields stay None and get defaults later."""
    out: dict[str, Any] = {"raw_text": text}
    t = " " + re.sub(r"\s+", " ", text.lower().strip()) + " "

    # building type
    bt = _find_keyword(t, _TYPE_KEYWORDS)
    if bt:
        out["building_type"] = bt

    # floors: "10 floor", "10-storey", "G+5", "ground plus 5"
    # lookbehind + up-to-4-digits so "100 floor" is 100 (not "00" -> 0)
    m = re.search(r"g\s*\+\s*(\d{1,3})", t)
    if m:
        out["floors"] = int(m.group(1)) + 1
    else:
        m = re.search(r"(?<!\d)(\d{1,4})\s*[-\s]?\s*"
                      r"(?:floor|storey|storeys|story|stories|storied)", t)
        if m:
            out["floors"] = int(m.group(1))

    # city (longest key match wins to prefer "bengaluru" style aliases)
    import config as _cfg  # local import to avoid cycles
    cities = _cfg.get_codes()["cities"]
    best_len, best_key = 0, None
    for key in cities:
        if key.startswith("_"):
            continue
        if re.search(rf"\b{re.escape(key)}\b", t) and len(key) > best_len:
            best_len, best_key = len(key), key
    if best_key:
        out["city"] = best_key.title()

    # budget: crore / lakh / rupees
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:crore|cr\b)", t)
    if m:
        out["budget_crores"] = _to_number(m.group(1))
    else:
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:lakh|lac\b|lakhs)", t)
        if m:
            out["budget_crores"] = _to_number(m.group(1)) / 100.0
        else:
            m = re.search(r"(?:budget|cost)[^0-9]{0,20}(?:₹|rs\.?|inr)?\s*(\d{7,10})", t)
            if m:
                out["budget_crores"] = _to_number(m.group(1)) / 1e7

    # units per floor (plural-only + lookbehind: "G+50 apartment" must NOT
    # set units=50, and "1000 units" must be 1000 not "000" -> 0)
    m = re.search(r"(?<!\d)(\d{1,4})\s*(?:units|flats|apartments|homes)\b"
                  r"(?:\s*(?:per|each|a|every|on each)\s*floor)?", t)
    if (m and ("floor" in t or "storey" in t or "unit" in t or "flat" in t)) or (m and out.get("building_type") == "residential"):
        out["units_per_floor"] = int(m.group(1))

    # land area
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:sq\.?\s*ft|sqft|square\s*feet)", t)
    if m:
        out["land_area_sqft"] = _to_number(m.group(1))
    else:
        m = re.search(r"(\d+(?:\.\d+)?)\s*acres?\b", t)
        if m:
            out["land_area_sqft"] = _to_number(m.group(1)) * 43560.0

    # special requirements
    special = _find_all_special(t)
    if special:
        out["special"] = special

    # seismic zone override: "zone iv", "zone 4"
    m = re.search(r"zone\s*(iv|v|ii|iii|2|3|4|5)\b", t)
    if m:
        out["seismic_zone"] = _ZONE_WORDS[m.group(1)]

    # soil
    soil = _find_keyword(t, _SOIL_KEYWORDS)
    if soil:
        out["soil_type"] = soil

    # terrain
    terr = _find_keyword(t, _TERRAIN_KEYWORDS)
    if terr:
        out["terrain_cat"] = terr

    # floor height "3.5m floor height" / "storey height 3.6 m"
    m = re.search(r"(?:floor|storey|story)\s*height[^0-9]{0,10}(\d(?:\.\d{1,2})?)\s*m", t)
    if not m:
        m = re.search(r"(\d(?:\.\d{1,2})?)\s*m\s*(?:floor|storey|story)\s*height", t)
    if m:
        out["floor_h_m"] = float(m.group(1))

    return out


def _find_all_special(t: str) -> list[str]:
    found: list[str] = []
    for value, words in _SPECIAL_KEYWORDS:
        if any(w in t for w in words):
            found.append(value)
    return found


# ---------------------------------------------------------------------------
# dict parser / validator
# ---------------------------------------------------------------------------

_ALLOWED_KEYS = {
    "building_type", "city", "floors", "land_area_sqft", "units_per_floor",
    "budget_crores", "special", "seismic_zone", "soil_type", "vb",
    "terrain_cat", "floor_h_m", "raw_text",
}

_VALID_TYPES = {"residential", "office", "retail", "commercial", "school",
                "hospital", "hotel", "warehouse", "industrial", "parking"}

_VALID_ZONES = {"II", "III", "IV", "V"}


def parse_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Validate/coerce a structured requirements dict. Unknown keys ignored."""
    out: dict[str, Any] = {}
    for key, val in data.items():
        if key not in _ALLOWED_KEYS or val is None:
            continue
        if key == "building_type":
            v = str(val).lower().strip()
            if v not in _VALID_TYPES:
                raise ValueError(f"unknown building_type '{val}' (valid: {sorted(_VALID_TYPES)})")
            out[key] = v
        elif key == "floors":
            out[key] = max(1, int(val))
        elif key in ("land_area_sqft", "budget_crores", "floor_h_m", "vb"):
            out[key] = float(val)
        elif key == "units_per_floor":
            out[key] = max(1, int(val))
        elif key == "terrain_cat":
            out[key] = min(4, max(1, int(val)))
        elif key == "seismic_zone":
            v = str(val).upper().strip()
            if v not in _VALID_ZONES:
                raise ValueError(f"unknown seismic_zone '{val}' (valid: {sorted(_VALID_ZONES)})")
            out[key] = v
        elif key == "soil_type":
            v = str(val).upper().strip()
            if v not in ("I", "II", "III"):
                raise ValueError("soil_type must be I, II or III")
            out[key] = v
        elif key == "special":
            if isinstance(val, str):
                val = [val]
            out[key] = [str(s).strip().lower().replace(" ", "_") for s in val]
        else:
            out[key] = val
    return out


def load(text: str | None = None, data: dict | None = None) -> Requirements:
    """Build Requirements from free text and/or a structured dict (dict wins)."""
    fields: dict[str, Any] = {}
    if text:
        fields.update(parse_text(text))
    if data:
        fields.update(parse_dict(data))
    known = {k: v for k, v in fields.items() if k in _ALLOWED_KEYS and v is not None}
    return Requirements(**known)
