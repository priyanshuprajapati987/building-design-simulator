"""Code-based structural screening: gravity loads, IS 1893 seismic (equivalent
static method), IS 875 wind, member sizing and safety checks.

Everything here is a *preliminary* code calculation for early comparison - a
documented simplification of IS 456 / IS 1893 / IS 875, not a substitute for
a licensed engineer's detailed analysis.
"""
from __future__ import annotations

import math

import config as cfg
from .models import Requirements, Design, Check

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
    if T <= codes["plateau_end"]:
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
    for i in range(d.floors):
        ll = ll_per_floor[i]
        roof_bonus = green_roof_dl if i == d.floors - 1 else 0.0
        dl_i = dl + roof_bonus
        floor_gravity.append((dl_i + ll) * plate)
        participation = 1.0 if ll >= 5.0 else 0.5
        floor_w_seismic.append((dl_i + participation * ll) * plate)

    # parapet on roof
    perimeter = 2.0 * (d.len_x_m + d.len_y_m)
    parapet_w = perimeter * 1.0 * 0.2 * 24.0
    W_total = sum(floor_w_seismic) + parapet_w
    G_total = sum(floor_gravity)

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
    for i in range(d.floors - 1, -1, -1):
        shear = sum(F[i:])
        storey_forces.append({"floor": i + 1, "h_m": round((i + 1) * d.floor_h_m, 2),
                              "F_kN": round(F[i], 1), "V_kN": round(shear, 1)})
    storey_forces.reverse()

    # ---- column schedule ---------------------------------------------------
    ladder = sorted(_COLUMN_LADDER, key=lambda s: s[0] * s[1])
    cap_coeff = 0.4 * fck + 0.67 * fy * 0.01        # N/mm2 at 1% steel
    col_rows = []
    max_col_util = 0.0
    governing_col = (230, 300)
    for i in range(d.floors):                        # i = 0 bottom
        n_above = d.floors - i
        P_u = 1.5 * floor_gravity[i] * n_above / n_col * 1.10
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
                         "P_u_kN": round(P_u, 0), "util": round(chosen_util, 2)})

    # ---- beams -------------------------------------------------------------
    trib_main = d.bay_x_m / 2 if d.secondary else d.bay_x_m
    beam_span = (d.bay_x_m + d.bay_y_m) / 2.0
    w_u_main = 1.5 * (dl + ll_per_floor[0]) * trib_main
    M_u_main = w_u_main * beam_span ** 2 / 10.0
    mu_lim_main = 0.136 * fck * d.beam_w_mm * (d.beam_d_mm - 50) ** 2 / 1e6
    main_util = M_u_main / mu_lim_main if mu_lim_main else 9.9

    sec_util = 0.0
    if d.secondary:
        w_u_sec = 1.5 * (dl + ll_per_floor[0]) * (d.bay_x_m / 4.0)
        M_u_sec = w_u_sec * beam_span ** 2 / 10.0
        mu_lim_sec = 0.136 * fck * d.beam_w_mm * (d.sec_beam_d_mm - 50) ** 2 / 1e6
        sec_util = M_u_sec / mu_lim_sec if mu_lim_sec else 9.9
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
        f"main beam {d.beam_w_mm}x{d.beam_d_mm} mm, span {beam_span:.1f} m"
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
    add("Seismic base shear ratio V/W", bs_ratio, "-", "0.005 - 0.15",
        0.005 <= bs_ratio <= 0.15, "sanity band for Indian low/mid-rise")
    add("Fundamental period in spectrum range", T, "s", "0.10 - 4.00",
        0.10 <= T <= 4.0, "equivalent static method valid band")
    add("Building height", height, "m", "<= 60",
        height <= 60, f"{d.floors} floors x {d.floor_h_m} m")
    if "green_roof" in req.special:
        add("Roof green-loading allowance applied", green_roof_dl, "kN/m2", "> 0",
            True, "extra 2.0 kN/m2 dead load on top floor included")

    return {
        "loads": {
            "dead_load_knm2": round(dl, 2),
            "live_load_knm2_ground": ll_per_floor[0],
            "live_load_knm2_typical": ll_per_floor[-1],
            "gravity_per_floor_kN": round(floor_gravity[0], 0),
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
        "checks": [c.to_dict() for c in checks],
        "passed": sum(1 for c in checks if c.passed),
        "total_checks": len(checks),
        "max_utilisation": round(max(max_col_util, beam_util, slab_util), 2),
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
