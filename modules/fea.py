"""OpenSeesPy frame FEA backend (Phase 2).

Builds a full 3D elastic frame model of a generated design and runs three
linear-static load cases:

  G   - service gravity (per-floor loads tributarised to the frame grid)
  Ex  - IS 1893 equivalent-static storey forces F_i along +X
  Ey  - the same forces along +Y

Extracts verification quantities that are cross-checked against the hand
method: base-shear equilibrium, inter-story drift index, beam moment
utilisation and column P-M interaction.

Screening-level assumptions (stated in every report):
  * linear static, small deformations (no P-delta) - Phase 3 scope
  * elastic properties from the same member sections as the hand method
  * limit-state capacity formulas identical to the hand method
    (0.136 fck b d^2 moments; 0.85 x (0.4 fck + 0.0067 fy) column axial)
  * gravity member utilisation = 1.5 x elastic end moments vs capacity
  * core shear walls = vertical centre-line line elements per wall, tied to
    the floor grid with equalDOF in-plane diaphragm constraints
  * secondary beams are not modelled (slab checked separately by hand)

Force extraction verified empirically against probe models
(tests/test_fea.py + the probe harness) on openseespy 3.7.0.1:
  eleForce returns GLOBAL end forces [Fx, Fy, Fz, Mx, My, Mz] x 2
    verticals: slot 2/8  = Fz (axial)
               slot 3/9  = Mx (bends the column in global Y)
               slot 4/10 = My (bends the column in global X)
    X-beams:   strong = My (slot 4/10), plan/weak = Mz (slot 5/11)
    Y-beams:   strong = Mx (slot 3/9), plan/weak = Mz (slot 5/11)

Engine quirks handled here (measured on openseespy 3.7.0.1):
  * ops.system('Plain') misbehaves -> 'Umfpack' + numberer('RCM')
  * ops.remove('load', tag) is NOT supported -> every case gets a fresh
    wiped model build (build cost is milliseconds at screening sizes)
  * ops.nodeReaction() returns zeros under constraints('Transformation')
    -> base shear is summed from base-level element end forces instead
  * ops.eleLoad silently applies nothing -> gravity line loads become
    work-equivalent nodal fixed-end actions on the X-beam grid

The OpenSees interpreter is process-global; all builds run under a module
lock so parallel Streamlit threads can never corrupt each other's models.
"""
from __future__ import annotations

import threading
import time
from typing import Any

import config as cfg

_LOCK = threading.Lock()
_OPS = None
_IMPORT_ERROR: str | None = None
_ATTEMPTED = False

MAX_NODES = 12000

_E_COL = 100_000
_E_BX = 200_000
_E_BY = 300_000
_E_WALL = 600_000


def _load() -> None:
    global _OPS, _IMPORT_ERROR, _ATTEMPTED
    if _ATTEMPTED:
        return
    _ATTEMPTED = True
    try:
        import openseespy.opensees as ops
        _OPS = ops
    except Exception as e:                  # ImportError / DLL failure
        _IMPORT_ERROR = f"{type(e).__name__}: {e}"


def is_available() -> bool:
    _load()
    return _OPS is not None


def unavailable_reason() -> str:
    _load()
    return _IMPORT_ERROR or "not attempted"


def _parse_section(text: str) -> tuple[float, float]:
    parts = text.lower().replace("x", " ").split()
    return float(parts[0]), float(parts[1])


def _col_props(b_mm: float, d_mm: float) -> tuple[float, float, float, float]:
    """(A m2, Iy m4, Iz m4, J m4) with the larger dimension along global X -
    matches the hand drift model's orientation convention."""
    bx, by = max(b_mm, d_mm), min(b_mm, d_mm)
    a = bx * by / 1e6
    iy = by * bx ** 3 / 12.0 / 1e12        # resists global X
    iz = bx * by ** 3 / 12.0 / 1e12        # resists global Y
    j = 0.2 * (iy + iz)
    return a, iy, iz, j


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def _guard(d, analysis) -> str | None:
    """Return the skip reason if FEA cannot run for this design/analysis,
    else None. Keeps verify() within the project's return-count limit."""
    if not cfg.FEA_ENABLED:
        return "FEA disabled (FEA_ENABLED=0)"
    if not is_available():
        return f"OpenSeesPy unavailable: {unavailable_reason()}"
    loads = analysis.get("loads", {})
    gravity = loads.get("floor_gravity_kN")
    storey = analysis.get("seismic", {}).get("storey_forces", [])
    f_lateral = [row["F_kN"] for row in sorted(storey, key=lambda r: r["floor"])]
    cols = analysis.get("members", {}).get("columns", [])
    if not gravity or len(gravity) != d.floors or len(f_lateral) != d.floors:
        return "load record missing from hand analysis"
    if len(cols) != d.floors:
        return "column schedule missing"
    n_grid = (d.bays_x + 1) * (d.bays_y + 1) * (d.floors + 1)
    if n_grid + 4 * (d.floors + 1) > MAX_NODES:
        return f"model too large ({n_grid} grid nodes > {MAX_NODES})"
    return None


def verify(d, req, analysis: dict[str, Any]) -> dict[str, Any]:
    """Run the OpenSees verification for one design.

    Always returns a JSON-safe dict with "ok"; never raises. Returns
    ok=False with a "skipped" reason when FEA is disabled, unavailable or
    the model would be too large, and ok=False with "error" on solve
    failures.
    """
    reason = _guard(d, analysis)
    if reason is not None:
        return {"ok": False, "skipped": reason}

    gravity = analysis["loads"]["floor_gravity_kN"]
    storey = analysis.get("seismic", {}).get("storey_forces", [])
    f_lateral = [row["F_kN"] for row in sorted(storey, key=lambda r: r["floor"])]
    cols = analysis["members"]["columns"]

    t0 = time.perf_counter()
    try:
        return _run(d, analysis, gravity, f_lateral,
                    [_parse_section(r["section_mm"]) for r in cols], t0)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def append_checks(analysis: dict[str, Any]) -> None:
    """Append the four FEA checks from analysis['fea'] (ok=True only) and
    refresh passed/total. Called after every verify()."""
    fea = analysis.get("fea") or {}
    if not fea.get("ok"):
        return
    checks = analysis.setdefault("checks", [])
    eq = fea["equilibrium_err_pct"]
    checks.append({
        "name": "FEA base-shear equilibrium (max error)",
        "value": eq, "unit": "%", "limit": "<= 5",
        "passed": bool(eq <= 5.0),
        "detail": f"element-summed base reaction vs applied V = "
                  f"{fea['base_shear_applied_kN']:.0f} kN "
                  f"({fea['base_shear_fea_kN']['x']:.0f} / "
                  f"{fea['base_shear_fea_kN']['y']:.0f} kN reacted)",
    })
    dr = fea["drift_max_index"]
    checks.append({
        "name": "FEA inter-story drift index (max)",
        "value": dr, "unit": "-", "limit": "<= 0.004",
        "passed": bool(dr <= 0.004),
        "detail": f"governing direction {fea['drift_direction']}, "
                  f"floor {fea['drift_governing_floor']}",
    })
    bu = fea["beam_max_util"]
    checks.append({
        "name": "FEA beam moment (max utilisation)",
        "value": bu, "unit": "-", "limit": "<= 1.00",
        "passed": bool(bu <= 1.0),
        "detail": f"1.5 x elastic end moments vs 0.136 fck b d^2; "
                  f"worst M_u = {fea['beam_Mmax_kNm']:.0f} kNm",
    })
    cu = fea["column_max_interaction"]
    checks.append({
        "name": "FEA column interaction (max utilisation)",
        "value": cu, "unit": "-", "limit": "<= 1.00",
        "passed": bool(cu <= 1.0),
        "detail": f"P/cap + max(My/mu_y, Mz/mu_z); worst {fea['column_worst']}",
    })
    analysis["passed"] = sum(1 for c in checks if c["passed"])
    analysis["total_checks"] = len(checks)


# ---------------------------------------------------------------------------
# model build + solve (one fresh model per load case)
# ---------------------------------------------------------------------------

def _run(d, analysis, gravity, f_lateral, col_sections,
         t0: float) -> dict[str, Any]:
    ops = _OPS
    nx, ny, nz = d.bays_x, d.bays_y, d.floors
    bx, by, h = d.bay_x_m, d.bay_y_m, d.floor_h_m
    codes = cfg.get_codes()
    mat = codes["materials"]
    fck = mat["concrete_M25"]["fck"] if nz <= 10 else mat["concrete_M30"]["fck"]
    fy = mat["steel_Fe500"]["fy"]
    E = (mat["concrete_M25"]["Ec_knm2"] if nz <= 10
         else mat["concrete_M30"]["Ec_knm2"])
    Gmod = E / 2.4                                    # nu = 0.2
    plate = d.plate_sqm

    n_per_floor = (nx + 1) * (ny + 1)

    def nid(ix: int, iy: int, iz: int) -> int:
        return 1 + ix + iy * (nx + 1) + iz * n_per_floor

    wall0 = 1 + n_per_floor * (nz + 1)

    def wid(i: int, iz: int) -> int:
        return wall0 + i + 4 * iz

    share: list[float] = []
    for iy in range(ny + 1):
        for ix in range(nx + 1):
            tx = 1.0 if 0 < ix < nx else 0.5
            ty = 1.0 if 0 < iy < ny else 0.5
            share.append(tx * bx * ty * by / plate)
    assert len(share) == n_per_floor
    # normalise so the per-floor total applied equals the hand model exactly
    # (site plate can be slightly larger than the column grid area)
    _ssum = sum(share)
    if _ssum > 0:
        share = [s / _ssum for s in share]

    def build():
        """Fresh wiped model; returns (col_reg, bx_reg, by_reg, wall_reg)."""
        ops.wipe()
        ops.model("basic", "-ndm", 3, "-ndf", 6)
        ops.constraints("Transformation")     # required by equalDOF
        for iz in range(nz + 1):
            for iy in range(ny + 1):
                for ix in range(nx + 1):
                    ops.node(nid(ix, iy, iz), ix * bx, iy * by, iz * h)
        wall_pts: list[tuple[float, float]] = []
        if d.core:
            cx = nx * bx / 2.0
            cy = ny * by / 2.0
            wall_pts = [(cx, cy - d.core_ly_m / 2), (cx, cy + d.core_ly_m / 2),
                        (cx - d.core_lx_m / 2, cy), (cx + d.core_lx_m / 2, cy)]
            for iz in range(nz + 1):
                for i, (x, y) in enumerate(wall_pts):
                    ops.node(wid(i, iz), x, y, iz * h)

        for iy in range(ny + 1):
            for ix in range(nx + 1):
                ops.fix(nid(ix, iy, 0), 1, 1, 1, 1, 1, 1)
        if d.core:
            for i in range(4):
                ops.fix(wid(i, 0), 1, 1, 1, 1, 1, 1)
            for iz in range(1, nz + 1):
                for i in range(4):
                    wx, wy = ops.nodeCoord(wid(i, iz))[:2]
                    ref = nid(min(max(round(wx / bx), 0), nx),
                              min(max(round(wy / by), 0), ny), iz)
                    ops.equalDOF(ref, wid(i, iz), 1, 2, 6)   # Ux Uy Rz

        # transforms: 1 beams-X, 2 beams-Y, 3 verticals (columns + walls)
        ops.geomTransf("Linear", 1, 0, 0, 1)
        ops.geomTransf("Linear", 2, 0, 0, 1)
        ops.geomTransf("Linear", 3, 1, 0, 0)

        wa, wd = d.beam_w_mm, d.beam_d_mm
        beam_a = wa * wd / 1e6
        beam_iy = wa * wd ** 3 / 12.0 / 1e12      # strong (vertical)
        beam_iz = wd * wa ** 3 / 12.0 / 1e12      # weak (horizontal)
        beam_j = 0.2 * (beam_iy + beam_iz)
        col_reg: list[tuple[int, int]] = []
        bx_reg: list[tuple[int, int]] = []
        by_reg: list[tuple[int, int]] = []
        wall_reg: list[tuple[int, int]] = []
        el = 0
        for iz in range(nz + 1):
            if iz > 0:
                cb, cd = col_sections[iz - 1]
                c_a, c_iy, c_iz, c_j = _col_props(cb, cd)
                for iy in range(ny + 1):
                    for ix in range(nx + 1):
                        tag = _E_COL + el
                        el += 1
                        ops.element("elasticBeamColumn", tag,
                                    nid(ix, iy, iz - 1), nid(ix, iy, iz),
                                    c_a, E, Gmod, c_j, c_iy, c_iz, 3)
                        col_reg.append((tag, iz - 1))
            for iy in range(ny + 1):
                for ix in range(nx):
                    tag = _E_BX + el
                    el += 1
                    ops.element("elasticBeamColumn", tag,
                                nid(ix, iy, iz), nid(ix + 1, iy, iz),
                                beam_a, E, Gmod, beam_j, beam_iy, beam_iz, 1)
                    bx_reg.append((tag, iz))
            for iy in range(ny):
                for ix in range(nx + 1):
                    tag = _E_BY + el
                    el += 1
                    ops.element("elasticBeamColumn", tag,
                                nid(ix, iy, iz), nid(ix, iy + 1, iz),
                                beam_a, E, Gmod, beam_j, beam_iy, beam_iz, 2)
                    by_reg.append((tag, iz))
            if d.core and iz > 0:
                tw = d.wall_t_mm / 1000.0
                jw = max(d.core_lx_m * tw ** 3 / 3.0, 1e-9)
                awx = tw * d.core_lx_m
                iyw = tw * d.core_lx_m ** 3 / 12.0        # resists global X
                for i in (0, 1):
                    tag = _E_WALL + el
                    el += 1
                    ops.element("elasticBeamColumn", tag,
                                wid(i, iz - 1), wid(i, iz),
                                awx, E, Gmod, jw, iyw, 1e-6, 3)
                    wall_reg.append((tag, iz - 1))
                awy = tw * d.core_ly_m
                izw = tw * d.core_ly_m ** 3 / 12.0        # resists global Y
                for i in (2, 3):
                    tag = _E_WALL + el
                    el += 1
                    ops.element("elasticBeamColumn", tag,
                                wid(i, iz - 1), wid(i, iz),
                                awy, E, Gmod, jw, 1e-6, izw, 3)
                    wall_reg.append((tag, iz - 1))

        ops.timeSeries("Linear", 1)
        ops.numberer("RCM")
        ops.system("Umfpack")
        ops.integrator("LoadControl", 1.0)
        ops.algorithm("Linear")
        ops.analysis("Static")
        return col_reg, bx_reg, by_reg, wall_reg

    def apply_case(per_floor, horizontal: str | None) -> None:
        """Load pattern 1 on floor levels 1..nz (level 0 is ground).

        Gravity: one-way slab onto the X-beams as work-equivalent nodal
        loads (fixed-end actions V = qL/2, M = qL^2/12) - equivalent nodal
        loads reproduce a uniform line load exactly, and the members then
        develop real bending moments (the eleLoad command is broken on this
        OpenSees build). Y-beams carry no slab load (one-way, per hand).

        Lateral: hand storey forces F_i as nodal masses (share sums to 1 so
        the level total matches the hand model exactly).
        """
        ops.pattern("Plain", 1, 1)
        Lx = nx * bx
        grid_area = Lx * ny * by
        if horizontal is None:
            for iz in range(1, nz + 1):
                w_f = per_floor[iz - 1]
                for iy in range(ny + 1):
                    trib = by * (0.5 if iy in (0, ny) else 1.0)
                    q = w_f * trib / grid_area        # kN/m on this beam line
                    # per-SPAN equivalents (bay bx, not total length Lx)
                    v = q * bx / 2.0
                    m = q * bx * bx / 12.0
                    for ix in range(nx):
                        ops.load(nid(ix, iy, iz), 0, 0, -v, 0, m, 0)
                        ops.load(nid(ix + 1, iy, iz), 0, 0, -v, 0, -m, 0)
        else:
            for iz in range(1, nz + 1):
                w = per_floor[iz - 1]
                for j, s in enumerate(share):
                    node = 1 + j + iz * n_per_floor
                    if horizontal == "x":
                        ops.load(node, w * s, 0, 0, 0, 0, 0)
                    else:
                        ops.load(node, 0, w * s, 0, 0, 0, 0)
        if ops.analyze(1) != 0:
            raise RuntimeError("analyze failed")

    def forces(reg) -> dict[int, list[float]]:
        out: dict[int, list[float]] = {}
        for tag, _iz in reg:
            f = ops.eleForce(tag)
            if f:
                out[tag] = f
        return out

    def floor_means(dof: int) -> list[float]:
        means = []
        for iz in range(nz + 1):
            vals = [ops.nodeDisp(nid(ix, iy, iz), dof)
                    for iy in range(ny + 1) for ix in range(nx + 1)]
            means.append(sum(vals) / len(vals))
        return means

    def floor_spread(dof: int) -> float:
        worst = 0.0
        for iz in range(nz + 1):
            vals = [ops.nodeDisp(nid(ix, iy, iz), dof)
                    for iy in range(ny + 1) for ix in range(nx + 1)]
            worst = max(worst, max(vals) - min(vals))
        return worst

    def base_shear(col_reg, wall_reg) -> tuple[float, float]:
        """Global (X, Y) base shear summed from base-level element end-I
        forces. eleForce returns GLOBAL end forces on this build
        ([Fx, Fy, Fz, Mx, My, Mz] x2 - verified empirically), so slot 0 =
        Fx, slot 1 = Fy. nodeReaction is unusable under
        constraints('Transformation') on this build."""
        sx = sy = 0.0
        for tag, iz in col_reg:
            if iz != 0:
                continue
            f = ops.eleForce(tag)
            if f:
                sx += f[0]
                sy += f[1]
        for tag, iz in wall_reg:
            if iz != 0:
                continue
            f = ops.eleForce(tag)
            if f:
                sx += f[0]
                sy += f[1]
        return sx, sy

    with _LOCK:
        try:
            # ---- case G ---------------------------------------------------
            col_reg, bx_reg, by_reg, wall_reg = build()
            apply_case(gravity, None)
            f_g = (forces(col_reg), forces(bx_reg), forces(by_reg),
                   forces(wall_reg))
            u_g_top = floor_means(3)[-1]
            n_nodes = len(ops.getNodeTags())
            n_els = len(ops.getEleTags())

            # ---- case Ex --------------------------------------------------
            col_reg, bx_reg, by_reg, wall_reg = build()
            apply_case(f_lateral, "x")
            f_ex = forces(col_reg)
            bx_ex, by_ex = forces(bx_reg), forces(by_reg)
            ux = floor_means(1)
            spread_x = floor_spread(1)
            rx, _ry_g = base_shear(col_reg, wall_reg)

            # ---- case Ey --------------------------------------------------
            col_reg, bx_reg, by_reg, wall_reg = build()
            apply_case(f_lateral, "y")
            f_ey = forces(col_reg)
            bx_ey, by_ey = forces(bx_reg), forces(by_reg)
            uy = floor_means(2)
            spread_y = floor_spread(2)
            _rx_g, ry = base_shear(col_reg, wall_reg)
        finally:
            solve_ms = int((time.perf_counter() - t0) * 1000)
            ops.wipe()

    # ---- equilibrium ------------------------------------------------------
    ground = [r for r in sorted(analysis["seismic"]["storey_forces"],
                                key=lambda r: r["floor"]) if r["floor"] == 1]
    v_applied = ground[0]["V_kN"] if ground else 0.0
    err_x = abs(abs(rx) - v_applied) / v_applied * 100.0 if v_applied else 0.0
    err_y = abs(abs(ry) - v_applied) / v_applied * 100.0 if v_applied else 0.0

    # ---- drift ------------------------------------------------------------
    def max_drift(means: list[float]) -> tuple[float, int]:
        worst, gov = 0.0, 1
        for i in range(1, len(means)):
            idx = abs(means[i] - means[i - 1]) / h
            if idx > worst:
                worst, gov = idx, i
        return worst, gov

    dx_i, dx_f = max_drift(ux)
    dy_i, dy_f = max_drift(uy)
    if dx_i >= dy_i:
        drift_max, drift_dir, drift_floor = dx_i, "X", dx_f
    else:
        drift_max, drift_dir, drift_floor = dy_i, "Y", dy_f

    # ---- capacities (identical formulas to the hand method) ---------------
    cap_coeff = 0.4 * fck + 0.67 * fy * 0.01          # N/mm2 at 1% steel
    mu_strong = 0.136 * fck * d.beam_w_mm * (d.beam_d_mm - 50) ** 2 / 1e6
    mu_weak = 0.136 * fck * d.beam_d_mm * (d.beam_w_mm - 50) ** 2 / 1e6

    col_g, bx_g, by_g, _wall_g = f_g

    def end_vals(f: list[float]) -> tuple[float, float, float]:
        """Column helper: (|My|, |Mx|, |Fz|) over both ends.

        eleForce returns GLOBAL end forces on openseespy 3.7.0.1
        ([Fx, Fy, Fz, Mx, My, Mz] x2 - verified empirically): Fz = slot 2
        (axial for verticals), My = slot 4 (bends a column in global X),
        Mx = slot 3 (bends it in global Y)."""
        if len(f) >= 12:
            my = max(abs(f[4]), abs(f[10]))
            mx = max(abs(f[3]), abs(f[9]))
        else:
            my, mx = abs(f[4]), abs(f[3])
        return my, mx, abs(f[2])

    def beam_vals(f: list[float], axis: str) -> tuple[float, float]:
        """Beam helper: (|strong|, |weak|) end moments from GLOBAL forces.
        X-beam strong = My (slot 4/10); Y-beam strong = Mx (slot 3/9);
        plan bending (weak) = Mz (slot 5/11) for both orientations."""
        if len(f) >= 12:
            weak = max(abs(f[5]), abs(f[11]))
            strong = (max(abs(f[3]), abs(f[9])) if axis == "Y"
                      else max(abs(f[4]), abs(f[10])))
        else:
            strong = abs(f[3] if axis == "Y" else f[4])
            weak = abs(f[5])
        return strong, weak

    # beams: strong-axis from gravity, weak-axis (plan bending) from seismic
    beam_util_max = 0.0
    beam_m_max = 0.0
    beam_top: list[dict] = []
    for lbl, reg, ex_map, ey_map, g_map in (
            ("X", bx_reg, bx_ex, bx_ey, bx_g),
            ("Y", by_reg, by_ex, by_ey, by_g)):
        for tag, iz in reg:
            fg = g_map.get(tag)
            if not fg:
                continue
            mg, _ = beam_vals(fg, lbl)
            weak_e = 0.0
            if tag in ex_map:
                weak_e = max(weak_e, beam_vals(ex_map[tag], lbl)[1])
            if tag in ey_map:
                weak_e = max(weak_e, beam_vals(ey_map[tag], lbl)[1])
            m_u = 1.5 * mg
            mz_u = 1.5 * weak_e
            s_u = m_u / mu_strong if mu_strong else 9.9
            w_u = mz_u / mu_weak if mu_weak else 9.9
            util = max(s_u, w_u)
            beam_util_max = max(beam_util_max, util)
            beam_m_max = max(beam_m_max, m_u, mz_u)
            beam_top.append({
                "tag": tag, "axis": lbl, "floor": iz,
                "strong_kNm": round(m_u, 1), "strong_util": round(s_u, 3),
                "weak_kNm": round(mz_u, 1), "weak_util": round(w_u, 3),
                "util": round(util, 3),
            })
    beam_top.sort(key=lambda t: t["util"], reverse=True)
    beam_top = beam_top[:3]

    # columns: P-M interaction per element
    col_inter_max = 0.0
    col_worst = "-"
    col_detail: dict[str, Any] = {}
    for tag, iz in col_reg:
        cb, cd = col_sections[iz]
        bxp, byp = max(cb, cd), min(cb, cd)
        cap = 0.85 * cap_coeff * cb * cd / 1000.0     # kN (hand formula)
        mu_y = 0.136 * fck * byp * (bxp - 50) ** 2 / 1e6   # global-X bending
        mu_z = 0.136 * fck * bxp * (byp - 50) ** 2 / 1e6   # global-Y bending
        fg = col_g.get(tag)
        if not fg or cap <= 0:
            continue
        my_g, mz_g, n_g = end_vals(fg)
        n_e = my_e = mz_e = 0.0
        if tag in f_ex:
            ey_, ez_, en_ = end_vals(f_ex[tag])
            n_e, my_e, mz_e = en_, ey_, ez_
        if tag in f_ey:
            ey_, ez_, en_ = end_vals(f_ey[tag])
            n_e = max(n_e, en_)
            my_e = max(my_e, ey_)
            mz_e = max(mz_e, ez_)
        p_u = 1.5 * (n_g + n_e)
        my_u = 1.5 * max(my_g, my_e)
        mz_u = 1.5 * max(mz_g, mz_e)
        inter = p_u / cap + max(my_u / mu_y if mu_y else 9.9,
                                mz_u / mu_z if mu_z else 9.9)
        if inter > col_inter_max:
            col_inter_max = inter
            col_worst = f"floor {iz + 1}, {cb:.0f} x {cd:.0f} mm"
            col_detail = {
                "n_g": round(n_g, 1), "n_e": round(n_e, 1),
                "p_term": round(p_u / cap, 3),
                "my_u": round(my_u, 1), "mz_u": round(mz_u, 1),
                "mu_y": round(mu_y, 1), "mu_z": round(mu_z, 1),
                "cap_kN": round(cap, 1),
            }

    return {
        "ok": True,
        "backend": "openseespy",
        "nodes": n_nodes,
        "elements": n_els,
        "cases": ["G service", "Ex IS 1893 F_i", "Ey IS 1893 F_i"],
        "solve_ms": solve_ms,
        "equilibrium_err_pct": round(max(err_x, err_y), 3),
        "base_shear_fea_kN": {"x": round(abs(rx), 1), "y": round(abs(ry), 1)},
        "base_shear_applied_kN": round(v_applied, 1),
        "drift_max_index": round(drift_max, 5),
        "drift_direction": drift_dir,
        "drift_governing_floor": drift_floor,
        "beam_max_util": round(beam_util_max, 3),
        "beam_Mmax_kNm": round(beam_m_max, 1),
        "beam_top3": beam_top,
        "column_max_interaction": round(col_inter_max, 3),
        "column_worst": col_worst,
        "column_worst_detail": col_detail,
        "diaphragm_spread_max_mm": round(max(spread_x, spread_y) * 1000, 1),
        "gravity_top_mm": round(abs(u_g_top) * 1000, 2),
        "core_walls_modelled": bool(d.core),
        "debug_q": {
            "gravity_per_floor": gravity,
            "grid_area": round(nx * bx * ny * by, 1),
            "q_interior_kNm": round(gravity[0] * by / (nx * bx * ny * by), 2)
            if gravity else 0,
            "Lx": round(nx * bx, 1),
        },
        "approximations": [
            "linear static, no P-delta (Phase 3)",
            "1.5 x elastic end moments vs limit-state capacity",
            "core walls as centre-line elements tied by diaphragm",
            "secondary beams not modelled (slab checked by hand)",
        ],
    }
