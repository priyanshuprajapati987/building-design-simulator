"""Rule-based optimisation: read failed checks, apply engineering fixes, re-test.

Phase-1 stand-in for the AI optimiser (genetic/ML comes later). Each fix is a
deterministic, explainable engineering action; the design is re-analysed after
every fix (the re-test loop), and a fix log is kept for the report.
"""
from __future__ import annotations

import copy
from typing import Any

from .models import Requirements, Design
from .structural_analyzer import analyze

MAX_ITER = 5


def optimize(design: Design, req: Requirements) -> tuple[Design, dict, list[dict[str, Any]]]:
    """Returns (best_design, analysis, fix_log). Always re-tests after a fix."""
    d = copy.deepcopy(design)
    fix_log: list[dict[str, Any]] = []
    advisories: set[str] = set()
    analysis = analyze(d, req)

    for it in range(MAX_ITER):
        failed = [c for c in analysis["checks"] if not c["passed"]]
        actionable = None
        for c in failed:
            key = c["name"]
            if key in advisories:
                continue
            if ("utilisation" in key and "Column" in key) or "Column axial" in key:
                actionable = c
                break
            if "Beam moment" in key:
                actionable = c
                break
            if "Slab" in key:
                actionable = c
                break
            if "drift" in key.lower():
                actionable = c
                break
            # non-actionable (height, base-shear sanity band, OT) -> advisory
            advisories.add(key)

        if actionable is None:
            break

        name = actionable["name"]
        before = actionable["value"]

        if "Column axial" in name:
            d.column_boost += 1
            action = (f"column sections stepped up one size on the ladder "
                      f"(boost={d.column_boost})")
        elif "Beam moment" in name:
            d.beam_d_mm += 50
            if d.secondary:
                d.sec_beam_d_mm = max(d.sec_beam_d_mm + 50, d.beam_d_mm // 2)
            action = f"beam depth increased to {d.beam_w_mm} x {d.beam_d_mm} mm"
        elif "Slab moment" in name:
            d.slab_t_mm += 25
            action = f"slab thickness increased to {d.slab_t_mm} mm"
        elif "L/d" in name:
            d.slab_t_mm += 25
            action = f"slab thickened to {d.slab_t_mm} mm for serviceability"
        elif "drift" in name.lower():
            if not d.core:
                d.core = True
                d.system = "rc_dual"
                d.core_lx_m = max(4.0, round(min(0.35 * d.len_x_m, 10.0), 1))
                d.core_ly_m = max(3.5, round(min(0.40 * d.len_y_m, 8.0), 1))
                action = ("structural shear-wall core added at centre "
                          "(system upgraded to rc_dual)")
            elif d.wall_t_mm < 350:
                d.wall_t_mm += 50
                action = f"core wall thickness increased to {d.wall_t_mm} mm"
            else:
                advisories.add(name)
                fix_log.append({"iteration": it + 1, "issue": name,
                                "before": before, "action":
                                    "REVIEW: core walls at 350 mm - reduce bay "
                                    "spacing or increase concrete grade (Phase 2)"})
                break
        else:
            advisories.add(name)
            break

        fix_log.append({"iteration": it + 1, "issue": name,
                        "before": before, "action": action})
        analysis = analyze(d, req)

    remaining = [c["name"] for c in analysis["checks"] if not c["passed"]]
    if remaining:
        for r in sorted(set(remaining)):
            if not any(f["issue"] == r for f in fix_log):
                fix_log.append({"iteration": "-", "issue": r, "before": "-",
                                "action": "advisory: needs manual/site decision "
                                          "(not auto-fixable)"})
    return d, analysis, fix_log


def summarize_fixes(fix_log: list[dict[str, Any]]) -> str:
    if not fix_log:
        return "No optimisation needed - all checks passed on first analysis."
    return "; ".join(f"[{f['issue']}] {f['action']}" for f in fix_log)
