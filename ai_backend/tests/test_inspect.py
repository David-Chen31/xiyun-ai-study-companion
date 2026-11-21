"""D1 inspect 独立测试：单元级（WAV 实测与句界）+ 真实 HTTP 级（本机回环）。

运行（仓库根目录）：
  python -m unittest discover -s ai_backend/tests -t . -v

全部用程序合成的技术样本，不依赖真实音频与外部服务。
"""

from __future__ import annotations

import io
import json
from pathlib import Path
import threading
import unittest
import wave
from unittest import mock

from ai_backend import inspection
from ai_backend.inspection import InspectError, inspect, measure_wav
from ai_backend.multipart import build_multipart, parse_multipart
from ai_backend.replay import send_inspect
from ai_backend.server import MAX_BODY_BYTES, make_server

TOKEN = "test-token-xyz"


def wav_bytes(rate: int = 16000, channels: int = 1, sampwidth: int = 2,
              nframes: int = 16000 * 12) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(rate)
        wf.writeframes(b"\x00" * (nframes * channels * sampwidth))
    return buf.getvalue()


def meta(phrases=None, request_id="q7", revision_id="r1") -> dict:
    if phrases is None:
        phrases = [{"id": "p2", "text": "乙", "lead_ms": 4000,
                    "start_ms": 5000, "entry_ms": 5100, "end_ms": 8000}]
    return {"request_id": request_id, "revision_id": revision_id, "phrases": phrases}


def phrase(**overrides) -> dict:
    base = {"id": "p1", "text": "甲", "lead_ms": 0, "start_ms": 500, "entry_ms": 600, "end_ms": 3900}
    base.update(overrides)
    return base


class MultipartTests(unittest.TestCase):
    def test_binary_roundtrip(self):
        data = bytes(range(256)) * 4 + b"\r\n--not-a-boundary\r\n"
        body, boundary = build_multipart({"audio": data, "metadata": b"{}"},
                                         filenames={"audio": "demo.wav"})
        parts = parse_multipart(body, boundary.encode("utf-8"))
        self.assertEqual(parts["audio"], data)
        self.assertEqual(parts["metadata"], b"{}")


class MeasureWavTests(unittest.TestCase):
    def test_12s_mono(self):
        self.assertEqual(measure_wav(wav_bytes()), (12000, 16000, 1))

    def test_stereo(self):
        self.assertEqual(measure_wav(wav_bytes(channels=2)), (12000, 16000, 2))

    def test_duration_from_actual_frames_floor(self):
        # 16001 帧 @16000Hz = 1000.0625ms → 向下取整 1000ms。
        self.assertEqual(measure_wav(wav_bytes(nframes=16001))[0], 1000)
        # 12.5 秒整。
        self.assertEqual(measure_wav(wav_bytes(nframes=16000 * 12 + 8000))[0], 12500)

    def test_8bit_rejected(self):
        with self.assertRaises(InspectError) as ctx:
            measure_wav(wav_bytes(sampwidth=1))
        self.assertEqual((ctx.exception.status, ctx.exception.code), (415, "UNSUPPORTED_AUDIO"))

    def test_three_channels_rejected(self):
        with self.assertRaises(InspectError) as ctx:
            measure_wav(wav_bytes(channels=3))
        self.assertEqual(ctx.exception.status, 415)

    def test_truncated_rejected(self):
        data = wav_bytes()[:-100]
        with self.assertRaises(InspectError) as ctx:
            measure_wav(data)
        self.assertEqual((ctx.exception.status, ctx.exception.code), (415, "UNSUPPORTED_AUDIO"))
        self.assertIn("截断", ctx.exception.message)

    def test_not_wav_rejected(self):
        with self.assertRaises(InspectError) as ctx:
            measure_wav(b"this is not a wav file at all")
        self.assertEqual(ctx.exception.status, 415)

    def test_zero_frames_rejected(self):
        with self.assertRaises(InspectError) as ctx:
            measure_wav(wav_bytes(nframes=0))
        self.assertEqual(ctx.exception.status, 415)

    def test_over_10_minutes_rejected(self):
        # 601 秒 @8000Hz 单声道 ≈ 9.6 MB，真实帧数超限。
        with self.assertRaises(InspectError) as ctx:
            measure_wav(wav_bytes(rate=8000, nframes=8000 * 601))
        self.assertEqual((ctx.exception.status, ctx.exception.code), (413, "TOO_LARGE"))

    def test_over_100mb_rejected(self):
        with mock.patch.object(inspection, "MAX_FILE_BYTES", 10):
            with self.assertRaises(InspectError) as ctx:
                measure_wav(b"\x00" * 11)
        self.assertEqual((ctx.exception.status, ctx.exception.code), (413, "TOO_LARGE"))
        self.assertEqual(ctx.exception.field, "audio")

    def test_d2_disk_sample_measures_actual_wav(self):
        sample = Path(__file__).resolve().parents[1] / "samples" / "demo_12s.wav"
        measured = measure_wav(sample.read_bytes())
        self.assertEqual(measured, (12000, 16000, 1))



class MetadataTests(unittest.TestCase):
    def test_missing_request_id(self):
        m = meta()
        del m["request_id"]
        with self.assertRaises(InspectError) as ctx:
            inspect(wav_bytes(), m)
        self.assertEqual((ctx.exception.status, ctx.exception.field), (400, "request_id"))

    def test_empty_phrases(self):
        with self.assertRaises(InspectError) as ctx:
            inspect(wav_bytes(), meta(phrases=[]))
        self.assertEqual((ctx.exception.status, ctx.exception.field), (400, "phrases"))

    def test_duplicate_id(self):
        with self.assertRaises(InspectError) as ctx:
            inspect(wav_bytes(), meta(phrases=[phrase(), phrase()]))
        self.assertEqual((ctx.exception.status, ctx.exception.code), (422, "INVALID_BOUNDARY"))
        self.assertEqual(ctx.exception.field, "phrases[1].id")

    def test_non_integer_time(self):
        with self.assertRaises(InspectError) as ctx:
            inspect(wav_bytes(), meta(phrases=[phrase(end_ms="3900")]))
        self.assertEqual((ctx.exception.status, ctx.exception.field), (400, "phrases[0].end_ms"))

    def test_boolean_time_rejected(self):
        with self.assertRaises(InspectError) as ctx:
            inspect(wav_bytes(), meta(phrases=[phrase(lead_ms=True)]))
        self.assertEqual(ctx.exception.status, 400)

    def test_bad_entry_type(self):
        with self.assertRaises(InspectError) as ctx:
            inspect(wav_bytes(), meta(phrases=[phrase(entry_ms="600")]))
        self.assertEqual(ctx.exception.status, 400)


class BoundaryTests(unittest.TestCase):
    def check(self, p, field):
        with self.assertRaises(InspectError) as ctx:
            inspect(wav_bytes(), meta(phrases=[p]))
        self.assertEqual((ctx.exception.status, ctx.exception.code), (422, "INVALID_BOUNDARY"))
        self.assertEqual(ctx.exception.field, field)
        return ctx.exception

    def test_end_beyond_audio(self):
        err = self.check(phrase(end_ms=13000), "phrases[0].end_ms")
        self.assertEqual(err.message, "结束时间超出音频")

    def test_negative_lead(self):
        self.check(phrase(lead_ms=-1), "phrases[0].lead_ms")

    def test_start_before_lead(self):
        self.check(phrase(lead_ms=600, start_ms=500), "phrases[0].start_ms")

    def test_end_not_after_start(self):
        self.check(phrase(start_ms=3900, end_ms=3900), "phrases[0].end_ms")

    def test_entry_below_lead(self):
        self.check(phrase(lead_ms=700, start_ms=800, entry_ms=600), "phrases[0].entry_ms")

    def test_entry_at_end_rejected(self):
        self.check(phrase(entry_ms=3900), "phrases[0].entry_ms")

    def test_entry_at_lead_allowed(self):
        result = inspect(wav_bytes(), meta(phrases=[phrase(entry_ms=0)]))
        self.assertEqual(result["phrase_count"], 1)

    def test_null_entry_allowed(self):
        result = inspect(wav_bytes(), meta(phrases=[phrase(entry_ms=None)]))
        self.assertEqual(result["phrase_count"], 1)

    def test_end_equal_duration_allowed(self):
        result = inspect(wav_bytes(), meta(phrases=[phrase(end_ms=12000)]))
        self.assertEqual(result["duration_ms"], 12000)

    def test_second_phrase_field_index(self):
        ok = phrase()
        bad = phrase(id="p2", end_ms=12001)
        with self.assertRaises(InspectError) as ctx:
            inspect(wav_bytes(), meta(phrases=[ok, bad]))
        self.assertEqual(ctx.exception.field, "phrases[1].end_ms")

    def test_d2_three_phrase_disk_metadata_allows_null_entry(self):
        sample_dir = Path(__file__).resolve().parents[1] / "samples"
        metadata = json.loads((sample_dir / "success_three.json").read_text(encoding="utf-8"))
        result = inspect((sample_dir / "demo_12s.wav").read_bytes(), metadata)
        self.assertEqual(result["phrase_count"], 3)
        self.assertEqual(result["duration_ms"], 12000)

    def test_d2_three_phrase_disk_metadata_points_to_third_end(self):
        sample_dir = Path(__file__).resolve().parents[1] / "samples"
        metadata = json.loads((sample_dir / "invalid_three_boundary.json").read_text(encoding="utf-8"))
        with self.assertRaises(InspectError) as ctx:
            inspect((sample_dir / "demo_12s.wav").read_bytes(), metadata)
        self.assertEqual((ctx.exception.status, ctx.exception.code, ctx.exception.field),
                         (422, "INVALID_BOUNDARY", "phrases[2].end_ms"))


class HttpTests(unittest.TestCase):
    """在回环随机端口起真实服务，走真实 HTTP。"""

    @classmethod
    def setUpClass(cls):
        cls.server = make_server("127.0.0.1", 0, TOKEN)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.wav = wav_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def call(self, metadata: dict, token=TOKEN, audio=None):
        return send_inspect(self.base, token,
                            audio if audio is not None else self.wav,
                            json.dumps(metadata, ensure_ascii=False).encode("utf-8"))

    def test_success_single_phrase(self):
        status, body = self.call(meta())
        self.assertEqual(status, 200)
        self.assertEqual(body, {
            "request_id": "q7", "revision_id": "r1", "processor_version": "wav-inspect-1",
            "duration_ms": 12000, "sample_rate_hz": 16000, "channels": 1, "phrase_count": 1,
        })

    def test_success_three_phrases(self):
        phrases = [
            {"id": "p1", "text": "甲", "lead_ms": 0, "start_ms": 500, "entry_ms": 600, "end_ms": 3900},
            {"id": "p2", "text": "乙", "lead_ms": 4000, "start_ms": 5000, "entry_ms": 5100, "end_ms": 8000},
            {"id": "p3", "text": "丙", "lead_ms": 8100, "start_ms": 9000, "entry_ms": None, "end_ms": 12000},
        ]
        status, body = self.call(meta(phrases=phrases, request_id="q9"))
        self.assertEqual(status, 200)
        self.assertEqual(body["phrase_count"], 3)
        self.assertEqual(body["duration_ms"], 12000)

    def test_422_end_beyond_audio(self):
        status, body = self.call(meta(request_id="q8", phrases=[phrase(id="p2", end_ms=13000)]))
        self.assertEqual(status, 422)
        self.assertEqual(body, {
            "request_id": "q8", "code": "INVALID_BOUNDARY",
            "message": "结束时间超出音频", "field": "phrases[0].end_ms",
        })

    def test_401_wrong_token(self):
        status, body = self.call(meta(), token="wrong-token")
        self.assertEqual(status, 401)
        self.assertEqual(body["code"], "INVALID_TOKEN")

    def test_401_missing_token(self):
        status, body = self.call(meta(), token="")
        self.assertEqual(status, 401)
        self.assertEqual(body["code"], "INVALID_TOKEN")

    def test_400_bad_json(self):
        status, body = send_inspect(self.base, TOKEN, self.wav, b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "INVALID_INPUT")
        self.assertEqual(body["field"], "metadata")

    def test_400_missing_audio_part(self):
        payload, boundary = build_multipart({"metadata": json.dumps(meta()).encode("utf-8")})
        import urllib.request
        import urllib.error
        req = urllib.request.Request(
            self.base + "/internal/v1/inspect", data=payload, method="POST",
            headers={"Authorization": f"Bearer {TOKEN}",
                     "Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status, body = resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            status, body = err.code, json.loads(err.read())
        self.assertEqual(status, 400)
        self.assertEqual(body["field"], "audio")

    def test_413_oversized_content_length(self):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/internal/v1/inspect", body=b"", headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "multipart/form-data; boundary=x",
            "Content-Length": str(MAX_BODY_BYTES + 1),
        })
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
        self.assertEqual(resp.status, 413)
        self.assertEqual(body["code"], "TOO_LARGE")

    def test_415_bad_audio_over_http(self):
        status, body = self.call(meta(), audio=wav_bytes(sampwidth=1))
        self.assertEqual(status, 415)
        self.assertEqual(body["code"], "UNSUPPORTED_AUDIO")
        self.assertEqual(body["request_id"], "q7")

    def test_404_unknown_path(self):
        import urllib.request
        import urllib.error
        try:
            with urllib.request.urlopen(self.base + "/nope", timeout=10) as resp:
                status = resp.status
        except urllib.error.HTTPError as err:
            status = err.code
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
