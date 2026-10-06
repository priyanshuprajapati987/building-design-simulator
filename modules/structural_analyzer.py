"""Code-based structural screening: gravity loads, IS 1893 seismic (equivalent
static method), IS 875 wind, member sizing and safety checks.

Everything here is a *preliminary* code calculation for early comparison - a
documented simplification of IS 456 / IS 1893 / IS 875, not a substitute for
a licensed engineer's detailed analysis.
"""
from __future__ import annotations

import math

import config as cfg

from .foundation import (
    design_footing,
    existing_positions,
    seismic_axial_factors,
    trib_ratios,
    worst_utils,
)
from .models import Check, Design, Requirements

G = 9.81

# column sections ladder (b x d mm), sorted by capacity at runtime
_COLUMN_LADDER = [
    (230, 300), (230, 400), (300, 400), (300, 450), (400, 400),
    (300, 600), (450, 450), (350, 600), (400, 600), (450, 600),
    (600, 600), (600, 750), (750, 750),
]

DRIFT_LIMIT = 0.004          # inter-story drift index (IS 1893 guidance)
OT_LIMIT = 1.5               # overturning stability factor
OW_LD_LIMIT = 42.0           # simplified IS 456 serviceability (Annex-C style)
TW_LD_LIMIT = 48.0


# ---------------------------------------------------------------------------
# IS 1893 helpers
# ---------------------------------------------------------------------------

def spectrum_sa_g(T: float, soil: str) -> float:
    """IS 1893 (Part 1):2016 design response spectrum, 5% damping."""
    codes = cfg.get_codes()["seismic"]["spectrum"][soil]
    if T < 0.10:
        return 1.0 + 15.0 * T
    if codes["plateau_end"] >= T:
        return 2.5
    if T <= 4.0:
        return codes["decay_k"] / T
    return codes["tail"]


def period_T(system: str, height_m: float, plan_min_m: float) -> float:
    """Fundamental period approximations (IS 1893 clause 7.2)."""
    if system == "rc_dual":
        T = 0.09 * height_m / math.sqrt(max(plan_min_m, 1.0))
    else:
        T = 0.075 * (height_m ** 0.75)
    return max(0.10, min(T, 4.0))


def k2_factor(terrain_cat: int, height_m: float) -> float:
    """IS 875 (Part 3):2015 Table 2 - linear interpolation between heights."""
    wind = cfg.get_codes()["wind"]
    hs = wind["k2_heights"]
    vals = wind["k2"][str(terrain_cat)]
    if height_m <= hs[0]:
        return vals[0]
    if height_m >= hs[-1]:
        return vals[-1]
    for i in range(len(hs) - 1):
        if hs[i] <= height_m <= hs[i + 1]:
            f = (height_m - hs[i]) / (hs[i + 1] - hs[i])
            return vals[i] + f * (vals[i + 1] - vals[i])
    return vals[-1]


# ---------------------------------------------------------------------------
# gravity / seismic weight
# ---------------------------------------------------------------------------

def _live_load(req: Requirements, floor_index: int) -> float:
    """kN/m2 for floor_index (0 = ground)."""
    codes = cfg.get_codes()
    if floor_index == 0 and "parking" in req.special:
        return codes["live_loads_knm2"]["parking"]
    if req.building_type == "parking" and floor_index == 0:
        return codes["live_loads_knm2"]["parking"]
    return codes["live_loads_knm2"].get(req.building_type, 2.0)


def _dead_load(req: Requirements, d: Design) -> float:
    """kN/m2 floor dead load incl. slab self, finishes, MEP, partitions, beam self."""
    codes = cfg.get_codes()["dead_load_knm2"]
    slab_self = 25.0 * d.slab_t_mm / 1000.0
    if req.building_type in ("office", "retail", "commercial", "warehouse", "industrial"):
        part = codes["partitions_office"] if req.building_type == "office" else 0.3
    else:
        part = codes["partitions_residential"]
    if "green_roof" in req.special and d.floors >= 1:
        pass  # applied only to top floor in weight calc
    return slab_self + codes["floor_finish"] + codes["mep_ceiling"] + part + codes["beam_column_self"]


# ---------------------------------------------------------------------------
# main analysis
# ---------------------------------------------------------------------------

def analyze(d: Design, req: Requirements) -> dict:
    codes = cfg.get_codes()
    mat = codes["materials"]
    fck = mat["concrete_M25"]["fck"] if d.floors <= 10 else mat["concrete_M30"]["fck"]
    Ec = mat["concrete_M25"]["Ec_knm2"] if d.floors <= 10 else mat["concrete_M30"]["Ec_knm2"]
    fy = mat["steel_Fe500"]["fy"]

    dl = _dead_load(req, d)
    plate = d.plate_sqm
    n_col = d.n_columns

    ll_per_floor = [_live_load(req, i) for i in range(d.floors)]
    green_roof_dl = 2.0 if "green_roof" in req.special else 0.0

    # ---- gravity + seismic weight -----------------------------------------
    floor_w_seismic: list[float] = []
    floor_gravity: list[float] = []
    floor_dead: list[float] = []           # dead-only (for combo LC3)
    for i in range(d.floors):
        ll = ll_per_floor[i]
        roof_bonus = green_roof_dl if i == d.floors - 1 else 0.0
        dl_i = dl + roof_bonus
        floor_gravity.append((dl_i + ll) * plate)
        floor_dead.append(dl_i * plate)
        participation = 1.0 if ll >= 5.0 else 0.5
        floor_w_seismic.append((dl_i + participation * ll) * plate)

    # parapet on roof
    perimeter = 2.0 * (d.len_x_m + d.len_y_m)
    parapet_w = perimeter * 1.0 * 0.2 * 24.0
    W_total = sum(floor_w_seismic) + parapet_w
    G_total = sum(floor_gravity)
    D_total = sum(floor_dead)

    # ---- seismic (IS 1893 equivalent static) -------------------------------
    sec = codes["seismic"]
    Z = sec["zone_factors"][req.seismic_zone]
    I = sec["importance_I"].get(req.building_type, 1.0)
    R = sec["response_reduction_R"][d.system]
    T = period_T(d.system, d.height_m, min(d.len_x_m, d.len_y_m))
    Sa = spectrum_sa_g(T, req.soil_type)
    Ah = (Z / 2.0) * Sa * I / R
    V_base = Ah * W_total

    # triangular distribution F_i = W_i h_i V / sum(W h); h from ground
    moments = []
    for i in range(d.floors):
        h = (i + 1) * d.floor_h_m - d.floor_h_m / 2.0
        moments.append(floor_w_seismic[i] * h)
    sum_m = sum(moments) or 1.0
    storey_forces = []
    F = [moments[i] / sum_m * V_base for i in range(d.floors)]
    # base overturning moment for column/foundation axial from seismic (LC2/LC3)
    M_ot = sum(F[i] * ((i + 1) * d.floor_h_m - d.floor_h_m / 2.0)
               for i in range(d.floors))
    for i in range(d.floors - 1, -1, -1):
        shear = sum(F[i:])
        storey_forces.append({"floor": i + 1, "h_m": round((i + 1) * d.floor_h_m, 2),
                              "F_kN": round(F[i], 1), "V_kN": round(shear, 1)})
    storey_forces.reverse()

    # ---- column schedule (governing load combo per position) ---------------
    # Load combos (IS 456 CL 18.2 / IS 1893 CL 6.4.1): LC1 = 1.5(DL+LL),
    # LC2 = 1.2(DL+LL+EQ), LC3 = 0.9DL + 1.2EQ (min-gravity, edge/corner).
    # Seismic axial per position from a rigid-couple distribution of M_ot;
    # intermediate floors scaled by n_above/floors (screening approximation).
    ladder = sorted(_COLUMN_LADDER, key=lambda s: s[0] * s[1])
    cap_coeff = 0.4 * fck + 0.67 * fy * 0.01        # N/mm2 at 1% steel
    col_rows = []
    max_col_util = 0.0
    governing_col = (230, 300)
    MISC = 1.10                                      # allowance for misc loads
    trib_map = trib_ratios(d)
    pos_list = existing_positions(d)
    axial_f = seismic_axial_factors(d)               # kN axial per kNm M_ot
    for i in range(d.floors):                        # i = 0 bottom
        n_above = d.floors - i
        scale = n_above / d.floors
        grav_above = sum(floor_gravity[i:])
        dead_above = sum(floor_dead[i:])
        P_u = 0.0
        gov_pos, gov_lc = pos_list[0], "LC1"
        for pos in pos_list:
            tr = trib_map[pos]
            pe = axial_f[pos] * M_ot * scale
            for lc, v in (
                ("LC1", 1.5 * grav_above * tr * MISC),
                ("LC2", 1.2 * (grav_above * tr * MISC + pe)),
                ("LC3", 0.9 * dead_above * tr * MISC + 1.2 * pe),
            ):
                if v > P_u:
                    P_u, gov_pos, gov_lc = v, pos, lc
        chosen = ladder[-1]
        chosen_util = 1.0
        start = min(d.column_boost, len(ladder) - 1)
        for sec_idx in range(start, len(ladder)):
            b, dd = ladder[sec_idx]
            Ag = b * dd
            cap = 0.85 * cap_coeff * Ag / 1000.0     # kN (0.85 = moment interaction allowance)
            if cap >= P_u:
                chosen = (b, dd)
                chosen_util = P_u / cap if cap else 9.9
                break
            chosen = (b, dd)
            chosen_util = P_u / cap if cap else 9.9
        if i == 0:
            governing_col = chosen
        max_col_util = max(max_col_util, chosen_util)
        col_rows.append({"floor": i + 1, "section_mm": f"{chosen[0]} x {chosen[1]}",
                         "P_u_kN": round(P_u, 0), "util": round(chosen_util, 2),
                         "position": gov_pos, "combo": gov_lc})

    # ---- spread footings (service bearing + factored structural checks) ----
    fdc = codes["foundation"]
    sbc = float(fdc["sbc_knm2"].get(req.soil_type, 250.0))
    foot_rows = []
    for pos in pos_list:
        tr = trib_map[pos]
        p_srv = G_total * tr
        pe = axial_f[pos] * M_ot
        lc1 = 1.5 * G_total * tr * MISC
        lc2 = 1.2 * (G_total * tr * MISC + pe)
        lc3 = 0.9 * (D_total * tr * MISC) + 1.2 * pe
        p_u = max(lc1, lc2, lc3)
        row = design_footing(p_srv, p_u, sbc, fck, col_mm=governing_col,
                             bump=d.footing_bump, t_mm=d.footing_t_mm)
        row["position"] = pos
        row["trib_ratio"] = round(tr, 4)
        row["P_E_kN"] = round(pe, 0)
        row["governing_combo"] = ("LC1" if p_u == lc1 else
                                  "LC2" if p_u == lc2 else "LC3")
        foot_rows.append(row)
    f_bearing, f_shear, f_moment = worst_utils(foot_rows)
    gov_foot = max(foot_rows, key=lambda r: r["util"]) if foot_rows else None

    # ---- beams -------------------------------------------------------------
    # One-way slab: the load lands on the beams perpendicular to the span
    # direction, and the slab spans the shorter bay.  Check BOTH grid-beam
    # orientations and keep the worst - the loaded orientation always
    # dominates (trib*span^2 = short*long^2 > long*short^2), so the max is
    # exactly the physical load path and stays conservative for rectangular
    # GA grids where bay_x != bay_y.  Main beams span the full bay; secondary
    # beams (mid-bay grid) share the tributary width but span bay/2 between
    # crossings (matches the generator's _beam_d(bay/2) sizing).
    w_u_beam = 1.5 * (dl + ll_per_floor[0])
    mu_lim_main = 0.136 * fck * d.beam_w_mm * (d.beam_d_mm - 50) ** 2 / 1e6
    mu_lim_sec = (0.136 * fck * d.beam_w_mm
                  * (d.sec_beam_d_mm - 50) ** 2 / 1e6)
    hx = d.bay_x_m / 2.0 if d.secondary else d.bay_x_m    # trib of Y-beams
    hy = d.bay_y_m / 2.0 if d.secondary else d.bay_y_m    # trib of X-beams

    def _worst_util(pairs, mu) -> float:
        if not mu:
            return 0.0
        worst = 0.0
        for trib, span in pairs:
            worst = max(worst, w_u_beam * trib * span ** 2 / 10.0 / mu)
        return worst

    main_util = _worst_util(((hx, d.bay_y_m), (hy, d.bay_x_m)), mu_lim_main)
    sec_util = (_worst_util(((d.bay_x_m / 2.0, d.bay_y_m / 2.0),
                             (d.bay_y_m / 2.0, d.bay_x_m / 2.0)), mu_lim_sec)
                if d.secondary else 0.0)
    beam_util = max(main_util, sec_util)

    # ---- slab --------------------------------------------------------------
    slab_self = 25.0 * d.slab_t_mm / 1000.0
    part_s = codes_dead(req)
    w_u_slab = 1.5 * (slab_self + part_s + ll_per_floor[0])
    two_way = True  # panels are square-ish by construction
    M_u_slab = w_u_slab * d.panel_m ** 2 / 10.0
    d_eff = d.slab_t_mm - 25
    mu_lim_slab = 0.136 * fck * 1000.0 * d_eff ** 2 / 1e6
    slab_util = M_u_slab / mu_lim_slab if mu_lim_slab else 9.9
    Ld = d.panel_m * 1000.0 / max(d_eff, 1)
    ld_limit = TW_LD_LIMIT if two_way else OW_LD_LIMIT

    # ---- drift -------------------------------------------------------------
    # orientation: larger column dimension along X (strong for X, weak for Y)
    b_col, d_col = governing_col
    bx, by = max(b_col, d_col), min(b_col, d_col)
    Ix = by * bx ** 3 / 12.0 / 1e12          # m4 about Y (resists X)
    Iy = bx * by ** 3 / 12.0 / 1e12          # m4 about X (resists Y)
    I_wall_x = I_wall_y = 0.0
    if d.core:
        t = d.wall_t_mm / 1000.0
        I_wall_x = 2.0 * t * d.core_lx_m ** 3 / 12.0   # 2 walls in X planes
        I_wall_y = 2.0 * t * d.core_ly_m ** 3 / 12.0
    tot_Ix = n_col * Ix + I_wall_x
    tot_Iy = n_col * Iy + I_wall_y
    h_storey = d.floor_h_m
    Kx = 12.0 * Ec * tot_Ix / h_storey ** 3
    Ky = 12.0 * Ec * tot_Iy / h_storey ** 3

    max_drift = 0.0
    drift_dir = "X"
    storey_detail = []
    for row in storey_forces:
        dx = row["V_kN"] / Kx if Kx else 99.0
        dy = row["V_kN"] / Ky if Ky else 99.0
        ix = dx / h_storey
        iy = dy / h_storey
        gov = max(ix, iy)
        if gov > max_drift:
            max_drift = gov
            drift_dir = "X" if ix >= iy else "Y"
        storey_detail.append({"floor": row["floor"], "V_kN": row["V_kN"],
                              "drift_x": round(ix, 5), "drift_y": round(iy, 5)})

    # ---- wind (IS 875 part 3) ---------------------------------------------
    wind_c = codes["wind"]
    height = d.height_m
    k2 = k2_factor(int(req.terrain_cat), height)
    Vz = req.vb * wind_c["k1"] * k2 * wind_c["k3_flat"] * wind_c["k4_non_cyclone"]
    pz = 0.6 * Vz ** 2 / 1000.0                      # kN/m2
    pd = max(wind_c["Kd"] * wind_c["Ka"] * wind_c["Kc"],
             wind_c["pd_min_fraction_of_pz"]) * pz   # kN/m2

    def _ot(dim_along: float, dim_across: float) -> tuple[float, float, float]:
        """(base_shear, overturning moment, stability factor) for force
        acting along `dim_along` (hits face of width dim_across)."""
        area = dim_across * height
        Vw = pd * area
        Mot = Vw * height / 2.0
        Mres = W_total * dim_along / 2.0
        ratio = Mres / Mot if Mot else 99.0
        return Vw, Mot, ratio

    Vw_x, Mot_x, ot_x = _ot(d.len_x_m, d.len_y_m)   # wind along X
    Vw_y, Mot_y, ot_y = _ot(d.len_y_m, d.len_x_m)   # wind along Y
    ot_min = min(ot_x, ot_y)

    # ---- checks ------------------------------------------------------------
    bs_ratio = V_base / W_total if W_total else 0.0
    checks: list[Check] = []

    def add(name, value, unit, limit_txt, passed, detail=""):
        checks.append(Check(name, round(value, 4), unit, limit_txt, bool(passed), detail))

    add("Column axial capacity (max utilisation)", max_col_util, "-", "<= 1.00",
        max_col_util <= 1.0,
        f"governing section {governing_col[0]}x{governing_col[1]} mm, bottom floor")
    add("Beam moment capacity (max utilisation)", beam_util, "-", "<= 1.00",
        beam_util <= 1.0,
        f"main beam {d.beam_w_mm}x{d.beam_d_mm} mm, "
        f"span {max(d.bay_x_m, d.bay_y_m):.1f} m"
        + (f"; secondary {d.beam_w_mm}x{d.sec_beam_d_mm}" if d.secondary else ""))
    add("Slab moment capacity (utilisation)", slab_util, "-", "<= 1.00",
        slab_util <= 1.0,
        f"{d.slab_t_mm} mm slab, panel {d.panel_m:.1f} m two-way")
    add("Slab serviceability L/d", Ld, "-", f"<= {ld_limit:.0f}",
        Ld <= ld_limit, "simplified IS 456 Annex-C style check")
    add("Inter-story drift index (max)", max_drift, "-", f"<= {DRIFT_LIMIT}",
        max_drift <= DRIFT_LIMIT, f"governing direction {drift_dir}")
    add("Wind overturning stability (min factor)", ot_min, "-", f">= {OT_LIMIT}",
        ot_min >= OT_LIMIT, "weight restoring moment / wind overturning moment")
    # V/W upper bound = max plausible Ah = (Z/2) x 2.5 x I_max(1.5) / R_min(3)
    bs_hi = 0.625 * Z
    add("Seismic base shear ratio V/W", bs_ratio, "-",
        f"0.005 - {bs_hi:.3f}",
        0.005 <= bs_ratio <= bs_hi + 1e-9,
        "sanity band; upper bound = (Z/2)x2.5x1.5/3 for critical buildings")
    add("Fundamental period in spectrum range", T, "s", "0.10 - 4.00",
        0.10 <= T <= 4.0, "equivalent static method valid band")
    add("Building height", height, "m", "<= 60",
        height <= 60,
        f"{d.floors} floors x {d.floor_h_m} m - 60 m is the Phase-1 "
        f"screening-scope boundary (not a code limit); beyond it dynamic "
        f"analysis + system/zone height limits apply")
    if "green_roof" in req.special:
        add("Roof green-loading allowance applied", green_roof_dl, "kN/m2", "> 0",
            True, "extra 2.0 kN/m2 dead load on top floor included")
    if gov_foot:
        add("Foundation bearing pressure (max utilisation)", f_bearing, "-", "<= 1.00",
            f_bearing <= 1.0,
            f"soil class {req.soil_type}, SBC {sbc:.0f} kN/m2, governing "
            f"{gov_foot['position']} {gov_foot['size_mm']} mm")
        add("Foundation shear (max utilisation)", f_shear, "-", "<= 1.00",
            f_shear <= 1.0,
            f"one-way at d + punching at d/2, governing "
            f"{gov_foot['thickness_mm']} mm thick")
        add("Foundation moment (max utilisation)", f_moment, "-", "<= 1.00",
            f_moment <= 1.0,
            f"at column face of {governing_col[0]}x{governing_col[1]} mm column")

    return {
        "loads": {
            "dead_load_knm2": round(dl, 2),
            "live_load_knm2_ground": ll_per_floor[0],
            "live_load_knm2_typical": ll_per_floor[-1],
            "gravity_per_floor_kN": round(floor_gravity[0], 0),
            "floor_gravity_kN": [round(x, 1) for x in floor_gravity],
            "floor_dead_kN": [round(x, 1) for x in floor_dead],
            "floor_seismic_w_kN": [round(x, 1) for x in floor_w_seismic],
            "seismic_weight_per_floor_kN": round(floor_w_seismic[0], 0),
            "W_total_kN": round(W_total, 0),
            "G_total_kN": round(G_total, 0),
            "concrete_grade": f"M{int(fck)}",
        },
        "seismic": {
            "zone": req.seismic_zone, "Z": Z, "I": I, "R": R, "soil": req.soil_type,
            "T_s": round(T, 3), "Sa_g": round(Sa, 3),
            "Ah": round(Ah, 5), "V_base_kN": round(V_base, 0),
            "base_shear_ratio": round(bs_ratio, 4),
            "storey_forces": storey_forces,
        },
        "wind": {
            "vb_mps": req.vb, "terrain": req.terrain_cat, "k2": round(k2, 3),
            "Vz_mps": round(Vz, 1), "pz_knm2": round(pz, 3), "pd_knm2": round(pd, 3),
            "base_shear_x_kN": round(Vw_x, 0), "base_shear_y_kN": round(Vw_y, 0),
            "ot_moment_x_kNm": round(Mot_x, 0), "ot_moment_y_kNm": round(Mot_y, 0),
            "ot_factor_x": round(ot_x, 2), "ot_factor_y": round(ot_y, 2),
        },
        "members": {
            "columns": col_rows,
            "governing_column_mm": f"{governing_col[0]} x {governing_col[1]}",
            "beam_main_mm": f"{d.beam_w_mm} x {d.beam_d_mm}",
            "beam_secondary_mm": f"{d.beam_w_mm} x {d.sec_beam_d_mm}" if d.secondary else "-",
            "beam_util": round(beam_util, 2),
            "slab_mm": d.slab_t_mm, "slab_util": round(slab_util, 2),
        },
        "drift": {"max_index": round(max_drift, 5), "limit": DRIFT_LIMIT,
                  "direction": drift_dir, "storeys": storey_detail},
        "load_combos": {k: v for k, v in codes["load_combos"].items()
                        if not k.startswith("_")},
        "foundation": {
            "soil_type": req.soil_type,
            "sbc_knm2": sbc,
            "column_loads": {
                r["position"]: {"trib_ratio": r["trib_ratio"],
                                "P_service_kN": r["P_service_kN"],
                                "P_u_kN": r["P_u_kN"],
                                "governing_combo": r["governing_combo"],
                                "P_E_kN": r["P_E_kN"]}
                for r in foot_rows},
            "footings": foot_rows,
            "max_thickness_mm": max((r["thickness_mm"] for r in foot_rows),
                                    default=fdc["min_depth_mm"]),
            "note": "screening-level spread footings - geotechnical "
                    "investigation required before construction",
        },
        "checks": [c.to_dict() for c in checks],
        "passed": sum(1 for c in checks if c.passed),
        "total_checks": len(checks),
        "max_utilisation": round(
            max(max_col_util, beam_util, slab_util, f_bearing, f_shear,
                f_moment), 2),
    }


def codes_dead(req: Requirements) -> float:
    """Slab-level superimposed dead load (finishes + MEP + partitions)."""
    dd = cfg.get_codes()["dead_load_knm2"]
    if req.building_type in ("office",):
        part = dd["partitions_office"]
    elif req.building_type in ("retail", "commercial", "warehouse", "industrial"):
        part = 0.3
    else:
        part = dd["partitions_residential"]
    return dd["floor_finish"] + dd["mep_ceiling"] + part
