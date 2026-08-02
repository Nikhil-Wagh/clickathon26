"""SonyLIV — Daily Wrapped.

A Spotify-Wrapped-style story of a single day's viewing: vivid gradient cards you
step through, one big insight each, ending in a shareable recap poster. Reuses the
same ClickHouse queries as the dashboard (queries.py / business.py) — this module
is pure presentation + a thin data-gather layer.

Design: bold duotone gradients rotating per card, a characterful display face
(Bricolage Grotesque) over a clean body face (Instrument Sans), Instagram-style
segmented progress bars, staggered fade-up reveals, CSS/SVG mini-viz that lives
inside each card's gradient, and a grain overlay for texture.

The story auto-anchors to the BUSIEST day in the loaded data (so it always feels
alive), with a picker to flip to any other day.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

import business as bizmod
import queries
import ui
from clickhouse_client import query_df
from config import DB

# ---------------------------------------------------------------------------
# Palette — one vivid duotone per card. ink = main text, accent = the hero pop.
# ---------------------------------------------------------------------------
PALETTES = [
    {"bg": "linear-gradient(150deg,#ff2d95 0%,#7a04eb 100%)", "ink": "#ffffff", "accent": "#ffe600", "soft": "rgba(255,255,255,.22)"},
    {"bg": "linear-gradient(150deg,#c6ff1a 0%,#00c2a8 100%)", "ink": "#05231f", "accent": "#ff2d95", "soft": "rgba(5,35,31,.16)"},
    {"bg": "linear-gradient(150deg,#ff8a00 0%,#ff2d95 100%)", "ink": "#ffffff", "accent": "#ffe600", "soft": "rgba(255,255,255,.22)"},
    {"bg": "linear-gradient(150deg,#00e0ff 0%,#3a1cff 100%)", "ink": "#ffffff", "accent": "#c6ff1a", "soft": "rgba(255,255,255,.22)"},
    {"bg": "linear-gradient(150deg,#ffd200 0%,#f7531f 100%)", "ink": "#2a1400", "accent": "#7a04eb", "soft": "rgba(42,20,0,.16)"},
    {"bg": "linear-gradient(150deg,#8f00ff 0%,#ff5db1 100%)", "ink": "#ffffff", "accent": "#c6ff1a", "soft": "rgba(255,255,255,.22)"},
    {"bg": "linear-gradient(150deg,#12c2e9 0%,#c471ed 55%,#f64f59 100%)", "ink": "#ffffff", "accent": "#ffe600", "soft": "rgba(255,255,255,.22)"},
    {"bg": "linear-gradient(150deg,#f9f871 0%,#2af598 100%)", "ink": "#062b16", "accent": "#ff2d95", "soft": "rgba(6,43,22,.16)"},
]

# Subtle film grain (SVG turbulence) laid over every card for texture.
_NOISE = (
    "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' "
    "width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' "
    "baseFrequency='0.85' numOctaves='2'/%3E%3C/filter%3E%3Crect width='100%25' "
    "height='100%25' filter='url(%23n)' opacity='0.5'/%3E%3C/svg%3E\")"
)


# ===========================================================================
# Data layer
# ===========================================================================
def list_days(limit: int = 30) -> pd.DataFrame:
    """Days present in the data, busiest (by watch-hours) first.

    Columns: day (str YYYY-MM-DD), watch_hrs (float), peak (int).
    """
    return query_df(
        f"""
        WITH per_min AS (
          SELECT minute, sum(concurrent) AS c FROM {DB}.concurrency_now GROUP BY minute
        )
        SELECT toString(toDate(minute)) AS day,
               round(sum(c) / 60.0, 1)  AS watch_hrs,
               toUInt32(max(c))         AS peak
        FROM per_min
        GROUP BY day
        ORDER BY watch_hrs DESC
        LIMIT {int(limit)}"""
    )


def day_window(day: str) -> dict:
    """{'from','to'} covering the full calendar day `day` (YYYY-MM-DD)."""
    start = datetime.strptime(day, "%Y-%m-%d")
    return {
        "from": start.strftime("%Y-%m-%d 00:00:00"),
        "to": (start + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00"),
    }


def gather(time_filter: dict) -> dict:
    """Everything the story needs for one day/window, from the shared queries."""
    f = {**queries.EMPTY_FILTERS, **time_filter}
    stats = queries.get_stats(f)
    vs = bizmod.get_viewer_stats(f)[0]
    top = queries.get_top_content_leaderboard(time_filter, 5)
    by_hour = queries.get_audience_by_hour(f)
    lv = queries.get_live_vs_vod(time_filter)
    geo = queries.get_geo_distribution(time_filter)
    bd = queries.get_breakdowns(f)
    try:
        ph = bizmod.get_playback_health(f)[0]
        vst = bizmod.get_vst(f)[0]
    except Exception:  # noqa: BLE001 — experience data is a "nice to have" on the poster
        ph, vst = {}, {}

    return {
        "stats": stats,
        "viewer_stats": vs,
        "top": top,
        "by_hour": by_hour,
        "live_vs_vod": lv,
        "geo": geo,
        "breakdowns": bd,
        "playback": ph,
        "vst": vst,
        "has_data": (stats.get("peak_concurrency") or 0) > 0 or not top.empty,
    }


# ===========================================================================
# Theme
# ===========================================================================
def inject_css() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600..800&family=Instrument+Sans:wght@400;500;600&display=swap');

        /* darken the whole page so the gradient cards pop */
        .stApp { background: #08080b; }
        .block-container { padding-top: 2.6rem; max-width: 760px;
                           margin-left: auto; margin-right: auto; }
        header[data-testid="stHeader"] { background: transparent; height: 0; }
        #MainMenu, footer { visibility: hidden; }

        /* brand */
        .wrp-brand { display:flex; align-items:center; gap:10px; margin: 0 2px 16px; }
        .wrp-brand-name { font-family:'Bricolage Grotesque',sans-serif; font-weight:800;
                          font-size:20px; color:#f4f0ff; letter-spacing:-.2px; }
        .wrp-brand-name span { opacity:.55; font-weight:600; }

        /* segmented story progress bars */
        .wrp-bars { display:flex; gap:6px; margin: 0 2px 14px; }
        .wrp-seg { flex:1; height:4px; border-radius:99px; background: rgba(255,255,255,.16); overflow:hidden; }
        .wrp-seg.on { background: rgba(255,255,255,.9); }

        /* the card */
        .wrp-card {
          position: relative; border-radius: 26px; padding: 40px 38px 44px;
          min-height: 60vh; display:flex; flex-direction:column;
          box-shadow: 0 30px 80px rgba(0,0,0,.55), inset 0 1px 0 rgba(255,255,255,.14);
          overflow: hidden; isolation: isolate;
          font-family: 'Instrument Sans', ui-sans-serif, system-ui, sans-serif;
        }
        .wrp-card::after {            /* grain overlay */
          content:""; position:absolute; inset:0; z-index:0; pointer-events:none;
          mix-blend-mode: soft-light; opacity:.5; background-image: %NOISE%;
        }
        .wrp-card > * { position: relative; z-index: 1; }

        .wrp-eyebrow { font-size: 13px; font-weight:600; letter-spacing:.22em;
                       text-transform: uppercase; opacity:.82; }
        /* main content sits in a flex-grow body, vertically centred, so every
           card shares the same rhythm regardless of how much content it holds */
        .wrp-body { flex:1 1 auto; display:flex; flex-direction:column; justify-content:center; }

        /* the giant hero stat */
        .wrp-big { font-family:'Bricolage Grotesque', sans-serif; font-weight:800;
                   font-size: clamp(64px, 15vw, 132px); line-height:.9; letter-spacing:-.02em;
                   margin: 4px 0 2px; }
        .wrp-unit { font-family:'Bricolage Grotesque',sans-serif; font-weight:700;
                    font-size: clamp(20px,4vw,30px); opacity:.9; margin-left:6px; }
        .wrp-head { font-family:'Bricolage Grotesque',sans-serif; font-weight:700;
                    font-size: clamp(26px,5.4vw,42px); line-height:1.02; letter-spacing:-.01em;
                    margin: 10px 0 0; max-width: 15ch; }
        .wrp-sub { font-size: 15px; opacity:.82; margin-top:16px; max-width: 42ch; line-height:1.5; }

        /* cover / poster */
        .wrp-kicker { font-family:'Bricolage Grotesque',sans-serif; font-weight:800;
                      font-size: clamp(40px,9vw,78px); line-height:.94; letter-spacing:-.02em; }
        .wrp-date { font-size:16px; font-weight:600; letter-spacing:.04em; opacity:.9; margin-top:10px; }

        /* staggered fade-up reveal (replays each card because the node is re-rendered) */
        @keyframes wrpUp { from { opacity:0; transform: translateY(16px); } to { opacity:1; transform:none; } }
        .wrp-card .wrp-eyebrow { animation: wrpUp .5s ease both; }
        .wrp-card .wrp-big, .wrp-card .wrp-kicker { animation: wrpUp .6s .06s ease both; }
        .wrp-card .wrp-head { animation: wrpUp .6s .14s ease both; }
        .wrp-card .wrp-viz  { animation: wrpUp .6s .2s ease both; }
        .wrp-card .wrp-sub  { animation: wrpUp .6s .28s ease both; }

        /* leaderboard bars */
        .wrp-row { display:flex; align-items:center; gap:12px; margin:9px 0; }
        .wrp-rank { font-family:'Bricolage Grotesque',sans-serif; font-weight:800; font-size:20px;
                    width:26px; text-align:right; opacity:.65; }
        .wrp-row.top .wrp-rank { opacity:1; font-size:26px; }
        .wrp-barwrap { flex:1; }
        .wrp-title { font-weight:600; font-size:15px; margin-bottom:5px; }
        .wrp-row.top .wrp-title { font-size:19px; font-weight:700; }
        .wrp-track { height:12px; border-radius:99px; overflow:hidden; }
        .wrp-fill { height:100%; border-radius:99px; }
        .wrp-val { font-variant-numeric: tabular-nums; font-weight:600; font-size:13px; opacity:.8; white-space:nowrap; }

        /* prime-time hour bars */
        .wrp-hours { display:flex; align-items:flex-end; gap:3px; height:150px; margin-top:6px; }
        .wrp-hbar { flex:1; border-radius:5px 5px 2px 2px; min-height:3px; }
        .wrp-hlabels { display:flex; justify-content:space-between; font-size:11px; opacity:.7; margin-top:8px;
                       font-variant-numeric: tabular-nums; }

        /* live/vod ring */
        .wrp-ring { width:170px; height:170px; border-radius:50%; display:grid; place-items:center; }
        .wrp-ring-in { width:118px; height:118px; border-radius:50%; display:grid; place-items:center;
                       text-align:center; line-height:1; }
        .wrp-ring-pct { font-family:'Bricolage Grotesque',sans-serif; font-weight:800; font-size:38px; }
        .wrp-ring-lab { font-size:12px; letter-spacing:.14em; text-transform:uppercase; opacity:.85; margin-top:4px; }
        .wrp-legend { display:flex; gap:18px; align-items:center; margin-left:26px; }
        .wrp-leg { display:flex; align-items:center; gap:8px; font-weight:600; }
        .wrp-dot { width:12px; height:12px; border-radius:4px; }

        /* poster stat grid */
        .wrp-grid { display:grid; grid-template-columns:1fr 1fr; gap:14px 26px; margin-top:8px; }
        .wrp-gk { font-size:12px; letter-spacing:.12em; text-transform:uppercase; opacity:.7; }
        .wrp-gv { font-family:'Bricolage Grotesque',sans-serif; font-weight:800; font-size:30px; line-height:1; margin-top:4px; }

        /* nav buttons */
        div[data-testid="stButton"] > button { border-radius: 99px; font-weight:700;
              border:1px solid rgba(255,255,255,.18); background: rgba(255,255,255,.06); color:#fff; }
        div[data-testid="stButton"] > button[kind="primary"] { background:#fff; color:#08080b; border:0; }
        </style>
        """.replace("%NOISE%", _NOISE),
        unsafe_allow_html=True,
    )


# ===========================================================================
# Mini-viz (HTML/CSS/SVG, themed to the card palette)
# ===========================================================================
def _leaderboard(top: pd.DataFrame, pal: dict) -> str:
    if top.empty:
        return ""
    mx = max(1, int(top.iloc[0]["streams"]))
    rows = []
    for i, (_, r) in enumerate(top.iterrows()):
        pct = max(6, round(100 * int(r["streams"]) / mx))
        cls = "wrp-row top" if i == 0 else "wrp-row"
        fill = pal["accent"] if i == 0 else pal["ink"]
        rows.append(
            f'<div class="{cls}"><div class="wrp-rank">{i + 1}</div>'
            f'<div class="wrp-barwrap"><div class="wrp-title">{_esc(r["title"])}</div>'
            f'<div class="wrp-track" style="background:{pal["soft"]}">'
            f'<div class="wrp-fill" style="width:{pct}%;background:{fill}"></div></div></div>'
            f'<div class="wrp-val">{int(r["streams"]):,}</div></div>'
        )
    return f'<div class="wrp-viz" style="margin-top:18px">{"".join(rows)}</div>'


def _hour_bars(by_hour: pd.DataFrame, prime_hour: int, pal: dict) -> str:
    if by_hour.empty:
        return ""
    vals = {int(h): int(v) for h, v in zip(by_hour["hour"], by_hour["avg_viewers"])}
    mx = max([1, *vals.values()])
    bars = []
    for h in range(24):
        v = vals.get(h, 0)
        pct = max(2, round(100 * v / mx))
        col = pal["accent"] if h == prime_hour else pal["soft"]
        bars.append(f'<div class="wrp-hbar" style="height:{pct}%;background:{col}"></div>')
    return (
        f'<div class="wrp-viz" style="margin-top:18px">'
        f'<div class="wrp-hours">{"".join(bars)}</div>'
        f'<div class="wrp-hlabels"><span>12a</span><span>6a</span><span>12p</span>'
        f"<span>6p</span><span>11p</span></div></div>"
    )


def _ratio_ring(vod_pct: int, live_pct: int, pal: dict) -> str:
    dominant_pct, dominant_lab = (
        (vod_pct, "On-demand") if vod_pct >= live_pct else (live_pct, "Live")
    )
    ring_bg = f"conic-gradient({pal['accent']} 0 {vod_pct}%, {pal['soft']} 0)"
    return (
        f'<div class="wrp-viz" style="display:flex;align-items:center;margin-top:20px">'
        f'<div class="wrp-ring" style="background:{ring_bg}">'
        f'<div class="wrp-ring-in" style="background:{pal["bg"]}">'
        f'<div><div class="wrp-ring-pct" style="color:{pal["accent"]}">{dominant_pct}%</div>'
        f'<div class="wrp-ring-lab">{dominant_lab}</div></div></div></div>'
        f'<div class="wrp-legend">'
        f'<div class="wrp-leg"><span class="wrp-dot" style="background:{pal["accent"]}"></span>On-demand {vod_pct}%</div>'
        f'<div class="wrp-leg"><span class="wrp-dot" style="background:{pal["soft"]}"></span>Live {live_pct}%</div>'
        f"</div></div>"
    )


# ===========================================================================
# Card assembly
# ===========================================================================
def _esc(s) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _shell(pal: dict, inner: str, min_h: str = "60vh") -> str:
    return (
        f'<div class="wrp-card" style="background:{pal["bg"]};color:{pal["ink"]};min-height:{min_h}">'
        f"{inner}</div>"
    )


def _card(pal: dict, eyebrow: str, body: str, min_h: str = "60vh") -> str:
    """Standard card: eyebrow pinned top, `body` vertically centred beneath it."""
    inner = f'<div class="wrp-eyebrow">{eyebrow}</div><div class="wrp-body">{body}</div>'
    return _shell(pal, inner, min_h)


def _stat_card(pal, eyebrow, big, unit, head, sub, viz="") -> str:
    unit_html = f'<span class="wrp-unit">{unit}</span>' if unit else ""
    sub_html = f'<div class="wrp-sub">{sub}</div>' if sub else ""
    body = (
        f'<div class="wrp-big">{big}{unit_html}</div>'
        f'<div class="wrp-head">{head}</div>{viz}{sub_html}'
    )
    return _card(pal, eyebrow, body)


def _time_of_day(h: int) -> str:
    return (
        "the small hours" if h < 5 else "the morning" if h < 12
        else "the afternoon" if h < 17 else "prime-time evening" if h < 22 else "late night"
    )


def build_cards(day: str, d: dict) -> list[str]:
    """Return the list of card HTML strings for the day `d` payload."""
    P = PALETTES
    pretty = datetime.strptime(day, "%Y-%m-%d").strftime("%A · %B %-d, %Y")
    from ui import human

    if not d["has_data"]:
        return [
            _card(
                P[3],
                "SonyLIV · Daily Wrapped",
                f'<div class="wrp-kicker">A quiet day.</div>'
                f'<div class="wrp-sub">No viewing was recorded on {pretty}. '
                f"Pick a busier day above to see its Wrapped.</div>",
            )
        ]

    s, vs = d["stats"], d["viewer_stats"]
    peak = int(s.get("peak_concurrency") or 0)
    peak_min = str(s.get("peak_minute") or "")[11:16] or "—"
    watch = vs.get("viewer_hours") or 0
    cards: list[str] = []

    # 0 — Cover
    cards.append(
        _card(
            P[0],
            "SonyLIV · Daily Wrapped",
            f'<div class="wrp-kicker">How the<br>country<br>watched.</div>'
            f'<div class="wrp-date">{pretty}</div>'
            f'<div class="wrp-sub">Your day on SonyLIV, one story at a time. Hit Next →</div>',
        )
    )

    # 1 — Total watch time
    cards.append(
        _stat_card(
            P[1], "Total time watched",
            human(watch), "hrs",
            "That's how long the audience pressed play today.",
            f"About {human((watch or 0) / 24)} days of non-stop streaming, rolled into one day.",
        )
    )

    # 2 — Peak moment
    cards.append(
        _stat_card(
            P[2], "The busiest moment",
            human(peak), "watching at once",
            f"At {peak_min}, everyone showed up together.",
            "The single minute the most people were streaming side by side.",
        )
    )

    # 3 — Top titles
    top = d["top"]
    if not top.empty:
        no1 = _esc(top.iloc[0]["title"])
        cards.append(
            _card(
                P[3],
                "Today's top titles",
                f'<div class="wrp-head">The one nobody could stop watching: '
                f'<span style="color:{P[3]["accent"]}">{no1}</span></div>'
                f'{_leaderboard(top, P[3])}'
                f'<div class="wrp-sub">Your top 5 by peak audience.</div>',
            )
        )

    # 4 — Prime time
    by_hour = d["by_hour"]
    if not by_hour.empty:
        ph = int(by_hour.loc[by_hour["avg_viewers"].idxmax(), "hour"])
        cards.append(
            _card(
                P[4],
                "Prime time",
                f'<div class="wrp-big" style="font-size:clamp(52px,12vw,104px)">{ph:02d}<span class="wrp-unit">:00</span></div>'
                f'<div class="wrp-head">The audience peaked in {_time_of_day(ph)}.</div>'
                f'{_hour_bars(by_hour, ph, P[4])}'
                f'<div class="wrp-sub">Average viewers by hour of day — the taller the bar, the bigger the crowd.</div>',
            )
        )

    # 5 — Live vs VOD
    lv = d["live_vs_vod"]
    if not lv.empty:
        tot = int(lv["streams"].sum()) or 1
        vod = int(lv.loc[lv["video_type"].str.lower() == "vod", "streams"].sum())
        vod_pct = round(100 * vod / tot)
        live_pct = 100 - vod_pct
        mood = (
            "On-demand ruled the day." if vod_pct >= 65
            else "Live had the edge." if vod_pct <= 35
            else "A tug-of-war between live and on-demand."
        )
        cards.append(
            _card(
                P[5],
                "Live vs on-demand",
                f'<div class="wrp-head">{mood}</div>'
                f"{_ratio_ring(vod_pct, live_pct, P[5])}"
                f'<div class="wrp-sub">How today\'s viewing split between live streams and the on-demand library.</div>',
            )
        )

    # 6 — Geography
    geo = d["geo"]
    if not geo.empty:
        n = len(geo)
        top_country = _esc(str(geo.iloc[0]["country"]).title())
        cards.append(
            _stat_card(
                P[6], "Where they watched",
                human(n), "countr" + ("y" if n == 1 else "ies"),
                f"{top_country} led the way.",
                "Viewers tuned in from across the map — this is where your audience lives.",
            )
        )

    # 7 — Top genre
    bd = d["breakdowns"]
    cat = bd[bd["dimension"] == "category"].sort_values("peak", ascending=False) if not bd.empty else bd
    if cat is not None and not cat.empty:
        genre = _esc(str(cat.iloc[0]["name"]).title())
        cards.append(
            _card(
                P[7],
                "Today's mood",
                f'<div class="wrp-kicker" style="color:{P[7]["accent"]}">{genre}</div>'
                f'<div class="wrp-head" style="margin-top:12px">The genre that owned the day.</div>'
                f'<div class="wrp-sub">The category with the biggest live audience.</div>',
            )
        )

    # 8 — Poster recap
    cards.append(_poster(day, d, human))
    return cards


def _poster(day: str, d: dict, human) -> str:
    pal = {"bg": "linear-gradient(160deg,#141018 0%,#241033 60%,#3a0f5e 100%)",
           "ink": "#f4f0ff", "accent": "#ffe600", "soft": "rgba(255,255,255,.14)"}
    s, vs = d["stats"], d["viewer_stats"]
    pretty = datetime.strptime(day, "%Y-%m-%d").strftime("%A · %B %-d, %Y")
    peak = int(s.get("peak_concurrency") or 0)
    watch = vs.get("viewer_hours") or 0
    top = d["top"]
    no1 = _esc(top.iloc[0]["title"]) if not top.empty else "—"
    by_hour = d["by_hour"]
    prime = f"{int(by_hour.loc[by_hour['avg_viewers'].idxmax(),'hour']):02d}:00" if not by_hour.empty else "—"
    geo = d["geo"]
    country = _esc(str(geo.iloc[0]["country"]).title()) if not geo.empty else "—"

    def cell(k, v):
        return f'<div><div class="wrp-gk">{k}</div><div class="wrp-gv" style="color:{pal["accent"]}">{v}</div></div>'

    grid = (
        cell("Watch time", f"{human(watch)} hrs")
        + cell("Peak viewers", human(peak))
        + cell("Prime time", prime)
        + cell("Top country", country)
    )
    body = (
        f'<div class="wrp-date">{pretty}</div>'
        f'<div class="wrp-head" style="margin-top:16px;font-size:clamp(24px,5vw,36px)">'
        f'The day in one card.</div>'
        f'<div class="wrp-viz wrp-grid">{grid}</div>'
        f'<div style="margin-top:22px;font-size:15px;opacity:.9">Most-watched title<br>'
        f'<span style="font-family:\'Bricolage Grotesque\',sans-serif;font-weight:800;'
        f'font-size:28px;color:{pal["accent"]}">{no1}</span></div>'
        f'<div class="wrp-sub">That\'s a wrap. 🎬</div>'
    )
    return _card(pal, "SonyLIV · Daily Wrapped", body, min_h="62vh")


# ===========================================================================
# Stepper / render
# ===========================================================================
def _go(delta: int, n: int) -> None:
    st.session_state.wrap_step = max(0, min(n - 1, st.session_state.get("wrap_step", 0) + delta))


def render() -> None:
    inject_css()

    # Brand
    st.markdown(
        f'<div class="wrp-brand">{ui.CH_LOGO}'
        f'<div class="wrp-brand-name">SonyLIV <span>· Daily Wrapped</span></div></div>',
        unsafe_allow_html=True,
    )

    try:
        days = list_days()
    except Exception as e:  # noqa: BLE001
        st.error(f"Couldn't load days: {e}")
        return
    if days.empty:
        st.info("No viewing data yet.")
        return

    # Day picker — default to the busiest (row 0).
    def _day_label(i: int) -> str:
        r = days.iloc[i]
        pretty = datetime.strptime(r["day"], "%Y-%m-%d").strftime("%a, %b %-d")
        return f"{pretty}  ·  {ui.human(r['watch_hrs'])} watch-hrs"

    pcol, _sp = st.columns([1.6, 1.4])
    day_idx = pcol.selectbox(
        "Choose a day", range(len(days)), format_func=_day_label, key="wrap_day_idx"
    )
    day = days.iloc[day_idx]["day"]

    # Reset the story to card 0 whenever the chosen day changes.
    if st.session_state.get("_wrap_day") != day:
        st.session_state._wrap_day = day
        st.session_state.wrap_step = 0

    try:
        payload = gather(day_window(day))
        cards = build_cards(day, payload)
    except Exception as e:  # noqa: BLE001
        st.error(f"Couldn't build today's Wrapped: {e}")
        return

    n = len(cards)
    step = max(0, min(n - 1, st.session_state.get("wrap_step", 0)))

    # progress segments
    segs = "".join(f'<span class="wrp-seg {"on" if i <= step else ""}"></span>' for i in range(n))
    st.markdown(f'<div class="wrp-bars">{segs}</div>', unsafe_allow_html=True)

    # the card
    st.markdown(cards[step], unsafe_allow_html=True)
    st.write("")

    # nav — Back (left) · counter (centered) · Next (right), vertically aligned
    b, mid, nx = st.columns([1, 1.4, 1], vertical_alignment="center")
    b.button("← Back", disabled=step == 0, on_click=_go, args=(-1, n), width="stretch")
    mid.markdown(
        f'<div style="text-align:center;color:#8a8a99;font-size:13px;'
        f'font-variant-numeric:tabular-nums">{step + 1} / {n}</div>',
        unsafe_allow_html=True,
    )
    if step < n - 1:
        nx.button("Next →", type="primary", on_click=_go, args=(1, n), width="stretch")
    else:
        nx.button("↺ Replay", on_click=_go, args=(-n, n), width="stretch")
