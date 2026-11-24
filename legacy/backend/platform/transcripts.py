"""Authorized asynchronous transcription kept separate from reviewed draft phrases."""

import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import uuid
import wave
from pathlib import Path

from ai_backend.transcribe import MAX_HINT_LENGTH, MAX_WINDOW_MS, MIN_WINDOW_MS

from .. import ROOT
from ..course import CourseError, invalid, json_text, now_utc, require_text
from ..media import slice_wav
from . import access, drafts


PURPOSE = "lyrics_transcription"


def run_transcription(model, audio, start, end, *, hint=""):
    with wave.open(io.BytesIO(audio), "rb") as wav:
        duration = wav.getnframes() * 1000 // wav.getframerate()
    duration = min(duration, end - start)
    # Slice boundaries can round down a millisecond. Preserve a safe local end.
    if duration < MIN_WINDOW_MS:
        raise CourseError(422, "TRANSCRIPTION_WINDOW", "片段太短，请稍微扩大范围")
    with tempfile.TemporaryDirectory(prefix="xiyun-transcript-") as work:
        source = Path(work) / "excerpt.wav"
        source.write_bytes(audio)
        env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1")
        env.pop("AI_BACKEND_TOKEN", None)
        command = [sys.executable, "-X", "utf8", "-m", "ai_backend.transcribe",
                   "--model-dir", str(model), "--audio", str(source), "--start-ms", "0", "--end-ms", str(duration)]
        if hint:
            hint_path = Path(work) / "hint.txt"
            hint_path.write_text(hint, encoding="utf-8")
            command.extend(("--hint-file", str(hint_path)))
        try:
            proc = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, timeout=110,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as error:
            raise CourseError(503, "TRANSCRIPTION_TIMEOUT", "本机转写超时，可缩短范围或继续手填") from error
        if proc.returncode == 2:
            raise CourseError(503, "TRANSCRIPTION_UNAVAILABLE", "本机缺少转写依赖，可继续手填")
        if proc.returncode:
            raise CourseError(503, "TRANSCRIPTION_FAILED", "本机模型未能完成转写，可缩短范围或手填")
        try:
            result = json.loads(proc.stdout)["candidates"]
            if not isinstance(result, list) or len(result) > 40:
                raise ValueError("invalid candidate array")
            candidates = []
            last = 0
            for candidate in result:
                text, first, final = (candidate[key] for key in ("text", "start_ms", "end_ms"))
                if (not isinstance(text, str) or not text.strip() or len(text) > 200
                        or type(first) is not int or type(final) is not int or not last <= first < final <= duration):
                    raise ValueError("invalid candidate")
                candidates.append({"text": text, "start_ms": start + first, "end_ms": start + final})
                last = final
            return candidates
        except (ValueError, KeyError, TypeError) as error:
            raise CourseError(503, "TRANSCRIPTION_INVALID", "本机模型返回了不完整的候选") from error


def permitted(db, row):
    grant = drafts.grant_for(db, row["grant_ref"])
    return PURPOSE in grant["purposes"] and row["digest"] is not None


def previous(db, row):
    job = db.execute("SELECT * FROM transcription_jobs WHERE draft_id=?", (row["id"],)).fetchone()
    if not job or job["digest"] != row["digest"]:
        return None
    result = {key: job[key] for key in ("id", "state", "start_ms", "end_ms", "started_at", "finished_at")}
    result["candidates"] = json.loads(job["candidates"]) if job["candidates"] else []
    result["error"] = json.loads(job["error"]) if job["error"] else None
    return result


class TranscriptWorker:
    def __init__(self, store):
        self.store = store
        self.slot = threading.BoundedSemaphore(1)
        configured = os.environ.get("XIYUN_WHISPER_MODEL")
        self.model = Path(configured).resolve() if configured else ROOT / ".xiyun" / "models" / "whisper-tiny"

    @property
    def configured(self):
        return (self.model / "model.safetensors").is_file() and (self.model / "config.json").is_file()

    def recover_interrupted(self):
        error = CourseError(503, "TRANSCRIPTION_INTERRUPTED", "上次转写因服务停止中断，可重新提交")
        with self.store.connect(write=True) as db:
            db.execute("UPDATE transcription_jobs SET state='interrupted', finished_at=?, error=? WHERE state='processing'",
                       (now_utc(), json_text(error.body())))

    def status(self, db, account, draft_id):
        access.role_required(account, "teacher")
        row = drafts.find_draft(db, account, draft_id)
        allowed = permitted(db, row)
        return {"configured": self.configured, "permitted": allowed, "job": previous(db, row) if allowed else None}

    def start(self, account, draft_id, data):
        start, end, version = (data.get(key) for key in ("start_ms", "end_ms", "version"))
        if type(start) is not int or type(end) is not int or not 0 <= start < end or not MIN_WINDOW_MS <= end-start <= MAX_WINDOW_MS:
            raise invalid("end_ms", "转写窗口须为 0.5–28 秒的整数毫秒")
        hint = data.get("hint", "")
        if not isinstance(hint, str) or len(hint) > MAX_HINT_LENGTH or "<|" in hint:
            raise invalid("hint", "参考唱词须为 120 字以内的普通文本")
        hint = require_text(data, "hint").strip() if hint.strip() else ""
        if not self.slot.acquire(blocking=False):
            raise CourseError(409, "TRANSCRIPTION_BUSY", "已有转写任务运行，可稍后重试")
        job_id = None
        try:
            with self.store.connect(write=True) as db:
                row = drafts.editable(db, account, draft_id, version)
                if not permitted(db, row):
                    raise CourseError(403, "TRANSCRIPTION_FORBIDDEN", "素材须已上传且许可包含唱词转写用途")
                if not self.configured:
                    raise CourseError(503, "TRANSCRIPTION_UNAVAILABLE", "请配置本机 Whisper 模型，也可继续手填")
                if end > json.loads(row["analysis"])["duration_ms"]:
                    raise invalid("end_ms", "转写窗口超出当前音轨")
                job_id = uuid.uuid4().hex
                db.execute(
                    """INSERT INTO transcription_jobs VALUES (?, ?, ?, ?, ?, 'processing', ?, NULL, NULL, NULL)
                       ON CONFLICT(draft_id) DO UPDATE SET id=excluded.id, digest=excluded.digest,
                         start_ms=excluded.start_ms, end_ms=excluded.end_ms, started_at=excluded.started_at,
                         state='processing', finished_at=NULL, candidates=NULL, error=NULL""",
                    (draft_id, job_id, row["digest"], start, end, now_utc()),
                )
                result = previous(db, row)
            thread = threading.Thread(target=self.run, args=(account, draft_id, job_id, row["digest"], start, end, hint),
                                      daemon=True)
            thread.start()
            return result
        except Exception:
            try:
                if job_id:
                    self.finish(job_id, error=CourseError(503, "TRANSCRIPTION_FAILED", "无法启动转写，可稍后重试"))
            finally:
                self.slot.release()
            raise

    def finish(self, job_id, error):
        with self.store.connect(write=True) as db:
            db.execute("UPDATE transcription_jobs SET state='failed', finished_at=?, error=? WHERE id=? AND state='processing'",
                       (now_utc(), json_text(error.body()), job_id))

    def assert_source(self, db, account, draft_id, digest):
        row = drafts.find_draft(db, account, draft_id)
        if row["digest"] != digest or row["state"] not in ("draft", "returned"):
            raise CourseError(409, "TRANSCRIPTION_STALE", "素材或草稿状态已改变，请重新打开草稿")
        if not permitted(db, row):
            raise CourseError(403, "TRANSCRIPTION_FORBIDDEN", "当前许可不包含唱词转写")

    def run(self, account, draft_id, job_id, digest, start, end, hint):
        try:
            with self.store.connect() as db:
                self.assert_source(db, account, draft_id, digest)
                excerpt = slice_wav(db, digest, start, end)
            candidates = run_transcription(self.model, excerpt, start, end, hint=hint)
            with self.store.connect(write=True) as db:
                self.assert_source(db, account, draft_id, digest)
                db.execute("UPDATE transcription_jobs SET state='ready', finished_at=?, candidates=? WHERE id=? AND state='processing'",
                           (now_utc(), json_text(candidates), job_id))
        except CourseError as error:
            self.finish(job_id, error)
        except Exception:
            logging.exception("Transcription job failed: %s", job_id)
            self.finish(job_id, CourseError(503, "TRANSCRIPTION_FAILED", "本机转写任务未完成"))
        finally:
            self.slot.release()
