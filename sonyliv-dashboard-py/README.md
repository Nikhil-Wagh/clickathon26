# SonyLIV Concurrency Dashboard (Python / Streamlit)

A Python dashboard for **live viewing-concurrency insights** served from
ClickHouse Cloud (`sonyliv_concurrency`). It reads the `concurrency_now` serving
view and renders a filtered per-minute concurrency curve plus peak / current /
average KPI tiles, with auto-refresh for still-open sessions.

This is a Python (Streamlit) port of the original Next.js dashboard — same data,
no Node/JS toolchain required — restyled to take inspiration from ClickHouse's
brand (signature yellow `#FAFF69` on near-black, with a columnar bar logo mark).

## Architecture

```
Streamlit app  ──►  clickhouse_client  ──►  ClickHouse Cloud
 filters/charts      cached CH client        concurrency_now + content_dim
                     parameterized SQL
```

| File | Responsibility |
|---|---|
| `app.py` | App shell: header, global filters, 4 tabs, Insights Copilot side panel |
| `ui.py` | Shared UI: theme CSS, KPI tiles, chart builders |
| `queries.py` | Concurrency + drill-down SQL (**real** — `concurrency_now`, `concurrency_ext_abs`) |
| `errors.py` | Errors pane data (**real**, partial — see errors.py for the taxonomy gap) |
| `insights.py` | Insights pane data (**real** — session_intervals ⋈ events_raw; segments are a heuristic) |
| `clickhouse_client.py` | `.env` loading + thread-local ClickHouse client |
| `assistant.py` | Insights Copilot — calls LibreChat's remote-agents API (Ollama fallback) |
| `guardrails.py` | Strips Bloomberg-proxy env vars before any outbound HTTP call |
| `config.py` | DB name, refresh cadence, LibreChat + Ollama endpoints, theme palette |
| `.streamlit/config.toml` | ClickHouse-inspired theme (yellow on near-black) |

### Panes
1. **📈 Concurrency** — real data from `concurrency_now`: peak/current/avg/min/p95
   stats, per-minute curve, per-dimension breakdowns, top content. `avg` uses
   the benchmark's canonical formula — `sum(concurrent) / (#buckets in the
   selected range)`, with a bucket that has no row (zero concurrency) counted
   as 0 — not `avg()` over only the minutes that had data, which quietly
   overstates the average whenever the range has any gap. Breakdowns show each
   dimension's own **peak-at** minute: concurrency isn't additive across
   dimensions, so platform/video-type/category routinely peak at different
   minutes (see `queries.py`'s `_Q_BREAKDOWNS`).
2. **🚨 Errors** — playback-error metrics from `events_raw` (VideoError events):
   real counts/rate/time-series/by-platform. The schema has no error-code/
   message taxonomy, so "by type"/"top messages" show one honest generic
   bucket instead of a fabricated breakdown — see `errors.py`.
3. **🧭 Insights** — derived user & content analytics, real: `session_intervals`
   (watch time) ⋈ `events_raw` (viewers) for top content + engagement by
   category. **User segments are a stated heuristic** (session-count buckets,
   not an official definition) — see `insights.py`. `session_intervals` has a
   3-day TTL, so ranges further back will under-report here.
4. **🔬 Drill-down** — the benchmark's extended drill-down query, reading
   `concurrency_ext_abs` (the EXTENDED serving table) with 4 additional
   high-cardinality filters — app version, audio language, subtitle language,
   player version — on top of the core dims. Language values are normalized
   at ingest (`config.sql`): pass e.g. `hin`, not `HIN`/`hin-hindi`.

### Insights Copilot (✨)
A native chat panel (`st.chat_message` / `st.chat_input`), not an embedded app —
lives in **`st.sidebar`**, always docked regardless of which tab
(Concurrency/Errors/Insights/Drill-down) is active. Deliberately not a
main-content `st.columns()` split: that layout stacks vertically below all the
tab content on narrower/zoomed browsers (Streamlit's responsive breakpoint),
which made the panel easy to miss without scrolling. The sidebar is a
structurally separate panel — it never reflows with the main content, doesn't
toggle, reset, or lose its history on a tab switch, and
`st.session_state.chat_messages` persists across reruns untouched. Each
message is sent with the *currently selected* filters, time range, and
Concurrency-pane KPIs/top-content as JSON context (see
`render_concurrency`'s return value in `app.py`), so it can answer questions
like "explain the peak" or "summarize this range" without the user repeating
filters.

Backed by **LibreChat running locally on Docker** (`assistant.py` →
`POST {LIBRECHAT_URL}/chat/completions`, LibreChat's OpenAI-compatible
*remote agents* API). LibreChat is the hub — it runs each turn on **Ollama**
(`llama3.2:3b`, on the host) and gives the agent **ClickHouse MCP** and
**ClickStack MCP** as tools:

```
Streamlit → LibreChat → { Ollama, ClickHouse MCP, ClickStack MCP }
```

So the model is local (no paid key) but every request flows through — and is
logged/managed by — LibreChat, and the agent can query the data directly via
MCP. See **[Connect to LibreChat](#connect-to-librechat)** below and the setup
guide in [`../librechat-setup/README.md`](../librechat-setup/README.md).

If LibreChat isn't configured (no `LIBRECHAT_API_KEY` / `LIBRECHAT_AGENT_ID`) or
is unreachable, the panel **falls back** to calling Ollama directly at
`http://localhost:11434` (`config.OLLAMA_URL`) with the same model, so a demo
still works. Set `ASSISTANT_ALLOW_OLLAMA_FALLBACK=0` to require LibreChat.

The dashboard runs on the host, so it reaches both LibreChat (`:3080`) and
Ollama (`:11434`) via their published ports; inside the compose network
LibreChat itself uses the `http://ollama:11434` service DNS name.

All queries use ClickHouse named params (`{name:Type}`); empty string `''` means
**all** for a dimension and **full range** for `from`/`to`.

## Setup

1. **Install** (Python 3.10+):
   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. **Credentials** — no setup needed if `../producer/.env` exists: the app
   reuses it automatically. To use separate credentials instead:
   ```bash
   cp .env.example .env    # then fill it in
   ```
   Keys match `producer/produce_events.py`: `CLICKHOUSE_HOST`, `CLICKHOUSE_PORT`,
   `CLICKHOUSE_USER`, `CLICKHOUSE_PASSWORD`, `CLICKHOUSE_SECURE`,
   `CLICKHOUSE_DATABASE`. `.env` is gitignored.
3. **(Optional) Insights Copilot** — see [Connect to LibreChat](#connect-to-librechat)
   below. You can skip this and still run the dashboard; the Copilot panel just
   shows a connection message until it can reach LibreChat (or Ollama).
4. **Run**:
   ```bash
   streamlit run app.py      # http://localhost:8501
   ```

## Connect to LibreChat

The Insights Copilot routes through a **local LibreChat on Docker**, which runs
the model on **Ollama**. Do this once:

1. **Run LibreChat locally** (Docker) with Ollama on the host and the MCP
   servers wired — full walkthrough in
   [`../librechat-setup/README.md`](../librechat-setup/README.md). In short:
   ```bash
   OLLAMA_HOST=0.0.0.0:11434 ollama serve   # Ollama on the HOST (bind to 0.0.0.0)
   ollama pull llama3.2:3b                   # first run only
   # in your LibreChat clone (with this repo's librechat-setup/ files copied in):
   docker compose up -d                      # LibreChat at :3080
   ```
   Register the first user (becomes admin), create an agent on the **Ollama**
   endpoint, **add the `clickhouse` + `clickstack` MCP tools to it**, and make an
   agent API key. Copy the **agent id** and **API key**.
2. **Point the dashboard at LibreChat** — add these to
   `sonyliv-dashboard-py/.env` (gitignored) or export them:
   ```bash
   LIBRECHAT_URL=http://localhost:3080/api/agents/v1   # default; usually leave as-is
   LIBRECHAT_API_KEY=<the agent API key>
   LIBRECHAT_AGENT_ID=<the agent id>
   ```
3. **Run the dashboard** (`streamlit run app.py`) and open the **✨ Insights
   Copilot** panel — messages now go to LibreChat → Ollama.

**Fallback:** if the two LibreChat vars are unset or LibreChat is unreachable,
the panel calls Ollama directly at `OLLAMA_URL` (default
`http://localhost:11434`, model `OLLAMA_MODEL` = `llama3.2:3b`). Set
`ASSISTANT_ALLOW_OLLAMA_FALLBACK=0` to require LibreChat and disable this path.

## Features

- **Filters**: platform, country, video type, category, content, and a from/to
  time window (blank = full data range).
- **KPI tiles**: peak concurrency (+ its minute), current concurrency (latest
  minute in range), average concurrency over the range.
- **Concurrency curve**: per-minute Plotly area chart, filter-reactive.
- **Live updates**: auto-refresh every 30s (toggle) + manual refresh + a
  "last updated" stamp.
- **✨ Insights Copilot**: native, context-aware chat panel — see
  [Insights Copilot](#insights-copilot-) above.

## Stack

Python · Streamlit · `clickhouse-connect` · Plotly · pandas · LibreChat
remote-agents API (OpenAI-compatible) backed by a local Ollama model, with a
direct-Ollama fallback (plain HTTP, no SDK dependency, no paid API).
