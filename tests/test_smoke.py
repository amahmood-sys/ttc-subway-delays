"""Smoke tests: the data files load with the expected schema and the app runs."""
import os

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DELAY_COLS = ["Date", "Time", "Day", "Station", "Code", "Description",
              "Min Delay", "Min Gap", "Bound", "Line", "Vehicle"]


def test_delay_csv_schema():
    df = pd.read_csv(os.path.join(ROOT, "data/ttc_subway_delays.csv"))
    assert list(df.columns) == DELAY_COLS
    assert len(df) > 0
    assert set(df["Line"].unique()) <= {"YU", "BD", "SHP"}
    assert pd.to_datetime(df["Date"], errors="coerce").notna().all()


def test_alerts_log_schema():
    path = os.path.join(ROOT, "data/live_alerts_log.csv")
    if not os.path.exists(path):
        pytest.skip("no alerts logged yet")
    df = pd.read_csv(path)
    assert {"alert_id", "lines", "header", "start_toronto"} <= set(df.columns)
    assert df["alert_id"].is_unique


def test_app_runs():
    from streamlit.testing.v1 import AppTest

    cwd = os.getcwd()
    os.chdir(ROOT)  # app.py reads data/ relative to the repo root
    try:
        at = AppTest.from_file("app.py", default_timeout=60).run()
    finally:
        os.chdir(cwd)
    assert not at.exception, at.exception
