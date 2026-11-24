"""Verify the packaged public fixtures and standalone HTTP runner."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from ai_backend.check_basics import load_assets, main


class BasicPackTests(unittest.TestCase):
    def test_pack_contains_speech_effect_tones_and_bad_format_with_hashes(self):
        manifest, assets = load_assets()
        self.assertFalse(manifest["teacher_reviewed"])
        self.assertEqual(set(assets), {"public-speech", "public-pluck", "synthetic-three",
                                       "public-pluck-unsupported-8bit"})
        self.assertEqual(assets["public-speech"]["info"]["measured_offline"]["frame_count"], 485100)
        self.assertEqual(assets["public-pluck"]["info"]["expected"]["duration_ms"], 299)

    def test_one_command_starts_service_runs_cases_and_saves_report(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "results.json"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = main(["--output", str(output)])
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual((report["case_count"], report["passed_count"]), (12, 12))
        self.assertTrue(report["all_passed"])
        self.assertEqual(report["service_mode"], "temporary_loopback_service")

    def test_changed_sample_is_detected_before_validation(self):
        with mock.patch("ai_backend.check_basics.hashlib.sha256") as digest:
            digest.return_value.hexdigest.return_value = "invalid"
            with self.assertRaisesRegex(ValueError, "校验值不匹配"):
                load_assets()
