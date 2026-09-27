-- Generated from the bot's DDL by tests/test_pipeline_hunts_tool.py (UPDATE_PIPELINE_HUNTS_FIXTURE=1). Never edit by hand.
CREATE TABLE applications (
    id            TEXT    PRIMARY KEY,
    date          TEXT    NOT NULL DEFAULT '',
    user_id       TEXT    NOT NULL DEFAULT '',
    company       TEXT    NOT NULL DEFAULT '',
    title         TEXT    NOT NULL DEFAULT '',
    stack         TEXT    NOT NULL DEFAULT '',
    ats_status    TEXT    NOT NULL DEFAULT '',
    url           TEXT    NOT NULL DEFAULT '',
    url_norm      TEXT    NOT NULL DEFAULT '',
    folder        TEXT    NOT NULL DEFAULT '',
    sent          TEXT    NOT NULL DEFAULT '',
    reapplication TEXT    NOT NULL DEFAULT '',
    to_learn      TEXT    NOT NULL DEFAULT '',
    drive_url     TEXT    NOT NULL DEFAULT '',
    confirmation  TEXT    NOT NULL DEFAULT '',
    answer        TEXT    NOT NULL DEFAULT '',
    sheets_row    INTEGER,
    sheets_dirty  INTEGER NOT NULL DEFAULT 0,
    fail_count    INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL
, ats_verdict REAL, claimed_at TEXT, claimed_by TEXT NOT NULL DEFAULT '', outcome_label TEXT NOT NULL DEFAULT '', outcome_at TEXT, skip_reason TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', pending_meta TEXT, queued_at TEXT);
CREATE TABLE bot_commands (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'pending',
    result      TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT
);
CREATE TABLE generation_runs (
    run_id            TEXT    PRIMARY KEY,
    user_id           TEXT    NOT NULL DEFAULT '',
    url_norm          TEXT    NOT NULL DEFAULT '',
    row_id            TEXT,
    started_at        TEXT,
    finished_at       TEXT,
    pipeline          TEXT    NOT NULL DEFAULT '',
    profile           TEXT    NOT NULL DEFAULT '',
    gen_model         TEXT    NOT NULL DEFAULT '',
    judge_model       TEXT    NOT NULL DEFAULT '',
    track             TEXT    NOT NULL DEFAULT '',
    posting_lang      TEXT    NOT NULL DEFAULT '',
    source            TEXT    NOT NULL DEFAULT '',
    is_manual         INTEGER NOT NULL DEFAULT 0,
    is_force          INTEGER NOT NULL DEFAULT 0,
    ats_pre_score     REAL,
    ats_pre_keyword   REAL,
    ats_pdf_score     REAL,
    verdict_first     REAL,
    verdict_final     REAL,
    refine_rounds     INTEGER,
    refine_accepted   INTEGER,
    best_round_kind   TEXT,
    judge_violations  INTEGER,
    judge_repaired    INTEGER,
    judge_surviving   INTEGER,
    lang_gate_hits    INTEGER,
    lang_gate_blocked INTEGER,
    scrub_fixes       INTEGER,
    reused_donor      TEXT,
    cost_usd          REAL,
    outcome           TEXT,
    exit_code         INTEGER
);
CREATE TABLE hunt_jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    hunt_id      TEXT NOT NULL,
    ts           TEXT NOT NULL,
    url_norm     TEXT NOT NULL DEFAULT '',
    url          TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL DEFAULT '',
    company      TEXT NOT NULL DEFAULT '',
    fate         TEXT NOT NULL,
    fate_detail  TEXT NOT NULL DEFAULT ''
);
CREATE TABLE hunt_live (
    hunt_id         TEXT    PRIMARY KEY,
    "trigger"       TEXT    NOT NULL,
    sources         TEXT    NOT NULL DEFAULT '[]',
    started_at      TEXT    NOT NULL,
    step            TEXT    NOT NULL,
    step_started_at TEXT    NOT NULL,
    current_source  TEXT    NOT NULL DEFAULT '',
    sources_done    INTEGER NOT NULL DEFAULT 0,
    sources_total   INTEGER NOT NULL DEFAULT 0,
    found_so_far    INTEGER NOT NULL DEFAULT 0,
    command_id      TEXT    NOT NULL DEFAULT '',
    finished_at     TEXT
);
CREATE TABLE hunt_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL,
    "trigger"       TEXT    NOT NULL,
    sources         TEXT    NOT NULL,
    found           INTEGER NOT NULL DEFAULT 0,
    filtered_out    INTEGER NOT NULL DEFAULT 0,
    filter_reasons  TEXT    NOT NULL DEFAULT '{}',
    dup_url         INTEGER NOT NULL DEFAULT 0,
    dup_ct          INTEGER NOT NULL DEFAULT 0,
    dup_cooldown    INTEGER NOT NULL DEFAULT 0,
    "new"           INTEGER NOT NULL DEFAULT 0,
    capped          INTEGER NOT NULL DEFAULT 0,
    queued          INTEGER NOT NULL DEFAULT 0,
    applied_inline  INTEGER NOT NULL DEFAULT 0,
    duration_ms     INTEGER NOT NULL DEFAULT 0,
    hunt_id         TEXT    NOT NULL DEFAULT '',
    per_source      TEXT    NOT NULL DEFAULT '{}'
);
CREATE TABLE link_attempts (
    chat_id      INTEGER PRIMARY KEY,
    window_start TEXT    NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE pipeline_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT    NOT NULL,
    ts          TEXT    NOT NULL,
    stage       TEXT    NOT NULL,
    event       TEXT    NOT NULL,
    duration_ms INTEGER,
    payload     TEXT    NOT NULL DEFAULT ''
);
CREATE TABLE profile_jobs (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'pending',
    result     TEXT NOT NULL DEFAULT '',
    error      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE TABLE subsystem_health (
    subsystem             TEXT    PRIMARY KEY,
    consecutive_failures  INTEGER NOT NULL DEFAULT 0,
    last_error            TEXT    NOT NULL DEFAULT '',
    last_alert_at          TEXT
);
CREATE TABLE telegram_link_codes (
    code       TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE telegram_links (
    chat_id    INTEGER PRIMARY KEY,
    user_id    TEXT    UNIQUE NOT NULL,
    linked_at  TEXT
);
CREATE TABLE user_settings (
    user_id    TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL DEFAULT '',
    updated_at TEXT,
    PRIMARY KEY (user_id, key)
);
CREATE INDEX idx_ats
    ON applications(ats_status);
CREATE INDEX idx_bot_commands_status
    ON bot_commands(status, created_at);
CREATE INDEX idx_company
    ON applications(company);
CREATE INDEX idx_generation_runs_started_at ON generation_runs(started_at);
CREATE INDEX idx_generation_runs_url_norm ON generation_runs(url_norm);
CREATE INDEX idx_hunt_jobs_hunt ON hunt_jobs(hunt_id, id);
CREATE INDEX idx_hunt_jobs_url ON hunt_jobs(url_norm);
CREATE INDEX idx_hunt_live_started ON hunt_live(started_at);
CREATE INDEX idx_hunt_runs_hunt_id ON hunt_runs(hunt_id);
CREATE INDEX idx_hunt_runs_ts ON hunt_runs(ts);
CREATE INDEX idx_pipeline_events_run_id ON pipeline_events(run_id, id);
CREATE INDEX idx_pipeline_events_ts ON pipeline_events(ts);
CREATE INDEX idx_profile_jobs_status
    ON profile_jobs(status, created_at);
CREATE INDEX idx_user_ats ON applications(user_id, ats_status);
CREATE UNIQUE INDEX idx_user_url_norm ON applications(user_id, url_norm) WHERE url_norm != '';
