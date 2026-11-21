"""D2 回归：真实 WAV 容器、帧边界及可保存的本机验证记录。"""

import contextlib
import io
import json
from pathlib import Path
import struct
import tempfile
import threading
import unittest

from ai_backend.d2_validate import main
from ai_backend.inspection import InspectError, measure_wav
from ai_backend.replay import send_inspect
from ai_backend.server import make_server
from ai_backend.tests.test_inspect import meta, wav_bytes


def riff(chunks):
    payload = b"WAVE" + b"".join(
        name + struct.pack("<I", len(data)) + data + (b"\x00" if len(data) % 2 else b"")
        for name, data in chunks
    )
    return b"RIFF" + struct.pack("<I", len(payload)) + payload


def fmt(channels=1, rate=16000, encoding=1):
    return struct.pack("<HHIIHH", encoding, channels, rate, rate * channels * 2, channels * 2, 16)


class D2WavTests(unittest.TestCase):
    def assert_bad_audio(self, audio):
        with self.assertRaises(InspectError) as raised:
            measure_wav(audio)
        self.assertEqual((raised.exception.status, raised.exception.field), (415, "audio"))

    def test_44100_hz_stereo_is_12_seconds_not_24(self):
        self.assertEqual(measure_wav(wav_bytes(rate=44100, channels=2, nframes=44100 * 12)),
                         (12000, 44100, 2))

    def test_48000_hz_stereo(self):
        self.assertEqual(measure_wav(wav_bytes(rate=48000, channels=2, nframes=48000 * 12)),
                         (12000, 48000, 2))

    def test_extra_metadata_chunk_and_odd_padding_are_allowed(self):
        audio = riff([(b"fmt ", fmt()), (b"JUNK", b"abc"), (b"data", b"\0\0" * 16000)])
        self.assertEqual(measure_wav(audio), (1000, 16000, 1))

    def test_partial_stereo_frame_is_rejected(self):
        # A stereo 16-bit frame needs four bytes, not two.
        self.assert_bad_audio(riff([(b"fmt ", fmt(channels=2)), (b"data", b"\0" * 6)]))

    def test_partial_mono_frame_with_valid_riff_size_is_rejected(self):
        self.assert_bad_audio(riff([(b"fmt ", fmt()), (b"data", b"\0" * 3)]))

    def test_truncated_metadata_after_complete_audio_is_rejected(self):
        audio = riff([(b"fmt ", fmt()), (b"data", b"\0\0" * 16000), (b"JUNK", b"abcd")])
        self.assert_bad_audio(audio[:-1])

    def test_inconsistent_riff_length_is_rejected(self):
        audio = bytearray(wav_bytes())
        struct.pack_into("<I", audio, 4, len(audio) - 9)
        self.assert_bad_audio(bytes(audio))

    def test_wrong_block_align_and_byte_rate_are_rejected(self):
        for offset, value in ((28, 123), (32, 4)):
            with self.subTest(offset=offset):
                audio = bytearray(wav_bytes())
                struct.pack_into("<I" if offset == 28 else "<H", audio, offset, value)
                self.assert_bad_audio(bytes(audio))

    def test_non_pcm_encoding_is_rejected(self):
        self.assert_bad_audio(riff([(b"fmt ", fmt(encoding=3)), (b"data", b"\0\0" * 16000)]))

    def test_multiple_data_chunks_are_rejected(self):
        self.assert_bad_audio(riff([(b"fmt ", fmt()), (b"data", b"\0\0"), (b"data", b"\0\0")]))

    def test_exact_duration_limit_and_one_frame_above(self):
        # Low sample rate keeps the fixture small while exercising the exact limit.
        self.assertEqual(measure_wav(wav_bytes(rate=100, nframes=60000))[0], 600000)
        with self.assertRaises(InspectError) as raised:
            measure_wav(wav_bytes(rate=16000, nframes=16000 * 600 + 1))
        self.assertEqual((raised.exception.status, raised.exception.code), (413, "TOO_LARGE"))


class D2ReportTests(unittest.TestCase):
    def run_case(self, root, metadata):
        audio_path = root / "demo.wav"
        metadata_path = root / "metadata.json"
        output_path = root / "report.json"
        audio_path.write_bytes(wav_bytes())
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            code = main(["--audio", str(audio_path), "--metadata", str(metadata_path),
                         "--source", "合成测试样本", "--output", str(output_path)])
        return code, json.loads(output_path.read_text(encoding="utf-8"))

    def test_report_records_measured_frames_hash_and_source(self):
        with tempfile.TemporaryDirectory() as folder:
            code, report = self.run_case(Path(folder), meta())
        self.assertEqual(code, 0)
        self.assertEqual((report["duration_ms"], report["frame_count"], report["sample_width_bits"]),
                         (12000, 192000, 16))
        self.assertEqual(report["source"], "合成测试样本")
        self.assertEqual(len(report["audio_sha256"]), 64)
        self.assertGreaterEqual(report["elapsed_ms"], 0)

    def test_failure_report_preserves_request_id_and_exact_field(self):
        metadata = meta()
        metadata["phrases"][0]["end_ms"] = 12001
        with tempfile.TemporaryDirectory() as folder:
            code, report = self.run_case(Path(folder), metadata)
        self.assertEqual((code, report["http_status"], report["request_id"], report["field"]),
                         (1, 422, "q7", "phrases[0].end_ms"))

    def test_output_cannot_overwrite_input(self):
        with tempfile.TemporaryDirectory() as folder:
            audio_path = Path(folder) / "demo.wav"
            original = wav_bytes()
            audio_path.write_bytes(original)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                main(["--audio", str(audio_path), "--metadata", str(Path(folder) / "m.json"),
                      "--output", str(audio_path)])
            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(audio_path.read_bytes(), original)


class D2HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server("127.0.0.1", 0, "d2-test-token")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def test_missing_entry_is_400_not_internal_server_failure(self):
        metadata = meta()
        del metadata["phrases"][0]["entry_ms"]
        status, body = send_inspect(self.base, "d2-test-token", wav_bytes(), json.dumps(metadata).encode())
        self.assertEqual((status, body["code"], body["field"]),
                         (400, "INVALID_INPUT", "phrases[0].entry_ms"))

    def test_third_phrase_error_over_http_and_null_entry_success(self):
        samples = Path(__file__).resolve().parents[1] / "samples"
        audio = (samples / "demo_12s.wav").read_bytes()
        good_status, good = send_inspect(self.base, "d2-test-token", audio,
                                        (samples / "success_three.json").read_bytes())
        bad_status, bad = send_inspect(self.base, "d2-test-token", audio,
                                      (samples / "invalid_three_boundary.json").read_bytes())
        self.assertEqual((good_status, good["phrase_count"]), (200, 3))
        self.assertEqual((bad_status, bad["request_id"], bad["field"]),
                         (422, "q10", "phrases[2].end_ms"))
