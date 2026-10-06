"""Phase-2d: certified EnergyPlus engine (IDF build, weather, sql, wiring).

Hermetic by default - no EnergyPlus binary, no network. The single real
end-to-end test skips itself when the repo-local install or the cached
Mumbai EPW is missing (CI machines do not ship the ~2 GB install).
"""
from __future__ import annotations

import contextlib
import re
import shutil
import sqlite3
import zlib
from pathlib import Path

import pytest

import config as cfg
from modules import (
    design_generator,
    energy_model,
    input_handler,
    pipeline,
    report_generator,
)
from modules import (
    energyplus_engine as E,
)


@pytest.fixture(scope="module")
def brief():
    req = input_handler.load(
        "Residential apartment in Mumbai, G+4, plot 1500 sqft", None)
    req.city = "mumbai"
    d = design_generator.generate(req, seed=7)[0]
    return req, d


@pytest.fixture(scope="module")
def energy(brief):
    req, d = brief
    return energy_model.model(req, d)


@pytest.fixture(scope="module")
def idf_text(brief, energy):
    req, d = brief
    return E.build_idf(req, d, energy.get("assumptions") or {}, "26.2")


def _pdf_text(path: Path) -> str:
    """Decompress PDF content streams (reportlab/fpdf text lives there)."""
    out = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream",
                         path.read_bytes(), re.S):
        with contextlib.suppress(zlib.error):
            out.append(zlib.decompress(m.group(1)))
    return b" ".join(out).decode("latin-1", "ignore")


# ---------------------------------------------------------------------------
# IDF construction
# ---------------------------------------------------------------------------

def test_idf_required_objects(idf_text):
    assert "Version,26.2;" in idf_text
    assert "RunPeriod," in idf_text, "explicit RunPeriod required (fatal without)"
    assert "Output:SQLite, SimpleAndTabular;" in idf_text
    assert "WindowMaterial:SimpleGlazingSystem" in idf_text
    assert "ZoneHVAC:IdealLoadsAirSystem" in idf_text
    assert "Timestep," in idf_text


def test_idf_simulation_control_seven_fields(idf_text):
    m = re.search(r"SimulationControl,\s*([^;]+);", idf_text, re.S)
    assert m, "SimulationControl block missing"
    body = re.sub(r"!.*", "", m.group(1))
    fields = [f.strip() for f in body.split(",") if f.strip()]
    assert len(fields) == 7
    assert fields[4] == "Yes", "A5 (weather-file run periods) must be Yes"


def test_idf_run_period_full_year(idf_text):
    m = re.search(r"RunPeriod,\s*([^;]+);", idf_text, re.S)
    assert m
    body = re.sub(r"!.*", "", m.group(1))
    assert "1," in body and "12," in body      # 1 Jan .. 31 Dec


def test_idf_absolute_loads_match_preliminary(brief, energy, idf_text):
    """Watts must cover the plate x floors program, not one floor."""
    _, d = brief
    area = d.plate_sqm * d.floors
    assump = energy["assumptions"]
    lights = re.search(r"^\s*([0-9.]+),\s*!- Lighting Level", idf_text, re.M)
    equip = re.search(r"^\s*([0-9.]+),\s*!- Design Level", idf_text, re.M)
    people = re.search(r"^\s*([0-9.]+),\s*!- Number of People", idf_text, re.M)
    assert lights and equip and people
    assert float(lights.group(1)) == pytest.approx(assump["lpd_wm2"] * area)
    assert float(equip.group(1)) == pytest.approx(assump["plug_wm2"] * area)
    assert float(people.group(1)) == pytest.approx(0.05 * area)


def test_idf_is_single_zone_ribbon_model(idf_text):
    assert idf_text.count("BuildingSurface:Detailed") >= 5   # base+walls+roof
    assert "FenestrationSurface:Detailed" in idf_text


# ---------------------------------------------------------------------------
# scope / units / weather (hermetic)
# ---------------------------------------------------------------------------

def test_in_scope():
    assert E.in_scope(1, "winner") is True
    assert E.in_scope(2, "winner") is False
    assert E.in_scope(2, "all") is True
    assert E.in_scope(1, "off") is False
    # undocumented / typo values fall back to the default (winner-only)
    assert E.in_scope(1, "never") is True
    assert E.in_scope(2, "never") is False


def test_unit_factor_energy_only():
    assert E._unit_factor("GJ") == pytest.approx(277.7778)
    assert E._unit_factor("MJ") == pytest.approx(0.277778)
    assert E._unit_factor("kWh") == 1.0
    assert E._unit_factor(" m3 ") is None, "water row must not enter totals"
    assert E._unit_factor("W") is None


def test_resolve_weather_cache_hit(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "ENERGYPLUS_WEATHER_DIR", tmp_path)
    epw = tmp_path / "mumbai.epw"
    epw.write_bytes(b"x" * 20_000)               # resolver ignores tiny files
    p, info = E.resolve_weather("mumbai", allow_download=False)
    assert p == epw
    assert info["kind"] == "cached"


def test_resolve_weather_unknown_city_no_network(monkeypatch):
    def boom(*_a, **_k):                        # download must never happen
        raise AssertionError("network hit for unknown city")
    monkeypatch.setattr(E, "_fetch", boom)
    p, info = E.resolve_weather("zzz-nowhere-city", allow_download=True)
    assert p is None
    assert "zzz-nowhere-city" in info.get("reason", "")


def test_exe_version_unreadable_returns_unknown():
    assert E.exe_version(str(Path(__file__) / "no-such-exe")) == "unknown"


def test_simulate_missing_binary_fails_soft(monkeypatch, tmp_path, brief, energy):
    monkeypatch.setattr(E, "find_energyplus", lambda: None)
    req, d = brief
    out = E.simulate(req, d, energy, tmp_path)
    assert out["ok"] is False
    assert "binary" in out["reason"].lower()


# ---------------------------------------------------------------------------
# sql summarise (fake eplusout.sql, regression: double-count + water rows)
# ---------------------------------------------------------------------------

def _fake_sql(path: Path, rows: list[tuple[str, str, str, str]]) -> None:
    con = sqlite3.connect(str(path))
    try:
        con.execute("CREATE TABLE Raw (RowName TEXT, ColumnName TEXT,"
                    " Units TEXT, Value TEXT)")
        con.executemany("INSERT INTO Raw VALUES (?,?,?,?)", rows)
        con.execute(
            "CREATE VIEW TabularDataWithStrings AS SELECT"
            " 'AnnualBuildingUtilityPerformanceSummary' AS ReportName,"
            " 'End Uses' AS TableName, RowName, ColumnName, Units, Value"
            " FROM Raw")
        con.commit()
    finally:
        con.close()


def test_summarise_skips_total_and_water_rows(tmp_path):
    _fake_sql(tmp_path / "eplusout.sql", [
        ("Cooling", "District Cooling", "GJ", "10.0"),
        ("Interior Lighting", "Electricity", "GJ", "5.0"),
        ("Interior Equipment", "Electricity", "GJ", "2.0"),
        ("Total End Uses", "District Cooling", "GJ", "10.0"),   # double-count
        ("Total End Uses", "Electricity", "GJ", "7.0"),
        ("Cooling", "Water", "m3", "99.0"),                     # non-energy
        ("Time of Peak", "Electricity", "GJ", "0.1"),           # label row
    ])
    out = E._summarise(tmp_path, 100.0, 6.5)
    assert out["annual_kwh"] == round(17 * 277.7778)
    assert out["eui_kwh_m2yr"] == pytest.approx(17 * 277.7778 / 100, abs=0.1)
    assert out["breakdown_kwh"]["District Cooling"] == round(10 * 277.7778)
    assert "Water" not in out["breakdown_kwh"]
    assert out["units_read"] == ["GJ"]
    assert out["annual_cost_inr"] == round(17 * 277.7778 * 6.5)


def test_summarise_missing_sql_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        E._summarise(tmp_path, 100.0, 6.5)


def test_summarise_empty_table_raises(tmp_path):
    _fake_sql(tmp_path / "eplusout.sql", [])
    with pytest.raises(ValueError):
        E._summarise(tmp_path, 100.0, 6.5)


# ---------------------------------------------------------------------------
# pipeline wiring
# ---------------------------------------------------------------------------

def test_pipeline_energyplus_off(tmp_path):
    summary = pipeline.run(text="3 floor house in Pune",
                           out_dir=tmp_path / "run",
                           make_pdf=False, make_images=False, make_ifc=False,
                           seed=1, genetic=False, energyplus=False)
    assert summary["energyplus"] == {"enabled": False}
    for r in summary["results"]:
        assert "energyplus" not in (r["energy"] or {})


def test_pipeline_energyplus_winner_only(tmp_path, monkeypatch):
    calls: list[Path] = []

    def fake_simulate(req, d, dd, work_dir, allow_download=None):
        calls.append(work_dir)
        return {"ok": True, "eui_kwh_m2yr": 123.4, "annual_kwh": 50000}

    monkeypatch.setattr(pipeline.energyplus_engine, "simulate", fake_simulate)
    summary = pipeline.run(text="3 floor house in Pune",
                           out_dir=tmp_path / "run",
                           make_pdf=False, make_images=False, make_ifc=False,
                           seed=1, genetic=False, energyplus=True)
    assert len(calls) == 1                          # scope=winner by default
    runs = summary["energyplus"]["runs"]
    assert runs == [{"id": summary["results"][0]["design"]["id"],
                     "ok": True, "eui_kwh_m2yr": 123.4, "reason": None}]
    winner = summary["results"][0]
    assert winner["energy"]["energyplus"]["ok"] is True
    for r in summary["results"][1:]:
        assert "energyplus" not in r["energy"]


def test_pipeline_energyplus_error_isolated(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(pipeline.energyplus_engine, "simulate", boom)
    summary = pipeline.run(text="3 floor house in Pune",
                           out_dir=tmp_path / "run",
                           make_pdf=False, make_images=False, make_ifc=False,
                           seed=1, genetic=False, energyplus=True)
    run = summary["energyplus"]["runs"][0]
    assert run["ok"] is False
    assert "RuntimeError: boom" in run["reason"]
    assert summary["results"][0]["energy"]["eui_kwh_m2yr"] > 0  # prelim kept


def test_pipeline_energyplus_failed_run_keeps_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline.energyplus_engine, "simulate",
                        lambda *a, **k: {"ok": False, "reason": "no EPW weather"})
    summary = pipeline.run(text="3 floor house in Pune",
                           out_dir=tmp_path / "run",
                           make_pdf=False, make_images=False, make_ifc=False,
                           seed=1, genetic=False, energyplus=True)
    assert summary["energyplus"]["runs"][0]["reason"] == "no EPW weather"


# ---------------------------------------------------------------------------
# report rendering
# ---------------------------------------------------------------------------

_OK_EP = {"ok": True, "engine": "energyplus-26.2", "eui_kwh_m2yr": 172.3,
          "annual_kwh": 124072, "annual_cost_inr": 806470,
          "tariff_inr_kwh": 6.5,
          "breakdown_kwh": {"District Cooling": 93469, "Electricity": 30600},
          "weather": {"city": "mumbai", "source": "cache"}, "runtime_s": 1.0}


def test_report_renders_certified_block(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline.energyplus_engine, "simulate",
                        lambda *a, **k: dict(_OK_EP))
    summary = pipeline.run(text="3 floor house in Pune",
                           out_dir=tmp_path / "run",
                           make_pdf=False, make_images=False, make_ifc=False,
                           seed=1, genetic=False, energyplus=True)
    pdf = report_generator.build_report(summary, {}, tmp_path / "r1.pdf")
    text = _pdf_text(pdf)
    # PDF strings escape parens as \( \) - assert on the unescaped parts
    assert "Certified simulation" in text
    assert "EnergyPlus 26.2" in text
    assert "End uses:" in text


def test_report_renders_skipped_note(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline.energyplus_engine, "simulate",
                        lambda *a, **k: {"ok": False, "reason": "no EPW weather"})
    summary = pipeline.run(text="3 floor house in Pune",
                           out_dir=tmp_path / "run",
                           make_pdf=False, make_images=False, make_ifc=False,
                           seed=1, genetic=False, energyplus=True)
    pdf = report_generator.build_report(summary, {}, tmp_path / "r2.pdf")
    assert "Certified EnergyPlus run: skipped - no EPW weather" in _pdf_text(pdf)


def test_summary_json_serialisable(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(pipeline.energyplus_engine, "simulate",
                        lambda *a, **k: dict(_OK_EP))
    summary = pipeline.run(text="3 floor house in Pune",
                           out_dir=tmp_path / "run",
                           make_pdf=False, make_images=False, make_ifc=False,
                           seed=1, genetic=False, energyplus=True)
    json.dumps(summary, default=str)               # must not raise


# ---------------------------------------------------------------------------
# real end-to-end (skips without the local install + cached EPW)
# ---------------------------------------------------------------------------

def test_real_mumbai_run(tmp_path, brief, energy):
    exe = energy_model.find_energyplus()
    epw = cfg.ENERGYPLUS_WEATHER_DIR / "mumbai.epw"
    if not exe or not epw.is_file():
        pytest.skip("EnergyPlus install / cached mumbai.epw missing")
    req, d = brief
    # relative work_dir on purpose: regress the subprocess-cwd bug where a
    # relative IDF path resolved against work_dir and E+ could not find it
    rel = Path("output") / "_test_energyplus_rel"
    shutil.rmtree(rel, ignore_errors=True)
    try:
        out = E.simulate(req, d, energy, rel, allow_download=False)
        assert out["ok"], out.get("reason")
        # lights 5 + plug 12 W/m2 x 720 m2 x 2500 h = 30,600 kWh (parity)
        elec = out["breakdown_kwh"].get("Electricity", 0)
        assert elec == pytest.approx(30600, rel=0.02)
        assert out["annual_kwh"] > 0
        assert out["eui_kwh_m2yr"] > 0
    finally:
        shutil.rmtree(rel, ignore_errors=True)
