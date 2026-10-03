"""Regression tests for bugs found in the Phase-1 bug-hunt probe (Oct 2026).

Covers: partial-number text parsing, non-positive floor clamping, the
seismic V/W sanity band for critical buildings, optimizer round-robin +
honest advisories, footprint-vs-plot warnings, and output-dir collision.
"""
import datetime as _dt

import pytest

import config as cfg
from modules import input_handler, optimization_engine, pipeline, requirement_analyzer
from modules.models import Requirements

# ---------------------------------------------------------------------------
# BUG 1-4: parse_text partial-number / false-positive matches
# ---------------------------------------------------------------------------

def test_floors_three_digit_not_sliced():
    out = input_handler.parse_text("100 floor residential in Pune")
    assert out["floors"] == 100          # was floors=0 ("00")


def test_floors_120_storey_not_sliced():
    out = input_handler.parse_text("120 storey tower in Mumbai")
    assert out["floors"] == 120          # was floors=20 ("20")


def test_units_1000_not_sliced():
    out = input_handler.parse_text("1000 units per floor residential")
    assert out["units_per_floor"] == 1000  # was units=0


def test_g_plus_n_does_not_set_units():
    out = input_handler.parse_text("G+50 apartment in Chennai")
    assert out["floors"] == 51
    assert out.get("units_per_floor") is None  # was units=50


def test_units_still_parses_normally():
    out = input_handler.parse_text(
        "10 floor residential in Pune with 4 units per floor")
    assert out["floors"] == 10
    assert out["units_per_floor"] == 4

# ---------------------------------------------------------------------------
# BUG 3: non-positive floor count must warn (not silently clamp)
# ---------------------------------------------------------------------------

def test_zero_floors_dict_warns_then_clamps():
    req = Requirements(floors=0)
    requirement_analyzer.analyze(req)
    assert req.floors == 1
    assert any("invalid floors=0" in w for w in req.warnings)

# ---------------------------------------------------------------------------
# BUG 10: V/W sanity band upper bound must scale with zone factor Z
# ---------------------------------------------------------------------------

def test_zone_v_critical_building_band_passes():
    # 1-floor hospital (I=1.5) in Bhuj (Zone V), soft soil -> Ah up to 0.225;
    # the old hard-coded 0.15 upper band falsely failed this.
    s = pipeline.run(text="1 floor hospital in Bhuj, budget 20 crore",
                     make_pdf=False, make_images=False)
    for r in s["results"]:
        band = next(c for c in r["analysis"]["checks"]
                    if c["name"] == "Seismic base shear ratio V/W")
        assert band["passed"], band

def test_band_limit_text_is_zone_derived():
    s = pipeline.run(text="2 floor school in Delhi, budget 30 crore",
                     make_pdf=False, make_images=False)
    band = next(c for c in s["results"][0]["analysis"]["checks"]
                if c["name"] == "Seismic base shear ratio V/W")
    # Delhi is Zone IV (Z=0.24) -> upper bound 0.625 * 0.24 = 0.15
    hi = float(band["limit"].rsplit("-", 1)[1].strip())
    assert hi == pytest.approx(0.625 * 0.24, abs=5e-4)

# ---------------------------------------------------------------------------
# BUG 5/6: optimizer fairness + honest advisories (no fake fixes)
# ---------------------------------------------------------------------------

def test_stubborn_case_still_fixes_beam():
    """60-floor industrial: column dominates the failed list, but with fair
    rotation the beam must still get fix attempts (was starved forever)."""
    s = pipeline.run(text="60 floor industrial in Guwahati, zone V",
                     make_pdf=False, make_images=False)
    for r in s["results"]:
        beam_fixes = [f for f in r["fixes"]
                      if f["issue"].startswith("Beam")
                      and not f["action"].startswith("advisory")]
        assert beam_fixes, f"{r['design']['id']} never attempted a beam fix"

def test_column_ladder_exhaustion_is_advisory_not_fake_fix():
    s = pipeline.run(text="60 floor industrial in Guwahati, zone V",
                     make_pdf=False, make_images=False)
    for r in s["results"]:
        final = [c for c in r["analysis"]["checks"]
                 if c["name"] == "Column axial capacity (max utilisation)"]
        if final and not final[0]["passed"]:
            tail = [f for f in r["fixes"]
                    if f["issue"] == final[0]["name"]
                    and str(f["iteration"]) == "-"]
            assert tail and "still failing" in tail[0]["action"]

def test_height_advisory_is_specific():
    s = pipeline.run(text="60 floor industrial in Guwahati",
                     make_pdf=False, make_images=False)
    advisories = [f for f in s["results"][0]["fixes"]
                  if f["issue"] == "Building height"]
    assert advisories, "height failure must carry an advisory"
    assert "dynamic analysis" in advisories[-1]["action"]

def test_midrise_still_fully_fixable():
    """Fair-rotation rewrite must not regress the normal happy path."""
    s = pipeline.run(
        text="5 floor residential in Pune with 8 units per floor on 1 acre, "
             "budget 40 crore",
        make_pdf=False, make_images=False)
    for r in s["results"]:
        failed = [c for c in r["analysis"]["checks"] if not c["passed"]]
        assert not failed, [c["name"] for c in failed]

# ---------------------------------------------------------------------------
# BUG 7: footprint vs plot / units implied-footprint warnings
# ---------------------------------------------------------------------------

def test_footprint_vs_tiny_plot_warns():
    s = pipeline.run(
        data={"floors": 2, "building_type": "residential",
              "units_per_floor": 50, "land_area_sqft": 200,
              "city": "Pune"},
        make_pdf=False, make_images=False)
    warns = s["requirements"]["warnings"]
    assert any("exceeds 70% of the" in w for w in warns), warns

def test_units_implied_footprint_grid_cap_warns():
    s = pipeline.run(
        data={"floors": 5, "building_type": "residential",
              "units_per_floor": 400, "land_area_sqft": 200000,
              "city": "Pune"},
        make_pdf=False, make_images=False)
    warns = s["requirements"]["warnings"]
    assert any("capped at 12 bays" in w for w in warns), warns

# ---------------------------------------------------------------------------
# BUG 8: same-second output-dir collision -> uniquified
# ---------------------------------------------------------------------------

class _FrozenDateTime:
    @staticmethod
    def now() -> _dt.datetime:
        return _dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)

def test_output_dir_uniquified_on_collision(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(pipeline, "datetime", _FrozenDateTime)
    s1 = pipeline.run(text="2 floor office in Pune",
                      make_pdf=False, make_images=False)
    s2 = pipeline.run(text="2 floor office in Pune",
                      make_pdf=False, make_images=False)
    d1 = (tmp_path / "run_20260101_120000")
    d2 = (tmp_path / "run_20260101_120000_1")
    assert d1.is_dir() and d2.is_dir()
    assert s1 is not None and s2 is not None

# ---------------------------------------------------------------------------
# optimizer module sanity (direct)
# ---------------------------------------------------------------------------

def test_optimizer_direct_returns_honest_log():
    req = input_handler.load(text="15 floor commercial in Delhi, budget 60")
    requirement_analyzer.analyze(req)
    from modules import design_generator
    d = design_generator.generate(req)[0]
    _best, analysis, log = optimization_engine.optimize(d, req)
    assert isinstance(log, list)
    # every entry must be a real action or an explicit advisory - no silent
    # "fixed" claim that the final analysis contradicts
    for f in log:
        assert f["issue"] and f["action"]
    still_failed = [c["name"] for c in analysis["checks"] if not c["passed"]]
    for name in still_failed:
        if name in ("Budget compliance",):
            continue
        mentioned = any(f["issue"] == name for f in log)
        assert mentioned, f"failed check {name!r} missing from fix log"
