CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin','teacher','reviewer','student')),
    salt BLOB NOT NULL,
    password BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    expires INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires);
CREATE TABLE IF NOT EXISTS classes (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS class_members (
    class_id TEXT NOT NULL REFERENCES classes(id),
    account_id TEXT NOT NULL REFERENCES accounts(id),
    expires_at TEXT,
    PRIMARY KEY (class_id, account_id)
);
CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES accounts(id),
    class_id TEXT NOT NULL REFERENCES classes(id),
    grant_ref TEXT NOT NULL REFERENCES grants(grant_ref),
    digest TEXT REFERENCES media(digest),
    analysis TEXT,
    content TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'draft' CHECK (state IN ('draft','submitted','returned','published')),
    feedback TEXT,
    course_id TEXT UNIQUE REFERENCES courses(course_id)
);
CREATE TABLE IF NOT EXISTS class_courses (
    course_id TEXT PRIMARY KEY REFERENCES courses(course_id),
    class_id TEXT NOT NULL REFERENCES classes(id)
);
CREATE TABLE IF NOT EXISTS video_previews (
    digest TEXT NOT NULL UNIQUE,
    mp4 BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS media_jobs (
    id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES drafts(id),
    draft_version INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('processing','ready','failed','interrupted')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_media_job_per_draft ON media_jobs(draft_id) WHERE state='processing';
CREATE TABLE IF NOT EXISTS transcription_jobs (
    draft_id TEXT PRIMARY KEY REFERENCES drafts(id),
    id TEXT NOT NULL UNIQUE,
    digest TEXT NOT NULL REFERENCES media(digest),
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('processing','ready','failed','interrupted')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    candidates TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS grant_revocations (
    grant_ref TEXT PRIMARY KEY REFERENCES grants(grant_ref),
    revoked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    object_id TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT,
    happened_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS progress_epochs (
    account_id TEXT NOT NULL REFERENCES accounts(id),
    course_id TEXT NOT NULL REFERENCES courses(course_id),
    generation INTEGER NOT NULL,
    PRIMARY KEY (account_id, course_id)
);
CREATE TABLE IF NOT EXISTS practice_events (
    account_id TEXT NOT NULL REFERENCES accounts(id),
    event_id TEXT NOT NULL,
    course_id TEXT NOT NULL REFERENCES courses(course_id),
    phrase_id TEXT NOT NULL,
    action TEXT NOT NULL,
    happened_at TEXT NOT NULL,
    PRIMARY KEY (account_id, event_id)
);
CREATE INDEX IF NOT EXISTS practice_by_course ON practice_events(course_id, account_id);
CREATE TABLE IF NOT EXISTS practice_bookmarks (
    account_id TEXT NOT NULL REFERENCES accounts(id),
    course_id TEXT NOT NULL REFERENCES courses(course_id),
    phrase_id TEXT NOT NULL,
    PRIMARY KEY (account_id, course_id)
);
