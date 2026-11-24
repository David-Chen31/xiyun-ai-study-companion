"""对同一份音频完成许可检查、C 检查和原子发布。"""

import hashlib
import http.client
import json
import uuid

from ai_backend.inspection import MAX_DURATION_MS, MAX_FILE_BYTES, MAX_METADATA_BYTES
from ai_backend.multipart import build_multipart
from ai_backend.replay import validate_success
from ai_backend.server import INSPECT_PATH

from .course import CourseError, check_phrase_windows, json_text, normalize_course, now_utc, practice_path


class Inspector:
    def __init__(self, port, token, timeout=30):
        self.port = port
        self.token = token
        self.timeout = timeout

    def inspect(self, audio, metadata):
        if not self.token or any(not 33 <= ord(char) <= 126 for char in self.token):
            raise CourseError(503, "ANALYSIS_UNAVAILABLE", "请配置 C 使用的 AI_BACKEND_TOKEN 后重试")
        metadata_text = json_text(metadata).encode("utf-8")
        if len(metadata_text) > MAX_METADATA_BYTES:
            raise CourseError(413, "TOO_LARGE", "句表超过检查服务大小上限", "phrases")
        body, boundary = build_multipart(
            {"audio": audio, "metadata": metadata_text}, filenames={"audio": "demo.wav"},
        )
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        try:
            connection.request("POST", INSPECT_PATH, body=body, headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            })
            with connection.getresponse() as response:
                status = response.status
                result = json.loads(response.read(64 * 1024))
            if not isinstance(result, dict):
                raise ValueError("检查服务返回的 JSON 不是对象")
            if status == 200:
                validate_success(result, metadata)
                if result["duration_ms"] > MAX_DURATION_MS:
                    raise ValueError("检查结果超过音频时长上限")
                check_phrase_windows(metadata["phrases"], result["duration_ms"])
                return {key: result[key] for key in (
                    "request_id", "revision_id", "processor_version", "duration_ms",
                    "sample_rate_hz", "channels", "phrase_count",
                )}
            code = {400: "INVALID_INPUT", 413: "TOO_LARGE", 415: "UNSUPPORTED_AUDIO",
                    422: "INVALID_BOUNDARY"}.get(status)
            message, field = result.get("message"), result.get("field")
            if (code and result.get("code") == code and isinstance(message, str)
                    and result.get("request_id") in (None, metadata["request_id"])):
                raise CourseError(status, code, message, field if isinstance(field, str) else None)
            if status == 401:
                raise CourseError(503, "ANALYSIS_UNAVAILABLE", "C 服务拒绝内部令牌，请统一配置后重试")
            raise ValueError(f"检查服务返回 HTTP {status}")
        except (OSError, http.client.HTTPException, ValueError) as error:
            raise CourseError(503, "ANALYSIS_UNAVAILABLE", f"无法取得有效的音频检查结果，请用同一包重试（{error}）") from error
        finally:
            connection.close()


def previous_import(store, db, course_id, manifest, digest):
    previous = store.find_course(db, course_id)
    if previous is None:
        return None
    if previous["manifest"] != manifest or previous["audio_sha256"] != digest:
        raise CourseError(409, "COURSE_CONFLICT", "作业编号已绑定其他快照，重发请使用新编号", "course_id")
    return {"course_id": course_id, "status": store.status(previous), "created": False,
            "path": practice_path(course_id), "analysis": json.loads(previous["analysis"])}


def import_course(store, inspector, data, audio_path):
    course = normalize_course(data)
    with audio_path.open("rb") as file:
        audio = file.read(MAX_FILE_BYTES + 1)
    if len(audio) > MAX_FILE_BYTES:
        raise CourseError(413, "TOO_LARGE", "WAV 超过 100 MiB", "audio")
    digest = hashlib.sha256(audio).hexdigest()
    manifest, course_id = json_text(course), course["course_id"]
    request_id = uuid.uuid4().hex
    metadata = {
        "request_id": request_id,
        "revision_id": course["revision_id"],
        "phrases": course["phrases"],
    }
    with store.connect(write=True) as db:
        previous = previous_import(store, db, course_id, manifest, digest)
        if previous:
            return previous
        store.check_grant(db, course, digest)
        db.execute(
            """INSERT INTO import_attempts
               (request_id, course_id, revision_id, audio_sha256, started_at, status)
               VALUES (?, ?, ?, ?, ?, 'checking')""",
            (request_id, course_id, course["revision_id"], digest, now_utc()),
        )
    try:
        analysis = inspector.inspect(audio, metadata)
        with store.connect(write=True) as db:
            previous = previous_import(store, db, course_id, manifest, digest)
            if previous:
                store.finish_attempt(db, request_id, "duplicate")
                return previous
            store.check_grant(db, course, digest)
            db.execute("INSERT INTO media (digest, wav) VALUES (?, ?) ON CONFLICT (digest) DO NOTHING",
                       (digest, audio))
            db.execute(
                """INSERT INTO courses
                   (course_id, grant_ref, audio_sha256, manifest, analysis, expires_at, published_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (course_id, course["grant_ref"], digest, manifest, json_text(analysis),
                 course["expires_at"], now_utc()),
            )
            store.finish_attempt(db, request_id, "published")
    except CourseError as error:
        with store.connect(write=True) as db:
            status = "retryable" if error.status == 503 else "rejected"
            store.finish_attempt(db, request_id, status, error.body())
        raise
    return {"course_id": course_id, "status": "published", "created": True,
            "path": practice_path(course_id), "analysis": analysis}
