-- 003_incremental_session_cursor.sql
-- #####################################################################
-- THE 100× DERIVATION FIX (PLAN.md §9a). Replaces the full-history
-- re-derivation in mv_session_intervals with an INCREMENTAL per-session
-- cursor, so per-cycle derivation cost is O(new tail events), not
-- O(events-so-far). Turns the old cost ∝ session DURATION (a super-linear
-- wall for hours-long live-sport sessions, GAP_ANALYSIS §F / PLAN §9) into
-- cost ∝ event ARRIVAL RATE.
--
-- ── THE PROBLEM THIS FIXES ────────────────────────────────────────────
-- The prior mv_session_intervals read `events_raw WHERE video_session_id
-- IN recent` with NO time bound on the events — only the SESSION was scoped
-- to the 20-min recency window. So every 30s it re-read (and re-ran window
-- functions over) each active session's ENTIRE event history. A 3-hour
-- session reprocessed its opening events ~360×; total work per session was
-- O(D²) in duration.
--
-- ── HOW THE CURSOR WORKS ──────────────────────────────────────────────
-- Apply the same hot/cold split we already use for SERVING to the STATE
-- MACHINE itself. Per refresh at wall-time now():
--   * seal boundary B = now() - cfg_seal_lag_seconds() (00_config.sql).
--   * Everything a session had at/before its stored cursor_ts is COMMITTED
--     (immutable) and carried forward verbatim, clipped at cursor_ts.
--   * Only events with ts > cursor_ts (the "provisional tail", ≈ seal_lag
--     wide) are re-read and re-derived. THIS is the line that changes the
--     asymptotics (`WHERE ev.event_timestamp > c.cursor_ts_prev`).
--   * A synthetic SEED event at cursor_ts carrying the stored cursor_state/
--     platform/user_id resumes the state machine, so a session that stayed
--     ACTIVE across a heartbeat gap (state carried, first tail event is a
--     bare heartbeat) — or whose activating +1 is now sealed — reopens
--     correctly instead of being dropped to inactive. Without the seed the
--     tail would recompute state_sign from scratch and lose the carry.
--   * The cursor then advances to the last event ts ≤ B, storing the state/
--     platform/user at that instant for next cycle.
-- Interval-merge is count-irrelevant (uniqExact per (dims,minute) is a set
-- membership test — GAP §1), so clipping an island at the boundary and
-- reproducing its tail from the seed double-covers only the boundary minute,
-- which dedupes to one — the counts are identical to a full re-derivation.
--
-- ── LATE-DATA SELF-HEAL (mv_session_intervals_resync) ─────────────────
-- The incremental path reads only ts > cursor_ts, so an event that lands
-- BEHIND the seal horizon (later than p99 ingest lag) would be missed. The
-- low-cadence resync MV re-runs the FULL derivation for a wider recency
-- window every few minutes and APPENDs superseding rows (higher version
-- wins under ReplacingMergeTree(version) FINAL), correcting any drift. It
-- pays the old O(D) cost but ~10× less often and is the SAFETY NET, not the
-- hot path. (A fully surgical dirty-session path — flag only sessions that
-- received a late event, via an ingest-time column — is the next refinement;
-- see PLAN §9a.)
--
-- ── SCALE COMPOSITION ─────────────────────────────────────────────────
-- Cursor state is per-session (video_session_id) — the same shard key
-- (PLAN §9 scale-out). So sharding (more concurrent sessions, linear) ×
-- cursor (longer sessions, linear) compose to a genuine 100×. Serving
-- (concurrency_now) is untouched and already scale-independent (M4).
--
-- ── COST PROFILE (is this efficient enough?) ──────────────────────────
-- The query is verbose but the WORK per 30s refresh is bounded:
--   * events_raw read = TAIL only (ts > cursor_ts) — the whole point; bounded
--     by cfg_seal_lag_seconds() × active sessions, NOT session history.
--   * window functions (stated/islands/per_island) run over that bounded tail.
--   * cursor read = session_intervals FINAL SCOPED to recent sessions (the
--     WHERE ... IN session_last_seen below) so ReplacingMergeTree merge-on-read
--     prunes by the video_session_id ORDER BY key — NOT a full-table FINAL.
-- Remaining cost knobs to watch on the first live run (system.view_refreshes
-- last_success_duration_ms, system.query_log read_rows) — all bounded by
-- ACTIVE-session count, none by session duration:
--   1. CTEs are re-evaluated per reference (ClickHouse doesn't materialize
--      them); `cursor` is read ~3× (JOIN, IN, seed). It's now scoped, so cheap,
--      but if it shows up hot, split into a staged INSERT or a temp view.
--   2. session_intervals accumulates versioned rows between merges (APPEND every
--      30s); keep FINAL scoped and let background merges + the 3-day TTL bound it.
--   3. The hot/cold serving MVs (D3/D4) ALSO read session_intervals FINAL each
--      cycle (pre-existing) — same scoping optimization applies there and is the
--      next tuning target if refresh duration approaches the interval.
-- HARD RULE: a refresh must finish inside its interval. If it doesn't at target
-- scale, shard by video_session_id (cursor is per-session — shards cleanly) or
-- widen the cadence; do NOT let refreshes queue.
--
-- ── IDEMPOTENCY / ROLLOUT ─────────────────────────────────────────────
-- Cursor columns are added with ADD COLUMN IF NOT EXISTS (additive — does
-- NOT drop session_intervals, so the backfill/seed survive, unlike 002).
-- New columns default to epoch/0/'' → any pre-existing row bootstraps with
-- a single full read on its first incremental cycle, then goes incremental.
-- Safe to re-run any number of times (DROP IF EXISTS + CREATE, ADD COLUMN
-- IF NOT EXISTS).
--
-- STATUS: correct-by-construction; NOT yet executed on live ClickHouse
-- (same standing caveat as the rest of the pipeline, PLAN §10). Two engine
-- assumptions to confirm on first run: (1) a REFRESH MV may read its own
-- target table in its SELECT (used here as the cursor read) — standard for
-- refreshable MVs, which are scheduled INSERT…SELECT; (2) the incremental==
-- full equivalence gate in 06_verify.sql check H must show 0 mismatches
-- before this path is trusted.
--
-- REQUIRED AFTER APPLYING: re-run 03_backfill.sql once (it now writes the
-- cursor columns). See 03_backfill.sql / migrations/README.md.
-- #####################################################################

-- Drop dependents BEFORE the view they depend on (hot DEPENDS ON the
-- session-intervals MV; cold DEPENDS ON hot).
DROP VIEW IF EXISTS sonyliv_concurrency.mv_cold_compaction;
DROP VIEW IF EXISTS sonyliv_concurrency.concurrency_hot_abs_mv;
DROP VIEW IF EXISTS sonyliv_concurrency.mv_session_intervals;
DROP VIEW IF EXISTS sonyliv_concurrency.mv_session_intervals_resync;

-- ---------------------------------------------------------------------
-- Cursor state carried on session_intervals (additive; preserves data).
--   cursor_ts       — sealed boundary: events at/before this are committed
--                     and never re-read. epoch(0) => bootstrap (full read).
--   cursor_state    — watching state (state_sign) as of cursor_ts (1/-1/0).
--   cursor_platform — platform in effect at cursor_ts (seed keeps island id).
--   cursor_user_id  — user_id  in effect at cursor_ts.
-- ---------------------------------------------------------------------
ALTER TABLE sonyliv_concurrency.session_intervals
  ADD COLUMN IF NOT EXISTS cursor_ts       DateTime64(3,'UTC') DEFAULT toDateTime64(0,3,'UTC'),
  ADD COLUMN IF NOT EXISTS cursor_state    Int8                DEFAULT 0,
  ADD COLUMN IF NOT EXISTS cursor_platform LowCardinality(String) DEFAULT '',
  ADD COLUMN IF NOT EXISTS cursor_user_id  String              DEFAULT '';

-- =====================================================================
-- D2. INCREMENTAL mv_session_intervals — the fast path.
-- Column order MUST match session_intervals (positional APPEND insert):
--   video_session_id, intervals, is_provisional, content_id, country,
--   video_type, category, title, version,
--   cursor_ts, cursor_state, cursor_platform, cursor_user_id.
-- =====================================================================
CREATE MATERIALIZED VIEW sonyliv_concurrency.mv_session_intervals
REFRESH EVERY 30 SECOND APPEND TO sonyliv_concurrency.session_intervals EMPTY AS
SELECT sess.video_session_id AS video_session_id,
       sess.intervals        AS intervals,
       -- provisional threshold tied to the gap timeout (00_config.sql), same
       -- convention as before: a session whose last active edge is within one
       -- gap timeout of now() may still be open.
       toUInt8(sess.last_active_end >= now() - toIntervalSecond(cfg_gap_timeout_seconds())) AS is_provisional,
       sess.content_id AS content_id, sess.country AS country,
       cd.video_type AS video_type, cd.category AS category, cd.title AS title,
       toUnixTimestamp64Milli(now64(3)) AS version,
       sess.cursor_ts AS cursor_ts, sess.cursor_state AS cursor_state,
       sess.cursor_platform AS cursor_platform, sess.cursor_user_id AS cursor_user_id
FROM
(
  WITH
  -- Only sessions active in the derivation recency window (O(active sessions),
  -- reads the tiny session_last_seen — not a full events_raw scan). MUST stay
  -- wider than cfg_seal_lag_seconds() so a session seals before it ages out.
  recent AS (
    SELECT video_session_id FROM sonyliv_concurrency.session_last_seen
    WHERE last_ts >= now() - INTERVAL 20 MINUTE ),
  -- The cursor = the session's PRIOR row, read back from our own target.
  -- LEFT JOIN so a brand-new session gets empty intervals + epoch cursor and
  -- bootstraps with one full read (cursor_ts_prev = 0 => tail = all events).
  cursor AS (
    SELECT r.video_session_id AS sid,
           si.intervals         AS prev_intervals,
           si.content_id        AS content_id_prev,
           si.country           AS country_prev,
           si.cursor_ts         AS cursor_ts_prev,
           si.cursor_state      AS cursor_state_prev,
           si.cursor_platform   AS cursor_platform_prev,
           si.cursor_user_id    AS cursor_user_prev
    FROM recent r
    LEFT JOIN (
      SELECT video_session_id, intervals, content_id, country,
             cursor_ts, cursor_state, cursor_platform, cursor_user_id
      FROM sonyliv_concurrency.session_intervals FINAL
      -- SCOPE FINAL to recent sessions: video_session_id is the ORDER BY key,
      -- so this prunes the ReplacingMergeTree merge-on-read to just the active
      -- sessions' granules instead of FINAL-ing the whole table every 30s
      -- (the dominant cost at scale — see migration header perf note).
      WHERE video_session_id IN (
        SELECT video_session_id FROM sonyliv_concurrency.session_last_seen
        WHERE last_ts >= now() - INTERVAL 20 MINUTE )
    ) si ON r.video_session_id = si.video_session_id ),
  -- TAIL ONLY: events strictly after the sealed boundary. This is the
  -- O(D)->O(tail) win — sealed events are never re-read.
  tail_raw AS (
    SELECT ev.video_session_id AS sid, ev.user_id AS user_id, ev.event_timestamp AS ts,
           ev.content_id AS content_id, ev.platform AS platform, ev.country AS country,
           multiIf(ev.event_type IN ('VideoSessionStart','VideoPlay','AppForegrounded') OR ev.event IN ('resume','speed-resume','AdResume'), 1,
                   ev.event_type IN ('AppBackgrounded','VideoSessionEnd','VideoError','VideoPause','AdBreakStart') OR ev.event IN ('pause','speed-pause','AdPause'), -1,
                   0) AS transition
    FROM sonyliv_concurrency.events_raw ev
    INNER JOIN cursor c ON ev.video_session_id = c.sid
    WHERE ev.video_session_id IN (SELECT sid FROM cursor)     -- prune events_raw by session (primary key)
      AND ev.event_timestamp > c.cursor_ts_prev ),            -- <-- the tail bound
  sessions_with_tail AS ( SELECT DISTINCT sid FROM tail_raw ),
  -- Synthetic SEED at the boundary, carrying the stored state so a session
  -- active across a heartbeat gap (or with a now-sealed activating +1)
  -- resumes correctly. Only for sessions with a real prior cursor AND new
  -- tail events (idle sessions are skipped — APPEND leaves their row as-is).
  -- Carries content/country/platform/user so a seed-only boundary island
  -- (state=1, next real event beyond the gap) still gets correct dims.
  seed AS (
    SELECT c.sid AS sid, c.cursor_user_prev AS user_id, c.cursor_ts_prev AS ts,
           c.content_id_prev AS content_id, c.cursor_platform_prev AS platform,
           c.country_prev AS country, toInt8(c.cursor_state_prev) AS transition
    FROM cursor c
    WHERE c.cursor_ts_prev > toDateTime64(0,3,'UTC')
      AND c.sid IN (SELECT sid FROM sessions_with_tail) ),
  per_event AS (
    SELECT sid, user_id, ts, content_id, platform, country, transition FROM tail_raw
    UNION ALL
    SELECT sid, user_id, ts, content_id, platform, country, transition FROM seed ),
  -- ---- identical active definition to 03_backfill.sql from here ----
  collapsed AS (                                    -- one row per (session, ms); deactivate wins
    SELECT sid, ts, if(min(transition) < 0, toInt8(-1), toInt8(max(transition))) AS transition,
           any(user_id) AS user_id, any(content_id) AS content_id, any(platform) AS platform, any(country) AS country
    FROM per_event GROUP BY sid, ts ),
  stated AS (
    SELECT sid, ts, user_id, content_id, platform, country,
      argMax(transition, if(transition!=0, ts, toDateTime64('1970-01-01 00:00:00',3,'UTC')))
        OVER (PARTITION BY sid ORDER BY ts ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS state_sign,
      row_number() OVER (PARTITION BY sid ORDER BY ts) AS rn,
      count()      OVER (PARTITION BY sid)             AS n,
      leadInFrame(ts) OVER (PARTITION BY sid ORDER BY ts ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS next_ts
    FROM collapsed ),
  segments AS (
    SELECT sid, user_id, content_id, platform, country, ts AS seg_start,
      multiIf(rn=n, addSeconds(ts, cfg_heartbeat_seconds()),
              dateDiff('second', ts, next_ts) <= cfg_gap_timeout_seconds(), next_ts,
              addSeconds(ts, cfg_heartbeat_seconds())) AS seg_end
    FROM stated WHERE state_sign = 1 ),
  islands AS (
    SELECT *, if(seg_start > max(seg_end) OVER (PARTITION BY sid ORDER BY seg_start
               ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
               OR platform != lagInFrame(platform) OVER (PARTITION BY sid ORDER BY seg_start
               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
               OR user_id != lagInFrame(user_id) OVER (PARTITION BY sid ORDER BY seg_start
               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW), 1, 0) AS new_island
    FROM segments ),
  per_island AS (
    SELECT sid, island_id, min(seg_start) AS istart, max(seg_end) AS iend,
           any(user_id) AS user_id, any(content_id) AS content_id,
           any(platform) AS platform, any(country) AS country
    FROM (SELECT *, sum(new_island) OVER (PARTITION BY sid ORDER BY seg_start
               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS island_id FROM islands)
    GROUP BY sid, island_id HAVING iend > istart ),
  -- Freshly-derived TAIL islands per session (all islands from the seed/tail
  -- events; content/country are session-constant so any() is safe).
  tail_islands AS (
    SELECT sid,
           groupArray((istart, iend, platform, user_id)) AS tail_intervals,
           max(iend) AS tail_last_end,
           any(content_id) AS content_id, any(country) AS country
    FROM per_island GROUP BY sid ),
  -- Advance the cursor to the last event ts <= B (seal boundary). The seed's
  -- ts (= old cursor_ts) is always <= B, so an existing session always has a
  -- row here (cursor is monotonic non-decreasing). A brand-new session whose
  -- events are all < seal_lag old has none yet -> keep epoch (bootstrap again).
  new_cursor AS (
    SELECT sid,
           max(ts)                 AS cursor_ts,
           argMax(state_sign, ts)  AS cursor_state,
           argMax(platform, ts)    AS cursor_platform,
           argMax(user_id, ts)     AS cursor_user_id
    FROM stated
    WHERE ts <= now() - toIntervalSecond(cfg_seal_lag_seconds())
    GROUP BY sid )
  -- Assemble: COMMITTED (prior intervals clipped at the old cursor, immutable)
  -- ++ freshly-derived TAIL. Emit only sessions that have tail events this
  -- cycle (INNER JOIN tail_islands); sessions with no new events keep their
  -- prior row untouched under APPEND.
  SELECT t.sid AS video_session_id,
         arraySort(s -> s.1, arrayConcat(
           -- committed: islands that began strictly before the old boundary,
           -- clipped to end at it. Anything at/after the boundary is
           -- reproduced by the seed+tail, so it's dropped here to avoid a
           -- stale copy (the seed re-covers a boundary-touching island).
           arrayFilter(a -> a.1 < c.cursor_ts_prev,
             arrayMap(m -> (m.1, least(m.2, c.cursor_ts_prev), m.3, m.4), c.prev_intervals)),
           t.tail_intervals)) AS intervals,
         greatest(t.tail_last_end,
                  arrayReduce('max', arrayPushBack(arrayMap(e -> e.2, c.prev_intervals), toDateTime64(0,3,'UTC')))) AS last_active_end,
         t.content_id AS content_id, t.country AS country,
         -- carry cursor forward; new_cursor is present for any session with an
         -- event <= B (always true once bootstrapped), else keep the prior.
         coalesce(nc.cursor_ts, c.cursor_ts_prev)             AS cursor_ts,
         toInt8(coalesce(nc.cursor_state, c.cursor_state_prev)) AS cursor_state,
         coalesce(nc.cursor_platform, c.cursor_platform_prev) AS cursor_platform,
         coalesce(nc.cursor_user_id, c.cursor_user_prev)      AS cursor_user_id
  FROM tail_islands t
  INNER JOIN cursor c    ON t.sid = c.sid
  LEFT  JOIN new_cursor nc ON t.sid = nc.sid
) AS sess
LEFT JOIN sonyliv_concurrency.content_dim AS cd FINAL USING (content_id);

-- =====================================================================
-- D2b. mv_session_intervals_resync — late-data SELF-HEAL (safety net).
-- Full re-derivation (reads all events for each recent session, the OLD
-- O(D) cost) over a WIDER window, at a LOW cadence. APPENDs superseding
-- rows so any drift from a late-past-horizon event is corrected, and
-- resets the cursor to the authoritative full-derivation state so the
-- incremental path continues cleanly from it. This is the documented
-- residual cost of the design (PLAN §9a) — bounded by cadence.
-- =====================================================================
CREATE MATERIALIZED VIEW sonyliv_concurrency.mv_session_intervals_resync
REFRESH EVERY 5 MINUTE APPEND TO sonyliv_concurrency.session_intervals EMPTY AS
SELECT sess.video_session_id AS video_session_id,
       sess.intervals AS intervals,
       toUInt8(sess.last_active_end >= now() - toIntervalSecond(cfg_gap_timeout_seconds())) AS is_provisional,
       sess.content_id AS content_id, sess.country AS country,
       cd.video_type AS video_type, cd.category AS category, cd.title AS title,
       toUnixTimestamp64Milli(now64(3)) AS version,
       sess.cursor_ts AS cursor_ts, sess.cursor_state AS cursor_state,
       sess.cursor_platform AS cursor_platform, sess.cursor_user_id AS cursor_user_id
FROM
(
  WITH
  recent AS (
    SELECT video_session_id FROM sonyliv_concurrency.session_last_seen
    WHERE last_ts >= now() - INTERVAL 30 MINUTE ),        -- wider than the fast path
  per_event AS (
    SELECT video_session_id AS sid, user_id, event_timestamp AS ts, content_id, platform, country,
      multiIf(event_type IN ('VideoSessionStart','VideoPlay','AppForegrounded') OR event IN ('resume','speed-resume','AdResume'), 1,
              event_type IN ('AppBackgrounded','VideoSessionEnd','VideoError','VideoPause','AdBreakStart') OR event IN ('pause','speed-pause','AdPause'), -1,
              0) AS transition
    FROM sonyliv_concurrency.events_raw
    WHERE video_session_id IN (SELECT video_session_id FROM recent) ),  -- FULL history per session
  collapsed AS (
    SELECT sid, ts, if(min(transition) < 0, toInt8(-1), toInt8(max(transition))) AS transition,
           any(user_id) AS user_id, any(content_id) AS content_id, any(platform) AS platform, any(country) AS country
    FROM per_event GROUP BY sid, ts ),
  stated AS (
    SELECT sid, ts, user_id, content_id, platform, country,
      argMax(transition, if(transition!=0, ts, toDateTime64('1970-01-01 00:00:00',3,'UTC')))
        OVER (PARTITION BY sid ORDER BY ts ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS state_sign,
      row_number() OVER (PARTITION BY sid ORDER BY ts) AS rn,
      count()      OVER (PARTITION BY sid)             AS n,
      leadInFrame(ts) OVER (PARTITION BY sid ORDER BY ts ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS next_ts
    FROM collapsed ),
  segments AS (
    SELECT sid, user_id, content_id, platform, country, ts AS seg_start,
      multiIf(rn=n, addSeconds(ts, cfg_heartbeat_seconds()),
              dateDiff('second', ts, next_ts) <= cfg_gap_timeout_seconds(), next_ts,
              addSeconds(ts, cfg_heartbeat_seconds())) AS seg_end
    FROM stated WHERE state_sign = 1 ),
  islands AS (
    SELECT *, if(seg_start > max(seg_end) OVER (PARTITION BY sid ORDER BY seg_start
               ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
               OR platform != lagInFrame(platform) OVER (PARTITION BY sid ORDER BY seg_start
               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
               OR user_id != lagInFrame(user_id) OVER (PARTITION BY sid ORDER BY seg_start
               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW), 1, 0) AS new_island
    FROM segments ),
  per_island AS (
    SELECT sid, island_id, min(seg_start) AS istart, max(seg_end) AS iend,
           any(user_id) AS user_id, any(content_id) AS content_id,
           any(platform) AS platform, any(country) AS country
    FROM (SELECT *, sum(new_island) OVER (PARTITION BY sid ORDER BY seg_start
               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS island_id FROM islands)
    GROUP BY sid, island_id HAVING iend > istart ),
  -- authoritative cursor from the full derivation (last event ts <= B).
  new_cursor AS (
    SELECT sid, max(ts) AS cursor_ts, argMax(state_sign, ts) AS cursor_state,
           argMax(platform, ts) AS cursor_platform, argMax(user_id, ts) AS cursor_user_id
    FROM stated
    WHERE ts <= now() - toIntervalSecond(cfg_seal_lag_seconds())
    GROUP BY sid )
  SELECT pi.sid AS video_session_id,
         arraySort(iv -> iv.1, groupArray((istart, iend, platform, user_id))) AS intervals,
         max(iend) AS last_active_end,
         any(pi.content_id) AS content_id, any(pi.country) AS country,
         coalesce(any(nc.cursor_ts), toDateTime64(0,3,'UTC'))  AS cursor_ts,
         toInt8(coalesce(any(nc.cursor_state), 0))             AS cursor_state,
         coalesce(any(nc.cursor_platform), '')                 AS cursor_platform,
         coalesce(any(nc.cursor_user_id), '')                  AS cursor_user_id
  FROM per_island pi
  LEFT JOIN new_cursor nc ON pi.sid = nc.sid
  GROUP BY pi.sid
) AS sess
LEFT JOIN sonyliv_concurrency.content_dim AS cd FINAL USING (content_id);

-- =====================================================================
-- D3 / D4 — hot + cold serving MVs. UNCHANGED logic from 002 (they read
-- the interval tuple, which is unchanged), recreated here because they were
-- dropped above to release the DEPENDS ON chain.
-- =====================================================================
CREATE MATERIALIZED VIEW sonyliv_concurrency.concurrency_hot_abs_mv
REFRESH EVERY 30 SECOND DEPENDS ON sonyliv_concurrency.mv_session_intervals
TO sonyliv_concurrency.concurrency_hot_abs EMPTY AS
SELECT country, platform, video_type, category, minute, content_id,
       toUInt32(uniqExact(video_session_id)) AS concurrent,
       toUInt32(uniqExact(user_id))          AS concurrent_users
FROM (
  SELECT video_session_id, user_id, country, platform, video_type, category, content_id,
         toStartOfInterval(active_start, toIntervalSecond(cfg_bucket_seconds()))
           + toIntervalSecond(number * cfg_bucket_seconds()) AS minute
  FROM (
    SELECT video_session_id, country, video_type, category, content_id,
           iv.1 AS active_start, iv.2 AS active_end, iv.3 AS platform, iv.4 AS user_id
    FROM sonyliv_concurrency.session_intervals FINAL
    ARRAY JOIN intervals AS iv
    WHERE iv.2 > iv.1
      AND iv.2 >= toStartOfInterval(now(), toIntervalSecond(cfg_bucket_seconds())) - INTERVAL 10 MINUTE
  )
  ARRAY JOIN range(0, toUInt64(dateDiff('second',
                 toStartOfInterval(active_start, toIntervalSecond(cfg_bucket_seconds())),
                 toStartOfInterval(active_end - INTERVAL 1 MILLISECOND, toIntervalSecond(cfg_bucket_seconds())))
                 / cfg_bucket_seconds()) + 1) AS number
)
WHERE minute > toStartOfInterval(now(), toIntervalSecond(cfg_bucket_seconds())) - toIntervalSecond(cfg_hot_window_seconds())
GROUP BY country, platform, video_type, category, minute, content_id;

CREATE MATERIALIZED VIEW sonyliv_concurrency.mv_cold_compaction
REFRESH EVERY 1 MINUTE DEPENDS ON sonyliv_concurrency.concurrency_hot_abs_mv
APPEND
TO sonyliv_concurrency.concurrency_cold_abs EMPTY AS
SELECT country, platform, video_type, category, minute, content_id,
       toUInt32(uniqExact(video_session_id)) AS concurrent,
       toUInt32(uniqExact(user_id))          AS concurrent_users
FROM (
  SELECT video_session_id, user_id, country, platform, video_type, category, content_id,
         toStartOfInterval(active_start, toIntervalSecond(cfg_bucket_seconds()))
           + toIntervalSecond(number * cfg_bucket_seconds()) AS minute
  FROM (
    SELECT video_session_id, country, video_type, category, content_id,
           iv.1 AS active_start, iv.2 AS active_end, iv.3 AS platform, iv.4 AS user_id
    FROM sonyliv_concurrency.session_intervals FINAL
    ARRAY JOIN intervals AS iv
    WHERE iv.2 > iv.1
      AND iv.2 <= toStartOfInterval(now(), toIntervalSecond(cfg_bucket_seconds())) - INTERVAL 10 MINUTE
  )
  ARRAY JOIN range(0, toUInt64(dateDiff('second',
                 toStartOfInterval(active_start, toIntervalSecond(cfg_bucket_seconds())),
                 toStartOfInterval(active_end - INTERVAL 1 MILLISECOND, toIntervalSecond(cfg_bucket_seconds())))
                 / cfg_bucket_seconds()) + 1) AS number
)
WHERE minute <= toStartOfInterval(now(), toIntervalSecond(cfg_bucket_seconds())) - INTERVAL 10 MINUTE
  AND minute >  coalesce((SELECT max(minute) FROM sonyliv_concurrency.concurrency_cold_abs), toDateTime(0))
GROUP BY country, platform, video_type, category, minute, content_id;
