"""Phase-3A: ML surrogate scorer (pure-Python ridge)."""
from __future__ import annotations

import json
import math
import random

import pytest

from modules import surrogate as S


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Every test works against a fresh tmp dataset dir + cold cache."""
    monkeypatch.setenv("SURROGATE_DIR", str(tmp_path))
    S.reset_cache()
    yield
    S.reset_cache()


def _linear_rows(n=40, seed=7):
    rng = random.Random(seed)
    x, y = [], []
    for _ in range(n):
        row = [rng.uniform(-5, 5) for _ in range(S.N_FEATURES)]
        x.append(row)
        y.append(2.0 * row[0] - 1.5 * row[3] + 0.7 * row[8] + 5.0)
    return x, y


# ---------------------------------------------------------------------------
# training
# ---------------------------------------------------------------------------

def test_exact_linear_fit_r2():
    x, y = _linear_rows()
    m = S.train(x, y)
    assert m.r2 > 0.99
    assert abs(m.predict(x[0]) - y[0]) < 0.5


def test_train_rejects_ragged_and_empty():
    with pytest.raises(ValueError):
        S.train([[1.0, 2.0], [1.0]], [1.0, 2.0])
    with pytest.raises(ValueError):
        S.train([], [])


def test_constant_features_do_not_crash():
    # zero-variance column -> std guard returns 1.0, solve stays finite
    x = [[5.0] + [float(i) for i in range(S.N_FEATURES - 1)] for i in range(20)]
    y = [float(i) for i in range(20)]
    m = S.train(x, y)
    assert all(math.isfinite(w) for w in m.weights)   # no NaN/inf
    assert m.predict(x[0]) == m.predict(x[0])         # stable


def test_json_roundtrip_and_version_gate():
    x, y = _linear_rows()
    m = S.train(x, y)
    blob = json.dumps(m.to_json())
    m2 = S.SurrogateModel.from_json(json.loads(blob))
    assert m2.predict(x[0]) == m.predict(x[0])
    assert m2.r2 == m.r2
    with pytest.raises(ValueError):
        S.SurrogateModel.from_json({"version": 99, "n_features": S.N_FEATURES})
    with pytest.raises(ValueError):
        S.SurrogateModel.from_json({"version": S._DATASET_VERSION,
                                    "n_features": 3})


# ---------------------------------------------------------------------------
# dataset + singleton
# ---------------------------------------------------------------------------

def test_untrained_returns_none(tmp_path):
    assert S.get_surrogate() is None
    st = S.stats()
    assert st["samples"] == 0 and st["trained"] is False
    assert st["r2_train"] is None


def test_record_threshold_and_stats():
    S.record([0.0] * S.N_FEATURES, 50.0)
    assert S.get_surrogate() is None               # below MIN_SAMPLES
    for i in range(S.MIN_SAMPLES):
        S.record([float(i)] * S.N_FEATURES, 50.0 + i)
    model = S.get_surrogate()
    assert model is not None
    st = S.stats()
    assert st["trained"] is True
    assert st["samples"] == S.MIN_SAMPLES + 1
    assert st["r2_train"] is not None
    assert "dataset" in st


def test_corrupt_lines_tolerated():
    S.record([1.0] * S.N_FEATURES, 60.0)
    p = S.dataset_path()
    with p.open("a", encoding="utf-8") as fh:
        fh.write('{"version": 1, "features": [1, 2}\n')   # torn line
        fh.write("not json at all\n")
    rows = S.load_dataset()
    assert len(rows) == 1
    assert rows[0]["score"] == 60.0


def test_reset_cache_reloads():
    S.record([0.0] * S.N_FEATURES, 10.0)
    for i in range(S.MIN_SAMPLES):
        S.record([float(i)] * S.N_FEATURES, 50.0 + i)
    first = S.get_surrogate()
    assert first is not None
    cached = S.get_surrogate()
    assert cached is first                         # cache hit (same file key)
    S.reset_cache()
    fresh = S.get_surrogate()
    assert fresh is not first and fresh.r2 == first.r2
