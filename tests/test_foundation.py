"""Phase-2b: IS 456 spread-footing design + load-combination wiring."""
import math

import pytest

import config as cfg
from modules import (
    design_generator,
    foundation,
    input_handler,
    optimization_engine,
    requirement_analyzer,
    structural_analyzer,
)
from modules.foundation import (
    design_footing,
    existing_positions,
    seismic_axial_factors,
    trib_ratios,
    worst_utils,
)
from modules.structural_analyzer import analyze


def _req():
    return requirement_analyzer.analyze(
        input_handler.load(text="3 floor house in Pune"))


# ---------------------------------------------------------------------------
# codes.json foundation + load_combos blocks
# ---------------------------------------------------------------------------

def test_sbc_zone_ordering():
    sbc = cfg.get_codes()["foundation"]["sbc_knm2"]
    assert sbc["I"] >= sbc["II"] >= sbc["III"] > 0


def test_foundation_block_bounds():
    fd = cfg.get_codes()["foundation"]
    assert fd["min_depth_mm"] < fd["max_depth_mm"]
    assert fd["footing_cover_mm"] < fd["min_depth_mm"]
    assert fd["bump_step_mm"] % fd["size_step_mm"] == 0
    assert fd["max_bumps"] > 0


def test_load_combo_factors():
    lc = cfg.get_codes()["load_combos"]
    assert lc["LC1"]["EQ"] == 0.0 and lc["LC1"]["DL"] == 1.5
    assert lc["LC2"]["EQ"] == 1.2 and lc["LC2"]["DL"] == 1.2
    assert lc["LC3"]["LL"] == 0.0 and lc["LC3"]["DL"] == 0.9
    # gravity+EQ combo can never be lighter than min-gravity combo on DL
    assert lc["LC2"]["DL"] > lc["LC3"]["DL"]


# ---------------------------------------------------------------------------
# tributary / position / seismic-couple helpers
# ---------------------------------------------------------------------------

def test_trib_ratios_scale():
    req = _req()
    d = design_generator.generate(req)[0]
    t = trib_ratios(d)
    assert set(t) == set(foundation.POSITIONS)
    assert t["edge"] == 2 * t["corner"]
    assert t["interior"] == 4 * t["corner"]
    assert all(v > 0 for v in t.values())


def test_existing_positions_grid_dependent():
    req = _req()
    d = design_generator.generate(req)[0]
    assert existing_positions(d) == list(foundation.POSITIONS)
    # 1x1 grid: every column sits on a corner
    d1 = type(d)(**{**d.__dict__, "bays_x": 1, "bays_y": 1})
    assert existing_positions(d1) == ["corner"]


def test_seismic_axial_corner_governs():
    req = _req()
    d = design_generator.generate(req)[0]
    f = seismic_axial_factors(d)
    assert f["corner"] >= f["edge"] >= f["interior"] > 0


# ---------------------------------------------------------------------------
# design_footing: sizing + the three checks (hand-recomputed)
# ---------------------------------------------------------------------------

def test_min_size_and_thickness():
    r = design_footing(150.0, 220.0, 250.0, 25.0, col_mm=(300, 300))
    fd = cfg.get_codes()["foundation"]
    assert r["B_mm"] >= 600
    assert r["B_mm"] % fd["size_step_mm"] == 0
    assert r["thickness_mm"] >= fd["min_depth_mm"]


def test_bearing_util_passes_for_sane_load():
    r = design_footing(2000.0, 3000.0, 250.0, 25.0, col_mm=(450, 450))
    assert r["bearing_util"] <= 1.0
    # service pressure never exceeds SBC (self-weight iteration included)
    assert r["q_service_knm2"] <= r["sbc_knm2"] * 1.001


def test_bump_enlarges_base_by_step():
    r0 = design_footing(800.0, 1200.0, 250.0, 25.0, col_mm=(400, 400))
    r3 = design_footing(800.0, 1200.0, 250.0, 25.0, col_mm=(400, 400),
                        bump=3)
    assert r3["B_mm"] == r0["B_mm"] + 3 * 100     # bump_step = 100 mm
    assert r3["q_service_knm2"] <= r0["q_service_knm2"]


def test_forced_thickness_kept():
    r = design_footing(500.0, 750.0, 250.0, 25.0, col_mm=(300, 300),
                       t_mm=650)
    assert r["thickness_mm"] == 650


def test_shear_and_punching_formulas():
    """Recompute one-way + punching stress exactly as IS 456 CL 40.2/31.6
    (punching tau = V / (u * d) - regression for the historic missing-d bug)."""
    p_srv, p_u, sbc, fck = 1200.0, 1800.0, 250.0, 25.0
    cx, cy = 450, 450
    r = design_footing(p_srv, p_u, sbc, fck, col_mm=(cx, cy))
    b, t = r["B_mm"], r["thickness_mm"]
    d_eff = t - 50 - 6
    q_u = p_u / (b / 1000.0) ** 2

    over = (b - cx) / 2.0
    v = q_u * max(over - d_eff, 0.0) / 1000.0 * (b / 1000.0)
    tau = v * 1000.0 / (b * d_eff)
    assert r["shear_util"] == round(tau / (0.16 * math.sqrt(fck)), 2)

    px, py = cx + d_eff, cy + d_eff
    v_p = q_u * max((b / 1000.0) ** 2 - px * py / 1e6, 0.0)
    tau_p = v_p * 1000.0 / (2.0 * (px + py) * d_eff)
    assert r["punching_util"] == round(tau_p / (0.33 * math.sqrt(fck)), 2)


def test_moment_formula():
    p_u, fck = 1800.0, 25.0
    r = design_footing(1200.0, p_u, 250.0, fck, col_mm=(450, 450))
    b, t = r["B_mm"], r["thickness_mm"]
    d_eff = t - 50 - 6
    q_u = p_u / (b / 1000.0) ** 2
    a_max = (b - 450) / 2.0 / 1000.0
    m_u = q_u * a_max ** 2 / 2.0
    mu_cap = 0.136 * fck * 1000.0 * d_eff ** 2 / 1e6
    assert r["moment_util"] == round(m_u / mu_cap, 2)


def test_worst_utils_aggregation():
    rows = [{"bearing_util": 0.4, "shear_util": 0.6, "punching_util": 0.3,
             "moment_util": 0.2},
            {"bearing_util": 0.9, "shear_util": 0.2, "punching_util": 0.5,
             "moment_util": 0.7}]
    assert worst_utils(rows) == (0.9, 0.6, 0.7)
    assert worst_utils([]) == (0.0, 0.0, 0.0)


# ---------------------------------------------------------------------------
# optimizer helpers + fix path
# ---------------------------------------------------------------------------

def test_optimizer_helper_gate():
    assert optimization_engine.codes_max_bumps() >= 1
    assert optimization_engine.codes_max_depth() >= 300
    assert optimization_engine.foundation_sbc(None) == 250.0   # zone II default
    assert optimization_engine.foundation_sbc(
        {"foundation": {"sbc_knm2": 100.0}}) == 100.0


def test_optimizer_kind_mapping():
    kind = optimization_engine._kind
    assert kind("Foundation bearing utilisation") == "foundation_bearing"
    assert kind("Foundation bearing/shear/moment (max utilisation)") \
        == "foundation_bearing"
    assert kind("Foundation one-way shear (max utilisation)") \
        == "foundation_depth"
    assert kind("Foundation moment at column face") == "foundation_depth"
    assert kind("Budget compliance") is None


def test_optimizer_foundation_thickness_lookup():
    req = _req()
    d = design_generator.generate(req)[0]
    last = optimization_engine._last_foundation_thickness
    # forced design value wins
    d.footing_t_mm = 450
    assert last({"foundation": {"max_thickness_mm": 700}}, d) == 450
    # else analysis-derived value
    d.footing_t_mm = 0
    assert last({"foundation": {"max_thickness_mm": 700}}, d) == 700
    # else codes ceiling fallback
    assert last({}, d) == optimization_engine.codes_max_depth()


def test_optimizer_fix_loop_enlarges_footing(monkeypatch, tmp_path):
    """End-to-end through optimize(): a failing foundation-bearing check
    must produce a footing-bump fix (or a one-time advisory at the ceiling)."""
    req = _req()
    d = design_generator.generate(req)[0]
    call = {"n": 0}
    fail = {"checks": [{"name": "Foundation bearing (max utilisation)",
                         "value": 1.3, "unit": "-", "limit": "<= 1.00",
                         "passed": False}],
            "foundation": {"sbc_knm2": 250.0, "max_thickness_mm": 300}}

    def fake_refresh(_d, _req):
        call["n"] += 1
        if call["n"] == 1:
            return dict(fail)
        return {"checks": [], "foundation": {"sbc_knm2": 250.0,
                                             "max_thickness_mm": 300}}

    monkeypatch.setattr(optimization_engine, "_refresh", fake_refresh)
    best, _analysis, fix_log = optimization_engine.optimize(d, req)
    assert call["n"] >= 2, "optimizer must re-test after the fix"
    assert best.footing_bump == 1
    assert any("footing base enlarged" in f["action"] for f in fix_log)


# ---------------------------------------------------------------------------
# analyzer wiring: analysis['foundation'] + ['load_combos']
# ---------------------------------------------------------------------------

def test_analysis_foundation_block_shape():
    req = _req()
    d = design_generator.generate(req)[0]
    a = analyze(d, req)
    fnd = a["foundation"]
    for key in ("soil_type", "sbc_knm2", "column_loads", "footings",
                "max_thickness_mm", "note"):
        assert key in fnd, key
    assert fnd["footings"], "at least one footing row"
    for row in fnd["footings"]:
        assert row["position"] in existing_positions(d)
        assert row["B_mm"] >= 600 and row["thickness_mm"] >= 300
        assert row["governing_combo"] in ("LC1", "LC2", "LC3")
        assert row["bearing_util"] <= 1.001
        assert row["util"] <= 1.001
    assert fnd["max_thickness_mm"] == max(r["thickness_mm"]
                                          for r in fnd["footings"])


def test_analysis_load_combos_present_and_lc2_governs_with_eq():
    req = _req()
    d = design_generator.generate(req)[0]
    a = analyze(d, req)
    lc = a["load_combos"]
    assert set(lc) == {"LC1", "LC2", "LC3"}
    assert "_comment" not in lc
    # every column row carries a legal combo + position tag
    for row in a["members"]["columns"]:
        assert row["combo"] in ("LC1", "LC2", "LC3")
        assert row["position"] in existing_positions(d)
    # EQ present (Pune) -> LC2 (D+L+EQ all at 1.2) must be more severe on
    # the total load than LC1 (D+L at 1.5): 3.6 > 3.0
    f = lc["LC1"]
    g2 = lc["LC2"]
    assert (g2["DL"] + g2["LL"] + g2["EQ"]) > (f["DL"] + f["LL"])


def test_dead_live_split_recorded():
    req = _req()
    d = design_generator.generate(req)[0]
    a = analyze(d, req)
    loads = a["loads"]
    dead = loads["floor_dead_kN"]
    grav = loads["floor_gravity_kN"]
    assert len(dead) == len(grav) == d.floors
    assert all(0 < dd <= g for dd, g in zip(dead, grav))
    assert sum(dead) < sum(grav)               # live load exists on floors
    # exact dead-load unit weight: slab self + finish + MEP + partition + beam
    dl = structural_analyzer._dead_load(req, d)
    assert dead[0] == pytest.approx(dl * d.plate_sqm, abs=0.15)
