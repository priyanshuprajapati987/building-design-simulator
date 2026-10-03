"""IS 456 spread-footing design (screening-level): square isolated footings
at corner / edge / interior column positions.

Sizing from allowable bearing pressure (with footing self-weight iteration),
depth auto-derived from one-way shear and punching shear, then a moment check
at the column face. Every result is a utilisation ratio (<= 1.00 passes).

This is a *preliminary* code calculation for early comparison - not a
substitute for a licensed geotechnical report and detailed foundation design.
"""
from __future__ import annotations

import math
from typing import Any

import config as cfg

from .models import Design

GAMMA_C = 25.0          # kN/m3 reinforced concrete unit weight

POSITIONS = ("corner", "edge", "interior")


def trib_ratios(d: Design) -> dict[str, float]:
    """Tributary-area / plate ratios for the three column position classes."""
    plate = max(d.plate_sqm, 1e-6)
    a = d.bay_x_m * d.bay_y_m
    return {"corner": a / 4.0 / plate, "edge": a / 2.0 / plate,
            "interior": a / plate}


def _grid(d: Design) -> list[tuple[int, int]]:
    """Column grid indices (i along X, j along Y)."""
    return [(i, j) for i in range(d.bays_x + 1) for j in range(d.bays_y + 1)]


def _classify(d: Design, i: int, j: int) -> str:
    on_x = i in (0, d.bays_x)
    on_y = j in (0, d.bays_y)
    if on_x and on_y:
        return "corner"
    if on_x or on_y:
        return "edge"
    return "interior"


def existing_positions(d: Design) -> list[str]:
    """Which of corner / edge / interior exist as real columns on this grid."""
    have = dict.fromkeys(POSITIONS, False)
    for i, j in _grid(d):
        have[_classify(d, i, j)] = True
    return [k for k in POSITIONS if have[k]]


def axial_coords(d: Design) -> tuple[list[float], list[float]]:
    """Grid coordinates in metres about the plan centroid."""
    xs = [i * d.bay_x_m - d.len_x_m / 2.0 for i in range(d.bays_x + 1)]
    ys = [j * d.bay_y_m - d.len_y_m / 2.0 for j in range(d.bays_y + 1)]
    return xs, ys


def seismic_axial_factors(d: Design) -> dict[str, float]:
    """kN of column axial per kNm of base overturning moment, per position
    class. Rigid-couple distribution over the column grid: dP_i = M * c_i /
    sum(c_j^2) (both directions, worst value taken per class)."""
    xs, ys = axial_coords(d)
    nx, ny = len(xs), len(ys)
    sum_x2 = ny * sum(x * x for x in xs)
    sum_y2 = nx * sum(y * y for y in ys)
    out = dict.fromkeys(POSITIONS, 0.0)
    for i, j in _grid(d):
        key = _classify(d, i, j)
        dp = 0.0
        if sum_x2 > 0:
            dp = max(dp, abs(xs[i]) / sum_x2)
        if sum_y2 > 0:
            dp = max(dp, abs(ys[j]) / sum_y2)
        out[key] = max(out[key], dp)
    return out


def design_footing(p_service_kn: float, p_u_kn: float, sbc: float, fck: float,
                   col_mm: tuple[int, int] = (300, 450), bump: int = 0,
                   t_mm: int = 0) -> dict[str, Any]:
    """Design one square isolated footing.

    B is sized from the bearing pressure (self-weight iteration, rounded up to
    the codes size step, + bump_step per optimizer bump). Depth is auto-derived
    from one-way / punching shear when t_mm == 0 (capped at codes max depth);
    t_mm > 0 forces that thickness (optimizer-controlled).
    """
    fd = cfg.get_codes()["foundation"]
    step = int(fd["size_step_mm"])
    depth_step = int(fd["depth_step_mm"])
    t_min = int(fd["min_depth_mm"])
    t_max = int(fd["max_depth_mm"])
    cover = int(fd["footing_cover_mm"])
    sw_f = float(fd["self_weight_factor"])
    bump = max(0, min(int(bump), int(fd["max_bumps"])))

    cx, cy = max(col_mm), min(col_mm)

    t = max(int(t_mm), t_min)
    t_eff = max(t - cover - 6, 1)              # cover 50 + bar radius ~6 -> d
    b = 1000
    q_srv = q_u = 0.0
    bearing_util = shear_util = punch_util = 0.0
    overhang_x = overhang_y = 0.0

    for _ in range(12):
        # bearing-driven size (footing self-weight reduces effective SBC)
        eff_sbc = sbc - GAMMA_C * sw_f * (t / 1000.0)
        eff_sbc = max(eff_sbc, 0.2 * sbc)
        b = math.ceil(math.sqrt(max(p_service_kn, 1.0) / eff_sbc)
                      * 1000.0 / step) * step
        b += bump * int(fd["bump_step_mm"])
        b = max(b, 600)

        w_self = GAMMA_C * (b / 1000.0) ** 2 * (t / 1000.0)
        q_srv = (p_service_kn + w_self * sw_f) / ((b / 1000.0) ** 2)
        bearing_util = q_srv / sbc if sbc else 9.9

        q_u = p_u_kn / ((b / 1000.0) ** 2)     # kN/m2 factored soil pressure

        # one-way shear at d from the column face (worst of the two axes)
        d_mm = t_eff
        overhang_x = (b - cx) / 2.0
        overhang_y = (b - cy) / 2.0
        v_x = q_u * max(overhang_x - d_mm, 0.0) / 1000.0 * (b / 1000.0)
        v_y = q_u * max(overhang_y - d_mm, 0.0) / 1000.0 * (b / 1000.0)
        v_max = max(v_x, v_y)
        tau = v_max * 1000.0 / (b * d_mm) if b * d_mm else 9.9     # N/mm2
        tau_cap = 0.16 * math.sqrt(fck)
        shear_util = tau / tau_cap if tau_cap else 9.9

        # punching shear at d/2 perimeter: tau = V / (u * d)
        px, py = cx + d_mm, cy + d_mm
        v_punch = q_u * max((b / 1000.0) ** 2 - (px * py) / 1e6, 0.0)
        u_mm = 2.0 * (px + py)
        tau_p = v_punch * 1000.0 / (u_mm * d_mm) if u_mm * d_mm else 9.9
        tau_p_cap = 0.33 * math.sqrt(fck)
        punch_util = tau_p / tau_p_cap if tau_p_cap else 9.9

        shear_ok = shear_util <= 1.0 and punch_util <= 1.0
        if t_mm or shear_ok or t >= t_max:
            break
        t += depth_step
        t_eff = max(t - cover - 6, 1)

    # moment at the column face (worst direction), per metre width
    a_max = max(overhang_x, overhang_y) / 1000.0
    m_u = q_u * a_max ** 2 / 2.0                               # kNm/m
    mu_cap = 0.136 * fck * 1000.0 * t_eff ** 2 / 1e6           # kNm/m
    moment_util = m_u / mu_cap if mu_cap else 9.9

    return {
        "P_service_kN": round(p_service_kn, 0),
        "P_u_kN": round(p_u_kn, 0),
        "size_mm": f"{b} x {b}",
        "B_mm": b,
        "thickness_mm": t,
        "q_service_knm2": round(q_srv, 1),
        "sbc_knm2": sbc,
        "bearing_util": round(bearing_util, 2),
        "shear_util": round(shear_util, 2),
        "punching_util": round(punch_util, 2),
        "moment_util": round(moment_util, 2),
        "util": round(max(shear_util, punch_util, moment_util), 2),
    }


def worst_utils(footings: list[dict[str, Any]]) -> tuple[float, float, float]:
    """(bearing, shear, moment) worst utilisation across footing rows."""
    if not footings:
        return 0.0, 0.0, 0.0
    return (max(f["bearing_util"] for f in footings),
            max(max(f["shear_util"], f["punching_util"]) for f in footings),
            max(f["moment_util"] for f in footings))
