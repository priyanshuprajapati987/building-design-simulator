"""End-to-end run: requirements -> 3 designs -> optimise -> cost -> score ->
images -> JSON summary -> PDF report."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import config as cfg

from . import (
    cost_estimator,
    design_generator,
    input_handler,
    optimization_engine,
    report_generator,
    requirement_analyzer,
    visualization,
)
from .models import Design, Requirements

# ---------------------------------------------------------------------------
# scoring / ranking
# ---------------------------------------------------------------------------

def score_design(analysis: dict, cost: dict, budget_cr: float | None) -> float:
    """0-100: 40 code compliance + 25 member efficiency + 20 cost + 15 drift."""
    comp = 40.0 * analysis["passed"] / max(analysis["total_checks"], 1)

    u = analysis["max_utilisation"]
    if u > 1.0:
        s_u = 0.0
    else:
        s_u = 25.0 * max(0.0, 1.0 - abs(u - 0.75) / 0.75)

    if budget_cr is None or cost.get("budget_inr") is None:
        s_c = 15.0
    else:
        ratio = cost["total_inr"] / cost["budget_inr"]
        if ratio <= 1.0:
            s_c = 10.0 + 10.0 * (1.0 - ratio)
        else:
            s_c = max(0.0, 10.0 - 25.0 * (ratio - 1.0))

    drift = analysis["drift"]["max_index"]
    limit = analysis["drift"]["limit"]
    s_d = 15.0 * max(0.0, 1.0 - drift / limit)

    return round(comp + s_u + s_c + s_d, 1)


# ---------------------------------------------------------------------------
# main entry
# ---------------------------------------------------------------------------

def run(text: str | None = None,
        data: dict | None = None,
        out_dir: str | Path | None = None,
        make_pdf: bool = True,
        make_images: bool = True,
        seed: int | None = None) -> dict[str, Any]:
    """Run the full pipeline. Returns the summary dict (also saved as JSON)."""
    req: Requirements = input_handler.load(text, data)
    requirement_analyzer.analyze(req)

    designs: list[Design] = design_generator.generate(req, seed=seed)

    # ---- footprint sanity warnings (post-grid-generation) ------------------
    if req.land_area_sqft:
        plate_max = max(d.plate_sqft for d in designs)
        if plate_max > 0.70 * req.land_area_sqft:
            req.warnings.append(
                f"generated footprint {plate_max:.0f} sqft exceeds 70% of the "
                f"{req.land_area_sqft:.0f} sqft plot - reduce units/coverage "
                f"or increase plot area")
    if req.building_type == "residential" and req.units_per_floor:
        codes = cfg.get_codes()
        unit_area = codes["unit_areas_sqft"]["residential_default"]
        implied = req.units_per_floor * unit_area * 0.85
        actual = max(d.plate_sqft for d in designs)
        if actual < 0.70 * implied:
            req.warnings.append(
                f"footprint {actual:.0f} sqft is below the ~{implied:.0f} sqft "
                f"implied by {req.units_per_floor} units/floor - grid is "
                f"capped at 12 bays (Phase-1 limit)")

    results: list[dict[str, Any]] = []
    for d in designs:
        # optimize() also runs the Phase-2 OpenSees FEA verification and
        # appends its 4 checks (analysis["fea"] carries the raw result)
        opt_d, analysis, fixes = optimization_engine.optimize(d, req)
        cost = cost_estimator.estimate(opt_d, req)

        # budget becomes an explicit check so it feeds the compliance score
        if cost.get("within_budget") is not None:
            ok = cost["within_budget"]
            analysis["checks"].append({
                "name": "Budget compliance",
                "value": cost["total_crores"],
                "unit": "Cr",
                "limit": f"<= {req.budget_crores:g} Cr",
                "passed": bool(ok),
                "detail": (f"used {cost['total_inr'] / cost['budget_inr']:.0%} of budget"
                           if ok else
                           f"over by Rs. {(cost['total_inr'] - cost['budget_inr']) / 1e7:.2f} Cr"),
            })
            analysis["passed"] = sum(1 for c in analysis["checks"] if c["passed"])
            analysis["total_checks"] = len(analysis["checks"])

        score = score_design(analysis, cost, req.budget_crores)
        results.append({"design": opt_d, "analysis": analysis, "cost": cost,
                        "fixes": fixes, "score": score})

    results.sort(key=lambda r: (-r["score"], r["cost"]["total_inr"]))
    for i, r in enumerate(results, start=1):
        r["rank"] = i

    # ---- output dir --------------------------------------------------------
    if out_dir is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = cfg.OUTPUT_DIR / f"run_{stamp}"
        i = 1
        while out.exists():                      # same-second rerun collision
            out = cfg.OUTPUT_DIR / f"run_{stamp}_{i}"
            i += 1
    else:
        out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # ---- images ------------------------------------------------------------
    images: dict[str, Any] = {}
    if make_images:
        for r in results:
            d = r["design"]
            aid = d.id
            images[aid] = {
                "plan": visualization.floor_plan_png(
                    d, out / f"design_{aid}_plan.png",
                    foundation=r["analysis"].get("foundation")),
                "elevation": visualization.elevation_png(d, out / f"design_{aid}_elevation.png"),
                "seismic": visualization.seismic_chart_png(
                    d, r["analysis"], out / f"design_{aid}_seismic.png"),
                "three_d": visualization.three_d_png(d, out / f"design_{aid}_3d.png"),
                "three_d_html": visualization.three_d_html(d, out / f"design_{aid}_3d.html"),
            }
        images["cost"] = visualization.cost_chart_png(results, out / "cost_comparison.png")
        images["score"] = visualization.score_chart_png(results, out / "score_comparison.png")

    # ---- serialisable summary ---------------------------------------------
    ser_results = []
    for r in results:
        dd = r["design"].to_dict()
        dd["description"] = r["design"].notes
        ser_results.append({
            "design": dd,
            "analysis": r["analysis"],
            "cost": r["cost"],
            "fixes": r["fixes"],
            "score": r["score"],
            "rank": r["rank"],
        })

    summary: dict[str, Any] = {
        "app": cfg.APP_NAME,
        "version": cfg.VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "disclaimer": cfg.DISCLAIMER,
        "requirements": req.to_dict(),
        "results": ser_results,
        "winner": {"id": results[0]["design"].id,
                   "name": results[0]["design"].name,
                   "score": results[0]["score"]},
    }

    # ---- PDF ---------------------------------------------------------------
    pdf_path = None
    if make_pdf:
        try:
            pdf_path = report_generator.build_report(
                summary, images, out / "report.pdf")
            summary["files"] = {"pdf": str(pdf_path)}
        except Exception as exc:                    # PDF must never kill the run
            summary["files"] = {}
            summary["pdf_error"] = f"{type(exc).__name__}: {exc}"

    file_list = [str(p) for p in sorted(out.glob("*"))]
    summary.setdefault("files", {})
    summary["files"]["dir"] = str(out)
    summary["files"]["all"] = file_list

    (out / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8")
    return summary
