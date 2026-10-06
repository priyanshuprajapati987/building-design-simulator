"""Phase-2 OpenSees FEA backend: availability, skip paths, solve sanity,
check wiring and determinism."""
import json

import pytest

import config as cfg
from modules import (
    design_generator,
    fea,
    input_handler,
    optimization_engine,
    requirement_analyzer,
)
from modules.structural_analyzer import analyze

# four checks append_checks() must add when a solve succeeds
_FEA_CHECKS = (
    "FEA base-shear equilibrium (max error)",
    "FEA inter-story drift index (max)",
    "FEA beam moment (max utilisation)",
    "FEA column interaction (max utilisation)",
)


def _req():
    return requirement_analyzer.analyze(
        input_handler.load(text="3 floor house in Pune"))


@pytest.fixture(scope="module")
def small():
    """(req, design, hand analysis) for a cheap 3-floor model."""
    req = _req()
    d = design_generator.generate(req)[0]
    return req, d, analyze(d, req)


# ---------------------------------------------------------------------------
# availability + skip paths (never raise)
# ---------------------------------------------------------------------------

def test_openseespy_available():
    if not fea.is_available():
        pytest.skip(f"OpenSeesPy unavailable: {fea.unavailable_reason()}")
    assert fea.is_available() is True


def test_disabled_flag_skips(monkeypatch, small):
    req, d, analysis = small
    monkeypatch.setattr(cfg, "FEA_ENABLED", False)
    res = fea.verify(d, req, analysis)
    assert res == {"ok": False, "skipped": "FEA disabled (FEA_ENABLED=0)"}


def test_unavailable_engine_skips(monkeypatch, small):
    req, d, analysis = small
    monkeypatch.setattr(fea, "_ATTEMPTED", True)
    monkeypatch.setattr(fea, "_OPS", None)
    monkeypatch.setattr(fea, "_IMPORT_ERROR", "DLL load failed")
    res = fea.verify(d, req, analysis)
    assert res["ok"] is False
    assert "OpenSeesPy unavailable: DLL load failed" in res["skipped"]


def test_missing_load_record_skips(small):
    req, d, _ = small
    res = fea.verify(d, req, {"checks": []})
    assert res["ok"] is False
    assert "load record missing" in res["skipped"]


def test_garbage_analysis_never_raises(small):
    req, d, _ = small
    res = fea.verify(d, req, {})
    assert res["ok"] is False


def test_model_too_large_skips(small):
    req, d, _ = small
    d2 = type(d)(**{**d.__dict__, "floors": 60, "bays_x": 14, "bays_y": 14})
    analysis = {
        "loads": {"floor_gravity_kN": [1000.0] * 60},
        "seismic": {"storey_forces": [
            {"floor": i, "F_kN": 10.0} for i in range(1, 61)]},
        "members": {"columns": [{"section_mm": "400 x 400"}] * 60},
    }
    res = fea.verify(d2, req, analysis)
    assert res["ok"] is False
    assert "model too large" in res["skipped"]


# ---------------------------------------------------------------------------
# real solve: shape, sanity bounds, JSON safety, determinism
# ---------------------------------------------------------------------------

def test_verify_solves_and_is_json_safe(small):
    req, d, analysis = small
    res = fea.verify(d, req, analysis)
    if not res.get("ok"):
        pytest.skip(f"FEA skipped: {res.get('skipped') or res.get('error')}")
    json.dumps(res)                      # must be report/summary safe
    for key in ("equilibrium_err_pct", "base_shear_applied_kN",
                "drift_max_index", "beam_max_util",
                "column_max_interaction", "solve_ms"):
        assert key in res, key


def test_result_declares_four_cases_and_combos(small):
    """Phase-2b: D/L/Ex/Ey cases + explicit LC1-LC3 combination record."""
    req, d, analysis = small
    res = fea.verify(d, req, analysis)
    if not res.get("ok"):
        pytest.skip(f"FEA skipped: {res.get('skipped') or res.get('error')}")
    assert res["cases"] == ["D dead", "L live",
                            "Ex IS 1893 F_i", "Ey IS 1893 F_i"]
    assert len(res["combos"]) == 3
    assert res["combos"][0].startswith("LC1")
    # dead/live split actually recorded in the hand loads used for D + L
    loads = analysis["loads"]
    assert len(loads["floor_dead_kN"]) == d.floors
    assert sum(loads["floor_dead_kN"]) < sum(loads["floor_gravity_kN"])
    # vertical equilibrium of the D case is folded into the max-error check
    assert res["vertical_equilibrium_err_pct"] <= 5.0
    assert res["equilibrium_err_pct"] >= res["vertical_equilibrium_err_pct"]


def test_verify_sane_bounds(small):
    req, d, analysis = small
    res = fea.verify(d, req, analysis)
    if not res.get("ok"):
        pytest.skip(f"FEA skipped: {res.get('skipped') or res.get('error')}")
    # equilibrium is solver integrity, not a design limit
    assert abs(res["equilibrium_err_pct"]) <= 5.0
    # raw (pre-optimisation) sanity: non-negative + these bounds catch the
    # historic per-span load bug that inflated moments ~nx^2 (col inter ~10+)
    assert 0.0 <= res["drift_max_index"] <= 0.004
    assert 0.0 <= res["beam_max_util"] <= 6.0
    # honesty anchor: corrected end forces (eleForce - f_eq) must track the
    # hand span formula - pre-fix K*u alone under-reported raw beams ~6x
    hand_beam = next((c["value"] for c in analysis["checks"]
                      if "Beam moment" in c["name"]), None)
    if hand_beam:
        assert res["beam_max_util"] == pytest.approx(hand_beam, rel=0.5)
    assert res["column_max_interaction"] >= 0.0
    assert res["base_shear_applied_kN"] > 0


@pytest.fixture(scope="module")
def optimized(small):
    """Design A after the fix/re-test loop (hand + FEA checks)."""
    req, d, _ = small
    best, analysis, _fixes = optimization_engine.optimize(d, req)
    return req, best, analysis


def test_optimised_design_passes_all_checks(optimized):
    """End-to-end: the optimizer must drive every hand + FEA check to pass
    on a small design (incl. FEA column P-M interaction <= 1.0)."""
    _, _, analysis = optimized
    if not analysis.get("fea", {}).get("ok"):
        pytest.skip("FEA skipped in this environment")
    res = analysis["fea"]
    assert abs(res["equilibrium_err_pct"]) <= 5.0
    assert res["drift_max_index"] <= 0.004
    assert res["beam_max_util"] <= 1.0
    assert res["column_max_interaction"] <= 1.0
    failed = [c["name"] for c in analysis["checks"] if not c["passed"]]
    assert not failed, failed


def test_verify_deterministic(small):
    req, d, analysis = small
    r1 = fea.verify(d, req, analysis)
    if not r1.get("ok"):
        pytest.skip(f"FEA skipped: {r1.get('skipped') or r1.get('error')}")
    r2 = fea.verify(d, req, analysis)
    assert r2["ok"] is True
    assert r1["equilibrium_err_pct"] == pytest.approx(
        r2["equilibrium_err_pct"], abs=1e-9)
    assert r1["drift_max_index"] == pytest.approx(
        r2["drift_max_index"], rel=1e-9)
    assert r1["beam_max_util"] == pytest.approx(r2["beam_max_util"],
                                                rel=1e-9)


# ---------------------------------------------------------------------------
# append_checks wiring
# ---------------------------------------------------------------------------

def _ok_fea():
    return {"ok": True, "equilibrium_err_pct": 0.4,
            "base_shear_applied_kN": 1000.0,
            "base_shear_fea_kN": {"x": 600.0, "y": 400.0},
            "drift_max_index": 0.001, "drift_direction": "X",
            "drift_governing_floor": 3,
            "beam_max_util": 0.7, "beam_Mmax_kNm": 120.0,
            "column_max_interaction": 0.9, "column_worst": "floor 1"}


def test_append_checks_adds_four():
    analysis = {"checks": [{"name": "hand check", "value": 1, "unit": "-",
                            "limit": "<= 2", "passed": True}]}
    analysis["fea"] = _ok_fea()
    fea.append_checks(analysis)
    names = [c["name"] for c in analysis["checks"]]
    for n in _FEA_CHECKS:
        assert n in names
    assert analysis["total_checks"] == len(analysis["checks"])
    assert analysis["passed"] == sum(1 for c in analysis["checks"]
                                     if c["passed"])
    # values mirror the fea dict
    by_name = {c["name"]: c for c in analysis["checks"]}
    assert by_name[_FEA_CHECKS[0]]["value"] == 0.4
    assert by_name[_FEA_CHECKS[3]]["value"] == 0.9
    assert by_name[_FEA_CHECKS[3]]["passed"] is True


def test_append_checks_respects_limits():
    fea_bad = _ok_fea()
    fea_bad.update(equilibrium_err_pct=9.0, drift_max_index=0.02,
                   beam_max_util=1.5, column_max_interaction=2.0)
    analysis = {"checks": [], "fea": fea_bad}
    fea.append_checks(analysis)
    assert all(c["passed"] is False for c in analysis["checks"])
    assert analysis["passed"] == 0


def test_append_checks_noop_when_not_ok():
    checks = [{"name": "hand check", "value": 1, "unit": "-",
               "limit": "<= 2", "passed": True}]
    analysis = {"checks": checks, "fea": {"ok": False,
                                          "skipped": "FEA disabled"}}
    fea.append_checks(analysis)
    assert analysis["checks"] == checks
    assert "total_checks" not in analysis


# ---------------------------------------------------------------------------
# optimiser integration: _refresh must expose both hand + FEA failures
# ---------------------------------------------------------------------------

def test_refresh_runs_hand_and_fea():
    req = _req()
    d = design_generator.generate(req)[0]
    analysis = optimization_engine._refresh(d, req)
    names = [c["name"] for c in analysis["checks"]]
    if analysis.get("fea", {}).get("ok"):
        for n in _FEA_CHECKS:
            assert n in names
    else:
        # FEA skipped (engine missing/disabled): hand checks still intact
        assert any("Column" in n for n in names)
    assert analysis["passed"] <= analysis["total_checks"]
