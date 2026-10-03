"""2D plans, elevations, charts (matplotlib) and interactive 3D (plotly)."""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Rectangle

from .models import Design

PALETTE = {"A": "#2563eb", "B": "#7c3aed", "C": "#059669"}


def _color(d: Design) -> str:
    return PALETTE.get(d.id, "#334155")


# ---------------------------------------------------------------------------
# 2D floor plan
# ---------------------------------------------------------------------------

def floor_plan_png(d: Design, path: Path, dpi: int = 120) -> Path:
    L, W = d.len_x_m, d.len_y_m
    m = 3.0                                   # margin for dimensions
    fig_w = min(14.0, max(7.0, L + 2 * m + 1))
    scale = fig_w / (L + 2 * m)
    fig_h = (W + 2 * m + 2.5) * scale
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))

    # slab outline
    ax.add_patch(Rectangle((0, 0), L, W, fill=True, fc="#f8fafc", ec="#0f172a", lw=2.0))

    # beam grid
    xs = [i * d.bay_x_m for i in range(d.bays_x + 1)]
    ys = [j * d.bay_y_m for j in range(d.bays_y + 1)]
    for x in xs:
        ax.plot([x, x], [0, W], color="#94a3b8", lw=1.0, zorder=1)
    for y in ys:
        ax.plot([0, L], [y, y], color="#94a3b8", lw=1.0, zorder=1)

    # secondary beams (mid-bay lines)
    if d.secondary:
        for i in range(d.bays_x):
            x = xs[i] + d.bay_x_m / 2
            ax.plot([x, x], [0, W], color="#cbd5e1", lw=0.7, ls=(0, (4, 3)), zorder=1)
        for j in range(d.bays_y):
            y = ys[j] + d.bay_y_m / 2
            ax.plot([0, L], [y, y], color="#cbd5e1", lw=0.7, ls=(0, (4, 3)), zorder=1)

    # columns
    s = max(0.5, min(L, W) * 0.025)
    for x in xs:
        for y in ys:
            ax.add_patch(Rectangle((x - s / 2, y - s / 2), s, s,
                                   fc="#0f172a", ec="none", zorder=3))

    # structural core
    if d.core:
        cx = L / 2 - d.core_lx_m / 2
        cy = W / 2 - d.core_ly_m / 2
        ax.add_patch(Rectangle((cx, cy), d.core_lx_m, d.core_ly_m,
                               fc="#e2e8f0", ec="#7c3aed", hatch="///", lw=1.6, zorder=2))
        ax.text(L / 2, W / 2, "CORE", ha="center", va="center",
                fontsize=9, fontweight="bold", color="#7c3aed", zorder=4)

    # dimension labels
    ax.annotate("", xy=(0, -m * 0.55), xytext=(L, -m * 0.55),
                arrowprops=dict(arrowstyle="<->", color="#475569", lw=1.0))
    ax.text(L / 2, -m * 0.5 - 0.4, f"{L:.1f} m", ha="center", fontsize=9, color="#334155")
    ax.annotate("", xy=(-m * 0.55, 0), xytext=(-m * 0.55, W),
                arrowprops=dict(arrowstyle="<->", color="#475569", lw=1.0))
    ax.text(-m * 0.5 - 0.4, W / 2, f"{W:.1f} m", va="center", rotation=90,
            fontsize=9, color="#334155")
    for i, x in enumerate(xs):
        if i < d.bays_x:
            ax.text(x + d.bay_x_m / 2, W + 0.5, f"{d.bay_x_m:g}",
                    ha="center", fontsize=7, color="#64748b")

    ax.set_xlim(-m, L + m)
    ax.set_ylim(-m, W + m + 1.0)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title(f"{d.id} - {d.name}   |   plan {L:.1f} x {W:.1f} m   "
                 f"|   {d.plate_sqft:.0f} sqft/floor   |   {d.n_columns} columns",
                 fontsize=11, fontweight="bold", color=_color(d), pad=10)
    fig.tight_layout()
    path = Path(path)
    fig.savefig(path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return path


# ---------------------------------------------------------------------------
# elevation
# ---------------------------------------------------------------------------

def elevation_png(d: Design, path: Path, dpi: int = 120) -> Path:
    L, H = d.len_x_m, d.height_m
    m = 3.0
    fig_w = min(13.0, max(7.0, L + 2 * m))
    scale = fig_w / (L + 2 * m)
    fig_h = max(5.0, (H + 2 * m + 2) * scale * 0.55)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    ax.add_patch(Rectangle((0, 0), L, H, fc="#f1f5f9", ec="#0f172a", lw=1.8))
    for i in range(1, d.floors):
        z = i * d.floor_h_m
        ax.plot([0, L], [z, z], color="#94a3b8", lw=0.9, ls="--")
        ax.text(-0.4, z + 0.3, f"{i + 1}", ha="right", fontsize=6, color="#64748b")
    # ground
    ax.plot([-L * 0.05, L * 1.05], [0, 0], color="#0f172a", lw=2.5)
    # columns
    n = max(2, int(L // d.bay_x_m) + 1)
    for i in range(n):
        x = i * L / (n - 1)
        ax.plot([x, x], [0, H], color="#334155", lw=1.4, alpha=0.7)
    # height dimension
    ax.annotate("", xy=(L + m * 0.5, 0), xytext=(L + m * 0.5, H),
                arrowprops=dict(arrowstyle="<->", color="#475569", lw=1.0))
    ax.text(L + m * 0.55, H / 2, f"{H:.1f} m", va="center", rotation=90,
            fontsize=9, color="#334155")
    if d.core:
        ax.add_patch(Rectangle((L / 2 - d.core_lx_m / 2, 0),
                               d.core_lx_m, H, fc="#ede9fe",
                               ec="#7c3aed", hatch="///", lw=1.2))
    ax.set_xlim(-m, L + m * 1.6)
    ax.set_ylim(-0.5, H + m * 0.8)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title(f"{d.id} - elevation: {d.floors} floors x {d.floor_h_m} m",
                 fontsize=11, fontweight="bold", color=_color(d), pad=8)
    fig.tight_layout()
    path = Path(path)
    fig.savefig(path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return path


# ---------------------------------------------------------------------------
# charts
# ---------------------------------------------------------------------------

def seismic_chart_png(d: Design, analysis: dict, path: Path, dpi: int = 110) -> Path:
    sf = analysis["seismic"]["storey_forces"]
    floors = [r["floor"] for r in sf]
    forces = [r["F_kN"] for r in sf]
    drift = analysis["drift"]["storeys"]
    dx = [s["drift_x"] for s in drift]
    dy = [s["drift_y"] for s in drift]
    limit = analysis["drift"]["limit"]

    fig, axes = plt.subplots(1, 2, figsize=(10.5, 4.2))
    axes[0].barh(floors, forces, color=_color(d), alpha=0.85)
    axes[0].set_xlabel("floor seismic force F (kN)")
    axes[0].set_ylabel("floor")
    axes[0].set_title(f"Seismic force distribution (V base = "
                      f"{analysis['seismic']['V_base_kN']:.0f} kN)", fontsize=10)
    axes[0].grid(axis="x", alpha=0.3)

    axes[1].plot(dx, floors, "o-", color="#2563eb", label="drift X")
    axes[1].plot(dy, floors, "s-", color="#dc2626", label="drift Y")
    axes[1].axvline(limit, color="#16a34a", ls="--", lw=1.5, label=f"limit {limit}")
    axes[1].set_xlabel("inter-story drift index")
    axes[1].set_ylabel("floor")
    axes[1].set_title("Drift per storey", fontsize=10)
    axes[1].legend(fontsize=8)
    axes[1].grid(alpha=0.3)

    fig.suptitle(f"{d.id} - {d.name}", fontsize=11, fontweight="bold", color=_color(d))
    fig.tight_layout()
    path = Path(path)
    fig.savefig(path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return path


def _did(r) -> str:
    d = r["design"]
    return d["id"] if isinstance(d, dict) else d.id


def cost_chart_png(results: list[dict], path: Path, dpi: int = 110) -> Path:
    labels = []
    for r in results:
        d = r["design"]
        name = d["name"] if isinstance(d, dict) else d.name
        labels.append(f"{_did(r)} - {name[:24]}")
    totals = [r["cost"]["total_inr"] / 1e7 for r in results]
    colors = [PALETTE.get(_did(r), "#334155") for r in results]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.2))
    bars = ax1.bar(labels, totals, color=colors, alpha=0.9)
    for b, t in zip(bars, totals):
        ax1.text(b.get_x() + b.get_width() / 2, t, f"{t:.2f} Cr",
                 ha="center", va="bottom", fontsize=9, fontweight="bold")
    ax1.set_ylabel("total cost (Rs crore)")
    ax1.set_title("Cost comparison", fontsize=10)
    ax1.grid(axis="y", alpha=0.3)
    ax1.tick_params(axis="x", labelsize=8)

    # stacked breakdown
    keys = ["structure", "finishes", "mep_services", "external_and_misc"]
    bottoms = np.zeros(len(results))
    for k, color in zip(keys, ["#334155", "#2563eb", "#059669", "#d97706"]):
        vals = np.array([r["cost"]["breakdown_inr"][k] / 1e7 for r in results])
        ax2.bar(labels, vals, bottom=bottoms, label=k.replace("_", " "), color=color)
        bottoms += vals
    ax2.set_ylabel("Rs crore")
    ax2.set_title("Cost breakdown", fontsize=10)
    ax2.legend(fontsize=7)
    ax2.grid(axis="y", alpha=0.3)
    ax2.tick_params(axis="x", labelsize=8)

    fig.tight_layout()
    path = Path(path)
    fig.savefig(path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return path


def score_chart_png(results: list[dict], path: Path, dpi: int = 110) -> Path:
    labels = [_did(r) for r in results]
    scores = [r["score"] for r in results]
    colors = [PALETTE.get(_did(r), "#334155") for r in results]
    fig, ax = plt.subplots(figsize=(7.5, 3.6))
    ax.bar(labels, scores, color=colors, alpha=0.9, width=0.5)
    for x, s in zip(labels, scores):
        ax.text(x, s, f"{s:.0f}", ha="center", va="bottom", fontweight="bold")
    ax.set_ylim(0, 100)
    ax.set_ylabel("overall score (0-100)")
    ax.set_title("Design scoring: 40 compliance + 25 efficiency + 20 cost + 15 drift",
                 fontsize=9)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    path = Path(path)
    fig.savefig(path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return path


# ---------------------------------------------------------------------------
# 3D
# ---------------------------------------------------------------------------

def _box_verts(x0, x1, y0, y1, z0, z1):
    return [
        (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
        (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
    ]


_BOX_FACES = [
    (0, 1, 2), (0, 2, 3),   # bottom
    (4, 6, 5), (4, 7, 6),   # top
    (0, 5, 1), (0, 4, 5),   # front
    (2, 7, 3), (2, 6, 7),   # back
    (0, 3, 7), (0, 7, 4),   # left
    (1, 5, 6), (1, 6, 2),   # right
]


def three_d_html(d: Design, path: Path) -> Path:
    import plotly.graph_objects as go

    hexc = _color(d)
    fig = go.Figure()

    # slabs
    for i in range(d.floors):
        z0 = i * d.floor_h_m
        z1 = z0 + d.slab_t_mm / 1000.0
        verts = _box_verts(0, d.len_x_m, 0, d.len_y_m, z0, z1)
        idx, xs, ys, zs = [], [], [], []
        for v in verts:
            xs.append(v[0])
            ys.append(v[1])
            zs.append(v[2])
        for f in _BOX_FACES:
            idx.extend(f)
        fig.add_trace(go.Mesh3d(
            x=xs, y=ys, z=zs, i=idx[0::3], j=idx[1::3], k=idx[2::3],
            color=hexc, opacity=0.16, flatshading=True,
            name=f"floor {i + 1}", showlegend=(i == 0),
            hoverinfo="skip",
        ))

    # columns as vertical lines
    for xi in range(d.bays_x + 1):
        for yj in range(d.bays_y + 1):
            x, y = xi * d.bay_x_m, yj * d.bay_y_m
            fig.add_trace(go.Scatter3d(
                x=[x, x], y=[y, y], z=[0, d.height_m],
                mode="lines", line=dict(color="#0f172a", width=6),
                showlegend=False, hoverinfo="skip",
            ))

    # core
    if d.core:
        cx0 = d.len_x_m / 2 - d.core_lx_m / 2
        cy0 = d.len_y_m / 2 - d.core_ly_m / 2
        verts = _box_verts(cx0, cx0 + d.core_lx_m, cy0, cy0 + d.core_ly_m,
                           0, d.height_m)
        xs = [v[0] for v in verts]
        ys = [v[1] for v in verts]
        zs = [v[2] for v in verts]
        idx = []
        for f in _BOX_FACES:
            idx.extend(f)
        fig.add_trace(go.Mesh3d(
            x=xs, y=ys, z=zs, i=idx[0::3], j=idx[1::3], k=idx[2::3],
            color="#7c3aed", opacity=0.5, name="core", showlegend=True,
            hoverinfo="skip",
        ))

    fig.update_layout(
        title=f"{d.id} - {d.name} ({d.floors} floors, {d.height_m} m)",
        scene=dict(
            xaxis_title="X (m)", yaxis_title="Y (m)", zaxis_title="Z (m)",
            aspectmode="data",
            camera=dict(eye=dict(x=1.6, y=1.6, z=0.9)),
        ),
        margin=dict(l=0, r=0, t=40, b=0),
        legend=dict(orientation="h", y=1.05),
    )
    path = Path(path)
    fig.write_html(str(path), include_plotlyjs=True)
    return path


def three_d_png(d: Design, path: Path, dpi: int = 110) -> Path:
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    fig = plt.figure(figsize=(8, 7))
    ax = fig.add_subplot(111, projection="3d")
    hexc = _color(d)

    def add_box(x0, x1, y0, y1, z0, z1, color, alpha):
        v = _box_verts(x0, x1, y0, y1, z0, z1)
        quads = [[v[a], v[b], v[c]] for (a, b, c) in _BOX_FACES]
        ax.add_collection3d(Poly3DCollection(quads, facecolor=color,
                                             edgecolor="#334155",
                                             linewidths=0.25, alpha=alpha))

    for i in range(d.floors):
        z0 = i * d.floor_h_m
        add_box(0, d.len_x_m, 0, d.len_y_m, z0, z0 + d.slab_t_mm / 1000.0,
                hexc, 0.28)

    # columns
    for xi in range(d.bays_x + 1):
        for yj in range(d.bays_y + 1):
            x, y = xi * d.bay_x_m, yj * d.bay_y_m
            add_box(x - 0.15, x + 0.15, y - 0.15, y + 0.15, 0, d.height_m,
                    "#334155", 0.55)

    if d.core:
        cx0 = d.len_x_m / 2 - d.core_lx_m / 2
        cy0 = d.len_y_m / 2 - d.core_ly_m / 2
        add_box(cx0, cx0 + d.core_lx_m, cy0, cy0 + d.core_ly_m, 0, d.height_m,
                "#7c3aed", 0.45)

    ax.set_xlim(0, d.len_x_m)
    ax.set_ylim(0, d.len_y_m)
    ax.set_zlim(0, d.height_m)
    ax.set_box_aspect((d.len_x_m, d.len_y_m, max(d.height_m * 0.7, 1)))
    ax.set_xlabel("X (m)", fontsize=8)
    ax.set_ylabel("Y (m)", fontsize=8)
    ax.set_zlabel("Z (m)", fontsize=8)
    ax.view_init(elev=22, azim=-55)
    ax.tick_params(labelsize=7)
    ax.set_title(f"{d.id} - {d.name}", fontsize=11, fontweight="bold", color=hexc)
    fig.tight_layout()
    path = Path(path)
    fig.savefig(path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return path
