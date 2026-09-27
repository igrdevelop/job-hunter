-- docs/HUNT_DRILLDOWN_PLAN.md M2 — fixture for the hunts list + one hunt's
-- drill-down (tools/pipeline_snapshot.py hunts_list / hunt_detail).
-- Applied on top of the real schema (hunter.db.init_db + the lazy DDL of
-- hunt_live / hunt_runs / hunt_jobs / generation_runs / pipeline_events).
-- Clock frozen at 2026-09-27T10:00:00+00:00. User scope: 'u1'.
-- job-hunter-api copies this file verbatim into its own contract fixtures.

-- Four hunts: a finished scheduled one with vacancies, one fetching now, a
-- retry pass, and one that failed at the dedup read.
INSERT INTO hunt_live (hunt_id, "trigger", sources, started_at, step, step_started_at,
  current_source, sources_done, sources_total, found_so_far, command_id, finished_at) VALUES
 ('h_done',  'scheduled', '["justjoin","pracuj"]', '2026-09-27T08:00:00+00:00', 'done',
  '2026-09-27T08:01:30+00:00', '', 2, 2, 57, '', '2026-09-27T08:01:30+00:00'),
 ('h_run',   'web',       '["linkedin"]',          '2026-09-27T09:59:00+00:00', 'fetch',
  '2026-09-27T09:59:00+00:00', 'linkedin', 0, 1, 0, 'c1', NULL),
 ('h_retry', 'retry',     '[]',                    '2026-09-27T07:45:00+00:00', 'done',
  '2026-09-27T07:45:10+00:00', '', 0, 0, 0, '', '2026-09-27T07:45:10+00:00'),
 ('h_err',   'manual',    '["justjoin"]',          '2026-09-27T06:00:00+00:00', 'error',
  '2026-09-27T06:00:20+00:00', '', 1, 1, 30, '', '2026-09-27T06:00:20+00:00');

INSERT INTO hunt_runs (ts, "trigger", sources, found, filtered_out, filter_reasons, dup_url,
  dup_ct, dup_cooldown, "new", capped, queued, applied_inline, duration_ms, hunt_id,
  per_source) VALUES
 ('2026-09-27T08:00:00+00:00', 'scheduled', '["justjoin","pracuj"]', 57, 47,
  '{"level":30,"location":17}', 1, 1, 0, 9, 1, 8, 0, 90000, 'h_done',
  '{"justjoin":57,"pracuj":"ERR"}');

-- h_done's eleven filter-passed vacancies, in the order the loop decided them.
INSERT INTO hunt_jobs (hunt_id, ts, url_norm, url, source, title, company, fate, fate_detail) VALUES
 ('h_done','2026-09-27T08:00:00+00:00','ex.com/j1','https://ex.com/j1','justjoin','Angular Dev','Acme','queued',''),
 ('h_done','2026-09-27T08:00:00+00:00','ex.com/j2','https://ex.com/j2','justjoin','Angular Dev','Beta','queued',''),
 ('h_done','2026-09-27T08:00:00+00:00','ex.com/j3','https://ex.com/j3','justjoin','Angular Dev','Gamma','queued',''),
 ('h_done','2026-09-27T08:00:00+00:00','ex.com/j4','https://ex.com/j4','justjoin','Angular Dev','Delta','queued',''),
 ('h_done','2026-09-27T08:00:00+00:00','ex.com/j5','https://ex.com/j5','justjoin','Angular Dev','Eps','queued',''),
 ('h_done','2026-09-27T08:00:00+00:00','ex.com/j6','https://ex.com/j6','justjoin','Angular Dev','Zeta','queued',''),
 ('h_done','2026-09-27T08:00:00+00:00','ex.com/j7','https://ex.com/j7','justjoin','Angular Dev','Eta','queued',''),
 ('h_done','2026-09-27T08:00:00+00:00','ex.com/j9','https://ex.com/j9','justjoin','Angular Dev','Lambda','queued',''),
 ('h_done','2026-09-27T08:00:00+00:00','ex.com/j8','https://ex.com/j8','justjoin','Angular Dev','Theta','capped',''),
 ('h_done','2026-09-27T08:00:00+00:00','ex.com/d1','https://ex.com/d1','justjoin','Angular Dev','Iota','dup_url','tracker'),
 ('h_done','2026-09-27T08:00:00+00:00','ex.com/d2','https://ex.com/d2','justjoin','Angular Dev','Kappa','dup_ct','fuzzy');

-- Tracker rows, in rowid order. p0 (another hunt's vacancy) is first in the
-- queue, so j1 is #2. j7 has no row (a soft abort deleted its placeholder).
INSERT INTO applications (id, date, user_id, company, title, ats_status, url, url_norm, sent,
  queued_at, claimed_at, skip_reason, folder, drive_url, ats_verdict, cost_usd) VALUES
 ('p0','2026-09-27','u1','Older','Angular Dev','PENDING','https://ex.com/p0','ex.com/p0','','2026-09-27T07:00:00Z',NULL,'','','',NULL,NULL),
 ('r1','2026-09-27','u1','Acme','Angular Dev','PENDING','https://ex.com/j1','ex.com/j1','','2026-09-27T08:01:00Z',NULL,'','','',NULL,NULL),
 ('r2','2026-09-27','u1','Beta','Angular Dev','IN_PROGRESS','https://ex.com/j2','ex.com/j2','','2026-09-27T08:01:00Z','2026-09-27T09:40:00Z','','','',NULL,NULL),
 ('r3','2026-09-27','u1','Gamma','Angular Dev','93','https://ex.com/j3','ex.com/j3','','',NULL,'','/app/users/u1/Applications/2026-09-27/Gamma','https://drive.test/g',93,0.42),
 ('r4','2026-09-27','u1','Delta','Angular Dev','90','https://ex.com/j4','ex.com/j4','2026-09-27','',NULL,'','/app/users/u1/Applications/2026-09-27/Delta','',90,0.3),
 ('r5','2026-09-27','u1','Eps','Angular Dev','SKIP','https://ex.com/j5','ex.com/j5','—','',NULL,'doomed:pl_onsite','','',NULL,NULL),
 ('r6','2026-09-27','u1','Zeta','Angular Dev','FAIL','https://ex.com/j6','ex.com/j6','—','',NULL,'','','',NULL,NULL),
 -- generated, then declined by the owner by hand (a dash in Sent): not ready
 ('r9','2026-09-27','u1','Lambda','Angular Dev','88','https://ex.com/j9','ex.com/j9','—','',NULL,'','','',88,NULL),
 ('rd','2026-08-01','u1','Iota','Angular Dev','91','https://ex.com/d1','ex.com/d1','2026-08-02','',NULL,'','','',91,NULL),
 -- another user's row for the capped vacancy must never leak into u1's view
 ('x8','2026-09-27','u2','Theta','Angular Dev','95','https://ex.com/j8','ex.com/j8','','',NULL,'','','',95,NULL);

-- generation_runs: j2 is open (refine round 2 decided), j3 finished,
-- j6 failed; a backfill row for j3 must be ignored.
INSERT INTO generation_runs (run_id, user_id, url_norm, started_at, finished_at, pipeline,
  outcome, verdict_first, verdict_final, refine_rounds, cost_usd) VALUES
 ('g2','u1','ex.com/j2','2026-09-27T09:40:00+00:00',NULL,'api',NULL,85,NULL,NULL,NULL),
 ('g3','u1','ex.com/j3','2026-09-27T08:05:00+00:00','2026-09-27T08:35:00+00:00','api','ok',86,93,2,0.42),
 ('g3b','u1','ex.com/j3','2026-09-27T09:00:00+00:00','2026-09-27T09:00:00+00:00','backfill','ok',NULL,NULL,NULL,NULL),
 ('g6','u1','ex.com/j6','2026-09-27T08:40:00+00:00','2026-09-27T08:42:00+00:00','api','fail',NULL,NULL,NULL,0.05);

INSERT INTO pipeline_events (run_id, ts, stage, event, duration_ms, payload) VALUES
 ('g2','2026-09-27T09:40:00+00:00','fetch','ok',1000,''),
 ('g2','2026-09-27T09:45:00+00:00','verdict','ok',1000,'{"score":85}'),
 ('g2','2026-09-27T09:50:00+00:00','refine','start',NULL,'{"target":95,"max_rounds":5,"verdict_first":85}'),
 ('g2','2026-09-27T09:55:00+00:00','refine','accepted',NULL,'{"round":2,"kind":"honest","score":90,"best":90}');
