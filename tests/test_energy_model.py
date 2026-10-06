"""Phase-2 preliminary energy model (ECBC-aligned degree-day)."""
import pytest

from modules import energy_model, input_handler, pipeline, requirement_analyzer
from modules.design_generator import generate
from modules.energy_model import find_energyplus, model
from modules.models import Design


def _req(text="3 floor house in Pune"):
    return requirement_analyzer.analyze(input_handler.load(text=text))


def _design(bays=4, floors=3, bay=5.0, fh=3.0) -> Design:
    return Design(id="A", name="T", system="rc_frame", bay_x_m=bay,
                  bay_y_m=bay, bays_x=bays, bays_y=bays, floors=floors,
                  floor_h_m=fh)


# ---------------------------------------------------------------------------
# hand oracle: geometry -> UA -> loads, independent literals
# ---------------------------------------------------------------------------

def test_formula_oracle_residential_mumbai():
    # d: 4x4 bays @5m, 3 floors x 3m -> len 20x20, plate 400, area 1200
    # perim 80, wall 80x9=720, roof 400, win 0.25*720=180
    # UA = 0.8*720 + 0.45*400 + 2.8*180 = 1260 W/K
    req = _req("3 floor house in Mumbai")
    e = model(req, _design())
    assert e["area_sqm"] == pytest.approx(1200.0, rel=1e-6)
    lighting = 5.0 * 1200 * 2500 / 1000.0            # 15000
    plug = 12.0 * 1200 * 2500 / 1000.0               # 36000
    cool_env = 1260 * 3600 * 24 / 1000.0 / 3.4       # mumbai CDD24=3600
    cool_solar = 180 * 0.40 * 130.0 / 3.4
    cool_int = (5.0 + 12.0) * 1200 * 2500 * 0.65 / 1000.0 / 3.4
    cooling = cool_env + cool_solar + cool_int
    fans = 0.12 * cooling
    heating = 0.0                                    # mumbai HDD16=0
    total = lighting + plug + cooling + fans + heating
    bd = e["breakdown_kwh"]
    assert bd["lighting"] == pytest.approx(lighting, rel=0.01)
    assert bd["plug_appliances"] == pytest.approx(plug, rel=0.01)
    assert bd["cooling"] == pytest.approx(cooling, rel=0.01)
    assert bd["fans_pumps"] == pytest.approx(fans, rel=0.01)
    assert bd["heating"] == 0
    assert e["annual_kwh"] == pytest.approx(total, rel=0.01)
    assert e["eui_kwh_m2yr"] == pytest.approx(total / 1200.0, rel=0.01)
    # residential tariff discount (6.5 vs 8.0 default)
    assert e["tariff_inr_kwh"] == 6.5
    assert e["annual_cost_inr"] == pytest.approx(total * 6.5, rel=0.01)


def test_breakdown_sums_to_annual():
    req = _req("5 floor office in Delhi")
    e = model(req, _design(bays=5, floors=5))
    assert sum(e["breakdown_kwh"].values()) == pytest.approx(
        e["annual_kwh"], abs=3)          # per-component rounding only


def test_climate_lookup_and_default():
    req = _req("building in Delhi")
    e = model(req, _design())
    assert e["climate"]["cdd24"] == 2800
    assert e["climate"]["hdd16"] == 400
    req2 = requirement_analyzer.analyze(
        input_handler.load(data={"city": "Atlantis"}))
    e2 = model(req2, _design())
    assert e2["climate"] == {"city": "Atlantis", "design_db_c": 38,
                             "cdd24": 2800, "hdd16": 250}


def test_reasonable_eui_bands():
    for text, lo, hi in (
            ("10 floor residential building in Mumbai", 30, 150),
            ("10 floor office in Mumbai", 60, 250),
            ("5 floor hospital in Delhi", 200, 500),
            ("school building in Bengaluru", 20, 120)):
        e = model(_req(text), _design(bays=5, floors=5))
        assert lo <= e["eui_kwh_m2yr"] <= hi, (
            f"{text}: EUI {e['eui_kwh_m2yr']} outside [{lo}, {hi}]")


def test_bigger_building_uses_more_energy():
    small = model(_req(), _design(bays=4, floors=3))
    big = model(_req(), _design(bays=8, floors=8))
    assert big["annual_kwh"] > small["annual_kwh"]
    assert big["area_sqm"] > small["area_sqm"]


def test_engine_and_note_fields():
    e = model(_req(), _design())
    assert e["engine"] == energy_model.ENGINE
    assert "degree-day" in e["note"]
    assert e["assumptions"]["eer"] == 3.40


# ---------------------------------------------------------------------------
# EnergyPlus detection (swap-in hook; binary may legitimately be absent)
# ---------------------------------------------------------------------------

def test_energyplus_detection_explicit_dir(tmp_path):
    find_energyplus.cache_clear()
    exe = tmp_path / ("energyplus.exe" if __import__("os").name == "nt"
                      else "energyplus")
    exe.write_text("stub")
    try:
        assert find_energyplus(str(tmp_path)) == str(exe)
    finally:
        find_energyplus.cache_clear()


def test_energyplus_absent_returns_none_or_path():
    find_energyplus.cache_clear()
    out = find_energyplus()
    assert out is None or isinstance(out, str)


# ---------------------------------------------------------------------------
# pipeline integration
# ---------------------------------------------------------------------------

def test_pipeline_results_include_energy(tmp_path):
    s = pipeline.run(text="3 floor house in Pune", out_dir=tmp_path,
                     make_pdf=False, make_images=False, genetic=False)
    for r in s["results"]:
        en = r["energy"]
        assert en and en["annual_kwh"] > 0
        assert 10 <= en["eui_kwh_m2yr"] <= 500
        assert en["engine"] == energy_model.ENGINE
        assert r["rank"] in (1, 2, 3)


def test_generated_designs_all_modelled():
    req = _req("10 floor office in Chennai")
    for d in generate(req):
        e = model(req, d)
        assert e["annual_kwh"] > 0
        assert e["climate"]["cdd24"] == 3700       # chennai
