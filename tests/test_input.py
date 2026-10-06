"""Input parsing + requirement analysis tests."""
import pytest

from modules import input_handler, requirement_analyzer
from modules.models import Requirements

# ---------------------------------------------------------------------------
# parse_text
# ---------------------------------------------------------------------------

def test_floors_g_plus_n():
    out = input_handler.parse_text("G+5 apartment in Pune")
    assert out["floors"] == 6


def test_floors_storey_word():
    out = input_handler.parse_text("a 10-storey office in Delhi")
    assert out["floors"] == 10


def test_city_detected_case_insensitive():
    out = input_handler.parse_text("residential tower, mumbai, 4 floors")
    assert out["city"].lower() == "mumbai"


def test_budget_crore():
    out = input_handler.parse_text("budget 10 crore")
    assert out["budget_crores"] == pytest.approx(10.0)


def test_budget_lakh():
    out = input_handler.parse_text("budget 250 lakh")
    assert out["budget_crores"] == pytest.approx(2.5)


def test_building_type_priority_hospital_wins_over_school():
    out = input_handler.parse_text("hospital and school building")
    assert out["building_type"] == "hospital"


def test_units_per_floor():
    out = input_handler.parse_text("10 floor residential with 4 units per floor")
    assert out["units_per_floor"] == 4
    assert out["floors"] == 10


def test_land_sqft_and_acres():
    assert input_handler.parse_text("plot 5000 sqft")["land_area_sqft"] == 5000
    assert input_handler.parse_text("plot 1 acre")["land_area_sqft"] == pytest.approx(43560)


def test_special_keywords():
    out = input_handler.parse_text("with parking and green roof")
    assert "parking" in out["special"]
    assert "green_roof" in out["special"]


def test_zone_override():
    out = input_handler.parse_text("seismic zone iv building")
    assert out["seismic_zone"] == "IV"


def test_soil_and_terrain():
    out = input_handler.parse_text("on soft clay, sea face")
    assert out["soil_type"] == "III"
    assert out["terrain_cat"] == 1


def test_floor_height():
    out = input_handler.parse_text("storey height 3.6 m")
    assert out["floor_h_m"] == pytest.approx(3.6)


# ---------------------------------------------------------------------------
# parse_dict / load
# ---------------------------------------------------------------------------

def test_parse_dict_rejects_unknown_type():
    with pytest.raises(ValueError):
        input_handler.parse_dict({"building_type": "spaceship"})


def test_parse_dict_rejects_unknown_zone():
    with pytest.raises(ValueError):
        input_handler.parse_dict({"seismic_zone": "IX"})


def test_parse_dict_rejects_non_finite_numbers():
    # regression: float("nan") used to slip through and poison every later
    # stage (cost ratios, surrogate rows, JSON summaries)
    for key in ("budget_crores", "land_area_sqft", "floor_h_m", "vb"):
        with pytest.raises(ValueError):
            input_handler.parse_dict({key: float("nan")})
        with pytest.raises(ValueError):
            input_handler.parse_dict({key: float("inf")})


def test_parse_dict_ignores_unknown_keys():
    out = input_handler.parse_dict({"city": "Pune", "hacker_key": 1})
    assert out == {"city": "Pune"}


def test_load_text_only():
    req = input_handler.load(text="8 floor school in Chennai, budget 12 crore")
    assert req.building_type == "school"
    assert req.city.lower() == "chennai"
    assert req.floors == 8
    assert req.budget_crores == pytest.approx(12)


def test_load_dict_wins_over_text():
    req = input_handler.load(text="office in Delhi", data={"city": "Kolkata"})
    assert req.city.lower() == "kolkata"


def test_defaults_when_empty():
    req = input_handler.load()
    assert isinstance(req, Requirements)
    assert req.building_type == "residential"
    assert req.floors >= 1


# ---------------------------------------------------------------------------
# requirement_analyzer
# ---------------------------------------------------------------------------

def test_city_resolves_zone_and_wind():
    req = requirement_analyzer.analyze(input_handler.load(text="building in Delhi"))
    assert req.seismic_zone == "IV"
    assert req.vb == 47
    assert req.terrain_cat == 4


def test_unknown_city_gets_defaults_and_warning():
    req = requirement_analyzer.analyze(
        input_handler.load(data={"city": "Atlantis"}))
    assert req.seismic_zone == "III"
    assert any("not in database" in w for w in req.warnings)


def test_land_default_when_nothing_given():
    req = requirement_analyzer.analyze(input_handler.load(text="residential"))
    assert req.land_area_sqft and req.land_area_sqft > 0


def test_floors_clamped():
    req = requirement_analyzer.analyze(input_handler.load(
        data={"floors": 500}))
    assert req.floors == 60
    assert any("clamped" in w for w in req.warnings)


def test_tall_building_warning():
    req = requirement_analyzer.analyze(input_handler.load(text="20 floor residential"))
    assert any("tall-building regime" in w for w in req.warnings)


def test_specials_deduped_and_sorted():
    req = requirement_analyzer.analyze(input_handler.load(
        text="parking parking and solar panels"))
    assert req.special == sorted(set(req.special))
    assert "parking" in req.special


def test_importance_hint_school():
    req = requirement_analyzer.analyze(input_handler.load(text="school building"))
    assert any("importance factor" in w for w in req.warnings)


# ---------------------------------------------------------------------------
# regressions (round-2 bug hunt)
# ---------------------------------------------------------------------------

def test_storey_word_keeps_residential_type():
    # keyword boundaries: "storey" must not trip building-type keywords
    out = input_handler.parse_text("10 storey residential building in Delhi")
    assert out["building_type"] == "residential"
    assert out["floors"] == 10


def test_hyphenated_storey_apartment():
    out = input_handler.parse_text("12-storey apartment in Bengaluru")
    assert out["building_type"] == "residential"
    assert out["floors"] == 12


def test_storage_keyword_still_detects_warehouse():
    # boundary fix must not lose whole-word keywords
    out = input_handler.parse_text("workshop and storage godown")
    assert out["building_type"] == "warehouse"


def test_comma_numbers_budget_and_land():
    out = input_handler.parse_text(
        "2000 sqft plot, budget Rs 2,50,00,000 for a shop")
    assert out["budget_crores"] == pytest.approx(2.5)
    assert out["land_area_sqft"] == pytest.approx(2000)
    assert out["building_type"] == "retail"


def test_specials_union_text_and_dict():
    # dict special used to REPLACE the brief's; now they union
    req = input_handler.load(
        "10 storey residential building with parking in Pune",
        {"special": ["solar_panels"]})
    assert req.special == ["parking", "solar_panels"]


def test_vb_out_of_range_falls_back_with_warning():
    req = requirement_analyzer.analyze(Requirements(raw_text="", vb=999))
    assert 30.0 <= req.vb <= 60.0
    assert any("outside the IS 875 range" in w for w in req.warnings)


def test_vb_zero_falls_back():
    req = requirement_analyzer.analyze(Requirements(raw_text="", vb=0))
    assert 30.0 <= req.vb <= 60.0


def test_vb_in_range_kept_without_warning():
    req = requirement_analyzer.analyze(Requirements(raw_text="", vb=47))
    assert req.vb == 47
    assert not any("outside the IS 875 range" in w for w in req.warnings)
