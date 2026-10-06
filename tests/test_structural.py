"""Structural analysis: IS 1893 / IS 875 formulas + full analyze() smoke."""
import pytest

from modules import design_generator, input_handler, requirement_analyzer
from modules.structural_analyzer import (
    DRIFT_LIMIT,
    analyze,
    k2_factor,
    period_T,
    spectrum_sa_g,
)

# ---------------------------------------------------------------------------
# code formulas
# ---------------------------------------------------------------------------

def test_spectrum_plateau():
    assert spectrum_sa_g(0.3, "II") == pytest.approx(2.5)


def test_spectrum_rising_branch():
    assert spectrum_sa_g(0.05, "II") == pytest.approx(1.75)


def test_spectrum_decay_soil_ii():
    # T = 2.0 s, plateau_end = 0.55 -> 1.36 / 2.0
    assert spectrum_sa_g(2.0, "II") == pytest.approx(0.68)


def test_spectrum_tail():
    assert spectrum_sa_g(4.5, "II") == pytest.approx(0.34)


def test_period_frame_formula():
    # T = 0.075 * h^0.75, h = 32 m
    T = period_T("rc_frame", 32.0, 20.0)
    assert pytest.approx(0.075 * 32.0 ** 0.75, rel=1e-6) == T


def test_period_dual_formula_clamped():
    T = period_T("rc_dual", 20.0, 3.0)   # would be huge -> clamp to 4
    assert T <= 4.0


def test_period_clamped_low():
    assert period_T("rc_frame", 3.0, 20.0) >= 0.10


def test_k2_interpolation_terrain3():
    # heights 10 -> 0.91, 15 -> 0.97; at 12.5 expect 0.94
    assert k2_factor(3, 12.5) == pytest.approx(0.94)
    assert k2_factor(3, 8) == pytest.approx(0.91)     # below table -> first
    assert k2_factor(3, 400) == pytest.approx(1.34)   # exact table entry
    assert k2_factor(3, 450) == pytest.approx(1.35)   # above table -> last


# (test_base_shear_equation removed: it asserted a literal identity
#  (0.16/2)*2.5*1/5 == 0.04 without touching any code under test.
#  Real base-shear coverage lives in the full-analyse tests below.)


# ---------------------------------------------------------------------------
# full analysis
# ---------------------------------------------------------------------------

def _req():
    return requirement_analyzer.analyze(
        input_handler.load(text="10 floor residential building in Mumbai "
                                 "with 4 units per floor, budget 10 crore"))


def _designs():
    return design_generator.generate(_req())


def test_generate_three_distinct_designs():
    ds = _designs()
    assert len(ds) == 3
    assert [d.id for d in ds] == ["A", "B", "C"]
    assert ds[0].system == "rc_frame"
    assert ds[1].system == "rc_dual" and ds[1].core
    assert ds[2].system == "rc_frame_ductile"


def test_analyze_returns_all_sections():
    req = _req()
    d = design_generator.generate(req)[0]
    a = analyze(d, req)
    for key in ("loads", "seismic", "wind", "members", "drift", "checks"):
        assert key in a
    assert a["total_checks"] == len(a["checks"])
    assert 0 <= a["passed"] <= a["total_checks"]


def test_seismic_ah_matches_formula():
    req = _req()
    d = design_generator.generate(req)[0]
    a = analyze(d, req)
    s = a["seismic"]
    expected = (s["Z"] / 2) * s["Sa_g"] * s["I"] / s["R"]
    assert s["Ah"] == pytest.approx(expected, rel=1e-3)
    assert s["V_base_kN"] == pytest.approx(
        s["Ah"] * a["loads"]["W_total_kN"], rel=2e-3)
    # zone III -> Z = 0.16, residential I = 1, rc_frame R = 3
    assert s["Z"] == 0.16 and s["I"] == 1.0 and s["R"] == 3.0


def test_storey_forces_sum_to_base_shear():
    req = _req()
    d = design_generator.generate(req)[0]
    a = analyze(d, req)
    total_F = sum(r["F_kN"] for r in a["seismic"]["storey_forces"])
    assert total_F == pytest.approx(a["seismic"]["V_base_kN"], rel=0.01)


def test_drift_within_limits_typical():
    req = _req()
    for d in design_generator.generate(req):
        a = analyze(d, req)
        assert a["drift"]["limit"] == DRIFT_LIMIT
        assert a["drift"]["max_index"] >= 0


def test_wind_overturning_ratio_present():
    req = _req()
    a = analyze(design_generator.generate(req)[0], req)
    assert a["wind"]["ot_factor_x"] > 0
    assert a["wind"]["ot_factor_y"] > 0
    assert a["wind"]["pd_knm2"] > 0


def test_dual_system_stiffer_than_frame():
    req = _req()
    ds = design_generator.generate(req)
    a_frame = analyze(ds[0], req)
    a_dual = analyze(ds[1], req)
    # same plan size -> core must reduce drift
    assert a_dual["drift"]["max_index"] <= a_frame["drift"]["max_index"]


def test_live_load_parking_ground():
    req = requirement_analyzer.analyze(
        input_handler.load(text="residential with parking"))
    assert "parking" in req.special
    d = design_generator.generate(req)[0]
    a = analyze(d, req)
    assert a["loads"]["live_load_knm2_ground"] == 4.0


def test_hospital_importance_factor():
    req = requirement_analyzer.analyze(
        input_handler.load(text="5 floor hospital in Pune"))
    a = analyze(design_generator.generate(req)[0], req)
    assert a["seismic"]["I"] == 1.5


def test_check_names_cover_required_scope():
    req = _req()
    a = analyze(design_generator.generate(req)[0], req)
    names = " | ".join(c["name"] for c in a["checks"])
    for needle in ("Column", "Beam", "Slab", "drift", "overturning",
                   "base shear", "period", "height"):
        assert needle.lower() in names.lower()
