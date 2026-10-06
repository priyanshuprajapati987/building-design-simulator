"""Phase-2d certified EnergyPlus simulation (hourly, whole-building).

The Phase-2 ``energy_model`` (degree-day) stays the cross-design COMPARISON
engine - every candidate is scored by the identical fast method. This module
runs the REAL EnergyPlus simulation on the shortlisted design(s):

* ``resolve_weather()``  city -> cached / downloaded TMYx EPW (data/epw_map.json)
* ``build_idf()``        single-zone ribbon-window model built from the Design
                         + the preliminary model's assumptions (parasitic parity)
* ``run_idf()``          ``energyplus -w city.epw -d out/ design.idf``
* ``_summarise()``       eplusout.sql "End Uses" -> kWh / EUI / breakdown
* ``simulate()``         the whole chain - NEVER raises: missing binary,
                         missing weather, severe errors all come back as
                         ``{"ok": False, "reason": ...}`` so the pipeline keeps
                         its degree-day numbers either way

Design decisions (verified against EnergyPlus 26.2 IDD + ExampleFiles):
* one rectangular zone (not per-floor) - avoids interzone surface bookkeeping
* ribbon windows: height = WWR x H centred on each facade, 0.05 m edge inset
  -> glazing area == WWR x wall area exactly (parity with the degree-day model)
* vertex order = right-hand rule with outward normal (cross-product checked)
* flat schedules value = hours/8760 so annual load energy matches the
  preliminary model's ``W x hours`` exactly (no shape credit or penalty)
* internal gains written as ABSOLUTE watts for the plate x floors program -
  the single zone's own floor area is just the footprint, so Watts/Area
  methods would silently count one floor of loads
* ZoneHVAC:IdealLoadsAirSystem, NoLimit heating/cooling (autosized) - E+ reports
  its demand as DistrictHeatingWater / DistrictCooling in the End Uses table
"""
from __future__ import annotations

import io
import json
import re
import sqlite3
import subprocess
import time
import zipfile
from functools import lru_cache
from pathlib import Path
from urllib.request import Request, urlopen

import config as cfg

from .energy_model import find_energyplus
from .models import Design, Requirements

ENGINE = "energyplus-hourly-v1"
EPW_BASE = "https://climate.onebuilding.org/WMO_Region_2_Asia/"
USER_AGENT = "BuildingDesignSimulator/0.1"
GJ_TO_KWH = 277.7778
# thermostat setpoints (C) - ECBC-style comfort band for the certified run
HEAT_SP_C = 18.0
COOL_SP_C = 24.0
OCC_PER_SQM = 0.05


# ---------------------------------------------------------------------------
# weather: city -> EPW
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _epw_map() -> dict:
    try:
        return json.loads(cfg.EPW_MAP_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _fetch(url: str) -> bytes:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=60) as resp:
        return resp.read()


def _extract_epw(blob: bytes) -> bytes:
    """First ``.epw`` member of a onebuilding.org TMYx zip."""
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        name = next(n for n in zf.namelist() if n.lower().endswith(".epw"))
        return zf.read(name)


def resolve_weather(
    city: str, allow_download: bool | None = None
) -> tuple[Path | None, dict]:
    """City -> (cached EPW path, info). ``info.kind``: cached | downloaded | missing.

    Offline-safe: ``allow_download=False`` only accepts an already-cached file.
    """
    if allow_download is None:
        allow_download = cfg.ENERGYPLUS_ALLOW_DOWNLOAD
    key = city.strip().lower()
    info: dict = {"city": city, "epw": None, "source": None, "kind": "missing"}
    cache = cfg.ENERGYPLUS_WEATHER_DIR / f"{key}.epw"
    if cache.is_file() and cache.stat().st_size > 10_000:
        info.update(epw=str(cache), source="cache", kind="cached")
        return cache, info
    rel = _epw_map().get(key)
    if not rel:
        info["reason"] = f"no EPW source mapped for city '{city}'"
        return None, info
    if not allow_download:
        info["reason"] = ("EPW not cached and downloads disabled "
                          "(ENERGYPLUS_ALLOW_DOWNLOAD=0)")
        return None, info
    url = rel if rel.startswith("http") else EPW_BASE + rel
    try:
        payload = _extract_epw(_fetch(url))
    except Exception as exc:
        info["reason"] = f"weather download failed: {type(exc).__name__}: {exc}"
        return None, info
    cfg.ENERGYPLUS_WEATHER_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(payload)
    info.update(epw=str(cache), source=url, kind="downloaded",
                bytes=len(payload))
    return cache, info


# ---------------------------------------------------------------------------
# binary
# ---------------------------------------------------------------------------

@lru_cache(maxsize=4)
def exe_version(exe: str) -> str:
    """``Major.Minor`` from ``energyplus --version`` (e.g. '26.2')."""
    try:
        proc = subprocess.run([exe, "--version"], capture_output=True,
                              text=True, timeout=30)
        m = re.search(r"Version\s+([0-9]+\.[0-9]+)",
                      (proc.stdout or "") + (proc.stderr or ""))
        return m.group(1) if m else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def in_scope(rank: int, scope: str | None = None) -> bool:
    """ENERGYPLUS_SCOPE: ``winner`` (default) | ``all`` | ``off``."""
    s = (cfg.ENERGYPLUS_SCOPE if scope is None else scope).strip().lower()
    if s in ("off", "none", "0", "false"):
        return False
    if s == "all":
        return True
    return rank == 1


# ---------------------------------------------------------------------------
# IDF geometry
# ---------------------------------------------------------------------------

def _f(x: float) -> str:
    s = f"{x:.4f}".rstrip("0").rstrip(".")
    return s if s and s != "-0" else "0"


def _pid(d: Design) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", str(d.id)) or "D"


def _surfaces(lx: float, ly: float, h: float) -> list[tuple]:
    """Six closed box faces; vertex order gives the right-hand OUTWARD normal."""
    return [
        ("Floor", "Floor", "Ground", "NoSun", "NoWind",
         [(0.0, 0.0, 0.0), (0.0, ly, 0.0), (lx, ly, 0.0), (lx, 0.0, 0.0)]),
        ("Roof", "Roof", "Outdoors", "SunExposed", "WindExposed",
         [(0.0, 0.0, h), (lx, 0.0, h), (lx, ly, h), (0.0, ly, h)]),
        ("Wall_S", "Wall", "Outdoors", "SunExposed", "WindExposed",
         [(0.0, 0.0, 0.0), (lx, 0.0, 0.0), (lx, 0.0, h), (0.0, 0.0, h)]),
        ("Wall_N", "Wall", "Outdoors", "SunExposed", "WindExposed",
         [(0.0, ly, 0.0), (0.0, ly, h), (lx, ly, h), (lx, ly, 0.0)]),
        ("Wall_E", "Wall", "Outdoors", "SunExposed", "WindExposed",
         [(lx, 0.0, 0.0), (lx, ly, 0.0), (lx, ly, h), (lx, 0.0, h)]),
        ("Wall_W", "Wall", "Outdoors", "SunExposed", "WindExposed",
         [(0.0, 0.0, 0.0), (0.0, 0.0, h), (0.0, ly, h), (0.0, ly, 0.0)]),
    ]


def _windows(lx: float, ly: float, h: float, wwr: float) -> list[tuple]:
    """One ribbon window per facade (height = WWR*H, centred, 0.05 m insets).

    Each window's winding mirrors its parent wall so the normal points outward.
    """
    hw = wwr * h
    if hw <= 0.1:
        return []
    zs = (h - hw) / 2.0
    zt = zs + hw
    ins = 0.05
    wins: list[tuple] = []
    if lx > 2 * ins:
        wins.append(("Wall_S", [(ins, 0.0, zs), (lx - ins, 0.0, zs),
                                (lx - ins, 0.0, zt), (ins, 0.0, zt)]))
        wins.append(("Wall_N", [(ins, ly, zs), (ins, ly, zt),
                                (lx - ins, ly, zt), (lx - ins, ly, zs)]))
    if ly > 2 * ins:
        wins.append(("Wall_E", [(lx, ins, zs), (lx, ly - ins, zs),
                                (lx, ly - ins, zt), (lx, ins, zt)]))
        wins.append(("Wall_W", [(0.0, ins, zs), (0.0, ins, zt),
                                (0.0, ly - ins, zt), (0.0, ly - ins, zs)]))
    return wins


def _verts(verts: list[tuple], last: bool = False) -> str:
    out = []
    for i, (x, y, z) in enumerate(verts):
        end = ";" if last and i == len(verts) - 1 else ","
        out.append(f"  {_f(x)},{_f(y)},{_f(z)}{end}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# IDF sections
# ---------------------------------------------------------------------------

def _sec_header(pid: str, version: str, req: Requirements, d: Design) -> str:
    return (
        f"!- {cfg.APP_NAME} Phase-2d certified EnergyPlus model\n"
        f"!- design {d.id} ({d.name}) - {req.city} - {req.building_type}\n"
        f"!- engine {ENGINE} - single zone, ribbon windows, flat schedules\n"
        f"Version,{version};\n"
        "Timestep,4;\n"
        "Building,\n"
        f"  {pid}_Bldg,           !- Name\n"
        "  0,                      !- North Axis {deg}\n"
        "  Suburbs,                !- Terrain\n"
        "  0.05,                   !- Loads Convergence Tolerance Value {W}\n"
        "  0.05,                   !- Temperature Convergence Tolerance {deltaC}\n"
        "  FullInteriorAndExterior, !- Solar Distribution\n"
        "  50,                     !- Maximum Number of Warmup Days\n"
        "  6;                      !- Minimum Number of Warmup Days\n"
        "GlobalGeometryRules,\n"
        "  LowerLeftCorner,        !- Starting Vertex Position\n"
        "  CounterClockWise,       !- Vertex Entry Direction\n"
        "  World;                  !- Coordinate System\n"
    )


def _sec_schedules(pid: str, hours: float) -> str:
    frac = min(max(hours / 8760.0, 0.0), 1.0)
    return (
        "ScheduleTypeLimits,\n"
        "  Any Number;             !- Name\n"
        f"Schedule:Constant, {pid}_Activity, Any Number, 120;\n"
        f"Schedule:Compact,\n"
        f"  {pid}_Occ,              !- Name\n"
        "  Any Number,             !- Schedule Type Limits Name\n"
        "  Through: 12/31,         !- Field 1\n"
        "  For: AllDays,           !- Field 2\n"
        f"  Until: 24:00,{_f(frac)};\n"
        f"Schedule:Compact,\n"
        f"  {pid}_Ctrl,             !- Name\n"
        "  Any Number,             !- Schedule Type Limits Name\n"
        "  Through: 12/31,\n"
        "  For: AllDays,\n"
        "  Until: 24:00,4;         !- 4 = DualSetpoint thermostat\n"
        f"Schedule:Compact,\n"
        f"  {pid}_HeatSp,           !- Name\n"
        "  Any Number,\n"
        "  Through: 12/31,\n"
        "  For: AllDays,\n"
        f"  Until: 24:00,{_f(HEAT_SP_C)};\n"
        f"Schedule:Compact,\n"
        f"  {pid}_CoolSp,           !- Name\n"
        "  Any Number,\n"
        "  Through: 12/31,\n"
        "  For: AllDays,\n"
        f"  Until: 24:00,{_f(COOL_SP_C)};\n"
    )


def _sec_envelope(pid: str, env: dict) -> str:
    r_wall = 1.0 / float(env["u_wall_wm2k"])
    r_roof = 1.0 / float(env["u_roof_wm2k"])
    r_flr = 1.0 / 1.0
    return (
        f"Material:NoMass, {pid}_WallR, Rough, {_f(r_wall)}, 0.9, 0.6, 0.6;\n"
        f"Material:NoMass, {pid}_RoofR, Rough, {_f(r_roof)}, 0.9, 0.6, 0.6;\n"
        f"Material:NoMass, {pid}_FloorR, Rough, {_f(r_flr)}, 0.9, 0.6, 0.6;\n"
        f"WindowMaterial:SimpleGlazingSystem, {pid}_Win, "
        f"{_f(float(env['u_win_wm2k']))}, {_f(float(env['shgc']))};\n"
        f"Construction, {pid}_Constr_Wall, {pid}_WallR;\n"
        f"Construction, {pid}_Constr_Roof, {pid}_RoofR;\n"
        f"Construction, {pid}_Constr_Floor, {pid}_FloorR;\n"
        f"Construction, {pid}_Constr_Win, {pid}_Win;\n"
    )


def _sec_shell(pid: str, d: Design, wwr: float) -> str:
    zone = f"{pid}_Zone"
    lx, ly, h = d.len_x_m, d.len_y_m, d.height_m
    parts = [f"Zone,\n  {zone},  !- Name\n"
             "  0, 0, 0, 0, 1, 1, autocalculate, autocalculate;\n"]
    for name, stype, obc, sun, wind, verts in _surfaces(lx, ly, h):
        constr = {"Floor": "Floor", "Roof": "Roof"}.get(stype, "Wall")
        parts.append(
            f"BuildingSurface:Detailed,\n"
            f"  {pid}_{name},         !- Name\n"
            f"  {stype},              !- Surface Type\n"
            f"  {pid}_Constr_{constr},  !- Construction Name\n"
            f"  {zone},               !- Zone Name\n"
            "  ,                     !- Space Name\n"
            f"  {obc},                !- Outside Boundary Condition\n"
            "  ,                     !- Outside Boundary Condition Object\n"
            f"  {sun},                !- Sun Exposure\n"
            f"  {wind},               !- Wind Exposure\n"
            "  0.5,                  !- View Factor to Ground\n"
            "  4,                    !- Number of Vertices\n"
            f"{_verts(verts, last=True)}\n")
    for i, (parent, wverts) in enumerate(_windows(lx, ly, h, wwr), 1):
        parts.append(
            f"FenestrationSurface:Detailed,\n"
            f"  {pid}_Win{i},         !- Name\n"
            "  Window,               !- Surface Type\n"
            f"  {pid}_Constr_Win,     !- Construction Name\n"
            f"  {pid}_{parent},       !- Building Surface Name\n"
            "  ,                     !- Outside Boundary Condition Object\n"
            "  0.5,                  !- View Factor to Ground\n"
            "  ,                     !- Frame and Divider Name\n"
            "  1.0,                  !- Multiplier\n"
            "  4,                    !- Number of Vertices\n"
            f"{_verts(wverts, last=True)}\n")
    return "".join(parts)


def _sec_gains(pid: str, area_sqm: float, lpd: float, plug: float) -> str:
    """Internal gains as ABSOLUTE watts for the whole multi-floor program.

    The single zone's floor area is the footprint only, so per-area methods
    would count one floor of loads; absolute values keep parity with the
    preliminary model's ``area = plate x floors`` accounting.
    """
    zone = f"{pid}_Zone"
    lights_w = lpd * area_sqm
    plug_w = plug * area_sqm
    occ = OCC_PER_SQM * area_sqm
    return (
        f"People,\n"
        f"  {pid}_Occupants,       !- Name\n"
        f"  {zone},                !- Zone\n"
        f"  {pid}_Occ,             !- Number of People Schedule Name\n"
        "  People,                !- Number of People Calculation Method\n"
        f"  {_f(occ)},            !- Number of People\n"
        "  ,                     !- People per Floor Area\n"
        "  ,                     !- Floor Area per Person\n"
        "  0.3,                   !- Fraction Radiant\n"
        "  AUTOCALCULATE,         !- Sensible Heat Fraction\n"
        f"  {pid}_Activity;        !- Activity Level Schedule Name\n"
        f"Lights,\n"
        f"  {pid}_Lights,          !- Name\n"
        f"  {zone},                !- Zone\n"
        f"  {pid}_Occ,             !- Schedule Name\n"
        "  LightingLevel,         !- Design Level Calculation Method\n"
        f"  {_f(lights_w)},        !- Lighting Level {{W}}\n"
        "  ,                     !- Watts per Floor Area\n"
        "  ,                     !- Watts per Person\n"
        "  0.0,                   !- Return Air Fraction\n"
        "  0.5,                   !- Fraction Radiant\n"
        "  0.2,                   !- Fraction Visible\n"
        "  1.0,                   !- Fraction Replaceable\n"
        "  GeneralLights;         !- End-Use Subcategory\n"
        f"ElectricEquipment,\n"
        f"  {pid}_Plug,            !- Name\n"
        f"  {zone},                !- Zone\n"
        f"  {pid}_Occ,             !- Schedule Name\n"
        "  EquipmentLevel,        !- Design Power Input Calculation Method\n"
        f"  {_f(plug_w)},          !- Design Level {{W}}\n"
        "  ,                     !- Watts per Floor Area\n"
        "  ,                     !- Watts per Person\n"
        "  0.0,                   !- Fraction Latent\n"
        "  0.3,                   !- Fraction Radiant\n"
        "  0.0,                   !- Fraction Lost\n"
        "  PlugLoads;             !- End-Use Subcategory\n"
    )


def _sec_hvac(pid: str) -> str:
    zone = f"{pid}_Zone"
    return (
        f"ZoneControl:Thermostat,\n"
        f"  {pid}_Thermostat,      !- Name\n"
        f"  {zone},                !- Zone\n"
        f"  {pid}_Ctrl,            !- Control Type Schedule Name\n"
        "  ThermostatSetpoint:DualSetpoint, !- Control 1 Object Type\n"
        f"  {pid}_DualSP;          !- Control 1 Name\n"
        f"ThermostatSetpoint:DualSetpoint,\n"
        f"  {pid}_DualSP,          !- Name\n"
        f"  {pid}_HeatSp,          !- Heating Setpoint Schedule Name\n"
        f"  {pid}_CoolSp;          !- Cooling Setpoint Schedule Name\n"
        f"ZoneHVAC:IdealLoadsAirSystem,\n"
        f"  {pid}_Ideal,           !- Name\n"
        "  ,                     !- Availability Schedule Name\n"
        f"  {pid}_ZoneInlet,       !- Zone Supply Air Node Name\n"
        "  ,                     !- Zone Exhaust Air Node Name\n"
        "  ,                     !- System Inlet Air Node Name\n"
        "  50,                    !- Maximum Heating Supply Air Temperature {C}\n"
        "  13,                    !- Minimum Cooling Supply Air Temperature {C}\n"
        "  0.015,                 !- Maximum Heating Supply Air Humidity Ratio\n"
        "  0.009,                 !- Minimum Cooling Supply Air Humidity Ratio\n"
        "  NoLimit,               !- Heating Limit\n"
        "  autosize,              !- Maximum Heating Air Flow Rate\n"
        "  ,                     !- Maximum Sensible Heating Capacity\n"
        "  NoLimit,               !- Cooling Limit\n"
        "  autosize,              !- Maximum Cooling Air Flow Rate\n"
        "  ,                     !- Maximum Total Cooling Capacity\n"
        "  ,                     !- Heating Availability Schedule Name\n"
        "  ,                     !- Cooling Availability Schedule Name\n"
        "  ConstantSupplyHumidityRatio, !- Dehumidification Control Type\n"
        "  ,                     !- Cooling Sensible Heat Ratio\n"
        "  ConstantSupplyHumidityRatio, !- Humidification Control Type\n"
        "  ,                     !- Design Specification Outdoor Air Object\n"
        "  ,                     !- Outdoor Air Inlet Node Name\n"
        "  ,                     !- Demand Controlled Ventilation Type\n"
        "  ,                     !- Outdoor Air Economizer Type\n"
        "  ,                     !- Heat Recovery Type\n"
        "  ,                     !- Sensible Heat Recovery Effectiveness\n"
        "  ;                     !- Latent Heat Recovery Effectiveness\n"
        f"ZoneHVAC:EquipmentList,\n"
        f"  {pid}_EqList,         !- Name\n"
        "  SequentialLoad,        !- Load Distribution Scheme\n"
        "  ZoneHVAC:IdealLoadsAirSystem, !- Zone Equipment 1 Object Type\n"
        f"  {pid}_Ideal,           !- Zone Equipment 1 Name\n"
        "  1,                     !- Zone Equipment 1 Cooling Sequence\n"
        "  1,                     !- Zone Equipment 1 Heating Sequence\n"
        "  ,                     !- Sequential Cooling Fraction Schedule\n"
        "  ;                     !- Sequential Heating Fraction Schedule\n"
        f"ZoneHVAC:EquipmentConnections,\n"
        f"  {zone},                !- Zone Name\n"
        f"  {pid}_EqList,         !- Zone Conditioning Equipment List Name\n"
        f"  {pid}_ZoneInlet,       !- Zone Air Inlet Node Name\n"
        "  ,                     !- Zone Air Exhaust Node Name\n"
        f"  {pid}_ZoneNode,        !- Zone Air Node Name\n"
        f"  {pid}_ReturnNode;      !- Zone Return Air Node Name\n"
    )


def _sec_tail() -> str:
    return (
        # explicit annual RunPeriod: E+ refuses to run with only SimulationControl
        "RunPeriod,\n"
        "  Annual,               !- Name\n"
        "  1,                    !- Begin Month\n"
        "  1,                    !- Begin Day of Month\n"
        "  ,                     !- Begin Year\n"
        "  12,                   !- End Month\n"
        "  31,                   !- End Day of Month\n"
        "  ;                     !- End Year\n"
        "SimulationControl, No, No, No, No, Yes, No, 1;\n"
        "Output:SQLite, SimpleAndTabular;\n"
        "Output:Table:SummaryReports, AllSummary;\n"
    )


def build_idf(req: Requirements, d: Design, assump: dict,
              version: str = "26.2") -> str:
    """Full one-zone IDF for one design (assumptions = degree-day dict)."""
    pid = _pid(d)
    env = {
        "u_wall_wm2k": assump.get("u_wall_wm2k", 0.8),
        "u_roof_wm2k": assump.get("u_roof_wm2k", 0.45),
        "u_win_wm2k": assump.get("u_win_wm2k", 2.8),
        "shgc": assump.get("shgc", 0.4),
    }
    wwr = float(assump.get("wwr", 0.25))
    lpd = float(assump.get("lpd_wm2", 5.0))
    plug = float(assump.get("plug_wm2", 12.0))
    hours = float(assump.get("hours", 3000.0))
    area_sqm = d.plate_sqm * d.floors           # whole-program area (parity)
    parts = [
        _sec_header(pid, version, req, d),
        _sec_schedules(pid, hours),
        _sec_envelope(pid, env),
        _sec_shell(pid, d, wwr),
        _sec_gains(pid, area_sqm, lpd, plug),
        _sec_hvac(pid),
        _sec_tail(),
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# run + parse
# ---------------------------------------------------------------------------

def run_idf(idf_text: str, epw: Path, work_dir: Path, exe: str,
            timeout: int | None = None) -> dict:
    """Write + run one IDF; returns {ok, idf, err?, rc, runtime_s, reason?}."""
    timeout = timeout or cfg.ENERGYPLUS_TIMEOUT
    work_dir.mkdir(parents=True, exist_ok=True)
    idf_path = work_dir / "design.idf"
    idf_path.write_text(idf_text, encoding="utf-8")
    # absolute paths everywhere: the subprocess cwd is work_dir, so a
    # relative IDF/-w/-d would resolve against THAT and miss the file
    work_abs = work_dir.resolve()
    idf_abs = idf_path.resolve()
    epw_abs = Path(epw).resolve()
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            [exe, "-w", str(epw_abs), "-d", str(work_abs), str(idf_abs)],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(work_abs))
    except subprocess.TimeoutExpired:
        return {"ok": False, "idf": str(idf_path),
                "reason": f"EnergyPlus timeout after {timeout}s"}
    except OSError as exc:
        return {"ok": False, "idf": str(idf_path),
                "reason": f"failed to launch EnergyPlus: {exc}"}
    runtime = round(time.perf_counter() - t0, 1)
    err_path = work_dir / "eplusout.err"
    err_txt = (err_path.read_text(encoding="utf-8", errors="replace")
               if err_path.is_file() else "")
    sql_ok = (work_dir / "eplusout.sql").is_file()
    completed = "Completed Successfully" in err_txt or (
        not err_txt and proc.returncode == 0)
    ok = proc.returncode == 0 and sql_ok and completed
    reason = None
    if not ok:
        bad = [ln.strip() for ln in err_txt.splitlines()
               if "Severe" in ln or "Fatal" in ln][:4]
        reason = "; ".join(bad) or (
            f"rc={proc.returncode} sql={sql_ok} "
            f"{(proc.stderr or '')[-300:].strip()}")
    return {
        "ok": ok,
        "idf": str(idf_path),
        "err": str(err_path) if err_path.is_file() else None,
        "rc": proc.returncode,
        "runtime_s": runtime,
        "reason": reason,
    }


_UNIT_FACTORS = {
    "gj": GJ_TO_KWH,
    "mj": 0.277778,
    "kj": 0.000277778,
    "kwh": 1.0,
    "mwh": 1000.0,
    "j": 1.0 / 3_600_000.0,
}


def _unit_factor(units: str) -> float | None:
    """kWh multiplier, or None for non-energy units (End Uses has a Water/m3
    row that must NOT enter the site-energy total)."""
    return _UNIT_FACTORS.get(units.strip().lower())


def _summarise(work_dir: Path, area_sqm: float, tariff: float) -> dict:
    """eplusout.sql AUBUPS 'End Uses' table -> kWh totals (fuel rows summed)."""
    sql = work_dir / "eplusout.sql"
    if not sql.is_file():
        raise FileNotFoundError("eplusout.sql not produced by the run")
    con = sqlite3.connect(str(sql))
    try:
        rows = con.execute(
            "SELECT RowName, ColumnName, Units, Value"
            " FROM TabularDataWithStrings"
            " WHERE ReportName='AnnualBuildingUtilityPerformanceSummary'"
            " AND TableName='End Uses'").fetchall()
    finally:
        con.close()
    if not rows:
        raise ValueError("End Uses table empty in eplusout.sql")
    total = 0.0
    by_end: dict[str, float] = {}
    units_seen: set[str] = set()
    for _row, col, units, value in rows:
        if _row in ("Total End Uses", "Time of Peak"):
            continue                              # totals row would double-count
        try:
            num = float(value)                    # Value column is TEXT in sql
        except (TypeError, ValueError):
            continue                              # blank/header/peak-time cells
        factor = _unit_factor(str(units or ""))
        if factor is None:
            continue                              # water / non-energy row
        kwh = num * factor
        if kwh == 0.0:
            continue
        units_seen.add(str(units or "").strip())
        total += kwh
        by_end[str(col)] = by_end.get(str(col), 0.0) + kwh
    eui = total / area_sqm if area_sqm else 0.0
    return {
        "eui_kwh_m2yr": round(eui, 1),
        "annual_kwh": round(total),
        "annual_cost_inr": round(total * tariff),
        "tariff_inr_kwh": tariff,
        "breakdown_kwh": {k: round(v) for k, v in
                          sorted(by_end.items(), key=lambda kv: -kv[1])
                          if v >= 1.0},
        "units_read": sorted(units_seen),
    }


def simulate(req: Requirements, d: Design, dd: dict, work_dir: Path,
             allow_download: bool | None = None) -> dict:
    """Full certified run for one design. ``dd`` = preliminary energy dict.

    Never raises: every failure path returns ``{"ok": False, "reason": ...}``
    so the pipeline's degree-day numbers survive an EnergyPlus miss.
    """
    t0 = time.perf_counter()
    base: dict = {"engine": ENGINE, "weather": None}

    def _fail(reason: str) -> dict:
        return {**base, "ok": False, "reason": reason,
                "runtime_s": round(time.perf_counter() - t0, 1)}

    try:
        exe = find_energyplus()
        if not exe:
            return _fail("EnergyPlus binary not found (install EnergyPlus or "
                         "set ENERGYPLUS_DIR / repo-local .energyplus/)")
        epw, winfo = resolve_weather(req.city, allow_download)
        base["weather"] = winfo
        if epw is None:
            return _fail(winfo.get("reason", "no EPW weather available"))
        version = exe_version(exe)
        idf = build_idf(req, d, dd.get("assumptions") or {}, version)
        run = run_idf(idf, epw, work_dir, exe, cfg.ENERGYPLUS_TIMEOUT)
        if not run.get("ok"):
            out = _fail(run.get("reason", "simulation failed"))
            out.update({k: v for k, v in run.items() if k != "ok"})
            return out
        summary = _summarise(work_dir,
                            float(dd.get("area_sqm") or 0.0),
                            float(dd.get("tariff_inr_kwh") or 8.0))
        return {
            **base,
            "ok": True,
            "engine": f"energyplus-{version}",
            **summary,
            "idf": run["idf"],
            "err": run.get("err"),
            "rc": run.get("rc"),
            "runtime_s": round(time.perf_counter() - t0, 1),
        }
    except Exception as exc:                     # never kill a pipeline run
        return _fail(f"{type(exc).__name__}: {exc}")
