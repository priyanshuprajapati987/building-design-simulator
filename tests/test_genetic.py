"""Phase-3A: genetic grid search + pipeline wiring."""
from __future__ import annotations

import contextlib
import json
import re
import zlib
from dataclasses import replace
from pathlib import Path

import pytest

from modules import (
    cost_estimator,
    design_generator,
    input_handler,
    pipeline,
    report_generator,
    requirement_analyzer,
)
from modules import (
    genetic_optimizer as G,
)
from modules.models import SQFT_TO_SQM
from modules.pipeline import apply_budget_check, score_design


@pytest.fixture()
def req():
    return requirement_analyzer.analyze(
        input_handler.load(text="3 floor house in Pune"))


@pytest.fixture()
def parent(req):
    return design_generator.generate(req, seed=1)[0]


@pytest.fixture(autouse=True)
def _isolated_surrogate(tmp_path, monkeypatch):
    monkeypatch.setenv("SURROGATE_DIR", str(tmp_path))
    from modules import surrogate as S
    S.reset_cache()
    yield
    S.reset_cache()


def _grid_str(g):
    return G._grid_str(g)


def _grid_str_of(d: dict) -> str:
    """Grid string for a serialised design dict (summary results)."""
    return G._grid_str({"bays_x": d["bays_x"], "bay_x": d["bay_x_m"],
                        "bays_y": d["bays_y"], "bay_y": d["bay_y_m"]})


# ---------------------------------------------------------------------------
# genome / repair
# ---------------------------------------------------------------------------

def test_genome_roundtrip(parent):
    g = G.genome_from(parent)
    assert set(g) == {"bays_x", "bay_x", "bays_y", "bay_y"}
    assert g["bays_x"] == parent.bays_x
    assert g["bay_x"] == parent.bay_x_m


def test_repair_clamps_hard_bounds(parent, req):
    r = G.repair({"bays_x": 99.0, "bay_x": 42.0,
                  "bays_y": -3.0, "bay_y": 0.1}, parent, req)
    assert 2 <= r["bays_x"] <= 12 and 2 <= r["bays_y"] <= 12
    assert 3.0 <= r["bay_x"] <= 9.0 and 3.0 <= r["bay_y"] <= 9.0
    for bay in (r["bay_x"], r["bay_y"]):
        assert abs(bay / G.BAY_STEP - round(bay / G.BAY_STEP)) < 1e-9


def test_repair_respects_parent_length_window(parent, req):
    r = G.repair({"bays_x": 12.0, "bay_x": 9.0,
                  "bays_y": 2.0, "bay_y": 3.0}, parent, req)
    for axis, p_bays, p_bay in (("bays_x", parent.bays_x, parent.bay_x_m),
                                ("bays_y", parent.bays_y, parent.bay_y_m)):
        length = r[axis] * r[axis.replace("bays", "bay")]
        p_len = p_bays * p_bay
        assert 0.85 * p_len - 0.01 <= length <= 1.15 * p_len + 0.01, axis


def test_repair_keeps_coverage_within_70pct(parent, req):
    from modules.models import SQFT_TO_SQM
    assert parent.plate_sqft <= 0.70 * req.land_area_sqft      # precondition
    r = G.repair({"bays_x": 12.0, "bay_x": 9.0,
                  "bays_y": 12.0, "bay_y": 9.0}, parent, req)
    plate = r["bays_x"] * r["bay_x"] * r["bays_y"] * r["bay_y"] / SQFT_TO_SQM
    assert plate <= 0.70 * req.land_area_sqft + 1.0


def test_repair_never_raises_on_over_cap_parent(parent, req):
    over = replace(parent, bays_x=12, bay_x_m=9.0)
    r = G.repair({"bays_x": 2.0, "bay_x": 3.0,
                  "bays_y": 2.0, "bay_y": 3.0}, over, req)
    assert 2 <= r["bays_x"] <= 12


def test_repair_over_cap_child_never_grows(parent, req):
    # regression: over-cap parents previously skipped the coverage clamp
    # entirely, so children could compound the footprint up to +32%
    req.land_area_sqft = 100.0                    # tiny plot -> tiny cap
    over = replace(parent, bays_x=12, bay_x_m=9.0)
    assert over.plate_sqft > 0.70 * req.land_area_sqft      # precondition
    r = G.repair({"bays_x": 12.0, "bay_x": 9.0,
                  "bays_y": 12.0, "bay_y": 9.0}, over, req)
    plate = r["bays_x"] * r["bay_x"] * r["bays_y"] * r["bay_y"] / SQFT_TO_SQM
    assert plate <= over.plate_sqft + 1.0


# ---------------------------------------------------------------------------
# apply_genome / fitness
# ---------------------------------------------------------------------------

def test_apply_genome_derives_and_resets(parent, req):
    g = G.genome_from(parent)
    g["bay_x"] = 6.0 if abs(parent.bay_x_m - 6.0) > 0.06 else 7.5
    d = G.apply_genome(parent, g)
    assert d.bay_x_m == g["bay_x"]
    assert d.column_boost == 0 and d.footing_bump == 0
    assert d.footing_t_mm == 0
    panel = d.bay_x_m / 2 if d.secondary else d.bay_x_m
    assert d.slab_t_mm == design_generator._slab_t(panel)
    assert d.beam_d_mm == design_generator._beam_d(d.bay_x_m)


def test_fitness_matches_pipeline_score(parent, req):
    fa = G.fitness(parent, req)
    expect = score_design(G.analyze(parent, req),
                          cost_estimator.estimate(parent, req),
                          req.budget_crores)
    assert abs(fa - expect) < 1e-9


def test_fitness_applies_budget_check(parent, req):
    # regression (bug-hunt P1): GA fitness must include the budget check
    # or it ranks designs differently from the pipeline near the boundary
    req.budget_crores = 0.01                      # absurd -> guaranteed over
    cost = cost_estimator.estimate(parent, req)
    assert cost.get("within_budget") is False     # precondition
    bare = score_design(G.analyze(parent, req), cost, req.budget_crores)
    analysis = G.analyze(parent, req)
    apply_budget_check(analysis, cost, req)
    expected = score_design(analysis, cost, req.budget_crores)
    assert expected < bare                        # failed budget check counts
    assert abs(G.fitness(parent, req) - expected) < 1e-9


def test_fitness_exception_returns_zero(parent, req, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("x")
    monkeypatch.setattr(G, "analyze", boom)
    assert G.fitness(parent, req) == 0.0


# ---------------------------------------------------------------------------
# evolution
# ---------------------------------------------------------------------------

def test_evolve_deterministic_and_json_safe(parent, req):
    d1, s1 = G.evolve(parent, req, seed=42, use_surrogate=False,
                      pop=8, gens=4)
    d2, s2 = G.evolve(parent, req, seed=42, use_surrogate=False,
                      pop=8, gens=4)
    assert s1["best_grid"] == s2["best_grid"]
    assert s1["best_score"] == s2["best_score"]
    assert G.genome_from(d1) == G.genome_from(d2)
    assert json.loads(json.dumps(s1))["seed"] == 42


def test_evolve_never_worsens_parent(parent, req):
    _, s = G.evolve(parent, req, seed=7, use_surrogate=False,
                    pop=8, gens=3)
    assert s["best_score"] >= s["parent_score"] - 1e-6
    assert 0 < s["evaluations"] <= s["population"] * (s["generations_run"] + 1)
    for key in ("parent_grid", "best_grid", "surrogate", "elapsed_ms"):
        assert key in s
    # evolved design grid honours repair constraints
    lo_x = 0.85 * parent.bays_x * parent.bay_x_m
    hi_x = 1.15 * parent.bays_x * parent.bay_x_m
    d, _ = G.evolve(parent, req, seed=7, use_surrogate=False,
                    pop=8, gens=3)
    lx = d.bays_x * d.bay_x_m
    assert lo_x - 0.01 <= lx <= hi_x + 0.01


def test_evolve_with_surrogate_does_not_crash(parent, req):
    _, s = G.evolve(parent, req, seed=1, use_surrogate=True,
                    pop=6, gens=2)
    assert s["evaluations"] > 0
    assert s["surrogate"]["enabled"] is True
    assert "prescreened_generations" in s["surrogate"]


def test_evolve_terminates_when_genome_space_tiny(parent, req, monkeypatch):
    # regression: the initial-fill loop had no attempt bound - with the
    # reachable genome set smaller than pop it spun forever
    fixed = G.genome_from(parent)
    monkeypatch.setattr(G, "repair", lambda g, p, r: dict(fixed))
    _, s = G.evolve(parent, req, seed=3, use_surrogate=False,
                    pop=6, gens=2)
    assert s["evaluations"] <= 6                  # parent + unique set only
    assert s["generations_run"] >= 1


# ---------------------------------------------------------------------------
# pipeline wiring
# ---------------------------------------------------------------------------

def test_pipeline_genetic_on_adds_stats(tmp_path):
    summary = pipeline.run(text="3 floor house in Pune",
                           out_dir=tmp_path / "run",
                           make_pdf=False, make_images=False,
                           seed=1, genetic=True)
    g = summary["genetic"]
    assert g["enabled"] is True
    assert g["designs_searched"] == len(summary["results"])
    assert "surrogate" in g and "samples" in g["surrogate"]
    for r in summary["results"]:
        ga = r["genetic"]
        assert ga is not None and ga["enabled"] is True
        assert ga["evaluations"] > 0
        assert ga["parent_grid"] and ga["best_grid"]
        json.dumps(ga)                             # JSON-safe
        ga_fixes = [f for f in r["fixes"] if f["iteration"] == "GA"]
        if ga["improved"]:
            assert ga_fixes and ga_fixes[0]["issue"] == "Grid topology"
            assert ga_fixes[0]["before"] == ga["parent_grid"]
            # optimize() never changes the grid -> final grid is the best one
            assert _grid_str_of(r["design"]) == ga["best_grid"]
        else:
            assert not ga_fixes
            assert _grid_str_of(r["design"]) == ga["parent_grid"]


def test_pipeline_genetic_off(tmp_path):
    summary = pipeline.run(text="3 floor house in Pune",
                           out_dir=tmp_path / "run",
                           make_pdf=False, make_images=False,
                           seed=1, genetic=False)
    assert summary["genetic"] == {"enabled": False}
    for r in summary["results"]:
        assert r["genetic"] is None
        assert not [f for f in r["fixes"] if f["iteration"] == "GA"]


def test_cli_no_genetic_flag(tmp_path, capsys, monkeypatch):
    import config as cfg
    from main import main
    # production default (config ON) -> GA line must show...
    monkeypatch.setattr(cfg, "GENETIC_ENABLED", True)
    rc = main(["3 floor house in Pune", "--no-images", "--no-pdf",
               "--seed", "1", "--out", str(tmp_path / "run_on")])
    assert rc == 0
    assert "GA      :" in capsys.readouterr().out
    # ...and --no-genetic must actually suppress it
    rc = main(["3 floor house in Pune", "--no-genetic", "--no-images",
               "--no-pdf", "--seed", "1", "--out", str(tmp_path / "run_off")])
    assert rc == 0
    assert "GA      :" not in capsys.readouterr().out


def test_config_flag_respected_without_cli_override(tmp_path, capsys,
                                                    monkeypatch):
    # regression: main passed `genetic=not args.no_genetic` (always a bool)
    # so GENETIC_ENABLED=0 was dead for CLI runs without --no-genetic
    import config as cfg
    from main import main
    monkeypatch.setattr(cfg, "GENETIC_ENABLED", False)
    rc = main(["3 floor house in Pune", "--no-images", "--no-pdf",
               "--out", str(tmp_path / "run")])
    assert rc == 0
    assert "GA      :" not in capsys.readouterr().out


def test_pipeline_genetic_error_isolated(tmp_path, monkeypatch):
    # regression: an evolve() crash must degrade to an error record,
    # never kill the run (and never emit a fake GA fix row)
    def boom(*_a, **_k):
        raise RuntimeError("search exploded")
    monkeypatch.setattr(pipeline.genetic_optimizer, "evolve", boom)
    summary = pipeline.run(text="3 floor house in Pune",
                           out_dir=tmp_path / "run",
                           make_pdf=False, make_images=False,
                           seed=1, genetic=True)
    assert summary["genetic"]["enabled"] is True
    for r in summary["results"]:
        ga = r["genetic"]
        assert ga is not None and "error" in ga
        assert "search exploded" in ga["error"]
        assert not [f for f in r["fixes"] if f["iteration"] == "GA"]
    # JSON-safe error record
    json.dumps(summary["results"][0]["genetic"])


def _pdf_text(path: Path) -> str:
    """Decompress PDF content streams (reportlab/fpdf text lives there)."""
    out = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream",
                         path.read_bytes(), re.S):
        with contextlib.suppress(zlib.error):
            out.append(zlib.decompress(m.group(1)))
    return b" ".join(out).decode("latin-1", "ignore")


def test_report_renders_ga_section_and_error_stats(tmp_path):
    summary = pipeline.run(text="3 floor house in Pune",
                           out_dir=tmp_path / "run",
                           make_pdf=False, make_images=False,
                           seed=1, genetic=True)
    # 1. normal GA stats render in section 4
    pdf1 = report_generator.build_report(summary, {}, tmp_path / "r1.pdf")
    text1 = _pdf_text(pdf1)
    assert "Genetic grid search" in text1
    # 2. an evolve error record must not break the report build
    summary["results"][0]["genetic"] = {"enabled": True,
                                        "error": "RuntimeError: boom"}
    pdf2 = report_generator.build_report(summary, {}, tmp_path / "r2.pdf")
    text2 = _pdf_text(pdf2)
    assert "genetic search skipped" in text2
    assert "RuntimeError: boom" in text2
