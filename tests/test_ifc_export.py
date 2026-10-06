"""Phase-3B: IFC4 export - GUID scheme, STEP structure, validation, wiring."""
from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path

import pytest

from modules import (
    design_generator,
    input_handler,
    pipeline,
    requirement_analyzer,
)
from modules.ifc_export import compress_guid, export
from modules.models import Requirements
from modules.structural_analyzer import analyze

_BRIEF = "5 floor office in Mumbai"


def _count(text: str, entity: str) -> int:
    return len(re.findall(rf"^#\d+={entity}\(", text, re.M))


def _refs(text: str) -> set[str]:
    return set(re.findall(r"#(\d+)", text))


def _defs(text: str) -> set[str]:
    return set(re.findall(r"^#(\d+)=", text, re.M))


@pytest.fixture(scope="module")
def built():
    """One generated design + analysis + its exported IFC file."""
    req = requirement_analyzer.analyze(input_handler.load(text=_BRIEF))
    d = design_generator.generate(req)[0]
    a = analyze(d, req)
    return req, d, a


@pytest.fixture(scope="module")
def sample(built, tmp_path_factory):
    req, d, a = built
    out = tmp_path_factory.mktemp("ifc") / "design_A.ifc"
    p = export(d.to_dict(), a, req, out, score=70.0,
               cost={"total_inr": 10_000_000},
               energy={"eui_kwh_m2yr": 99.5}, timestamp=1_700_000_000)
    return req, d, a, p, p.read_text(encoding="ascii")


@pytest.fixture(scope="module")
def full_flags(built, tmp_path_factory):
    """Design with core + secondary beams forced on (all element types)."""
    req, _d0, _ = built
    d = design_generator.generate(req)[0]
    d.core = True
    d.core_lx_m = max(6.0, min(10.0, 0.35 * d.len_x_m))
    d.core_ly_m = max(6.0, min(10.0, 0.35 * d.len_y_m))
    # generator only sets secondary=True with a real sec depth (bay > 6 m);
    # this fixture forces both halves of that invariant on explicitly
    d.secondary = True
    d.sec_beam_d_mm = design_generator._beam_d(d.bay_x_m / 2) or 300
    a = analyze(d, req)
    out = tmp_path_factory.mktemp("ifc_full") / "design_full.ifc"
    p = export(d.to_dict(), a, req, out, timestamp=1_700_000_000)
    return d, a, p, p.read_text(encoding="ascii")


# ---------------------------------------------------------------------------
# GUID scheme
# ---------------------------------------------------------------------------

def test_guid_format_and_charset():
    g = compress_guid("project|A")
    assert len(g) == 22
    alphabet = set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                   "abcdefghijklmnopqrstuvwxyz_$")
    assert set(g) <= alphabet
    assert g[0] in "0123"           # 128 bits -> top char holds 2 bits
    assert compress_guid("project|A") == g        # deterministic
    assert compress_guid("project|B") != g        # distinct seeds


def test_guid_matches_ifcopenshell_reference():
    pytest.importorskip("ifcopenshell")
    import ifcopenshell.guid as ifcguid

    for seed in ("a", "project|A", "slab|A|3", "col|B|2|5|9", "pset|C"):
        digest = hashlib.md5(seed.encode("utf-8")).digest()
        ref = ifcguid.compress(str(uuid.UUID(bytes=digest)))
        assert compress_guid(seed) == ref


# ---------------------------------------------------------------------------
# STEP file structure
# ---------------------------------------------------------------------------

def test_header_and_footer(sample):
    _, _, _, _, text = sample
    assert text.startswith("ISO-10303-21;\nHEADER;\n")
    assert "FILE_SCHEMA(('IFC4'));" in text
    assert text.rstrip().endswith("END-ISO-10303-21;")
    assert "FILE_DESCRIPTION(('ViewDefinition [ReferenceView]'),'2;1');" in text


def test_referential_integrity(sample):
    _, _, _, _, text = sample
    defs = _defs(text)
    assert len(defs) > 100
    missing = _refs(text) - defs
    assert not missing, f"dangling refs: {sorted(missing)[:10]}"


def test_spatial_tree_counts_and_storeys(sample):
    _, d, _, _, text = sample
    assert _count(text, "IFCPROJECT") == 1
    assert _count(text, "IFCSITE") == 1
    assert _count(text, "IFCBUILDING") == 1
    assert _count(text, "IFCBUILDINGSTOREY") == d.floors
    assert _count(text, "IFCSLAB") == d.floors
    elevs = [float(e) for e in re.findall(
        r"IFCBUILDINGSTOREY\(.*?\.ELEMENT\.,([\d.]+)\)", text)]
    assert len(elevs) == d.floors
    assert elevs == sorted(elevs) and elevs[0] == 0.0
    for i, e in enumerate(elevs):
        assert e == pytest.approx(i * d.floor_h_m)


def test_element_counts(sample):
    _, d, _, _, text = sample
    cols = (d.bays_x + 1) * (d.bays_y + 1)
    assert _count(text, "IFCCOLUMN") == cols * d.floors
    assert _count(text, "IFCFOOTING") == cols            # ground level only
    if d.secondary:
        sec = 4 * d.bays_x * d.bays_y
    else:
        sec = 0
    per_floor = (d.bays_y + 1) * d.bays_x + (d.bays_x + 1) * d.bays_y + sec
    assert _count(text, "IFCBEAM") == per_floor * d.floors
    assert _count(text, "IFCWALL") == 0                  # no core in _BRIEF
    assert _count(text, "IFCSHAPEREPRESENTATION") > 0
    assert _count(text, "IFCEXTRUDEDAREASOLID") > 0


def test_full_flags_core_secondary(full_flags):
    d, _, _, text = full_flags
    assert _count(text, "IFCWALL") == 4 * d.floors
    sec = 4 * d.bays_x * d.bays_y
    per_floor = (d.bays_y + 1) * d.bays_x + (d.bays_x + 1) * d.bays_y + sec
    assert _count(text, "IFCBEAM") == per_floor * d.floors
    # referential integrity also holds for the bigger file
    assert not (_refs(text) - _defs(text))


def test_pset_building_sim(sample):
    _, _, _, _, text = sample
    assert "IFCPROPERTYSET(" in text
    assert "'Pset_BuildingSim'" in text
    assert "IFCREAL(70.)" in text                       # score
    assert "IFCLABEL('INR 10,000,000')" in text         # cost
    assert "IFCREAL(99.5)" in text                      # EUI
    assert "IFCRELDEFINESBYPROPERTIES(" in text


def test_deterministic_same_path(built, tmp_path):
    req, d, a = built
    out = tmp_path / "det.ifc"
    export(d.to_dict(), a, req, out, score=70.0,
           cost={"total_inr": 1}, timestamp=1_700_000_000)
    first = out.read_bytes()
    export(d.to_dict(), a, req, out, score=70.0,
           cost={"total_inr": 1}, timestamp=1_700_000_000)
    assert out.read_bytes() == first


def test_export_edge_minimal(tmp_path):
    """Minimal design dict + empty analysis must still export cleanly."""
    d = {"id": "X", "floors": 1, "floor_h_m": 3.0,
         "len_x_m": 6.0, "len_y_m": 6.0, "bay_x_m": 6.0, "bay_y_m": 6.0,
         "bays_x": 1, "bays_y": 1, "slab_t_mm": 125,
         "beam_w_mm": 300, "beam_d_mm": 400}
    p = export(d, {}, Requirements(), tmp_path / "min.ifc",
               timestamp=1_700_000_000)
    text = p.read_text(encoding="ascii")
    assert not (_refs(text) - _defs(text))
    assert _count(text, "IFCBUILDINGSTOREY") == 1
    assert _count(text, "IFCCOLUMN") == 4
    assert _count(text, "IFCFOOTING") == 0              # no analysis data
    assert _count(text, "IFCWALL") == 0
    # optional pset rows omitted when score/cost/energy are None
    assert "Overall score" not in text
    assert "EUI" not in text


# ---------------------------------------------------------------------------
# ifcopenshell validation
# ---------------------------------------------------------------------------

def test_ifcopenshell_opens_and_validates(sample):
    ifcopenshell = pytest.importorskip("ifcopenshell")
    from ifcopenshell import validate

    _, _, _, path, _ = sample
    f = ifcopenshell.open(str(path))
    assert f.schema == "IFC4"
    logger = validate.json_logger()
    validate.validate(f, logger)
    errors = [s for s in logger.statements if s.get("level") == "error"]
    assert errors == [], [str(e)[:300] for e in errors]


def test_ifcopenshell_express_rules(full_flags):
    ifcopenshell = pytest.importorskip("ifcopenshell")
    from ifcopenshell import validate

    _, _, path, _ = full_flags
    f = ifcopenshell.open(str(path))
    logger = validate.json_logger()
    try:
        validate.validate(f, logger, express_rules=True)
    except Exception as exc:            # express interpreter is optional
        pytest.skip(f"express rules unavailable: {exc}")
    errors = [s for s in logger.statements if s.get("level") == "error"]
    assert errors == [], [str(e)[:300] for e in errors]


def test_units_and_contexts(sample):
    _, _, _, _, text = sample
    assert "IFCSIUNIT(*,.LENGTHUNIT.,$,.METRE.);" in text
    assert "IFCSIUNIT(*,.AREAUNIT.,$,.SQUARE_METRE.);" in text
    assert "IFCSIUNIT(*,.VOLUMEUNIT.,$,.CUBIC_METRE.);" in text
    assert "IFCUNITASSIGNMENT(" in text
    assert "IFCGEOMETRICREPRESENTATIONCONTEXT(" in text
    assert "IFCGEOMETRICREPRESENTATIONSUBCONTEXT('Body'" in text


# ---------------------------------------------------------------------------
# pipeline / CLI wiring
# ---------------------------------------------------------------------------

def test_pipeline_writes_ifc(tmp_path):
    s = pipeline.run(text="3 floor house in Pune", out_dir=tmp_path,
                     make_pdf=False, make_images=False, make_ifc=True,
                     genetic=False)
    ifc = s.get("files", {}).get("ifc")
    assert ifc and set(ifc) == {"A", "B", "C"}
    for p in ifc.values():
        assert Path(p).read_text(encoding="ascii").startswith("ISO-10303-21;")
    assert not s.get("ifc_error")


def test_pipeline_can_skip_ifc(tmp_path):
    s = pipeline.run(text="3 floor house in Pune", out_dir=tmp_path,
                     make_pdf=False, make_images=False, make_ifc=False,
                     genetic=False)
    assert "ifc" not in s.get("files", {})
    assert not (tmp_path / "ifc").exists()
    assert s["results"]               # run itself unaffected
