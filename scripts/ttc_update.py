"""
Daily TTC data update (run by a scheduled job, 6:00 AM America/Toronto).

  1. Poll the TTC GTFS-RT service-alerts feed and append new subway alerts to
     data/live_alerts_log.csv (dedupe by alert ID). Uses the same hardening as
     app.py / SECURITY.md: 5 MB streamed cap, MAX_ALERTS bound, _clean_feed_text.
  2. Check the Open Data "TTC Subway Delay Data since 2025" resource. If its
     last_modified changed, run data/download_data.py and verify the cleaned
     CSV's row count went up and its header (schema) is unchanged.
  3. If anything changed and nothing failed: run tests, commit
     "data: TTC update YYYY-MM-DD (+N rows)", push to origin/auto-update.
  4. If the feed is down or the schema changed: no commit, report instead.

Prints a short human report to stdout ONLY when something changed or failed —
empty stdout means "no new data" (the scheduler then sends nothing).

Flags: --dry-run (no commit/push), --verbose (always print a report).
"""
import argparse
import csv
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
from zoneinfo import ZoneInfo

import pandas as pd
import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)
PY = sys.executable
BRANCH = "auto-update"
TZ = ZoneInfo("America/Toronto")

# ── Feed hardening (mirrors app.py; see SECURITY.md) ──────────────────────────
ALERTS_URL = os.environ.get("TTC_ALERTS_URL", "https://bustime.ttc.ca/gtfsrt/alerts")
MAX_FEED_BYTES = 5 * 1024 * 1024
MAX_ALERTS = 500
MAX_TEXT_LEN = 500
_ROUTE_TO_LINE = {"1": "YU", "2": "BD", "4": "SHP"}
_LINE_KEYWORDS = {
    "YU": {"line 1", "yonge", "university", "yus"},
    "BD": {"line 2", "bloor", "danforth"},
    "SHP": {"line 4", "sheppard"},
}
ALERT_ID_RE = re.compile(r"[^A-Za-z0-9_.:\-]")

ALERTS_LOG = "data/live_alerts_log.csv"
ALERT_COLS = ["alert_id", "lines", "header", "description",
              "start_toronto", "end_toronto", "logged_at_toronto"]
DELAYS_CSV = "data/ttc_subway_delays.csv"
STATE_FILE = "data/opendata_state.json"
CKAN_URL = ("https://ckan0.cf.opendata.inter.prod-toronto.ca/api/3/action/"
            "package_show?id=ttc-subway-delay-data")
RESOURCE_NAME = "TTC Subway Delay Data since 2025"


def _clean_feed_text(text) -> str:
    """Identical rules to app._clean_feed_text."""
    if not isinstance(text, str):
        return ""
    text = "".join(ch for ch in text if ch == "\n" or ch >= " ")
    text = re.sub(r"[*_`#\[\]<>]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > MAX_TEXT_LEN:
        text = text[:MAX_TEXT_LEN].rstrip() + "…"
    return text


def _csv_safe(text: str) -> str:
    """Prevent spreadsheet formula injection when the log is opened in Excel."""
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def _ts(epoch: int) -> str:
    if not epoch:
        return ""
    return datetime.datetime.fromtimestamp(epoch, TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def fetch_feed_bytes() -> bytes:
    resp = requests.get(ALERTS_URL, timeout=15, stream=True,
                        headers={"User-Agent": "TTC-Dashboard/1.0"})
    resp.raise_for_status()
    declared = resp.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > MAX_FEED_BYTES:
        raise RuntimeError("feed response too large (>5 MB)")
    content = bytearray()
    for chunk in resp.iter_content(chunk_size=16384):
        content.extend(chunk)
        if len(content) > MAX_FEED_BYTES:
            raise RuntimeError("feed response too large (>5 MB)")
    return bytes(content)


def poll_alerts() -> int:
    """Append new subway alerts. Returns number of rows added. Raises on feed failure."""
    from google.transit import gtfs_realtime_pb2

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(fetch_feed_bytes())

    existing = set()
    if os.path.exists(ALERTS_LOG):
        with open(ALERTS_LOG, newline="", encoding="utf-8") as f:
            existing = {row["alert_id"] for row in csv.DictReader(f)}

    now = datetime.datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    new_rows, seen = [], 0
    for entity in feed.entity:
        if seen >= MAX_ALERTS:
            break
        if not entity.HasField("alert"):
            continue
        seen += 1
        alert = entity.alert
        alert_id = ALERT_ID_RE.sub("", entity.id)[:100]
        if not alert_id or alert_id in existing:
            continue
        lines = {_ROUTE_TO_LINE[ie.route_id] for ie in alert.informed_entity
                 if ie.route_id in _ROUTE_TO_LINE}
        header = _clean_feed_text(alert.header_text.translation[0].text
                                  if alert.header_text.translation else "")
        desc = _clean_feed_text(alert.description_text.translation[0].text
                                if alert.description_text.translation else "")
        if not lines:  # same keyword fallback as app.py
            lower = header.lower()
            for key, kws in _LINE_KEYWORDS.items():
                if any(kw in lower for kw in kws):
                    lines.add(key)
            if not lines and "subway" in lower:
                lines = {"YU", "BD", "SHP"}
        if not lines:
            continue
        period = alert.active_period[0] if alert.active_period else None
        new_rows.append({
            "alert_id": alert_id,
            "lines": "|".join(sorted(lines)),
            "header": _csv_safe(header),
            "description": _csv_safe(desc),
            "start_toronto": _ts(period.start) if period else "",
            "end_toronto": _ts(period.end) if period else "",
            "logged_at_toronto": now,
        })
        existing.add(alert_id)

    if new_rows:
        write_header = not os.path.exists(ALERTS_LOG)
        with open(ALERTS_LOG, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=ALERT_COLS)
            if write_header:
                w.writeheader()
            w.writerows(new_rows)
    return len(new_rows)


def opendata_last_modified() -> str:
    r = requests.get(CKAN_URL, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    for res in r.json()["result"]["resources"]:
        if res.get("name", "").startswith(RESOURCE_NAME) and res.get("last_modified"):
            return res["last_modified"]
    raise RuntimeError(f"resource '{RESOURCE_NAME}' not found on Open Data")


def csv_shape(path):
    with open(path, newline="", encoding="utf-8") as f:
        header = next(csv.reader(f))
    return header, len(pd.read_csv(path))


def refresh_delays(state: dict) -> tuple[int, str | None]:
    """Returns (rows_added, error). Restores the previous CSV on any failure."""
    last_mod = opendata_last_modified()
    if state.get("since_2025_last_modified") == last_mod:
        return 0, None
    old_header, old_rows = csv_shape(DELAYS_CSV)
    safety = DELAYS_CSV + ".pre-update"
    shutil.copy2(DELAYS_CSV, safety)
    try:
        proc = subprocess.run([PY, "data/download_data.py"], capture_output=True,
                              text=True, timeout=900)
        if proc.returncode != 0:
            raise RuntimeError("download_data.py failed: "
                               + (proc.stdout + proc.stderr).strip()[-300:])
        new_header, new_rows = csv_shape(DELAYS_CSV)
        if new_header != old_header:
            raise RuntimeError(f"schema changed: {old_header} → {new_header}")
        if new_rows <= old_rows:
            if new_rows < old_rows:
                raise RuntimeError(f"row count dropped {old_rows:,} → {new_rows:,}")
            state["since_2025_last_modified"] = last_mod  # same data, re-stamped
            os.remove(safety)
            return 0, None
    except Exception as exc:
        shutil.copy2(safety, DELAYS_CSV)
        os.remove(safety)
        return 0, f"delay dataset: {exc} (previous CSV restored, nothing committed)"
    os.remove(safety)
    state["since_2025_last_modified"] = last_mod
    return new_rows - old_rows, None


def run_tests() -> str | None:
    if os.path.isdir("tests"):
        proc = subprocess.run([PY, "-m", "pytest", "-q", "tests"],
                              capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            return "tests failed: " + proc.stdout.strip()[-400:]
    return None


def git(*args) -> str:
    proc = subprocess.run(["git", *args], capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()[-300:]}")
    return proc.stdout.strip()


def rows_vs_head(path: str) -> int:
    """Data rows in the working file minus data rows in the committed (HEAD) version."""
    if not os.path.exists(path):
        return 0
    now = len(pd.read_csv(path))
    proc = subprocess.run(["git", "show", f"HEAD:{path}"], capture_output=True, text=True)
    if proc.returncode != 0:  # file not yet tracked
        return now
    from io import StringIO
    return now - len(pd.read_csv(StringIO(proc.stdout)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    today = datetime.datetime.now(TZ).strftime("%Y-%m-%d")
    errors, notes = [], []

    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if branch != BRANCH:
        print(f"TTC update {today}: ABORTED — repo is on '{branch}', expected '{BRANCH}'.")
        return
    try:  # pick up anything pushed from elsewhere; never touches main
        git("fetch", "origin", BRANCH)
        git("merge", "--ff-only", f"origin/{BRANCH}")
    except RuntimeError:
        pass  # remote branch may not exist yet

    state = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}

    alerts_added, feed_down = 0, False
    try:
        alerts_added = poll_alerts()
    except Exception as exc:
        feed_down = True
        errors.append(f"alerts feed down/unreadable: {exc}")

    delay_rows = 0
    try:
        delay_rows, err = refresh_delays(state)
        if err:
            errors.append(err)
    except Exception as exc:
        errors.append(f"Open Data check failed: {exc}")
    schema_problem = any("schema changed" in e or "row count dropped" in e for e in errors)

    if delay_rows:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
        notes.append(f"delay dataset +{delay_rows:,} rows")
    if alerts_added:
        notes.append(f"{alerts_added} new subway alert(s) logged")

    # Also pick up data changes left uncommitted by an earlier failed/dry run.
    paths = [p for p in (DELAYS_CSV, ALERTS_LOG, STATE_FILE) if os.path.exists(p)]
    pending = bool(git("status", "--porcelain", "--", *paths))
    if pending and not (delay_rows or alerts_added):
        notes.append("committing data changes pending from a previous run")
    changed = bool(delay_rows or alerts_added or pending)
    committed = False
    if changed and not feed_down and not schema_problem:
        test_err = run_tests()
        if test_err:
            errors.append(test_err + " — not committed")
        elif not args.dry_run:
            total = rows_vs_head(DELAYS_CSV) + rows_vs_head(ALERTS_LOG)
            git("add", "--", *paths)
            if git("diff", "--cached", "--name-only"):
                git("commit", "-m", f"data: TTC update {today} (+{total:,} rows)")
                git("push", "origin", f"HEAD:refs/heads/{BRANCH}")
                committed = True
    elif changed:
        notes.append("changes left uncommitted because of the failure below")

    if errors or changed or args.verbose:
        lines = [f"🚇 TTC update {today}"]
        lines += [f"• {n}" for n in notes] or ["• no new data"]
        if committed:
            lines.append(f"• committed + pushed to {BRANCH}")
        elif args.dry_run and changed:
            lines.append("• dry run — not committed")
        lines += [f"⚠️ {e}" for e in errors]
        print("\n".join(lines))


if __name__ == "__main__":
    main()
