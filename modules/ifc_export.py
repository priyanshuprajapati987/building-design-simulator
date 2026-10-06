"""IFC4 export (Phase 3B): hand-rolled ISO-10303-21 (STEP) writer.

Zero runtime dependencies. Writes an IFC4 file from one pipeline result
(design + analysis + cost/score/energy) that opens in common viewers
(BIMvision, BIMcollab, FreeCAD, Revit, online IFC viewers).

Geometry produced, per storey:
* slab      - rectangular extrusion of the full plate (thickness = slab_t)
* columns   - grid intersections, per-floor section from the analysis ladder
* beams     - per-bay segments (main beams span the full bay, secondary
              beams at mid-grid when the design uses them, panel = bay/2),
              top flush with the slab top
* core ring - 4 IfcWall segments (design.core only), centred on the plan
* footings  - spread pads under every column, top at grade (analysis data)
Spatial tree: IfcProject -> IfcSite -> IfcBuilding -> IfcBuildingStorey,
plus a ``Pset_BuildingSim`` property set on the building (score, cost, EUI).

GUIDs use the IFC compressed-GUID scheme (128-bit digest, custom base64,
first character always 0-3) seeded deterministically per element, so a
re-export of the same design is byte-identical for a fixed timestamp.

Honest scope: concept-stage BIM-ish handoff - no materials, no
reinforcement, no quantities/Qto sets, not a documentation-grade model.
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

import config as cfg

from .models import Requirements

IFC_SCHEMA = "IFC4"

# IFC compressed-GUID alphabet (64 chars); encoding a 128-bit digest into
# 22 chars means the first char covers only 2 bits -> always '0'-'3'.
_GUID_B64 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_$"


def compress_guid(seed: str) -> str:
    """Deterministic 22-char IfcGloballyUniqueId from a seed string."""
    n = int.from_bytes(hashlib.md5(seed.encode("utf-8")).digest(), "big")
    out: list[str] = []
    for _ in range(22):
        out.append(_GUID_B64[n & 63])
        n >>= 6
    return "".join(reversed(out))


def _real(x: float) -> str:
    """STEP REAL literal (must contain a '.')."""
    v = float(x)
    if abs(v) < 1e-12:
        return "0."
    s = f"{v:.6f}".rstrip("0")
    return s if "." in s else s + "."


def _str(s: Any) -> str:
    """STEP STRING literal: ASCII only, single-quoted, '' escape."""
    clean = "".join(ch for ch in str(s) if 32 <= ord(ch) < 127)
    return "'" + clean.replace("'", "''") + "'"


class _Writer:
    """Collects STEP entity bodies, dedupes identical ones, renders the file."""

    def __init__(self) -> None:
        self._bodies: dict[int, str] = {}
        self._by_body: dict[str, int] = {}
        self._next = 0

    def add(self, body: str) -> int:
        eid = self._by_body.get(body)
        if eid is None:
            self._next += 1
            eid = self._next
            self._bodies[eid] = body
            self._by_body[body] = eid
        return eid

    def render(self, file_name: str, timestamp: int) -> str:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(timestamp))
        head = [
            "ISO-10303-21;",
            "HEADER;",
            "FILE_DESCRIPTION(('ViewDefinition [ReferenceView]'),'2;1');",
            f"FILE_NAME({_str(file_name)},{_str(ts)},"
            "('Priyanshu Prajapati'),('Building Design Simulator'),"
            f"{_str('Building Design Simulator ' + cfg.VERSION)},"
            f"{_str('Building Design Simulator ' + cfg.VERSION)},'');",
            f"FILE_SCHEMA(('{IFC_SCHEMA}'));",
            "ENDSEC;",
            "DATA;",
        ]
        body = [f"#{eid}={self._bodies[eid]};" for eid in sorted(self._bodies)]
        return "\n".join(head + body + ["ENDSEC;", "END-ISO-10303-21;", ""])


# ---------------------------------------------------------------------------
# primitive helpers (all dedupe through the writer, safe to call repeatedly)
# ---------------------------------------------------------------------------

def _p000(w: _Writer) -> int:
    return w.add("IFCCARTESIANPOINT((0.,0.,0.))")


def _p00(w: _Writer) -> int:
    return w.add("IFCCARTESIANPOINT((0.,0.))")


def _d2(w: _Writer) -> int:
    return w.add("IFCDIRECTION((1.,0.))")


def _dz(w: _Writer) -> int:
    return w.add("IFCDIRECTION((0.,0.,1.))")


def _dx(w: _Writer) -> int:
    return w.add("IFCDIRECTION((1.,0.,0.))")


def _dy(w: _Writer) -> int:
    return w.add("IFCDIRECTION((0.,1.,0.))")


def _ax_id(w: _Writer) -> int:
    """Identity axis placement (local Z = +Z)."""
    return w.add(f"IFCAXIS2PLACEMENT3D(#{_p000(w)},#{_dz(w)},#{_dx(w)})")


def _pt3(w: _Writer, x: float, y: float, z: float) -> int:
    return w.add(f"IFCCARTESIANPOINT(({_real(x)},{_real(y)},{_real(z)}))")


def _local_placement(w: _Writer, parent: int, x: float, y: float, z: float) -> int:
    ax = w.add(f"IFCAXIS2PLACEMENT3D(#{_pt3(w, x, y, z)},#{_dz(w)},#{_dx(w)})")
    return w.add(f"IFCLOCALPLACEMENT(#{parent},#{ax})")


def _profile(w: _Writer, width: float, length: float) -> int:
    ax2 = w.add(f"IFCAXIS2PLACEMENT2D(#{_p00(w)},#{_d2(w)})")
    return w.add(f"IFCRECTANGLEPROFILEDEF(.AREA.,$,#{ax2},"
                 f"{_real(width)},{_real(length)})")


def _extrude(w: _Writer, profile: int, pos: int, depth: float) -> int:
    return w.add(f"IFCEXTRUDEDAREASOLID(#{profile},#{pos},#{_dz(w)},"
                 f"{_real(depth)})")


def _product(w: _Writer, entity: str, seed: str, name: str, oh: int,
             storey_pl: int, xyz: tuple[float, float, float],
             profile: tuple[float, float], pos: int, depth: float,
             predefined: str) -> int:
    """One placement + shape rep + typed product entity."""
    pl = _local_placement(w, storey_pl, *xyz)
    prof = _profile(w, *profile)
    solid = _extrude(w, prof, pos, depth)
    shape = w.add(f"IFCSHAPEREPRESENTATION(#{_body_ctx(w)},'Body',"
                  f"'SweptSolid',(#{solid}))")
    rep = w.add(f"IFCPRODUCTDEFINITIONSHAPE($,$,(#{shape}))")
    gid = compress_guid(seed)
    return w.add(f"{entity}('{gid}',#{oh},{_str(name)},$,$,#{pl},#{rep},"
                 f"$,{predefined})")


# ---------------------------------------------------------------------------
# header entities: owner history, units, context
# ---------------------------------------------------------------------------

def _owner_history(w: _Writer, timestamp: int) -> int:
    person = w.add("IFCPERSON($,'Prajapati','Priyanshu',$,$,$,$,$)")
    org = w.add("IFCORGANIZATION($,'Building Design Simulator',$,$,$)")
    pao = w.add(f"IFCPERSONANDORGANIZATION(#{person},#{org},$)")
    app = w.add(f"IFCAPPLICATION(#{org},{_str(cfg.VERSION)},"
                f"'Building Design Simulator','BuildingDesignSimulator')")
    return w.add(f"IFCOWNERISTORY(#{pao},#{app},$,.ADDED.,$,$,$,{int(timestamp)})")


def _units(w: _Writer) -> int:
    length = w.add("IFCSIUNIT(*,.LENGTHUNIT.,$,.METRE.)")
    area = w.add("IFCSIUNIT(*,.AREAUNIT.,$,.SQUARE_METRE.)")
    volume = w.add("IFCSIUNIT(*,.VOLUMEUNIT.,$,.CUBIC_METRE.)")
    angle = w.add("IFCSIUNIT(*,.PLANEANGLEUNIT.,$,.RADIAN.)")
    return w.add(f"IFCUNITASSIGNMENT((#{length},#{area},#{volume},#{angle}))")


def _context(w: _Writer) -> int:
    north = w.add("IFCDIRECTION((0.,1.))")
    return w.add(f"IFCGEOMETRICREPRESENTATIONCONTEXT($,'Model',3,1.0E-05,"
                 f"#{_ax_id(w)},#{north})")


def _body_ctx(w: _Writer) -> int:
    """The 'Body' sub-context (all shape representations point at it)."""
    north = w.add("IFCDIRECTION((0.,1.))")
    ctx = w.add(f"IFCGEOMETRICREPRESENTATIONCONTEXT($,'Model',3,1.0E-05,"
                f"#{_ax_id(w)},#{north})")
    return w.add(f"IFCGEOMETRICREPRESENTATIONSUBCONTEXT('Body','Model',"
                 f"*,*,*,*,#{ctx},$,.MODEL_VIEW.,$)")


# ---------------------------------------------------------------------------
# element builders
# ---------------------------------------------------------------------------

def _grid(d: dict[str, Any]) -> tuple[list[float], list[float]]:
    xs = [-float(d["len_x_m"]) / 2 + i * float(d["bay_x_m"])
          for i in range(int(d["bays_x"]) + 1)]
    ys = [-float(d["len_y_m"]) / 2 + j * float(d["bay_y_m"])
          for j in range(int(d["bays_y"]) + 1)]
    return xs, ys


def _column_section(analysis: dict[str, Any], floor: int) -> tuple[float, float]:
    rows = (analysis.get("members") or {}).get("columns") or []
    sec = rows[min(floor, len(rows) - 1)]["section_mm"] if rows else "300 x 300"
    b, h = sec.split("x")
    return float(b.strip()) / 1000.0, float(h.strip()) / 1000.0


def _footing_sizes(analysis: dict[str, Any]) -> dict[str, tuple[float, float]]:
    fnd = analysis.get("foundation") or {}
    return {r["position"]: (float(r["B_mm"]) / 1000.0,
                            float(r["thickness_mm"]) / 1000.0)
            for r in fnd.get("footings", [])}


def _add_slab(w: _Writer, oh: int, d: dict[str, Any], floor: int,
              storey_pl: int) -> int:
    elev = round(floor * float(d["floor_h_m"]), 6)
    return _product(
        w, "IFCSLAB", f"{d['id']}|slab|{floor}", f"Slab L{floor + 1:02d}", oh,
        storey_pl, (0.0, 0.0, elev),
        (float(d["len_x_m"]), float(d["len_y_m"])),
        _ax_id(w), float(d["slab_t_mm"]) / 1000.0, ".FLOOR.")


def _add_columns(w: _Writer, oh: int, d: dict[str, Any],
                 analysis: dict[str, Any], floor: int, storey_pl: int) -> list[int]:
    did = str(d["id"])
    elev = round(floor * float(d["floor_h_m"]), 6)
    fh = float(d["floor_h_m"])
    col_b, col_h = _column_section(analysis, floor)
    xs, ys = _grid(d)
    out: list[int] = []
    for i, x in enumerate(xs):
        for j, y in enumerate(ys):
            out.append(_product(
                w, "IFCCOLUMN", f"{did}|col|{floor}|{i}|{j}",
                f"Column {i}-{j} L{floor + 1:02d}", oh, storey_pl,
                (x, y, elev), (col_b, col_h), _ax_id(w), fh, ".COLUMN."))
    return out


def _add_footings(w: _Writer, oh: int, d: dict[str, Any],
                  feet: dict[str, tuple[float, float]],
                  storey_pl: int) -> list[int]:
    did = str(d["id"])
    n_x, n_y = int(d["bays_x"]), int(d["bays_y"])
    xs, ys = _grid(d)
    out: list[int] = []
    for i, x in enumerate(xs):
        for j, y in enumerate(ys):
            pos = ("corner" if i in (0, n_x) and j in (0, n_y)
                   else "edge" if i in (0, n_x) or j in (0, n_y)
                   else "interior")
            if pos not in feet:
                continue
            b, t = feet[pos]
            out.append(_product(
                w, "IFCFOOTING", f"{did}|foot|{i}|{j}",
                f"Footing {pos} {i}-{j}", oh, storey_pl,
                (x, y, -t), (b, b), _ax_id(w), t, ".PAD_FOOTING."))
    return out


def _add_beams(w: _Writer, oh: int, d: dict[str, Any], floor: int,
               storey_pl: int) -> list[int]:
    """Per-bay beam segments: primary on the grid, secondary at mid-grid."""
    did = str(d["id"])
    elev = round(floor * float(d["floor_h_m"]), 6)
    slab_t = float(d["slab_t_mm"]) / 1000.0
    xs, ys = _grid(d)
    n_x, n_y = int(d["bays_x"]), int(d["bays_y"])
    bx, by = float(d["bay_x_m"]), float(d["bay_y_m"])
    beam_w = float(d["beam_w_mm"]) / 1000.0
    plan: list[tuple[str, float]] = [("p", float(d["beam_d_mm"]) / 1000.0)]
    if d.get("secondary") and d.get("sec_beam_d_mm"):
        plan.append(("s", float(d["sec_beam_d_mm"]) / 1000.0))
    pos_x = w.add(f"IFCAXIS2PLACEMENT3D(#{_p000(w)},#{_dx(w)},#{_dz(w)})")
    pos_y = w.add(f"IFCAXIS2PLACEMENT3D(#{_p000(w)},#{_dy(w)},#{_dz(w)})")
    out: list[int] = []
    for kind, bd in plan:
        zc = elev + slab_t - bd / 2.0
        mul = 2 if kind == "s" else 1
        rows = [float(j) + (0.5 if mul == 2 else 0.0)
                for j in range(n_y + (0 if mul == 2 else 1))]
        seg_x, step_x = bx / mul, mul * n_x
        for jj, fj in enumerate(rows):
            y = ys[0] + fj * by
            for i_s in range(step_x):
                out.append(_product(
                    w, "IFCBEAM", f"{did}|bx|{floor}|{kind}|{jj}|{i_s}",
                    f"Beam X {floor + 1:02d} r{jj} s{i_s}", oh, storey_pl,
                    (xs[0] + i_s * seg_x, y, zc), (bd, beam_w),
                    pos_x, seg_x, ".BEAM."))
        cols = [float(i) + (0.5 if mul == 2 else 0.0)
                for i in range(n_x + (0 if mul == 2 else 1))]
        seg_y, step_y = by / mul, mul * n_y
        for ii, fi in enumerate(cols):
            x = xs[0] + fi * bx
            for j_s in range(step_y):
                out.append(_product(
                    w, "IFCBEAM", f"{did}|by|{floor}|{kind}|{ii}|{j_s}",
                    f"Beam Y {floor + 1:02d} c{ii} s{j_s}", oh, storey_pl,
                    (x, ys[0] + j_s * seg_y, zc), (bd, beam_w),
                    pos_y, seg_y, ".BEAM."))
    return out


def _add_core_walls(w: _Writer, oh: int, d: dict[str, Any], floor: int,
                    storey_pl: int) -> list[int]:
    """Structural core: ring of 4 walls, centred on the plan."""
    if not (d.get("core") and d.get("core_lx_m") and d.get("core_ly_m")):
        return []
    t = float(d["wall_t_mm"]) / 1000.0
    clx, cly = float(d["core_lx_m"]), float(d["core_ly_m"])
    if clx <= 2 * t or cly <= 2 * t:
        return []
    did = str(d["id"])
    elev = round(floor * float(d["floor_h_m"]), 6)
    fh = float(d["floor_h_m"])
    half_y, half_x = cly / 2 - t / 2, clx / 2 - t / 2
    span_e = (t, cly - 2 * t)
    return [
        _product(w, "IFCWALL", f"{did}|cw|{floor}|n",
                 f"Core wall N L{floor + 1:02d}", oh, storey_pl,
                 (0.0, half_y, elev), (clx, t), _ax_id(w), fh, ".SOLIDWALL."),
        _product(w, "IFCWALL", f"{did}|cw|{floor}|s",
                 f"Core wall S L{floor + 1:02d}", oh, storey_pl,
                 (0.0, -half_y, elev), (clx, t), _ax_id(w), fh, ".SOLIDWALL."),
        _product(w, "IFCWALL", f"{did}|cw|{floor}|e",
                 f"Core wall E L{floor + 1:02d}", oh, storey_pl,
                 (half_x, 0.0, elev), span_e, _ax_id(w), fh, ".SOLIDWALL."),
        _product(w, "IFCWALL", f"{did}|cw|{floor}|w",
                 f"Core wall W L{floor + 1:02d}", oh, storey_pl,
                 (-half_x, 0.0, elev), span_e, _ax_id(w), fh, ".SOLIDWALL."),
    ]


def _storey_products(w: _Writer, oh: int, d: dict[str, Any],
                     analysis: dict[str, Any], floor: int, storey_pl: int,
                     feet: dict[str, tuple[float, float]]) -> list[int]:
    out = [_add_slab(w, oh, d, floor, storey_pl),
           *_add_columns(w, oh, d, analysis, floor, storey_pl),
           *_add_beams(w, oh, d, floor, storey_pl),
           *_add_core_walls(w, oh, d, floor, storey_pl)]
    if floor == 0:
        out.extend(_add_footings(w, oh, d, feet, storey_pl))
    return out


# ---------------------------------------------------------------------------
# spatial tree + property set
# ---------------------------------------------------------------------------

def _spatial(w: _Writer, oh: int, ctx: int, units: int,
             d: dict[str, Any], analysis: dict[str, Any],
             req: Requirements) -> int:
    """Project/site/building/storeys + per-storey containment. Returns the
    building id (storeys stay referenced through containment/aggregates)."""
    did = str(d["id"])
    feet = _footing_sizes(analysis)
    label = f"BuildingSim {did} - {req.building_type}, {req.city}"

    project = w.add(
        f"IFCPROJECT('{compress_guid('project|' + did)}',#{oh},"
        f"{_str(label)},$,$,$,$,(#{ctx}),#{units})")
    site_pl = w.add(f"IFCLOCALPLACEMENT($,#{_ax_id(w)})")
    site = w.add(f"IFCSITE('{compress_guid('site|' + did)}',#{oh},"
                 f"{_str('Site ' + req.city)},$,$,#{site_pl},$,$,.ELEMENT.,"
                 f"$,$,$,$,$)")
    bldg_pl = w.add(f"IFCLOCALPLACEMENT(#{site_pl},#{_ax_id(w)})")
    b_name = str(d.get("name") or "Building")
    building = w.add(f"IFCBUILDING('{compress_guid('building|' + did)}',#{oh},"
                     f"{_str(b_name)},$,$,#{bldg_pl},$,$,"
                     f".ELEMENT.,$,$,$)")

    floors = int(d["floors"])
    fh = float(d["floor_h_m"])
    storeys: list[int] = []
    for fl in range(floors):
        elev = round(fl * fh, 6)
        pl = _local_placement(w, bldg_pl, 0.0, 0.0, elev)
        st = w.add(f"IFCBUILDINGSTOREY('{compress_guid(f'storey|{did}|{fl}')}',"
                   f"#{oh},{_str(f'Level {fl + 1:02d}')},$,$,#{pl},$,$,"
                   f".ELEMENT.,{_real(elev)})")
        storeys.append(st)
        elems = _storey_products(w, oh, d, analysis, fl, pl, feet)
        w.add(f"IFCRELCONTAINEDINSPATIALSTRUCTURE("
              f"'{compress_guid(f'contain|{did}|{fl}')}',#{oh},$,$,"
              f"({','.join(f'#{e}' for e in elems)}),#{st})")

    w.add(f"IFCRELAGGREGATES('{compress_guid('agg|proj|' + did)}',#{oh},$,$,"
          f"#{project},(#{site}))")
    w.add(f"IFCRELAGGREGATES('{compress_guid('agg|site|' + did)}',#{oh},$,$,"
          f"#{site},(#{building}))")
    w.add(f"IFCRELAGGREGATES('{compress_guid('agg|bldg|' + did)}',#{oh},$,$,"
          f"#{building},({','.join(f'#{s}' for s in storeys)}))")
    return building


def _building_pset(w: _Writer, oh: int, building: int, d: dict[str, Any],
                   analysis: dict[str, Any], req: Requirements,
                   score: float | None, cost: dict[str, Any] | None,
                   energy: dict[str, Any] | None) -> None:
    """Pset_BuildingSim on the building: the run's headline numbers."""
    rows: list[tuple[str, str]] = [
        ("City", f"IFCLABEL({_str(req.city)})"),
        ("Building type", f"IFCLABEL({_str(req.building_type)})"),
        ("Structural system", f"IFCLABEL({_str(d.get('system', ''))})"),
        ("Seismic zone",
         f"IFCLABEL({_str((analysis.get('seismic') or {}).get('zone', ''))})"),
        ("Floors", f"IFCLABEL({_str(d.get('floors', ''))})"),
    ]
    if score is not None:
        rows.append(("Overall score (0-100)",
                     f"IFCREAL({_real(float(score))})"))
    if cost:
        total = "INR " + f"{int(cost.get('total_inr', 0)):,}"
        rows.append(("Estimated cost", f"IFCLABEL({_str(total)})"))
    if energy:
        eui = float(energy.get("eui_kwh_m2yr", 0.0) or 0.0)
        rows.append(("EUI (kWh/m2.yr)", f"IFCREAL({_real(eui)})"))
    props = [w.add(f"IFCPROPERTYSINGLEVALUE({_str(name)},$,{typed},$)")
             for name, typed in rows]
    pset = w.add(f"IFCPROPERTYSET('{compress_guid('pset|' + str(d['id']))}',"
                 f"#{oh},'Pset_BuildingSim',$,"
                 f"({','.join(f'#{p}' for p in props)}))")
    rel = compress_guid(f"psetrel|{d['id']!s}")
    w.add(f"IFCRELDEFINESBYPROPERTIES('{rel}',#{oh},$,$,"
          f"(#{building}),#{pset})")


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def export(design: dict[str, Any], analysis: dict[str, Any],
           req: Requirements, out_path: str | Path, *,
           score: float | None = None,
           cost: dict[str, Any] | None = None,
           energy: dict[str, Any] | None = None,
           timestamp: float | None = None) -> Path:
    """Write one IFC4 file for a pipeline result. Returns the written path.

    ``design`` is the serialised design dict (``Design.to_dict()``). The
    export never touches the network and is deterministic for a fixed
    ``timestamp`` (GUIDs only depend on the design id + element index)."""
    ts = int(timestamp if timestamp is not None else time.time())
    w = _Writer()
    oh = _owner_history(w, ts)
    units = _units(w)
    ctx = _context(w)
    building = _spatial(w, oh, ctx, units, design, analysis, req)
    _building_pset(w, oh, building, design, analysis, req, score, cost, energy)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(w.render(out_path.name, ts), encoding="ascii")
    return out_path
