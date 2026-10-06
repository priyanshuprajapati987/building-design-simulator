"""Phase-2 preliminary energy model (ECBC-aligned, degree-day method).

Screening-level annual energy estimate used to COMPARE designs - not an
hourly EnergyPlus simulation. Honest scope:

* envelope heat flow  = UA x annual degree-days (CDD24 cooling / HDD16 heating,
  rounded climate normals per city - IS 876-style timing, single temperature)
* solar gain          = glazing area x SHGC x annual plane-of-array irradiance
* lighting / plug     = ECBC-style W/m2 x operating hours by building type
* cooling / heating   = thermal load / seasonal efficiency (EER / COP)
* optional EnergyPlus : ``find_energyplus()`` detects an installed binary so
  the engine can be swapped in a later run (never installed silently)

EUI (kWh/m2-yr) is per-total-covered-area and excludes DHW, lifts and
specialty equipment - expect it to under-read versus a certified model.
"""
from __future__ import annotations

import contextlib
import os
import shutil
from functools import lru_cache
from pathlib import Path

import config as cfg

from .models import Design, Requirements

ENGINE = "preliminary-degree-day-v1"


@lru_cache(maxsize=4)
def find_energyplus(explicit_dir: str = "") -> str | None:
    """Locate an EnergyPlus installation (env ENERGYPLUS_DIR, PATH, Program
    Files). Cached - tests may call find_energyplus.cache_clear()."""
    candidates: list[Path] = []
    if explicit_dir:
        candidates.append(Path(explicit_dir))
    env = os.environ.get("ENERGYPLUS_DIR", "").strip()
    if env:
        candidates.append(Path(env))
    candidates.append(cfg.ROOT / ".energyplus")      # repo-local install (Phase-2d)
    with contextlib.suppress(OSError):
        for base in (r"C:\Program Files", r"C:\Program Files (x86)",
                     "/usr/local", "/usr"):
            root = Path(base)
            if root.is_dir():
                candidates.extend(sorted(root.glob("EnergyPlus-*"),
                                         reverse=True))
    exe_name = "energyplus.exe" if os.name == "nt" else "energyplus"
    for c in candidates:
        exe = c / exe_name
        if exe.is_file():
            return str(exe)
    on_path = shutil.which("energyplus")
    return on_path or None


def _climate(energy: dict, city: str) -> list[float]:
    clim = energy["climate_by_city"].get(city.strip().lower())
    if clim is None:
        # try alias spellings the parser produces
        from config import _RATE_ALIASES
        clim = energy["climate_by_city"].get(
            _RATE_ALIASES.get(city.strip().lower(), ""))
    return list(clim or energy["climate_default"])


def _tariff(energy: dict, building_type: str) -> float:
    t = energy.get("tariff_inr_kwh", {})
    return float(t.get(building_type, t.get("_default", 8.0)))


def model(req: Requirements, d: Design) -> dict:
    """Annual energy estimate for one design (kWh/yr, EUI, cost, breakdown)."""
    energy = cfg.get_codes()["energy"]
    env = energy["envelope"]
    ld = energy["loads_by_type"].get(
        req.building_type,
        energy["loads_by_type"]["residential"])
    design_db, cdd24, hdd16 = _climate(energy, req.city)

    plate = d.plate_sqm
    area = plate * d.floors
    perim = 2.0 * (d.len_x_m + d.len_y_m)
    wall = perim * d.height_m
    roof = plate
    win = wall * env["wwr"]

    ua = env["u_wall_wm2k"] * wall + env["u_roof_wm2k"] * roof \
        + env["u_win_wm2k"] * win                     # W/K

    lighting = ld["lpd_wm2"] * area * ld["hours"] / 1000.0
    plug = ld["plug_wm2"] * area * ld["hours"] / 1000.0

    cool_env = ua * cdd24 * 24.0 / 1000.0 / env["eer"]
    cool_solar = win * env["shgc"] * env["solar_kwh_m2yr"] / env["eer"]
    cool_int = ((ld["lpd_wm2"] + ld["plug_wm2"]) * area * ld["hours"]
                * env["internal_cool_frac"] / 1000.0 / env["eer"])
    cooling = cool_env + cool_solar + cool_int
    fans = env["fan_frac"] * cooling
    heating = ua * hdd16 * 24.0 / 1000.0 / env["cop_heat"]

    total = lighting + plug + cooling + fans + heating
    eui = total / area if area else 0.0
    tariff = _tariff(energy, req.building_type)
    return {
        "engine": ENGINE,
        "energyplus_binary": find_energyplus(),
        "eui_kwh_m2yr": round(eui, 1),
        "annual_kwh": round(total),
        "annual_cost_inr": round(total * tariff),
        "tariff_inr_kwh": tariff,
        "area_sqm": round(area, 1),
        "breakdown_kwh": {
            "lighting": round(lighting),
            "plug_appliances": round(plug),
            "cooling": round(cooling),
            "fans_pumps": round(fans),
            "heating": round(heating),
        },
        "climate": {
            "city": req.city,
            "design_db_c": design_db,
            "cdd24": cdd24,
            "hdd16": hdd16,
        },
        "assumptions": {
            "u_wall_wm2k": env["u_wall_wm2k"],
            "u_roof_wm2k": env["u_roof_wm2k"],
            "u_win_wm2k": env["u_win_wm2k"],
            "wwr": env["wwr"],
            "shgc": env["shgc"],
            "eer": env["eer"],
            "lpd_wm2": ld["lpd_wm2"],
            "plug_wm2": ld["plug_wm2"],
            "hours": ld["hours"],
        },
        "note": ("preliminary degree-day estimate (envelope + solar + "
                 "lighting + plug loads); excludes DHW, lifts and specialty "
                 "equipment - not a certified energy model"),
    }
