# Building Design Simulator

**AI + parametric preliminary building design** — give it a plain-English brief
(or a form), get **3 structural design alternatives**, **IS-code checks**,
**auto-optimisation**, **city-wise cost estimates**, **2D drawings + interactive
3D**, and a **PDF report** — all from your own machine.

> **PRELIMINARY DESIGN ONLY - not for construction.** Every number here comes
> from documented code-based approximations for early-stage comparison. Final
> design must be checked and signed by a licensed structural engineer.

---

## What it does (Phase 1 - shipped)

| Step | What happens |
|------|--------------|
| 1. Input | Free text / voice transcript / structured JSON / sidebar form |
| 2. Requirements | City → seismic zone (IS 1893), wind speed + terrain (IS 875), defaults, sanity warnings |
| 3. Generate | **3 parametric alternatives**: A Compact RC Frame, B Core Shear-Wall Dual, C Open-Plan Ductile Frame |
| 4. Code checks | Seismic (equivalent static), wind, columns/beams/slab capacity + drift + overturning (IS 1893 / IS 875 / IS 456) |
| 5. Optimise | Rule-based re-test loop: bigger columns / deeper beams / thicker slab / add core - then re-analyse |
| 6. Cost | City rate (₹/sqft) × system + zone + soil factors, breakdown structure/finishes/MEP/other, budget check |
| 7. Score & rank | 40% compliance + 25% member efficiency + 20% cost + 15% drift margin |
| 8. Output | Floor plan, elevation, seismic + cost + score charts, **interactive 3D (HTML)**, `summary.json`, **PDF report** |

## Phase 2 (shipped) - OpenSeesPy FEA verification

Every design is additionally solved with a **3D elastic frame FEA**
(OpenSeesPy, linear static) *inside the same optimise/re-test loop*, so FEA
failures are auto-fixed too - not just reported:

| FEA check | Limit | What it proves |
|-----------|-------|----------------|
| Base-shear equilibrium | ≤ 5 % | element-summed base reactions match applied V (model integrity) |
| Inter-story drift index | ≤ 0.004 | independent drift cross-check vs the hand portal model |
| Beam moment utilisation | ≤ 1.0 | governing **LC1/LC2/LC3** factored end moments vs `0.136 fck b d²` |
| Column P-M interaction | ≤ 1.0 | `P/cap + max(My/μy, Mz/μz)` on every column, worst combo governs |

- Load cases per design: **D** (service dead) and **L** (service live,
  one-way slab → X-beam work-equivalent nodal loads), **Ex** and **Ey**
  (IS 1893 equivalent-static storey forces) - each on a fresh wiped model
- Member forces are combined with the **explicit IS 456 / IS 1893 load
  combinations** - LC1 `1.5(D+L)`, LC2 `1.2(D+L+EQ)` evaluated per EQ
  direction, LC3 `0.9D + 1.2EQ` - the worst combo governs each member
  (never a fixed `1.5 × service` envelope)
- Equilibrium is checked both ways: lateral base reaction vs applied V
  **and** vertical dead reaction vs applied ΣD
- Optimiser escalates on FEA failure: deeper beams → column size ladder →
  shear-wall core (rc_dual) → honest advisory when ladders are exhausted
- Engine quirks handled: `Umfpack`+`RCM` solver, no `ops.remove('load')`,
  broken `eleLoad`/`nodeReaction` (global-force slot extraction verified by
  probe models)
- Toggle with `FEA_ENABLED` in `config.py`; models above `MAX_NODES`
  (12 000 nodes) skip gracefully - the pipeline never blocks
- `tests/test_fea.py` covers availability/skip paths, JSON safety,
  sanity bounds, determinism and the 4-check wiring

## Phase 2b (shipped) - spread footings + load combinations

- **IS 456 screening-level spread footings** (`modules/foundation.py`) at
  corner / edge / interior column positions, sized from allowable bearing
  pressure (self-weight iteration), depth auto-derived from one-way shear
  and punching shear (`τ = V/(u·d)`), moment checked at the column face -
  every result a utilisation ≤ 1.00
- Governing per-position column loads from **LC1/LC2/LC3** (seismic axial
  from a rigid-couple distribution of the base overturning moment) feed the
  footings; the analysis carries an `analysis["foundation"]` block and the
  optimiser escalates on failure: enlarge base (+100 mm bumps) → thicken
  footing → honest advisory at the code ceiling (mat/raft for soft soil)
- **Explicit load-combination tables** in `data/codes.json`
  (`load_combos`), printed in the PDF alongside the **spread footing
  schedule**; cost breakdown gains a **Foundation (substructure)** row and
  the floor-plan PNG draws the footing squares
- Preliminary only - actual SBC governs from a geotechnical
  investigation; the report states this on every page

## Phase 3A (shipped) - genetic grid search + ML surrogate

Phase 1/2 only escalates member sizes - the **grid topology** (bay counts ×
bay sizes) was fixed at generation. Phase 3A searches it before the
optimise/re-test loop runs:

- **Genetic search** (`modules/genetic_optimizer.py`) evolves
  `(bays_x, bay_x, bays_y, bay_y)` around each generated alternative:
  elitism + tournament-3 + uniform crossover + bay/bay-count mutation,
  seed-deterministic, adaptive budget (10×6 normally, 6×4 for very large
  models), early stop after 3 stagnant generations
- **Genome repair** enforces every hard/soft constraint: 2-12 bays,
  3.0-9.0 m bay at 0.05 m steps, ±15% length window per axis vs the
  parent grid, footprint inside the 70% plot-coverage norm (infeasible
  children fall back to the parent genome - no silent constraint breaks)
- **Fitness = the same hand-analysis score the pipeline ranks on**
  (compliance + efficiency + cost + drift, **including the budget-compliance
  check** so search ranking is identical to the final ranking) - milliseconds
  per candidate, **no FEA inside the search**; the winning grid still goes
  through the full optimise/re-test loop with FEA verification afterwards.
  Results are reported as *hand score* in the stats, never as final score
- **ML surrogate pre-screening** (`modules/surrogate.py`): pure-Python
  ridge regression (19 features: grid, sizes, system, zone, soil, budget...)
  trained online from every evaluated design (append-only dataset at
  `output/surrogate/dataset.jsonl`, survives across runs). From generation 1
  the offspring pool is doubled and only the predicted-best half is really
  analysed. Below 15 samples it refuses to predict (`predict() -> None`) and
  every candidate is evaluated honestly; the report always quotes the
  in-sample R² next to predictions - it is a ranking aid, not a score
- **Wiring**: `pipeline.run(..., genetic=True/False)`; summary carries a
  top-level `genetic` block (search stats + surrogate status), every result
  a per-design `genetic` dict, improvements appear as a `GA` entry at the
  top of the optimisation log, and the PDF report gains a
  "Genetic grid search (Phase 3A)" subsection in *4. Optimisation summary*
- **Toggles**: default ON - `--no-genetic` on the CLI or
  `GENETIC_ENABLED=0` in `config.py`/env. On tall structures the re-test
  loop re-verifies the new grid with FEA, so runs can take a little longer;
  `FEA_ENABLED=0` skips that verification for quick runs
- Honest limits: hand-score fitness can pick a grid that needs more FEA
  fix iterations than its parent (search optimises the hand model, FEA
  re-validates it); surrogate R² is in-sample on a small linear model
- `tests/test_surrogate.py` + `tests/test_genetic.py` (33 tests) cover
  fitting/roundtrip/thresholds, repair invariants, determinism, fitness
  parity with the pipeline (incl. budget check), bounded search loops,
  error isolation, poison-row/unreadable-dataset hardening, report
  rendering, and both pipeline/CLI wiring paths

## Quickstart

```bash
# 1. environment
python -m venv .venv
.venv\Scripts\activate          # Windows (pip install -r requirements.txt)
pip install -r requirements.txt

# 2. CLI run
python main.py "Design a 10-floor residential building in Mumbai with 4 units per floor, budget 10 crore, parking and green roof"

# 3. or structured input
python main.py --dict inputs.json --no-pdf

# 4. dashboard
streamlit run ui/web_app.py      # (or: python main.py --web)

# 5. tests
python -m pytest tests -q
```

Each run writes to `output/run_<timestamp>/`:

```
report.pdf                 - full PDF report (comparison + per-design sheets)
summary.json               - machine-readable results
design_A_plan.png          - floor plan (grid, columns, core, dimensions)
design_A_elevation.png     - elevation
design_A_seismic.png       - storey forces + drift chart
design_A_3d.png            - 3D massing (for the PDF)
design_A_3d.html           - interactive 3D - open in any browser
cost_comparison.png / score_comparison.png
```

## Scoring (how designs are ranked)

`score = 40 × code-compliance + 25 × member-efficiency + 20 × cost + 15 × drift-margin`

- **compliance** - fraction of checks passed (incl. budget, when given)
- **efficiency** - utilisation closest to 0.75 scores highest (safe *and* right-sized)
- **cost** - within budget = up to 20; over budget decays fast
- **drift** - margin against the 0.004 inter-story drift limit

## Codes used (and honest limitations)

- **IS 1893:2016 (Part 1)** - zone factors, importance `I`, response reduction `R`,
  equivalent static method, response spectrum, drift limit 0.004
- **IS 875 (Part 3):2015** - basic wind speed, `k1–k4`, pressures, overturning check
- **IS 875 (Part 2):2015** - live loads by occupancy
- **IS 456:2000** - RC capacity approximations (`0.136 fck bd²`,
  `0.4fck·Ag + 0.67fy·Asc`, L/d serviceability)

*Simplifications (by design, for concept stage):* triangular seismic
distribution, portal-frame drift, effective column stiffness, no P-Delta /
response-spectrum combination / detailed foundation design (only
screening-level spread footings) / seismic detailing.
The PDF report repeats this honestly.

## Roadmap

- **Phase 2 + 2b (shipped: FEA, foundations, load combinations)** -
  OpenSeesPy FEA cross-check in the optimiser ✅; spread-footing sizing
  with shear/punching/bearing checks ✅; explicit LC1-LC3 combinations in
  hand + FEA ✅; still planned: EnergyPlus energy model
- **Phase 3A (shipped: genetic grid search + ML surrogate)** -
  genome-repaired GA over bay counts/sizes feeding the re-test loop ✅;
  online ridge surrogate pre-screening candidates across runs ✅
- **Phase 3 (planned)** - voice input, BIM-ish export (IFC)

## Project structure

```
BuildingSim/
├── main.py                  # CLI entry
├── config.py                # paths, version, disclaimer, code-data loader
├── data/codes.json          # verified IS-code tables + city costs/zones
├── modules/
│   ├── models.py            # Requirements / Design / Check dataclasses
│   ├── input_handler.py     # text + dict parsing (voice feeds the same path)
│   ├── requirement_analyzer.py
│   ├── design_generator.py  # 3 parametric alternatives
│   ├── structural_analyzer.py  # IS 1893 / 875 / 456 checks + combos + footings
│   ├── foundation.py        # Phase 2b: IS 456 spread-footing design
│   ├── fea.py                # Phase 2: OpenSeesPy 3D frame FEA verification
│   ├── genetic_optimizer.py  # Phase 3A: GA over grid topology (genome repair)
│   ├── surrogate.py          # Phase 3A: pure-Python ridge surrogate scorer
│   ├── optimization_engine.py  # re-test loop (hand + FEA failures)
│   ├── cost_estimator.py
│   ├── visualization.py     # matplotlib 2D/charts + plotly 3D
│   ├── report_generator.py  # fpdf2 PDF
│   └── pipeline.py          # end-to-end orchestration
├── ui/web_app.py            # Streamlit dashboard
└── tests/                   # 151 tests (incl. FEA, foundations, GA + surrogate)
```

## License

MIT - see [LICENSE](LICENSE).
