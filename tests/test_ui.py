"""Streamlit UI tests (AppTest - runs the real app script in-process)."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parents[1] / "ui" / "web_app.py")


@pytest.fixture
def at():
    return AppTest.from_file(APP, default_timeout=120)


def test_welcome_screen_no_exceptions(at):
    at.run()
    assert not at.exception
    assert any("Welcome" in md.value for md in at.markdown)


def test_sidebar_form_present(at):
    at.run()
    assert not at.exception
    # brief textarea + number inputs + run button
    assert len(at.text_area) >= 1
    assert len(at.button) >= 1
    assert at.button[0].label.strip().endswith("Run analysis")


def test_full_run_renders_results(at):
    at.run()
    assert not at.exception
    at.button[0].set_value(True).run()
    assert not at.exception, str([str(e.value) for e in at.exception])
    headers = [h.value for h in at.header]
    assert headers and "Results" in headers[0]
    assert len(at.tabs) == 6
    # winner metric rendered
    metric_labels = [m.label for m in at.metric]
    assert any("Winner" in lbl for lbl in metric_labels)
