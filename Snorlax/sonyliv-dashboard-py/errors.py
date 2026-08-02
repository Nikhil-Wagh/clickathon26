"""Errors pane — data layer.

STATUS: REAL. All charts here are queried from `events_raw`: total errors,
error rate, peak errors per time bucket, errors over time, errors by platform,
affected sessions.

The schema has no error-code/message taxonomy (every `event_type='VideoError'`
row carries the same generic `event='error'` marker — confirmed in
`producer/produce_events.py`), so there is intentionally NO "errors by type" or
"top error messages" breakdown: a single-bucket placeholder would be dummy data,
not a real breakdown. Wiring one needs a schema change (an error-code/message
column or a dedicated errors table).
"""

from __future__ import annotations

import pandas as pd

from clickhouse_client import query_df
from config import DB

IS_SAMPLE = False  # real ClickHouse queries; error taxonomy is a known schema gap (see module docstring)

_RANGE_CTE = f"""
  coalesce(parseDateTimeBestEffortOrNull({{from:String}}, 'UTC'),
           (SELECT min(event_timestamp) FROM {DB}.events_raw)) AS from_ts,
  coalesce(parseDateTimeBestEffortOrNull({{to:String}}, 'UTC'),
           (SELECT max(event_timestamp) FROM {DB}.events_raw)) AS to_ts"""

_ERROR_WHERE = "event_type = 'VideoError' AND event_timestamp >= from_ts AND event_timestamp < to_ts"
_RANGE_WHERE = "event_timestamp >= from_ts AND event_timestamp < to_ts"

SQL_ERRORS_OVER_TIME = f"""
WITH {_RANGE_CTE}
SELECT toStartOfMinute(event_timestamp) AS ts, count() AS errors
FROM {DB}.events_raw
WHERE {_ERROR_WHERE}
GROUP BY ts ORDER BY ts"""

SQL_ERRORS_BY_PLATFORM = f"""
WITH {_RANGE_CTE}
SELECT if(empty(platform), 'Unknown', platform) AS platform, count() AS errors
FROM {DB}.events_raw
WHERE {_ERROR_WHERE}
GROUP BY platform ORDER BY errors DESC"""

# Scalar totals for the KPI row. Independent subqueries (not a GROUP BY) so a
# zero-row result on one metric doesn't collapse the others via an inner join.
SQL_ERROR_TOTALS = f"""
WITH {_RANGE_CTE}
SELECT
  (SELECT count() FROM {DB}.events_raw WHERE {_ERROR_WHERE})                    AS total_errors,
  (SELECT uniqExact(video_session_id) FROM {DB}.events_raw WHERE {_ERROR_WHERE}) AS affected_sessions,
  (SELECT uniqExact(video_session_id) FROM {DB}.events_raw WHERE {_RANGE_WHERE}) AS total_sessions"""


def get_errors_over_time(f: dict) -> pd.DataFrame:
    return query_df(SQL_ERRORS_OVER_TIME, f)


def get_errors_by_platform(f: dict) -> pd.DataFrame:
    return query_df(SQL_ERRORS_BY_PLATFORM, f)


def get_error_kpis(f: dict) -> dict:
    totals_df = query_df(SQL_ERROR_TOTALS, f)
    totals = (
        totals_df.iloc[0].to_dict()
        if not totals_df.empty
        else {"total_errors": 0, "affected_sessions": 0, "total_sessions": 0}
    )
    over_time = get_errors_over_time(f)
    by_platform = get_errors_by_platform(f)

    total = int(totals["total_errors"])
    total_sessions = int(totals["total_sessions"])
    return {
        "total_errors": total,
        "error_rate_pct": round(100 * total / total_sessions, 2) if total_sessions else 0.0,
        "affected_sessions": int(totals["affected_sessions"]),
        "top_platform": by_platform.iloc[0]["platform"] if not by_platform.empty else "—",
        "peak_errors_min": int(over_time["errors"].max()) if not over_time.empty else 0,
    }
