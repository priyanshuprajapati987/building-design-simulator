"""Streamlit dashboard for the Building Design Simulator.

Run from the repo root:  streamlit run ui/web_app.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st

import config as cfg
from modules import pipeline, voice_input

st.set_page_config(page_title=cfg.APP_NAME, page_icon="🏢", layout="wide")

_ALIAS_CITIES = {"bangalore", "gurgaon"}
_TYPES = ["residential", "office", "retail", "commercial", "school",
          "hospital", "hotel", "warehouse", "industrial", "parking"]


def _city_options() -> list[str]:
    cities = cfg.get_codes()["cities"]
    names = {k.title() for k in cities if not k.startswith("_")
             and k not in _ALIAS_CITIES}
    return sorted(names)


# ---------------------------------------------------------------------------
# sidebar - requirements
# ---------------------------------------------------------------------------
with st.sidebar:
    st.title("🏢 " + cfg.APP_NAME)
    st.caption(f"v{cfg.VERSION} - preliminary code-based comparison")

    if cfg.VOICE_ENABLED and voice_input.available():
        st.subheader("Voice brief (Phase-3C)")
        clip = st.audio_input("Record your brief, then add it to the text",
                              key="mic_clip")
        if clip is not None and st.button("Add clip to brief",
                                          key="mic_go"):
            raw = clip.getvalue()
            if st.session_state.get("_mic_done") == raw:
                st.caption("This clip was already added to the brief")
            else:
                try:
                    spoken = voice_input.transcribe(raw)
                except voice_input.VoiceInputError as exc:
                    st.error(str(exc))
                else:
                    current = st.session_state.get("brief_input") or ""
                    st.session_state["brief_input"] = (
                        f"{current} {spoken}".strip())
                    st.session_state["_mic_done"] = raw
                    st.caption(f"Added to brief: {spoken}")
    elif cfg.VOICE_ENABLED:
        st.caption("Voice input: pip install faster-whisper to enable")

    brief = st.text_area(
        "Design brief (text / voice transcript)",
        height=130,
        key="brief_input",
        placeholder="e.g. Design a 10-floor residential building in Mumbai "
                    "with 4 units per floor, budget 10 crore, parking and "
                    "green roof")

    st.subheader("Structured inputs")
    c1, c2 = st.columns(2)
    building_type = c1.selectbox("Building type", _TYPES, index=0)
    city = c2.selectbox("City", _city_options(),
                        index=_city_options().index("Delhi"))
    floors = st.number_input("Floors", 1, 60, 5, step=1)
    units = st.number_input("Units / floor (0 = n/a)", 0, 40, 0)
    budget = st.number_input("Budget (crore, 0 = n/a)", 0.0, 5000.0, 0.0, step=0.5)

    c3, c4 = st.columns(2)
    zone = c3.selectbox("Seismic zone", ["Auto", "II", "III", "IV", "V"], index=0)
    soil = c4.selectbox("Soil (IS 1893)", ["I", "II", "III"], index=1)

    st.subheader("Special requirements")
    specials = []
    if st.checkbox("Parking (ground)"):
        specials.append("parking")
    if st.checkbox("Green roof"):
        specials.append("green_roof")
    if st.checkbox("Solar panels"):
        specials.append("solar_panels")
    if st.checkbox("Rainwater harvesting"):
        specials.append("rainwater_harvesting")

    run_clicked = st.button("▶  Run analysis", type="primary",
                            width="stretch")


def _form_data() -> dict:
    """Only fields the user actually changed are sent - an untouched widget
    must NOT clobber what the brief said (the dict used to win over parse_text
    for building_type/city/floors/soil, so "10 storey office in Pune" came back
    as a 5-floor residential Mumbai building). Widget defaults mirror the
    Requirements defaults so an untouched form = pure-brief (or pure-default)
    run. ``special`` unions with the brief's (see input_handler.load)."""
    data: dict = {}
    if building_type != "residential":          # Requirements default
        data["building_type"] = building_type
    if city != "Delhi":                         # Requirements default
        data["city"] = city
    if int(floors) != 5:                        # Requirements default
        data["floors"] = int(floors)
    if soil != "II":                            # Requirements default
        data["soil_type"] = soil
    if units:
        data["units_per_floor"] = int(units)
    if budget:
        data["budget_crores"] = float(budget)
    if zone != "Auto":
        data["seismic_zone"] = zone
    if specials:
        data["special"] = specials
    return data


# ---------------------------------------------------------------------------
# run / session state
# ---------------------------------------------------------------------------
if run_clicked:
    try:
        with st.spinner("Analysing requirements, generating 3 designs, "
                        "running code checks, optimising, costing, rendering..."):
            st.session_state["summary"] = pipeline.run(
                text=brief.strip() or None, data=_form_data())
        st.session_state.pop("run_error", None)
    except Exception as exc:
        st.session_state["run_error"] = f"{type(exc).__name__}: {exc}"

summary = st.session_state.get("summary")

if st.session_state.get("run_error"):
    st.error("Run failed: " + st.session_state["run_error"])

if not summary:
    st.markdown(
        f"""
### 👋 Welcome
Enter a brief on the left (or just hit **Run analysis** for defaults) and this
tool will:

1. parse + validate your requirements (city → seismic zone / wind speed),
2. generate **3 parametric design alternatives**,
3. run **IS 1893 / IS 875 / IS 456 code checks** on each,
4. auto-**optimise** failing members (re-test loop),
5. estimate **city-wise cost**, rank the designs,
6. render **2D plan + elevation + charts + interactive 3D** and a **PDF report**.

> {cfg.DISCLAIMER}
""")
    st.stop()

req = summary["requirements"]
results = summary["results"]
files = summary.get("files", {})
out_dir = Path(files.get("dir", cfg.OUTPUT_DIR))

# ---------------------------------------------------------------------------
# header metrics
# ---------------------------------------------------------------------------
st.header(f"Results - {req['building_type'].title()} in {req['city']} "
          f"({req['floors']} floors, Zone {req['seismic_zone']})")
m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("🏆 Winner", f"{summary['winner']['id']} - "
          f"{summary['winner']['name'].split()[0]}",
          help=summary["winner"]["name"])
costs = [r["cost"]["total_inr"] / 1e7 for r in results]
m2.metric("Cost spread", f"{min(costs):.2f} - {max(costs):.2f} Cr")
m3.metric("Best score", f"{max(r['score'] for r in results):.0f} / 100")
checks_passed = sum(r["analysis"]["passed"] for r in results)
checks_total = sum(r["analysis"]["total_checks"] for r in results)
m4.metric("Checks passed", f"{checks_passed} / {checks_total}")
m5.metric("Budget", f"₹{req['budget_crores']:.2f} Cr"
          if req.get("budget_crores") else "not set")

if req.get("warnings"):
    with st.expander(f"⚠ Input notes ({len(req['warnings'])})", expanded=False):
        for wmsg in req["warnings"]:
            st.write("• " + wmsg)

tab_cmp, tab_a, tab_b, tab_c, tab_opt, tab_rep = st.tabs(
    ["📊 Compare", "🅰 Design A", "🅱 Design B", "🅲 Design C",
     "🔧 Optimisation", "📄 Report"])

# ---------------------------------------------------------------------------
# compare tab
# ---------------------------------------------------------------------------
with tab_cmp:
    import pandas as pd

    def _ep_eui(row):
        ep = ((row.get("energy") or {}).get("energyplus")) or {}
        return ep.get("eui_kwh_m2yr") if ep.get("ok") else "-"

    rows = [{
        "ID": r["design"]["id"],
        "Design": r["design"]["name"],
        "System": r["design"]["system"].replace("_", " "),
        "Bay (m)": r["design"]["bay_x_m"],
        "Plan (m)": f"{r['design']['len_x_m']} x {r['design']['len_y_m']}",
        "Slab (mm)": r["design"]["slab_t_mm"],
        "Core": "yes" if r["design"]["core"] else "-",
        "Cost (₹ Cr)": round(r["cost"]["total_inr"] / 1e7, 2),
        "EUI (kWh/m²·yr)": (r.get("energy") or {}).get("eui_kwh_m2yr", "-"),
        "EUI+ site (kWh/m²·yr)": _ep_eui(r),
        "Checks": f"{r['analysis']['passed']}/{r['analysis']['total_checks']}",
        "Max util": r["analysis"]["max_utilisation"],
        "Drift": r["analysis"]["drift"]["max_index"],
        "FEA drift": (f"{r['analysis']['fea']['drift_max_index']:.4f}"
                      if r["analysis"].get("fea", {}).get("ok") else "-"),
        "Score": r["score"],
        "Rank": r["rank"],
    } for r in results]
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    winner = results[0]
    cheapest = min(results, key=lambda r: r["cost"]["total_inr"])
    stiffest = min(results, key=lambda r: r["analysis"]["drift"]["max_index"])
    st.info(
        f"**Verdict:** {winner['design']['id']} - {winner['design']['name']} "
        f"wins overall (score {winner['score']:.0f}/100). "
        f"Cheapest: {cheapest['design']['id']} "
        f"(₹{cheapest['cost']['total_inr'] / 1e7:.2f} Cr). "
        f"Stiffest: {stiffest['design']['id']} "
        f"(drift {stiffest['analysis']['drift']['max_index']:.4f}). "
        "Score = 40% compliance + 25% efficiency + 20% cost + 15% drift.")

    c1, c2 = st.columns(2)
    if (p := out_dir / "cost_comparison.png").exists():
        c1.image(str(p), caption="Cost comparison")
    if (p := out_dir / "score_comparison.png").exists():
        c2.image(str(p), caption="Overall scores")

# ---------------------------------------------------------------------------
# per-design tabs
# ---------------------------------------------------------------------------
for tab, r in zip((tab_a, tab_b, tab_c), results):
    with tab:
        d = r["design"]
        a = r["analysis"]
        c = r["cost"]
        st.subheader(f"Design {d['id']} - {d['name']}  (rank {r['rank']})")
        st.caption(d["description"])
        k1, k2, k3, k4, k5 = st.columns(5)
        k1.metric("Score", f"{r['score']:.0f}/100")
        k2.metric("Cost", f"₹{c['total_crores']:.2f} Cr",
                  delta=f"₹{c['effective_rate_inr_sqft']:.0f}/sqft")
        k3.metric("Checks", f"{a['passed']}/{a['total_checks']}")
        k4.metric("Max util", f"{a['max_utilisation']:.2f}")
        k5.metric("Drift", f"{a['drift']['max_index']:.4f}",
                  delta=f"limit {a['drift']['limit']}", delta_color="inverse")

        fea_r = a.get("fea") or {}
        if fea_r.get("ok"):
            st.success(
                f"OpenSees FEA verified: equilibrium err "
                f"{fea_r['equilibrium_err_pct']}% | drift "
                f"{fea_r['drift_max_index']:.4f} (dir {fea_r['drift_direction']})"
                f" | beam {fea_r['beam_max_util']:.2f} | column "
                f"{fea_r['column_max_interaction']:.2f} | {fea_r['nodes']} nodes, "
                f"{fea_r['solve_ms']} ms")
        elif fea_r.get("skipped"):
            st.info("OpenSees FEA skipped: " + fea_r["skipped"])
        elif fea_r.get("error"):
            st.warning("OpenSees FEA failed: " + fea_r["error"])

        st.markdown("**Code checks**")
        import pandas as pd
        chk = pd.DataFrame([{
            "Check": x["name"], "Value": x["value"], "Unit": x["unit"],
            "Limit": x["limit"],
            "Result": "✅ PASS" if x["passed"] else "❌ FAIL",
            "Detail": x["detail"],
        } for x in a["checks"]])
        st.dataframe(chk, width="stretch", hide_index=True)

        failed = [x for x in a["checks"] if not x["passed"]]
        if failed:
            st.warning(f"{len(failed)} check(s) still failing: "
                       + ", ".join(x["name"] for x in failed))

        g1, g2 = st.columns(2)
        plan = out_dir / f"design_{d['id']}_plan.png"
        elev = out_dir / f"design_{d['id']}_elevation.png"
        if plan.exists():
            g1.image(str(plan), caption="Floor plan")
        if elev.exists():
            g2.image(str(elev), caption="Elevation")

        g3, g4 = st.columns(2)
        seis = out_dir / f"design_{d['id']}_seismic.png"
        png3d = out_dir / f"design_{d['id']}_3d.png"
        if seis.exists():
            g3.image(str(seis), caption="Seismic forces + drift")
        if png3d.exists():
            g4.image(str(png3d), caption="3D massing")

        html3d = out_dir / f"design_{d['id']}_3d.html"
        if html3d.exists():
            st.markdown("**Interactive 3D** (rotate: drag, zoom: scroll)")
            try:
                import streamlit.components.v1 as components
                components.html(html3d.read_text(encoding="utf-8"),
                                height=520, scrolling=False)
            except Exception as exc:
                st.warning(f"3D viewer failed ({exc}) - open the file manually: "
                           f"`{html3d}`")

        with st.expander("Column schedule & seismic detail"):
            st.markdown("**Column schedule (bottom → top)**")
            st.dataframe(pd.DataFrame(a["members"]["columns"]),
                         width="stretch", hide_index=True)
            st.markdown("**Storey forces (IS 1893 equivalent static)**")
            st.dataframe(pd.DataFrame(a["seismic"]["storey_forces"]),
                         width="stretch", hide_index=True)
            st.markdown("**Wind (IS 875-3)**")
            st.json(a["wind"])
            if a.get("fea", {}).get("ok"):
                st.markdown("**OpenSees FEA detail (Phase 2)**")
                st.json(a["fea"])

# ---------------------------------------------------------------------------
# optimisation tab
# ---------------------------------------------------------------------------
with tab_opt:
    import pandas as pd
    any_fix = False
    for r in results:
        if r["fixes"]:
            any_fix = True
            st.markdown(f"### {r['design']['id']} - {r['design']['name']}")
            st.dataframe(pd.DataFrame([{
                "Iter": f["iteration"], "Failed check": f["issue"],
                "Before": f["before"], "Action": f["action"],
            } for f in r["fixes"]]), width="stretch", hide_index=True)
    if not any_fix:
        st.success("No design needed optimisation - every check passed on "
                   "the first analysis.")
    st.caption("Phase-1 rule-based re-test loop: each fix is an engineering "
               "action followed by a full re-analysis. A 'GA' row (if present) "
               "is the Phase-3A genetic grid search that ran first; its full "
               "stats are in the report's section 4.")

# ---------------------------------------------------------------------------
# report tab
# ---------------------------------------------------------------------------
with tab_rep:
    st.markdown("### Downloads")
    d1, d2, d3 = st.columns(3)
    pdf_path = out_dir / "report.pdf"
    if pdf_path.exists():
        d1.download_button("⬇  PDF report",
                           data=pdf_path.read_bytes(),
                           file_name="building_design_report.pdf",
                           mime="application/pdf", width="stretch")
    elif summary.get("pdf_error"):
        d1.error(f"PDF failed: {summary['pdf_error']}")
    else:
        d1.info("PDF not generated (see run settings)")
    d2.download_button("⬇  summary.json",
                       data=json.dumps(summary, indent=2, default=str),
                       file_name="summary.json", mime="application/json",
                       width="stretch")
    for did in ("A", "B", "C"):
        h = out_dir / f"design_{did}_3d.html"
        if h.exists() and d3.download_button(
                f"⬇  3D model ({did})", data=h.read_bytes(),
                file_name=f"design_{did}_3d.html", mime="text/html",
                width="stretch"):
            break

    ifc_files = sorted((out_dir / "ifc").glob("*.ifc")) \
        if (out_dir / "ifc").exists() else []
    if ifc_files:
        i1, i2, i3 = st.columns(3)
        for p, col in zip(ifc_files, (i1, i2, i3), strict=False):
            col.download_button(f"⬇  IFC {p.stem.replace('design_', '')}",
                                data=p.read_bytes(), file_name=p.name,
                                mime="application/x-step", width="stretch")
    else:
        st.info("IFC models not generated (see run settings)")

    st.markdown("### Output files")
    for p in sorted(out_dir.glob("*")):
        st.write(f"`{p}`  ({p.stat().st_size / 1024:.0f} KB)")
    for p in sorted(out_dir.glob("ifc/*.ifc")):
        st.write(f"`{p}`  ({p.stat().st_size / 1024:.0f} KB)")

    st.warning(cfg.DISCLAIMER)
