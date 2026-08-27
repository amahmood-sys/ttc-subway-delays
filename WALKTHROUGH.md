# Code Walkthrough — `app.py`

A line-by-line tour of the TTC Subway Delay Tracker, told by **following one
request end to end**: what happens, in order, from the moment someone opens the
page (or clicks a widget) to the moment charts and status cards appear.

---

## 0. The one idea that explains everything: Streamlit re-runs the whole script

Streamlit has no routes and no callbacks. **Every interaction re-runs `app.py`
from top to bottom.** Open the page → the script runs once. Move a slider →
the script runs *again*, start to finish. Click Refresh → runs again.

So "follow a request end-to-end" literally means "read the file top to bottom,"
with two twists:
- Functions (`def …`) are *defined* on the way down but only *execute* when
  called near the bottom (lines 401–442).
- `@st.cache_data` lets expensive work (loading the CSV, fetching the feed) be
  computed once and reused across re-runs, so the constant re-running stays fast.

Keep that in mind and the rest falls into place. We'll walk the file in the
order Python actually executes it.

---

## 1. Imports (lines 1–22)

```python
import datetime, os, re, time
from zoneinfo import ZoneInfo
import pandas as pd
import plotly.express as px
import requests
import streamlit as st
```
- `datetime` + `zoneinfo.ZoneInfo` — timestamps in Toronto time.
- `os` — read the feed URL from an environment variable.
- `re` — regex used by the feed-text sanitizer.
- `time` — `time.monotonic()` for the refresh throttle (a clock that only ever
  moves forward, ideal for measuring elapsed seconds).
- `pandas` — all the data wrangling.
- `plotly.express as px` — the charts.
- `requests` — HTTP call to the live feed.
- `streamlit as st` — the web framework.

```python
try:
    from google.transit import gtfs_realtime_pb2
    _GTFS_RT_OK = True
except ImportError:
    _GTFS_RT_OK = False
```
The GTFS-Realtime protobuf parser is an **optional** dependency. If it isn't
installed, we don't crash — we set a flag `_GTFS_RT_OK = False` and later show a
friendly message instead of the live panel. Same pattern for the auto-refresh
component:

```python
try:
    from streamlit_autorefresh import st_autorefresh
    _AUTOREFRESH_OK = True
except ImportError:
    _AUTOREFRESH_OK = False
```
This "import guard" pattern means the app degrades gracefully rather than
white-screening if a package is missing.

---

## 2. Page config + constants (lines 24–90)

```python
st.set_page_config(page_title="TTC Subway Delays — Live", page_icon="🚇",
                   layout="wide", initial_sidebar_state="expanded")
```
Must be the **first** Streamlit call. Sets the browser tab title/icon, uses the
full page width, and opens the sidebar by default.

```python
LINE_COLORS = {"YU": "#E3131B", "BD": "#009A44", "SHP": "#A8518A"}
LINE_LABELS = {"YU": "Line 1 · Yonge–University", ...}
DAY_ORDER = ["Monday", ... "Sunday"]
```
Lookup tables reused everywhere. `YU/BD/SHP` are the TTC's internal line codes;
the colors are the official line colors (Line 1 red, Line 2 green, Line 4
purple). `DAY_ORDER` fixes weekday ordering so charts read Mon→Sun, not
alphabetically.

```python
GTFS_RT_ALERTS_URL = os.environ.get("TTC_ALERTS_URL",
                                    "https://bustime.ttc.ca/gtfsrt/alerts")
REFRESH_SECONDS = 30
EASTERN = ZoneInfo("America/Toronto")
```
- The feed URL is **config, not a secret** — overridable via the `TTC_ALERTS_URL`
  env var, defaulting to TTC's public endpoint.
- `REFRESH_SECONDS = 30` is used in three places (auto-refresh interval, cache
  lifetime, and the caption) — one constant, one source of truth.
- `EASTERN` is the Toronto timezone; using the named zone (not a fixed offset)
  means EST/EDT switches automatically.

```python
MAX_FEED_BYTES = 5 * 1024 * 1024
MAX_ALERTS = 500
MAX_TEXT_LEN = 500
MANUAL_REFRESH_COOLDOWN = 5
```
Safety limits for the untrusted feed: cap the download at 5 MB, process at most
500 alerts, truncate any text field at 500 chars, and require 5 s between manual
refreshes.

```python
def _clean_feed_text(text: str) -> str:
    if not isinstance(text, str):
        return ""
    text = "".join(ch for ch in text if ch == "\n" or ch >= " ")
    text = re.sub(r"[*_`#\[\]<>]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > MAX_TEXT_LEN:
        text = text[:MAX_TEXT_LEN].rstrip() + "…"
    return text
```
Sanitizes text coming from the feed before it's rendered:
1. Non-strings → empty string (defensive).
2. `ch >= " "` keeps normal characters and newlines but drops control chars
   (anything with a code point below the space character).
3. The regex removes Markdown/HTML-significant characters so a feed can't inject
   formatting or markup into the page.
4. Collapse runs of whitespace to single spaces, trim ends.
5. Truncate to 500 chars with an ellipsis.

```python
_ROUTE_TO_LINE = {"1": "YU", "2": "BD", "4": "SHP"}
_SUBWAY_ROUTE_IDS = set(_ROUTE_TO_LINE.keys())
_LINE_KEYWORDS = {"YU": {"line 1", "yonge", ...}, ...}
```
The feed identifies routes by GTFS `route_id` (`"1"`, `"2"`, `"4"`). This maps
those to our codes. `_SUBWAY_ROUTE_IDS` is a `set` for fast membership tests.
`_LINE_KEYWORDS` is a fallback: if an alert has no machine-readable route, we
sniff the headline text for words like "yonge" or "danforth".

```python
_EFFECT_LABEL = {1: ("No Service", "error"), 3: ("Significant Delays", "warning"), ...}
_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2, "success": 3}
```
The GTFS spec encodes an alert's "effect" as a number; this maps each to a
human label plus a severity bucket that decides the card color. `_SEVERITY_ORDER`
lets us pick the *worst* alert on a line (lowest number = most severe).

---

## 3. Function definitions (lines 93–398) — defined now, called later

Python reads these `def` blocks but doesn't run them yet. We'll explain each at
the moment it's actually **called** below, which is the true request order.

---

## 4. The request begins here (lines 400–442)

This bottom block is what runs, in order, on every page load and every
interaction.

```python
df_all = load_data()
```
First real work. Jumps into `load_data` (line 94):

```python
@st.cache_data
def load_data() -> pd.DataFrame:
    df = pd.read_csv("data/ttc_subway_delays.csv", parse_dates=["Date"])
    required = {"Date", "Time", "Day", "Station", "Line", "Min Delay"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Dataset is missing required columns: {sorted(missing)}")
    df["Hour"] = pd.to_numeric(df["Time"].astype(str).str[:2], errors="coerce")
    df = df.dropna(subset=["Hour"])
    df["Hour"] = df["Hour"].astype(int)
    df["Month"] = df["Date"].dt.to_period("M").astype(str)
    df["Day"] = pd.Categorical(df["Day"], categories=DAY_ORDER, ordered=True)
    return df
```
- `@st.cache_data` — the 47k-row CSV is read **once**; every later re-run gets the
  cached DataFrame instantly. This is why constant re-running is cheap.
- Read the CSV, parsing `Date` as real datetimes.
- **Validate**: compute which required columns are missing (`set` difference);
  raise a clear error if the data shape is wrong, instead of failing cryptically
  three functions later.
- Derive `Hour` from the first two chars of `Time` (`"08:42"` → `8`).
  `errors="coerce"` turns anything malformed into `NaN`; we then drop those rows
  and cast to `int`.
- Derive `Month` (`"2024-03"`) for the monthly trend chart.
- Make `Day` an **ordered categorical** so it sorts Mon→Sun everywhere.

Back to the app body:

```python
with st.sidebar:
    st.title("🚇 TTC Delays")
    st.markdown("**Live status + delay analytics**")
    st.caption("Source: City of Toronto Open Data · TTC GTFS-RT")
    st.divider()
    st.markdown("**History filters**")
    st.caption("These apply to the History tab.")
    min_date = df_all["Date"].min().date()
    max_date = df_all["Date"].max().date()
    date_range = st.date_input("Date range", value=(min_date, max_date),
                               min_value=min_date, max_value=max_date)
    selected_lines = st.multiselect("Lines", options=["YU","BD","SHP"],
                       default=["YU","BD","SHP"], format_func=lambda x: LINE_LABELS[x])
    selected_days = st.multiselect("Days of week", options=DAY_ORDER, default=DAY_ORDER)
    min_delay = st.slider("Min. delay (minutes)", 0, 30, 0)
```
Everything in `with st.sidebar:` renders into the left panel. The widgets
(`date_input`, `multiselect`, `slider`) each **return their current value**. On
first load they return the defaults; after the user changes one, Streamlit
re-runs the whole script and the widget returns the *new* value here. `min/max_date`
clamp the date picker to the data's real range. `format_func` shows friendly line
names in the dropdown while keeping the short codes as values.

```python
if len(date_range) == 2:
    start, end = pd.Timestamp(date_range[0]), pd.Timestamp(date_range[1])
else:
    start, end = df_all["Date"].min(), df_all["Date"].max()
```
A date-range picker returns one date mid-selection and two once complete. This
guard avoids a crash during that in-between moment by falling back to the full
range.

```python
df = df_all[
    df_all["Date"].between(start, end)
    & df_all["Line"].isin(selected_lines)
    & df_all["Day"].isin(selected_days)
    & (df_all["Min Delay"] >= min_delay)
].copy()
```
The heart of the History filtering. Each line is a **boolean mask** over all
rows; `&` combines them (all conditions must hold); the result selects the
matching rows. `.copy()` avoids pandas "setting on a slice" warnings downstream.
Note `df_all` (everything) stays intact for the sidebar bounds; `df` is the
filtered view.

```python
st.title("TTC Subway Delay Tracker")
tab_live, tab_history = st.tabs(["🔴 Live Status", "📊 History"])
with tab_live:
    render_live()
with tab_history:
    render_history(df, start, end)
```
The page title, then two tabs. Streamlit renders **both** tab bodies on every
run (it just shows/hides them client-side), so both functions are always called.
Live gets no arguments (it fetches its own data); History gets the filtered `df`
and the date bounds.

---

## 5. Inside `render_live()` (lines 181–262)

```python
if _AUTOREFRESH_OK:
    st_autorefresh(interval=REFRESH_SECONDS * 1000, key="live_autorefresh")
```
If the auto-refresh component is available, schedule a re-run every 30 s
(interval is in milliseconds). This is what makes the panel "live" — it
re-triggers the whole script on a timer. The `key` gives the component a stable
identity across re-runs.

```python
if "refresh_count" not in st.session_state:
    st.session_state.refresh_count = 0
alerts, fetched_at, err = fetch_live_alerts(st.session_state.refresh_count)
```
`st.session_state` is a per-user dict that **survives re-runs** (unlike normal
variables, which are recreated every run). `refresh_count` is initialized once.
It's passed into `fetch_live_alerts` as its `cache_key`: incrementing it later is
how the Refresh button forces a fresh fetch past the cache.

### `fetch_live_alerts()` (lines 110–177)

```python
@st.cache_data(ttl=REFRESH_SECONDS)
def fetch_live_alerts(cache_key: int = 0):
    if not _GTFS_RT_OK:
        return [], datetime.datetime.now(EASTERN), "gtfs-realtime-bindings not installed"
```
- `ttl=30` — the result is cached for 30 seconds, so the auto-refresh's re-runs
  reuse a cached fetch instead of hammering TTC on every tick.
- The function returns a **3-tuple** everywhere: `(alerts, fetched_at, error)`.
  If the parser is missing, return empty alerts + an error message immediately.

```python
    try:
        resp = requests.get(GTFS_RT_ALERTS_URL, timeout=10,
                            headers={"User-Agent": "TTC-Dashboard/1.0"}, stream=True)
        resp.raise_for_status()
```
HTTP GET with a 10-second timeout and `stream=True` (don't download the whole
body yet — we want to size-check it as it arrives). `raise_for_status()` turns a
4xx/5xx into an exception caught below.

```python
        declared = resp.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > MAX_FEED_BYTES:
            return [], datetime.datetime.now(EASTERN), "Live feed response too large"
        content = bytearray()
        for chunk in resp.iter_content(chunk_size=16384):
            content.extend(chunk)
            if len(content) > MAX_FEED_BYTES:
                return [], datetime.datetime.now(EASTERN), "Live feed response too large"
```
Defense against a malicious/broken feed: first reject fast if the declared size
is over 5 MB, then stream in 16 KB chunks and bail the moment the accumulated
bytes exceed the cap. Either way memory can't blow up.

```python
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(bytes(content))
```
Parse the raw protobuf bytes into a structured `FeedMessage` object.

```python
        alerts = []
        for entity in feed.entity:
            if len(alerts) >= MAX_ALERTS:
                break
            if not entity.HasField("alert"):
                continue
            alert = entity.alert
```
Loop the feed entities. Stop after 500 alerts (bound the work). A feed entity can
be a vehicle position, trip update, *or* an alert — skip anything that isn't an
alert.

```python
            lines = {
                _ROUTE_TO_LINE[ie.route_id]
                for ie in alert.informed_entity
                if ie.route_id in _SUBWAY_ROUTE_IDS
            }
```
A set comprehension: for each entity this alert affects, if its `route_id` is a
subway route (`1/2/4`), translate it to our code. Result is the set of subway
lines this alert touches (e.g. `{"YU"}`).

```python
            header = _clean_feed_text(alert.header_text.translation[0].text
                                      if alert.header_text.translation else "")
            desc = _clean_feed_text(alert.description_text.translation[0].text
                                    if alert.description_text.translation else "")
```
GTFS text is translatable (a list of translations); take the first, or `""` if
none, and run it through the sanitizer from section 2.

```python
            if not lines:
                lower = header.lower()
                for line_key, keywords in _LINE_KEYWORDS.items():
                    if any(kw in lower for kw in keywords):
                        lines.add(line_key)
                if not lines and "subway" in lower:
                    lines = {"YU", "BD", "SHP"}
            if not lines:
                continue
```
Fallback for alerts with no machine-readable route: scan the headline for line
keywords; a generic "subway" alert applies to all three lines. If we still can't
attribute it to a subway line, skip it (it's probably a bus/streetcar alert).

```python
            label, severity = _EFFECT_LABEL.get(alert.effect, ("Service Advisory", "info"))
            alerts.append({"lines": lines, "header": header, "description": desc,
                           "label": label, "severity": severity})
        return alerts, datetime.datetime.now(EASTERN), None
    except Exception as exc:
        return [], datetime.datetime.now(EASTERN), str(exc)
```
Translate the numeric effect to a label + severity (default to a mild advisory),
build a tidy dict, and collect it. On success return `(alerts, now, None)`. **Any**
failure in the whole block returns `([], now, "<error text>")` — the app never
crashes on a feed problem; it degrades to a warning.

### Back in `render_live()` — rendering the result (lines 190–262)

```python
    hdr, btn = st.columns([6, 1])
    with hdr:
        st.subheader("🔴 Live Subway Status")
        auto = f"auto-refreshes every {REFRESH_SECONDS}s" if _AUTOREFRESH_OK else "manual refresh"
        st.caption(f"Last updated {fetched_at.strftime('%I:%M:%S %p %Z')} · {auto}")
```
Two columns in a 6:1 width ratio — a wide header and a narrow button. The
timestamp is formatted in Toronto time; `%Z` prints `EST`/`EDT` automatically.

```python
    with btn:
        if st.button("↻ Refresh", use_container_width=True):
            now_mono = time.monotonic()
            last = st.session_state.get("last_manual_refresh", 0.0)
            if now_mono - last < MANUAL_REFRESH_COOLDOWN:
                st.toast(f"Please wait {MANUAL_REFRESH_COOLDOWN}s between refreshes.")
            else:
                st.session_state.last_manual_refresh = now_mono
                st.session_state.refresh_count += 1
                st.rerun()
```
`st.button(...)` returns `True` on the run where it was just clicked. The throttle
compares the current monotonic clock to the last accepted refresh: within 5 s →
show a toast and do nothing; otherwise record the time, bump `refresh_count`
(which changes `fetch_live_alerts`'s cache key → forces a real fetch), and
`st.rerun()` to restart the script immediately.

```python
    if err:
        st.warning(f"Could not reach the TTC live feed: {err}")
        st.caption("The live feed is provided by TTC and may be temporarily unavailable, ...")
        return
```
If the fetch failed, show a warning + explanation and **stop here** — don't try to
render status cards from missing data.

```python
    line_alerts = {"YU": [], "BD": [], "SHP": []}
    for a in alerts:
        for line in a["lines"]:
            if line in line_alerts:
                line_alerts[line].append(a)
    lines_with_issues = [k for k, v in line_alerts.items() if v]
```
Re-index the flat alert list into per-line buckets (one alert affecting two lines
lands in both). `lines_with_issues` is the list of lines that have any alert.

```python
    if not lines_with_issues:
        st.success("✅ All three subway lines are running normally — no active alerts.")
    else:
        names = ", ".join(LINE_LABELS[k].split("·")[0].strip() for k in lines_with_issues)
        st.warning(f"⚠️ {len(lines_with_issues)} line(s) reporting issues: {names}")
    st.write("")
```
The system-wide banner: green all-clear, or an amber summary naming the affected
lines (`split("·")[0]` grabs just "Line 1" from the full label). `st.write("")`
adds a little vertical spacing.

```python
    cols = st.columns(3)
    for col, line_key in zip(cols, ["YU", "BD", "SHP"]):
        with col:
            for_line = line_alerts[line_key]
            color = LINE_COLORS[line_key]
            st.markdown(f"<span style='color:{color};font-weight:700;font-size:1.05rem'>"
                        f"{LINE_LABELS[line_key]}</span>", unsafe_allow_html=True)
```
Three equal columns, one per line. The line name is drawn in its official color
via a small inline HTML span (`unsafe_allow_html=True` is safe here — the string
is ours, not from the feed).

```python
            if not for_line:
                st.success("On Time")
            else:
                worst = min(for_line, key=lambda a: _SEVERITY_ORDER[a["severity"]])
                {"error": st.error, "warning": st.warning,
                 "info": st.info, "success": st.success}[worst["severity"]](worst["label"])
                for a in for_line:
                    if a["header"]:
                        st.markdown(f"**{a['header']}**")
                    if a["description"]:
                        with st.expander("Details"):
                            st.write(a["description"])
```
No alerts → green "On Time". Otherwise pick the **most severe** alert
(`min` by severity rank) and render its label with the matching Streamlit status
box — the dict maps a severity to `st.error/warning/info/success` and immediately
calls it with `(worst["label"])`. Then list each alert's headline, with full text
tucked inside an expandable "Details" section.

```python
    st.divider()
    st.caption("Live data: TTC GTFS-Realtime service alerts feed. ...")
```
A closing note explaining that the subway feed reports service alerts, not
per-train delay minutes.

---

## 6. Inside `render_history(df, start, end)` (lines 266–397)

```python
    st.caption(f"Showing **{len(df):,}** delay events · "
               f"{start.strftime('%b %d, %Y')} – {end.strftime('%b %d, %Y')}")
    if df.empty:
        st.warning("No data matches the current filters.")
        return
```
A summary line (`:,` adds thousands separators). If the filters excluded
everything, say so and stop before trying to chart an empty frame.

```python
    worst_station = df.groupby("Station")["Min Delay"].count().idxmax()
    worst_line_label = LINE_LABELS[df.groupby("Line")["Min Delay"].count().idxmax()].split("·")[1].strip()
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Total Delays", f"{len(df):,}")
    k2.metric("Avg Delay", f"{df['Min Delay'].mean():.1f} min")
    k3.metric("Total Delay Time", f"{df['Min Delay'].sum():,} min")
    k4.metric("Most Delayed Line", worst_line_label)
    k5.metric("Most Delayed Station", worst_station)
    st.divider()
```
Five KPI cards. `groupby(...).count().idxmax()` finds the group with the most
rows — i.e. the station/line with the most delay events. `.metric` renders the
big-number cards.

The rest of the function builds **six Plotly charts**, all following the same
three-step recipe, so once you read one you can read them all:

1. **Aggregate** with pandas `groupby(...).agg(...)` into a small summary frame.
2. **Build** a Plotly figure (`px.bar`, `px.line`, `px.imshow`, `px.box`), passing
   `color_discrete_map=LINE_COLORS` so every chart uses the official line colors.
3. **Style** with `fig.update_layout(...)` (transparent backgrounds so it blends
   with the page) and draw with `st.plotly_chart(fig, use_container_width=True)`.

Chart by chart:

- **Delays by Line** (lines 291–308) — `px.bar` of event counts per line, labeled
  with the count, avg delay on hover.
- **Monthly Delay Trend** (309–320) — `px.line` of counts per month per line;
  `xaxis_tickangle=-45` slants the month labels.
- **Delays by Hour of Day** (324–333) — stacked bar (`barmode="stack"`) of counts
  by hour; `dtick=2` labels every other hour. This is where the derived `Hour`
  column pays off.
- **Delay Heatmap: Day × Hour** (334–344) — `groupby(["Day","Hour"])` then
  `.pivot(...)` into a Day×Hour grid; `px.imshow` draws it as a heatmap.
  `reindex(DAY_ORDER)` forces Mon→Sun rows.
- **Top 15 Stations** (348–362) — `nlargest(15, "Count")` then a horizontal bar
  (`orientation="h"`), colored by line.
- **Top Delay Causes** (363–377) — counts per cause, colored by the *average*
  delay (`color_continuous_scale="OrRd"`) so long-average causes stand out.
- **Delay Duration Distribution** (379–389) — `px.box` showing the spread of delay
  minutes per line, with outliers.

```python
    with st.expander("Raw delay data"):
        st.dataframe(df[[...]].sort_values(["Date","Time"], ascending=False)
                     .reset_index(drop=True), use_container_width=True, height=300)
```
Finally, a collapsible raw-data table (newest first) so the underlying rows are
inspectable.

---

## 7. The whole request, in one paragraph

Someone opens the page → Streamlit runs `app.py` top to bottom → `load_data()`
returns the cached 47k-row DataFrame → the sidebar widgets render and return their
current values → those values build a boolean mask that produces the filtered
`df` → the two tabs render: **Live** fetches (or reuses a ≤30 s cached) TTC feed,
buckets alerts per line, and paints the banner + three status cards; **History**
aggregates `df` into KPI cards and six Plotly charts. Any widget change, the 30 s
auto-refresh, or the Refresh button simply runs this entire story again — and
caching keeps each re-run fast.
