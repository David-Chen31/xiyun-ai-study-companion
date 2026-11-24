"""SQLite 保存许可、发布快照、审计记录和私有 WAV。"""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .course import CourseError, json_text, now_utc


SCHEMA = """
CREATE TABLE IF NOT EXISTS grants (
    grant_ref TEXT PRIMARY KEY,
    record TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS media (
    digest TEXT NOT NULL UNIQUE,
    wav BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS courses (
    course_id TEXT PRIMARY KEY,
    grant_ref TEXT NOT NULL REFERENCES grants(grant_ref),
    audio_sha256 TEXT NOT NULL REFERENCES media(digest),
    manifest TEXT NOT NULL,
    analysis TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    published_at TEXT NOT NULL,
    withdrawn_at TEXT
);
CREATE TABLE IF NOT EXISTS import_attempts (
    request_id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    audio_sha256 TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    error TEXT
);
"""


class Store:
    def __init__(self, data_dir):
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = data_dir / "xiyun.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            schema = Path(__file__).resolve().parent / "platform" / "schema.sql"
            db.executescript(schema.read_text(encoding="utf-8"))

    @contextmanager
    def connect(self, *, write=False):
        db = sqlite3.connect(self.path, isolation_level=None, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                if write:
                    db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    def register_grant(self, grant):
        record = json_text(grant)
        with self.connect(write=True) as db:
            previous = db.execute("SELECT record FROM grants WHERE grant_ref = ?",
                                  (grant["grant_ref"],)).fetchone()
            if previous:
                if previous["record"] != record:
                    raise CourseError(409, "GRANT_CONFLICT", "该许可引用已绑定其他内容，请登记新引用")
                return
            db.execute("INSERT INTO grants VALUES (?, ?, ?)",
                       (grant["grant_ref"], record, grant["expires_at"]))

    @staticmethod
    def check_grant(db, course, digest):
        row = db.execute("SELECT record FROM grants WHERE grant_ref = ?", (course["grant_ref"],)).fetchone()
        if row is None:
            raise CourseError(403, "GRANT_UNKNOWN", "请先登记清单引用的许可", "grant_ref")
        if db.execute("SELECT 1 FROM grant_revocations WHERE grant_ref=?", (course["grant_ref"],)).fetchone():
            raise CourseError(410, "COURSE_GONE", "素材许可已撤回")
        grant = json.loads(row["record"])
        if grant["audio_sha256"] != digest:
            raise CourseError(403, "GRANT_AUDIO_MISMATCH", "WAV 的 SHA-256 与许可不匹配", "audio_sha256")
        if grant["credit"] != course["credit"]:
            raise CourseError(403, "GRANT_CREDIT_MISMATCH", "清单署名与许可不匹配", "credit")
        if course["expires_at"] > grant["expires_at"]:
            raise CourseError(403, "GRANT_TERM_EXCEEDED", "作业到期时间超过许可期限", "expires_at")
        if course["expires_at"] <= now_utc():
            raise CourseError(410, "COURSE_GONE", "作业许可已到期")

    @staticmethod
    def status(row):
        if row["withdrawn_at"]:
            return "withdrawn"
        if row["grant_revoked_at"]:
            return "revoked"
        if min(row["expires_at"], row["grant_expires_at"]) <= now_utc():
            return "expired"
        return "published"

    @staticmethod
    def find_course(db, course_id):
        return db.execute(
            """SELECT courses.*, grants.expires_at AS grant_expires_at, revoked_at AS grant_revoked_at
               FROM courses JOIN grants USING (grant_ref) LEFT JOIN grant_revocations USING (grant_ref)
               WHERE course_id = ?""",
            (course_id,),
        ).fetchone()

    def active_course(self, db, course_id):
        row = self.find_course(db, course_id)
        if row is None:
            raise CourseError(404, "COURSE_UNKNOWN", "没有找到这份作业")
        status = self.status(row)
        if status != "published":
            message = {"withdrawn": "作业已撤回", "revoked": "素材许可已撤回", "expired": "作业许可已到期"}[status]
            raise CourseError(410, "COURSE_GONE", message)
        return row

    def withdraw(self, course_id):
        with self.connect(write=True) as db:
            result = db.execute(
                "UPDATE courses SET withdrawn_at = COALESCE(withdrawn_at, ?) WHERE course_id = ?",
                (now_utc(), course_id),
            )
            if not result.rowcount:
                raise CourseError(404, "COURSE_UNKNOWN", "没有找到这份作业")

    @staticmethod
    def finish_attempt(db, request_id, status, error=None):
        db.execute(
            "UPDATE import_attempts SET finished_at = ?, status = ?, error = ? WHERE request_id = ?",
            (now_utc(), status, json_text(error) if error else None, request_id),
        )
