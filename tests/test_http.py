import io
import json
import re
import tempfile
import unittest
from http.client import HTTPResponse
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from server import catalog
from server.http import STATIC_FILES, WEB_ROOT, StudioHandler, main


class MemorySocket:
    def __init__(self, data=b""):
        self.input = io.BytesIO(data)
        self.output = bytearray()

    def makefile(self, *args, **kwargs):
        return self.input

    def sendall(self, data):
        self.output.extend(data)


class QuietHandler(StudioHandler):
    def log_message(self, *args):
        pass


def request(path, method="GET", headers="", body=b""):
    target = quote(path, safe="/?=&%")
    raw = f"{method} {target} HTTP/1.0\r\nHost: localhost\r\n{headers}\r\n".encode("ascii")
    connection = MemorySocket(raw + body)
    QuietHandler(connection, ("127.0.0.1", 12345), None)
    response = HTTPResponse(MemorySocket(bytes(connection.output)), method=method)
    response.begin()
    return response, response.read()


class StudioTests(unittest.TestCase):
    def test_homepage_and_song_url_share_the_current_app_shell(self):
        expected = (WEB_ROOT / "studio/index.html").read_bytes()
        for path in ("/", "/sing/wo-bu-nan-guo"):
            response, body = request(path)
            self.assertEqual(response.status, 200)
            self.assertEqual(body, expected)
            self.assertIsNone(response.getheader("Location"))

    def test_static_imports_are_public_and_relative_to_frontend(self):
        for name in STATIC_FILES:
            with self.subTest(asset=name):
                response, body = request("/" + name)
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Cache-Control"), "no-store")
                self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
                if name.endswith(".js"):
                    self.assertEqual(response.getheader("Content-Type"), "text/javascript; charset=utf-8")
                    matches = re.findall(r"(?:from\s*|import\s*)['\"](\.[^'\"]+)['\"]", body.decode())
                    for target in matches:
                        resolved = ((WEB_ROOT / name).parent / target).resolve().relative_to(WEB_ROOT).as_posix()
                        self.assertIn(resolved, STATIC_FILES)

    def test_catalog_only_discloses_display_metadata(self):
        response, body = request("/api/karaoke/songs")
        self.assertEqual(response.status, 200)
        song = next(entry for entry in json.loads(body) if entry["id"] == "wo-bu-nan-guo")
        self.assertNotIn("source", song)
        self.assertNotIn("backing", song)
        self.assertEqual(request("/api/karaoke/songs/unknown")[0].status, 404)

    def test_opera_candidates_are_listed_but_not_playable_without_local_assets(self):
        candidates = json.loads((catalog.ROOT / "library/opera-candidates.json").read_text(encoding="utf-8"))
        ids = [item["catalog_id"] for item in candidates["items"]]
        self.assertEqual(len(ids), 6)
        self.assertEqual(len(set(ids)), len(ids))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(catalog, "LIBRARY", root), patch.object(catalog, "ROOT", root):
                response, body = request("/api/karaoke/songs")
                self.assertEqual(response.status, 200)
                entries = {entry["id"]: entry for entry in json.loads(body)}
                for song_id in ids:
                    with self.subTest(song=song_id):
                        entry = entries[song_id]
                        self.assertFalse(entry["available"])
                        self.assertFalse(entry["prepared"])
                        self.assertIsNone(entry["duration_ms"])
                        self.assertEqual(entry["recording_status"], "awaiting_authorized_recording")
                        self.assertNotIn("source", entry)
                        self.assertEqual(request("/api/karaoke/songs/" + song_id)[0].status, 404)
                        self.assertEqual(request("/media/karaoke/" + song_id + "/reference.mp3")[0].status, 404)

    def test_opera_source_notes_match_registered_tracks(self):
        candidates = json.loads((catalog.ROOT / "library/opera-candidates.json").read_text(encoding="utf-8"))
        for item in candidates["items"]:
            with self.subTest(song=item["catalog_id"]):
                song = catalog.SONGS[item["catalog_id"]]
                self.assertEqual(song["genre"], item["genre"])
                self.assertTrue(item["references"])
                self.assertEqual(item["recording_license"], "not_obtained")
                self.assertIsNone(item["audio_file"])
                self.assertIsNone(item["lyric_timing"])
                self.assertTrue(all(ref["url"].startswith("https://") for ref in item["references"]))
                self.assertTrue(song["source"].startswith("audio/"))

    def test_music_serving_is_scoped_to_library_audio_and_supports_ranges(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "audio").mkdir()
            (root / "audio/test.mp3").write_bytes(b"0123456789")
            (root / "outside.mp3").write_bytes(b"private")
            with patch.object(catalog, "LIBRARY", root), patch.dict(catalog.SONGS, {
                "audio-test": {"source": "audio/test.mp3"},
                "private-audio": {"source": "outside.mp3"},
            }, clear=True):
                path = "/media/karaoke/audio-test/reference.mp3"
                response, body = request(path, headers="Range: bytes=2-5\r\n")
                self.assertEqual((response.status, body), (206, b"2345"))
                self.assertEqual(response.getheader("Content-Range"), "bytes 2-5/10")
                self.assertEqual(request(path, headers="Range: bytes=22-\r\n")[0].status, 416)
                self.assertEqual(request("/media/karaoke/private-audio/reference.mp3")[0].status, 404)
                response, body = request(path, method="HEAD")
                self.assertEqual((response.status, response.getheader("Content-Length"), body), (200, "10", b""))

    def test_internal_paths_and_foreign_analysis_requests_are_refused(self):
        for path in ("/server/http.py", "/../AGENTS.md", "/%2e%2e/AGENTS.md", "/library/catalog.json",
                     "/.xiyun/models/coach.key", "/legacy/frontend/index.html"):
            self.assertEqual(request(path)[0].status, 404)
        self.assertEqual(request("/not-an-api", "POST")[0].status, 405)
        self.assertEqual(request("/api/karaoke/coach", "POST", headers="Origin: https://example.org\r\n")[0].status, 403)
        self.assertEqual(request("/api/karaoke/pitch", "POST", headers="Content-Length: 1\r\n", body=b"x")[0].status, 400)

    def test_port_collision_is_reported(self):
        with patch("server.http.ThreadingHTTPServer", side_effect=OSError("Address already in use")), patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(main(["--port", "5173"]), 1)
            self.assertIn("--port 5174", stderr.getvalue())
