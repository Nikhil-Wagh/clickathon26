"""Business-insights pane — data layer (time-series overlays + QoE/engagement KPIs).

Powers the "💼 Business insights" tab. Time-series overlays (TS-1..TS-5) bucket to
5-minute grain and honor the global time range.

SOURCES
  * concurrency_now (cold ∪ hot view) → concurrency-level series (attention,
    ramp, retention, viewer-hours). Same CORRECTNESS RULE as queries.py: sum
    `concurrent` per minute FIRST, then aggregate.
  * events_raw → session lifecycle + QoE (arrivals/departures, errors, ad-break,
    buffering, first-frame). events_raw has platform/country/content_id but NOT
    video_type/category (those are content attributes on content_dim), so the
    event-based charts filter by platform/country/content only.

QoE EVENT NAMES are centralized below. The playback-quality signals (buffering,
dropped frames, first-frame) live in the `event` LowCardinality(String) column —
NEVER compared against the event_type Enum (that would raise "Unknown element").
If your ingest uses different spellings, change these four constants only.
"""

from __future__ import annotations

import time

import pandas as pd

from clickhouse_client import query_df
from config import DB

# --- QoE event vocabulary (in the `event` String column) ---------------------
EV_BUFFER_START = "BufferStart"
EV_BUFFER_END = "BufferEnd"
EV_DROPPED = "dropped-frames"
EV_FIRST_FRAME = "first-frame"
EV_AD_RESUME = "AdResume"  # emitted when a session resumes after an ad break

# --- Shared range + lenient dimension filters --------------------------------
_RANGE_CTE = f"""
  coalesce(parseDateTimeBestEffortOrNull({{from:String}}, 'UTC'),
           (SELECT min(minute) FROM {DB}.concurrency_now)) AS from_ts,
  coalesce(parseDateTimeBestEffortOrNull({{to:String}}, 'UTC'),
           (SELECT max(minute) FROM {DB}.concurrency_now)) AS to_ts"""

# concurrency_now carries all core dims + content_id.
_WHERE_CONC = """
  minute >= from_ts AND minute < to_ts
  AND (empty({platforms:Array(String)})   OR platform   IN {platforms:Array(String)})
  AND (empty({countries:Array(String)})   OR country    IN {countries:Array(String)})
  AND (empty({video_types:Array(String)}) OR video_type IN {video_types:Array(String)})
  AND (empty({categories:Array(String)})  OR category   IN {categories:Array(String)})
  AND (content_id = toUInt64OrZero({content_id:String})
       OR toUInt64OrZero({content_id:String}) = 0)"""

# events_raw has platform/country/content_id only (no video_type/category).
_WHERE_EV = """
  event_timestamp >= from_ts AND event_timestamp < to_ts
  AND (empty({platforms:Array(String)}) OR platform IN {platforms:Array(String)})
  AND (empty({countries:Array(String)}) OR country  IN {countries:Array(String)})
  AND (content_id = toUInt64OrZero({content_id:String})
       OR toUInt64OrZero({content_id:String}) = 0)"""


def timed_query(sql: str, params: dict | None = None) -> tuple[pd.DataFrame, float]:
    """Run a query and return (DataFrame, elapsed_ms) — for the per-chart latency badge."""
    t0 = time.perf_counter()
    df = query_df(sql, params or {})
    return df, (time.perf_counter() - t0) * 1000.0


# ===========================================================================
# TS-1 — Attention ratio (active vs total-open), 5-min buckets
# ===========================================================================
_Q_TS1 = f"""
WITH {_RANGE_CTE},
conc AS (
  SELECT toStartOfFiveMinutes(minute) AS ts, sum(concurrent) AS c
  FROM {DB}.concurrency_now
  WHERE {_WHERE_CONC}
  GROUP BY minute
),
active AS (SELECT ts, avg(c) AS active_streams FROM conc GROUP BY ts),
opened AS (
  SELECT toStartOfFiveMinutes(event_timestamp) AS ts,
         uniqExact(video_session_id) AS total_open
  FROM {DB}.events_raw
  WHERE {_WHERE_EV}
  GROUP BY ts
)
SELECT toString(a.ts)                                   AS ts,
       toUInt32(round(a.active_streams))                AS total_streams,
       round(least(1.0, a.active_streams / nullIf(o.total_open, 0)), 3) AS attention_ratio
FROM active AS a
LEFT JOIN opened AS o ON a.ts = o.ts
ORDER BY ts"""


# ===========================================================================
# TS-2 — Ramp velocity (Δ concurrent per bucket)
# ===========================================================================
_Q_TS2 = f"""
WITH {_RANGE_CTE},
curve AS (
  SELECT toStartOfFiveMinutes(minute) AS ts, sum(concurrent) AS streams
  FROM {DB}.concurrency_now
  WHERE {_WHERE_CONC}
  GROUP BY ts
)
SELECT toString(ts) AS ts,
       toUInt32(streams) AS streams,
       toInt64(streams) - toInt64(lagInFrame(streams) OVER (ORDER BY ts)) AS delta_streams
FROM curve
ORDER BY ts"""


# ===========================================================================
# TS-3 — Net flow (arrivals vs departures + running open sessions)
# ===========================================================================
_Q_TS3 = f"""
WITH {_RANGE_CTE},
flow AS (
  SELECT toStartOfFiveMinutes(event_timestamp) AS ts,
         uniqExactIf(video_session_id, event_type = 'VideoSessionStart') AS arrivals,
         uniqExactIf(video_session_id, event_type = 'VideoSessionEnd')   AS departures
  FROM {DB}.events_raw
  WHERE {_WHERE_EV}
  GROUP BY ts
)
SELECT toString(ts) AS ts,
       toUInt32(arrivals)                                    AS arrivals,
       toInt64(-departures)                                  AS departures,
       toInt64(arrivals) - toInt64(departures)              AS net_flow,
       sum(toInt64(arrivals) - toInt64(departures)) OVER (ORDER BY ts) AS open_sessions
FROM flow
ORDER BY ts"""


# ===========================================================================
# TS-4 — Retention % of window peak
# ===========================================================================
_Q_TS4 = f"""
WITH {_RANGE_CTE},
curve AS (
  SELECT toStartOfFiveMinutes(minute) AS ts, sum(concurrent) AS streams
  FROM {DB}.concurrency_now
  WHERE {_WHERE_CONC}
  GROUP BY ts
)
SELECT toString(ts) AS ts,
       toUInt32(streams) AS streams,
       round(100.0 * streams / nullIf(max(streams) OVER (), 0), 1) AS pct_of_peak
FROM curve
ORDER BY ts"""


# ===========================================================================
# TS-5 — QoE overlay (errors + rebuffers over the concurrency curve)
# ===========================================================================
_Q_TS5 = f"""
WITH {_RANGE_CTE},
conc AS (
  SELECT toStartOfFiveMinutes(minute) AS ts, sum(concurrent) AS streams
  FROM {DB}.concurrency_now
  WHERE {_WHERE_CONC}
  GROUP BY ts
),
ev AS (
  SELECT toStartOfFiveMinutes(event_timestamp) AS ts,
         uniqExactIf(video_session_id, event_type = 'VideoError') AS error_sessions,
         countIf(event_type = 'VideoError')                       AS error_events,
         countIf(event = '{EV_BUFFER_START}')                     AS buffer_start_events,
         countIf(event = '{EV_BUFFER_END}')                       AS buffer_end_events,
         countIf(event = '{EV_DROPPED}')                          AS dropped_frame_events
  FROM {DB}.events_raw
  WHERE {_WHERE_EV}
  GROUP BY ts
)
SELECT toString(c.ts) AS ts,
       toUInt32(c.streams)               AS streams,
       toUInt32(ifNull(ev.error_sessions, 0))      AS error_sessions,
       toUInt32(ifNull(ev.error_events, 0))        AS error_events,
       toUInt32(ifNull(ev.buffer_start_events, 0)) AS buffer_start_events,
       toUInt32(ifNull(ev.buffer_end_events, 0))   AS buffer_end_events,
       toUInt32(ifNull(ev.dropped_frame_events, 0)) AS dropped_frame_events
FROM conc AS c
LEFT JOIN ev ON c.ts = ev.ts
ORDER BY ts"""


# ===========================================================================
# KPI-4 — Ad-break resume & drop-off
# ===========================================================================
_Q_KPI4 = f"""
WITH {_RANGE_CTE},
per_session AS (
  SELECT video_session_id,
         max(event_type = 'AdBreakStart')      AS had_ad,
         max(event = '{EV_AD_RESUME}')         AS resumed,
         max(event_type = 'VideoSessionEnd')   AS ended
  FROM {DB}.events_raw
  WHERE {_WHERE_EV}
  GROUP BY video_session_id
  HAVING had_ad = 1
)
SELECT
  toUInt64(count())                                              AS ad_sessions,
  toUInt64(countIf(resumed = 1))                                AS resumed_after_ad,
  toUInt64(countIf(ended = 1 AND resumed = 0))                  AS ended_during_ad,
  round(100.0 * countIf(resumed = 1) / nullIf(count(), 0), 1)   AS resume_rate,
  round(100.0 * countIf(ended = 1 AND resumed = 0) / nullIf(count(), 0), 1) AS dropoff_rate
FROM per_session"""


# ===========================================================================
# KPI-6 — Playback success / VSF / EBVS / VPF (session-level)
# ===========================================================================
_Q_KPI6 = f"""
WITH {_RANGE_CTE},
per_session AS (
  SELECT video_session_id,
         max(event = '{EV_FIRST_FRAME}')                      AS has_frame,
         max(event_type = 'VideoError')                       AS has_error,
         minIf(event_timestamp, event = '{EV_FIRST_FRAME}')   AS frame_ts,
         minIf(event_timestamp, event_type = 'VideoError')    AS error_ts
  FROM {DB}.events_raw
  WHERE {_WHERE_EV}
  GROUP BY video_session_id
)
SELECT
  toUInt64(count())                                                       AS attempts,
  round(100.0 * countIf(has_frame = 1) / nullIf(count(), 0), 2)           AS play_success_rate,
  round(100.0 * countIf(has_error = 1 AND (has_frame = 0 OR error_ts < frame_ts)) / nullIf(count(), 0), 2) AS vsf_pct,
  round(100.0 * countIf(has_frame = 0 AND has_error = 0) / nullIf(count(), 0), 2) AS ebvs_pct,
  round(100.0 * countIf(has_frame = 1 AND has_error = 1 AND error_ts >= frame_ts) / nullIf(count(), 0), 2) AS vpf_pct
FROM per_session"""


# ===========================================================================
# KPI-7 — Video Start Time (first-frame − session start), percentiles
# ===========================================================================
_Q_KPI7 = f"""
WITH {_RANGE_CTE},
per_session AS (
  SELECT video_session_id,
         any(session_start_epoch)                            AS sse,
         minIf(event_timestamp, event = '{EV_FIRST_FRAME}')  AS frame_ts
  FROM {DB}.events_raw
  WHERE {_WHERE_EV}
  GROUP BY video_session_id
  HAVING max(event = '{EV_FIRST_FRAME}') = 1
),
vst AS (
  SELECT dateDiff('millisecond', sse, frame_ts) / 1000.0 AS secs
  FROM per_session
  WHERE frame_ts > sse AND dateDiff('second', sse, frame_ts) < 600
)
SELECT
  round(quantile(0.50)(secs), 1) AS p50,
  round(quantile(0.75)(secs), 1) AS p75,
  round(quantile(0.90)(secs), 1) AS p90,
  round(quantile(0.95)(secs), 1) AS p95,
  round(quantile(0.99)(secs), 1) AS p99,
  round(avg(secs), 1)            AS avg
FROM vst"""


# ===========================================================================
# KPI-8 — Rebuffering ratio (BufferStart→BufferEnd durations)
# ===========================================================================
_Q_KPI8 = f"""
WITH {_RANGE_CTE},
seq AS (
  SELECT video_session_id, event, event_timestamp,
         lagInFrame(event_timestamp) OVER w AS prev_ts,
         lagInFrame(event)           OVER w AS prev_event
  FROM {DB}.events_raw
  WHERE {_WHERE_EV} AND event IN ('{EV_BUFFER_START}', '{EV_BUFFER_END}')
  WINDOW w AS (PARTITION BY video_session_id ORDER BY event_timestamp
               ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)
),
durs AS (
  SELECT video_session_id,
         dateDiff('millisecond', prev_ts, event_timestamp) / 1000.0 AS secs
  FROM seq
  WHERE event = '{EV_BUFFER_END}' AND prev_event = '{EV_BUFFER_START}'
),
watch AS (
  SELECT sum(concurrent) * 60.0 AS watch_secs
  FROM {DB}.concurrency_now
  WHERE {_WHERE_CONC}
)
SELECT
  toUInt64(uniqExact(d.video_session_id))                                    AS sessions_with_rebuffer,
  round(sum(d.secs) / nullIf(uniqExact(d.video_session_id), 0), 1)           AS avg_rebuffer_secs,
  round(100.0 * sum(d.secs) / nullIf((SELECT watch_secs FROM watch), 0), 2)  AS rebuffering_ratio
FROM durs AS d"""


# ===========================================================================
# KPI-9 — Viewer-hours, peak-to-avg, sessions/user
# ===========================================================================
_Q_KPI9_CONC = f"""
WITH {_RANGE_CTE},
per_min AS (
  SELECT minute, sum(concurrent) AS c
  FROM {DB}.concurrency_now
  WHERE {_WHERE_CONC}
  GROUP BY minute
)
SELECT
  round(sum(c) / 60.0, 1)                       AS viewer_hours,
  toUInt32(ifNull(max(c), 0))                   AS peak_concurrent,
  round(avg(c), 1)                              AS avg_concurrent,
  round(max(c) / nullIf(avg(c), 0), 1)          AS peak_to_avg
FROM per_min"""

_Q_KPI9_USERS = f"""
WITH {_RANGE_CTE}
SELECT round(uniqExact(video_session_id) / nullIf(uniqExact(user_id), 0), 2) AS sessions_per_user
FROM {DB}.events_raw
WHERE {_WHERE_EV}"""


# --- Timed accessors (each returns (data, elapsed_ms)) -----------------------
def _row(df: pd.DataFrame, defaults: dict) -> dict:
    return df.iloc[0].to_dict() if not df.empty else dict(defaults)


def get_attention_ratio(f: dict) -> tuple[pd.DataFrame, float]:
    return timed_query(_Q_TS1, f)


def get_ramp_velocity(f: dict) -> tuple[pd.DataFrame, float]:
    return timed_query(_Q_TS2, f)


def get_net_flow(f: dict) -> tuple[pd.DataFrame, float]:
    return timed_query(_Q_TS3, f)


def get_retention(f: dict) -> tuple[pd.DataFrame, float]:
    return timed_query(_Q_TS4, f)


def get_qoe_overlay(f: dict) -> tuple[pd.DataFrame, float]:
    return timed_query(_Q_TS5, f)


def get_ad_break(f: dict) -> tuple[dict, float]:
    df, ms = timed_query(_Q_KPI4, f)
    return _row(df, {
        "ad_sessions": 0, "resumed_after_ad": 0, "ended_during_ad": 0,
        "resume_rate": 0.0, "dropoff_rate": 0.0,
    }), ms


def get_playback_health(f: dict) -> tuple[dict, float]:
    df, ms = timed_query(_Q_KPI6, f)
    return _row(df, {
        "attempts": 0, "play_success_rate": 0.0, "vsf_pct": 0.0,
        "ebvs_pct": 0.0, "vpf_pct": 0.0,
    }), ms


def get_vst(f: dict) -> tuple[dict, float]:
    df, ms = timed_query(_Q_KPI7, f)
    return _row(df, {"p50": 0.0, "p75": 0.0, "p90": 0.0, "p95": 0.0, "p99": 0.0, "avg": 0.0}), ms


def get_rebuffering(f: dict) -> tuple[dict, float]:
    df, ms = timed_query(_Q_KPI8, f)
    return _row(df, {
        "sessions_with_rebuffer": 0, "avg_rebuffer_secs": 0.0, "rebuffering_ratio": 0.0,
    }), ms


def get_viewer_stats(f: dict) -> tuple[dict, float]:
    conc, ms1 = timed_query(_Q_KPI9_CONC, f)
    users, ms2 = timed_query(_Q_KPI9_USERS, f)
    out = _row(conc, {
        "viewer_hours": 0.0, "peak_concurrent": 0, "avg_concurrent": 0.0, "peak_to_avg": 0.0,
    })
    out.update(_row(users, {"sessions_per_user": 0.0}))
    return out, ms1 + ms2
