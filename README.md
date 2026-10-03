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
| Beam moment utilisation | ≤ 1.0 | 1.5 × elastic end moments vs `0.136 fck b d²` |
| Column P-M interaction | ≤ 1.0 | `P/cap + max(My/μy, Mz/μz)` on every column |

- Load cases per design: **G** (service gravity, one-way slab → X-beam
  work-equivalent nodal loads), **Ex** and **Ey** (IS 1893 equivalent-static
  storey forces) - each on a fresh wiped model
- Optimiser escalates on FEA failure: deeper beams → column size ladder →
  shear-wall core (rc_dual) → honest advisory when ladders are exhausted
- Engine quirks handled: `Umfpack`+`RCM` solver, no `ops.remove('load')`,
  broken `eleLoad`/`nodeReaction` (global-force slot extraction verified by
  probe models)
- Toggle with `FEA_ENABLED` in `config.py`; models above `MAX_NODES`
  (12 000 nodes) skip gracefully - the pipeline never blocks
- `tests/test_fea.py` covers availability/skip paths, JSON safety,
  sanity bounds, determinism and the 4-check wiring

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
response-spectrum combination / foundation design / seismic detailing.
The PDF report repeats this honestly.

## Roadmap

- **Phase 2 (shipped: FEA)** - OpenSeesPy FEA cross-check in the optimiser ✅;
  still planned: EnergyPlus energy model, foundation sizing, load combinations
- **Phase 3 (planned)** - ML surrogate scoring, genetic optimisation of grids,
  voice input, BIM-ish export (IFC)

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
│   ├── structural_analyzer.py  # IS 1893 / 875 / 456 checks
│   ├── fea.py                # Phase 2: OpenSeesPy 3D frame FEA verification
│   ├── optimization_engine.py  # re-test loop (hand + FEA failures)
│   ├── cost_estimator.py
│   ├── visualization.py     # matplotlib 2D/charts + plotly 3D
│   ├── report_generator.py  # fpdf2 PDF
│   └── pipeline.py          # end-to-end orchestration
├── ui/web_app.py            # Streamlit dashboard
└── tests/                   # 89 tests (incl. FEA + bug-hunt regression suite)
```

## License

MIT - see [LICENSE](LICENSE).
