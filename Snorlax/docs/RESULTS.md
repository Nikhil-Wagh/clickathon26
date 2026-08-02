# Snorlax — Results & Pipeline-Run Evidence

*Captured evidence from running the Snorlax pipeline end-to-end on ClickHouse
Cloud. This closes **GAP_ANALYSIS §C** ("no end-to-end pipeline-run evidence").*

Two independent runs, both executed **2026-08-02**:

- **Part A — Derived insight metrics** (`schema/insights.sql`) — read-only, against
  the loaded catalog. The metric **definitions** are the deliverable; the
  magnitudes below are live.
- **Part B — Correctness + latency on a sealed unseen day** (`benchmark/benchmark.py`)
  — the sealed file ingested through the **same pipeline the live app uses**, then
  benchmarked cold.

### Headline

| | |
|---|---|
| **Serving layer vs. raw-events oracle** | agree on **99.974 %** of cells (103 of 395,528 differ, all ±1–2) |
| **Global peak concurrency** | **18,858 sessions** @ `2026-07-31 11:16` — matches the oracle **exactly** |
| **Dashboard query latency** | every query shape clears **p95 < 100 ms** (worst p95 = 46 ms) |
| **Foreground-only overcount avoided** | **29.6 %** vs. a naive heartbeat count, on unseen data |

---

## Environment

| | |
|---|---|
| Engine | ClickHouse Cloud (`ap-south-1`), database `sonyliv_concurrency` |
| Executed | 2026-08-02 |
| Part A source | `schema/insights.sql`, all params defaulted (`''` = all dims, full range), **read-only** (only `SELECT`/`WITH`) |
| Part A data range | `2026-07-14 15:43` → `2026-08-02` UTC · **9** active dates · **26,493** densified minute buckets |
| Part A audience | **95,253** distinct sessions · **94,005** distinct users |

---

## Part A — Derived insight metrics

**11 / 11 queries execute clean.** Magnitudes are for this loaded catalog and
differ per sealed day; the definitions are the deliverable.

### Headline metrics

| Metric | Value | Reading |
|---|---|---|
| **Overall attention ratio** | **0.646** | 361,053 foreground ÷ 558,592 open session-buckets — **~35 % of open session-time is paused/backgrounded**, exactly the overcount that foreground-only concurrency avoids. The single most persuasive number for the project. |
| **Peak concurrency** | **5,108** | max simultaneous foreground sessions |
| **Peak ramp surge** | **+3,948 / min** | @ `2026-08-01 22:57` — a live-event kickoff; the autoscale trigger |
| **Peak ramp drop** | **−2,650 / min** | @ `2026-08-02 00:16` — post-event decline |

### Part I — concurrency-derived (the five §E metrics)

| # | Metric | Result |
|---|---|---|
| 1 | **Attention / foreground ratio** | overall **0.646** (361,053 fg / 558,592 open session-buckets); per-minute series over 26,493 buckets |
| 2 | **Concurrency ramp velocity** | peak **+3,948/min** (`2026-08-01 22:57`), trough **−2,650/min** (`2026-08-02 00:16`); per-bucket Δ + %/min series |
| 3 | **Join vs. leave net flow** | per-minute arrivals − departures + running-open cross-check (3,919 event-minutes) |
| 4 | **Ad-break drop-off / resume** | **31,186** ad-break sessions → **76.2 % resumed** (23,778), **23.8 % dropped** (7,408) |
| 5 | **Retention + QoE overlay** | concurrency as % of peak (peak 5,108) + per-minute error/rebuffer correlation series |

### Part II — industry-standard QoE / engagement / stickiness

| # | Metric | Result |
|---|---|---|
| 6 | **Startup / reliability funnel** | attempts **95,232** · plays **93,205** · play-rate **97.9 %** · VSF **0.0 %** (0) · EBVS **2.13 %** (2,027) · VPF **20.74 %** (19,328) · playback-success **79.7 %** |
| 7 | **Video Startup Time (VST)** | n = 91,599 · avg **7.86 s** · p50 **4.88 s** · p95 **23.68 s** · p99 **37.66 s** |
| 8 | **Rebuffering ratio** | **1.62 %** (259,430 stall-sec / 15,715,472 play-sec; 20,595 sessions stalled ≥ once) |
| 9 | **Watch-time / engagement** | **5,697.6 viewer-hours** · 3.6 min/session · 3.6 min/viewer · peak 5,108 · avg 12.9 · peak-to-avg **395.8** · sessions/user **1.01** |
| 10 | **Stickiness (DAU/MAU)** | 9 days · avg DAU **10,456** · peak DAU **74,588** · MAU **94,020** · **DAU/MAU 0.111** |

### Conclusions

- **Attention 0.646 is the crux** — ~35 % of "open" session-time is inactive, which
  a naive open-session count would overcount. This is the project thesis in one number.
- **VSF = 0** — no session errored *before* first playback; every error is mid-play
  (VPF). Failures in this data are during-playback, not startup.
- **peak-to-avg ≈ 396** — `avg` spans the full multi-day range including empty
  buckets, so a single large live event (2026-08-01) makes the curve extremely
  "peaky": the provisioning-headroom argument in one number.
- **sessions/user ≈ 1.01** — negligible multi-device / account-sharing here.

---

## Part B — Correctness & latency on a sealed unseen day

The sealed unseen-day file was ingested through the **same pipeline the live app
uses**, then benchmarked cold — a genuine generalization test, not a replay of
known data.

```bash
# 1. Ingest the unseen CSV through events_incoming (Null) → mv_incoming_to_raw →
#    events_raw (the live path; no Redpanda/ClickPipes needed):
cd Snorlax/producer && source .venv/bin/activate
python produce_events.py --csv ../unseen_data/unseen_data_sonyliv/ch-hackathon-raw-data_surprise.csv \
       --batch-size 50000 --no-wait

# 2. Offline-build the serving layer from historical events (refreshable MVs are
#    now()-relative, so a past day builds via the backfill path):
cd ../schema/migrations
python run_sql.py ../03_backfill.sql ../04_approaches.sql ../05_compare.sql

# 3. Correctness harness (raw-events oracle vs. serving layer):
cd ../../benchmark && python benchmark.py
```

### Run environment (this ingest)

| | |
|---|---|
| Source file | `ch-hackathon-raw-data_surprise.csv` — SonyLIV sealed unseen day (~1.8 GB) |
| Rows sent → landed | **7,000,000** sent → **6,998,842** in `events_raw` (Δ 1,158 = 0.017 %) |
| Audience | **108,158** sessions · **82,748** users · **14,974** content_ids |
| Event-time span | `2026-07-03` → `2026-08-03`; **99.1 %** on the unseen day **2026-07-31** (6,936,152 rows) |
| Serving cells | `concurrency_now` **395,528** `(dims, minute)` cells · `concurrency_ext_abs` 484,249 |
| Ingest throughput | single-thread CSV replay, ~10.2 K rows/s, 7 M rows in 689 s |

> The unseen day carries **real, messy** dimension values (`country='india'`,
> `platform='JIO_ANDROID_TV'`, `event='video-resize'`, `subtitle_language` in
> `OFF/unk/UNK`) vs. the synthetic catalog Part A ran on. `video_resolution` (a new
> column) was auto-skipped — not required for concurrency; table schema unchanged.

### B.1 — Serving layer vs. raw-events oracle

The serving layer (session-aware) and an independent raw-events oracle
(session-independent) were reconciled across every grain. They agree on
**99.974 %** of cells and on **every headline peak**; residual disagreement is a
handful of ±1–2 boundary cells (root cause in B.2).

| Check | Grain | Agreement | Delta |
|---|---|---|---|
| B0 | Cell-grain (serving == reference) | 395,425 / 395,528 cells | **103** cells differ (0.026 %), all ±1–2 |
| B1 | Global peak & avg — sessions | peak **18,858 == 18,858** @ `2026-07-31 11:16` | avg 21.1806 vs 21.1802 (Δ 0.0005) |
| B2 | Global peak & avg — users | peak **18,676 == 18,676** @ `11:16` | avg 20.9883 vs 20.9878 (Δ 0.0005) |
| B3 | Peak per platform | 18 / 19 keys exact | `SONY_HTML_TV` 126 vs 125 (Δ 1) |
| B4 | Peak per platform × country | 18 / 19 keys exact | `SONY_HTML_TV \| india` 126 vs 125 (Δ 1) |
| B5 | Peak per content | 13,744 / 13,750 keys exact | 6 keys off by ±1–2 |
| B6 | Peak per hour | 136 / 136 keys exact | — |
| B7 | Peak per day | 22 / 22 keys exact | — |
| B8 | Foreground-only exclusion | 918,986 vs 919,007 session-buckets | Δ 21 (0.002 %) |
| B9 | Extended drill-down cell-grain | 484,123 / 484,249 cells | 126 cells differ (0.026 %) |
| B10 | Extended → core roll-up | 0 cells where ext ≠ core | — |

> Peak-bucket **ties** (same peak value, different tie-broken minute) are
> informational — 8,772 across the multi-day range, none affect a peak value.

### B.2 — Root cause of the residual (`05_compare.sql`)

The two independent derivations disagree on exactly the same 103 cells B0 flags —
so the residual **is** a semantic tie, not a serving bug:

| | session-aware (`concurrency_now` / serving) | session-independent (oracle) | Δ |
|---|---|---|---|
| cells | 395,528 | 395,536 | 8 |
| session-minutes | 918,986 | 919,007 | 21 |
| user-minutes | 910,640 | 910,660 | 20 |
| **global peak (sessions)** | **18,858** | **18,858** | **0** |
| cell mismatches | — | — | **103** |

The two approaches differ only in **interval-merge boundary handling**:
session-aware merges adjacent active islands before minute-expansion,
session-independent does not. This is count-irrelevant *except* at a session's
edge minutes, where a merged vs. unmerged island can flip one bucket by one. On
the synthetic dataset the two agreed to **0**; the messier real feed (out-of-order
heartbeats, tied-ms events, odd `event` verbs) surfaces the edge on a handful of
pathological sessions (`987654320`, `21328993`, `2078155114`…). Both derivations
are correct-by-definition — a documented tie in interval semantics, not data loss.

### B.3 — Benchmark answers (from the serving layer)

**Concurrency = distinct foreground-active _sessions_; user-concurrency = distinct
_users_.** Values are `concurrency_now` (session-aware serving layer).

| Grain / cut | Peak (sessions) | Peak minute | Peak (users) |
|---|---|---|---|
| **Global (minute)** | **18,858** | `2026-07-31 11:16` | **18,676** |
| Per **day** (peak) | 18,858 | `2026-07-31` | — |
| Per **hour** (peak) | 18,858 | `2026-07-31 11:00` | — |
| Per **country** | india **18,858** | `11:16` | — |
| Per **platform** (top 3) | ANDROID_PHONE **6,146** · JIO_ANDROID_TV **4,839** · SONY_ANDROID_TV **2,673** | | |
| Per **content** (top 3 by peak) | `2078157818` **6,612** · `2078157680` 645 · `2078157821` 386 | | |

- **Average** concurrency over the full ingested span: **21.2 sessions / 21.0
  users** per bucket — low because the span is multi-day but activity is one live
  day, so empty buckets dominate the mean (peak-to-avg ≈ **890×**).
- **Foreground-only vs. naive (B8):** counting every heartbeat-in-bucket (naive)
  gives **1,305,807** session-buckets; foreground-only gives **918,986** — a
  **29.6 % overcount avoided**, on unseen data. This is the whole point of the project.

### B.4 — Query latency (dashboard-grade) — **p95 < 100 ms met**

Each shape run 30× (all-dims, **full-range = worst case**), percentiles of
`query_duration_ms` from `system.query_log`:

| Query shape | p50 ms | p95 ms | p99 ms | max ms | read_rows |
|---|---|---|---|---|---|
| filtered curve (ui q1) | 39 | **46** | 48 | 48 | 1,606,688 |
| KPI tiles (ui q2) | 30 | **32** | 56 | 67 | 1,606,688 |
| dim breakdown (ui q4) | 34 | **37** | 53 | 60 | 1,606,688 |
| hour/day roll-up (ui q5) | 35 | **39** | 47 | 50 | 1,606,688 |
| extended drill-down (ui q7) | 17 | **19** | 36 | 44 | 1,452,747 |

All five clear **p95 < 100 ms**. The heaviest full-range read touches ~1.6 M
serving rows in under 50 ms — filtered/narrower dashboard reads are faster still.

### B.5 — Data-quality / dedup reconciliation

| Metric | Expected | Actual | Delta |
|---|---|---|---|
| rows sent → `events_raw` | 7,000,000 | 6,998,842 | Δ 1,158 (0.017 %, async-insert block dedup) |
| duplicate `(session, ts, type, event)` | source may repeat | 25,135 dups (0.36 %) | deduped once-per-minute downstream |
| null `video_session_id` | 0 | **0** | — |
| null `event_timestamp` (epoch-0 coercion) | 0 | **0** | — |
| event-time coverage | full | Jul 31 = 99.1 %; contiguous, no empty active hour | — |

---

## Conclusions

1. **The serving layer is correct.** It reconciles to an independent raw-events
   oracle at **99.974 %** across 395,528 cells, matches the global peak
   (**18,858 @ 11:16**) and every hour/day peak exactly, and its only residual is a
   documented ±1 interval-boundary tie — not a data or logic defect.
2. **It generalizes.** All of the above holds on a **sealed, messy, real** unseen
   day never used to build the pipeline.
3. **It's fast enough for a live dashboard.** Every query shape clears
   **p95 < 100 ms** on the worst-case full-range read.
4. **The core idea pays off.** Foreground-only concurrency avoids a **29.6 %**
   overcount vs. naive heartbeat counting on the unseen day, consistent with the
   **~35 %** inactive-session-time measured independently in Part A.

---

## Reproduce (Part A)

Read-only, parametrized run of every query in `schema/insights.sql` with default
params (all dims, full range):

```bash
cd Snorlax
# per-query counts + preview (all-dims, full range):
./producer/.venv/bin/python /tmp/run_insights.py

# or a single query with the shared runner:
./producer/.venv/bin/python schema/migrations/run_sql.py -c "SELECT …"
```

> `WITH FILL FROM/TO` rejects a **Nullable** bound, so the coalesced date bounds in
> `insights.sql` are wrapped in `assumeNotNull(...)`; with that, all 11 queries run.
