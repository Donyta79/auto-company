"""Local control boundaries using synthetic in-memory HTTP requests only."""

from email.message import Message
import importlib.util
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "dashboard_guard_server", Path(__file__).resolve().parents[1] / "dashboard/server.py")
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


class DashboardApiGuardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patch = mock.patch.object(server, "REPO_ROOT", self.root)
        patch.start()
        self.addCleanup(patch.stop)
        action_patch = mock.patch.object(server, "run_dashboard_action", return_value={
            "ok": True, "exitCode": 0, "elapsedMs": 1, "output": "fixture"})
        self.action = action_patch.start()
        self.addCleanup(action_patch.stop)

    def request(self, path="/api/action/start", body=b"{}", headers=(), include_length=True):
        handler = object.__new__(server.DashboardHandler)
        handler.server = SimpleNamespace(server_address=("127.0.0.1", 8810))
        handler.path = path
        handler.headers = Message()
        base = [("Host", "127.0.0.1:8810"), ("Content-Type", "application/json")]
        if include_length:
            base.append(("Content-Length", str(len(body))))
        for key, value in (*base, *headers):
            handler.headers[key] = value
        handler.rfile = io.BytesIO(body)
        handler._json = mock.Mock()
        handler._text = mock.Mock()
        handler.do_POST()
        return handler._json.call_args

    def test_malformed_bodies_never_run_actions(self):
        for body in (b"{", b"[]", b"null", b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":1e999}', b" " * 1025):
            with self.subTest(body=body[:40]):
                self.assertEqual(self.request(body=body).kwargs["code"], 400)
        self.action.assert_not_called()

    def test_ambiguous_headers_never_run_actions(self):
        for headers in ((("Host", "evil.invalid"),),
                        (("Origin", "http://127.0.0.1:8810"), ("Origin", "https://evil.invalid")),
                        (("Content-Length", "2"),), (("Transfer-Encoding", "chunked"),)):
            with self.subTest(headers=headers):
                self.assertIn(self.request(headers=headers).kwargs["code"], (400, 403))
        self.action.assert_not_called()

    def test_incomplete_or_invalid_length_never_runs_actions(self):
        for length in ("3", "-1", "invalid", "1025"):
            with self.subTest(length=length):
                result = self.request(headers=(("Content-Length", length),), include_length=False)
                self.assertEqual(result.kwargs["code"], 400)
        self.action.assert_not_called()

    def test_transfer_encoding_presence_never_runs_actions(self):
        for headers in ((("Transfer-Encoding", ""),),
                        (("Transfer-Encoding", ""), ("Transfer-Encoding", "chunked")),
                        (("Transfer-Encoding", "chunked"), ("Transfer-Encoding", ""))):
            for body, include_length in ((b"{}", True), (b"", True), (b"", False)):
                with self.subTest(headers=headers, body=body, include_length=include_length):
                    result = self.request(body=body, headers=headers, include_length=include_length)
                    self.assertEqual(result.kwargs.get("code", 200), 400)
        self.action.assert_not_called()

    def test_content_length_requires_ascii_decimal_digits(self):
        for length in ("", "+2", "0_2", "\u0662", "\uff12", "2.0", "2 0", "2,2", "2\n", "\u00a02"):
            with self.subTest(length=length):
                result = self.request(headers=(("Content-Length", length),), include_length=False)
                self.assertEqual(result.kwargs.get("code", 200), 400)
        self.action.assert_not_called()

    def test_valid_decimal_length_with_http_whitespace_still_runs(self):
        for length in ("2", "002", " \t2\t "):
            with self.subTest(length=length):
                result = self.request(headers=(("Content-Length", length),), include_length=False)
                self.assertEqual(result.kwargs.get("code", 200), 200)
        self.assertEqual(self.action.call_count, 3)

    def test_invalid_payload_cannot_change_language_or_capture(self):
        with mock.patch.object(server.localization, "set_language") as language, \
                mock.patch.object(server, "capture_product_media") as capture:
            for path in ("/api/language", "/api/product-media/capture"):
                for body in (b"", b"[]", b'{"language":"en","language":"zh-CN"}', b'{"productId":[]}'):
                    with self.subTest(path=path, body=body):
                        self.assertEqual(self.request(path, body).kwargs["code"], 400)
            language.assert_not_called()
            capture.assert_not_called()

    def test_managed_runtime_still_blocks_legacy_mutations(self):
        (self.root / ".autonomous-company-center.json").write_text("{}")
        self.assertEqual(self.request().kwargs["code"], 409)
        self.action.assert_not_called()
        self.assertEqual(self.request("/api/action/refresh").kwargs.get("code", 200), 200)
        self.action.assert_called_once_with("refresh")

    def test_nonrelative_and_query_write_urls_never_run_actions(self):
        for path in ("http://evil.invalid/api/action/start", "/api/action/start?extra=1", "/api/action/start#fragment"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path=path).kwargs["code"], 400)
        self.action.assert_not_called()

    def test_action_failure_does_not_expose_exception_details(self):
        self.action.side_effect = OSError("PRIVATE-SENTINEL /private/token")
        with self.assertLogs(level="ERROR") as logs:
            result = self.request()
        self.assertIn("Dashboard start action failed: OSError", logs.output[0])
        self.assertNotIn("PRIVATE-SENTINEL", str(logs.output))
        self.assertEqual(result.kwargs["code"], 504)
        self.assertNotIn("PRIVATE-SENTINEL", str(result))

    def test_empty_action_body_remains_supported(self):
        for include_length in (True, False):
            self.assertEqual(self.request(body=b"", include_length=include_length).kwargs.get("code", 200), 200)
        self.assertEqual(self.action.call_count, 2)

    def test_valid_language_and_capture_still_use_their_body(self):
        with mock.patch.object(server.localization, "set_language") as language, \
                mock.patch.object(server.localization, "language_state", return_value={"language": "en"}):
            self.assertEqual(self.request("/api/language", b'{"language":"en"}').kwargs.get("code", 200), 200)
            language.assert_called_once_with(self.root, "en")
        product_id = "a" * 32
        with mock.patch.object(server, "gather_status_payload", return_value={"ok": True, "parsed": {"loop": {"processState": "stopped"}}}), \
                mock.patch.object(server, "capture_product_media", return_value={"ok": True}) as capture:
            self.assertEqual(self.request("/api/product-media/capture", ('{"productId":"' + product_id + '"}').encode()).kwargs.get("code", 200), 200)
            capture.assert_called_once_with(product_id)
        self.action.assert_not_called()

    def test_valid_action_still_runs(self):
        self.assertEqual(self.request().kwargs.get("code", 200), 200)
        self.action.assert_called_once_with("start")

    def test_language_shape_errors_are_client_errors(self):
        for body in (b'{"language":[]}', b'{"language":{}}', b'{"language":true}'):
            with self.subTest(body=body):
                self.assertEqual(self.request('/api/language', body).kwargs['code'], 400)
        self.action.assert_not_called()


if __name__ == "__main__":
    unittest.main()
