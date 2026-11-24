"""用真实 HTTP 走通老师、审核员、学员和跨班隔离。"""

import http.client
import hashlib
import io
import json
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
import wave
from pathlib import Path
from unittest.mock import PropertyMock, patch

from ai_backend.server import make_server as make_c_server

from backend import ROOT
from backend.course import normalize_grant, read_json
from backend.importer import Inspector
from backend.platform.access import create_account
from backend.platform.uploads import UploadWorkers
from backend.platform.transcripts import TranscriptWorker
from backend.store import Store
from backend.tests.test_integration import DEMO_AUDIO, running
from backend.web import make_server


PASSWORD = "test-only-phrase"


class PlatformTests(unittest.TestCase):
    def setUp(self):
        data = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(data)
        self.grant = read_json(ROOT / "contracts" / "demo-grant.json")
        self.demo = read_json(ROOT / "contracts" / "demo-course.json")
        self.store.register_grant(normalize_grant(self.grant))
        for name, role in [("operator", "admin"), ("author", "teacher"), ("reader", "reviewer"),
                           ("learner", "student"), ("outsider", "student")]:
            create_account(self.store, name, name, role, PASSWORD)
        c_port = self.enterContext(running(make_c_server("127.0.0.1", 0, "test-token")))
        self.port = self.enterContext(running(make_server(
            self.store, 0, "c1", platform=True, inspector=Inspector(c_port, "test-token"),
        )))
        self.cookies = {}

    def request(self, method, path, *, who=None, data=None, binary=None, headers=None, status=200):
        base = {"Origin": f"http://127.0.0.1:{self.port}"}
        base.update(headers or {})
        if who:
            base["Cookie"] = self.cookies[who]
        body = binary
        if data is not None:
            base["Content-Type"] = "application/json"
            body = json.dumps(data).encode()
        elif binary is not None:
            base["Content-Type"] = "application/octet-stream"
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=base)
            with connection.getresponse() as response:
                payload = response.read()
                if payload and response.getheader("Content-Type", "").startswith("application/json"):
                    payload = json.loads(payload)
                self.assertEqual(response.status, status, (method, path, payload))
                return payload, dict(response.getheaders())
        finally:
            connection.close()

    def send(self, method, path, **kwargs):
        return self.request(method, path, **kwargs)[0]

    def login(self, who):
        _, headers = self.request("POST", "/v1/sessions", data={"account_id": who, "password": PASSWORD})
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.cookies[who] = headers["Set-Cookie"].split(";")[0]

    def draft_flow(self):
        for who in ("operator", "author", "reader", "learner", "outsider"):
            self.login(who)
        group = self.send("POST", "/v1/classes", who="operator", data={"title": "课堂甲"}, status=201)
        for who in ("author", "reader", "learner"):
            self.send("PUT", f"/v1/classes/{group['id']}/members/{who}", who="operator", data={})
        draft = self.send("POST", "/v1/drafts", who="author", status=201,
                          data={"class_id": group["id"], "title": "听辨课", "grant_ref": self.grant["grant_ref"]})
        return group, draft

    def test_draft_review_progress_withdraw_and_private_media(self):
        group, draft = self.draft_flow()
        base = f"/v1/drafts/{draft['id']}"
        sample = DEMO_AUDIO.read_bytes()
        self.send("GET", base, who="learner", status=403)
        self.send("GET", base, who="reader", status=403)
        self.send("PUT", base + "/media", who="author", binary=b"bad",
                  headers={"If-Match": str(draft["version"])}, status=403)
        draft = self.send("PUT", base + "/media", who="author", binary=sample,
                          headers={"If-Match": str(draft["version"])})
        self.assertEqual(draft["duration_ms"], 12000)
        start = draft["version"]
        phrases = self.demo["phrases"]
        data = {"title": "听辨课", "version": start, "phrases": phrases}
        bad = [phrases[0] | {"end_ms": 12001}]
        self.send("PATCH", base, who="author", data=data | {"phrases": bad}, status=422)
        phrases[0] |= {"role": "角色甲", "hint": "听完引导再开口", "term": "过门",
                       "definition": "这里用于引向唱句的伴奏", "source": "合成样例释义"}
        phrases[1] |= {"term": "板式", "definition": "唱腔节奏结构的类别", "source": "合成样例 · 术语二"}
        draft = self.send("PATCH", base, who="author", data=data)
        self.send("PATCH", base, who="author", data=data, status=409)
        draft = self.send("POST", base + "/submit", who="author", data={"version": draft["version"]})
        self.send("POST", base + "/review", who="author",
                  data={"decision": "publish", "version": draft["version"]}, status=403)
        body = self.send("GET", base + "/media", who="reader", headers={"Range": "bytes=5-20"}, status=206)
        self.assertEqual(body, sample[5:21])
        draft = self.send("POST", base + "/review", who="reader",
                          data={"decision": "return", "reason": "复核标注", "version": draft["version"]})
        self.assertEqual(draft["feedback"], "复核标注")
        draft = self.send("POST", base + "/submit", who="author", data={"version": draft["version"]})
        release = self.send("POST", base + "/review", who="reader",
                            data={"decision": "publish", "version": draft["version"]})
        course_id = release["course_id"]
        course_url = f"/v1/courses/{course_id}"
        result = self.send("GET", course_url, who="learner")
        self.assertEqual(result["phrases"][0]["term"], "过门")
        self.assertTrue(result["class_course"])
        clip = self.send("GET", result["stream_url"] + "&phrase_id=p2", who="learner")
        with wave.open(io.BytesIO(clip), "rb") as audio:
            self.assertEqual((audio.getnframes(), audio.getframerate()), (64000, 16000))
            self.assertEqual(audio.readframes(audio.getnframes()), sample[44 + 128000:44 + 256000])
        self.send("GET", course_url, status=401)
        self.send("GET", result["stream_url"], who="outsider", status=403)
        self.assertEqual(self.send("GET", "/v1/catalog", who="outsider")["courses"], [])
        copy = self.send("POST", base + "/fork", who="author", data={"version": release["version"]})
        self.assertNotEqual(copy["id"], draft["id"])
        self.assertEqual(self.send("GET", course_url, who="learner")["revision_id"], result["revision_id"])
        copy_path = f"/v1/drafts/{copy['id']}"
        linked = [phrases[0] | {"alternate_course_id": course_id, "alternate_phrase_id": "p2"}]
        copy = self.send("PATCH", copy_path, who="author", data={
            "version": copy["version"], "title": "对照版", "phrases": linked,
        })
        copy = self.send("POST", copy_path + "/submit", who="author", data={"version": copy["version"]})
        self.send("POST", copy_path + "/review", who="reader", data={"version": copy["version"], "decision": "publish"})
        comparison = f"/v1/courses/{copy['id']}/alternatives?phrase_id=p1"
        alternate = self.send("GET", comparison, who="learner")["alternative"]
        self.assertEqual((alternate["course_id"], alternate["phrases"][0]["id"]), (course_id, "p2"))
        self.send("GET", comparison, who="outsider", status=403)

        question = {"course_id": course_id, "question": "什么是过门？"}
        citations = self.send("POST", "/v1/questions", who="learner", data=question)["citations"]
        self.assertEqual(citations[0]["source"], "合成样例释义")
        term_comparison = self.send("POST", "/v1/questions", who="learner",
                                    data=question | {"question": "过门和板式有什么区别？"})
        self.assertTrue(term_comparison["comparison"])
        self.assertEqual([item["phrase_id"] for item in term_comparison["citations"]], ["p1", "p2"])
        self.assertFalse(self.send("POST", "/v1/questions", who="learner",
                                   data=question | {"question": "过门和无收录词有什么区别？"})["citations"])
        self.send("POST", "/v1/questions", who="outsider", data=question, status=403)
        self.assertFalse(self.send("POST", "/v1/questions", who="learner",
                                   data=question | {"question": "过门最早来自哪里？"})["citations"])

        event = {"event_id": str(uuid.uuid4()), "course_id": course_id, "revision_id": result["revision_id"],
                 "phrase_id": "p1", "generation": 0, "action": "complete"}
        for _ in range(2):
            self.send("POST", "/v1/practice-events", who="learner", data=event)
        self.send("POST", "/v1/practice-events", who="learner", data=event | {"audio": "unaccepted"}, status=400)
        self.send("POST", "/v1/practice-events", who="learner",
                  data=event | {"event_id": str(uuid.uuid4()), "action": "mark"})
        summary = self.send("GET", f"/v1/classes/{group['id']}/summary", who="author")
        course_summary = next(item for item in summary["courses"] if item["course_id"] == course_id)
        self.assertEqual(course_summary["phrases"][0]["completed"], 1)
        self.assertEqual(course_summary["phrases"][0]["marked"], 1)
        self.assertEqual(self.send("GET", course_url, who="learner")["progress"]["phrase_id"], "p1")
        reset = self.send("DELETE", f"/v1/me/progress?course_id={course_id}", who="learner")
        self.assertEqual(reset["generation"], 1)
        self.send("POST", "/v1/practice-events", who="learner", data=event, status=409)
        self.send("POST", course_url + "/withdraw", who="reader", data={"reason": "课程已结束"})
        self.send("GET", result["stream_url"], who="learner", headers={"Range": "bytes=0-10"}, status=410)
        self.assertIsNone(self.send("GET", comparison, who="learner")["alternative"])
        self.send("POST", f"/v1/grants/{self.grant['grant_ref']}/revoke", who="operator", data={"reason": "许可撤销"})
        self.send("GET", f"/v1/courses/{copy['id']}", who="learner", status=410)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools are optional")
    def test_video_source_uses_explicit_scope_and_retains_clock_offset(self):
        group, _ = self.draft_flow()
        work = self.store.path.parent
        video = work / "synthetic.mp4"
        subprocess.run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=64x64:r=25:d=1",
            "-itsoffset", "0.35", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=0.4",
            "-c:v", "mpeg4", "-c:a", "alac", str(video),
        ], capture_output=True, check=True, timeout=20)
        original = video.read_bytes()
        for scope in ([], ["video_audio_extraction"], ["video_audio_extraction", "video_preview"]):
            grant = self.grant | {"grant_ref": f"video-{len(scope)}", "audio_sha256": hashlib.sha256(original).hexdigest(),
                                  "purposes": self.grant["purposes"] + scope}
            self.send("POST", "/v1/grants", who="operator", data=grant, status=201)
            draft = self.send("POST", "/v1/drafts", who="author", status=201,
                              data={"class_id": group["id"], "title": "合成视频", "grant_ref": grant["grant_ref"]})
            response = self.send("PUT", f"/v1/drafts/{draft['id']}/media", who="author", binary=original,
                                 headers={"If-Match": str(draft["version"])}, status=200 if scope else 403)
            if not scope:
                self.assertEqual(response["code"], "VIDEO_SCOPE_REQUIRED")
                continue
            self.assertEqual(response["source_type"], "mp4")
            self.assertAlmostEqual(response["audio_offset_ms"], 350, delta=1)
            self.assertAlmostEqual(response["duration_ms"], 400, delta=5)
            self.assertEqual(len(response["waveform"]), 160)
            wav = self.send("GET", response["stream_url"], who="author")
            self.assertTrue(wav.startswith(b"RIFF"))
            video_path = f"/v1/drafts/{draft['id']}/video"
            if "video_preview" not in scope:
                self.assertIsNone(response["video_preview"])
                self.send("GET", video_path, who="author", status=404)
                continue
            preview = response["video_preview"]
            self.assertAlmostEqual(preview["audio_start_ms"] - preview["video_start_ms"], 350, delta=60)
            content, headers = self.request("GET", video_path, who="author",
                                            headers={"Range": "bytes=0-63"}, status=206)
            self.assertEqual(headers["Content-Type"], "video/mp4")
            self.assertEqual(content[4:8], b"ftyp")
            self.send("GET", video_path, who="learner", status=403)
            self.send("GET", video_path, who="reader", status=403)
            self.send("POST", "/v1/grants/" + grant["grant_ref"] + "/revoke", who="operator",
                      data={"reason": "结束测试授权"})
            self.send("HEAD", video_path, who="author", status=410)

    def test_async_upload_can_be_polled_and_retried(self):
        _, draft = self.draft_flow()
        base = f"/v1/drafts/{draft['id']}"
        headers = {"If-Match": str(draft["version"]), "Prefer": "respond-async"}

        def wait_for(job_id):
            for _ in range(40):
                job = self.send("GET", base + "/media-job", who="author")["job"]
                if job["id"] == job_id and job["state"] != "processing":
                    return job
                time.sleep(.05)
            self.fail("媒体任务没有结束")

        failed = self.send("PUT", base + "/media", who="author", binary=b"bad",
                           headers=headers, status=202)
        outcome = wait_for(failed["id"])
        self.assertEqual((outcome["state"], outcome["error"]["code"]), ("failed", "GRANT_AUDIO_MISMATCH"))
        self.assertEqual(self.send("GET", base, who="author")["version"], draft["version"])
        job = self.send("PUT", base + "/media", who="author", binary=DEMO_AUDIO.read_bytes(),
                        headers=headers, status=202)
        self.assertEqual(wait_for(job["id"])["state"], "ready")
        ready = self.send("GET", base, who="author")
        self.assertEqual((ready["version"], ready["duration_ms"]), (draft["version"] + 1, 12000))
        self.send("GET", base + "/media-job", who="learner", status=403)
        old_job = uuid.uuid4().hex
        with self.store.connect(write=True) as db:
            db.execute("INSERT INTO media_jobs VALUES (?, ?, ?, 'processing', '2026-01-01', NULL, NULL)",
                       (old_job, draft["id"], ready["version"]))
        UploadWorkers(self.store, None).recover_interrupted()
        recovered = self.send("GET", base + "/media-job", who="author")["job"]
        self.assertEqual((recovered["id"], recovered["state"]), (old_job, "interrupted"))
        self.assertEqual(self.send("GET", base, who="author")["version"], ready["version"])

    def test_transcription_requires_scope_and_does_not_change_phrases(self):
        group, old = self.draft_flow()
        old_base = f"/v1/drafts/{old['id']}/transcription"
        self.send("POST", old_base, who="author",
                  data={"version":old["version"], "start_ms":0, "end_ms":4000}, status=403)
        grant = self.grant | {"grant_ref":"transcription-demo", "purposes":self.grant["purposes"] + ["lyrics_transcription"]}
        self.send("POST", "/v1/grants", who="operator", data=grant, status=201)
        draft = self.send("POST", "/v1/drafts", who="author", status=201,
                          data={"class_id":group["id"], "title":"转写校订", "grant_ref":grant["grant_ref"]})
        base = f"/v1/drafts/{draft['id']}"
        draft = self.send("PUT", base + "/media", who="author", binary=DEMO_AUDIO.read_bytes(),
                          headers={"If-Match":str(draft["version"])})
        args = {"version":draft["version"], "start_ms":4000, "end_ms":8000}
        with patch.object(TranscriptWorker, "configured", new_callable=PropertyMock, return_value=False):
            self.send("POST", base + "/transcription", who="author", data=args, status=503)
        candidate = {"text":"候选唱词", "start_ms":4900, "end_ms":7700}
        with patch.object(TranscriptWorker, "configured", new_callable=PropertyMock, return_value=True), patch(
            "backend.platform.transcripts.run_transcription", return_value=[candidate],
        ) as infer:
            task = self.send("POST", base + "/transcription", who="author", data=args, status=202)
            for _ in range(40):
                status = self.send("GET", base + "/transcription", who="author")["job"]
                if status["state"] != "processing":
                    break
                time.sleep(.05)
            self.assertEqual((status["id"], status["state"], status["candidates"]), (task["id"], "ready", [candidate]))
            _, wav, start, end = infer.call_args.args
            self.assertEqual((start, end), (4000, 8000))
            with wave.open(io.BytesIO(wav)) as audio:
                self.assertEqual(audio.getnframes(), 64000)
        self.assertEqual(self.send("GET", base, who="author")["content"]["phrases"], [])
        self.assertEqual(self.send("GET", base, who="author")["version"], draft["version"])
        self.send("GET", base + "/transcription", who="learner", status=403)
        self.send("POST", "/v1/grants/transcription-demo/revoke", who="operator", data={"reason":"实测结束"})
        self.send("GET", base + "/transcription", who="author", status=410)

    def test_session_signout_and_write_origin(self):
        self.send("GET", "/workspace/")
        self.send("POST", "/v1/sessions", data={"account_id": "operator", "password": PASSWORD},
                  headers={"Origin": "http://example.test"}, status=403)
        self.login("operator")
        self.send("GET", "/v1/workspace", who="operator")
        self.send("DELETE", "/v1/sessions/current", who="operator")
        self.send("GET", "/v1/workspace", who="operator", status=401)


if __name__ == "__main__":
    unittest.main()
