# 🛠️ Setting up Snorlax locally

A step-by-step guide to getting Snorlax running against your own ClickHouse Cloud
service — from an empty checkout to a verified concurrency serving layer. For the
one-paragraph version, see the [Runbook in the README](README.md#-runbook--running-it-locally).

> ℹ️ **Where things run.** Nothing runs *inside* ClickHouse on your laptop —
> Snorlax connects to a **ClickHouse Cloud** service (or any reachable ClickHouse
> instance). Your machine only runs Python: the schema runner, the event
> producer, and the benchmark.

---

## 0. Prerequisites

| Need | Why | Notes |
|---|---|---|
| 🐍 **Python 3.9+** | schema runner, producer, benchmark | one virtualenv works for all three |
| ☁️ **ClickHouse Cloud service** | the analytical engine + serving layer | [clickhouse.com/cloud](https://clickhouse.com/cloud) — spin one up with your event credits |
| 🔑 **Service credentials** | host / port / user / password / database | from the Cloud console → *Connect* |
| 📦 **The SQL schema** (`schema/*.sql`) | ⚠️ **the core solution** — see note below | required for anything to run |
| 📼 *(live path only)* **Redpanda + ClickPipes** | streaming ingestion | optional — the offline path skips this |
| 📊 *(datasets)* the two hackathon CSVs | real data to load | `ch-hackathon-raw-data.csv`, `ch-hackathon-content-data.csv` — download into `problem/data/` |

> ⚠️ **Heads-up on `schema/`.** The pipeline SQL (`00_config.sql`, `01_schema.sql`,
> `02_seed.sql`, `03_backfill.sql`, `04_approaches.sql`, `05_compare.sql`,
> `06_verify.sql`, `ui_queries.sql`) is what `run_sql.py` and `benchmark.py`
> expect in a top-level `schema/` directory. If your checkout doesn't have it
> yet, that SQL is the first thing to add — everything below assumes it's present.
> `docs/SCHEMA.md` and `docs/DATA_FLOW.md` document exactly what each file defines.

---

## 1. Clone & enter the repo

```bash
git clone <your-repo-url> snorlax
cd snorlax
```

## 2. Configure credentials (once)

The producer **and** the schema runner both read `producer/.env` — so you only
fill this in once.

```bash
cd producer
cp .env.example .env
```

Edit `producer/.env`:

```dotenv
# --- ClickHouse Cloud connection (required) ---
CLICKHOUSE_HOST=<your-service>.clickhouse.cloud
CLICKHOUSE_PORT=8443
CLICKHOUSE_USER=default
CLICKHOUSE_PASSWORD=<your-password>
CLICKHOUSE_DATABASE=sonyliv_concurrency
CLICKHOUSE_SECURE=true

# --- Producer throughput (optional; sensible defaults exist) ---
# EVENTS_PER_SECOND=0        # 0 = unthrottled, per-worker target rate
# PRODUCER_THREADS=8         # worker threads per process
# PRODUCER_PROCESSES=1       # OS processes (× threads = total workers)
```

> 🔐 `.env` is gitignored (`*.env` except `.env.example`) — your secrets stay local.

## 3. Set up the Python environment

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt    # clickhouse-connect + python-dotenv
```

Reuse this same virtualenv for `migrations/` and `benchmark/` — they share the
same two dependencies.

## 4. Build the schema

```bash
cd ../migrations
python run_sql.py --reset --build      # 💥 drop everything + recreate structure fresh
```

- `--reset --build` — clean slate: drops every object, then creates the database,
  config UDFs, tables, dictionary, `concurrency_now` view, and all materialized views.
- `--build` alone — idempotent recreate (safe on an existing service).
- `--migrate` — apply only the numbered `migrations/NNN_*.sql` on top of a live deployment.

See [`migrations/README.md`](migrations/README.md) for the full command table and
the migration conventions.

---

## 5. Choose a path: offline or live

### 🅰️ Offline path (simplest — recommended first run)

Runs the whole pipeline as a one-shot batch build, no streaming infra needed.

```bash
cd ../migrations
python run_sql.py --all
```

`--all` runs `00_config → 01_schema → 02_seed → 03_backfill → 04_approaches →
05_compare → 06_verify` in order, sharing one session. By default `02_seed.sql`
inserts a handful of synthetic `now()`-relative sessions so `concurrency_now`
shows a curve immediately.

**To load the real hackathon data instead:** place the CSVs in `problem/data/`,
then enable Section B (the `FROM INFILE` bulk load) in `schema/02_seed.sql` before
running `--all`. See `docs/DATA_FLOW.md` §4 for what each stage does.

### 🅱️ Live path (streaming, near-real-time)

Simulates a live event: events flow continuously and the concurrency curve builds
in real time.

1. **Stand up Redpanda + ClickPipes** — point a ClickPipes Kafka source at your
   Redpanda topic, landing into `events_incoming` (the `mv_incoming_to_raw` MV
   fans it out to `events_raw` automatically). *(Connector config is
   environment-specific; capture yours alongside your deployment.)*
2. **Start the producer:**
   ```bash
   cd ../producer
   source .venv/bin/activate
   python produce_events.py
   ```
   Crank volume with env vars — e.g. `EVENTS_PER_SECOND=0 PRODUCER_THREADS=16
   PRODUCER_PROCESSES=4 python produce_events.py`. Stop with `Ctrl-C` (workers
   flush first).

   > 📝 `produce_events.py` generates *synthetic* events that exercise the tricky
   > cases (pauses, ad breaks, backgrounding, abandoned sessions, late arrivals,
   > marathon sessions). To replay the actual hackathon CSV instead, use the
   > offline CSV load in step 5🅰️.

3. **Wait a cycle** — the live MVs refresh every 30s (hot) / 60s (cold compaction),
   so give it ~1 minute to catch up.

---

## 6. See it working

- 📈 **Dashboard:** run the Streamlit app locally — it reuses `producer/.env`
  automatically, so no extra config:
  ```bash
  cd ../sonyliv-dashboard-py
  python3 -m venv .venv && source .venv/bin/activate
  pip install -r requirements.txt
  streamlit run app.py          # http://localhost:8501
  ```
  Or just open the [hosted demo](https://snorlax.streamlit.app/). See
  [`sonyliv-dashboard-py/README.md`](sonyliv-dashboard-py/README.md) for the four
  panes, the Insights Copilot, and OTel/LibreChat wiring.
- 🔎 **Direct query:** poke the serving layer yourself —
  ```bash
  cd migrations
  python run_sql.py -c "SELECT max(concurrent) FROM sonyliv_concurrency.concurrency_now"
  ```
  See [`benchmark/BENCHMARK_QUERIES.md`](benchmark/BENCHMARK_QUERIES.md) for the
  peak/average/per-dimension query shapes.
- 🛰️ **Observability:** the ClickStack/HyperDX dashboards
  ([links in the README](README.md#-see-it-live)) show ingestion lag, data quality,
  and query performance.

## 7. Verify correctness

```bash
cd ../benchmark
source ../producer/.venv/bin/activate      # or a fresh venv
pip install -r requirements.txt
python benchmark.py
```

`benchmark.py` computes every benchmark answer **twice** — once from the serving
layer, once reconstructed independently from raw events — and asserts they match.

- **Exit code = number of failed checks** (`0` = serving layer matches ground
  truth to the row). Plugs straight into CI or a pre-submission gate.
- On **live** data, add `--grace-seconds N` to skip the still-provisional hot edge.
- `--since <ISO> --until <ISO>` restricts the compared window; `--no-extended`
  skips the drill-down checks (B9/B10).

---

## 🧹 Resetting between runs

```bash
cd migrations
python run_sql.py --reset --build
```

Drops every Snorlax object and rebuilds structure — do this before a fresh
sealed-dataset ("unseen day") replay so nothing leaks between runs.

## 🩹 Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `run_sql.py` can't connect | Check `producer/.env`; confirm the Cloud service is running and your IP is allowed. |
| `schema/…​.sql not found` | The `schema/` directory is missing — see the ⚠️ note in [Prerequisites](#0-prerequisites). |
| `concurrency_now` stays empty | Give the `REFRESH EVERY 30 SECOND` MVs a cycle; or force one: `run_sql.py -c "SYSTEM REFRESH VIEW mv_session_intervals"`. |
| `benchmark.py` reports mismatches on live data | Add `--grace-seconds` — you're comparing against the provisional hot edge. |
| `FROM INFILE` load fails | Confirm the CSVs are in `problem/data/` and Section B of `02_seed.sql` is enabled. |

---

## 🔌 Optional: the integration layers

These are separate services, not required to run the core pipeline — the
dashboard runs fine without them:

- ✨ **Insights Copilot (LibreChat + Ollama + MCP)** — the in-app chat panel routes
  through a local **LibreChat** (Docker), which runs the turn on a local **Ollama**
  model and hands it **[ClickHouse MCP](https://github.com/ClickHouse/mcp-clickhouse)**
  + ClickStack MCP tools. Full walkthrough in
  [`sonyliv-dashboard-py/README.md`](sonyliv-dashboard-py/README.md#connect-to-librechat).
  If unconfigured, the panel falls back to calling Ollama directly (or just shows a
  connection message).
- 🔭 **Langfuse** — LLM observability over the Copilot's turns (prompt / latency / cost).
- 🛰️ **ClickStack (HyperDX)** — pipeline + engine observability, and the OTLP target
  for the Streamlit app's OpenTelemetry traces (set `OTEL_EXPORTER_OTLP_ENDPOINT`).

See the [Integrations section of the README](README.md#-integrations--four-planes-each-with-a-job)
for what each plane does.
