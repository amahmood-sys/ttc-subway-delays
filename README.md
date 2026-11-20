# TTC Subway Delays

Where and why does the Toronto subway get delayed, and is my line running right now?

This is a Streamlit dashboard that pairs a live status board for Lines 1, 2 and 4 with an analysis of 92,519 historical delay records (2023–2026) from City of Toronto Open Data.

**Live demo:** https://ttc-subway-delays.onrender.com (Render free tier, so the first load can be slow)

## Findings

- **64% of logged incidents cause zero minutes of delay.** Counting incidents alone overstates how often riders are actually held up, so the dashboard reports delay minutes alongside counts.
- **10 of 127 delay causes account for 55% of total delay minutes.** Delay time is concentrated in a small set of causes.

## What it does

- **Live Status:** polls TTC's GTFS-Realtime service-alerts feed every 30 seconds and shows a status card per line.
- **History:** filter by date, line, weekday and minimum delay; charts cover monthly trends, hour of day, a day × hour heatmap, top stations and top causes.
- **Daily refresh:** `scripts/ttc_update.py` pulls new Open Data releases, runs the tests and only commits if they pass.

## Decisions & tradeoffs

- **Two sources, two jobs.** The live feed reports service alerts, not per-train delay minutes, so all quantitative analysis runs on the Open Data delay log. The live tab answers "is my line OK right now?"
- **30-second cache on the feed.** Streamlit re-runs the whole script on every refresh, so the fetch is cached for 30 seconds instead of hitting TTC on every re-run.
- **Cleaning messy fields.** Delay codes are mapped to plain-English causes, station names are normalized (the "STATION" suffix stripped, consistent casing), non-subway lines are dropped, and double-encoded characters in the newer Open Data descriptions are repaired.
- **Treat the feed as untrusted.** Responses are capped at 5 MB and 500 alerts, and alert text is sanitized before it is rendered. The daily job refuses to commit if the schema changes or the row count drops.

## What I'd do next

- Point the daily job at `main` (it currently writes to an `auto-update` branch) so the deployed app picks up new data on its own.
- Join the logged live alerts against the delay log to see how well alerts line up with recorded delays.

## Run it

```bash
pip install -r requirements.txt
streamlit run app.py              # http://localhost:8501
python data/download_data.py      # rebuild the delay dataset
pytest tests/
```

`WALKTHROUGH.md` is a line-by-line tour of `app.py`.

Built with Python, Streamlit, Plotly and pandas. Developed with Claude Code as a pair programmer.
