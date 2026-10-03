"""Rule-based optimisation: read failed checks, apply engineering fixes, re-test.

Phase-1 stand-in for the AI optimiser (genetic/ML comes later). Each fix is a
deterministic, explainable engineering action; the design is re-analysed after
every fix (the re-test loop), and a fix log is kept for the report.

Fixes are applied round-robin: every actionable issue gets at least one fix
attempt before any issue repeats, so a stubborn column check can no longer
starve the beam/slab fixes of their iterations. Size ladders/caps degrade to
an honest advisory when they cannot fix the issue (instead of logging fake
"stepped up" fixes forever).
"""
from __future__ import annotations

import copy
from typing import Any

from .models import Design, Requirements
from .structural_analyzer import _COLUMN_LADDER, analyze

MAX_ITER = 8

_BEAM_MAX_D = 1200   # beyond this a RC beam is uneconomical -> advisory
_SLAB_MAX_T = 350    # beyond this a RC slab is uneconomical -> advisory

# genuinely non-auto-fixable issues -> honest advisory text
_ADVISE_TEXT = {
    "Budget compliance": "reduce scope/finishes or raise the budget",
    "Building height": "beyond Phase-1 scope - dynamic analysis required "
                       "(IS 1893) + system/zone height limits",
}


def _kind(name: str) -> str | None:
    """Map a failed check name to its fix kind (or None if not actionable)."""
    if "Column axial" in name or ("utilisation" in name and "Column" in name):
        return "column"
    if "Beam moment" in name:
        return "beam"
    if "Slab" in name:
        return "slab"
    if "drift" in name.lower():
        return "drift"
    return None


def optimize(design: Design, req: Requirements) -> tuple[Design, dict, list[dict[str, Any]]]:
    """Returns (best_design, analysis, fix_log). Always re-tests after a fix."""
    d = copy.deepcopy(design)
    fix_log: list[dict[str, Any]] = []
    advisories: set[str] = set()      # issues that can no longer be auto-fixed
    attempts: dict[str, int] = {}     # fix-attempts per issue (fair rotation)
    analysis = analyze(d, req)

    def log(it: int, name: str, before: Any, action: str) -> None:
        fix_log.append({"iteration": it + 1, "issue": name,
                        "before": before, "action": action})

    for it in range(MAX_ITER):
        failed = [c for c in analysis["checks"] if not c["passed"]]

        # non-actionable failures -> advisory (recorded once)
        for c in failed:
            if _kind(c["name"]) is None and c["name"] not in advisories:
                advisories.add(c["name"])

        actionable = [(i, c) for i, c in enumerate(failed)
                      if c["name"] not in advisories]
        if not actionable:
            break

        # least-attempted issue first (fair rotation), ties -> original order
        _, chosen = min(actionable,
                        key=lambda t: (attempts.get(t[1]["name"], 0), t[0]))
        name, kind = chosen["name"], _kind(chosen["name"])
        before = chosen["value"]
        attempts[name] = attempts.get(name, 0) + 1

        if kind == "column":
            if d.column_boost >= len(_COLUMN_LADDER) - 1:
                advisories.add(name)
                log(it, name, before,
                    "advisory: column size ladder exhausted - reduce "
                    "loads/spans or switch to a core/outrigger system "
                    "(Phase 2)")
                continue
            d.column_boost += 1
            action = (f"column sections stepped up one size on the ladder "
                      f"(boost={d.column_boost})")
        elif kind == "beam":
            if d.beam_d_mm >= _BEAM_MAX_D:
                advisories.add(name)
                log(it, name, before,
                    f"advisory: beam depth at {_BEAM_MAX_D} mm ceiling - "
                    f"reduce spans or increase concrete grade (Phase 2)")
                continue
            d.beam_d_mm += 50
            if d.secondary:
                d.sec_beam_d_mm = max(d.sec_beam_d_mm + 50, d.beam_d_mm // 2)
            action = f"beam depth increased to {d.beam_w_mm} x {d.beam_d_mm} mm"
        elif kind == "slab":
            if d.slab_t_mm >= _SLAB_MAX_T:
                advisories.add(name)
                log(it, name, before,
                    f"advisory: slab at {_SLAB_MAX_T} mm ceiling - reduce "
                    f"span or add drops (Phase 2)")
                continue
            d.slab_t_mm += 25
            action = f"slab thickness increased to {d.slab_t_mm} mm"
        elif not d.core:
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
            log(it, name, before,
                "REVIEW: core walls at 350 mm - reduce bay spacing or "
                "increase concrete grade (Phase 2)")
            continue

        log(it, name, before, action)
        analysis = analyze(d, req)

    # ---- honest wrap-up for whatever still fails ---------------------------
    for c in analysis["checks"]:
        if c["passed"]:
            continue
        r = c["name"]
        if any(f["issue"] == r and "advisory" in str(f["action"]).lower()
               for f in fix_log):
            continue                      # already advised exactly once
        attempts = sum(1 for f in fix_log if f["issue"] == r)
        if attempts == 0:
            action = "advisory: " + _ADVISE_TEXT.get(
                r, "needs manual/site decision (not auto-fixable)")
        elif r == "Budget compliance":
            action = "advisory: " + _ADVISE_TEXT["Budget compliance"]
        else:
            action = (f"advisory: still failing after {attempts} fix "
                      f"attempt(s) - manual review required (Phase 2)")
        fix_log.append({"iteration": "-", "issue": r, "before": "-",
                        "action": action})
    return d, analysis, fix_log


def summarize_fixes(fix_log: list[dict[str, Any]]) -> str:
    if not fix_log:
        return "No optimisation needed - all checks passed on first analysis."
    return "; ".join(f"[{f['issue']}] {f['action']}" for f in fix_log)
