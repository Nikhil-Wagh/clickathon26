-- [READ — serve] not a pipeline step; higher-value DERIVED insight queries (run explicitly).
-- #####################################################################
-- insights.sql — derived / higher-value metrics beyond peak & average.
--
-- WHY this file exists (GAP_ANALYSIS §E): peak/avg concurrency answer "how
-- many are watching", but the same serving layer + events_raw support much
-- higher-value derived metrics at ~zero extra storage. This file is the
-- second ad-hoc READ tool alongside ui_queries.sql / tuning_variants.sql
-- (all unnumbered — they are not pipeline build steps). Nothing here writes a
-- table; every query reads concurrency_now (foreground-active serving view)
-- and/or events_raw (raw event stream) at query time.
--
-- CONVENTIONS (identical to ui_queries.sql, so tiles/curves stay consistent):
--   * ALL params are lenient strings; EMPTY '' = "all". Pass EVERY param.
--   * Standard 5-dim filter block on every concurrency_now / events_raw read:
--       AND (platform   = {platform:String}   OR {platform:String}   = '')
--       AND (country    = {country:String}     OR {country:String}    = '')
--       AND (video_type = {video_type:String}  OR {video_type:String} = '')
--       AND (category   = {category:String}    OR {category:String}  = '')
--       AND (content_id = toInt64OrZero({content_id:String}) OR toInt64OrZero({content_id:String}) = 0)
--     events_raw carries platform/country/content_id directly but NOT
--     video_type/category (content-level dims) — LEFT JOIN content_dim FINAL
--     to apply those, exactly as ui_queries.sql does.
--   * Bucket width is the configurable knob cfg_bucket_seconds() (00_config.sql),
--     NOT a hardcoded minute — the serving column is named `minute` but holds a
--     bucket start. WITH FILL steps by that same width so gaps render as true
--     zero buckets (a drop to zero is itself signal for these metrics).
--   * content_id uses toInt64OrZero (not toUInt64OrZero): the catalog has a
--     negative sentinel content_id an unsigned parse would silently zero.
--
-- METRICS
--   1) Attention / foreground ratio   — foreground-active ÷ open sessions
--   2) Concurrency ramp velocity      — Δconcurrent per bucket (surge/decline)
--   3) Join vs. leave net flow        — arrivals − departures + running open
--   4) Ad-break drop-off / resume     — windowFunnel over AdBreakStart→AdResume
--   5) Retention curve + QoE overlay  — % of peak + error/rebuffer correlation
-- #####################################################################


-- =====================================================================
-- 1) ATTENTION / FOREGROUND RATIO  (foreground-active ÷ open sessions)
-- ---------------------------------------------------------------------
-- The single most persuasive number for a foreground-only project: of every
-- session that is OPEN (started, not yet ended — including paused/backgrounded
-- time), what fraction is actually FOREGROUND-ACTIVE this bucket?
--   attention_ratio = foreground_concurrent / open_sessions   (0..1)
-- foreground_concurrent = concurrency_now (the whole project's output).
-- open_sessions         = distinct sessions whose lifespan [first event,
--   last event + heartbeat grace) covers the bucket — i.e. the "naive session
--   count" that overcounts paused/backgrounded viewers. (1 − ratio) is exactly
--   the overcount the project avoids; B8 measures the raw delta, this normalizes
--   it to a rate over time.
-- =====================================================================
WITH
  -- assumeNotNull: WITH FILL FROM/TO rejects a Nullable bound, and
  -- coalesce(parseDateTimeBestEffortOrNull(...), scalar-subquery) is Nullable-typed
  -- even though it folds to a constant. Both fallbacks make NULL unreachable here.
  assumeNotNull(coalesce(parseDateTimeBestEffortOrNull({from:String},'UTC'), (SELECT min(minute) FROM sonyliv_concurrency.concurrency_now))) AS from_ts,
  assumeNotNull(coalesce(parseDateTimeBestEffortOrNull({to:String},'UTC'),   (SELECT max(minute) FROM sonyliv_concurrency.concurrency_now))) AS to_ts,
  -- foreground-active per bucket (the serving view = this project's output)
  fg AS (
    SELECT minute, sum(concurrent) AS foreground
    FROM sonyliv_concurrency.concurrency_now
    WHERE minute BETWEEN from_ts AND to_ts
      AND (platform  = {platform:String}   OR {platform:String}   = '')
      AND (country   = {country:String}    OR {country:String}    = '')
      AND (video_type= {video_type:String} OR {video_type:String} = '')
      AND (category  = {category:String}   OR {category:String}  = '')
      AND (content_id= toInt64OrZero({content_id:String}) OR toInt64OrZero({content_id:String}) = 0)
    GROUP BY minute
  ),
  -- open sessions per bucket: expand each session's lifespan to every bucket it
  -- touches (same half-open-end / bucket idiom as the build files). "Open" spans
  -- the WHOLE session incl. paused/backgrounded time, so this is >= foreground.
  open AS (
    SELECT minute, uniqExact(video_session_id) AS open_sessions
    FROM (
      SELECT video_session_id,
             toStartOfInterval(span_start, toIntervalSecond(cfg_bucket_seconds()))
               + toIntervalSecond(number * cfg_bucket_seconds()) AS minute
      FROM (
        SELECT e.video_session_id AS video_session_id,
               min(e.event_timestamp) AS span_start,
               -- + heartbeat grace so a single-event session still occupies its bucket
               addSeconds(max(e.event_timestamp), cfg_heartbeat_seconds()) AS span_end
        FROM sonyliv_concurrency.events_raw AS e
        LEFT JOIN sonyliv_concurrency.content_dim AS cd FINAL USING (content_id)
        WHERE (e.platform  = {platform:String}    OR {platform:String}    = '')
          AND (e.country   = {country:String}     OR {country:String}     = '')
          AND (cd.video_type = {video_type:String} OR {video_type:String} = '')
          AND (cd.category   = {category:String}   OR {category:String}  = '')
          AND (e.content_id = toInt64OrZero({content_id:String}) OR toInt64OrZero({content_id:String}) = 0)
        GROUP BY video_session_id
      )
      ARRAY JOIN range(0, toUInt64(dateDiff('second',
                     toStartOfInterval(span_start, toIntervalSecond(cfg_bucket_seconds())),
                     toStartOfInterval(span_end - INTERVAL 1 MILLISECOND, toIntervalSecond(cfg_bucket_seconds())))
                     / cfg_bucket_seconds()) + 1) AS number
    )
    WHERE minute BETWEEN from_ts AND to_ts
    GROUP BY minute
  )
SELECT o.minute AS minute,
       coalesce(f.foreground, 0) AS foreground,
       o.open_sessions           AS open_sessions,
       -- 0..1; nullIf guards the empty-bucket divide. High = attentive audience.
       round(coalesce(f.foreground, 0) / nullIf(o.open_sessions, 0), 3) AS attention_ratio
FROM open AS o
LEFT JOIN fg AS f ON o.minute = f.minute
ORDER BY minute WITH FILL FROM from_ts TO to_ts + toIntervalSecond(cfg_bucket_seconds()) STEP toIntervalSecond(cfg_bucket_seconds());


-- =====================================================================
-- 2) CONCURRENCY RAMP VELOCITY  (Δconcurrent per bucket)
-- ---------------------------------------------------------------------
-- The live-sport kickoff/toss surge and the post-event decline, quantified:
-- first difference of the concurrency curve. Positive spikes = the autoscale
-- trigger; sustained negative = concurrency decline (the §D LLM use-case input).
-- Curve is densified (WITH FILL) INSIDE the CTE first so a bucket that drops to
-- zero produces a real -N delta instead of being skipped (same reason q5 in
-- ui_queries.sql densifies before averaging).
-- =====================================================================
WITH
  -- assumeNotNull: WITH FILL FROM/TO rejects a Nullable bound, and
  -- coalesce(parseDateTimeBestEffortOrNull(...), scalar-subquery) is Nullable-typed
  -- even though it folds to a constant. Both fallbacks make NULL unreachable here.
  assumeNotNull(coalesce(parseDateTimeBestEffortOrNull({from:String},'UTC'), (SELECT min(minute) FROM sonyliv_concurrency.concurrency_now))) AS from_ts,
  assumeNotNull(coalesce(parseDateTimeBestEffortOrNull({to:String},'UTC'),   (SELECT max(minute) FROM sonyliv_concurrency.concurrency_now))) AS to_ts,
  curve AS (
    SELECT minute, sum(concurrent) AS c
    FROM sonyliv_concurrency.concurrency_now
    WHERE minute BETWEEN from_ts AND to_ts
      AND (platform  = {platform:String}   OR {platform:String}   = '')
      AND (country   = {country:String}    OR {country:String}    = '')
      AND (video_type= {video_type:String} OR {video_type:String} = '')
      AND (category  = {category:String}   OR {category:String}  = '')
      AND (content_id= toInt64OrZero({content_id:String}) OR toInt64OrZero({content_id:String}) = 0)
    GROUP BY minute
    ORDER BY minute WITH FILL FROM from_ts TO to_ts + toIntervalSecond(cfg_bucket_seconds()) STEP toIntervalSecond(cfg_bucket_seconds())
  )
SELECT minute,
       toInt64(c) AS concurrency,
       -- Δ vs previous bucket. lagInFrame over the ordered curve (same idiom as
       -- the island detector in 01_schema.sql). First row's prev is 0.
       toInt64(c) - toInt64(lagInFrame(c) OVER (ORDER BY minute ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)) AS ramp_delta,
       -- normalized to per-MINUTE so the number is comparable across bucket widths
       round((toInt64(c) - toInt64(lagInFrame(c) OVER (ORDER BY minute ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)))
             * 60.0 / cfg_bucket_seconds(), 1) AS ramp_per_minute,
       -- % change vs previous bucket (nullIf guards the 0 -> N ramp-from-empty)
       round(100.0 * (toInt64(c) - toInt64(lagInFrame(c) OVER (ORDER BY minute ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)))
             / nullIf(lagInFrame(c) OVER (ORDER BY minute ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW), 0), 1) AS ramp_pct
FROM curve
ORDER BY minute;


-- =====================================================================
-- 3) JOIN vs. LEAVE NET FLOW  (arrivals − departures, + running open)
-- ---------------------------------------------------------------------
-- ui_queries.sql q3 already emits sessions started/ended per bucket; this
-- surfaces the DERIVED signals: net_flow = arrivals − departures (leading
-- indicator of a ramp/decline before the level curve moves), and a running
-- cumulative of net_flow as an independent cross-check of "sessions open".
-- events_raw doesn't carry video_type/category → LEFT JOIN content_dim.
-- =====================================================================
WITH
  coalesce(parseDateTimeBestEffortOrNull({from:String},'UTC'), (SELECT min(event_timestamp) FROM sonyliv_concurrency.events_raw)) AS from_ts,
  coalesce(parseDateTimeBestEffortOrNull({to:String},'UTC'),   (SELECT max(event_timestamp) FROM sonyliv_concurrency.events_raw)) AS to_ts
SELECT minute,
       toInt64(started) AS sessions_started,
       toInt64(ended)   AS sessions_ended,
       toInt64(started) - toInt64(ended) AS net_flow,
       -- running sum of net flow ≈ concurrently-open sessions (independent of the
       -- serving tiers — a coarse cross-check that arrivals/departures balance).
       sum(toInt64(started) - toInt64(ended)) OVER (ORDER BY minute ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cumulative_open
FROM (
  SELECT toStartOfInterval(e.event_timestamp, toIntervalSecond(cfg_bucket_seconds())) AS minute,
         uniqExactIf(e.video_session_id, e.event_type = 'VideoSessionStart') AS started,
         uniqExactIf(e.video_session_id, e.event_type = 'VideoSessionEnd')   AS ended
  FROM sonyliv_concurrency.events_raw AS e
  LEFT JOIN sonyliv_concurrency.content_dim AS cd FINAL USING (content_id)
  WHERE e.event_timestamp BETWEEN from_ts AND to_ts
    AND (e.platform  = {platform:String}    OR {platform:String}    = '')
    AND (e.country   = {country:String}     OR {country:String}     = '')
    AND (cd.video_type = {video_type:String} OR {video_type:String} = '')
    AND (cd.category   = {category:String}   OR {category:String}  = '')
    AND (e.content_id = toInt64OrZero({content_id:String}) OR toInt64OrZero({content_id:String}) = 0)
  GROUP BY minute
)
ORDER BY minute;


-- =====================================================================
-- 4) AD-BREAK DROP-OFF / RESUME RATE  (windowFunnel: AdBreakStart → AdResume)
-- ---------------------------------------------------------------------
-- Directly informs the ad-load decisions the problem names: of sessions that
-- ENTERED an ad break, what fraction RESUMED content afterwards vs dropped off?
-- Ad markers ride in the `event` column ("the actual event", dataset_details.md)
-- and/or the event_type — matched on BOTH so a marker can't slip through
-- (same robustness rule the state machine uses for pause).
--   windowFunnel(window)(ts, step1, step2) returns the longest achieved chain:
--     0 = no ad break · 1 = entered ad break, never resumed (DROP-OFF) ·
--     2 = entered ad break AND resumed content.
-- Window (seconds) is a param so you can size it to your ad-pod length.
-- =====================================================================
WITH
  coalesce(parseDateTimeBestEffortOrNull({from:String},'UTC'), (SELECT min(event_timestamp) FROM sonyliv_concurrency.events_raw)) AS from_ts,
  coalesce(parseDateTimeBestEffortOrNull({to:String},'UTC'),   (SELECT max(event_timestamp) FROM sonyliv_concurrency.events_raw)) AS to_ts,
  per_session AS (
    SELECT e.video_session_id AS video_session_id,
           -- toDateTime(...) so the window is in SECONDS: windowFunnel's window unit
           -- follows the timestamp column, and event_timestamp is DateTime64(3) (ms) —
           -- passing it raw would make {ad_window_seconds} mean milliseconds.
           windowFunnel(toUInt64OrZero({ad_window_seconds:String}) + if({ad_window_seconds:String} = '', 1800, 0))(toDateTime(e.event_timestamp),
             e.event = 'AdBreakStart' OR e.event = 'AdPause' OR e.event_type = 'AdBreakStart',
             e.event = 'AdResume'     OR e.event_type = 'AdResume') AS ad_level
    FROM sonyliv_concurrency.events_raw AS e
    LEFT JOIN sonyliv_concurrency.content_dim AS cd FINAL USING (content_id)
    WHERE e.event_timestamp BETWEEN from_ts AND to_ts
      AND (e.platform  = {platform:String}    OR {platform:String}    = '')
      AND (e.country   = {country:String}     OR {country:String}     = '')
      AND (cd.video_type = {video_type:String} OR {video_type:String} = '')
      AND (cd.category   = {category:String}   OR {category:String}  = '')
      AND (e.content_id = toInt64OrZero({content_id:String}) OR toInt64OrZero({content_id:String}) = 0)
    GROUP BY e.video_session_id
  )
SELECT countIf(ad_level >= 1) AS ad_break_sessions,
       countIf(ad_level >= 2) AS resumed_after_ad,
       countIf(ad_level  = 1) AS dropped_during_ad,
       round(100.0 * countIf(ad_level >= 2) / nullIf(countIf(ad_level >= 1), 0), 1) AS ad_resume_rate_pct,
       round(100.0 * countIf(ad_level  = 1) / nullIf(countIf(ad_level >= 1), 0), 1) AS ad_dropoff_rate_pct
FROM per_session;


-- =====================================================================
-- 5) RETENTION CURVE + QoE OVERLAY  (% of peak, error/rebuffer correlation)
-- ---------------------------------------------------------------------
-- 5a) RETENTION: concurrency as % of its own peak over the range — the live-
--     event decay/retention shape, normalized so different events overlay on
--     one axis. pct_of_peak = 100 when the curve is at its max.
-- =====================================================================
WITH
  -- assumeNotNull: WITH FILL FROM/TO rejects a Nullable bound, and
  -- coalesce(parseDateTimeBestEffortOrNull(...), scalar-subquery) is Nullable-typed
  -- even though it folds to a constant. Both fallbacks make NULL unreachable here.
  assumeNotNull(coalesce(parseDateTimeBestEffortOrNull({from:String},'UTC'), (SELECT min(minute) FROM sonyliv_concurrency.concurrency_now))) AS from_ts,
  assumeNotNull(coalesce(parseDateTimeBestEffortOrNull({to:String},'UTC'),   (SELECT max(minute) FROM sonyliv_concurrency.concurrency_now))) AS to_ts,
  curve AS (
    SELECT minute, sum(concurrent) AS c
    FROM sonyliv_concurrency.concurrency_now
    WHERE minute BETWEEN from_ts AND to_ts
      AND (platform  = {platform:String}   OR {platform:String}   = '')
      AND (country   = {country:String}    OR {country:String}    = '')
      AND (video_type= {video_type:String} OR {video_type:String} = '')
      AND (category  = {category:String}   OR {category:String}  = '')
      AND (content_id= toInt64OrZero({content_id:String}) OR toInt64OrZero({content_id:String}) = 0)
    GROUP BY minute
    ORDER BY minute WITH FILL FROM from_ts TO to_ts + toIntervalSecond(cfg_bucket_seconds()) STEP toIntervalSecond(cfg_bucket_seconds())
  )
SELECT minute, toInt64(c) AS concurrency,
       round(100.0 * c / nullIf(max(c) OVER (), 0), 1) AS pct_of_peak
FROM curve
ORDER BY minute;

-- ---------------------------------------------------------------------
-- 5b) QoE OVERLAY: foreground concurrency vs errors & rebuffer events per
--     bucket — a QoE proxy overlaid on the curve. A concurrency dip that
--     coincides with an error/rebuffer spike is TECHNICAL (§D "technical"
--     classification); a dip with no such spike is disengagement/asset-end.
--       errors          = VideoError events (event_type)
--       rebuffer_events = buffering stalls (event='speed-pause', the buffering
--                         toggle PLAN §9 keeps on the `event` value)
--       errored_sessions= distinct sessions that hit an error this bucket
--     Rates normalize by foreground concurrency so a spike isn't just "more
--     viewers" (errors_per_100_active). FULL JOIN so an all-error bucket with
--     no serving row (or vice-versa) still appears.
-- ---------------------------------------------------------------------
WITH
  -- assumeNotNull: WITH FILL FROM/TO rejects a Nullable bound, and
  -- coalesce(parseDateTimeBestEffortOrNull(...), scalar-subquery) is Nullable-typed
  -- even though it folds to a constant. Both fallbacks make NULL unreachable here.
  assumeNotNull(coalesce(parseDateTimeBestEffortOrNull({from:String},'UTC'), (SELECT min(minute) FROM sonyliv_concurrency.concurrency_now))) AS from_ts,
  assumeNotNull(coalesce(parseDateTimeBestEffortOrNull({to:String},'UTC'),   (SELECT max(minute) FROM sonyliv_concurrency.concurrency_now))) AS to_ts,
  fg AS (
    SELECT minute, sum(concurrent) AS c
    FROM sonyliv_concurrency.concurrency_now
    WHERE minute BETWEEN from_ts AND to_ts
      AND (platform  = {platform:String}   OR {platform:String}   = '')
      AND (country   = {country:String}    OR {country:String}    = '')
      AND (video_type= {video_type:String} OR {video_type:String} = '')
      AND (category  = {category:String}   OR {category:String}  = '')
      AND (content_id= toInt64OrZero({content_id:String}) OR toInt64OrZero({content_id:String}) = 0)
    GROUP BY minute
  ),
  qoe AS (
    SELECT toStartOfInterval(e.event_timestamp, toIntervalSecond(cfg_bucket_seconds())) AS minute,
           countIf(e.event_type = 'VideoError')                        AS errors,
           countIf(e.event = 'speed-pause')                            AS rebuffer_events,
           uniqExactIf(e.video_session_id, e.event_type = 'VideoError') AS errored_sessions
    FROM sonyliv_concurrency.events_raw AS e
    LEFT JOIN sonyliv_concurrency.content_dim AS cd FINAL USING (content_id)
    WHERE e.event_timestamp BETWEEN from_ts AND to_ts
      AND (e.platform  = {platform:String}    OR {platform:String}    = '')
      AND (e.country   = {country:String}     OR {country:String}     = '')
      AND (cd.video_type = {video_type:String} OR {video_type:String} = '')
      AND (cd.category   = {category:String}   OR {category:String}  = '')
      AND (e.content_id = toInt64OrZero({content_id:String}) OR toInt64OrZero({content_id:String}) = 0)
    GROUP BY minute
  )
SELECT coalesce(fg.minute, qoe.minute) AS minute,
       toInt64(coalesce(fg.c, 0))               AS concurrency,
       toInt64(coalesce(qoe.errors, 0))         AS errors,
       toInt64(coalesce(qoe.rebuffer_events, 0)) AS rebuffer_events,
       toInt64(coalesce(qoe.errored_sessions, 0)) AS errored_sessions,
       round(100.0 * coalesce(qoe.errored_sessions, 0) / nullIf(coalesce(fg.c, 0), 0), 2) AS errors_per_100_active
FROM fg
FULL OUTER JOIN qoe ON fg.minute = qoe.minute
ORDER BY minute WITH FILL FROM from_ts TO to_ts + toIntervalSecond(cfg_bucket_seconds()) STEP toIntervalSecond(cfg_bucket_seconds());


-- #####################################################################
-- PART II — INDUSTRY-STANDARD QoE / ENGAGEMENT / STICKINESS METRICS
-- ---------------------------------------------------------------------
-- Beyond the concurrency-derived set above, these are the canonical
-- streaming-analytics KPIs (Conviva OTT-101 / VSI dictionary, Mux Data's six
-- top-level metrics, Bitmovin, NPAW) — all computable from THIS event stream
-- (VideoSessionStart/End, VideoPlay, VideoHeartbeat, VideoError, the ad/pause
-- markers in `event`, + timestamps). They explain REALIZED vs. POTENTIAL peak
-- concurrency: a kickoff surge is capped by start failures, startup latency and
-- rebuffering, so overlaying these on the concurrency curve turns "how many
-- watched" into "where we lost the audience". Same filter/param conventions.
-- Refs: conviva.ai/resource/ott-101-your-guide-to-streaming-metrics-that-matter,
--       mux.com/docs/guides/data/understand-metric-definitions.
--
-- EXECUTED on ClickHouse Cloud (2026-08-02, full loaded range, all dims). The
-- data carries real startup latency and failures, so these are live numbers, not
-- placeholders: VST avg ~7.9s / p95 ~23.7s / p99 ~37.7s (§7); playback-success
-- ~79.7%, EBVS ~2.1%, VPF ~20.7% (§6); rebuffering ratio ~1.62% (§8); ad-resume
-- ~76.2% / drop-off ~23.8% (§4). Magnitudes will differ per sealed day.
-- #####################################################################


-- =====================================================================
-- 6) STARTUP & RELIABILITY FUNNEL  (attempts → plays; VSF / EBVS / VPF)
-- ---------------------------------------------------------------------
-- The Conviva "core" reliability KPIs, per session then aggregated:
--   VSF  Video Start Failure   — started, a fatal error, never reached playback.
--   EBVS Exit Before Video Start— started, NO error, abandoned before playback.
--   VPF  Video Playback Failure — reached playback, then a fatal error.
--   Play Rate                   — plays / attempts (intent → actual viewing).
--   Playback Success Rate       — 1 − (VSF+VPF)/attempts (all errors are one or
--                                 the other, split by whether playback started).
-- "Playback started" = a VideoPlay OR any VideoHeartbeat (heartbeats fire only
-- while playing). VSF (no play) and VPF (play) are disjoint by that flag.
-- =====================================================================
WITH
  coalesce(parseDateTimeBestEffortOrNull({from:String},'UTC'), (SELECT min(event_timestamp) FROM sonyliv_concurrency.events_raw)) AS from_ts,
  coalesce(parseDateTimeBestEffortOrNull({to:String},'UTC'),   (SELECT max(event_timestamp) FROM sonyliv_concurrency.events_raw)) AS to_ts,
  per_session AS (
    SELECT e.video_session_id AS video_session_id,
           max(e.event_type = 'VideoSessionStart')                    AS has_start,
           max(e.event_type IN ('VideoPlay','VideoHeartbeat'))        AS has_play,
           max(e.event_type = 'VideoError')                           AS has_error
    FROM sonyliv_concurrency.events_raw AS e
    LEFT JOIN sonyliv_concurrency.content_dim AS cd FINAL USING (content_id)
    WHERE e.event_timestamp BETWEEN from_ts AND to_ts
      AND (e.platform  = {platform:String}    OR {platform:String}    = '')
      AND (e.country   = {country:String}     OR {country:String}     = '')
      AND (cd.video_type = {video_type:String} OR {video_type:String} = '')
      AND (cd.category   = {category:String}   OR {category:String}  = '')
      AND (e.content_id = toInt64OrZero({content_id:String}) OR toInt64OrZero({content_id:String}) = 0)
    GROUP BY e.video_session_id
  )
SELECT count()                                            AS attempts,
       countIf(has_play)                                  AS plays,
       countIf(NOT has_play AND has_error)                AS video_start_failures,   -- VSF
       countIf(NOT has_play AND NOT has_error)            AS exit_before_start,       -- EBVS
       countIf(has_play AND has_error)                    AS playback_failures,       -- VPF
       round(100.0 * countIf(has_play)                / nullIf(count(), 0), 1)          AS play_rate_pct,
       round(100.0 * countIf(NOT has_play AND has_error)     / nullIf(count(), 0), 2)   AS vsf_pct,
       round(100.0 * countIf(NOT has_play AND NOT has_error) / nullIf(count(), 0), 2)   AS ebvs_pct,
       round(100.0 * countIf(has_play AND has_error)    / nullIf(countIf(has_play), 0), 2) AS vpf_pct,
       round(100.0 * (count() - countIf(has_error))     / nullIf(count(), 0), 2)        AS playback_success_rate_pct
FROM per_session;


-- =====================================================================
-- 7) VIDEO STARTUP TIME (VST / time-to-first-frame)  — P50 / P95 / P99
-- ---------------------------------------------------------------------
-- Seconds from VideoSessionStart to the first VideoPlay. Live audiences are
-- least patient — VST drives kickoff abandonment (EBVS), so it caps realized
-- peak. Report percentiles, not just mean (the P95/P99 tail is the pain).
-- CAVEAT: does not subtract pre-roll ad time (would need first-ad-frame markers),
-- so a pre-roll inflates VST for sessions that play an ad before the first frame.
-- =====================================================================
WITH
  coalesce(parseDateTimeBestEffortOrNull({from:String},'UTC'), (SELECT min(event_timestamp) FROM sonyliv_concurrency.events_raw)) AS from_ts,
  coalesce(parseDateTimeBestEffortOrNull({to:String},'UTC'),   (SELECT max(event_timestamp) FROM sonyliv_concurrency.events_raw)) AS to_ts,
  per_session AS (
    SELECT e.video_session_id AS video_session_id,
           minIf(e.event_timestamp, e.event_type = 'VideoSessionStart') AS start_ts,
           minIf(e.event_timestamp, e.event_type = 'VideoPlay')         AS first_play_ts,
           max(e.event_type = 'VideoSessionStart')                      AS has_start,
           max(e.event_type = 'VideoPlay')                              AS has_play
    FROM sonyliv_concurrency.events_raw AS e
    LEFT JOIN sonyliv_concurrency.content_dim AS cd FINAL USING (content_id)
    WHERE e.event_timestamp BETWEEN from_ts AND to_ts
      AND (e.platform  = {platform:String}    OR {platform:String}    = '')
      AND (e.country   = {country:String}     OR {country:String}     = '')
      AND (cd.video_type = {video_type:String} OR {video_type:String} = '')
      AND (cd.category   = {category:String}   OR {category:String}  = '')
      AND (e.content_id = toInt64OrZero({content_id:String}) OR toInt64OrZero({content_id:String}) = 0)
    GROUP BY e.video_session_id
  )
SELECT count()                                                                   AS sessions_measured,
       round(avg(dateDiff('millisecond', start_ts, first_play_ts)) / 1000, 3)    AS vst_avg_s,
       round(quantile(0.50)(dateDiff('millisecond', start_ts, first_play_ts)) / 1000, 3) AS vst_p50_s,
       round(quantile(0.95)(dateDiff('millisecond', start_ts, first_play_ts)) / 1000, 3) AS vst_p95_s,
       round(quantile(0.99)(dateDiff('millisecond', start_ts, first_play_ts)) / 1000, 3) AS vst_p99_s
FROM per_session
WHERE has_start AND has_play AND first_play_ts >= start_ts;


-- =====================================================================
-- 8) REBUFFERING RATIO  (connection-induced stall time ÷ (stall + play))
-- ---------------------------------------------------------------------
-- The single strongest QoE→engagement correlate. Buffering rides in `event`
-- as 'speed-pause' (the buffering-active toggle, PLAN §9) — distinct from a
-- user 'pause' / 'AdPause'. Stall duration = gap to the session's NEXT event,
-- capped at the gap timeout (00_config.sql) so a stall that runs into a silence
-- doesn't count unbounded. Play time = the project's own foreground-active
-- output (session_intervals), which already EXCLUDES buffering — so the two are
-- disjoint and the ratio is well-formed.
-- =====================================================================
WITH
  rb AS (
    SELECT sum(least(dateDiff('second', ts, next_ts), cfg_gap_timeout_seconds())) AS rebuffer_seconds,
           uniqExact(video_session_id) AS sessions_with_rebuffer
    FROM (
      SELECT e.video_session_id AS video_session_id, e.event AS event, e.event_timestamp AS ts,
             leadInFrame(e.event_timestamp) OVER (PARTITION BY e.video_session_id ORDER BY e.event_timestamp
                 ROWS BETWEEN CURRENT ROW AND 1 FOLLOWING) AS next_ts
      FROM sonyliv_concurrency.events_raw AS e
      LEFT JOIN sonyliv_concurrency.content_dim AS cd FINAL USING (content_id)
      WHERE (e.platform  = {platform:String}    OR {platform:String}    = '')
        AND (e.country   = {country:String}     OR {country:String}     = '')
        AND (cd.video_type = {video_type:String} OR {video_type:String} = '')
        AND (cd.category   = {category:String}   OR {category:String}  = '')
        AND (e.content_id = toInt64OrZero({content_id:String}) OR toInt64OrZero({content_id:String}) = 0)
    )
    WHERE event = 'speed-pause' AND next_ts > ts
  ),
  pt AS (
    -- foreground play-seconds from the project's own truly-active intervals.
    -- platform rides inside the tuple (iv.3); other dims are session columns.
    SELECT sum(dateDiff('second', iv.1, iv.2)) AS play_seconds
    FROM sonyliv_concurrency.session_intervals FINAL
    ARRAY JOIN intervals AS iv
    WHERE iv.2 > iv.1
      AND (iv.3        = {platform:String}   OR {platform:String}   = '')
      AND (country     = {country:String}     OR {country:String}    = '')
      AND (video_type  = {video_type:String}  OR {video_type:String} = '')
      AND (category    = {category:String}    OR {category:String}  = '')
      AND (content_id  = toInt64OrZero({content_id:String}) OR toInt64OrZero({content_id:String}) = 0)
  )
SELECT rb.rebuffer_seconds                    AS rebuffer_seconds,
       pt.play_seconds                        AS play_seconds,
       rb.sessions_with_rebuffer              AS sessions_with_rebuffer,
       -- Conviva definition: rebuffer / (rebuffer + play). Lower is better.
       round(100.0 * rb.rebuffer_seconds / nullIf(rb.rebuffer_seconds + pt.play_seconds, 0), 3) AS rebuffering_ratio_pct
FROM rb, pt;


-- =====================================================================
-- 9) WATCH-TIME & ENGAGEMENT SUMMARY  (viewer hours, minutes/viewer, peakedness)
-- ---------------------------------------------------------------------
-- Reuses the serving layer for foreground watch-time (sum of concurrent
-- session-buckets × bucket width) and events_raw for distinct sessions/users.
--   viewer_hours              — foreground session-time in hours (monetization base)
--   avg_minutes_per_session   — watch-time ÷ distinct sessions
--   avg_minutes_per_viewer    — watch-time ÷ distinct users
--   peak_to_avg_ratio         — "peakedness": PPV spike (high) vs all-day (low)
--   unique_to_peak_ratio      — reach vs simultaneity (low = everyone together = true live)
--   sessions_per_user         — multi-device / account-sharing signal
-- =====================================================================
WITH
  -- assumeNotNull: WITH FILL FROM/TO rejects a Nullable bound, and
  -- coalesce(parseDateTimeBestEffortOrNull(...), scalar-subquery) is Nullable-typed
  -- even though it folds to a constant. Both fallbacks make NULL unreachable here.
  assumeNotNull(coalesce(parseDateTimeBestEffortOrNull({from:String},'UTC'), (SELECT min(minute) FROM sonyliv_concurrency.concurrency_now))) AS from_ts,
  assumeNotNull(coalesce(parseDateTimeBestEffortOrNull({to:String},'UTC'),   (SELECT max(minute) FROM sonyliv_concurrency.concurrency_now))) AS to_ts,
  curve AS (
    SELECT minute, sum(concurrent) AS c
    FROM sonyliv_concurrency.concurrency_now
    WHERE minute BETWEEN from_ts AND to_ts
      AND (platform  = {platform:String}   OR {platform:String}   = '')
      AND (country   = {country:String}    OR {country:String}    = '')
      AND (video_type= {video_type:String} OR {video_type:String} = '')
      AND (category  = {category:String}   OR {category:String}  = '')
      AND (content_id= toInt64OrZero({content_id:String}) OR toInt64OrZero({content_id:String}) = 0)
    GROUP BY minute
  ),
  agg AS (
    SELECT sum(c) AS session_buckets, max(c) AS peak,
           sum(c) / (dateDiff('second', from_ts, to_ts) / cfg_bucket_seconds() + 1) AS avg_c
    FROM curve
  ),
  aud AS (
    SELECT uniqExact(e.video_session_id) AS sessions, uniqExact(e.user_id) AS users
    FROM sonyliv_concurrency.events_raw AS e
    LEFT JOIN sonyliv_concurrency.content_dim AS cd FINAL USING (content_id)
    WHERE e.event_timestamp BETWEEN from_ts AND to_ts
      AND (e.platform  = {platform:String}    OR {platform:String}    = '')
      AND (e.country   = {country:String}     OR {country:String}     = '')
      AND (cd.video_type = {video_type:String} OR {video_type:String} = '')
      AND (cd.category   = {category:String}   OR {category:String}  = '')
      AND (e.content_id = toInt64OrZero({content_id:String}) OR toInt64OrZero({content_id:String}) = 0)
  )
SELECT round(agg.session_buckets * cfg_bucket_seconds() / 3600.0, 1)                  AS viewer_hours,
       aud.sessions                                                                   AS distinct_sessions,
       aud.users                                                                      AS distinct_users,
       round(agg.session_buckets * cfg_bucket_seconds() / 60.0 / nullIf(aud.sessions, 0), 1) AS avg_minutes_per_session,
       round(agg.session_buckets * cfg_bucket_seconds() / 60.0 / nullIf(aud.users, 0), 1)    AS avg_minutes_per_viewer,
       toInt64(agg.peak)                                                              AS peak_concurrency,
       round(agg.avg_c, 1)                                                            AS avg_concurrency,
       round(agg.peak / nullIf(agg.avg_c, 0), 2)                                      AS peak_to_avg_ratio,
       round(aud.users / nullIf(agg.peak, 0), 2)                                      AS unique_to_peak_ratio,
       round(aud.sessions / nullIf(aud.users, 0), 2)                                  AS sessions_per_user
FROM agg, aud;


-- =====================================================================
-- 10) STICKINESS — DAU / MAU  (audience-loyalty ratio)
-- ---------------------------------------------------------------------
-- Standard product-health KPI: avg daily actives ÷ distinct actives over the
-- whole range. Shows whether an event-driven spike converts to habitual
-- viewing. CAVEAT: meaningful only over MULTI-DAY data — on a single sealed
-- "unseen day" DAU==MAU and stickiness == 1.0 by construction.
-- =====================================================================
WITH
  coalesce(parseDateTimeBestEffortOrNull({from:String},'UTC'), (SELECT min(event_timestamp) FROM sonyliv_concurrency.events_raw)) AS from_ts,
  coalesce(parseDateTimeBestEffortOrNull({to:String},'UTC'),   (SELECT max(event_timestamp) FROM sonyliv_concurrency.events_raw)) AS to_ts,
  daily AS (
    SELECT toDate(e.event_timestamp) AS d, uniqExact(e.user_id) AS dau
    FROM sonyliv_concurrency.events_raw AS e
    LEFT JOIN sonyliv_concurrency.content_dim AS cd FINAL USING (content_id)
    WHERE e.event_timestamp BETWEEN from_ts AND to_ts
      AND (e.platform  = {platform:String}    OR {platform:String}    = '')
      AND (e.country   = {country:String}     OR {country:String}     = '')
      AND (cd.video_type = {video_type:String} OR {video_type:String} = '')
      AND (cd.category   = {category:String}   OR {category:String}  = '')
      AND (e.content_id = toInt64OrZero({content_id:String}) OR toInt64OrZero({content_id:String}) = 0)
    GROUP BY d
  )
SELECT count()                                        AS days_in_range,
       round(avg(dau), 0)                             AS avg_dau,
       max(dau)                                       AS peak_dau,
       (SELECT uniqExact(user_id) FROM sonyliv_concurrency.events_raw
        WHERE event_timestamp BETWEEN from_ts AND to_ts
          AND (platform  = {platform:String}   OR {platform:String}   = '')
          AND (country   = {country:String}    OR {country:String}    = '')
          AND (content_id= toInt64OrZero({content_id:String}) OR toInt64OrZero({content_id:String}) = 0)) AS mau,
       round(avg(dau) / nullIf((SELECT uniqExact(user_id) FROM sonyliv_concurrency.events_raw
        WHERE event_timestamp BETWEEN from_ts AND to_ts
          AND (platform  = {platform:String}   OR {platform:String}   = '')
          AND (country   = {country:String}    OR {country:String}    = '')
          AND (content_id= toInt64OrZero({content_id:String}) OR toInt64OrZero({content_id:String}) = 0)), 0), 3) AS stickiness
FROM daily;
