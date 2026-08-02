# SonyLIV — Gap Analysis

*Originally: what the problem statement / README require, but the early
`DESIGN_PLAN.md` / `SOLUTION_OVERVIEW.md` omit or under-specify. **Updated
2026-08-02** — re-audited against the implemented `Snorlax/` solution. The
original correctness gaps (§1–§8 below) are now almost all addressed in code
(status noted inline). The gaps that remain are no longer in the concurrency
*model* — they are in the surrounding requirements the rubric also scores
(integration, visualization, pipeline evidence) and in enhancement headroom.*

## Open gaps (current — implemented `Snorlax/` solution)

*Ranked by scoring risk. The model itself is strong; these are what's left on
the table.*

### A. A required integration is not actually built — HARD REQUIREMENT, currently unmet
The problem statement (line 40) **requires** meaningfully integrating at least
one of **ClickStack / Langfuse / LibreChat**, and explicitly warns *"superficial
inclusion won't count."* In the current tree there is **no integration code** —
ClickStack appears only as two *comments* about sizing the watermark to p99 lag
(`Snorlax/schema/00_config.sql:78`, `Snorlax/schema/01_schema.sql:27`). This is
the single biggest scoring risk because it is a hard "must." **Lowest-effort
close:** ClickStack (ClickHouse + OpenTelemetry) observing producer→ClickPipes
ingest lag and query latency — it *also* produces the p99-lag number the hot-window
knob is already designed against, so one move satisfies the requirement and tunes
the model. Alternatives: LibreChat + the (preconfigured) ClickHouse MCP server as
a conversational layer; Langfuse only if an LLM layer is added (see §D).

### B. No runnable visualization of concurrency over time
`Snorlax/presentation/index.html` is a **pitch slide deck** ("The Problem / Our
Solution / Thank You"), not the "minimal visualization of concurrency over time"
the problem asks for (Suggested demo, line 79; "What great looks like"). PLAN
Track C (React/Streamlit curve + filters + KPI tiles) was scoped but never built.
The serving queries exist (`schema/ui_queries.sql`); what's missing is a thin
front end that reads `concurrency_now` and renders the curve with a
platform/country/content filter.

### C. No end-to-end pipeline-run evidence (the "unseen day") — GATES CORRECTNESS CREDIT
`Snorlax/README.md:196` states plainly the SQL has **not yet been executed
end-to-end** on a live ClickHouse instance. The rubric is blunt: *"No pipeline
evidence, no credit,"* and the sealed-day results *"carry significant weight in
shortlisting."* The correctness **harness** exists (`benchmark/benchmark.py`,
self-checking with no answer key), but there is no captured run: no benchmark
answers, no latencies, no `system.query_log` artifacts. This is the meta-gap —
until it's closed, the correct-by-design model earns no correctness credit. Close
by running `00→06` + `benchmark.py` on Cloud, then packaging answers + latencies +
query-log evidence for the sealed run.

### D. LLM + ClickStack concurrency-decline detection not implemented (optional, explicitly blessed)
The statement names this as the natural LLM use-case (line 42): detect and alert
on concurrency decline, classifying the cause — **asset ended** (expected;
correlate with a `VideoSessionEnd` spike), **technical** (correlate with
`VideoError` / buffering `speed-pause` spikes in the same minute), or
**disengagement** (gradual, no error/end spike). Optional, but a strong
differentiator and a natural home for the Langfuse integration.

### E. Insight coverage stops at peak/average (enhancement headroom)
The model answers peak/avg concurrency per dims/grain, but the data supports much
higher-value derived metrics that are cheap given the serving layer:
- **Foreground / attention ratio** = foreground-concurrent ÷ open-sessions — quantifies
  the overcount the whole project avoids (B8 already measures the raw delta); the
  most persuasive single number in the deck.
- **Concurrency ramp velocity** (Δconcurrent per bucket) — the live-sport
  kickoff/toss surge; also the autoscale trigger. One `lagInFrame` over the curve.
- **Join vs. leave rate / net flow** — already half-built (`ui_queries.sql` q3
  emits sessions started/ended per minute); surface arrivals − departures.
- **Ad-break drop-off / resume rate** around `AdBreakStart`→`AdResume`
  (`windowFunnel`) — directly informs the ad-load decisions the problem names.
- **Retention curve during the live event**; **error/rebuffer correlation** overlaid
  on the curve (QoE proxy from `VideoError` + buffering markers).

### F. Known 100× scale limitation flagged but not implemented
PLAN §9 documents that `mv_session_intervals` re-derives each session's **full
event history** every refresh, so per-session cost scales with **session
duration**, not just the activity window — fine at hackathon scale, a problem for
hours-long live-sport sessions at 100×. The two designed fixes — a **delta→cumsum
cold build** (PLAN §4, avoids per-session-minute expansion) and an **incremental
per-session cursor** (PLAN §9, carries `watching` state forward) — are described
but not built. Even shipping the delta+cumsum cold variant would materially
strengthen the "how does this behave at 100×" answer the judges will ask.

---

## Historical gaps (vs. the early `DESIGN_PLAN.md` / `SOLUTION_OVERVIEW.md`)

## Significant gaps (correctness-affecting)

### 1. The session-independent model is essentially missing from the plan
Both `PROBLEM_STATEMENT.md` (line 18) and `README_START_HERE.md` (lines 34–38, 59) require **two** views — *session-aware* AND *session-independent* — plus an explicit **comparison** to validate accuracy and trade-offs. `SOLUTION_OVERVIEW.md` names both (§2) and assigns "implement both" to Member B, but `DESIGN_PLAN.md` only designs one pipeline (interval→delta, the session-aware path). There is no design for computing "active foreground viewers directly from event state, no session reconstruction," and no design for how the two are reconciled/compared. **Biggest hole.**

> **RESOLVED** (Snorlax). Both approaches are now built and compared in one file,
> `Snorlax/schema/04_approaches.sql`: the *session-aware* path builds
> `concurrency_sa_abs` from the reconstructed `session_intervals`; the
> *session-independent* path builds `concurrency_si_abs` from per-event foreground
> state with **no interval reconstruction**. `Snorlax/schema/05_compare.sql`
> asserts session-aware == session-independent == `concurrency_now` per
> `(dims, minute)` — **0 mismatches expected**. Both share one active definition;
> because the count is `uniqExact` per minute, interval-merging vs. not-merging is
> irrelevant to the result, which is what makes the cross-check meaningful. (Runs
> as designed; end-to-end execution evidence is the open item — see §C.)

### 2. Paused state is under-specified — and the docs contradict each other
The problem calls out three inactive causes: *heartbeat missing, paused, backgrounded* (line 22). `DESIGN_PLAN.md §3` only handles heartbeat-gap and `AppBackgrounded` — no pause handling. Its event list (lines 15–16) has **no pause event**, yet `SOLUTION_OVERVIEW.md §3` says "AppBackgrounded/pause closes active." If a paused player keeps heartbeating, the gap rule won't catch it, so pause needs an explicit rule/state field. Resolve whether a pause event / playback-state marker exists (via `dataset_details.md`, which isn't in the folder).

> **RESOLVED** (Snorlax implementation). `dataset_details.md` (now in `Snorlax/problem/`) shows the contradiction dissolves: pause is **not** one of the 7 coarse `event_type`s — it is a **playback-state marker in the `event` column** ("the actual event"): `pause` / `speed-pause` / `AdPause`. So "no pause event_type" (DESIGN_PLAN) and "pause closes active" (SOLUTION_OVERVIEW) are both right at different granularities.
> - **Explicit state field, not the gap rule.** The Snorlax state machine classifies pause as a `−1` deactivation and carries the deactivated state forward; heartbeats are neutral (`0`) and never reset it, so a **paused player that keeps heartbeating is correctly excluded** (the concern above). Matched in `schema.sql` D2, `backfill_history.sql`, `approach_session_independent.sql`.
> - **Robustness fix.** Pause is now caught by `event_type` **OR** `event`: the deactivate branch also matches the `VideoPause` / `AdBreakStart` event_types directly, so a pause can't slip through as a neutral heartbeat if it arrives with a blank/unknown `event`. (`event_type` is now `LowCardinality(String)`, not a strict enum, so it also tolerates unseen-day event types instead of rejecting them on ingest — matching by `event_type` **and** `event` keeps the classifier robust as new event types appear.)
> - **Out of scope (flagged):** seek/buffering (`VideoSeek` / `speed-pause`) is the separate **buffering active/inactive toggle** (PLAN §9), deliberately left on the `event` value, not hardcoded as a pause. Docs updated: `README.md`, `plan/PLAN.md §3`.

### 2a. Initial session state was under-specified (discovered while resolving #2) — RESOLVED
The mirror image of the pause question: neither doc says what a session's state is *before* its first explicit `VideoPlay`. The Snorlax state machine had seeded every session **inactive** (`state_sign = 0`) until the first `+1` event, which silently undercounted two ways: (1) a session whose first state-changing event is a deactivation (pause/background/error/end) — with active heartbeats before it — contributed **zero** active minutes despite clearly being watched; (2) a late `VideoPlay` dropped every active heartbeat before it. Masked, because `verify.sql`'s naive comparison credits the lost time to "pause overcount avoided," and `compare_approaches.sql` can't see it (both approaches share the identical classifier).

> **RESOLVED.** `VideoSessionStart` now **seeds the session as active** (a session is watching from its start until a pause/background/error/end stops it — heartbeats only fire while active). Applied in `schema.sql` D2, `backfill_history.sql`, `approach_session_independent.sql`; the producer (`produce_events.py`) now emits `VideoPlay` right after `VideoSessionStart` to match the documented/seed convention. Trade-off pinned to the benchmark key (like the buffering toggle): a session that starts but never truly plays counts ~1 minute (the `[start, first-play)` window).

### 3. `VideoError` handling is undefined
It's in the event stream (`DESIGN_PLAN.md` line 16) but neither doc says whether an error terminates an active interval, marks inactivity, or is ignored. Directly affects active-range computation.

> **RESOLVED** (Snorlax). `VideoError` is classified as a **`−1` deactivation** in
> the state machine (PLAN §3; `schema/01_schema.sql` D2, `03_backfill.sql`,
> `04_approaches.sql`) — an errored player is not foreground-active. The state is
> carried forward until the next activating event, so a transient error that
> recovers (a later `VideoPlay`/`resume`) reopens the interval, while an error that
> ends the session stays excluded.

### 4. Deduplication of late/repeated events is not in the model
`README_START_HERE.md` step 3 (line 44) explicitly requires "deduplicate late or repeated events." `DESIGN_PLAN.md` never mentions dedup; `SOLUTION_OVERVIEW.md` only gestures at "null/dup handling" as a Member A chore. Dedup logic (ReplacingMergeTree? by event id?) should be part of the pipeline design since duplicate heartbeats corrupt interval derivation.

> **RESOLVED (design), one caveat open.** The state machine first **collapses
> events per `(session, millisecond)`** with priority *deactivate > reactivate >
> neutral* (PLAN §3 "Determinism"), which both fixes same-ms nondeterminism and
> makes duplicate heartbeats idempotent (neutral repeats never change state).
> `session_intervals` / `concurrency_cold_abs` are `ReplacingMergeTree` read
> `FINAL` (retry-safe). **Open caveat (PLAN §9 Fix #10):** `events_raw` is plain
> MergeTree, so a *retried batch load* can duplicate raw rows — dedup by
> `(video_session_id, event_timestamp, event_type, event)` or reload cleanly before
> the sealed-day run.

## Medium gaps (scope / coverage)

### 5. Hour/day grain roll-up is not designed
Problem asks peak & average at **minute/hour/day** grain (lines 24, 26). `DESIGN_PLAN.md` designs only the minute curve; how hour/day peak (max-of-minutes) and average roll up from it — and whether that needs additional serving tables — isn't addressed.

> **RESOLVED** (Snorlax). Hour/day grain rolls up from the minute curve at query
> time — no extra serving tables (`schema/ui_queries.sql` q5: `toStartOfHour` /
> `toStartOfDay`, `max` for peak, densified `avg` for average). Verified by
> benchmark checks **B6** (per-hour peak) and **B7** (per-day peak).

### 6. Average-concurrency semantics are undefined
Peak has the worked 300K/200K/50K example, but "average" has no denominator defined (mean over all minutes in the window including zeros, vs. only active minutes). This changes the answer against ground truth.

> **RESOLVED** (Snorlax). Average is defined as **sum ÷ number of buckets in the
> range, counting empty buckets as zero** — denominator is
> `dateDiff('second', from, to) / cfg_bucket_seconds() + 1`, and zero-activity
> buckets are densified with `WITH FILL` **before** averaging so absent minutes
> aren't silently skipped (a bug that had over-reported the average; `ui_queries.sql`
> q2/q5). Bucket-width-aware, so it stays correct if the bucket knob changes.

### 7. The provided benchmark query set is treated as something to author
The problem says the benchmark set is **given** ("A benchmark query set… the fixed concurrency questions your system will be evaluated on," line 32). `SOLUTION_OVERVIEW.md` Member C says "**Write** the benchmark query set." The plan should ingest/run the provided set and its answer format, not invent its own.

> **PARTIALLY ADDRESSED** (Snorlax). `benchmark/BENCHMARK_QUERIES.md` derives its
> questions directly from the problem statement (peak/avg at minute/hour/day with
> dimension filters) and `benchmark/benchmark.py` is a *self-checking oracle* —
> it computes each answer twice (raw-events reference vs. serving layer) and
> asserts they agree, needing no private key. **Still open:** when the actual
> provided benchmark set + answer format is released, run *that* set through the
> pipeline and produce answers in the required format — don't rely solely on the
> internally-authored questions. Tied to §C (pipeline evidence).

### 8. Content join is described as a plain dimension add, not a real-time enriched join
`README_START_HERE.md` (line 32, and the content-level-concurrency aggregation, line 52) stresses real-time join and **join consistency**. `DESIGN_PLAN.md` reduces it to step 5 ("content join for title/video_type/category"); join consistency and dedup of the ~33K content table aren't discussed.

> **RESOLVED** (Snorlax). The correctness-critical path uses a **`LEFT JOIN
> content_dim FINAL`** (not `dictGet`) for `video_type`/`category` — a ClickHouse
> Cloud dictionary reload is node-local, so a stale replica could silently serve
> wrong dims; the `FINAL` join is consistent. `content_dim` is `ReplacingMergeTree`
> (dedup by `content_id`). The `content_dict` dictionary is kept **only** for the
> UI's display-only `title` lookup, where staleness doesn't affect correctness
> (PLAN §5/§13; it's `COMPLEX_KEY_HASHED` to handle the negative sentinel
> `content_id`).

## Minor / worth noting

- **Latency SLA is unquantified** — "dashboard-grade latency" still has no target
  number. *Still open:* pin a concrete target (e.g. p95 < 100 ms on filtered
  minute-grain reads) and prove it via `system.query_log` (`ui_queries.sql` q6
  captures duration/`read_rows`); folds into the §C evidence pack.
- **Langfuse** — *now addressed as a decision:* only one integration is required
  and ClickStack is the primary choice (§A). Langfuse is the natural home **if**
  the LLM concurrency-decline layer is added (§D); otherwise deliberately dropped.
- **User-level vs session-level concurrency** — **RESOLVED.** Every aggregate now
  stores **two** measures: `concurrent` = distinct sessions
  (`uniqExact(video_session_id)`) and `concurrent_users` = distinct users
  (`uniqExact(user_id)`), both exact per cell (benchmark B1 vs B2). Caveat
  documented: summing `concurrent_users` *across* dims can overcount a user on >1
  content, so user counts are reported at cell grain.
- **Missing referenced files** — **RESOLVED.** `dataset_details.md` is now in
  `Snorlax/problem/`; it closed the pause-vs-heartbeat question (pause is an
  `event`-column marker, not an `event_type` — see §2) and pinned the event schema.
  The `data/` CSVs feed the seed/backfill and producer.
