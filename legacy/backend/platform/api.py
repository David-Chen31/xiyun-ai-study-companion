"""工作台 HTTP 路由，复用现有同源课程和媒体入口。"""

import json
import re
from urllib.parse import unquote

from ai_backend.inspection import MAX_FILE_BYTES

from ..course import CourseError, invalid, normalize_grant, now_utc, require_text
from . import access, drafts, learning, progress, uploads


UI_ASSETS = {
    "/workspace/": ("workspace/index.html", "text/html; charset=utf-8"),
    "/workspace/app.js": ("workspace/app.js", "text/javascript; charset=utf-8"),
    "/workspace/styles.css": ("workspace/styles.css", "text/css; charset=utf-8"),
}


def body(handler, limit, expected_type):
    if handler.headers.get_content_type() != expected_type:
        raise CourseError(415, "UNSUPPORTED_CONTENT", f"请求类型须为 {expected_type}")
    values = handler.headers.get_all("Content-Length", [])
    if handler.headers.get("Transfer-Encoding") or len(values) != 1 or not re.fullmatch(r"[0-9]{1,12}", values[0]):
        raise invalid(None, "需要合法的 Content-Length")
    length = int(values[0])
    if not 0 < length <= limit:
        raise CourseError(413, "TOO_LARGE", "请求内容为空或超过大小上限")
    handler.connection.settimeout(15)
    data = handler.rfile.read(length)
    if len(data) != length:
        raise invalid(None, "请求内容未完整接收")
    return data


def json_body(handler):
    try:
        data = json.loads(body(handler, 256 * 1024, "application/json"))
    except (ValueError, RecursionError) as error:
        raise invalid(None, "请提交有效 JSON") from error
    if not isinstance(data, dict):
        raise invalid(None, "请求须为 JSON 对象")
    return data


def get_token(handler):
    return access.read_token(handler.headers.get("Cookie", ""))


def workspace(store, db, account):
    classes = access.list_classes(db, account)
    result = {
        "account": dict(account),
        "classes": classes,
        "courses": progress.catalog(store, db, account, classes),
        "drafts": drafts.list_drafts(db, account, classes),
    }
    if account["role"] == "admin":
        result["accounts"] = [dict(row) for row in db.execute("SELECT id, name, role FROM accounts ORDER BY id")]
        result["members"] = [dict(row) for row in db.execute("SELECT * FROM class_members ORDER BY class_id, account_id")]
        result["grants"] = [dict(row) for row in db.execute(
            "SELECT grant_ref, expires_at, revoked_at FROM grants LEFT JOIN grant_revocations USING (grant_ref)"
        )]
    return result


def withdraw(store, db, account, course_id, data):
    access.role_required(account, "admin", "reviewer")
    association = access.course_access(db, account, course_id)
    if not association:
        raise CourseError(404, "CLASS_COURSE_UNKNOWN", "找不到指定班级作业")
    reason = require_text(data, "reason").strip()
    result = db.execute("UPDATE courses SET withdrawn_at=? WHERE course_id=? AND withdrawn_at IS NULL",
                        (now_utc(), course_id))
    if result.rowcount:
        drafts.audit(db, account, course_id, "withdraw", reason)
    return {"course_id": course_id, "status": "withdrawn"}


def handle(handler, method, path, query):
    if not handler.server.platform:
        return False
    if method == "GET" and path in UI_ASSETS:
        handler.asset(*UI_ASSETS[path])
        return True
    if method == "GET" and path == "/workspace":
        handler.reply(302, "text/plain", extra={"Location": "/workspace/"})
        return True
    session = path in ("/v1/sessions", "/v1/sessions/current")
    if not session and not any(path == prefix or path.startswith(prefix + "/") for prefix in (
        "/v1/workspace", "/v1/catalog", "/v1/accounts", "/v1/classes", "/v1/drafts", "/v1/grants",
        "/v1/practice-events", "/v1/me/progress", "/v1/questions",
    )) and not re.fullmatch(r"/v1/courses/[^/]+/(?:withdraw|alternatives)", path):
        return False
    store = handler.server.store
    token = get_token(handler)
    if method != "GET" and handler.headers.get("Origin") != "http://" + handler.headers.get("Host", ""):
        raise CourseError(403, "ORIGIN_FORBIDDEN", "请从同源工作台提交操作")
    if method == "POST" and path == "/v1/sessions":
        token, account = access.login(store, json_body(handler))
        handler.json(200, {"account": account}, {"Set-Cookie": access.session_cookie(token, access.SESSION_AGE)})
        return True
    if method == "DELETE" and path == "/v1/sessions/current":
        with store.connect(write=True) as db:
            if token:
                db.execute("DELETE FROM sessions WHERE token_hash=?", (access.token_hash(token),))
        handler.json(200, {"logged_out": True}, {"Set-Cookie": access.session_cookie()})
        return True
    with store.connect() as db:
        account = access.role_required(access.account_for(db, token))
    transcript = re.fullmatch(r"/v1/drafts/([0-9a-f]{32})/transcription", path)
    if transcript and method in ("GET", "POST"):
        if method == "GET":
            with store.connect() as db:
                result = handler.server.transcripts.status(db, account, transcript[1])
        else:
            result = handler.server.transcripts.start(account, transcript[1], json_body(handler))
        handler.json(200 if method == "GET" else 202, result)
        return True
    match = re.fullmatch(r"/v1/drafts/([0-9a-f]{32})/(media|video)", path)
    if method == "PUT" and match and match[2] == "media":
        raw_version = handler.headers.get("If-Match", "")
        if not raw_version.isdecimal() or len(raw_version) > 10:
            raise invalid("version", "需要当前草稿版本")
        with store.connect() as db:
            drafts.editable(db, account, match[1], int(raw_version))
            current_job = uploads.latest(db, match[1])
            if current_job and current_job["state"] == "processing":
                raise CourseError(409, "UPLOAD_IN_PROGRESS", "此草稿已有素材在处理，可查看进度")
        workers = handler.server.uploads
        if not workers.slots.acquire(blocking=False):
            raise CourseError(503, "WORKER_BUSY", "媒体处理繁忙，请稍后重试")
        asynchronous = handler.headers.get("Prefer") == "respond-async"
        started = False
        try:
            audio = body(handler, MAX_FILE_BYTES, "application/octet-stream")
            if asynchronous:
                result = workers.start(account, match[1], int(raw_version), audio)
                started = True
            else:
                result = drafts.attach_audio(store, handler.server.inspector, account, match[1], int(raw_version), audio)
        finally:
            if not started:
                workers.slots.release()
        handler.json(202 if asynchronous else 200, result)
        return True
    if method == "POST" and path == "/v1/grants":
        access.role_required(account, "admin")
        record = normalize_grant(json_body(handler))
        store.register_grant(record)
        handler.json(201, {"grant_ref": record["grant_ref"], "status": "registered"})
        return True
    if method == "POST" and path == "/v1/accounts":
        access.role_required(account, "admin")
        data = json_body(handler)
        account_id, name, role, password = (require_text(data, key) for key in ("account_id", "name", "role", "password"))
        access.create_account(store, account_id, name, role, password)
        handler.json(201, {"account_id": account_id, "status": "created"})
        return True
    data = json_body(handler) if method in ("POST", "PUT", "PATCH") else {}
    with store.connect(write=method != "GET") as db:
        if method == "GET" and match:
            row = drafts.find_draft(db, account, match[1])
            grant = drafts.grant_for(db, row["grant_ref"])
            analysis = json.loads(row["analysis"]) if row["analysis"] else {}
            if not row["digest"] or analysis.get("source_sha256", row["digest"]) != grant["audio_sha256"]:
                raise CourseError(404, "MEDIA_UNKNOWN", "草稿尚无可试听素材")
            if match[2] == "video":
                preview = analysis.get("preview")
                if "video_preview" not in grant["purposes"] or not preview:
                    raise CourseError(404, "VIDEO_UNAVAILABLE", "该素材没有获准播放的视频预览")
                handler.send_blob(db, "video_previews", "mp4", preview["digest"], "video/mp4")
            else:
                handler.wav(db, row["digest"])
            return True
        status, result = dispatch(store, db, account, method, path, query, data)
    handler.json(status, result)
    return True


def dispatch(store, db, account, method, path, query, data):
    if method == "GET" and path in ("/v1/workspace", "/v1/sessions/current"):
        return 200, workspace(store, db, account)
    if method == "GET" and path == "/v1/catalog":
        return 200, {"courses": progress.catalog(store, db, account, access.list_classes(db, account))}
    if method == "POST" and path == "/v1/classes":
        return 201, access.create_class(db, account, data)
    if method == "POST" and path == "/v1/drafts":
        return 201, drafts.create_draft(db, account, data)
    if method == "POST" and path == "/v1/practice-events":
        return 200, progress.save_event(store, db, account, data)
    if method == "POST" and path == "/v1/questions":
        return 200, learning.answer(store, db, account, data)
    match = re.fullmatch(r"/v1/grants/([^/]+)/revoke", path)
    if method == "POST" and match:
        access.role_required(account, "admin")
        ref = unquote(match[1])
        reason = require_text(data, "reason").strip()
        if not db.execute("SELECT 1 FROM grants WHERE grant_ref=?", (ref,)).fetchone():
            raise CourseError(404, "GRANT_UNKNOWN", "找不到许可")
        result = db.execute("INSERT OR IGNORE INTO grant_revocations VALUES (?, ?)", (ref, now_utc()))
        if result.rowcount:
            drafts.audit(db, account, ref, "revoke_grant", reason)
        return 200, {"grant_ref": ref, "status": "revoked"}
    if path == "/v1/me/progress" and method in ("GET", "DELETE"):
        access.role_required(account, "student")
        course_id = query.get("course_id", [""])[0]
        if method == "DELETE":
            return 200, progress.clear_progress(db, account, course_id)
        progress.published(store, db, account, course_id)
        return 200, progress.progress_state(db, account, course_id)
    match = re.fullmatch(r"/v1/classes/([0-9a-f]{32})/members/([A-Za-z0-9_-]{1,64})", path)
    if match and method in ("PUT", "DELETE"):
        return 200, access.manage_member(db, account, *match.groups(), data if method == "PUT" else None)
    match = re.fullmatch(r"/v1/classes/([0-9a-f]{32})/summary", path)
    if match and method == "GET":
        return 200, progress.class_summary(store, db, account, match[1])
    match = re.fullmatch(r"/v1/drafts/([0-9a-f]{32})/media-job", path)
    if match and method == "GET":
        drafts.find_draft(db, account, match[1])
        return 200, {"job": uploads.latest(db, match[1])}
    match = re.fullmatch(r"/v1/drafts/([0-9a-f]{32})(?:/(submit|review|fork))?", path)
    if match:
        draft_id, action = match.groups()
        if method == "GET" and action is None:
            return 200, drafts.detail(drafts.find_draft(db, account, draft_id))
        if method == "PATCH" and action is None:
            return 200, drafts.update_draft(store, db, account, draft_id, data)
        if method == "POST" and action:
            if action == "review":
                return 200, drafts.review(store, db, account, draft_id, data)
            operation = drafts.submit if action == "submit" else drafts.fork
            return 200, operation(db, account, draft_id, data)
    match = re.fullmatch(r"/v1/courses/([0-9a-f]{32})/(withdraw|alternatives)", path)
    if match:
        if method == "POST" and match[2] == "withdraw":
            return 200, withdraw(store, db, account, match[1], data)
        if method == "GET" and match[2] == "alternatives":
            return 200, learning.alternative(store, db, account, match[1], query.get("phrase_id", [""])[0])
    raise CourseError(404, "NOT_FOUND", "找不到此工作台操作")
