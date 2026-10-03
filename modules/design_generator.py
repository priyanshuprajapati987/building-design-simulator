"""Parametric generation of 3 design alternatives (hybrid rule-based + layout logic)."""
from __future__ import annotations

import math

import config as cfg

from .models import SQFT_TO_SQM, Design, Requirements

# coverage of plot by floor plate (typical urban norms)
_COVERAGE = 0.55
_COVERAGE_MAX = 0.65


def _target_plate_sqft(req: Requirements) -> float:
    codes = cfg.get_codes()
    if req.building_type == "residential" and req.units_per_floor:
        unit_area = codes["unit_areas_sqft"]["residential_default"]
        return req.units_per_floor * unit_area * 0.85   # plate incl. circulation
    land = req.land_area_sqft or 5000.0
    if req.building_type in ("warehouse", "industrial"):
        cov = _COVERAGE_MAX
    else:
        cov = _COVERAGE
    return land * cov


def _slab_t(panel_m: float) -> int:
    if panel_m <= 3.9:
        return 125
    if panel_m <= 4.75:
        return 150
    if panel_m <= 5.75:
        return 175
    return 200


def _beam_d(span_m: float) -> int:
    return max(350, min(750, round(span_m * 1000 / 15.0 / 50.0) * 50))


_SPEC = [
    dict(id="A", name="Compact RC Frame",
         system="rc_frame", aspect=1.0, bay=6.0, beam_w=250,
         secondary=False, core=False, note="Square plan, 6 m bays, moment "
         "frame only - lowest concrete volume, most walls (least open space)."),
    dict(id="B", name="Core Shear-Wall Dual System",
         system="rc_dual", aspect=1.4, bay=7.5, beam_w=300,
         secondary=True, core=True, note="Central structural core + frame, "
         "7.5 m bays with secondary beams - stiffest lateral system, best "
         "drift control, moderate extra concrete."),
    dict(id="C", name="Open-Plan Ductile Frame",
         system="rc_frame_ductile", aspect=1.2, bay=9.0, beam_w=350,
         secondary=True, core=False, note="9 m open bays (secondary beams "
         "quarter the slab panels), ductile detailing - most flexible layout, "
         "deepest beams, best for office/open plan."),
]


def generate(req: Requirements, seed: int | None = None) -> list[Design]:
    """Returns exactly 3 Design alternatives deterministically (seed optional)."""
    plate_sqft = _target_plate_sqft(req)
    plate_sqm = plate_sqft * SQFT_TO_SQM

    designs: list[Design] = []
    for spec in _SPEC:
        aspect = spec["aspect"]
        # target plan dimensions from plate + aspect (L/W = aspect)
        len_x = math.sqrt(plate_sqm * aspect)
        len_y = len_x / aspect
        bay = spec["bay"]
        bays_x = max(2, round(len_x / bay))
        bays_y = max(2, round(len_y / bay))
        # cap absurd grids (tiny plots / huge unit counts)
        bays_x = min(bays_x, 12)
        bays_y = min(bays_y, 12)

        secondary = bool(spec["secondary"]) and bay > 6.0
        panel = round(bay / 2, 2) if secondary else bay
        slab_t = _slab_t(panel)
        beam_d = _beam_d(bay)
        sec_d = _beam_d(bay / 2) if secondary else 0

        core_lx = core_ly = 0.0
        if spec["core"]:
            core_lx = round(min(0.35 * bays_x * bay, 10.0), 1)
            core_ly = round(min(0.40 * bays_y * bay, 8.0), 1)
            core_lx = max(core_lx, 4.0)
            core_ly = max(core_ly, 3.5)

        d = Design(
            id=spec["id"], name=spec["name"], system=spec["system"],
            bay_x_m=bay, bay_y_m=bay, bays_x=bays_x, bays_y=bays_y,
            floors=req.floors, floor_h_m=req.floor_h_m,
            slab_t_mm=slab_t, secondary=secondary,
            beam_w_mm=spec["beam_w"], beam_d_mm=beam_d,
            sec_beam_d_mm=sec_d,
            core=bool(spec["core"]), core_lx_m=core_lx, core_ly_m=core_ly,
            notes=spec["note"],
        )
        designs.append(d)
    return designs
