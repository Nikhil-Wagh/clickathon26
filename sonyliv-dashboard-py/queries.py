"""SQL for the dashboard, ported from the React app's `lib/queries.ts`.

All queries use ClickHouse server-side named params ({name:Type}). Dimension
filters (`platforms`/`countries`/`video_types`/`categories`) are Array(String):
an empty array means "all values" for that dimension, matching multi-select UI.
`content_id` stays a single value (empty string = all) since content is picked
one-at-a-time. Empty `from`/`to` means "full range".

CORRECTNESS RULE (do not violate): `concurrency_now` has one row per
(dims, minute, content_id) — never `avg(concurrent)` directly on it. Always sum
`concurrent` per minute for the requested grouping FIRST (a per-minute CTE),
then take max/avg of those per-minute totals. Otherwise you get the average
concurrency of individual content/dim cells, not of the whole group.

Queries read only base tables/views (`concurrency_now`, `content_dim`,
`content_dict`, `events_raw`) — never the materialized views that feed them
(e.g. `mv_session_intervals`), which hold no queryable data of their own.

Note vs the React version: the curve/KPI queries alias the time column to `ts`
(not `minute`) to avoid an alias-vs-column collision that ClickHouse rejects
(`NO_COMMON_TYPE`) when a String alias shadows the DateTime `minute` in WHERE.
"""

from __future__ import annotations

import pandas as pd

from clickhouse_client import query_df
from config import DB

# --- Filter dropdowns + time bounds ------------------------------------------
_Q_DISTINCT = (
    "SELECT DISTINCT {col} AS v FROM " + DB + ".concurrency_now "
    "WHERE {col} != '' ORDER BY v"
)
# Content dropdown = only content actually present in the LIVE serving data
# (concurrency_now = cold ∪ hot), resolved to a title via content_dict. Sourcing
# from content_dim instead would surface seeded/demo rows that never streamed;
# this keeps the filter in sync with what's really being ingested.
Q_CONTENTS = f"""
SELECT toString(content_id) AS content_id,
       if(empty(dictGetOrDefault('{DB}.content_dict', 'title', content_id, '')),
          concat('Content ', toString(content_id)),
          dictGetOrDefault('{DB}.content_dict', 'title', content_id, '')) AS title
FROM (SELECT DISTINCT content_id FROM {DB}.concurrency_now)
ORDER BY title
LIMIT 1000"""
Q_BOUNDS = (
    f"SELECT toString(min(minute)) AS min_ts, toString(max(minute)) AS max_ts "
    f"FROM {DB}.concurrency_now"
)

# Shared time-window CTE + lenient dimension WHERE fragment.
_RANGE_CTE = f"""
  coalesce(parseDateTimeBestEffortOrNull({{from:String}}, 'UTC'),
           (SELECT min(minute) FROM {DB}.concurrency_now)) AS from_ts,
  coalesce(parseDateTimeBestEffortOrNull({{to:String}}, 'UTC'),
           (SELECT max(minute) FROM {DB}.concurrency_now)) AS to_ts"""

# Half-open [from_ts, to_ts) so a minute bucket is never double-counted across
# adjacent ranges. Dimension filters are Array(String): empty array = all.
_WHERE = """
  minute >= from_ts AND minute < to_ts
  AND (empty({platforms:Array(String)})   OR platform   IN {platforms:Array(String)})
  AND (empty({countries:Array(String)})   OR country    IN {countries:Array(String)})
  AND (empty({video_types:Array(String)}) OR video_type IN {video_types:Array(String)})
  AND (empty({categories:Array(String)})  OR category   IN {categories:Array(String)})
  AND (content_id = toUInt64OrZero({content_id:String})
       OR toUInt64OrZero({content_id:String}) = 0)"""

Q_CURVE = f"""
WITH {_RANGE_CTE}
SELECT toString(minute) AS ts, toUInt32(sum(concurrent)) AS concurrency
FROM {DB}.concurrency_now
WHERE {_WHERE}
GROUP BY minute
ORDER BY minute"""

# Full stat set for the selected filters — the benchmark's "peak / average
# concurrency at a grain" query (schema/ui_queries.sql §2). The per-minute
# `curve` CTE sums concurrency across all dims first, then aggregates over
# minutes. `current` = concurrency at the latest minute in range (argMax is
# deterministic).
#
# avg_concurrency uses the CANONICAL benchmark formula: sum(c) divided by the
# number of buckets spanning the SELECTED range (from_ts to to_ts), with a
# bucket that has no row at all (zero concurrency) counted as 0 — NOT
# `avg(c)`, which silently skips those buckets and only averages over minutes
# that had ≥1 concurrent session. The two differ whenever the range has any
# gap, and only the bucket-count formula matches the ground-truth semantics
# the benchmark is graded against.
Q_STATS = f"""
WITH {_RANGE_CTE},
curve AS (
  SELECT minute, sum(concurrent) AS c
  FROM {DB}.concurrency_now
  WHERE {_WHERE}
  GROUP BY minute
)
SELECT
  toUInt32(ifNull(max(c), 0))                                  AS peak_concurrency,
  toString(argMax(minute, c))                                 AS peak_minute,
  toUInt32(ifNull(argMax(c, minute), 0))                      AS last_minute_concurrency,
  round(sum(c) / (dateDiff('second', from_ts, to_ts) / cfg_bucket_seconds() + 1), 1)
                                                               AS avg_concurrency,
  toUInt32(ifNull(min(c), 0))                                 AS min_concurrency,
  toUInt32(if(isNaN(quantile(0.95)(c)), 0, quantile(0.95)(c))) AS p95_concurrency,
  toUInt32(count())                                           AS active_minutes,
  toUInt32(ifNull(sum(c), 0))                                 AS total_session_minutes
FROM curve"""

# Dimensions the UI breaks peak/avg concurrency down by.
BREAKDOWN_DIMS = ["platform", "video_type", "category"]

# Single query for all three breakdowns (platform, video_type, category) — one
# round trip instead of three. Missing/empty dimension values are surfaced as
# 'Unknown' (a data-quality signal — see get_data_quality_report) rather than
# silently dropped, so a content_dim gap shows up as a visible bucket, not a
# missing bar. `peak_time` is the minute the group's peak occurred.
_Q_BREAKDOWNS = f"""
WITH {_RANGE_CTE},
filtered AS (
  SELECT minute, platform, video_type, category, concurrent
  FROM {DB}.concurrency_now
  WHERE {_WHERE}
),
per_dim_minute AS (
  SELECT 'platform' AS dimension, if(empty(platform), 'Unknown', platform) AS name,
         minute, sum(concurrent) AS c
  FROM filtered
  GROUP BY name, minute

  UNION ALL

  SELECT 'video_type' AS dimension, if(empty(video_type), 'Unknown', video_type) AS name,
         minute, sum(concurrent) AS c
  FROM filtered
  GROUP BY name, minute

  UNION ALL

  SELECT 'category' AS dimension, if(empty(category), 'Unknown', category) AS name,
         minute, sum(concurrent) AS c
  FROM filtered
  GROUP BY name, minute
)
SELECT dimension, name,
       toUInt32(max(c))     AS peak,
       round(avg(c), 1)     AS avg,
       toString(argMax(minute, c)) AS peak_time
FROM per_dim_minute
GROUP BY dimension, name
ORDER BY dimension, peak DESC
LIMIT 50 BY dimension"""

# Audience by hour of DAY (0–23), aggregated across every day in the range —
# answers the business question "when is prime time?". Per the correctness rule,
# sum concurrency per minute FIRST, then avg/max those per-minute totals within
# each hour-of-day bucket.
_Q_AUDIENCE_BY_HOUR = f"""
WITH {_RANGE_CTE},
per_min AS (
  SELECT minute, sum(concurrent) AS c
  FROM {DB}.concurrency_now
  WHERE {_WHERE}
  GROUP BY minute
)
SELECT toUInt8(toHour(minute)) AS hour,
       toUInt32(round(avg(c))) AS avg_viewers,
       toUInt32(max(c))        AS peak_viewers
FROM per_min
GROUP BY hour
ORDER BY hour"""

# Top content by peak concurrency. `content_dict` (a dictionary over
# `content_dim`, see schema.sql) avoids a JOIN on this hot path.
_Q_TOP_CONTENT = f"""
WITH {_RANGE_CTE},
per AS (
  SELECT content_id, minute, sum(concurrent) AS c
  FROM {DB}.concurrency_now
  WHERE {_WHERE}
  GROUP BY content_id, minute
)
SELECT toString(content_id) AS content_id,
       if(empty(dictGetOrDefault('{DB}.content_dict', 'title', content_id, '')),
          concat('Content ', toString(content_id)),
          dictGetOrDefault('{DB}.content_dict', 'title', content_id, '')) AS title,
       toUInt32(max(c))            AS peak,
       round(avg(c), 1)            AS avg,
       toString(argMax(minute, c)) AS peak_time
FROM per
GROUP BY content_id
ORDER BY peak DESC, content_id
LIMIT {{limit:UInt32}}"""

# --- Data-quality diagnostics -------------------------------------------------
# "Why do video_type / category show Unknown / No data?" video_type and category
# are populated from content_dim via content_dict; a content_id absent from
# content_dim yields empty values that propagate all the way to concurrency_now.
# These query BASE tables only (concurrency_now, content_dim, events_raw) —
# never the materialized views that feed them, which hold no data of their own.

# 1) How much of the selected range is affected.
_Q_DQ_MISSING_PCT = f"""
WITH {_RANGE_CTE}
SELECT
  count()                                                            AS total_rows,
  countIf(empty(video_type))                                        AS missing_video_type_rows,
  countIf(empty(category))                                          AS missing_category_rows,
  round(100.0 * countIf(empty(video_type)) / nullIf(count(), 0), 2) AS missing_video_type_pct,
  round(100.0 * countIf(empty(category))   / nullIf(count(), 0), 2) AS missing_category_pct
FROM {DB}.concurrency_now
WHERE minute >= from_ts AND minute < to_ts"""

# 2) Is content_dim itself incomplete (FINAL: ReplacingMergeTree — dedup before counting).
_Q_DQ_CONTENT_DIM_HEALTH = f"""
SELECT
  count()                     AS content_rows,
  uniqExact(content_id)       AS unique_content_ids,
  countIf(empty(video_type))  AS missing_video_type,
  countIf(empty(category))    AS missing_category,
  countIf(empty(title))       AS missing_title
FROM {DB}.content_dim FINAL"""

# 3) Which content_ids appear in events but have no content_dim row at all.
# LEFT ANTI JOIN (not LEFT JOIN ... IS NULL): ClickHouse fills unmatched LEFT
# JOIN columns with type defaults (0 for UInt64), not NULL, so an IS NULL check
# would silently match nothing — ANTI JOIN is the correct "rows with no match".
_Q_DQ_MISSING_CONTENT_IDS = f"""
SELECT
  e.content_id           AS content_id,
  count()                AS event_count,
  max(e.event_timestamp) AS latest_event
FROM {DB}.events_raw AS e
LEFT ANTI JOIN {DB}.content_dim AS c FINAL ON e.content_id = c.content_id
GROUP BY e.content_id
ORDER BY event_count DESC
LIMIT 100"""

# --- Extended drill-down (concurrency_ext_abs) -------------------------------
# The benchmark's drill-down query: reads the EXTENDED serving table (NOT the
# lean core tiers), which adds 4 high-cardinality dims — app_version,
# audio_language, subtitle_language, player_version — on top of the core 4.
# Matches schema/ui_queries.sql §7. Language values are normalized at ingest
# (config.sql norm_lang/norm_dim), so pass e.g. 'hin', not 'HIN'/'hin-hindi'.
_RANGE_CTE_EXT = f"""
  coalesce(parseDateTimeBestEffortOrNull({{from:String}}, 'UTC'),
           (SELECT min(minute) FROM {DB}.concurrency_ext_abs)) AS from_ts,
  coalesce(parseDateTimeBestEffortOrNull({{to:String}}, 'UTC'),
           (SELECT max(minute) FROM {DB}.concurrency_ext_abs)) AS to_ts"""

_EXT_WHERE = """
  minute >= from_ts AND minute < to_ts
  AND (empty({platforms:Array(String)})          OR platform          IN {platforms:Array(String)})
  AND (empty({countries:Array(String)})          OR country           IN {countries:Array(String)})
  AND (empty({video_types:Array(String)})        OR video_type        IN {video_types:Array(String)})
  AND (empty({categories:Array(String)})         OR category          IN {categories:Array(String)})
  AND (empty({app_versions:Array(String)})       OR app_version       IN {app_versions:Array(String)})
  AND (empty({audio_languages:Array(String)})    OR audio_language    IN {audio_languages:Array(String)})
  AND (empty({subtitle_languages:Array(String)}) OR subtitle_language IN {subtitle_languages:Array(String)})
  AND (empty({player_versions:Array(String)})    OR player_version    IN {player_versions:Array(String)})
  AND (content_id = toUInt64OrZero({content_id:String})
       OR toUInt64OrZero({content_id:String}) = 0)"""

Q_EXT_CURVE = f"""
WITH {_RANGE_CTE_EXT}
SELECT toString(minute) AS ts, toUInt32(sum(concurrent)) AS concurrency
FROM {DB}.concurrency_ext_abs
WHERE {_EXT_WHERE}
GROUP BY minute
ORDER BY minute"""

# Same canonical peak/avg formula as Q_STATS (see its comment) — sum divided
# by the number of buckets in the selected range, gaps counted as 0.
Q_EXT_STATS = f"""
WITH {_RANGE_CTE_EXT},
curve AS (
  SELECT minute, sum(concurrent) AS c
  FROM {DB}.concurrency_ext_abs
  WHERE {_EXT_WHERE}
  GROUP BY minute
)
SELECT
  toUInt32(ifNull(max(c), 0))                                  AS peak_concurrency,
  toString(argMax(minute, c))                                 AS peak_minute,
  round(sum(c) / (dateDiff('second', from_ts, to_ts) / cfg_bucket_seconds() + 1), 1)
                                                               AS avg_concurrency,
  toUInt32(count())                                           AS active_minutes
FROM curve"""

_Q_EXT_DISTINCT = (
    "SELECT DISTINCT {col} AS v FROM " + DB + ".concurrency_ext_abs "
    "WHERE {col} != '' ORDER BY v"
)

EXT_EMPTY_FILTERS: dict[str, object] = {
    "from": "",
    "to": "",
    "platforms": [],
    "countries": [],
    "video_types": [],
    "categories": [],
    "app_versions": [],
    "audio_languages": [],
    "subtitle_languages": [],
    "player_versions": [],
    "content_id": "",
}


def get_ext_filter_options() -> dict:
    """Dropdown values for the 4 extended drill-down dims (concurrency_ext_abs)."""

    def col(name: str) -> list[str]:
        df = query_df(_Q_EXT_DISTINCT.format(col=name))
        return df["v"].tolist() if not df.empty else []

    return {
        "app_versions": col("app_version"),
        "audio_languages": col("audio_language"),
        "subtitle_languages": col("subtitle_language"),
        "player_versions": col("player_version"),
    }


def get_ext_curve(filters: dict) -> pd.DataFrame:
    """Per-minute concurrency curve for the drill-down slice."""
    return query_df(Q_EXT_CURVE, filters)


def get_ext_stats(filters: dict) -> dict:
    """Peak/peak-minute/avg for a drill-down slice on concurrency_ext_abs."""
    df = query_df(Q_EXT_STATS, filters)
    if df.empty:
        return {
            "peak_concurrency": 0,
            "peak_minute": None,
            "avg_concurrency": 0,
            "active_minutes": 0,
        }
    return df.iloc[0].to_dict()


# Empty filter set: everything, full range.
EMPTY_FILTERS: dict[str, object] = {
    "from": "",
    "to": "",
    "platforms": [],
    "countries": [],
    "video_types": [],
    "categories": [],
    "content_id": "",
}


def get_filter_options() -> dict:
    """Dropdown values + time bounds for the filter bar."""

    def col(name: str) -> list[str]:
        df = query_df(_Q_DISTINCT.format(col=name))
        return df["v"].tolist() if not df.empty else []

    bounds_df = query_df(Q_BOUNDS)
    bounds = (
        bounds_df.iloc[0].to_dict()
        if not bounds_df.empty
        else {"min_ts": None, "max_ts": None}
    )
    contents_df = query_df(Q_CONTENTS)
    return {
        "platforms": col("platform"),
        "countries": col("country"),
        "video_types": col("video_type"),
        "categories": col("category"),
        "contents": contents_df,  # DataFrame[content_id, title]
        "bounds": bounds,
    }


def get_curve(filters: dict) -> pd.DataFrame:
    """Per-minute concurrency curve. Columns: ts (str), concurrency (int)."""
    return query_df(Q_CURVE, filters)


def get_stats(filters: dict) -> dict:
    """Full stat set (peak/current/avg/min/p95/active-minutes/total) for the
    current filter set."""
    df = query_df(Q_STATS, filters)
    if df.empty:
        return {
            "peak_concurrency": 0,
            "peak_minute": None,
            "last_minute_concurrency": 0,
            "avg_concurrency": 0,
            "min_concurrency": 0,
            "p95_concurrency": 0,
            "active_minutes": 0,
            "total_session_minutes": 0,
        }
    return df.iloc[0].to_dict()


def get_breakdowns(filters: dict) -> pd.DataFrame:
    """Peak/avg/peak_time for platform + video_type + category in one query.

    Columns: dimension, name, peak, avg, peak_time. Slice by
    `df[df["dimension"] == dim]` per BREAKDOWN_DIMS entry.
    """
    return query_df(_Q_BREAKDOWNS, filters)


def get_top_content(filters: dict, limit: int = 15) -> pd.DataFrame:
    """Top content by peak concurrency (with titles), honoring filters."""
    return query_df(_Q_TOP_CONTENT, {**filters, "limit": limit})


def get_audience_by_hour(filters: dict) -> pd.DataFrame:
    """Average & peak viewers per hour of day (0–23) — the 'prime time' view.

    Columns: hour (int 0–23), avg_viewers, peak_viewers.
    """
    return query_df(_Q_AUDIENCE_BY_HOUR, filters)


def get_data_quality_report(filters: dict) -> dict:
    """Diagnose 'video_type/category show Unknown' — see module docstring.

    Returns {"missing_pct": dict, "content_dim_health": dict,
    "missing_content_ids": DataFrame}.
    """
    missing_pct_df = query_df(_Q_DQ_MISSING_PCT, filters)
    health_df = query_df(_Q_DQ_CONTENT_DIM_HEALTH)
    missing_ids_df = query_df(_Q_DQ_MISSING_CONTENT_IDS)
    return {
        "missing_pct": missing_pct_df.iloc[0].to_dict() if not missing_pct_df.empty else {},
        "content_dim_health": health_df.iloc[0].to_dict() if not health_df.empty else {},
        "missing_content_ids": missing_ids_df,
    }


# --- Realtime ticker (global / unfiltered "live now") -------------------------
# Powers the stock-style strip at the top of the dashboard: the current value of
# three live metrics, plus a short recent series for the sparklines. Deliberately
# UNFILTERED and anchored to now() — it answers "what's happening on SonyLIV
# right now", independent of the tab filters and the selected time window.
#
#   * concurrency — latest-minute foreground concurrency from concurrency_now
#     (the canonical number, so it agrees with the Concurrency tab's "current").
#   * users       — distinct users with any activity in the last minute.
#   * sessions    — VideoSessionStart events in the last minute (new-session rate).
#
# users/sessions read events_raw (has user_id, video_session_id, the event_type
# Enum incl. 'VideoSessionStart', and DateTime64 event_timestamp). now() and the
# UTC event_timestamp compare on the same absolute instant, so no TZ juggling.

# Latest complete minute's total concurrency across all dims.
_Q_LIVE_CONCURRENCY = f"""
SELECT toUInt32(ifNull(sum(concurrent), 0)) AS concurrency
FROM {DB}.concurrency_now
WHERE minute = (SELECT max(minute) FROM {DB}.concurrency_now)"""

# Active users + new-session rate over the last 60 seconds.
_Q_LIVE_EVENTS = f"""
SELECT
  toUInt32(uniqExact(user_id))                          AS users,
  toUInt32(countIf(event_type = 'VideoSessionStart'))  AS sessions
FROM {DB}.events_raw
WHERE event_timestamp >= now() - INTERVAL 1 MINUTE"""

# Last 15 minutes, per minute — one point per series for the sparklines.
_Q_SPARK_CONCURRENCY = f"""
SELECT toString(minute) AS ts, toUInt32(sum(concurrent)) AS concurrency
FROM {DB}.concurrency_now
WHERE minute >= toStartOfMinute(now()) - INTERVAL 15 MINUTE
GROUP BY minute
ORDER BY minute"""

_Q_SPARK_EVENTS = f"""
SELECT toString(toStartOfMinute(event_timestamp))        AS ts,
       toUInt32(uniqExact(user_id))                       AS users,
       toUInt32(countIf(event_type = 'VideoSessionStart')) AS sessions
FROM {DB}.events_raw
WHERE event_timestamp >= toStartOfMinute(now()) - INTERVAL 15 MINUTE
GROUP BY toStartOfMinute(event_timestamp)
ORDER BY ts"""


def get_live_metrics() -> dict:
    """Current value of the three live ticker metrics (global, last minute).

    Returns {"concurrency": int, "users": int, "sessions": int}.
    """
    conc_df = query_df(_Q_LIVE_CONCURRENCY)
    ev_df = query_df(_Q_LIVE_EVENTS)
    concurrency = int(conc_df.iloc[0]["concurrency"]) if not conc_df.empty else 0
    users = int(ev_df.iloc[0]["users"]) if not ev_df.empty else 0
    sessions = int(ev_df.iloc[0]["sessions"]) if not ev_df.empty else 0
    return {"concurrency": concurrency, "users": users, "sessions": sessions}


def get_live_sparklines() -> pd.DataFrame:
    """Last ~15 minutes of the three live metrics for the ticker sparklines.

    Columns: ts, concurrency, users, sessions (outer-merged on the minute so a
    minute present in only one source still yields a row; gaps filled with 0).
    """
    conc = query_df(_Q_SPARK_CONCURRENCY)
    ev = query_df(_Q_SPARK_EVENTS)
    if conc.empty and ev.empty:
        return pd.DataFrame(columns=["ts", "concurrency", "users", "sessions"])
    merged = pd.merge(conc, ev, on="ts", how="outer").sort_values("ts")
    for col in ("concurrency", "users", "sessions"):
        if col not in merged:
            merged[col] = 0
    merged[["concurrency", "users", "sessions"]] = (
        merged[["concurrency", "users", "sessions"]].fillna(0).astype(int)
    )
    return merged.reset_index(drop=True)


# --- Real-time viewership overview (time-ranged, concurrency_now) ------------
# Section 1 of benchmark_queries.sql — a business-facing overview over
# concurrency_now (cold ∪ hot), windowed by the selected time range (from/to via
# _RANGE_CTE; empty from/to = full range). These honor the toolbar time picker
# but not the dimension filters (the Overview tab exposes no dimension selects).
#   * `streams` = sum(concurrent) — concurrent sessions, the TRUE concurrency
#     metric (one session has exactly one of each dim, so the sum is exact).
#   * `unique_viewers` = sum(concurrent_users) — an APPROXIMATION: a user can hold
#     sessions on two titles at once, so this over-counts (per the SUM()
#     correctness note in benchmark_queries.sql). Labelled "approx" in the UI.
_RT_TIME_WHERE = "minute >= from_ts AND minute < to_ts"

_Q_RT_HERO = f"""
WITH {_RANGE_CTE}
SELECT
  toUInt64(sum(concurrent))       AS total_streams,
  toUInt64(sum(concurrent_users)) AS total_unique_viewers,
  toUInt64(uniqExact(content_id)) AS active_content_count
FROM {DB}.concurrency_now
WHERE {_RT_TIME_WHERE}"""

_Q_RT_LIVE_VS_VOD = f"""
WITH {_RANGE_CTE}
SELECT video_type,
       toUInt64(sum(concurrent))       AS streams,
       toUInt64(sum(concurrent_users)) AS unique_viewers
FROM {DB}.concurrency_now
WHERE {_RT_TIME_WHERE} AND video_type != ''
GROUP BY video_type
ORDER BY streams DESC"""

# video_type / category come straight off the served rows (concurrency_now has
# them); title is resolved via content_dict (no JOIN on this path).
_Q_RT_TOP_CONTENT = f"""
WITH {_RANGE_CTE}
SELECT toString(content_id) AS content_id,
       if(empty(dictGetOrDefault('{DB}.content_dict', 'title', content_id, '')),
          concat('Content ', toString(content_id)),
          dictGetOrDefault('{DB}.content_dict', 'title', content_id, '')) AS title,
       any(video_type)                 AS video_type,
       any(category)                   AS category,
       toUInt64(sum(concurrent))       AS streams,
       toUInt64(sum(concurrent_users)) AS unique_viewers
FROM {DB}.concurrency_now
WHERE {_RT_TIME_WHERE}
GROUP BY content_id
ORDER BY streams DESC, content_id
LIMIT {{limit:UInt32}}"""

_Q_RT_GEO = f"""
WITH {_RANGE_CTE}
SELECT country,
       toUInt64(sum(concurrent))       AS streams,
       toUInt64(sum(concurrent_users)) AS unique_viewers
FROM {DB}.concurrency_now
WHERE {_RT_TIME_WHERE} AND country != ''
GROUP BY country
ORDER BY streams DESC"""


def get_realtime_hero(filters: dict) -> dict:
    """Hero KPIs over the selected range: total streams, ~unique viewers, active titles."""
    df = query_df(_Q_RT_HERO, filters)
    if df.empty:
        return {
            "total_streams": 0,
            "total_unique_viewers": 0,
            "active_content_count": 0,
        }
    return df.iloc[0].to_dict()


def get_live_vs_vod(filters: dict) -> pd.DataFrame:
    """Streams + ~unique viewers split by video_type (Live vs VOD), over the range."""
    return query_df(_Q_RT_LIVE_VS_VOD, filters)


def get_top_content_leaderboard(filters: dict, limit: int = 10) -> pd.DataFrame:
    """Top content by total concurrent streams (with title/type/category), over the range."""
    return query_df(_Q_RT_TOP_CONTENT, {**filters, "limit": limit})


def get_geo_distribution(filters: dict) -> pd.DataFrame:
    """Streams + ~unique viewers by country, over the range."""
    return query_df(_Q_RT_GEO, filters)
