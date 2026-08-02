"""SonyLIV — Viewing Concurrency & Insights dashboard (Streamlit).

Three panes, all backed by real ClickHouse Cloud queries (no sample data):
  1. Overview          — "real-time viewership" snapshot over concurrency_now
                         (cold ∪ hot), honoring the selected time range: hero
                         KPIs, Live vs VOD, viewers by country, top-content
                         leaderboard.
  2. Concurrency       — time-ranged analytics + graded serving-path proof:
                         peak/avg/current/min/p95 KPIs + decline monitor, the
                         per-minute curve, peak-by-dimension bars, per-hour
                         roll-up, platform×country, decline curve, and a
                         system.query_log latency proof (concurrency_now for the
                         core stats; SINK TABLES via benchmark.py for the graded
                         roll-up — see benchmark_queries.sql).
  3. Business insights — QoE & engagement overlays (TS-1..TS-5) and KPI tiles
                         (ad-break, playback health, VST, rebuffering, viewer
                         stats) over events_raw + concurrency_now
                         (see business.py).

Plus a global realtime ticker strip (active sessions / users / new-sessions)
above the tabs. Every chart shows its own query latency (⏱ N ms).

ClickHouse-inspired theme (config.py / ui.py). Run:
    pip install -r requirements.txt
    streamlit run app.py
"""

from __future__ import annotations

import guardrails  # noqa: F401  (must run first — strips Bloomberg proxy env vars)

from datetime import date, datetime, time, timedelta
from time import perf_counter

import pandas as pd
import streamlit as st

import benchmark as benchmod
import business as bizmod
import otel_setup
import queries
import ui
from config import (
    DB,
    DOWN,
    DOWN_FILL,
    QUICK_RANGES,
    REFRESH_INTERVALS,
    UP,
    UP_FILL,
)

# Build OTel providers once per process (idempotent across Streamlit reruns).
otel_setup.init_otel()
# Count each script execution — Streamlit reruns on every interaction/refresh,
# so this doubles as a page-render / interaction-rate signal.
otel_setup.app_runs().add(1)

st.set_page_config(page_title="SonyLIV — Viewing Concurrency", layout="wide")

try:
    from streamlit_autorefresh import st_autorefresh
except ImportError:  # pragma: no cover
    st_autorefresh = None

DANGER = "#ff6b6b"
DANGER_FILL = "rgba(255,107,107,0.14)"


def _timed(fn, *args, **kwargs):
    """Run a data fetch and return (result, elapsed_ms) for the per-chart ⏱ badge."""
    t0 = perf_counter()
    out = fn(*args, **kwargs)
    return out, (perf_counter() - t0) * 1000.0


# ===========================================================================
# Pane 1 — Concurrency (real data)
# ===========================================================================
def render_concurrency(time_filter: dict) -> None:
    try:
        opts = queries.get_filter_options()
    except Exception as e:  # noqa: BLE001
        st.error(f"⚠ Failed to load filters: {e}")
        return

    f = {**queries.EMPTY_FILTERS, **time_filter}

    # Dimension filters (concurrency-specific columns). Multi-select: no
    # selection = all values for that dimension (matches Array(String) filters
    # in queries.py — empty array means "all").
    r = st.columns([1, 1, 1, 1, 1.6])

    def multisel(container, label: str, values: list[str]) -> list[str]:
        return container.multiselect(label, values, default=[], placeholder="All")

    f["platforms"] = multisel(r[0], "Platform", opts["platforms"])
    f["countries"] = multisel(r[1], "Country", opts["countries"])
    f["video_types"] = multisel(r[2], "Video type", opts["video_types"])
    f["categories"] = multisel(r[3], "Category", opts["categories"])

    contents = opts["contents"]
    labels, ids = ["All content"], [""]
    if not contents.empty:
        for _, row in contents.iterrows():
            labels.append(row["title"] or row["content_id"])
            ids.append(row["content_id"])
    idx = r[4].selectbox("Content", range(len(labels)), format_func=lambda i: labels[i])
    f["content_id"] = ids[idx]

    try:
        stats = queries.get_stats(f)
        curve, curve_ms = _timed(queries.get_curve, f)
        decline = benchmod.get_decline(f)  # hot-tier decline monitor (sink path)
    except Exception as e:  # noqa: BLE001
        st.error(f"⚠ Query failed: {e}")
        return

    avg = stats["avg_concurrency"]
    avg_str = f"{avg:,.1f}" if pd.notna(avg) else "—"
    declining = decline["status"] == "DECLINE"

    ui.tiles_row(
        st.columns(4),
        [
            ui.kpi_tile(
                "Peak concurrency",
                ui.fmt(stats["peak_concurrency"]),
                f"at {ui.pretty_minute(stats['peak_minute'])}",
                "accent",
                peak=True,
            ),
            ui.kpi_tile(
                "Current concurrency",
                ui.fmt(stats["last_minute_concurrency"]),
                "latest minute in range",
                "accent2",
            ),
            ui.kpi_tile(
                "Average concurrency", avg_str, "over the range (gaps count as 0)"
            ),
            ui.kpi_tile(
                "Decline monitor",
                "▼ DECLINE" if declining else "● OK",
                f"{ui.fmt(decline['latest'])} vs {ui.fmt(decline['trailing_peak'])} "
                f"peak (−{decline['pct_below_peak']}%)",
                "danger" if declining else "accent",
            ),
        ],
    )
    st.write("")
    ui.tiles_row(
        st.columns(3),
        [
            ui.kpi_tile(
                "Min concurrency",
                ui.fmt(stats["min_concurrency"]),
                "lowest active minute",
            ),
            ui.kpi_tile(
                "P95 concurrency",
                ui.fmt(stats["p95_concurrency"]),
                "95th-percentile minute",
            ),
            ui.kpi_tile(
                "Active minutes",
                ui.fmt(stats["active_minutes"]),
                f"{ui.fmt(stats['total_session_minutes'])} session-minutes total",
            ),
        ],
    )
    st.write("")

    st.markdown("**Concurrency curve (per minute)**")
    if curve.empty:
        st.info("No data for the selected filters.")
    else:
        st.plotly_chart(
            ui.time_area(curve, "ts", "concurrency", "concurrent", latency_ms=curve_ms),
            width="stretch",
            config={"displayModeBar": False},
        )

    st.write("")
    st.markdown(
        "**Peak concurrency by dimension** — each bar is that value's OWN peak "
        "minute (concurrency isn't additive across dimensions, so platforms / "
        "types / categories peak at different minutes)."
    )
    labels_map = {
        "platform": "Platform",
        "video_type": "Video type",
        "category": "Category",
    }
    try:
        breakdowns, bd_ms = _timed(queries.get_breakdowns, f)  # one query for all 3 dims
    except Exception as e:  # noqa: BLE001
        st.error(f"⚠ Breakdown query failed: {e}")
        breakdowns, bd_ms = (
            pd.DataFrame(columns=["dimension", "name", "peak", "avg", "peak_time"]),
            None,
        )
    for col, dim in zip(
        st.columns(len(queries.BREAKDOWN_DIMS)), queries.BREAKDOWN_DIMS
    ):
        with col:
            st.caption(labels_map[dim])
            bdf = breakdowns.loc[breakdowns["dimension"] == dim, ["name", "peak"]]
            if bdf.empty:
                st.info("No data.")
            else:
                st.plotly_chart(
                    ui.bar(bdf, "name", "peak", "peak", latency_ms=bd_ms),
                    width="stretch",
                    config={"displayModeBar": False},
                )

    st.write("")
    st.markdown(
        "**Peak & average concurrency by hour** — two-level roll-up (sum to the "
        "minute, then max/avg the minute-totals per hour; graded sink path)."
    )
    hourly, hourly_ms = _timed(benchmod.get_peak_avg_by_hour, f)
    if hourly.empty:
        st.info("No data for the selected filters.")
    else:
        st.plotly_chart(
            ui.grouped_bar(
                hourly,
                "hour",
                [("peak", "Peak", ui.ACCENT), ("avg", "Average", ui.ACCENT_2)],
                latency_ms=hourly_ms,
            ),
            width="stretch",
            config={"displayModeBar": False},
        )

    st.write("")
    st.markdown("**Peak by platform × country** (top 15)")
    st.caption("Different dimension combos peak at different minutes.")
    by_combo, combo_ms = _timed(benchmod.get_peak_by_combo, f)
    if by_combo.empty:
        st.info("No data.")
    else:
        st.plotly_chart(
            ui.bar(by_combo, "combo", "peak", "peak", color=ui.ACCENT_2, latency_ms=combo_ms),
            width="stretch",
            config={"displayModeBar": False},
        )

    st.write("")
    st.markdown("**Concurrency decline monitor** — last 30 min (hot tier)")
    st.caption(
        "Alerts when the latest minute drops below 60% of the trailing 15-min "
        "peak — asset ended, a system issue, or content disengagement."
    )
    dcurve, dc_ms = _timed(benchmod.get_decline_curve, f)
    if dcurve.empty:
        st.info("No recent hot-tier data for the selected filters.")
    else:
        color, fill = (DOWN, DOWN_FILL) if declining else (UP, UP_FILL)
        st.plotly_chart(
            ui.time_area(
                dcurve, "ts", "concurrency", "concurrent",
                color=color, fill=fill, latency_ms=dc_ms,
            ),
            width="stretch",
            config={"displayModeBar": False},
        )

    st.write("")
    dq_col, proof_col = st.columns(2)
    with dq_col:
        with st.expander("🔍 Data quality diagnostics", expanded=False):
            st.caption(
                "Diagnose 'Unknown' / missing video type or category — see "
                "queries.get_data_quality_report."
            )
            if st.button("Run diagnostics", key="run_dq"):
                try:
                    dq = queries.get_data_quality_report(f)
                except Exception as e:  # noqa: BLE001
                    st.error(f"⚠ Diagnostics query failed: {e}")
                else:
                    st.markdown("**Missing dims in `concurrency_now` (selected range)**")
                    st.json(dq["missing_pct"])
                    st.markdown("**`content_dim` completeness**")
                    st.json(dq["content_dim_health"])
                    st.markdown("**Content IDs in events but missing from `content_dim`**")
                    if dq["missing_content_ids"].empty:
                        st.info("None — every content_id in events_raw has a content_dim row.")
                    else:
                        st.dataframe(
                            dq["missing_content_ids"], hide_index=True, width="stretch"
                        )
    with proof_col:
        with st.expander("🧾 Serving-layer proof (latency & rows read)", expanded=False):
            st.caption(
                "Evidence the benchmark ran through the pipeline: recent queries "
                "against the sink tables, with latency and rows/bytes scanned "
                "(system.query_log)."
            )
            if st.button("Load query stats", key="load_latency"):
                try:
                    lat = benchmod.get_serving_latency()
                except Exception as e:  # noqa: BLE001
                    st.error(f"⚠ query_log unavailable: {e}")
                else:
                    if lat.empty:
                        st.info("No serving-layer queries logged in the last 30 min.")
                    else:
                        st.dataframe(
                            lat.rename(
                                columns={
                                    "started": "Started",
                                    "ms": "Latency (ms)",
                                    "rows_read": "Rows read",
                                    "bytes_read": "Bytes read",
                                    "query_head": "Query",
                                }
                            ),
                            hide_index=True,
                            width="stretch",
                        )


# ===========================================================================
# Pane — Overview (Real-Time Viewership: global snapshot over concurrency_now)
# ===========================================================================
def render_overview(time_filter: dict) -> None:
    st.caption(
        "Real-time viewership over `concurrency_now` (cold ∪ hot) for the "
        "selected time range. **Streams** = concurrent sessions (the true "
        "concurrency metric); **unique viewers** sums `concurrent_users` and is "
        "an approximation (one user can hold sessions on two titles at once) — "
        "see benchmark_queries.sql."
    )
    try:
        hero = queries.get_realtime_hero(time_filter)
    except Exception as e:  # noqa: BLE001
        st.error(f"⚠ Query failed: {e}")
        return

    ui.tiles_row(
        st.columns(3),
        [
            ui.kpi_tile(
                "Total streams",
                ui.fmt(hero["total_streams"]),
                "concurrent sessions (all served data)",
                "accent",
            ),
            ui.kpi_tile(
                "Unique viewers",
                ui.fmt(hero["total_unique_viewers"]),
                "approx — sums concurrent_users",
                "accent2",
            ),
            ui.kpi_tile(
                "Active titles",
                ui.fmt(hero["active_content_count"]),
                "distinct content_id served",
            ),
        ],
    )
    st.write("")

    c1, c2 = st.columns([1, 1.3])
    with c1:
        st.markdown("**Live vs VOD split**")
        lv, lv_ms = _timed(queries.get_live_vs_vod, time_filter)
        if lv.empty:
            st.info("No data.")
        else:
            st.plotly_chart(
                ui.donut(lv, "video_type", "streams", latency_ms=lv_ms),
                width="stretch",
                config={"displayModeBar": False},
            )
    with c2:
        st.markdown("**Viewers by country**")
        geo, geo_ms = _timed(queries.get_geo_distribution, time_filter)
        if geo.empty:
            st.info("No data.")
        else:
            st.plotly_chart(
                ui.bar(geo, "country", "streams", "streams", latency_ms=geo_ms),
                width="stretch",
                config={"displayModeBar": False},
            )

    st.write("")
    st.markdown("**Top 10 content by concurrent streams**")
    top, top_ms = _timed(queries.get_top_content_leaderboard, time_filter, 10)
    if top.empty:
        st.info("No content served.")
    else:
        st.plotly_chart(
            ui.bar(top, "title", "streams", "streams", latency_ms=top_ms),
            width="stretch",
            config={"displayModeBar": False},
        )
        st.dataframe(
            top.rename(
                columns={
                    "content_id": "Content ID",
                    "title": "Title",
                    "video_type": "Type",
                    "category": "Category",
                    "streams": "Streams",
                    "unique_viewers": "Unique viewers (approx)",
                }
            ),
            hide_index=True,
            width="stretch",
        )


# ===========================================================================
# Pane — Business insights (QoE & engagement overlays + KPI tiles; see business.py)
# ===========================================================================
def _biz_chart(f: dict, title: str, caption: str, fetch, render) -> None:
    """Render one Business-insights chart block with a per-chart ⏱ latency badge.

    `fetch(f) -> (data, ms)`; `render(data, ms)` draws it. Wrapped so a query
    that references a not-yet-present event degrades to a per-chart warning.
    """
    st.markdown(f"**{title}**")
    if caption:
        st.caption(caption)
    try:
        data, ms = fetch(f)
    except Exception as e:  # noqa: BLE001
        st.error(f"⚠ Query failed: {e}")
        return
    if hasattr(data, "empty") and data.empty:
        st.info("No data for the selected range.")
    else:
        render(data, ms)


def render_business_insights(time_filter: dict) -> None:
    st.caption(
        "QoE & engagement analytics over `events_raw` + `concurrency_now`. "
        "Time-series overlays honor the selected range (5-min buckets). "
        "QoE / flow charts filter by platform / country / content; "
        "video-type & category apply to the concurrency-based series."
    )
    try:
        opts = queries.get_filter_options()
    except Exception as e:  # noqa: BLE001
        st.error(f"⚠ Failed to load filters: {e}")
        return

    f = {**queries.EMPTY_FILTERS, **time_filter}
    r = st.columns([1, 1, 1, 1, 1.6])

    def msel(container, label: str, values: list[str], key: str) -> list[str]:
        return container.multiselect(label, values, default=[], placeholder="All", key=key)

    f["platforms"] = msel(r[0], "Platform", opts["platforms"], "biz_platform")
    f["countries"] = msel(r[1], "Country", opts["countries"], "biz_country")
    f["video_types"] = msel(r[2], "Video type", opts["video_types"], "biz_video_type")
    f["categories"] = msel(r[3], "Category", opts["categories"], "biz_category")
    contents = opts["contents"]
    labels, ids = ["All content"], [""]
    if not contents.empty:
        for _, row in contents.iterrows():
            labels.append(row["title"] or row["content_id"])
            ids.append(row["content_id"])
    idx = r[4].selectbox(
        "Content", range(len(labels)), format_func=lambda i: labels[i], key="biz_content"
    )
    f["content_id"] = ids[idx]

    _cfg = {"displayModeBar": False}

    # ---- Time-series overlays ------------------------------------------------
    st.markdown("### 📈 Time-series overlays")
    _biz_chart(
        f, "TS-1 · Attention ratio",
        "Fraction of sessions actively playing vs paused/backgrounded (0–1), over the streams area.",
        bizmod.get_attention_ratio,
        lambda d, m: st.plotly_chart(
            ui.dual_axis(
                d, "ts", "total_streams", "attention_ratio",
                "Total streams", "Attention ratio",
                right_kind="line", right_range=[0, 1], latency_ms=m,
            ),
            width="stretch", config=_cfg,
        ),
    )
    st.write("")
    _biz_chart(
        f, "TS-2 · Ramp velocity (Δ concurrent)",
        "Bucket-over-bucket audience growth (green) / decline (red).",
        bizmod.get_ramp_velocity,
        lambda d, m: st.plotly_chart(
            ui.diverging_bar(d, "ts", "delta_streams", "Δ streams", latency_ms=m),
            width="stretch", config=_cfg,
        ),
    )
    st.write("")
    _biz_chart(
        f, "TS-3 · Net flow (arrivals vs departures)",
        "Arrivals (green) vs departures (red) per bucket, with a running open-sessions line.",
        bizmod.get_net_flow,
        lambda d, m: st.plotly_chart(
            ui.diverging_bar(
                d, "ts", "net_flow", "Net flow",
                line_col="open_sessions", line_label="Open sessions", latency_ms=m,
            ),
            width="stretch", config=_cfg,
        ),
    )
    st.write("")
    _biz_chart(
        f, "TS-4 · Retention % of peak",
        "Concurrency normalized to % of the window peak — post-peak audience decay.",
        bizmod.get_retention,
        lambda d, m: st.plotly_chart(
            ui.time_area(d, "ts", "pct_of_peak", "% of peak", latency_ms=m),
            width="stretch", config=_cfg,
        ),
    )
    st.write("")
    _biz_chart(
        f, "TS-5 · QoE overlay (errors + rebuffers)",
        "Error sessions (secondary-axis bars) over the concurrency curve — does a quality spike track an audience drop?",
        bizmod.get_qoe_overlay,
        lambda d, m: st.plotly_chart(
            ui.dual_axis(
                d, "ts", "streams", "error_sessions",
                "Streams", "Error sessions",
                right_kind="bar", right_color=DANGER, latency_ms=m,
            ),
            width="stretch", config=_cfg,
        ),
    )

    # ---- KPI tiles -----------------------------------------------------------
    st.write("")
    st.markdown("### 🔢 KPI tiles")

    try:
        k, m = bizmod.get_ad_break(f)
        ui.tiles_row(
            st.columns(3),
            [
                ui.kpi_tile("Ad-break sessions", ui.fmt(k["ad_sessions"]),
                            "sessions with an ad break", "accent"),
                ui.kpi_tile("Resume rate", f"{k['resume_rate']:.1f}%",
                            f"{ui.fmt(k['resumed_after_ad'])} resumed after ad "
                            + ("⚠" if k["resume_rate"] < 50 else "✅"),
                            "danger" if k["resume_rate"] < 50 else "accent"),
                ui.kpi_tile("Drop-off rate", f"{k['dropoff_rate']:.1f}%",
                            f"{ui.fmt(k['ended_during_ad'])} ended during/after ad", "accent2"),
            ],
        )
        st.caption(f"KPI-4 · Ad-break resume & drop-off · ⏱ {m:,.0f} ms")
    except Exception as e:  # noqa: BLE001
        st.error(f"⚠ KPI-4 failed: {e}")

    st.write("")
    try:
        k, m = bizmod.get_playback_health(f)
        ui.tiles_row(
            st.columns(4),
            [
                ui.kpi_tile("Play success", f"{k['play_success_rate']:.1f}%",
                            "reached first frame " + ("✅" if k["play_success_rate"] >= 97 else "⚠"),
                            "accent" if k["play_success_rate"] >= 97 else "danger"),
                ui.kpi_tile("VSF", f"{k['vsf_pct']:.2f}%",
                            "error before start " + ("✅" if k["vsf_pct"] < 1 else "⚠"),
                            "danger" if k["vsf_pct"] >= 1 else ""),
                ui.kpi_tile("EBVS", f"{k['ebvs_pct']:.2f}%",
                            "never started " + ("✅" if k["ebvs_pct"] < 3 else "⚠"),
                            "danger" if k["ebvs_pct"] >= 3 else ""),
                ui.kpi_tile("VPF", f"{k['vpf_pct']:.2f}%",
                            "error mid-play " + ("✅" if k["vpf_pct"] < 5 else "⚠"),
                            "danger" if k["vpf_pct"] >= 5 else ""),
            ],
        )
        st.caption(f"KPI-6 · Playback success / VSF / EBVS / VPF · ⏱ {m:,.0f} ms")
    except Exception as e:  # noqa: BLE001
        st.error(f"⚠ KPI-6 failed: {e}")

    st.write("")
    try:
        k, m = bizmod.get_vst(f)
        ui.tiles_row(
            st.columns(4),
            [
                ui.kpi_tile("VST p50", f"{k['p50']:.1f}s",
                            "median start " + ("✅" if k["p50"] < 3 else "⚠ >3s target"),
                            "danger" if k["p50"] >= 3 else "accent"),
                ui.kpi_tile("VST p95", f"{k['p95']:.1f}s", "95th percentile"),
                ui.kpi_tile("VST p99", f"{k['p99']:.1f}s", "99th percentile"),
                ui.kpi_tile("VST avg", f"{k['avg']:.1f}s", "mean start time", "accent2"),
            ],
        )
        st.caption(f"KPI-7 · Video Start Time · ⏱ {m:,.0f} ms")
    except Exception as e:  # noqa: BLE001
        st.error(f"⚠ KPI-7 failed: {e}")

    st.write("")
    try:
        k, m = bizmod.get_rebuffering(f)
        ui.tiles_row(
            st.columns(3),
            [
                ui.kpi_tile("Sessions w/ rebuffer", ui.fmt(k["sessions_with_rebuffer"]),
                            "≥1 rebuffer event", "accent2"),
                ui.kpi_tile("Avg rebuffer / session", f"{k['avg_rebuffer_secs']:.1f}s",
                            "mean stall time"),
                ui.kpi_tile("Rebuffering ratio", f"{k['rebuffering_ratio']:.2f}%",
                            "stall ÷ watch time " + ("✅" if k["rebuffering_ratio"] < 0.5 else "⚠"),
                            "danger" if k["rebuffering_ratio"] >= 0.5 else "accent"),
            ],
        )
        st.caption(f"KPI-8 · Rebuffering ratio · ⏱ {m:,.0f} ms")
    except Exception as e:  # noqa: BLE001
        st.error(f"⚠ KPI-8 failed: {e}")

    st.write("")
    try:
        k, m = bizmod.get_viewer_stats(f)
        ui.tiles_row(
            st.columns(4),
            [
                ui.kpi_tile("Viewer-hours", ui.fmt(k["viewer_hours"]), "content-hours in range", "accent"),
                ui.kpi_tile("Peak concurrent", ui.fmt(k["peak_concurrent"]), "busiest minute"),
                ui.kpi_tile("Peak-to-avg", f"{k['peak_to_avg']:.1f}×",
                            "spikiness — size for peak", "accent2"),
                ui.kpi_tile("Sessions / user", f"{k['sessions_per_user']:.2f}", "distinct sessions per user"),
            ],
        )
        st.caption(f"KPI-9 · Viewer-hours, peak-to-avg, sessions/user · ⏱ {m:,.0f} ms")
    except Exception as e:  # noqa: BLE001
        st.error(f"⚠ KPI-9 failed: {e}")


# ===========================================================================
# Toolbar (Grafana-style time range + refresh) + realtime ticker strip
# ===========================================================================
_QUICK_KEYS = list(QUICK_RANGES)
_INTERVAL_KEYS = list(REFRESH_INTERVALS)
_DEFAULT_QUICK = _QUICK_KEYS.index("Last 24 hours")
_DEFAULT_INTERVAL = _INTERVAL_KEYS.index("30s")

# Live ticker metrics: (display label, key in queries.get_live_metrics()).
_LIVE_METRICS = [
    ("Active sessions", "concurrency"),
    ("Active users", "users"),
    ("New sessions / min", "sessions"),
]


def _popover(label: str):
    """st.popover if available (Streamlit ≥1.32), else st.expander (fallback)."""
    pop = getattr(st, "popover", None)
    return pop(label) if pop is not None else st.expander(label)


def _fmt_time_filter(frm: datetime, to: datetime) -> dict:
    """Serialize a from/to window to the {'from','to'} dict the panes consume."""
    return {
        "from": frm.strftime("%Y-%m-%d %H:%M:%S"),
        "to": to.strftime("%Y-%m-%d %H:%M:%S"),
    }


def resolve_time_range(quick_label: str) -> dict:
    """Turn the quick-range selection into a {'from','to'} filter.

    Presets are anchored to wall-clock now(); "Custom" reveals absolute
    date/time inputs in a popover (see config.QUICK_RANGES).
    """
    secs = QUICK_RANGES[quick_label]
    if secs is not None:
        now = datetime.now()
        return _fmt_time_filter(now - timedelta(seconds=secs), now)

    today = date.today()
    with _popover("🗓  Custom range"):
        st.caption("Absolute window (matches the data clock).")
        from_date = st.date_input("From date", value=today, key="cust_from_date")
        from_time = st.time_input("From time", value=time(0, 0), key="cust_from_time")
        to_date = st.date_input("To date", value=today, key="cust_to_date")
        to_time = st.time_input("To time", value=time(23, 59), key="cust_to_time")
    return _fmt_time_filter(
        datetime.combine(from_date, from_time),
        datetime.combine(to_date, to_time),
    )


def render_realtime_strip() -> None:
    """Stock-style live tiles: current value + green/red delta vs the previous
    refresh + a ~15-min sparkline.

    Global and UNFILTERED ("what's live on SonyLIV right now") — independent of
    the tab filters and the selected time range, which keeps the "vs previous
    refresh" delta clean (values move when a minute passes, not on a filter
    change). Deltas are held in st.session_state across reruns.
    """
    try:
        cur = queries.get_live_metrics()
        spark = queries.get_live_sparklines()
    except Exception as e:  # noqa: BLE001
        st.warning(f"⚠ Realtime metrics unavailable: {e}")
        return

    prev = st.session_state.get("_live_prev", {})
    tiles = []
    for label, key in _LIVE_METRICS:
        val = int(cur.get(key, 0))
        p = prev.get(key)
        delta = None if p is None else val - p
        pct = ((val - p) / p * 100) if p else None
        series = spark[key].tolist() if (not spark.empty and key in spark) else []
        tiles.append(ui.ticker_tile(label, ui.fmt(val), delta, pct, series))
    ui.ticker_row(st.columns(3), tiles)
    st.session_state["_live_prev"] = cur


# ===========================================================================
# App shell
# ===========================================================================
def main() -> None:
    ui.inject_css()

    try:
        from clickhouse_client import get_client

        get_client()
    except Exception as e:  # noqa: BLE001
        st.error(f"⚠ Could not connect to ClickHouse: {e}")
        st.stop()

    # ---- Header + toolbar (Grafana-style) ------------------------------------
    left, right = st.columns([2, 3])
    with left:
        st.markdown(
            f'<div class="brand">{ui.CH_LOGO}<div>'
            f'<p class="dash-title">SonyLIV — Viewing Concurrency</p>'
            f'<div class="dash-sub">Concurrency · benchmark · insights · '
            f'ClickHouse Cloud · <span class="mono">{DB}</span></div>'
            f"</div></div>",
            unsafe_allow_html=True,
        )
    with right:
        tcol, rcol, bcol = st.columns([2, 1.1, 0.9], vertical_alignment="bottom")
        quick_label = tcol.selectbox("Time range", _QUICK_KEYS, index=_DEFAULT_QUICK)
        interval_label = rcol.selectbox("Refresh", _INTERVAL_KEYS, index=_DEFAULT_INTERVAL)
        refresh = bcol.button("🔄", width="stretch", type="primary", help="Refresh now")

    # Quick range → {'from','to'}; "Custom" opens a popover with absolute inputs.
    time_filter = resolve_time_range(quick_label)

    interval_ms = REFRESH_INTERVALS[interval_label]
    if interval_ms is not None and st_autorefresh is not None:
        st_autorefresh(interval=interval_ms, key="auto")
    if refresh:
        st.rerun()

    live = interval_ms is not None
    badge = "live-badge" if live else "live-badge paused"
    cadence = f"auto every {interval_label}" if live else "auto-refresh off"
    st.markdown(
        f'<div class="dash-sub"><span class="{badge}"><span class="pulse"></span>'
        f'{"LIVE" if live else "PAUSED"}</span> &nbsp;·&nbsp; {cadence} · updated '
        f'{datetime.now().strftime("%H:%M:%S")}</div>',
        unsafe_allow_html=True,
    )
    st.write("")

    # ---- Realtime ticker strip (global "live now", above the tabs) -----------
    render_realtime_strip()
    st.caption(
        "Live now — last minute across all traffic, vs the previous refresh "
        "(green ▲ up / red ▼ down). Independent of the tab filters and time range."
    )
    st.write("")

    # ---- Dashboard tabs --------------------------------------------------------
    tab_over, tab_conc, tab_biz = st.tabs(
        ["📺 Overview", "📈 Concurrency", "💼 Business insights"]
    )
    with tab_over:
        render_overview(time_filter)
    with tab_conc:
        render_concurrency(time_filter)
    with tab_biz:
        render_business_insights(time_filter)


if __name__ == "__main__":
    main()
