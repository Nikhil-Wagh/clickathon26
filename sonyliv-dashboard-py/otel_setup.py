"""OpenTelemetry bootstrap for the Streamlit dashboard.

Design goals specific to this app:

* **Idempotent.** Streamlit re-executes the whole script top-to-bottom on every
  interaction and auto-refresh, all within one process (ScriptRunner threads).
  `init_otel()` is guarded so the providers/instruments are built exactly once
  per process, no matter how many reruns call it.
* **Safe before install.** If the `opentelemetry-*` packages aren't installed
  (or `OTEL_DISABLED=1`), every accessor returns a no-op so the dashboard runs
  unchanged. Nothing here can crash the app.
* **No hard Streamlit dependency.** Uses a plain module-level guard + lock, so
  it can also be imported by worker threads that hold their own ClickHouse
  client (see clickhouse_client.py's threading.local pattern).

Exporter selection (mirrors producer/otel_setup.py for consistency):
    OTEL_CONSOLE=1                  → print spans + metrics to stderr (verify first)
    OTEL_EXPORTER_OTLP_ENDPOINT=…   → OTLP/HTTP to a collector (default localhost:4318)
    OTEL_DISABLED=1                 → force no-op
"""

from __future__ import annotations

import atexit
import os
import threading

# --- No-op fallbacks (used when OTel isn't installed or is disabled) ----------


class _NoopSpan:
    def set_attribute(self, *_a, **_k) -> None: ...
    def record_exception(self, *_a, **_k) -> None: ...
    def set_status(self, *_a, **_k) -> None: ...


class _NoopCtx:
    def __enter__(self) -> _NoopSpan:
        return _NoopSpan()

    def __exit__(self, *_a) -> bool:
        return False


class _NoopTracer:
    def start_as_current_span(self, *_a, **_k) -> _NoopCtx:
        return _NoopCtx()


class _NoopInstrument:
    def add(self, *_a, **_k) -> None: ...
    def record(self, *_a, **_k) -> None: ...


_NOOP_TRACER = _NoopTracer()
_NOOP_INSTRUMENT = _NoopInstrument()


def _load_env() -> None:
    """Load the app's .env NOW, before exporters are built.

    The dashboard otherwise loads .env lazily (clickhouse_client.get_client),
    which happens *after* OTel init — too late for the OTLP exporter to pick up
    OTEL_EXPORTER_OTLP_* vars placed there.
    """
    try:
        from pathlib import Path
        from dotenv import load_dotenv

        here = Path(__file__).resolve().parent
        for cand in (here / ".env", here.parent / "producer" / ".env"):
            if cand.is_file():
                load_dotenv(cand)
    except Exception:  # noqa: BLE001  (telemetry must never break the app)
        pass


def _parse_headers(raw: str) -> dict:
    """Parse OTEL_EXPORTER_OTLP_*_HEADERS ('k=v,k2=v2') into a dict."""
    out: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out

# --- State -------------------------------------------------------------------

_lock = threading.Lock()
_initialized = False
_enabled = os.getenv("OTEL_DISABLED", "").lower() not in ("1", "true", "yes")

_tracer = _NOOP_TRACER
_db = (_NOOP_INSTRUMENT, _NOOP_INSTRUMENT, _NOOP_INSTRUMENT)   # duration, count, errors
_llm = (_NOOP_INSTRUMENT, _NOOP_INSTRUMENT, _NOOP_INSTRUMENT)  # duration, count, errors
_app_runs = _NOOP_INSTRUMENT
_status_cls = None  # opentelemetry Status/StatusCode, set when real SDK loads


def init_otel() -> None:
    """Set up trace + metric providers once per process. Idempotent and safe."""
    global _initialized, _enabled, _tracer, _db, _llm, _app_runs, _status_cls
    if _initialized or not _enabled:
        return
    with _lock:
        if _initialized:
            return
        _initialized = True  # set first: never retry a failed/absent setup per process
        try:
            from opentelemetry import metrics, trace
            from opentelemetry.trace import Status, StatusCode
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import (
                BatchSpanProcessor,
                ConsoleSpanExporter,
            )
            from opentelemetry.sdk.trace.sampling import (
                ParentBased,
                TraceIdRatioBased,
            )
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import (
                ConsoleMetricExporter,
                PeriodicExportingMetricReader,
            )
        except ImportError:
            _enabled = False
            return

        _load_env()  # make OTEL_EXPORTER_OTLP_* from .env visible before export

        console = os.getenv("OTEL_CONSOLE", "").lower() in ("1", "true", "yes")

        def _endpoint(signal: str) -> str | None:
            # Signal-specific var wins over the generic base, per the OTel spec.
            return os.getenv(f"OTEL_EXPORTER_OTLP_{signal}_ENDPOINT") or os.getenv(
                "OTEL_EXPORTER_OTLP_ENDPOINT"
            )

        traces_ep = _endpoint("TRACES")
        metrics_ep = _endpoint("METRICS")

        if not console and not traces_ep and not metrics_ep:
            # No destination and not console mode: stay no-op. This is what
            # prevents the localhost:4318 "Connection refused" export spam when
            # nothing is configured.
            _enabled = False
            return

        interval = int(os.getenv("OTEL_METRIC_EXPORT_INTERVAL_MS", "10000"))
        resource = Resource.create(
            {"service.name": os.getenv("OTEL_SERVICE_NAME", "sonyliv-dashboard")}
        )
        ratio = float(os.getenv("OTEL_TRACES_SAMPLER_RATIO", "1.0"))
        tp = TracerProvider(
            resource=resource, sampler=ParentBased(TraceIdRatioBased(ratio))
        )

        # ---- Traces --------------------------------------------------------
        if console:
            tp.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        elif traces_ep:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            # Reads OTEL_EXPORTER_OTLP_TRACES_ENDPOINT/_HEADERS/_PROTOCOL (or the
            # generic OTEL_EXPORTER_OTLP_* fallbacks) from the environment.
            tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(tp)

        # ---- Metrics -------------------------------------------------------
        # Only export metrics when a metrics endpoint is configured, or when
        # OTEL_EXPORT_METRICS is set (then derive from the traces endpoint +
        # headers). Otherwise no reader is attached — instruments still work,
        # nothing is exported, and there's no failing-export noise.
        readers = []
        if console:
            readers.append(
                PeriodicExportingMetricReader(
                    ConsoleMetricExporter(), export_interval_millis=interval
                )
            )
        else:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                OTLPMetricExporter,
            )

            derive = os.getenv("OTEL_EXPORT_METRICS", "").lower() in (
                "1",
                "true",
                "yes",
            )
            metric_exp = None
            if metrics_ep:
                metric_exp = OTLPMetricExporter()  # reads METRICS/generic env
            elif derive and traces_ep:
                endpoint = traces_ep
                if endpoint.endswith("/v1/traces"):
                    endpoint = endpoint[: -len("/v1/traces")] + "/v1/metrics"
                headers = _parse_headers(
                    os.getenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS")
                    or os.getenv("OTEL_EXPORTER_OTLP_HEADERS")
                    or ""
                )
                metric_exp = OTLPMetricExporter(
                    endpoint=endpoint, headers=headers or None
                )
            if metric_exp is not None:
                readers.append(
                    PeriodicExportingMetricReader(
                        metric_exp, export_interval_millis=interval
                    )
                )

        mp = MeterProvider(resource=resource, metric_readers=readers)
        metrics.set_meter_provider(mp)

        _tracer = trace.get_tracer("sonyliv-dashboard")
        meter = metrics.get_meter("sonyliv-dashboard")

        _db = (
            meter.create_histogram("db.client.query.duration", unit="ms"),
            meter.create_counter("db.client.query.count", unit="{query}"),
            meter.create_counter("db.client.query.errors", unit="{error}"),
        )
        _llm = (
            meter.create_histogram("gen_ai.client.operation.duration", unit="ms"),
            meter.create_counter("gen_ai.client.operation.count", unit="{call}"),
            meter.create_counter("gen_ai.client.operation.errors", unit="{error}"),
        )
        _app_runs = meter.create_counter("app.script.runs", unit="{run}")
        _status_cls = (Status, StatusCode)

        def _shutdown() -> None:
            tp.shutdown()
            mp.shutdown()

        atexit.register(_shutdown)


# --- Accessors (always safe; lazily ensure init) -----------------------------


def get_tracer():
    if not _initialized:
        init_otel()
    return _tracer


def db_instruments():
    if not _initialized:
        init_otel()
    return _db


def llm_instruments():
    if not _initialized:
        init_otel()
    return _llm


def app_runs():
    if not _initialized:
        init_otel()
    return _app_runs


def mark_error(span, exc: BaseException) -> None:
    """Record an exception + set ERROR status on a span (no-op if OTel absent)."""
    span.record_exception(exc)
    if _status_cls is not None:
        Status, StatusCode = _status_cls
        span.set_status(Status(StatusCode.ERROR, str(exc)))
