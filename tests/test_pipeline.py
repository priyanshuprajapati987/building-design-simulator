"""End-to-end pipeline, optimisation, cost and report tests."""
import json
from pathlib import Path

import pytest

from modules import (
    cost_estimator,
    design_generator,
    input_handler,
    optimization_engine,
    pipeline,
    requirement_analyzer,
)

BRIEF = ("Design a 10-floor residential building in Mumbai with 4 units per "
         "floor, budget 10 crore, parking and green roof")


# ---------------------------------------------------------------------------
# scoring / cost
# ---------------------------------------------------------------------------

def test_score_range():
    analysis = {"passed": 10, "total_checks": 10, "max_utilisation": 0.75,
                "drift": {"max_index": 0.001, "limit": 0.004}}
    cost = {"total_inr": 5e7, "budget_inr": 1e8, "within_budget": True}
    s = pipeline.score_design(analysis, cost, 10.0)
    assert 0 <= s <= 100
    # full pass + good util + within budget + good drift -> high score
    assert s > 80


def test_score_zero_for_failing_utilisation():
    analysis = {"passed": 0, "total_checks": 10, "max_utilisation": 1.5,
                "drift": {"max_index": 0.02, "limit": 0.004}}
    cost = {"total_inr": 2e8, "budget_inr": 1e8, "within_budget": False}
    s = pipeline.score_design(analysis, cost, 10.0)
    assert s < 45


def test_cost_estimate_shape_and_budget():
    req = requirement_analyzer.analyze(input_handler.load(text=BRIEF))
    d = design_generator.generate(req)[0]
    c = cost_estimator.estimate(d, req)
    assert c["total_inr"] > 0
    assert c["within_budget"] is not None
    assert set(c["breakdown_inr"]) == {"structure", "foundation", "finishes",
                                       "mep_services", "external_and_misc"}
    assert c["breakdown_inr"]["foundation"] > 0
    assert sum(c["breakdown_inr"].values()) == pytest.approx(
        c["total_inr"], rel=0.01)


def test_optimization_retests_after_fix():
    req = requirement_analyzer.analyze(
        input_handler.load(text="25 floor office in Delhi"))
    d = design_generator.generate(req)[0]
    best, analysis, fixes = optimization_engine.optimize(d, req)
    assert best is not d                     # always a re-tested copy
    # whatever happened, the returned analysis must be consistent
    assert analysis["passed"] <= analysis["total_checks"]
    assert isinstance(fixes, list)
    if fixes:
        assert any("iter" in str(f["iteration"]) or f["iteration"] == "-"
                   for f in fixes)


# ---------------------------------------------------------------------------
# full pipeline (tmp dir, images ON, pdf ON)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def summary(tmp_path_factory):
    out = tmp_path_factory.mktemp("run")
    return pipeline.run(text=BRIEF, out_dir=out, make_pdf=True,
                        make_images=True)


def test_three_results_ranked(summary):
    assert len(summary["results"]) == 3
    ranks = [r["rank"] for r in summary["results"]]
    assert sorted(ranks) == [1, 2, 3]
    assert summary["results"][0]["rank"] == 1
    assert summary["winner"]["id"] == summary["results"][0]["design"]["id"]


def test_every_design_has_costs_and_checks(summary):
    for r in summary["results"]:
        assert r["cost"]["total_inr"] > 0
        assert r["analysis"]["total_checks"] >= 8
        assert 0 <= r["score"] <= 100


def test_budget_check_injected_when_budget_given(summary):
    names = [c["name"] for c in summary["results"][0]["analysis"]["checks"]]
    assert "Budget compliance" in names


def test_outputs_written(summary):
    out = Path(summary["files"]["dir"])
    assert (out / "summary.json").exists()
    assert (out / "cost_comparison.png").exists()
    assert (out / "score_comparison.png").exists()
    for did in ("A", "B", "C"):
        assert (out / f"design_{did}_plan.png").exists()
        assert (out / f"design_{did}_elevation.png").exists()
        assert (out / f"design_{did}_seismic.png").exists()
        assert (out / f"design_{did}_3d.png").exists()
        assert (out / f"design_{did}_3d.html").exists()


def test_pdf_is_real_pdf(summary):
    pdf = Path(summary["files"].get("pdf", ""))
    assert pdf.exists(), summary.get("pdf_error", "no pdf path")
    head = pdf.read_bytes()[:5]
    assert head == b"%PDF-"
    assert pdf.stat().st_size > 20_000


def test_summary_json_roundtrip(summary):
    raw = json.dumps(summary, default=str)
    back = json.loads(raw)
    assert back["requirements"]["city"].lower() == "mumbai"
    assert back["disclaimer"]


def test_pipeline_without_images_or_pdf(tmp_path):
    s = pipeline.run(text="3 floor house in Pune", out_dir=tmp_path,
                     make_pdf=False, make_images=False)
    assert len(s["results"]) == 3
    assert (tmp_path / "summary.json").exists()
    assert not (tmp_path / "report.pdf").exists()


def test_pipeline_structured_input(tmp_path):
    s = pipeline.run(data={"building_type": "warehouse",
                           "city": "Ahmedabad",
                           "floors": 2,
                           "land_area_sqft": 20000},
                     out_dir=tmp_path, make_pdf=False, make_images=False)
    assert s["requirements"]["building_type"] == "warehouse"
    assert s["results"][0]["analysis"]["seismic"]["Z"] == 0.16
