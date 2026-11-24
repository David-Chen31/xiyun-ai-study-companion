"""有限并发的本机媒体处理，保留状态以便重新打开草稿。"""

import json
import logging
import threading
import uuid

from ..course import CourseError, json_text, now_utc
from . import drafts


def latest(db, draft_id):
    row = db.execute("SELECT * FROM media_jobs WHERE draft_id=? ORDER BY rowid DESC LIMIT 1",
                     (draft_id,)).fetchone()
    if not row:
        return None
    info = dict(row)
    info["error"] = json.loads(info["error"]) if info["error"] else None
    return info


class UploadWorkers:
    def __init__(self, store, inspector):
        self.store = store
        self.inspector = inspector
        self.slots = threading.BoundedSemaphore(2)

    def recover_interrupted(self):
        failure = {"code": "UPLOAD_INTERRUPTED", "message": "上次处理因服务停止而中断，请重新选择原文件。"}
        with self.store.connect(write=True) as db:
            db.execute("UPDATE media_jobs SET state='interrupted', finished_at=?, error=? WHERE state='processing'",
                       (now_utc(), json_text(failure)))

    def start(self, account, draft_id, version, audio):
        job = uuid.uuid4().hex
        with self.store.connect(write=True) as db:
            drafts.editable(db, account, draft_id, version)
            current = latest(db, draft_id)
            if current and current["state"] == "processing":
                raise CourseError(409, "UPLOAD_IN_PROGRESS", "此草稿已有素材在处理，可查看进度")
            db.execute("INSERT INTO media_jobs VALUES (?, ?, ?, 'processing', ?, NULL, NULL)",
                       (job, draft_id, version, now_utc()))
            result = latest(db, draft_id)
        worker = threading.Thread(target=self.run, args=(account, draft_id, version, audio, job), daemon=True)
        try:
            worker.start()
        except RuntimeError as error:
            failure = CourseError(503, "WORKER_UNAVAILABLE", "无法启动媒体处理，请稍后重试")
            self.fail(job, failure)
            raise failure from error
        return result

    def fail(self, job, error):
        with self.store.connect(write=True) as db:
            db.execute("UPDATE media_jobs SET state='failed', finished_at=?, error=? WHERE id=? AND state='processing'",
                       (now_utc(), json_text(error.body()), job))

    def run(self, account, draft_id, version, audio, job):
        try:
            drafts.attach_audio(self.store, self.inspector, account, draft_id, version, audio, job=job)
        except CourseError as error:
            self.fail(job, error)
        except Exception:
            logging.exception("Media job failed: %s", job)
            self.fail(job, CourseError(503, "MEDIA_PROCESSING_FAILED", "媒体处理未完成，可重新上传"))
        finally:
            self.slots.release()
