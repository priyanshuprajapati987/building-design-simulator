"""Shared dataclasses for the building design simulator."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

SQFT_TO_SQM = 0.092903


@dataclass
class Requirements:
    building_type: str = "residential"
    city: str = "Delhi"
    floors: int = 5                      # total above-ground floors (G included)
    land_area_sqft: float | None = None
    units_per_floor: int | None = None
    budget_crores: float | None = None
    special: list[str] = field(default_factory=list)
    seismic_zone: str | None = None       # explicit override (e.g. "IV")
    soil_type: str = "II"                 # IS 1893 soil: I / II / III
    vb: float | None = None               # IS 875 basic wind speed override (m/s)
    terrain_cat: int | None = None        # IS 875 terrain category 1-4
    floor_h_m: float = 3.2
    raw_text: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Design:
    id: str                               # "A" | "B" | "C"
    name: str
    system: str                           # rc_frame | rc_frame_ductile | rc_dual
    bay_x_m: float
    bay_y_m: float
    bays_x: int
    bays_y: int
    floors: int
    floor_h_m: float = 3.2
    slab_t_mm: int = 125
    secondary: bool = False               # secondary beams -> panel = bay/2
    beam_w_mm: int = 300
    beam_d_mm: int = 400
    sec_beam_d_mm: int = 300
    core: bool = False                    # structural shear-wall core
    core_lx_m: float = 0.0
    core_ly_m: float = 0.0
    wall_t_mm: int = 200
    column_boost: int = 0                 # optimization: shift column ladder up
    footing_bump: int = 0                 # optimization: +100 mm footing base each
    footing_t_mm: int = 0                 # footing thickness, 0 = derive from shear
    notes: str = ""

    @property
    def len_x_m(self) -> float:
        return round(self.bay_x_m * self.bays_x, 2)

    @property
    def len_y_m(self) -> float:
        return round(self.bay_y_m * self.bays_y, 2)

    @property
    def height_m(self) -> float:
        return round(self.floors * self.floor_h_m, 2)

    @property
    def plate_sqm(self) -> float:
        return self.len_x_m * self.len_y_m

    @property
    def plate_sqft(self) -> float:
        return self.plate_sqm / SQFT_TO_SQM

    @property
    def panel_m(self) -> float:
        """Clear slab panel span (bay, or bay/2 with secondary beams)."""
        return round(self.bay_x_m / 2, 2) if self.secondary else self.bay_x_m

    @property
    def n_columns(self) -> int:
        return (self.bays_x + 1) * (self.bays_y + 1)

    @property
    def gross_sqft(self) -> float:
        return self.plate_sqft * self.floors

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.update(
            len_x_m=self.len_x_m,
            len_y_m=self.len_y_m,
            height_m=self.height_m,
            plate_sqm=round(self.plate_sqm, 2),
            plate_sqft=round(self.plate_sqft, 1),
            panel_m=self.panel_m,
            n_columns=self.n_columns,
            gross_sqft=round(self.gross_sqft, 1),
        )
        return d


@dataclass
class Check:
    name: str
    value: float
    unit: str
    limit: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
