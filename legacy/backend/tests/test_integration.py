"""同一份三句样例在 A、B、C 契约之间的回归。"""

import http.client
import json
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from ai_backend.server import make_server as make_c_server

from backend import ROOT
from backend.course import CourseError, normalize_grant, read_json
from backend.importer import Inspector, import_course
from backend.store import Store
from backend.web import make_server


DEMO_AUDIO = ROOT / "ai_backend" / "samples" / "demo_12s.wav"


@contextmanager
def running(server):
    with server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        thread.start()
        try:
            yield server.server_port
        finally:
            server.shutdown()
            thread.join()


def get(port, path, method="GET", **headers):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request(method, path, headers=headers)
        with connection.getresponse() as response:
            return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


class IntegratedCourseTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.data_dir = Path(temporary)
        self.store = Store(self.data_dir)
        self.course = read_json(ROOT / "contracts" / "demo-course.json")
        self.grant = read_json(ROOT / "contracts" / "demo-grant.json")
        self.token = "integration-test"

    def register(self):
        self.store.register_grant(normalize_grant(self.grant))

    def c(self):
        return running(make_c_server("127.0.0.1", 0, self.token))

    def import_(self, port, **overrides):
        return import_course(self.store, Inspector(port, self.token),
                             {**self.course, **overrides}, DEMO_AUDIO)

    def assert_course_error(self, code, operation, *args, **kwargs):
        with self.assertRaises(CourseError) as caught:
            operation(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_publish_and_replay_through_real_c_then_serve_without_c(self):
        self.register()
        with self.c() as c_port:
            result = self.import_(c_port)
        self.assertTrue(result["created"])
        self.assertEqual(result["analysis"]["duration_ms"], 12000)
        self.assertEqual(result["analysis"]["phrase_count"], 3)
        self.assertFalse(self.import_(c_port)["created"])
        self.assert_course_error("COURSE_CONFLICT", self.import_, c_port, title="修改标题")

        with running(make_server(self.store, 0, "c1")) as port:
            self.assertEqual(get(port, "/")[1]["Location"], "/?course=c1")
            self.assertIn(b"startPractice", get(port, "/?course=c1")[2])
            self.assertEqual(get(port, "/src/app.js")[0], 200)
            self.assertEqual(get(port, "/.xiyun/xiyun.sqlite3")[0], 404)
            status, headers, body = get(port, "/v1/courses/c1")
            self.assertEqual((status, headers["Cache-Control"]), (200, "no-store"))
            course = json.loads(body)
            self.assertNotIn("grant_ref", course)
            self.assertEqual(course["phrases"], self.course["phrases"])
            stream = course["stream_url"]
            audio = DEMO_AUDIO.read_bytes()
            status, headers, body = get(port, stream)
            self.assertEqual((status, headers["Content-Type"], body), (200, "audio/wav", audio))
            for value, start, end in [
                ("bytes=128000-128127", 128000, 128127),
                ("bytes=128000-", 128000, len(audio) - 1),
                ("bytes=-16", len(audio) - 16, len(audio) - 1),
            ]:
                with self.subTest(value=value):
                    status, headers, body = get(port, stream, Range=value)
                    self.assertEqual(status, 206)
                    self.assertEqual(headers["Content-Range"], f"bytes {start}-{end}/{len(audio)}")
                    self.assertEqual(body, audio[start:end + 1])
            status, headers, body = get(port, stream, "HEAD", Range="bytes=0-15")
            self.assertEqual((status, headers["Content-Length"], body), (206, "16", b""))
            for value in ("bytes=99999999-", "bytes=0-1,3-4", "bytes=-0"):
                status, headers, _ = get(port, stream, Range=value)
                self.assertEqual((status, headers["Content-Range"]), (416, f"bytes */{len(audio)}"))
            self.assertEqual(get(port, "/v1/media/other/stream?course_id=c1")[0], 403)
            self.assertEqual(get(port, "/v1/media/m1/stream?course_id=missing")[0], 404)
            self.assertEqual(get(port, "/v1/recordings", "POST")[0], 405)
            self.store.withdraw("c1")
            self.store.withdraw("c1")
            self.assertEqual(get(port, stream)[0], 410)
            self.assertEqual(get(port, "/v1/courses/c1")[0], 410)
        with running(make_server(Store(self.data_dir), 0, "c1")) as port:
            self.assertEqual(get(port, stream, Range="bytes=0-15")[0], 410)
            self.assertEqual(get(port, "/v1/courses/c1")[0], 410)
        self.assertEqual(self.import_(c_port)["status"], "withdrawn")

    def test_privileges_and_invalid_package_do_not_change_existing_course(self):
        self.assert_course_error("GRANT_UNKNOWN", self.import_, 1)
        self.assert_course_error("INVALID_INPUT", normalize_grant, {**self.grant, "purposes": ["onsite_playback"]})
        self.register()
        self.assert_course_error("SELF_REVIEW", self.import_, 1, reviewer_id=self.course["author_id"])
        self.assert_course_error("GRANT_CREDIT_MISMATCH", self.import_, 1, credit="其他署名")
        self.assert_course_error("COURSE_GONE", self.import_, 1, expires_at="2000-01-01T00:00:00Z")
        self.assert_course_error("GRANT_TERM_EXCEEDED", self.import_, 1, expires_at="2099-02-01T00:00:00Z")
        wrong_audio = self.data_dir / "wrong.wav"
        wrong_audio.write_bytes(b"other")
        self.assert_course_error("GRANT_AUDIO_MISMATCH", import_course, self.store,
                                 Inspector(1, self.token), self.course, wrong_audio)
        with self.c() as port:
            self.import_(port)
            phrases = self.course["phrases"]
            error = self.assert_course_error(
                "INVALID_BOUNDARY", self.import_, port, course_id="bad",
                phrases=[*phrases[:2], {**phrases[2], "end_ms": 12001}],
            )
            self.assertEqual((error.status, error.field), (422, "phrases[2].end_ms"))
        with running(make_server(self.store, 0, "c1")) as port:
            self.assertEqual(get(port, "/v1/courses/bad")[0], 404)
            self.assertEqual(get(port, "/v1/courses/c1")[0], 200)
            with patch("backend.store.now_utc", return_value="2099-01-01T00:00:00.000000+00:00"):
                self.assertEqual(get(port, "/v1/courses/c1")[0], 410)
                self.assertEqual(get(port, "/v1/media/m1/stream?course_id=c1")[0], 410)

    def test_timeout_is_recorded_and_same_package_can_retry(self):
        release = threading.Event()

        class SlowC(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                release.wait(5)

        self.register()
        with running(ThreadingHTTPServer(("127.0.0.1", 0), SlowC)) as c_port:
            try:
                self.assert_course_error(
                    "ANALYSIS_UNAVAILABLE", import_course, self.store,
                    Inspector(c_port, self.token, timeout=0.2), self.course, DEMO_AUDIO,
                )
            finally:
                release.set()
        with self.store.connect() as db:
            self.assertIsNone(self.store.find_course(db, "c1"))
            self.assertEqual(db.execute("SELECT status FROM import_attempts").fetchone()[0], "retryable")
        with self.c() as c_port:
            self.assertTrue(self.import_(c_port)["created"])
            self.assertFalse(self.import_(c_port)["created"])


if __name__ == "__main__":
    unittest.main()
