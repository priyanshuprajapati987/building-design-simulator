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
| 1. Input | Free text / spoken brief (mic in the dashboard, `--voice-file` on the CLI) / structured JSON / sidebar form |
| 2. Requirements | City → seismic zone (IS 1893), wind speed + terrain (IS 875), defaults, sanity warnings |
| 3. Generate | **3 parametric alternatives**: A Compact RC Frame, B Core Shear-Wall Dual, C Open-Plan Ductile Frame |
| 4. Code checks | Seismic (equivalent static), wind, columns/beams/slab capacity + drift + overturning (IS 1893 / IS 875 / IS 456) |
| 5. Optimise | Rule-based re-test loop: bigger columns / deeper beams / thicker slab / add core - then re-analyse |
| 6. Cost | City rate (₹/sqft) × system + zone + soil factors, breakdown structure/finishes/MEP/other, budget check |
| 7. Energy | ECBC-aligned degree-day model: EUI, annual kWh + ₹ cost per alternative (see Phase 2c) |
| 8. Score & rank | 40% compliance + 25% member efficiency + 20% cost + 15% drift margin |
| 9. Output | Floor plan, elevation, seismic + cost + score charts, **interactive 3D (HTML)**, `summary.json`, **PDF report**, **IFC4 model** |

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

## Phase 2c (shipped) - preliminary energy model (ECBC degree-day)

Every alternative now gets an **annual energy estimate** so designs can be
compared on running cost, not just capex:

- **Degree-day method** (`modules/energy_model.py`, engine
  `preliminary-degree-day-v1`): envelope `UA × CDD24/HDD16` + glazing
  solar gain (`SHGC × annual irradiance`) + ECBC-style lighting/plug loads
  (W/m² × operating hours by building type), cooling at EER 3.4 and
  heating at COP 2.8, HVAC fans/pumps as 12% of cooling
- **Data-driven** - all inputs live in `data/codes.json` → `energy`:
  envelope U-values, per-type loads (10 building types), tariff
  (₹/kWh, residential discount), and `[design DB, CDD24, HDD16]`
  climate normals for **all 37 cities** (+ safe default for unknown cities)
- **Reported**: EUI (kWh/m²·yr), annual kWh, annual ₹ cost, and a
  5-way breakdown in `summary.json`, the **PDF report** (comparison
  column + per-design energy block + codes table row) and the
  **Streamlit UI** comparison table
- Honest scope: screening-level comparison only - excludes DHW, lifts and
  specialty equipment; **not** an hourly simulation. `find_energyplus()`
  detects an installed EnergyPlus binary for a future engine swap-in
  (never installed silently; returns `None` here - binary not present)
- `tests/test_energy_model.py` (10 tests): independent hand oracle,
  climate defaulting, breakdown sums, EUI bands per building type,
  detection paths, pipeline + generator wiring

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

## Phase 3B (shipped) - IFC4 export (BIM-ish handoff)

Every run also writes one **IFC4** model per design under `out/ifc/`
(`design_A.ifc` ... `design_C.ifc`), openable in common viewers
(BIMvision, BIMcollab, FreeCAD, Revit, online viewers):

- **Hand-rolled ISO-10303-21 (STEP) writer** (`modules/ifc_export.py`) -
  zero runtime dependencies (no IfcOpenShell required at runtime). The
  entity stream follows the printed IFC4 spec: spatial tree
  `IfcProject -> IfcSite -> IfcBuilding -> IfcBuildingStorey`, per-storey
  `IfcRelContainedInSpatialStructure`, each solid an
  `IfcExtrudedAreaSolid` wrapped in an `IfcShapeRepresentation('Body')`
- **Geometry from the *analysed* design**: full-plate slabs, grid columns
  (per-floor section straight from the member schedule), per-bay beam
  segments (main beams span the full bay; secondary beams at mid-grid when
  the design uses them - panel = bay/2), the 4-wall structural core ring
  when `core=True`, and spread-footing pads under every column at the
  analysed `B_mm x thickness_mm` (top at grade, corner/edge/interior
  classification matching the drawings)
- **IFC compressed GUIDs**: 128-bit MD5 digest -> the IFC 22-char custom
  base64 alphabet (first char always `0-3`), seeded per element so a
  re-export of the same design is byte-identical for a fixed timestamp;
  equality with `ifcopenshell.guid.compress()` is asserted in tests
- **`Pset_BuildingSim`** on the building carries the run's headline
  numbers: city, building type, structural system, seismic zone, floors,
  score, estimated cost, EUI
- **Wiring**: on by default - `pipeline.run(..., make_ifc=False)` or
  `--no-ifc` to skip; `summary["files"]["ifc"]` maps design id -> path and
  failures land in `summary["ifc_error"]` (an IFC error never kills a
  run); the Streamlit report tab gains per-design download buttons
- **Validated**: the exported files open in IfcOpenShell and pass
  `ifcopenshell.validate` (schema + attributes + EXPRESS WHERE rules)
  with 0 errors - plus a manual open in a common viewer
- Honest limits: concept-stage only - extruded-box geometry, no
  materials, reinforcement, quantities (Qto) sets or drawings; it is a
  faithful BIM-ish handoff of what the engine computed, not a
  documentation-grade BIM model
- `tests/test_ifc_export.py` (15 tests): GUID charset/format + reference
  match, header, referential integrity, entity counts + storey
  elevations, core+secondary coverage, pset values, determinism,
  minimal-input edge case, IfcOpenShell validate + express rules, and
  pipeline on/off wiring

## Phase 3C (shipped) - voice input (offline speech-to-text)

Speak the brief instead of typing it - fully offline, no cloud STT:

- **Dashboard**: the sidebar gains a *Voice brief* mic widget
  (`st.audio_input`) - record, press **Add clip to brief**, and the
  transcript is appended to the design-brief text box (typed text is
  preserved); every engine failure surfaces as a readable error and the
  run is never blocked
- **CLI**: `--voice-file brief.wav` transcribes wav/mp3/m4a and uses it
  as the brief (typed text still wins when both are given)
- **Engine**: faster-whisper (CTranslate2, CPU int8, no torch) - in
  `requirements.txt`, model downloaded once into `output/voice/`
  (gitignored; copy the files there by hand for offline machines).
  Optional like FEA: `VOICE_ENABLED=0` or a missing install keeps the
  text path working
- **Config**: `VOICE_MODEL` (default `base.en`), `VOICE_LANGUAGE` (empty
  = auto-detect), `VOICE_MODEL_DIR`
- **Single front door**: transcripts feed `input_handler.parse_text()`
  like any typed brief - voice never bypasses requirement parsing, and
  the requirements block shows exactly what was heard
- Honest limits: short briefs only (not dictation); quality drops on
  noisy audio or heavy code-switching; the model needs one online run
  (or a manual copy) before fully offline use
- Verified end-to-end with a Windows-SAPI synthesised spoken brief:
  transcribed to *"...five-floor office building in Mumbai with a budget
  of 10 crore, parking, and a green roof"* and parsed to office / Mumbai
  / 5 floors / budget 10 Cr / `[green_roof, parking]`
- `tests/test_voice_input.py` (17 tests): availability + engine-missing
  paths, disabled/empty/missing-file/decode/no-speech errors, byte-clip
  and path happy paths, transcript -> parser integration, model cache +
  int8 CPU config, and CLI wiring (runs, error exit, typed-text priority)

## Quickstart

```bash
# 1. environment
python -m venv .venv
.venv\Scripts\activate          # Windows (pip install -r requirements.txt)
pip install -r requirements.txt

# 2. CLI run
python main.py "Design a 10-floor residential building in Mumbai with 4 units per floor, budget 10 crore, parking and green roof"

# 2b. spoken brief (Phase-3C: wav/mp3/m4a -> offline STT, model downloads once)
python main.py --voice-file brief.wav

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

- **Phase 2 + 2b + 2c (shipped: FEA, foundations, energy model)** -
  OpenSeesPy FEA cross-check in the optimiser ✅; spread-footing sizing
  with shear/punching/bearing checks ✅; explicit LC1-LC3 combinations in
  hand + FEA ✅; preliminary ECBC degree-day energy model (EUI, annual
  kWh/cost) ✅ - still planned: certified EnergyPlus engine (binary
  install required)
- **Phase 3A (shipped: genetic grid search + ML surrogate)** -
  genome-repaired GA over bay counts/sizes feeding the re-test loop ✅;
  online ridge surrogate pre-screening candidates across runs ✅
- **Phase 3B (shipped: IFC4 export)** - hand-rolled STEP writer emitting
  the spatial tree, analysed geometry and `Pset_BuildingSim` per design ✅
- **Phase 3C (shipped: voice input)** - offline faster-whisper STT (mic
  clip in the dashboard, `--voice-file` on the CLI) feeding the same text
  parser ✅ - Phase 3 fully shipped (3A search, 3B IFC, 3C voice)

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
│   ├── energy_model.py      # Phase 2c: ECBC degree-day energy model
│   ├── fea.py                # Phase 2: OpenSeesPy 3D frame FEA verification
│   ├── genetic_optimizer.py  # Phase 3A: GA over grid topology (genome repair)
│   ├── surrogate.py          # Phase 3A: pure-Python ridge surrogate scorer
│   ├── optimization_engine.py  # re-test loop (hand + FEA failures)
│   ├── cost_estimator.py
│   ├── visualization.py     # matplotlib 2D/charts + plotly 3D
│   ├── report_generator.py  # fpdf2 PDF
│   ├── ifc_export.py        # Phase 3B: hand-rolled IFC4 STEP writer (zero deps)
│   ├── voice_input.py       # Phase 3C: offline STT briefs (faster-whisper)
│   └── pipeline.py          # end-to-end orchestration
├── ui/web_app.py            # Streamlit dashboard
└── tests/                   # 193 tests (incl. FEA, foundations, energy, GA, IFC, voice)
```

## License

**MIT** - see [LICENSE](LICENSE) (© 2026 Priyanshu Prajapati). Use, modify,
redistribute and even sell freely, as long as the copyright notice stays
with the code.

### Dependency licenses (verified from installed packages)

| Package | License | Note |
|---|---|---|
| streamlit | Apache-2.0 | permissive, no strings |
| plotly | MIT | permissive |
| matplotlib | PSF | permissive |
| numpy | BSD-3-Clause | permissive |
| pytest | MIT | permissive (dev only) |
| **fpdf2** | **LGPL-3.0** | PDF generation: unmodified use is fine; if you distribute the app, ship its license text; if you modify fpdf2 itself, publish that modified source |
| **openseespy** | **custom (UC Berkeley / Oregon State)** | free for research, education and internal use - **commercial redistribution** (a paid app or cloud service that `import openseespy`) requires a commercial license from Dr. Minjie Zhu (zhum@oregonstate.edu) |

**Practical note:** the MIT license covers *this* repository's code. The two
bolded dependencies are the only ones with obligations beyond attribution -
watch them only when you ship a paid/commercial product (internal,
research or educational use is fine as-is).
