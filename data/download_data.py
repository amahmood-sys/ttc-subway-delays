"""
Build data/ttc_subway_delays.csv from the official TTC subway delay dataset
(City of Toronto Open Data, 2023 → present).

Sources:
  * 2023–2024: the official raw XLSX files, mirrored in a public GitHub repo
    (the Open Data portal blocks some cloud/CI IP ranges).
  * 2025 → present: the City of Toronto Open Data CKAN resource
    "TTC Subway Delay Data since 2025" — a single cumulative CSV the City
    refreshes roughly monthly:
        https://open.toronto.ca/dataset/ttc-subway-delay-data/

Delay codes are mapped to plain-English descriptions, rows are filtered to the
subway lines (1 = YU, 2 = BD, 4 = SHP), and a tidy CSV is written with the
schema the dashboard expects. Any existing output is backed up to
data/backups/ before it is overwritten.

Usage:
    python data/download_data.py
"""
import datetime
import io
import os
import shutil
import sys

import pandas as pd
import requests

RAW_BASE = "https://raw.githubusercontent.com/asvath/SmartTransit/main/data/raw"
DELAY_FILES = [
    f"{RAW_BASE}/delays/ttc-subway-delay-data-2023.xlsx",
    f"{RAW_BASE}/delays/ttc-subway-delay-data-2024.xlsx",
]
CODES_FILE = f"{RAW_BASE}/code_descriptions/ttc-subway-delay-codes.xlsx"

CKAN_BASE = "https://ckan0.cf.opendata.inter.prod-toronto.ca/api/3/action"
CKAN_PACKAGE = "ttc-subway-delay-data"
SINCE_2025_NAME = "TTC Subway Delay Data since 2025"
CODES_NAME = "Code Descriptions"

HEADERS = {"User-Agent": "Mozilla/5.0"}
OUT = "data/ttc_subway_delays.csv"
BACKUP_DIR = "data/backups"

FINAL_COLUMNS = [
    "Date", "Time", "Day", "Station", "Code",
    "Description", "Min Delay", "Min Gap", "Bound", "Line", "Vehicle",
]
RECENT_REQUIRED = {"Date", "Time", "Day", "Station", "Code",
                   "Min Delay", "Min Gap", "Bound", "Line", "Vehicle"}

# Newer exports may use route numbers instead of line codes — normalize so the
# output schema/values stay identical to what the dashboard expects.
LINE_ALIASES = {
    "1": "YU", "LINE 1": "YU", "YUS": "YU", "Y/U": "YU",
    "2": "BD", "LINE 2": "BD", "B/D": "BD",
    "4": "SHP", "LINE 4": "SHP", "SHEP": "SHP",
}


def ckan_resources() -> list:
    r = requests.get(f"{CKAN_BASE}/package_show", params={"id": CKAN_PACKAGE},
                     timeout=60, headers=HEADERS)
    r.raise_for_status()
    return r.json()["result"]["resources"]


def ckan_csv_url(resources: list, name: str):
    for res in resources:
        if res.get("name", "").strip() == f"{name}.csv":
            return res["url"]
    for res in resources:  # fallback: datastore dump
        if res.get("name", "").strip() == name and res.get("datastore_active"):
            return res["url"]
    return None


def load_code_map(resources: list) -> dict:
    r = requests.get(CODES_FILE, timeout=60, headers=HEADERS)
    r.raise_for_status()
    raw = pd.read_excel(io.BytesIO(r.content))
    code_map = {}
    # Two code tables side by side: SUB codes (cols 2,3) and SRT codes (cols 6,7)
    for _, row in raw.iterrows():
        for c_idx, d_idx in [(2, 3), (6, 7)]:
            code, desc = row.iloc[c_idx], row.iloc[d_idx]
            if isinstance(code, str) and isinstance(desc, str):
                code, desc = code.strip(), desc.strip()
                if code and "CODE" not in code:
                    code_map[code] = desc

    # Supplement with the Open Data code table. setdefault: only fills codes not
    # already known, so existing descriptions keep their current wording.
    url = ckan_csv_url(resources, CODES_NAME)
    if url:
        r = requests.get(url, timeout=60, headers=HEADERS)
        r.raise_for_status()
        codes = pd.read_csv(io.BytesIO(r.content))
        cols = {c.strip().upper(): c for c in codes.columns}
        if "CODE" in cols and "DESCRIPTION" in cols:
            for code, desc in zip(codes[cols["CODE"]], codes[cols["DESCRIPTION"]]):
                if isinstance(code, str) and isinstance(desc, str) and code.strip():
                    code_map.setdefault(code.strip(), _fix_mojibake(desc.strip()).capitalize())
    return code_map


def _fix_mojibake(text: str) -> str:
    """The Open Data code table has double-encoded UTF-8 (e.g. 'â' for an en dash)."""
    try:
        text = text.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return text.replace("\u2013", "-").replace("\u2014", "-")


def load_delays(resources: list) -> pd.DataFrame:
    frames = []
    for url in DELAY_FILES:
        r = requests.get(url, timeout=90, headers=HEADERS)
        r.raise_for_status()
        xls = pd.ExcelFile(io.BytesIO(r.content))
        for sheet in xls.sheet_names:
            frames.append(xls.parse(sheet))
        print(f"  fetched {url.split('/')[-1]}")

    url = ckan_csv_url(resources, SINCE_2025_NAME)
    if not url:
        raise RuntimeError(f"Open Data resource '{SINCE_2025_NAME}' not found")
    r = requests.get(url, timeout=180, headers=HEADERS)
    r.raise_for_status()
    recent = pd.read_csv(io.BytesIO(r.content))
    recent.columns = [c.strip() for c in recent.columns]
    recent = recent.drop(columns=["_id"], errors="ignore")
    missing = RECENT_REQUIRED - set(recent.columns)
    if missing:
        raise RuntimeError(f"Open Data schema changed — missing columns {sorted(missing)}")
    frames.append(recent)
    print(f"  fetched '{SINCE_2025_NAME}' ({len(recent):,} raw rows)")
    return pd.concat(frames, ignore_index=True)


def clean(df: pd.DataFrame, code_map: dict) -> pd.DataFrame:
    df = df.copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date"])
    df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
    df["Time"] = df["Time"].astype(str).str.strip().str[:5]

    df["Line"] = df["Line"].astype(str).str.strip().str.upper().replace(LINE_ALIASES)
    df = df[df["Line"].isin(["YU", "BD", "SHP"])]

    df["Description"] = (
        df["Code"].astype(str).str.strip().map(code_map)
        .fillna(df["Code"].astype(str).str.strip())
    )
    df["Station"] = (
        df["Station"].astype(str)
        .str.replace(" STATION", "", regex=False).str.title().str.strip()
    )

    for col in ("Min Delay", "Min Gap", "Vehicle"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)

    df["Bound"] = df["Bound"].fillna("").astype(str).str.strip()
    df = df[df["Station"].str.strip().ne("") & df["Station"].str.lower().ne("nan")]
    return df[FINAL_COLUMNS]


def backup_existing():
    """Copy the current output to data/backups/ before overwriting it."""
    if not os.path.exists(OUT):
        return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(BACKUP_DIR, f"ttc_subway_delays.{stamp}.csv")
    shutil.copy2(OUT, dest)
    return dest


def main():
    print("Building real TTC subway delay dataset (2023 → present)…")
    try:
        resources = ckan_resources()
        code_map = load_code_map(resources)
        print(f"  loaded {len(code_map)} delay-code descriptions")
        delays = load_delays(resources)
    except Exception as e:
        print(f"\nERROR: download failed — {e}")
        sys.exit(1)

    df = clean(delays, code_map)
    backup = backup_existing()
    if backup:
        print(f"  backed up previous CSV → {backup}")
    df.to_csv(OUT, index=False)
    print(f"\nSaved {len(df):,} real delay records → {OUT}")
    print("Date range:", df["Date"].min(), "–", df["Date"].max())
    print("Lines:", df["Line"].value_counts().to_dict())


if __name__ == "__main__":
    main()
