"""ClickHouse Cloud connection for the dashboard.

Credentials are read from a `.env` file using the SAME keys as the producer
(`producer/produce_events.py`). To avoid duplicating secrets, the loader looks
for, in order:

    1. sonyliv-dashboard-py/.env          (this app's own env, if you made one)
    2. ../producer/.env                    (reuse the producer's working creds)

so `cp` of credentials is never required — point the app at the producer's
existing `.env` and it just works.

THREADING: one client PER OS THREAD, not one shared client. `clickhouse_connect`
`Client` instances are not safe to use concurrently from multiple threads — a
second thread issuing a query while another is still in flight on the same
client raises "Attempt to execute concurrent queries within the same session."
Streamlit can run script executions from different sessions/reruns on
different threads at the same time (e.g. auto-refresh firing a rerun while a
previous run is still waiting on a slow ClickHouse Cloud cold-start), so a
single `@st.cache_resource`-cached client — shared process-wide by design —
is exactly the unsafe pattern the library warns about. `threading.local()`
gives each thread its own client (created once, reused across reruns on that
same thread), which keeps the "cheap to hold open" benefit without the
cross-thread race.
"""

from __future__ import annotations

import guardrails  # noqa: F401  (must run first — strips Bloomberg proxy env vars)

import os
import threading
import time
from pathlib import Path

import clickhouse_connect
import pandas as pd
from clickhouse_connect.driver.client import Client
from dotenv import load_dotenv

import otel_setup

_HERE = Path(__file__).resolve().parent
_ENV_CANDIDATES = [_HERE / ".env", _HERE.parent / "producer" / ".env"]

_local = threading.local()


def _load_env() -> Path | None:
    """Load the first `.env` that exists; return which one (for the UI)."""
    for candidate in _ENV_CANDIDATES:
        if candidate.is_file():
            load_dotenv(candidate)
            return candidate
    return None


def get_client() -> tuple[Client, str]:
    """Return a live ClickHouse client and the host it connected to.

    One client per OS thread (see module docstring) — reused across calls on
    the same thread, created fresh the first time a new thread calls this.
    Raises RuntimeError with an actionable message if no env / host is found.
    """
    cached = getattr(_local, "client_host", None)
    if cached is not None:
        return cached

    env_path = _load_env()
    host = os.getenv("CLICKHOUSE_HOST")
    if not host:
        looked = "\n  - ".join(str(p) for p in _ENV_CANDIDATES)
        raise RuntimeError(
            "CLICKHOUSE_HOST is not set. Create a .env (see .env.example) or "
            f"reuse the producer's. Looked for:\n  - {looked}"
        )
    client = clickhouse_connect.get_client(
        host=host,
        port=int(os.getenv("CLICKHOUSE_PORT", "8443")),
        username=os.getenv("CLICKHOUSE_USER", "default"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        secure=os.getenv("CLICKHOUSE_SECURE", "true").lower() in ("1", "true", "yes"),
        connect_timeout=15,
        send_receive_timeout=30,
        # Keep dashboard queries snappy and bounded (mirrors the React app).
        settings={"max_execution_time": 30},
    )
    _ = env_path  # loaded above; kept for clarity
    _local.client_host = (client, host)
    return client, host


def query_df(sql: str, params: dict | None = None) -> pd.DataFrame:
    """Run a parameterized query and return a pandas DataFrame.

    Uses ClickHouse server-side named params ({name:Type}) — same convention as
    the original `lib/queries.ts`.

    Instrumented with OpenTelemetry: one CLIENT span per query plus duration /
    count / error metrics. A no-op when OTel isn't installed (see otel_setup).
    """
    client, host = get_client()
    tracer = otel_setup.get_tracer()
    duration, count, errors = otel_setup.db_instruments()

    t0 = time.perf_counter()
    with tracer.start_as_current_span("clickhouse.query_df") as span:
        span.set_attribute("db.system", "clickhouse")
        span.set_attribute("server.address", host)
        span.set_attribute("db.query.text", sql[:300])
        try:
            df = client.query_df(sql, parameters=params or {})
        except Exception as e:  # noqa: BLE001
            errors.add(1)
            otel_setup.mark_error(span, e)
            raise
        finally:
            count.add(1)
            duration.record((time.perf_counter() - t0) * 1000.0)
        span.set_attribute("db.response.returned_rows", int(len(df)))
        return df
