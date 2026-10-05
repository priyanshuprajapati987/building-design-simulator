"""Phase 3A: ML surrogate scorer (pure-Python ridge regression).

Predicts the hand-analysis design score from a numeric feature vector so the
genetic grid search can pre-screen candidate genomes before paying for a
real analysis. Trained online: every evaluated design appends one record to
an append-only dataset under ``output/surrogate/`` (gitignored runtime data),
and the model retrains from that dataset on demand.

Honesty rules baked in:
  * untrained (too few samples) -> predict() returns None, the GA falls back
    to evaluating every candidate - no fake predictions
  * the report/summary always quotes the training R^2 alongside predictions
  * in-sample R^2 only (small linear model) - it is a cheap ranking aid, not
    a substitute for the real analysis

No numpy/scikit-learn: closed-form ridge via Gaussian elimination on a
standardized ~19x19 design matrix (sub-millisecond at these sizes).
"""
from __future__ import annotations

import json
import math
import os
import threading
from pathlib import Path
from typing import Any

import config as cfg

N_FEATURES = 19
MIN_SAMPLES = 15                 # below this we refuse to predict
RIDGE_LAMBDA = 1e-3
_DATASET_VERSION = 1
_MAX_BYTES = 1_000_000           # rotate before retraining gets slow
_KEEP_ROWS = 6000                # rows retained on rotation (newest)

_DEFAULT_DIR = cfg.OUTPUT_DIR / "surrogate"
_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"key": None, "model": None}


def dataset_dir() -> Path:
    """Runtime data dir (override: SURROGATE_DIR env - tests use tmp paths)."""
    env = os.environ.get("SURROGATE_DIR", "").strip()
    return Path(env) if env else _DEFAULT_DIR


def dataset_path() -> Path:
    return dataset_dir() / "dataset.jsonl"


def model_path() -> Path:
    return dataset_dir() / "model.json"


# ---------------------------------------------------------------------------
# features
# ---------------------------------------------------------------------------

def featurize(d, req) -> list[float]:
    """Stable numeric feature vector for (Design, Requirements)."""
    sys_code = {"rc_frame": 0.0, "rc_frame_ductile": 1.0,
                "rc_dual": 2.0}.get(d.system, 0.0)
    zone = {"I": 1.0, "II": 2.0, "III": 3.0, "IV": 4.0, "V": 5.0}.get(
        req.seismic_zone or "II", 2.0)
    soil = {"I": 1.0, "II": 2.0, "III": 3.0}.get(req.soil_type, 2.0)
    return [
        float(d.bays_x), float(d.bays_y), d.bay_x_m, d.bay_y_m,
        float(d.floors), float(d.slab_t_mm), float(d.beam_w_mm),
        float(d.beam_d_mm), sys_code,
        1.0 if d.secondary else 0.0,
        1.0 if d.core else 0.0,
        float(d.wall_t_mm),
        d.plate_sqm, d.len_x_m, d.len_y_m, float(d.n_columns),
        zone, soil, float(req.budget_crores or 0.0),
    ]


# ---------------------------------------------------------------------------
# linear algebra (tiny, dependency-free)
# ---------------------------------------------------------------------------

def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Solve A x = b by Gauss-Jordan with partial pivoting (A is n x n)."""
    n = len(a)
    m = [[*a[i], b[i]] for i in range(n)]
    for i in range(n):
        piv = max(range(i, n), key=lambda r: abs(m[r][i]))
        if abs(m[piv][i]) < 1e-15:
            m[piv][i] = 1e-15                 # ridge keeps this unreachable
        m[i], m[piv] = m[piv], m[i]
        di = m[i][i]
        for j in range(i, n + 1):
            m[i][j] /= di
        for r in range(n):
            if r == i:
                continue
            f = m[r][i]
            if f:
                for j in range(i, n + 1):
                    m[r][j] -= f * m[i][j]
    return [m[i][n] for i in range(n)]


class SurrogateModel:
    """Standardized ridge regression: score ~ features."""

    def __init__(self, means: list[float], stds: list[float],
                 weights: list[float], n_samples: int, r2: float):
        self.means = means
        self.stds = stds
        self.weights = weights          # len = N_FEATURES + 1 (bias first)
        self.n_samples = n_samples
        self.r2 = round(float(r2), 4)

    def predict(self, features: list[float]) -> float:
        if len(features) != len(self.means):
            raise ValueError(f"expected {len(self.means)} features, "
                             f"got {len(features)}")
        z = [(f - mu) / sd for f, mu, sd in zip(features, self.means,
                                                 self.stds)]
        y = self.weights[0]
        y += sum(w * v for w, v in zip(self.weights[1:], z))
        return round(y, 2)

    def to_json(self) -> dict[str, Any]:
        return {
            "version": _DATASET_VERSION,
            "n_features": len(self.means),
            "means": [round(v, 6) for v in self.means],
            "stds": [round(v, 6) for v in self.stds],
            "weights": [round(v, 6) for v in self.weights],
            "n_samples": self.n_samples,
            "r2": self.r2,
        }

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> "SurrogateModel":
        if obj.get("version") != _DATASET_VERSION:
            raise ValueError("surrogate model version mismatch")
        if obj.get("n_features") != N_FEATURES:
            raise ValueError("surrogate feature-count mismatch")
        return cls(obj["means"], obj["stds"], obj["weights"],
                   obj["n_samples"], obj["r2"])


def train(x: list[list[float]], y: list[float],
          lam: float = RIDGE_LAMBDA) -> SurrogateModel:
    """Closed-form ridge regression on standardized features."""
    if len(x) != len(y) or not x:
        raise ValueError("empty or mismatched training data")
    n, p = len(x), len(x[0])
    if any(len(row) != p for row in x):
        raise ValueError("ragged feature matrix")
    if (any(not math.isfinite(v) for row in x for v in row)
            or any(not math.isfinite(v) for v in y)):
        raise ValueError("non-finite training data")

    means = [sum(row[j] for row in x) / n for j in range(p)]
    stds = []
    for j in range(p):
        var = sum((row[j] - means[j]) ** 2 for row in x) / n
        stds.append(math.sqrt(var) if var > 1e-12 else 1.0)
    z = [[(row[j] - means[j]) / stds[j] for j in range(p)] for row in x]

    # normal equations with bias column: (Zb^T Zb + lam I) w = Zb^T y
    m = p + 1
    a = [[0.0] * m for _ in range(m)]
    bt = [0.0] * m
    for i in range(n):
        row = [1.0] + z[i]
        for r in range(m):
            rr = row[r]
            if not rr:
                continue
            for c in range(m):
                a[r][c] += rr * row[c]
            bt[r] += rr * y[i]
    for k in range(m):                      # ridge (bias left unpenalized)
        a[k][k] += 0.0 if k == 0 else lam
    w = _solve(a, bt)

    y_bar = sum(y) / n
    ss_res = ss_tot = 0.0
    for i in range(n):
        pred = w[0] + sum(w[j + 1] * z[i][j] for j in range(p))
        ss_res += (y[i] - pred) ** 2
        ss_tot += (y[i] - y_bar) ** 2
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else 0.0
    return SurrogateModel(means, stds, w, n, r2)


# ---------------------------------------------------------------------------
# dataset (append-only jsonl) + singleton access
# ---------------------------------------------------------------------------

def _finite_row(obj: dict[str, Any]) -> bool:
    """True when the stored row is usable for training (no NaN/inf)."""
    feats = obj.get("features")
    if not isinstance(feats, list) or len(feats) != N_FEATURES:
        return False
    try:
        return (all(math.isfinite(f) for f in feats)
                and math.isfinite(obj["score"]))
    except (KeyError, TypeError, ValueError):
        return False


def load_dataset(path: Path | None = None) -> list[dict[str, Any]]:
    p = path or dataset_path()
    try:
        raw = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []                          # missing/unreadable: train on nothing
    rows: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue                        # tolerate a torn last line
        if (obj.get("version") == _DATASET_VERSION
                and _finite_row(obj)):
            rows.append(obj)
    return rows


def _rotate(p: Path) -> None:
    """Keep the dataset bounded: retain only the newest _KEEP_ROWS rows."""
    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines()
             if ln.strip()]
    p.write_text("\n".join(lines[-_KEEP_ROWS:]) + "\n", encoding="utf-8")


def record(features: list[float], score: float,
           path: Path | None = None) -> None:
    """Append one evaluated design (thread-safe, bounded dataset size).

    Non-finite inputs are dropped here so a single NaN/inf can never poison
    the training set (a poisoned row once produced r2=nan summaries)."""
    try:
        vals = [float(f) for f in features] + [float(score)]
    except (TypeError, ValueError):
        return
    if not all(math.isfinite(v) for v in vals):
        return
    p = path or dataset_path()
    row = {"version": _DATASET_VERSION, "features": [round(f, 6)
                                                     for f in vals[:-1]],
           "score": round(vals[-1], 2)}
    with _LOCK:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
            if p.stat().st_size > _MAX_BYTES:
                _rotate(p)
        except OSError:
            pass                            # dataset dir unavailable: skip


def get_surrogate(min_samples: int = MIN_SAMPLES,
                  path: Path | None = None) -> SurrogateModel | None:
    """Trained model from the dataset, or None when not enough data.

    Cached on (file mtime, size) - retraining a 19x19 ridge is sub-ms but
    the GA hits this once per generation."""
    p = path or dataset_path()
    try:
        stat = p.stat() if p.exists() else None
        # min_samples is part of the key: a stricter caller must not be
        # served a model cached under a looser threshold
        key = (str(p), stat.st_mtime_ns, stat.st_size,
               max(1, min_samples)) if stat else None
    except OSError:
        key = None
    if key is None:
        return None
    with _LOCK:
        if _CACHE["key"] == key and _CACHE["model"] is not None:
            return _CACHE["model"]
        rows = load_dataset(p)
        if len(rows) < max(1, min_samples):
            return None
        try:
            model = train([r["features"] for r in rows],
                          [r["score"] for r in rows])
        except (ValueError, ZeroDivisionError):
            return None
        _CACHE["key"] = key
        _CACHE["model"] = model
        return model


def stats(path: Path | None = None) -> dict[str, Any]:
    """Honest status for reports/summaries."""
    p = path or dataset_path()
    rows = load_dataset(p)
    model = get_surrogate(path=p) if len(rows) >= MIN_SAMPLES else None
    return {
        "samples": len(rows),
        "min_samples": MIN_SAMPLES,
        "trained": model is not None,
        "r2_train": model.r2 if model else None,
        "dataset": str(p),
    }


def reset_cache() -> None:
    """Test hook - drop the in-memory model cache."""
    with _LOCK:
        _CACHE["key"] = None
        _CACHE["model"] = None
