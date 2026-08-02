"""Static configuration for the SonyLIV concurrency dashboard.

Single place for the database name, refresh cadence, and the theme palette.
The palette takes inspiration from ClickHouse's brand: signature bright yellow
(#FAFF69) on a near-black canvas, with an amber secondary accent.
"""

from __future__ import annotations

DB = "sonyliv_concurrency"

# Auto-refresh cadence for still-open ("live") sessions.
REFRESH_MS = 30_000

# Grafana-style refresh-interval picker: label → interval in ms (None = off).
# The default selection is "30s" (REFRESH_MS).
REFRESH_INTERVALS: dict[str, int | None] = {
    "Off": None,
    "5s": 5_000,
    "10s": 10_000,
    "30s": 30_000,
    "1m": 60_000,
    "5m": 300_000,
}

# Grafana-style quick time-range picker: label → seconds back from now()
# ("Custom" → None reveals the absolute date/time inputs instead). Ranges are
# anchored to wall-clock now(); the producer stamps events with now, so live
# data lines up (older/historical browsing uses "Custom").
QUICK_RANGES: dict[str, int | None] = {
    "Last 5 minutes": 300,
    "Last 15 minutes": 900,
    "Last 30 minutes": 1_800,
    "Last 1 hour": 3_600,
    "Last 3 hours": 10_800,
    "Last 6 hours": 21_600,
    "Last 12 hours": 43_200,
    "Last 24 hours": 86_400,
    "Last 7 days": 604_800,
    "Custom": None,
}

# --- ClickHouse-inspired palette ---------------------------------------------
BG = "#161619"  # near-black canvas
PANEL = "#1f1f24"  # cards / panels
PANEL_2 = "#26262d"  # gradient bottom / inputs
BORDER = "#33333d"
TEXT = "#f4f4f2"  # warm white
MUTED = "#a0a0ab"
ACCENT = "#faff69"  # ClickHouse yellow — primary (peak, chart, live)
ACCENT_2 = "#ffb454"  # amber — secondary (current)
DANGER = "#ff6b6b"

# Realtime-ticker delta colours (stock-style): green up / red down.
UP = "#3fb950"  # increase
DOWN = DANGER  # decrease (reuse the danger red)
UP_FILL = "rgba(63, 185, 80, 0.16)"  # green sparkline / pill tint
DOWN_FILL = "rgba(255, 107, 107, 0.16)"  # red sparkline / pill tint

# Yellow at low opacity for the chart's area fill.
ACCENT_FILL = "rgba(250, 255, 105, 0.14)"
