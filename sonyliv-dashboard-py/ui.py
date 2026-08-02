"""Shared UI helpers — theme CSS, KPI tiles, and reusable chart builders.

Kept separate from the data modules (queries/errors/insights) and the app shell
(app.py) so styling and chart construction live in one place. Palette comes from
config.py (ClickHouse-inspired yellow-on-near-black).
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import (
    ACCENT,
    ACCENT_2,
    ACCENT_FILL,
    BORDER,
    DOWN,
    DOWN_FILL,
    MUTED,
    PANEL,
    PANEL_2,
    TEXT,
    UP,
    UP_FILL,
)

# ClickHouse-style columnar bar mark (three tall bars + one short) in brand yellow.
CH_LOGO = f"""
<svg width="30" height="30" viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg">
  <rect x="4"  y="6"  width="8" height="36" rx="2" fill="{ACCENT}"/>
  <rect x="16" y="6"  width="8" height="36" rx="2" fill="{ACCENT}"/>
  <rect x="28" y="6"  width="8" height="36" rx="2" fill="{ACCENT}"/>
  <rect x="40" y="18" width="8" height="24" rx="2" fill="{ACCENT}"/>
</svg>"""


def inject_css() -> None:
    st.markdown(
        f"""
        <style>
          /* clear Streamlit's fixed top toolbar so the header isn't covered */
          header[data-testid="stHeader"] {{ background: transparent; height: 0; }}
          .block-container {{ padding-top: 4.5rem; max-width: 1250px; }}
          #MainMenu, footer {{ visibility: hidden; }}
          /* header / brand */
          .brand {{ display:flex; align-items:center; gap:12px; }}
          .brand svg {{ flex:0 0 auto; }}
          .dash-title {{ font-size: 23px; font-weight: 800; margin: 0;
                         letter-spacing: -0.2px; }}
          .dash-sub {{ color: {MUTED}; font-size: 13px; margin-top: 2px; }}
          .mono {{ font-family: ui-monospace, Menlo, Consolas, monospace;
                   font-variant-numeric: tabular-nums; }}
          /* KPI tiles */
          .tile {{ background: linear-gradient(180deg, {PANEL}, {PANEL_2});
                   border: 1px solid {BORDER}; border-radius: 14px;
                   padding: 18px 20px; box-shadow: 0 6px 24px rgba(0,0,0,0.35); }}
          .tile.peak {{ border-color: {ACCENT};
                        box-shadow: 0 0 0 1px {ACCENT}33, 0 6px 24px rgba(0,0,0,0.35); }}
          .k-label {{ font-size: 12px; text-transform: uppercase;
                      letter-spacing: 0.7px; color: {MUTED}; }}
          .k-value {{ font-size: 38px; font-weight: 800; margin-top: 8px;
                      line-height: 1; color: {TEXT}; }}
          .k-value.accent {{ color: {ACCENT}; }}
          .k-value.accent2 {{ color: {ACCENT_2}; }}
          .k-value.danger {{ color: #ff6b6b; }}
          .k-sub {{ font-size: 12px; color: {MUTED}; margin-top: 8px; }}
          /* live dot */
          .dot {{ display:inline-block; width:8px; height:8px; border-radius:50%;
                  background:{MUTED}; margin-right:6px; }}
          .dot.live {{ background:{ACCENT}; box-shadow:0 0 0 3px {ACCENT}2e; }}
          /* primary (Refresh) button → ClickHouse yellow with dark text */
          .stButton > button[kind="primary"] {{ background:{ACCENT}; color:#141414;
                      border:0; font-weight:700; }}
          /* tabs → yellow active accent */
          .stTabs [data-baseweb="tab-list"] {{ gap: 6px; }}
          .stTabs [data-baseweb="tab"] {{ font-weight: 600; }}
          .stTabs [aria-selected="true"] {{ color: {ACCENT} !important; }}
          .stTabs [data-baseweb="tab-highlight"] {{ background-color: {ACCENT}; }}
          div[data-testid="stMetricValue"] {{ font-variant-numeric: tabular-nums; }}
          /* realtime ticker strip (stock-style live tiles) */
          .ticker {{ background: linear-gradient(180deg, {PANEL}, {PANEL_2});
                     border: 1px solid {BORDER}; border-radius: 12px;
                     padding: 14px 16px 10px; box-shadow: 0 4px 18px rgba(0,0,0,0.30);
                     transition: transform .12s ease, border-color .12s ease; }}
          .ticker:hover {{ transform: translateY(-2px); border-color: {MUTED}55; }}
          .tk-label {{ font-size: 11px; text-transform: uppercase;
                       letter-spacing: 0.7px; color: {MUTED}; }}
          .tk-row {{ display:flex; align-items:baseline; justify-content:space-between;
                     gap:10px; margin-top:6px; }}
          .tk-value {{ font-size: 30px; font-weight: 800; line-height: 1; color: {TEXT}; }}
          .tk-delta {{ display:inline-flex; align-items:center; gap:4px; font-size:12px;
                       font-weight:700; padding:3px 9px; border-radius:999px;
                       font-variant-numeric: tabular-nums; white-space:nowrap; }}
          .tk-delta.up   {{ color:{UP};   background:{UP_FILL}; }}
          .tk-delta.down {{ color:{DOWN}; background:{DOWN_FILL}; }}
          .tk-delta.flat {{ color:{MUTED}; background:rgba(255,255,255,0.05); }}
          .tk-spark {{ display:block; margin-top:10px; width:100%; height:38px; }}
          /* live badge (pulsing green dot) */
          .live-badge {{ display:inline-flex; align-items:center; gap:7px; font-size:12px;
                         font-weight:700; letter-spacing:0.5px; color:{TEXT}; }}
          .live-badge.paused {{ color:{MUTED}; }}
          .live-badge .pulse {{ width:8px; height:8px; border-radius:50%; background:{UP};
                                animation:tkpulse 1.8s infinite; }}
          .live-badge.paused .pulse {{ background:{MUTED}; animation:none; }}
          @keyframes tkpulse {{ 0%{{box-shadow:0 0 0 0 {UP}88;}}
                                70%{{box-shadow:0 0 0 7px {UP}00;}}
                                100%{{box-shadow:0 0 0 0 {UP}00;}} }}
        </style>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def fmt(n) -> str:
    """Thousands-separated integer, or an em-dash for missing values."""
    return f"{int(n):,}" if pd.notna(n) else "—"


def human(n) -> str:
    """Compact, business-friendly number: 950, 12.3K, 1.2M, 3.4B.

    For headline tiles read by non-technical folks — easier to scan than a long
    comma-separated integer. Values under 1,000 stay exact.
    """
    if not pd.notna(n):
        return "—"
    n = float(n)
    a = abs(n)
    if a >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if a >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if a >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{int(round(n)):,}"


def pretty_minute(ts) -> str:
    if not ts or pd.isna(ts):
        return "—"
    return str(ts).replace("T", " ")[:16] + " UTC"


def kpi_tile(
    label: str, value: str, sub: str, accent: str = "", peak: bool = False
) -> str:
    """Return the HTML for one KPI tile (render with st.markdown, unsafe_allow_html)."""
    tile_cls = "tile peak" if peak else "tile"
    val_cls = f"k-value {accent}".strip()
    return (
        f'<div class="{tile_cls}"><div class="k-label">{label}</div>'
        f'<div class="{val_cls} mono">{value}</div>'
        f'<div class="k-sub">{sub}</div></div>'
    )


def tiles_row(cols, tiles: list[str]) -> None:
    """Render a row of KPI tiles into the given Streamlit columns."""
    for col, html in zip(cols, tiles):
        col.markdown(html, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Charts (Plotly, transparent background, ClickHouse palette)
# ---------------------------------------------------------------------------
def _base_layout(fig: go.Figure, height: int = 380) -> go.Figure:
    fig.update_layout(
        height=height,
        margin=dict(l=8, r=16, t=30, b=8),  # top room for the latency badge
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(
            color=MUTED,
            size=12,
            family="ui-sans-serif, -apple-system, 'Segoe UI', Roboto, sans-serif",
        ),
        showlegend=False,
        hoverlabel=dict(bgcolor=PANEL, bordercolor=BORDER, font=dict(color=TEXT, size=12)),
    )
    return fig


def _add_latency(fig: go.Figure, latency_ms: float | None) -> go.Figure:
    """Stamp a small '⏱ N ms' query-latency badge in the chart's top-right corner."""
    if latency_ms is None:
        return fig
    fig.add_annotation(
        xref="paper",
        yref="paper",
        x=1.0,
        y=1.10,
        xanchor="right",
        yanchor="top",
        text=f"⏱ {latency_ms:,.0f} ms",
        showarrow=False,
        font=dict(color=MUTED, size=11, family="ui-monospace, Menlo, monospace"),
        bgcolor=PANEL_2,
        bordercolor=BORDER,
        borderpad=3,
        borderwidth=1,
        opacity=0.92,
    )
    return fig


def _lat_hover(latency_ms: float | None) -> str:
    """Hover-tooltip suffix showing this chart's query latency (empty if unknown).

    Appended to a trace's hovertemplate so the measured latency also shows up on
    hover, not only in the always-on corner badge (_add_latency)."""
    return f"<br>⏱ {latency_ms:,.0f} ms" if latency_ms is not None else ""


def time_area(
    df: pd.DataFrame,
    ts_col: str,
    y_col: str,
    label: str,
    color: str = ACCENT,
    fill: str = ACCENT_FILL,
    latency_ms: float | None = None,
) -> go.Figure:
    """Filled area chart over a time axis."""
    x = pd.to_datetime(df[ts_col])
    hov = _lat_hover(latency_ms)
    fig = go.Figure(
        go.Scatter(
            x=x,
            y=df[y_col],
            mode="lines",
            line=dict(color=color, width=2, shape="spline"),
            fill="tozeroy",
            fillcolor=fill,
            hovertemplate=f"%{{x|%Y-%m-%d %H:%M}} UTC<br><b>%{{y:,}}</b> {label}{hov}<extra></extra>",
        )
    )
    _base_layout(fig)
    fig.update_layout(
        hovermode="x unified",
        xaxis=dict(
            showgrid=False,
            tickformat="%H:%M",
            color=MUTED,
            showspikes=True,
            spikethickness=1,
            spikedash="dot",
            spikecolor=MUTED,
            spikemode="across",
        ),
        yaxis=dict(
            gridcolor=BORDER,
            griddash="dot",
            rangemode="tozero",
            color=MUTED,
            tickformat=",",
        ),
    )
    return _add_latency(fig, latency_ms)


def bar(
    df: pd.DataFrame,
    cat_col: str,
    val_col: str,
    label: str,
    color: str = ACCENT,
    horizontal: bool = True,
    latency_ms: float | None = None,
) -> go.Figure:
    """Categorical bar chart. Horizontal by default (nice for ranked lists)."""
    hov = _lat_hover(latency_ms)
    if horizontal:
        d = df.iloc[::-1]  # largest at top
        fig = go.Figure(
            go.Bar(
                x=d[val_col],
                y=d[cat_col].astype(str),
                orientation="h",
                marker=dict(color=color),
                hovertemplate=f"%{{y}}<br><b>%{{x:,}}</b> {label}{hov}<extra></extra>",
            )
        )
        _base_layout(fig, height=max(220, 40 * len(df) + 60))
        fig.update_layout(
            xaxis=dict(gridcolor=BORDER, griddash="dot", color=MUTED, tickformat=","),
            yaxis=dict(color=TEXT),
        )
    else:
        fig = go.Figure(
            go.Bar(
                x=df[cat_col].astype(str),
                y=df[val_col],
                marker=dict(color=color),
                hovertemplate=f"%{{x}}<br><b>%{{y:,}}</b> {label}{hov}<extra></extra>",
            )
        )
        _base_layout(fig)
        fig.update_layout(
            xaxis=dict(color=TEXT),
            yaxis=dict(gridcolor=BORDER, griddash="dot", color=MUTED, tickformat=","),
        )
    return _add_latency(fig, latency_ms)


def grouped_bar(
    df: pd.DataFrame,
    cat_col: str,
    series: list[tuple[str, str, str]],
    latency_ms: float | None = None,
) -> go.Figure:
    """Grouped vertical bars — one trace per (value_col, label, color) in `series`.

    Used for side-by-side comparisons like peak vs. average per time bucket.
    """
    hov = _lat_hover(latency_ms)
    fig = go.Figure()
    for i, (val_col, label, color) in enumerate(series):
        fig.add_trace(
            go.Bar(
                x=df[cat_col].astype(str),
                y=df[val_col],
                name=label,
                marker=dict(color=color),
                # latency suffix on the first series only (avoids repeating it per
                # series in the shared hover box).
                hovertemplate=f"%{{x}}<br><b>%{{y:,}}</b> {label}{hov if i == 0 else ''}<extra></extra>",
            )
        )
    _base_layout(fig)
    fig.update_layout(
        barmode="group",
        showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, font=dict(color=MUTED)),
        xaxis=dict(color=TEXT),
        yaxis=dict(gridcolor=BORDER, griddash="dot", color=MUTED, tickformat=","),
    )
    return _add_latency(fig, latency_ms)


def donut(
    df: pd.DataFrame, name_col: str, val_col: str, latency_ms: float | None = None
) -> go.Figure:
    """Donut chart — used for share/segment breakdowns."""
    palette = [ACCENT, ACCENT_2, "#7c9cff", "#ff8fab", "#5eead4", "#c4b5fd"]
    hov = _lat_hover(latency_ms)
    fig = go.Figure(
        go.Pie(
            labels=df[name_col].astype(str),
            values=df[val_col],
            hole=0.62,
            marker=dict(colors=palette[: len(df)], line=dict(color=PANEL, width=2)),
            textinfo="label+percent",
            textfont=dict(color=TEXT, size=12),
            hovertemplate=f"%{{label}}<br><b>%{{value:,}}</b> (%{{percent}}){hov}<extra></extra>",
        )
    )
    _base_layout(fig, height=320)
    fig.update_layout(showlegend=False)
    return _add_latency(fig, latency_ms)


def dual_axis(
    df: pd.DataFrame,
    ts_col: str,
    left_col: str,
    right_col: str,
    left_label: str,
    right_label: str,
    right_kind: str = "line",  # "line" | "bar"
    right_range: list | None = None,
    left_color: str = ACCENT,
    left_fill: str = ACCENT_FILL,
    right_color: str = ACCENT_2,
    latency_ms: float | None = None,
) -> go.Figure:
    """Time chart with a left-axis filled area + a right-axis series (line or bars).

    Used for TS-1 (streams area + attention-ratio line 0–1) and TS-5 (concurrency
    area + QoE bars on the secondary axis).
    """
    x = pd.to_datetime(df[ts_col])
    hov = _lat_hover(latency_ms)
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=x, y=df[left_col], name=left_label, mode="lines",
            line=dict(color=left_color, width=2, shape="spline"),
            fill="tozeroy", fillcolor=left_fill,
            hovertemplate=f"<b>%{{y:,}}</b> {left_label}{hov}<extra></extra>",
        )
    )
    if right_kind == "bar":
        fig.add_trace(
            go.Bar(
                x=x, y=df[right_col], name=right_label, yaxis="y2",
                marker=dict(color=right_color), opacity=0.55,
                hovertemplate=f"<b>%{{y:,}}</b> {right_label}<extra></extra>",
            )
        )
    else:
        fig.add_trace(
            go.Scatter(
                x=x, y=df[right_col], name=right_label, mode="lines", yaxis="y2",
                line=dict(color=right_color, width=2),
                hovertemplate=f"<b>%{{y:,.3f}}</b> {right_label}<extra></extra>",
            )
        )
    _base_layout(fig)
    fig.update_layout(
        hovermode="x unified",
        showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, font=dict(color=MUTED)),
        xaxis=dict(
            showgrid=False, tickformat="%H:%M", color=MUTED,
            showspikes=True, spikethickness=1, spikedash="dot", spikecolor=MUTED,
        ),
        yaxis=dict(gridcolor=BORDER, griddash="dot", rangemode="tozero", color=left_color, tickformat=","),
        yaxis2=dict(
            overlaying="y", side="right", color=right_color, showgrid=False,
            rangemode="tozero", range=right_range,
        ),
    )
    return _add_latency(fig, latency_ms)


def diverging_bar(
    df: pd.DataFrame,
    x_col: str,
    y_col: str,
    label: str,
    line_col: str | None = None,
    line_label: str = "",
    latency_ms: float | None = None,
) -> go.Figure:
    """Time bars colored green (≥0) / red (<0) by sign; optional overlaid line (right axis).

    Used for TS-2 ramp velocity (Δ concurrent) and TS-3 net flow (arrivals −
    departures) with a running open-sessions line.
    """
    x = pd.to_datetime(df[x_col])
    y = df[y_col]
    colors = [UP if float(v) >= 0 else DOWN for v in y.fillna(0)]
    hov = _lat_hover(latency_ms)
    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=x, y=y, name=label, marker=dict(color=colors),
            hovertemplate=f"<b>%{{y:,}}</b> {label}{hov}<extra></extra>",
        )
    )
    if line_col is not None:
        fig.add_trace(
            go.Scatter(
                x=x, y=df[line_col], name=line_label, mode="lines", yaxis="y2",
                line=dict(color=ACCENT, width=2),
                hovertemplate=f"<b>%{{y:,}}</b> {line_label}<extra></extra>",
            )
        )
    _base_layout(fig)
    fig.update_layout(
        hovermode="x unified",
        xaxis=dict(showgrid=False, tickformat="%H:%M", color=MUTED),
        yaxis=dict(
            gridcolor=BORDER, griddash="dot", color=MUTED, tickformat=",",
            zeroline=True, zerolinecolor=MUTED, zerolinewidth=1,
        ),
    )
    if line_col is not None:
        fig.update_layout(
            showlegend=True,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, font=dict(color=MUTED)),
            yaxis2=dict(overlaying="y", side="right", color=ACCENT, showgrid=False),
        )
    return _add_latency(fig, latency_ms)


# ---------------------------------------------------------------------------
# Realtime ticker (stock-style live tiles: value + green/red delta + sparkline)
# ---------------------------------------------------------------------------
_SPARK_W = 150  # viewBox width; the SVG stretches to the tile via width:100%
_SPARK_H = 38


def sparkline_svg(values, color: str, fill: str) -> str:
    """Inline SVG sparkline for the last few points of a metric.

    Kept as raw SVG (not a Plotly widget) so it lives INSIDE the ticker tile's
    HTML — one card, no separate chart element. Stretches to the tile width via
    `width:100%` + a fixed viewBox with preserveAspectRatio="none". Returns an
    empty string if there is nothing meaningful to draw.
    """
    pts = [float(v) for v in values if v is not None]
    w, h, pad = _SPARK_W, _SPARK_H, 3
    if len(pts) < 2:
        return ""
    lo, hi = min(pts), max(pts)
    rng = (hi - lo) or 1.0
    n = len(pts)

    def px(i: int) -> float:
        return pad + i * (w - 2 * pad) / (n - 1)

    def py(v: float) -> float:
        return h - pad - (v - lo) / rng * (h - 2 * pad)

    coords = [(px(i), py(v)) for i, v in enumerate(pts)]
    line = " ".join(f"{cx:.1f},{cy:.1f}" for cx, cy in coords)
    area = f"{pad:.1f},{h - pad:.1f} {line} {w - pad:.1f},{h - pad:.1f}"
    last_x, last_y = coords[-1]
    return (
        f'<svg class="tk-spark" viewBox="0 0 {w} {h}" width="100%" height="{h}" '
        f'preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">'
        f'<polygon points="{area}" fill="{fill}" stroke="none"/>'
        f'<polyline points="{line}" fill="none" stroke="{color}" '
        f'stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round" '
        f'vector-effect="non-scaling-stroke"/>'
        f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="2.4" fill="{color}"/>'
        f"</svg>"
    )


def _delta_pill(delta: int | None, pct: float | None) -> tuple[str, str]:
    """(css_class, text) for the up/down/flat delta pill. None → first load."""
    if delta is None:
        return "flat", "—"
    if delta == 0:
        return "flat", "±0"
    arrow = "▲" if delta > 0 else "▼"
    cls = "up" if delta > 0 else "down"
    pct_str = f" ({pct:+.1f}%)" if pct is not None else ""
    return cls, f"{arrow} {fmt(abs(delta))}{pct_str}"


def ticker_tile(
    label: str,
    value: str,
    delta: int | None = None,
    pct: float | None = None,
    spark_values=None,
) -> str:
    """HTML for one realtime ticker tile (render via st.markdown, unsafe_allow_html).

    `delta` is the change vs the previous refresh (None on first load → flat).
    The sparkline colour follows the delta sign (green up / red down / muted flat).
    """
    cls, text = _delta_pill(delta, pct)
    color, fill = (
        (UP, UP_FILL) if cls == "up" else (DOWN, DOWN_FILL) if cls == "down" else (MUTED, "rgba(255,255,255,0.06)")
    )
    spark = sparkline_svg(spark_values or [], color, fill)
    return (
        f'<div class="ticker"><div class="tk-label">{label}</div>'
        f'<div class="tk-row"><span class="tk-value mono">{value}</span>'
        f'<span class="tk-delta {cls}">{text}</span></div>{spark}</div>'
    )


def ticker_row(cols, tiles: list[str]) -> None:
    """Render a row of realtime ticker tiles into the given Streamlit columns."""
    for col, html in zip(cols, tiles):
        col.markdown(html, unsafe_allow_html=True)
