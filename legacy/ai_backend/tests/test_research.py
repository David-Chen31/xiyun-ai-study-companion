import unittest
import io
import math
import random
import wave
from array import array
from pathlib import Path

from ai_backend.research.acoustic_alignment import acoustic_alignment
from ai_backend.research.acoustics import entry_candidate, pitch_track, voiced_entry_candidate
from ai_backend.research.alignment import anchor_map
from ai_backend.research.rhythm import rhythm_candidates
from ai_backend.transcribe import timestamped_candidates


SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "demo_12s.wav"


class ResearchBaselinesTests(unittest.TestCase):
    def test_entry_abstains_on_route_change_and_finds_synthetic_signal(self):
        audio = SAMPLE.read_bytes()
        config = {"capture_route": "r1", "calibrated_route": "r1", "delay_ms": 0, "reference_start_ms": 0,
                  "rate": 1, "reference_window_ms": [1000, 1200]}
        self.assertEqual(entry_candidate(audio, config | {"capture_route": "r2"})["status"], "abstained")
        result = entry_candidate(audio, config)
        self.assertEqual(result["relation"], "early")
        self.assertLessEqual(abs(result["captured_ms"] - 500), 25)

    def test_pitch_track_leaves_silence_empty(self):
        values = pitch_track(SAMPLE.read_bytes())
        self.assertIsNone(values[0]["f0_hz"])
        self.assertTrue(any(item["f0_hz"] for item in values if 1000 <= item["time_ms"] <= 2000))

    def test_voiced_entry_looks_back_to_a_soft_consonant_and_ignores_noise_only(self):
        rate = 16000
        randomizer = random.Random(42)
        weak = [randomizer.randrange(-220,221) for _ in range(rate // 10)]
        vocal = [round(2400 * math.sin(2 * math.pi * 240 * i / rate)) for i in range(rate // 2)]

        def clip(tail):
            output = io.BytesIO()
            with wave.open(output, "wb") as audio:
                audio.setparams((1, 2, rate, 0, "NONE", "not compressed"))
                audio.writeframes(array("h", [0] * (rate * 2 // 5) + weak + tail).tobytes())
            return output.getvalue()

        timing = {"capture_route":"a", "calibrated_route":"a", "delay_ms":0,
                  "reference_start_ms":0, "rate":1, "reference_window_ms":[300, 700]}
        result = voiced_entry_candidate(clip(vocal), timing)
        self.assertEqual(result["status"], "candidate")
        self.assertLessEqual(abs(result["captured_ms"] - 400), 25)
        self.assertEqual(voiced_entry_candidate(clip(weak * 4), timing)["status"], "abstained")

    def test_transcript_timestamp_parser_uses_source_offset_and_drops_unbounded_text(self):
        predictions = timestamped_candidates("<|0.50|>甲<|1.40|><|1.40|>乙<|3.20|>无结束标记", 4000, 6000)
        self.assertEqual(predictions, [{"text":"甲","start_ms":4500,"end_ms":5400},
                                       {"text":"乙","start_ms":5400,"end_ms":6000}])

    def test_separate_anchors_never_bridge_the_gap(self):
        result = anchor_map({"left_revision": "a", "right_revision": "b",
                             "sections": [[[0, 200], [1000, 1200]], [[2000, 2300], [3000, 3300]]],
                             "query_ms": [0, 500, 1500, 2500, 3000]})
        self.assertEqual([item["right_ms"] for item in result["mapping"]], [200, 700, None, 2800, 3300])

    def test_rhythm_compares_marked_hits_and_respects_free_timing(self):
        audio = SAMPLE.read_bytes()
        self.assertEqual(rhythm_candidates(audio, {"rhythm_mode": "free"})["status"], "abstained")
        result = rhythm_candidates(audio, {"rhythm_mode": "marked", "reference_hits_ms": [500, 5000, 9000]})
        self.assertEqual(result["status"], "candidate")
        self.assertTrue(all(abs(item["difference_ms"]) <= 30 for item in result["matches"]))

    def test_acoustic_path_leaves_silent_and_excluded_regions_empty(self):
        audio = SAMPLE.read_bytes()
        config = {"left_revision": "left", "right_revision": "right",
                  "sections": [[[0, 0], [1000, 1000]], [[4200, 4200], [4700, 4700]]],
                  "query_ms": [500, 2500, 4500]}
        result = acoustic_alignment(config, audio, audio)
        self.assertEqual([item["status"] for item in result["sections"]], ["candidate", "abstained"])
        self.assertEqual([point["right_ms"] for point in result["mapping"]], [500, None, None])
        self.assertEqual(result["mapping"][-1]["anchor_right_ms"], 4500)
