"""Phase 3A: genetic search over the structural grid.

Evolves (bays_x, bay_x, bays_y, bay_y) for a generated alternative so the
grid topology itself is optimised - the Phase-1/2 ladder only escalates
member sizes, it never questions the grid. Fitness is the same hand-analysis
score the pipeline ranks on (compliance + efficiency + cost + drift, no FEA -
the real FEA/fix loop still runs afterwards on whatever grid wins), so one
search eval costs milliseconds.

Search mechanics:
  * genome repair enforces hard bounds (2-12 bays, 3.0-9.0 m bay at 0.05 m
    steps) + an [0.85, 1.15] length window per axis vs the parent grid, and
    keeps the footprint inside the 70% plot-coverage norm when the parent
    already is - infeasible children fall back to the parent genome
  * elitism + tournament-3 selection + uniform crossover + bay/bay-count
    mutation, deterministic for a given seed
  * surrogate assistance (modules/surrogate.py): from generation 1 the
    offspring pool is doubled and the ML surrogate pre-screens it, so only
    the predicted-best half is really analysed; with no trained surrogate
    every candidate is evaluated honestly
  * adaptive budget: 10x6 generations normally, 6x4 for very large models

The GA never beats honesty: scores come from the real structural_analyzer,
results are reported as "hand score" and the FEA verification still runs on
the final design inside the existing optimise/re-test loop.
"""
from __future__ import annotations

import contextlib
import random
import time
from dataclasses import replace
from typing import Any

from . import cost_estimator, design_generator, surrogate
from .models import SQFT_TO_SQM, Design, Requirements
from .structural_analyzer import analyze

BAY_MIN = 3.0
BAY_MAX = 9.0
BAY_STEP = 0.05
BAYS_MIN = 2
BAYS_MAX = 12
AXIS_LO = 0.85                    # child axis length vs parent
AXIS_HI = 1.15
COVERAGE_MAX = 0.70               # same norm the pipeline warns on
_ELITE = 2
_TOURNAMENT = 3
_BIG_MODEL = 3000                  # floors * columns -> smaller budget

Genome = dict[str, float]


# ---------------------------------------------------------------------------
# genome <-> design
# ---------------------------------------------------------------------------

def _snap(v: float) -> float:
    return round(round(v / BAY_STEP) * BAY_STEP, 2)


def genome_from(d: Design) -> Genome:
    return {"bays_x": float(d.bays_x), "bay_x": d.bay_x_m,
            "bays_y": float(d.bays_y), "bay_y": d.bay_y_m}


def _key(g: Genome) -> tuple:
    return (g["bays_x"], g["bays_y"], g["bay_x"], g["bay_y"])


def _repair_axis(bays: float, bay: float, p_bays: int, p_bay: float
                 ) -> tuple[int, float]:
    """Clamp one axis into hard bounds + the parent length window."""
    bays_i = int(min(max(round(bays), BAYS_MIN), BAYS_MAX))
    bay_s = _snap(min(max(bay, BAY_MIN), BAY_MAX))
    p_len = p_bays * p_bay
    lo, hi = AXIS_LO * p_len, AXIS_HI * p_len
    for _ in range(6):
        length = bays_i * bay_s
        if lo - 1e-9 <= length <= hi + 1e-9:
            return bays_i, bay_s
        target = min(max(length, lo), hi)
        bay_s = _snap(min(max(target / bays_i, BAY_MIN), BAY_MAX))
        length = bays_i * bay_s
        if not (lo - 1e-9 <= length <= hi + 1e-9):
            bays_i = int(min(max(round(target / bay_s), BAYS_MIN), BAYS_MAX))
            bay_s = _snap(min(max(target / bays_i, BAY_MIN), BAY_MAX))
    # bounds fight (window not representable) -> parent axis is feasible
    return p_bays, p_bay


def repair(g: Genome, parent: Design, req: Requirements) -> Genome:
    """Force a genome into every hard/soft constraint (never raises)."""
    bays_x, bay_x = _repair_axis(g.get("bays_x", parent.bays_x),
                                 g.get("bay_x", parent.bay_x_m),
                                 parent.bays_x, parent.bay_x_m)
    bays_y, bay_y = _repair_axis(g.get("bays_y", parent.bays_y),
                                 g.get("bay_y", parent.bay_y_m),
                                 parent.bays_y, parent.bay_y_m)
    if req.land_area_sqft:
        cap_sqft = COVERAGE_MAX * req.land_area_sqft
        if parent.plate_sqft <= cap_sqft:      # never worsen an over-cap parent
            plate = bays_x * bay_x * bays_y * bay_y / SQFT_TO_SQM
            if plate > cap_sqft + 1e-6:
                f = (cap_sqft / plate) ** 0.5
                bay_x = _snap(max(BAY_MIN, bay_x * f))
                bay_y = _snap(max(BAY_MIN, bay_y * f))
                bays_x, bay_x = _repair_axis(bays_x, bay_x,
                                             parent.bays_x, parent.bay_x_m)
                bays_y, bay_y = _repair_axis(bays_y, bay_y,
                                             parent.bays_y, parent.bay_y_m)
                plate = bays_x * bay_x * bays_y * bay_y / SQFT_TO_SQM
                if plate > cap_sqft + 1e-6:
                    return genome_from(parent)     # honest fallback
    return {"bays_x": float(bays_x), "bay_x": bay_x,
            "bays_y": float(bays_y), "bay_y": bay_y}


def apply_genome(parent: Design, g: Genome) -> Design:
    """Inherit the parent, swap the grid, re-derive grid-driven sizes
    (slab panel, beam depths, core dims) exactly like the generator does,
    and reset the optimiser ladder state (the fix loop re-applies it)."""
    d = replace(parent,
                bays_x=int(g["bays_x"]), bays_y=int(g["bays_y"]),
                bay_x_m=float(g["bay_x"]), bay_y_m=float(g["bay_y"]),
                column_boost=0, footing_bump=0, footing_t_mm=0)
    panel = d.bay_x_m / 2 if d.secondary else d.bay_x_m
    d.slab_t_mm = design_generator._slab_t(panel)
    d.beam_d_mm = design_generator._beam_d(d.bay_x_m)
    if d.secondary:
        d.sec_beam_d_mm = design_generator._beam_d(d.bay_x_m / 2)
    if d.core:
        d.core_lx_m = max(round(min(0.35 * d.len_x_m, 10.0), 1), 4.0)
        d.core_ly_m = max(round(min(0.40 * d.len_y_m, 8.0), 1), 3.5)
    return d


# ---------------------------------------------------------------------------
# fitness (hand analysis only - lazy imports, pipeline imports this module)
# ---------------------------------------------------------------------------

def fitness(d: Design, req: Requirements) -> float:
    """Score a candidate exactly like the pipeline ranks designs.

    No FEA (too slow for a search loop) - the winning grid goes through the
    full optimise/re-test loop afterwards anyway. Errors -> 0.0 so one bad
    genome can never kill a run."""
    try:
        analysis = analyze(d, req)
        cost = cost_estimator.estimate(d, req)
        from .pipeline import score_design
        return float(score_design(analysis, cost, req.budget_crores))
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# genetic operators
# ---------------------------------------------------------------------------

def _mutate(rng: random.Random, g: Genome) -> Genome:
    out = dict(g)
    if rng.random() < 0.5:
        out["bay_x"] = _snap(out["bay_x"]
                             + rng.choice((-1, 1)) * rng.randint(1, 12) * BAY_STEP)
    if rng.random() < 0.3:
        out["bays_x"] += rng.choice((-1, 1))
    if rng.random() < 0.5:
        out["bay_y"] = _snap(out["bay_y"]
                             + rng.choice((-1, 1)) * rng.randint(1, 12) * BAY_STEP)
    if rng.random() < 0.3:
        out["bays_y"] += rng.choice((-1, 1))
    return out


def _crossover(rng: random.Random, a: Genome, b: Genome) -> Genome:
    """Uniform crossover per axis (bay count + bay size travel together)."""
    take_a_x = rng.random() < 0.5
    return {
        "bays_x": a["bays_x"] if take_a_x else b["bays_x"],
        "bay_x": a["bay_x"] if take_a_x else b["bay_x"],
        "bays_y": a["bays_y"] if not take_a_x else b["bays_y"],
        "bay_y": a["bay_y"] if not take_a_x else b["bay_y"],
    }


def _tournament(rng: random.Random, members: list[Genome],
                scores: dict[tuple, float]) -> Genome:
    picks = rng.sample(members, min(_TOURNAMENT, len(members)))
    return max(picks, key=lambda g: scores[_key(g)])


def _grid_str(g: Genome) -> str:
    lx = g["bays_x"] * g["bay_x"]
    ly = g["bays_y"] * g["bay_y"]
    return (f"{int(g['bays_x'])} x {g['bay_x']:.2f} m / "
            f"{int(g['bays_y'])} x {g['bay_y']:.2f} m "
            f"(L {lx:.1f} x {ly:.1f} m)")


# ---------------------------------------------------------------------------
# evolution loop
# ---------------------------------------------------------------------------

def evolve(parent: Design, req: Requirements, *, seed: int = 0,
           use_surrogate: bool = True, pop: int | None = None,
           gens: int | None = None) -> tuple[Design, dict[str, Any]]:
    """Search the grid around the parent design.

    Returns (best_design, stats) - stats is JSON-safe and lands verbatim in
    the pipeline result under the ``"genetic"`` key. Deterministic for a
    given seed when use_surrogate=False (surrogate pre-screening intentionally
    improves with the growing dataset)."""
    t0 = time.perf_counter()
    big = parent.floors * parent.n_columns > _BIG_MODEL
    pop = pop or (6 if big else 10)
    gens = gens or (4 if big else 6)
    pop = max(pop, _ELITE + 2)
    rng = random.Random(seed)

    evaluated: dict[tuple, float] = {}
    evals = 0

    def evaluate(d: Design, g: Genome) -> float:
        nonlocal evals
        k = _key(g)
        if k in evaluated:
            return evaluated[k]
        sc = fitness(d, req)
        evaluated[k] = sc
        with contextlib.suppress(OSError):
            # dataset dir unavailable: GA still runs, just no learning
            surrogate.record(surrogate.featurize(d, req), sc)
        evals += 1
        return sc

    parent_g = genome_from(parent)
    parent_score = evaluate(apply_genome(parent, parent_g), parent_g)

    members: list[Genome] = [parent_g]
    seen = {_key(parent_g)}
    while len(members) < pop:
        child = repair(_mutate(rng, parent_g), parent, req)
        if _key(child) not in seen:
            members.append(child)
            seen.add(_key(child))
    scores = dict.fromkeys(seen, 0.0)
    for g in members:
        scores[_key(g)] = evaluate(apply_genome(parent, g), g)

    best_score = max(scores.values())
    generations_run = 0
    prescreened = 0
    stagnant = 0

    for _gen in range(1, gens + 1):
        ranked = sorted(members, key=lambda g: scores[_key(g)], reverse=True)
        elite = ranked[:_ELITE]
        want = pop - len(elite)

        # surrogate pre-screen: double the pool, really analyse the best half
        sur = surrogate.get_surrogate() if use_surrogate else None
        pool_n = want * (2 if sur else 1)
        pool: list[Genome] = []
        pool_seen: set[tuple] = {_key(g) for g in elite}
        attempts = 0
        while len(pool) < pool_n and attempts < pool_n * 8:
            attempts += 1
            child = repair(_mutate(rng, _crossover(rng,
                          _tournament(rng, members, scores),
                          _tournament(rng, members, scores))),
                          parent, req)
            k = _key(child)
            if k not in pool_seen:
                pool.append(child)
                pool_seen.add(k)

        if sur is not None and len(pool) > want:
            preds = []
            for child in pool:
                try:
                    p = sur.predict(surrogate.featurize(
                        apply_genome(parent, child), req))
                except (ValueError, KeyError):
                    p = -1.0
                preds.append((p, child))
            preds.sort(key=lambda t: -t[0])
            chosen = [c for _, c in preds[:want]]
            prescreened += 1
        else:
            chosen = pool[:want]

        for child in chosen:
            scores[_key(child)] = evaluate(apply_genome(parent, child), child)
            if _key(child) not in seen:
                members.append(child)
                seen.add(_key(child))

        # keep population size: refill with fresh mutants if crossover deduped
        guard = 0
        while len(members) < pop and guard < pop * 4:
            guard += 1
            child = repair(_mutate(rng, parent_g), parent, req)
            if _key(child) not in seen:
                members.append(child)
                seen.add(_key(child))
                scores[_key(child)] = evaluate(apply_genome(parent, child),
                                               child)

        gen_best = max(scores[_key(g)] for g in members)
        generations_run = _gen
        if gen_best > best_score + 1e-9:
            best_score = gen_best
            stagnant = 0
        else:
            stagnant += 1
            if stagnant >= 3:              # early stop: no progress 3 gens
                break

    best_key = max(evaluated, key=lambda k: evaluated[k])
    best_g: Genome = {"bays_x": best_key[0], "bays_y": best_key[1],
                      "bay_x": best_key[2], "bay_y": best_key[3]}
    stats: dict[str, Any] = {
        "enabled": True,
        "seed": seed,
        "population": pop,
        "generations_run": generations_run,
        "evaluations": evals,
        "parent_score": round(parent_score, 2),
        "best_score": round(best_score, 2),
        "improved": bool(best_score > parent_score + 0.05),
        "parent_grid": _grid_str(parent_g),
        "best_grid": _grid_str(best_g),
        "surrogate": {
            "enabled": use_surrogate,
            "prescreened_generations": prescreened,
        },
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
    }
    sur = surrogate.stats() if use_surrogate else None
    if sur:
        stats["surrogate"].update({
            "trained": sur["trained"],
            "r2_train": sur["r2_train"],
            "samples": sur["samples"],
        })
    return apply_genome(parent, best_g), stats
