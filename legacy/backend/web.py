"""同源网页、课程快照和 WAV Range 响应。"""

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from . import ROOT
from .course import CourseError, json_text, practice_path, public_course
from .media import slice_wav
from .platform import access, api, progress
from .platform.transcripts import TranscriptWorker
from .platform.uploads import UploadWorkers


ASSET_TYPES = {
    "/styles.css": "text/css; charset=utf-8",
    "/assets/rehearsal.webp": "image/webp",
    "/src/app.js": "text/javascript; charset=utf-8",
    "/src/course.js": "text/javascript; charset=utf-8",
    "/src/audio.js": "text/javascript; charset=utf-8",
    "/src/view.js": "text/javascript; charset=utf-8",
    "/src/progress.js": "text/javascript; charset=utf-8",
}


def byte_range(value, size):
    match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", value, re.IGNORECASE)
    if match and any(match.groups()):
        first, last = match.groups()
        try:
            if not first:
                suffix = int(last)
                if suffix > 0:
                    return max(0, size - suffix), size - 1
            else:
                start = int(first)
                end = min(int(last), size - 1) if last else size - 1
                if 0 <= start <= end:
                    return start, end
        except ValueError:
            pass
    raise CourseError(416, "INVALID_RANGE", "请求的字节范围不可用")


class Handler(BaseHTTPRequestHandler):
    def headers_for(self, status, content_type, length, extra=None):
        self.send_response(status)
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def reply(self, status, content_type, body=b"", extra=None):
        self.headers_for(status, content_type, len(body), extra)
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, status, body, extra=None):
        self.reply(status, "application/json; charset=utf-8", json_text(body).encode("utf-8"), extra)

    def course(self, course_id):
        with self.server.store.connect() as db:
            account = access.account_for(db, api.get_token(self)) if self.server.platform else None
            association = access.course_access(db, account, course_id)
            row = self.server.store.active_course(db, course_id)
            result = public_course(json.loads(row["manifest"]), json.loads(row["analysis"])["duration_ms"])
            if association:
                result["class_course"] = True
                result["phrase_stream"] = True
                if account["role"] == "student":
                    result["progress"] = progress.progress_state(db, account, course_id)
        self.json(200, result)

    def stream(self, media_id, query):
        course_ids = query.get("course_id", [])
        if len(course_ids) != 1 or not course_ids[0]:
            raise CourseError(404, "COURSE_UNKNOWN", "需要有效的作业编号")
        with self.server.store.connect() as db:
            account = access.account_for(db, api.get_token(self)) if self.server.platform else None
            access.course_access(db, account, course_ids[0])
            course = self.server.store.active_course(db, course_ids[0])
            manifest = json.loads(course["manifest"])
            if manifest["media_id"] != media_id:
                raise CourseError(403, "MEDIA_FORBIDDEN", "该作业不包含请求的示范")
            phrase_id = query.get("phrase_id")
            if phrase_id is None:
                self.wav(db, course["audio_sha256"])
                return
            phrase = next((item for item in manifest["phrases"] if [item["id"]] == phrase_id), None)
            if phrase is None:
                raise CourseError(404, "PHRASE_UNKNOWN", "找不到对应乐句")
            self.phrase_wav(db, course["audio_sha256"], phrase)

    def phrase_wav(self, db, digest, phrase):
        self.reply(200, "audio/wav", slice_wav(db, digest, phrase["lead_ms"], phrase["end_ms"]))

    def wav(self, db, digest):
        self.send_blob(db, "media", "wav", digest, "audio/wav")

    def send_blob(self, db, table, column, digest, content_type):
        # Table and column names come only from application code.
        media = db.execute(f"SELECT rowid, length({column}) AS size FROM {table} WHERE digest = ?",
                           (digest,)).fetchone()
        if media is None:
            raise CourseError(404, "MEDIA_UNKNOWN", "找不到媒体")
        size = media["size"]
        extra = {"Accept-Ranges": "bytes"}
        ranges = self.headers.get_all("Range", [])
        if ranges:
            try:
                start, end = byte_range(",".join(ranges), size)
            except CourseError as error:
                extra["Content-Range"] = f"bytes */{size}"
                self.json(error.status, error.body(), extra)
                return
            extra["Content-Range"] = f"bytes {start}-{end}/{size}"
            status = 206
        else:
            start, end, status = 0, size - 1, 200
        length = end - start + 1
        if self.command == "HEAD":
            self.headers_for(status, content_type, length, extra)
            return
        with db.blobopen(table, column, media["rowid"], readonly=True) as blob:
            blob.seek(start)
            self.headers_for(status, content_type, length, extra)
            remaining = length
            while remaining:
                chunk = blob.read(min(64 * 1024, remaining))
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def route(self):
        port = self.server.server_port
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if port == 80:
            allowed_hosts.update(("127.0.0.1", "localhost"))
        if self.headers.get("Host", "").lower() not in allowed_hosts:
            raise CourseError(403, "HOST_FORBIDDEN", "请通过本机作业地址打开")
        path = urlsplit(self.path)
        query = parse_qs(path.query, keep_blank_values=True)
        method = "GET" if self.command == "HEAD" else self.command
        if api.handle(self, method, path.path, query):
            return
        if method != "GET":
            self.json(405, {"code": "METHOD_NOT_ALLOWED", "message": "此地址仅支持读取"},
                      {"Allow": "GET, HEAD"})
            return
        if path.path == "/":
            if not query.get("course", [""])[0]:
                self.reply(302, "text/plain; charset=utf-8",
                           extra={"Location": practice_path(self.server.default_course)})
                return
            self.asset("index.html", "text/html; charset=utf-8")
            return
        if path.path in ASSET_TYPES:
            self.asset(path.path[1:], ASSET_TYPES[path.path])
            return
        match = re.fullmatch(r"/v1/courses/([^/]+)", path.path)
        if match:
            self.course(unquote(match[1]))
            return
        match = re.fullmatch(r"/v1/media/([^/]+)/stream", path.path)
        if match:
            self.stream(unquote(match[1]), query)
            return
        raise CourseError(404, "NOT_FOUND", "没有找到请求的资源")

    def asset(self, name, content_type):
        frontend = (ROOT / "frontend").resolve()
        asset = (frontend / name).resolve()
        if frontend not in asset.parents or not asset.is_file():
            raise CourseError(404, "NOT_FOUND", "找不到前端资源")
        self.reply(200, content_type, asset.read_bytes())

    def do_GET(self):
        try:
            self.route()
        except CourseError as error:
            self.json(error.status, error.body())
        except (ConnectionError, TimeoutError):
            pass

    do_HEAD = do_GET

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_GET


def make_server(store, port, default_course, *, platform=False, inspector=None):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.store = store
    server.default_course = default_course
    server.platform = platform
    server.inspector = inspector
    if platform:
        server.uploads = UploadWorkers(store, inspector)
        server.transcripts = TranscriptWorker(store)
        try:
            server.uploads.recover_interrupted()
            server.transcripts.recover_interrupted()
        except Exception:
            server.server_close()
            raise
    return server


def serve(server):
    address = f"http://127.0.0.1:{server.server_port}"
    print(f"作业地址：{address}{practice_path(server.default_course)}\n按 Ctrl+C 停止。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n作业服务已停止。", flush=True)
