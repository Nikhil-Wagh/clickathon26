"""SonyLIV — Daily Wrapped (Streamlit).

A Spotify-Wrapped-style story of a single day's viewing on SonyLIV: vivid gradient
cards you step through, one big insight each, ending in a shareable recap poster.

All data comes from ClickHouse via queries.py / business.py; the story UI lives in
wrapped.py. Run:
    pip install -r requirements.txt
    streamlit run app.py
"""

from __future__ import annotations

import guardrails  # noqa: F401  (must run first — strips any unreachable proxy env)

import streamlit as st

import otel_setup
import wrapped

# Build OTel providers once per process (idempotent across Streamlit reruns).
otel_setup.init_otel()
otel_setup.app_runs().add(1)

st.set_page_config(page_title="SonyLIV — Daily Wrapped", layout="wide")


def main() -> None:
    try:
        from clickhouse_client import get_client

        get_client()
    except Exception as e:  # noqa: BLE001
        st.error(f"Couldn't connect to the data source: {e}")
        st.stop()

    wrapped.render()


if __name__ == "__main__":
    main()
