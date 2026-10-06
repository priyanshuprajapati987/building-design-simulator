"""PDF report builder (fpdf2) - comparison, per-design sheets, optimisation
log, green-potential notes, codes and the mandatory preliminary-only disclaimer.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from fpdf import FPDF
from fpdf.enums import XPos, YPos

import config as cfg

GREEN = (22, 163, 74)
RED = (190, 40, 40)
AMBER = (217, 119, 6)
INK = (15, 23, 42)
MUTED = (100, 116, 139)
ACCENT = (37, 99, 235)

# fpdf2 core fonts (helvetica) are latin-1 only: a single '₹' (or any other
# non-latin-1 rune) in the raw brief / a warning raised FPDFUnicodeEncodingException
# and the whole PDF build died (summary["pdf_error"]).
_LATIN_MAP = str.maketrans({
    "₹": "Rs.",
    "\u2013": "-", "\u2014": "-", "\u2212": "-",
    "\u2026": "...",
    "\u2018": "'", "\u2019": "'", "\u201a": "'",
    "\u201c": '"', "\u201d": '"',
    "\u00b0": " deg", "\u00d7": "x",
    "\u2264": "<=", "\u2265": ">=", "\u2192": "->",
    "\u00b7": "-", "\u2022": "-", "\u00a0": " ",
})


def _lat(text) -> str:
    """Any text -> latin-1-safe (unmapped runes become '?')."""
    s = str(text).translate(_LATIN_MAP)
    return s.encode("latin-1", "replace").decode("latin-1")


class ReportPDF(FPDF):
    def header(self):
        if self.page_no() == 1:
            return
        self.set_font("helvetica", "", 8)
        self.set_text_color(*MUTED)
        self.cell(0, 6, f"{cfg.APP_NAME} {cfg.VERSION} - PRELIMINARY DESIGN REPORT",
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_draw_color(220, 220, 220)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(2)
        self.set_text_color(0, 0, 0)

    def footer(self):
        self.set_y(-13)
        self.set_font("helvetica", "I", 7)
        self.set_text_color(*MUTED)
        self.multi_cell(0, 4,
                        cfg.DISCLAIMER,
                        align="C")
        self.set_font("helvetica", "", 7)
        self.cell(0, 4, f"- page {self.page_no()} / {{nb}} -",
                  align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _h1(pdf, text):
    pdf.set_font("helvetica", "B", 14)
    pdf.set_text_color(*ACCENT)
    pdf.ln(3)
    pdf.multi_cell(0, 8, _lat(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_draw_color(*ACCENT)
    pdf.set_line_width(0.5)
    pdf.line(pdf.l_margin, pdf.get_y() + 0.5, pdf.w - pdf.r_margin, pdf.get_y() + 0.5)
    pdf.set_line_width(0.2)
    pdf.ln(3)
    pdf.set_text_color(0, 0, 0)


def _h2(pdf, text):
    pdf.set_font("helvetica", "B", 11)
    pdf.set_text_color(*INK)
    pdf.ln(2)
    pdf.multi_cell(0, 6, _lat(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(1)
    pdf.set_text_color(0, 0, 0)


def _body(pdf, text, size=9.5):
    pdf.set_font("helvetica", "", size)
    pdf.set_text_color(30, 30, 30)
    pdf.multi_cell(0, 5, _lat(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(0, 0, 0)


def _kv_table(pdf, rows, w1=70):
    pdf.set_font("helvetica", "", 9)
    usable = pdf.w - pdf.l_margin - pdf.r_margin
    for k, v in rows:
        if pdf.get_y() > pdf.h - 30:
            pdf.add_page()
        pdf.set_font("helvetica", "B", 9)
        pdf.cell(w1, 5.6, _lat(k), border=1, new_x=XPos.RIGHT, new_y=YPos.TOP)
        pdf.set_font("helvetica", "", 9)
        pdf.cell(usable - w1, 5.6, _lat(v), border=1,
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _grid(pdf, headers, rows, widths, aligns=None, size=8.5, colors=None):
    usable = pdf.w - pdf.l_margin - pdf.r_margin
    total = sum(widths)
    widths = [w * usable / total for w in widths]
    aligns = aligns or ["L"] * len(headers)

    pdf.set_fill_color(238, 242, 247)
    pdf.set_font("helvetica", "B", size)
    for h, w, a in zip(headers, widths, aligns):
        pdf.cell(w, 6, _lat(h), border=1, align=a, fill=True,
                 new_x=XPos.RIGHT, new_y=YPos.TOP)
    pdf.ln(6)

    pdf.set_font("helvetica", "", size)
    for ri, row in enumerate(rows):
        if pdf.get_y() > pdf.h - 32:
            pdf.add_page()
            pdf.set_fill_color(238, 242, 247)
            pdf.set_font("helvetica", "B", size)
            for h, w, a in zip(headers, widths, aligns):
                pdf.cell(w, 6, _lat(h), border=1, align=a, fill=True,
                         new_x=XPos.RIGHT, new_y=YPos.TOP)
            pdf.ln(6)
            pdf.set_font("helvetica", "", size)
        for ci, (val, w, a) in enumerate(zip(row, widths, aligns)):
            if colors and colors.get((ri, ci)):
                pdf.set_text_color(*colors[(ri, ci)])
            pdf.cell(w, 5.6, _lat(val), border=1, align=a,
                     new_x=XPos.RIGHT if ci < len(row) - 1 else XPos.LMARGIN,
                     new_y=YPos.TOP if ci < len(row) - 1 else YPos.NEXT)
            pdf.set_text_color(0, 0, 0)


def _note(pdf, text, kind="info"):
    if kind == "warn":
        pdf.set_fill_color(254, 243, 199)
        pdf.set_text_color(*AMBER)
    else:
        pdf.set_fill_color(239, 246, 255)
        pdf.set_text_color(*ACCENT)
    pdf.set_font("helvetica", "", 8.5)
    usable = pdf.w - pdf.l_margin - pdf.r_margin
    pdf.multi_cell(usable, 5, _lat(text), border=1, fill=True,
                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(1.5)


def _image_if(pdf, path, w=170):
    if not (path and Path(path).exists()):
        return
    if pdf.page_no() == 0:
        pdf.add_page()
    # cap by aspect ratio: a 60-floor elevation PNG is taller than A4 and used
    # to overflow the page (only the width was given, fpdf2 kept the raw
    # aspect height). PIL gives true pixel ratio; fallback keeps old behaviour.
    try:
        from PIL import Image
        with Image.open(path) as im:
            iw, ih = im.size
        h = w * ih / iw if iw else 70.0
    except Exception:
        h = 70.0
    max_h = pdf.h - pdf.t_margin - max(pdf.b_margin, 16)   # match auto-break
    if h > max_h:
        w, h = w * max_h / h, max_h
    if pdf.get_y() + h > pdf.h - pdf.b_margin:
        pdf.add_page()
    pdf.image(str(path), w=w)
    pdf.ln(2)


def build_report(summary: dict, images: dict, out_pdf: Path) -> Path:
    """summary = pipeline summary; images = {design_id: {plan, elevation,
    seismic, three_d}} plus 'cost', 'score' at top level."""
    req = SimpleNamespace(**summary["requirements"])
    results = summary["results"]
    pdf = ReportPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.set_margins(16, 14, 16)
    pdf.alias_nb_pages()

    # ---------------- cover ----------------
    pdf.add_page()
    pdf.ln(24)
    pdf.set_font("helvetica", "B", 24)
    pdf.set_text_color(*INK)
    pdf.multi_cell(0, 11, cfg.APP_NAME, align="C",
                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("helvetica", "", 12)
    pdf.set_text_color(*MUTED)
    pdf.multi_cell(0, 7, "AI + Parametric Preliminary Design Comparison",
                   align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(4)
    pdf.set_font("helvetica", "", 10)
    pdf.set_text_color(*INK)
    pdf.multi_cell(0, 6,
                   f"Report date : {datetime.now():%d %B %Y %H:%M}    |    "
                   f"Engine : {cfg.VERSION}    |    Designs compared : {len(results)}",
                   align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(8)

    pdf.set_fill_color(254, 226, 226)
    pdf.set_draw_color(*RED)
    pdf.set_text_color(*RED)
    pdf.set_font("helvetica", "B", 10)
    pdf.multi_cell(0, 6, "PRELIMINARY ONLY - NOT FOR CONSTRUCTION",
                   border=1, fill=True, align="C",
                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(90, 30, 30)
    pdf.set_font("helvetica", "", 9)
    pdf.multi_cell(0, 5, cfg.DISCLAIMER, border=1, fill=True, align="C",
                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(0, 0, 0)
    pdf.set_draw_color(0, 0, 0)
    pdf.ln(6)

    _h2(pdf, "Design brief as entered")
    _body(pdf, req.raw_text or "(built from form inputs)")

    # ---------------- 1. requirements ----------------
    pdf.add_page()
    _h1(pdf, "1. Requirements and site inputs")
    _kv_table(pdf, [
        ("Building type", req.building_type.title()),
        ("City", f"{req.city.title()} (zone {req.seismic_zone})"),
        ("Floors", f"{req.floors} x {req.floor_h_m} m = {req.floors * req.floor_h_m:.1f} m"),
        ("Units per floor", req.units_per_floor or "-"),
        ("Land / plot", f"{req.land_area_sqft:,.0f} sqft" if req.land_area_sqft else "not specified"),
        ("Budget", f"Rs. {req.budget_crores:.2f} crore" if req.budget_crores else "not specified"),
        ("Seismic zone / soil",
         f"Zone {req.seismic_zone} "
         f"(Z={cfg.get_codes()['seismic']['zone_factors'][req.seismic_zone]}) / "
         f"Soil {req.soil_type}"),
        ("Wind basic speed", f"{req.vb} m/s, terrain cat {req.terrain_cat}"),
        ("Special requirements", ", ".join(req.special) if req.special else "none"),
    ])
    if req.warnings:
        pdf.ln(2)
        _h2(pdf, "Input warnings / notes")
        for wmsg in req.warnings[:12]:
            _note(pdf, "- " + wmsg, kind="warn")

    # ---------------- 2. comparison ----------------
    _h1(pdf, "2. Comparison of the three alternatives")
    headers = ["ID", "Design", "System", "Bay (m)", "Floors", "Height", "Cost (Cr)",
               "EUI", "Score", "Rank"]
    rows = []
    for r in results:
        d = r["design"]
        en = r.get("energy") or {}
        rows.append([d["id"], d["name"], d["system"], f"{d['bay_x_m']:g}",
                     d["floors"], f"{d['height_m']:.1f} m",
                     f"{r['cost']['total_inr'] / 1e7:.2f}",
                     f"{en['eui_kwh_m2yr']:.0f}" if en else "-",
                     f"{r['score']:.0f}", r["rank"]])
    _grid(pdf, headers, rows, [7, 40, 27, 13, 11, 15, 15, 12, 12, 9],
          aligns=["C", "L", "L", "C", "C", "C", "R", "R", "C", "C"])

    winner = results[0]
    cheapest = min(results, key=lambda r: r["cost"]["total_inr"])
    stiffest = min(results, key=lambda r: r["analysis"]["drift"]["max_index"])
    pdf.ln(2)
    _note(pdf,
          f"Verdict: {winner['design']['id']} - {winner['design']['name']} wins overall "
          f"(score {winner['score']:.0f}/100). Cheapest: {cheapest['design']['id']} "
          f"(Rs. {cheapest['cost']['total_inr'] / 1e7:.2f} Cr). Stiffest: "
          f"{stiffest['design']['id']} (drift {stiffest['analysis']['drift']['max_index']:.4f}). "
          f"Scoring = 40% code compliance + 25% member efficiency + 20% cost + 15% drift margin.")

    _image_if(pdf, images.get("cost"), w=176)
    _image_if(pdf, images.get("score"), w=140)

    # ---------------- 3. per design ----------------
    for idx, r in enumerate(results):
        d = r["design"]
        a = r["analysis"]
        c = r["cost"]
        pdf.add_page()
        _h1(pdf, f"3.{idx + 1} Design {d['id']} - {d['name']}  "
                 f"(rank {r['rank']}, score {r['score']:.0f}/100)")
        _body(pdf, d["description"])

        _kv_table(pdf, [
            ("Structural system", d["system"].replace("_", " ")),
            ("Plan size", f"{d['len_x_m']:.1f} x {d['len_y_m']:.1f} m"),
            ("Grid / bay", f"{d['bays_x']} x {d['bays_y']} @ {d['bay_x_m']:g} m"),
            ("Floor plate", f"{d['plate_sqm']:.0f} sqm ({d['plate_sqft']:.0f} sqft)"),
            ("Floor height", f"{d['floor_h_m']:.2f} m x {d['floors']} floors"),
            ("Slab / beam", f"{d['slab_t_mm']} mm | {d['beam_w_mm']} x {d['beam_d_mm']} mm"),
            ("Secondary beams", "yes" if d["secondary"] else "no"),
            ("Columns per floor", d["n_columns"]),
            ("Structural core", (f"{d['core_lx_m']} x {d['core_ly_m']} m, "
                                 f"{d['wall_t_mm']} mm walls") if d["core"] else "none"),
            ("Gross built-up", f"{d['gross_sqft']:,.0f} sqft"),
        ], w1=55)

        _h2(pdf, "Loads and seismic (IS 1893-2016 equivalent static)")
        load = a["loads"]
        s = a["seismic"]
        _grid(pdf, ["Parameter", "Value", "Parameter", "Value"],
              [
                  ["Dead load", f"{load['dead_load_knm2']} kN/m2",
                   "Zone factor Z", s["Z"]],
                  ["Live load (ground)", f"{load['live_load_knm2_ground']} kN/m2",
                   "Importance I", s["I"]],
                  ["Seismic weight / floor", f"{load['seismic_weight_per_floor_kN']} kN",
                   "Response reduction R", s["R"]],
                  ["Total weight W", f"{load['W_total_kN']} kN",
                   "Soil type", s["soil"]],
                  ["Period T", f"{s['T_s']} s", "Sa/g", s["Sa_g"]],
                  ["Ah", s["Ah"], "Base shear V", f"{s['V_base_kN']} kN"],
                  ["V/W ratio", s["base_shear_ratio"],
                   "Concrete grade", load["concrete_grade"]],
              ],
              [40, 40, 45, 45], aligns=["L", "R", "L", "R"], size=8.5)

        _h2(pdf, "Wind (IS 875 Part 3:2015)")
        wnd = a["wind"]
        _grid(pdf, ["Vb (m/s)", "k2", "Vz (m/s)", "pd (kN/m2)", "OT X", "OT Y"],
              [[wnd["vb_mps"], wnd["k2"], wnd["Vz_mps"], wnd["pd_knm2"],
                wnd["ot_factor_x"], wnd["ot_factor_y"]]],
              [20, 16, 22, 26, 22, 22], aligns=["C"] * 6, size=8.5)

        combos = a.get("load_combos") or {}
        if combos:
            _h2(pdf, "Load combinations (IS 456 CL 18.2 / IS 1893 CL 6.4.1)")
            _grid(pdf, ["ID", "Combination", "DL", "LL", "EQ"],
                  [[k, v["label"], v["DL"], v["LL"], v["EQ"]]
                   for k, v in combos.items()],
                  [16, 96, 16, 16, 16],
                  aligns=["C", "L", "C", "C", "C"], size=8.5)

        _h2(pdf, "Code checks")
        colors = {}
        check_rows = []
        for ci, chk in enumerate(a["checks"]):
            verdict = "PASS" if chk["passed"] else "FAIL"
            colors[(ci, 4)] = GREEN if chk["passed"] else RED
            check_rows.append([chk["name"], chk["value"], chk["unit"],
                               chk["limit"], verdict])
        _grid(pdf, ["Check", "Value", "Unit", "Limit", "Result"], check_rows,
              [72, 20, 16, 34, 16],
              aligns=["L", "R", "C", "C", "C"], size=8, colors=colors)
        _body(pdf, f"Score: {r['score']:.1f}/100  "
                   f"({a['passed']}/{a['total_checks']} checks passed, "
                   f"max utilisation {a['max_utilisation']}, drift "
                   f"{a['drift']['max_index']} <= {a['drift']['limit']}).",
               size=9)

        # ---- Phase-2 preliminary energy model ----------------------------
        en = r.get("energy")
        if en:
            _h2(pdf, "Energy estimate (preliminary ECBC degree-day model)")
            bd = en["breakdown_kwh"]
            _grid(pdf,
                  ["EUI (kWh/m2-yr)", "Annual (kWh)", "Energy cost (Rs/yr)",
                   "Cooling", "Lighting", "Plug + others"],
                  [[en["eui_kwh_m2yr"], f"{en['annual_kwh']:,}",
                    f"{en['annual_cost_inr']:,}",
                    f"{bd['cooling'] + bd['fans_pumps']:,}",
                    f"{bd['lighting']:,}",
                    f"{bd['plug_appliances'] + bd['heating']:,}"]],
                  [28, 24, 28, 22, 22, 26],
                  aligns=["C"] * 6, size=8.5)
            cl = en["climate"]
            _body(pdf, f"Engine: {en['engine']} - {en['note']}. "
                       f"Climate {cl['city']}: design {cl['design_db_c']} C, "
                       f"CDD24 {cl['cdd24']}, HDD16 {cl['hdd16']} "
                       f"(kWh figures include HVAC fans/pumps).", size=8)

        # ---- Phase-2 OpenSees FEA verification ---------------------------
        fea_r = a.get("fea") or {}
        if fea_r.get("ok"):
            _h2(pdf, "OpenSees FEA verification (Phase 2)")
            _grid(pdf, ["Quantity", "Value", "Limit / note"],
                  [["Model", f"{fea_r['nodes']} nodes, "
                             f"{fea_r['elements']} elements, "
                             f"{fea_r['solve_ms']} ms",
                    "4 linear-static cases (D, L, Ex, Ey) -> LC1-LC3"],
                   ["Base-shear equilibrium", f"{fea_r['equilibrium_err_pct']} %",
                    "<= 5 %"],
                   ["Inter-story drift index (max)",
                    f"{fea_r['drift_max_index']} ({fea_r['drift_direction']}, "
                    f"floor {fea_r['drift_governing_floor']})", "<= 0.004"],
                   ["Beam moment utilisation (max)",
                    f"{fea_r['beam_max_util']} (M_u "
                    f"{fea_r['beam_Mmax_kNm']:.0f} kNm)", "<= 1.00"],
                   ["Column interaction (max)",
                    f"{fea_r['column_max_interaction']} "
                    f"({fea_r['column_worst']})", "<= 1.00"],
                   ["Diaphragm spread (max)",
                    f"{fea_r['diaphragm_spread_max_mm']} mm",
                    "plan distortion under lateral load"],
                   ["Gravity top displacement", f"{fea_r['gravity_top_mm']} mm",
                    "service load, linear elastic"]],
                  [56, 66, 48], aligns=["L", "R", "L"], size=8)
            _note(pdf, "FEA assumptions: " + "; ".join(fea_r["approximations"])
                       + ".", kind="info")
        elif fea_r.get("skipped"):
            _note(pdf, f"OpenSees FEA verification skipped: {fea_r['skipped']}.",
                  kind="warn")
        elif fea_r.get("error"):
            _note(pdf, f"OpenSees FEA verification failed: {fea_r['error']}. "
                       "Hand-method checks above are unaffected.",
                  kind="warn")

        _h2(pdf, "Cost estimate")
        _grid(pdf, ["Component", "Share (Rs.)"],
              [["Structure (frame/slab/core)", f"{c['breakdown_inr']['structure']:,.0f}"],
               ["Foundation (substructure)", f"{c['breakdown_inr']['foundation']:,.0f}"],
               ["Finishes", f"{c['breakdown_inr']['finishes']:,.0f}"],
               ["MEP / services", f"{c['breakdown_inr']['mep_services']:,.0f}"],
               ["External + misc", f"{c['breakdown_inr']['external_and_misc']:,.0f}"],
               ["TOTAL", f"{c['total_inr']:,.0f}  ({c['total_crores']:.2f} Cr)"],
               ["Effective rate", f"Rs. {c['effective_rate_inr_sqft']}/sqft "
                                  f"(base {c['base_rate_inr_sqft']} x factors)"]],
              [70, 90], aligns=["L", "R"], size=8.5)
        if c["within_budget"] is not None:
            _note(pdf, "Within stated budget: YES - "
                       f"{c['total_inr'] / c['budget_inr']:.0%} of budget used."
                  if c["within_budget"] else
                  f"OVER stated budget by Rs. "
                  f"{(c['total_inr'] - c['budget_inr']) / 1e7:.2f} Cr - "
                  "reduce finishes, parking depth, or structural grade.",
                  kind="info" if c["within_budget"] else "warn")

        # Section-3 table is the re-test loop only: the GA row (long grid
        # strings) overflows the page-width cells and is reported in full
        # in section 4 instead
        loop_fixes = [f for f in r["fixes"] if f["iteration"] != "GA"]
        if loop_fixes:
            _h2(pdf, "Optimisation log (re-test loop)")
            fix_rows = [[f["iteration"], f["issue"], str(f["before"]), f["action"]]
                        for f in loop_fixes]
            _grid(pdf, ["Iter", "Failed check", "Before", "Action applied"],
                  fix_rows, [12, 58, 20, 80], aligns=["C", "L", "R", "L"], size=8)
        elif not r["fixes"]:
            _note(pdf, "Optimisation log: all checks passed on first analysis - "
                       "no fixes needed.")
        else:
            _note(pdf, "Optimisation log: no re-test-loop fixes - the genetic "
                       "grid search result is reported in section 4.")

        _h2(pdf, "Drawings")
        _image_if(pdf, images.get(d["id"], {}).get("plan"), w=150)
        _image_if(pdf, images.get(d["id"], {}).get("elevation"), w=150)
        _image_if(pdf, images.get(d["id"], {}).get("seismic"), w=165)
        _image_if(pdf, images.get(d["id"], {}).get("three_d"), w=140)
        _body(pdf, "Interactive 3D model: open the *_3d.html file in the run folder "
                   "(double-click).", size=8.5)

        # column schedule for this design
        cols = a["members"]["columns"]
        if cols:
            _h2(pdf, "Column schedule (bottom to top, by floor)")
            seen = {}
            for row in cols:
                seen.setdefault(row["section_mm"], []).append(row["floor"])
            sched = [[sec, f"{min(fl)}-{max(fl)}" if min(fl) != max(fl) else str(fl[0]),
                      f"{len(fl)}"] for sec, fl in seen.items()]
            _grid(pdf, ["Section (mm)", "Floors", "Count"], sched,
                  [50, 50, 30], aligns=["L", "C", "C"], size=8.5)

        # spread footing schedule for this design
        fnd = a.get("foundation") or {}
        if fnd.get("footings"):
            _h2(pdf, "Spread footing schedule (screening)")
            frows = [[fr["position"], fr["governing_combo"],
                      f"{fr['P_service_kN']:,.0f}", fr["size_mm"],
                      fr["thickness_mm"], fr["bearing_util"],
                      max(fr["shear_util"], fr["punching_util"]),
                      fr["moment_util"]]
                     for fr in fnd["footings"]]
            _grid(pdf, ["Position", "Gov. combo", "P serv (kN)", "Size BxB (mm)",
                        "t (mm)", "Bearing", "Shear", "Moment"],
                  frows, [24, 24, 26, 30, 16, 18, 18, 18],
                  aligns=["L", "C", "R", "C", "C", "R", "R", "R"], size=8)
            _note(pdf, f"Allowable bearing SBC = {fnd['sbc_knm2']:.0f} kN/m2 "
                       f"(soil class {fnd['soil_type']}); combined with LC1/LC2/"
                       f"LC3 above. {fnd['note']}.",
                  kind="info")

    # ---------------- 4. optimisation summary ----------------
    pdf.add_page()
    _h1(pdf, "4. Optimisation summary (all designs)")
    any_fix = False
    for r in results:
        if r["fixes"]:
            any_fix = True
            _h2(pdf, f"Design {r['design']['id']} - {r['design']['name']}")
            for f in r["fixes"]:
                _body(pdf, f"iter {f['iteration']}: [{f['issue']}] {f['action']}",
                      size=9)
    if not any_fix:
        _body(pdf, "No design required optimisation - every check passed on the "
                   "first analysis.")

    # Phase-3A: genetic grid search detail
    ga_any = False
    for r in results:
        ga = r.get("genetic") or {}
        if ga.get("error"):                # evolve failed: report, keep going
            if not ga_any:
                _h2(pdf, "Genetic grid search (Phase 3A)")
                ga_any = True
            _note(pdf, f"Design {r['design']['id']}: genetic search skipped "
                       f"({ga['error']}) - parent grid kept.", kind="warn")
            continue
        if not ga.get("enabled") or "parent_grid" not in ga:
            continue
        if not ga_any:
            _h2(pdf, "Genetic grid search (Phase 3A)")
            ga_any = True
        _body(pdf, f"Design {r['design']['id']}: {ga['parent_grid']}  ->  "
                   f"{ga['best_grid']} | hand score {ga['parent_score']} -> "
                   f"{ga['best_score']} | {ga['evaluations']} evaluations, "
                   f"{ga['generations_run']} generations, "
                   f"{ga['elapsed_ms']} ms (seed {ga['seed']})", size=9)
        if not ga.get("improved"):
            _body(pdf, "parent grid kept (no better grid found in budget)",
                  size=9)
    gsum = summary.get("genetic") or {}
    sur = gsum.get("surrogate") or {}
    if gsum.get("enabled"):
        if sur.get("trained"):
            _note(pdf, f"ML surrogate: trained on {sur['samples']} evaluated "
                       f"designs, in-sample R2 {sur['r2_train']} - used only "
                       "to pre-screen candidate grids; every final score "
                       "comes from the real structural analysis.", kind="info")
        else:
            _note(pdf, f"ML surrogate: warming up "
                       f"({sur.get('samples', 0)}/{sur.get('min_samples', 15)} "
                       "samples) - all candidate grids were fully evaluated "
                       "this run.", kind="info")

    # ---------------- 5. green potential ----------------
    _h1(pdf, "5. Green-building potential (indicative)")
    d0 = results[0]["design"]
    roof_sqm = d0["plate_sqm"]
    solar_kw = roof_sqm / 10.0            # ~10 sqm per kWp
    rain_l = roof_sqm * 1000.0 * 0.8      # 1000 mm assumed rainfall x 80% runoff
    daylight = max(d0["len_x_m"], d0["len_y_m"]) / 2.0
    _grid(pdf, ["Opportunity", "Indicative figure", "How to capture it"],
          [
              ["Rooftop solar", f"~{solar_kw:.0f} kWp potential",
               "PV panels on roof + carport (net-metering)"],
              ["Rainwater harvesting", f"~{rain_l / 1000:.0f} kL per 1000 mm rain",
               "Roof downpipes to recharge pits / storage"],
              ["Daylight depth", f"~{daylight:.1f} m core-to-glass",
               "<= 8 m is good; openable panels for glare control"],
              ["Cool roof / green roof", "special requirement detected"
               if "green_roof" in req.special else "not requested",
               "High-SRI paint or intensive green terrace"],
              ["Structure efficiency", f"max utilisation "
               f"{results[0]['analysis']['max_utilisation']}",
               "Right-sized members = less embodied carbon"],
          ],
          [42, 55, 78], aligns=["L", "L", "L"], size=8.5)
    _note(pdf, "These are heuristic potentials, not certified LEED/GRIHA points. "
               "A preliminary annual energy estimate (degree-day model) is in "
               "sections 2 and 3; formal certification needs an hourly "
               "simulation engine such as EnergyPlus.", kind="warn")

    # ---------------- 6. codes ----------------
    _h1(pdf, "6. Codes, methods and honesty note")
    _grid(pdf, ["Code / method", "What was used"],
          [["IS 1893:2016 (Part 1)", "Zone factors, importance I, response reduction R, "
                                     "equivalent static forces, response spectrum, "
                                     "drift limit 0.004"],
           ["IS 875 (Part 3):2015", "Basic wind speed, k1-k4, pressure pz, "
                                    "design pressure pd, overturning check"],
           ["IS 875 (Part 2):2015", "Live loads by occupancy (residential 2.0, "
                                    "office 2.5, parking 4.0 kN/m2)"],
            ["IS 456:2000", "RC member sizing (limit-state approximations: "
                            "0.136 fck bd2 moment capacity, column capacity "
                            "0.4fck Ag + 0.67fy Asc, L/d serviceability)"],
            ["Energy (preliminary)", "ECBC-aligned degree-day model: envelope "
                                     "UA x CDD24/HDD16 + solar + lighting/plug "
                                     "loads, EER 3.4 cooling / COP 2.8 heating "
                                     "- comparison only, not a certified "
                                     "energy model"],
            ["Simplified methods", "Triangular seismic distribution, portal-frame "
                                   "drift, effective-width column stiffness - "
                                   "documented simplifications for concept stage"]],
          [50, 126], aligns=["L", "L"], size=8.5)
    pdf.ln(2)
    _body(pdf,
          "Honesty note: this engine performs SCREENING-LEVEL checks with "
          "conservative simplifications. It does not do response-spectrum "
          "combination, P-Delta effects, detailed foundation design (only "
          "screening spread footings), or seismic detailing (which decides R "
          "actually achieved). Hand formulas are cross-checked by a "
          "linear-static OpenSees frame model where available. Numbers here "
          "are for comparing alternatives and budgeting - they are NOT "
          "construction values.", size=9.5)
    _note(pdf, cfg.DISCLAIMER, kind="warn")

    out_pdf = Path(out_pdf)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(out_pdf))
    return out_pdf
