"""Requirement analysis: fill defaults, resolve city -> zone/wind, sanity warnings."""
from __future__ import annotations

import config as cfg

from .models import Requirements

_VALID_TYPES = {"residential", "office", "retail", "commercial", "school",
                "hospital", "hotel", "warehouse", "industrial", "parking"}

_DEFAULT_LAND_SQFT = {"residential": 5000, "office": 6000, "retail": 4000,
                      "commercial": 6000, "school": 12000, "hospital": 10000,
                      "hotel": 8000, "warehouse": 15000, "industrial": 20000,
                      "parking": 4000}


def analyze(req: Requirements) -> Requirements:
    """Returns a completed Requirements (same object) with defaults + warnings."""
    codes = cfg.get_codes()
    w = req.warnings

    # --- type ---------------------------------------------------------------
    if req.building_type not in _VALID_TYPES:
        w.append(f"unknown building_type '{req.building_type}' -> residential")
        req.building_type = "residential"

    # --- floors -------------------------------------------------------------
    if req.floors < 1:
        w.append(f"invalid floors={req.floors} -> 1 (parser produced a "
                 f"non-positive floor count)")
    req.floors = max(req.floors, 1)
    if req.floors > 60:
        w.append(f"floors={req.floors} clamped to 60 (beyond tool scope)")
        req.floors = 60
    if req.floors > 12:
        w.append(f"{req.floors} floors: tall-building regime - results are "
                 f"screening-level only, detailed dynamic analysis required")
    if req.floors > 45:
        w.append("height > ~45 m: RC frame solution likely uneconomical / "
                 "code-restricted; consider core+outrigger system (Phase 2 tool)")

    # --- city -> seismic zone / wind / terrain ------------------------------
    info = cfg.city_info(req.city)
    if info:
        if not req.seismic_zone:
            req.seismic_zone = info["zone"]
        if req.vb is None:
            req.vb = float(info["vb"])
        if req.terrain_cat is None:
            req.terrain_cat = int(info["terrain"])
    else:
        if not req.seismic_zone:
            req.seismic_zone = "III"
        if req.vb is None:
            req.vb = 44.0
        if req.terrain_cat is None:
            req.terrain_cat = 3
        w.append(f"city '{req.city}' not in database - assumed Zone III, "
                 f"Vb=44 m/s, terrain 3 (override via input)")

    if req.seismic_zone not in ("II", "III", "IV", "V"):
        w.append(f"invalid zone '{req.seismic_zone}' -> III")
        req.seismic_zone = "III"

    # --- basic wind speed (IS 875 Part 3: 33-55 m/s by zone) ----------------
    # an explicit override used to bypass validation entirely (vb=0 made every
    # wind check trivially pass, vb=999 blew up pressures) - range-check it.
    try:
        vb_ok = req.vb is not None and 30.0 <= float(req.vb) <= 60.0
    except (TypeError, ValueError):
        vb_ok = False
    if not vb_ok:
        fallback = float(info["vb"]) if info else 44.0
        w.append(f"basic wind speed {req.vb!r} m/s outside the IS 875 range "
                 f"30-60 -> {fallback:g} m/s")
        req.vb = fallback

    # --- soil ---------------------------------------------------------------
    if req.soil_type not in ("I", "II", "III"):
        w.append(f"invalid soil '{req.soil_type}' -> II")
        req.soil_type = "II"
    if req.soil_type == "III":
        w.append("soft soil (III): higher spectral acceleration + settlement/"
                 "liquefaction check needed at detailed stage")

    # --- terrain ------------------------------------------------------------
    if req.terrain_cat is None or not (1 <= int(req.terrain_cat) <= 4):
        req.terrain_cat = 3
    if req.terrain_cat == 4:
        w.append("terrain 4 (dense urban): confirm upwind fetch before final design")

    # --- floor height -------------------------------------------------------
    if not (2.4 <= req.floor_h_m <= 6.0):
        w.append(f"floor height {req.floor_h_m} m outside 2.4-6.0 m -> 3.2 m")
        req.floor_h_m = 3.2

    # --- land / units -------------------------------------------------------
    if req.land_area_sqft is None and req.units_per_floor is None:
        req.land_area_sqft = float(_DEFAULT_LAND_SQFT[req.building_type])
        w.append(f"no land/unit input: assumed {req.land_area_sqft:.0f} sqft plot")
    if req.land_area_sqft is not None and req.land_area_sqft <= 0:
        req.land_area_sqft = float(_DEFAULT_LAND_SQFT[req.building_type])
        w.append("non-positive land area replaced with default plot")

    # units vs land cross-check (residential)
    if req.building_type == "residential" and req.units_per_floor and req.land_area_sqft:
        unit_area = codes["unit_areas_sqft"]["residential_default"]
        plate_needed = req.units_per_floor * unit_area * 0.85
        coverage = plate_needed / req.land_area_sqft
        if coverage > 0.75:
            w.append(f"units/land inconsistent: implied coverage {coverage:.0%} "
                     f"> 75% - footprint will be capped by plot")

    # --- budget -------------------------------------------------------------
    if req.budget_crores is not None and req.budget_crores <= 0:
        req.budget_crores = None
        w.append("non-positive budget ignored")
    if req.budget_crores is not None and req.budget_crores > 5000:
        w.append(f"budget {req.budget_crores} crore looks like a units error - "
                 f"check (values expected in crore)")

    # --- special requirements normalisation ---------------------------------
    req.special = sorted(set(req.special))
    if "earthquake_resistant" in req.special:
        w.append("'earthquake resistant' noted: all designs are analysed per "
                 "IS 1893 anyway (equivalent static method)")

    # --- importance hint ----------------------------------------------------
    if req.building_type in ("hospital", "school"):
        w.append(f"{req.building_type}: importance factor I=1.5 applied "
                 "(IS 1893 critical/essential category)")

    return req
