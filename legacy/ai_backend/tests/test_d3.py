"""D3 regression for protocol errors, lifecycle, and caller-side correlation."""

import contextlib
import copy
import http.client
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

from ai_backend.__main__ import main as service_main
from ai_backend.check_d3 import main as d3_main
from ai_backend.inspection import MAX_METADATA_BYTES, InspectError, decode_metadata, inspect
from ai_backend.multipart import build_multipart, parse_multipart
from ai_backend.replay import send_inspect, validate_success
from ai_backend.server import INSPECT_PATH, MAX_BODY_BYTES, make_server
from ai_backend.tests.test_inspect import meta, wav_bytes

TOKEN = "d3-test-token"


class D3MultipartTests(unittest.TestCase):
    def setUp(self):
        self.body, boundary = build_multipart({"audio": b"\0\xff\r\n", "metadata": b"{}"})
        self.boundary = boundary.encode()

    def reject(self, body, boundary=None):
        with self.assertRaises(InspectError) as caught:
            parse_multipart(body, boundary or self.boundary)
        self.assertEqual((caught.exception.status, caught.exception.code), (400, "INVALID_INPUT"))

    def test_missing_closing_boundary(self):
        self.reject(self.body[:-(len(self.boundary) + 6)])

    def test_duplicate_audio_is_rejected_instead_of_overwritten(self):
        part = b'--' + self.boundary + b'\r\nContent-Disposition: form-data; name="audio"\r\n\r\nother\r\n'
        self.reject(part + self.body)

    def test_duplicate_name_parameter_is_rejected(self):
        self.reject(self.body.replace(b'name="audio"', b'name="audio"; name="metadata"'))

    def test_unknown_fields_are_rejected(self):
        self.reject(self.body.replace(b'name="audio"', b'name="other"'))

    def test_boundary_like_bytes_inside_audio_do_not_change_payload(self):
        payload = b"prefix--abc123 middle\r\n--abc123-extra\r\n\xff\x00"
        body = (b'--abc123\r\nContent-Disposition: form-data; name="audio"\r\n\r\n' + payload
                + b'\r\n--abc123\r\nContent-Disposition: form-data; name="metadata"\r\n\r\n{}\r\n--abc123--\r\n')
        self.assertEqual(parse_multipart(body, b"abc123")["audio"], payload)

    def test_boundary_longer_than_70_characters(self):
        self.reject(self.body, b"a" * 71)

    def test_transfer_encoding_not_silently_decoded(self):
        self.reject(self.body.replace(b'name="audio"\r\n', b'name="audio"\r\nContent-Transfer-Encoding: base64\r\n'))


class D3JsonTests(unittest.TestCase):
    def test_duplicate_json_keys(self):
        with self.assertRaises(InspectError) as raised:
            decode_metadata(b'{"request_id":"one","request_id":"two"}')
        self.assertEqual(raised.exception.status, 400)

    def test_nan_and_infinity_rejected(self):
        for value in (b"NaN", b"Infinity", b"-Infinity", b"1e1000"):
            with self.subTest(value=value), self.assertRaises(InspectError):
                decode_metadata(b'{"x":' + value + b'}')

    def test_deep_nesting_does_not_escape_as_503(self):
        with self.assertRaises(InspectError) as raised:
            decode_metadata(b"[" * 2000 + b"0" + b"]" * 2000)
        self.assertEqual(raised.exception.status, 400)

    def test_metadata_size_limit(self):
        with self.assertRaises(InspectError) as raised:
            decode_metadata(b" " * (MAX_METADATA_BYTES + 1))
        self.assertEqual((raised.exception.status, raised.exception.field), (413, "metadata"))

    def test_unpaired_unicode_surrogate_is_invalid_input(self):
        with self.assertRaises(InspectError) as raised:
            decode_metadata(br'{"request_id":"\ud800"}')
        self.assertEqual(raised.exception.status, 400)

    def test_blank_request_revision_and_phrase_id(self):
        for key in ("request_id", "revision_id", "id"):
            data = meta()
            (data["phrases"][0] if key == "id" else data)[key] = "  "
            with self.subTest(key=key), self.assertRaises(InspectError) as raised:
                inspect(wav_bytes(), data)
            self.assertEqual(raised.exception.status, 400)


class D3CorrelationTests(unittest.TestCase):
    def setUp(self):
        self.metadata = meta()
        self.result = inspect(wav_bytes(), self.metadata)

    def test_matching_result_passes(self):
        self.assertIsNone(validate_success(self.result, self.metadata))

    def test_request_revision_version_and_count_mismatches_rejected(self):
        for field in ("request_id", "revision_id", "processor_version", "phrase_count"):
            result = dict(self.result, **{field: "wrong"})
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_success(result, self.metadata)

    def test_boolean_measurement_is_not_integer(self):
        result = dict(self.result, duration_ms=True)
        with self.assertRaises(ValueError):
            validate_success(result, self.metadata)


class D3HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server("127.0.0.1", 0, TOKEN, read_timeout=0.3)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_port
        cls.url = f"http://127.0.0.1:{cls.port}"
        cls.audio = wav_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def send(self, metadata=None, raw=None):
        data = meta() if metadata is None else metadata
        return send_inspect(self.url, TOKEN, self.audio,
                            json.dumps(data).encode() if raw is None else raw, timeout=3)

    def raw(self, headers, body=b"", close_write=False):
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as connection:
            request = b"POST " + INSPECT_PATH.encode() + b" HTTP/1.1\r\nHost: localhost\r\n"
            connection.sendall(request + headers + b"\r\n" + body)
            if close_write:
                connection.shutdown(socket.SHUT_WR)
            response = http.client.HTTPResponse(connection)
            response.begin()
            return response.status, json.loads(response.read())

    def test_authenticated_stalled_body_times_out_and_service_recovers(self):
        started = time.monotonic()
        status, body = self.raw(b"Authorization: Bearer " + TOKEN.encode() +
                                b"\r\nContent-Length: 100\r\nContent-Type: multipart/form-data; boundary=x\r\n")
        self.assertEqual((status, body["code"]), (400, "INVALID_INPUT"))
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual(self.send()[0], 200)

    def test_unauthorized_does_not_wait_for_declared_body(self):
        started = time.monotonic()
        status, body = self.raw(b"Authorization: Bearer wrong\r\nContent-Length: 10000\r\n")
        self.assertEqual((status, body["code"]), (401, "INVALID_TOKEN"))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_non_ascii_token_returns_401(self):
        status, body = self.raw(b"Authorization: Bearer \xff\r\nContent-Length: 0\r\n")
        self.assertEqual((status, body["code"]), (401, "INVALID_TOKEN"))

    def test_duplicate_content_length(self):
        status, _ = self.raw(b"Authorization: Bearer " + TOKEN.encode()
                             + b"\r\nContent-Length: 0\r\nContent-Length: 0\r\n")
        self.assertEqual(status, 400)

    def test_transfer_encoding_rejected(self):
        status, _ = self.raw(b"Authorization: Bearer " + TOKEN.encode()
                             + b"\r\nContent-Length: 0\r\nTransfer-Encoding: chunked\r\n")
        self.assertEqual(status, 400)

    def test_truncated_transport_body_is_400(self):
        status, _ = self.raw(b"Authorization: Bearer " + TOKEN.encode()
                             + b"\r\nContent-Length: 100\r\nContent-Type: multipart/form-data; boundary=x\r\n",
                             b"--x", close_write=True)
        self.assertEqual(status, 400)

    def test_malformed_multipart_rejected(self):
        body, boundary = build_multipart({"audio": self.audio, "metadata": json.dumps(meta()).encode()})
        body = body[:-(len(boundary) + 6)]
        status, result = self.raw(b"Authorization: Bearer " + TOKEN.encode()
                                  + f"\r\nContent-Length: {len(body)}\r\nContent-Type: multipart/form-data; boundary={boundary}\r\n".encode(), body)
        self.assertEqual((status, result["code"]), (400, "INVALID_INPUT"))

    def test_quoted_case_insensitive_boundary_parameter(self):
        body, boundary = build_multipart({"audio": self.audio, "metadata": json.dumps(meta()).encode()})
        status, _ = self.raw(b"Authorization: Bearer " + TOKEN.encode()
                             + f'\r\nContent-Length: {len(body)}\r\nContent-Type: Multipart/Form-Data; Boundary="{boundary}"\r\n'.encode(), body)
        self.assertEqual(status, 200)

    def test_metadata_limit_returns_413(self):
        status, result = self.send(raw=b" " * (MAX_METADATA_BYTES + 1))
        self.assertEqual((status, result["code"], result["field"]), (413, "TOO_LARGE", "metadata"))

    def test_duplicate_json_keys_return_400(self):
        status, result = self.send(raw=b'{"request_id":"x","request_id":"y"}')
        self.assertEqual((status, result["code"]), (400, "INVALID_INPUT"))

    def test_internal_fault_is_503_and_next_request_can_retry(self):
        with mock.patch("ai_backend.server.inspect", side_effect=RuntimeError("test fault")), \
                mock.patch("ai_backend.server.traceback.print_exc"):
            status, body = self.send()
        self.assertEqual((status, body["request_id"], body["code"]), (503, "q7", "ANALYSIS_UNAVAILABLE"))
        self.assertEqual(self.send()[0], 200)

    def test_new_revision_does_not_return_cached_old_revision(self):
        first = meta()
        second = meta(revision_id="r2")
        self.assertEqual(self.send(first)[1]["revision_id"], "r1")
        self.assertEqual(self.send(second)[1]["revision_id"], "r2")

    def test_no_store_header(self):
        body, boundary = build_multipart({"audio": self.audio, "metadata": json.dumps(meta()).encode()})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request("POST", INSPECT_PATH, body, {"Authorization": "Bearer " + TOKEN,
                               "Content-Type": "multipart/form-data; boundary=" + boundary})
            response = connection.getresponse()
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
            self.assertEqual(response.getheader("Connection"), "close")
            response.read()
        finally:
            connection.close()


class D3ConfigTests(unittest.TestCase):
    def test_bad_token_config_rejected_without_opening_socket(self):
        for token in ("", "中文", "has space", "new\nline", "control\x00"):
            with self.subTest(token=token), self.assertRaises(ValueError):
                make_server("127.0.0.1", 0, token)

    def test_non_numeric_environment_port_gives_usage_error(self):
        with mock.patch.dict(os.environ, {"AI_BACKEND_PORT": "invalid"}), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            service_main([])
        self.assertEqual(raised.exception.code, 2)

    def test_cli_port_overrides_invalid_environment_default(self):
        with mock.patch.dict(os.environ, {"AI_BACKEND_PORT": "invalid"}), \
                mock.patch("ai_backend.__main__.make_server", side_effect=ValueError("sentinel")) as factory, \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            service_main(["--port", "8300", "--token", TOKEN])
        self.assertEqual(factory.call_args.args[1], 8300)

    def test_invalid_cli_port_gives_usage_error(self):
        for port in ("0", "65536", "-1"):
            with self.subTest(port=port), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                service_main(["--port", port, "--token", TOKEN])
            self.assertEqual(raised.exception.code, 2)

    def test_bad_read_timeout_rejected(self):
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                make_server("127.0.0.1", 0, TOKEN, read_timeout=timeout)

    def test_d3_report_cannot_overwrite_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            metadata = Path(folder) / "metadata.json"
            original = json.dumps(meta())
            metadata.write_text(original, encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                d3_main(["--output", str(metadata)])
            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(metadata.read_text(encoding="utf-8"), original)

    def test_d3_subprocess_runner_stops_and_restarts_without_leftovers(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "d3.json"
            with contextlib.redirect_stdout(io.StringIO()):
                code = d3_main(["--output", str(output)])
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(code, 0, report)
        self.assertTrue(report["all_passed"])
        self.assertEqual(report["case_count"], 17)
