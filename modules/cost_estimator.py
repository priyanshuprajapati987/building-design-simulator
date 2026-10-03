"""Cost estimation: city rates + structural system / zone / soil adjustments."""
from __future__ import annotations

import config as cfg

from .models import Design, Requirements

_SYSTEM_FACTOR = {
    "rc_frame": 1.00,
    "rc_frame_ductile": 1.03,
    "rc_dual": 1.05,
}
_STRUCTURE_SHARE = {
    "rc_frame": 0.35,
    "rc_frame_ductile": 0.36,
    "rc_dual": 0.37,
}


def estimate(d: Design, req: Requirements) -> dict:
    codes = cfg.get_codes()
    rate = cfg.city_rate_inr_sqft(req.city) if cfg.city_info(req.city) \
        else codes["cost_inr_per_sqft"]["default"]

    sys_f = _SYSTEM_FACTOR.get(d.system, 1.0)
    zone_f = 1.04 if req.seismic_zone in ("IV", "V") else 1.00
    soil_f = 1.03 if req.soil_type == "III" else 1.00
    parking_f = 1.02 if "parking" in req.special else 1.00
    green_f = 1.03 if "green_roof" in req.special else 1.00

    builtup = d.gross_sqft
    eff_rate = rate * sys_f * zone_f * soil_f * parking_f * green_f
    total = builtup * eff_rate

    share = _STRUCTURE_SHARE.get(d.system, 0.35)
    breakdown = {
        "structure": round(total * share, 0),
        "finishes": round(total * 0.30, 0),
        "mep_services": round(total * 0.20, 0),
        "external_and_misc": round(total * (1.0 - share - 0.30 - 0.20), 0),
    }

    within = None
    budget_used = None
    if req.budget_crores:
        budget_used = req.budget_crores * 1e7
        within = total <= budget_used

    return {
        "city": req.city,
        "base_rate_inr_sqft": rate,
        "factors": {"system": sys_f, "zone": zone_f, "soil": soil_f,
                    "parking": parking_f, "green": green_f},
        "effective_rate_inr_sqft": round(eff_rate, 1),
        "builtup_sqft": round(builtup, 1),
        "total_inr": round(total, 0),
        "total_crores": round(total / 1e7, 3),
        "per_sqft_inr": round(eff_rate, 1),
        "breakdown_inr": breakdown,
        "budget_inr": budget_used,
        "within_budget": within,
    }
