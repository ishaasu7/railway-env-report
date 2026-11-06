import html
import http.client
import importlib
import json
import os
import pathlib
import sys
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def blocked(*args, **kwargs):
    raise AssertionError("network must be patched before import")


with patch("urllib.request.urlopen", blocked):
    web_service = importlib.import_module("web_service")


class DashboardTests(unittest.TestCase):
    def test_bytes_1024_boundaries(self):
        self.assertEqual(web_service._bytes(1023), "1,023 B")
        for power, unit in ((1, "KB"), (2, "MB"), (3, "GB"), (4, "TB")):
            self.assertEqual(web_service._bytes(1024 ** power), "1.0 " + unit)

    def test_datetime_plus_eight(self):
        formatted = web_service._time("2026-01-01T00:00:00+00:00")
        self.assertIn("2026-01-01 08:00:00", formatted)
        self.assertIn("UTC+08:00", formatted)

    def test_html_escaping_and_branding(self):
        rendered = web_service.render_dashboard(
            {"report": {"generated_at_utc": "<x>"}, "hostname": "<script>"},
            "<refresh>", None, "<started>", 1,
        )
        self.assertNotIn("<script>", rendered)
        self.assertIn(html.escape("<script>"), rendered)
        self.assertIn("Railway 环境报告", rendered)
        self.assertIn("https://github.com/ishaasu7/railway-env-report", rendered)

    def test_port_resolution(self):
        cases = [
            ({}, 8080),
            ({"PORT": "12345", "SERVER_PORT": "23456"}, 12345),
            ({"PORT": " 9090 "}, 9090),
            ({"PORT": "bad", "SERVER_PORT": "23456"}, 23456),
            ({"PORT": "0", "SERVER_PORT": "65536"}, 8080),
        ]
        for environment, expected in cases:
            with self.subTest(environment=environment):
                with patch.dict(os.environ, environment, clear=True):
                    self.assertEqual(web_service.port_from_env(), expected)

    def test_schema_does_not_dump_environment(self):
        marker = "SYNTHETIC_TEST_SECRET_NOT_A_CREDENTIAL"
        with patch.dict(os.environ, {"PRIVATE_TEST_VALUE": marker}):
            with patch.object(web_service, "command", return_value={"ok": False}):
                report = web_service.collect_report({"ok": False, "ip": None})
        self.assertEqual(report["report"]["name"], "railway-environment")
        self.assertEqual(report["report"]["schema_version"], 1)
        self.assertIsNone(report["network"]["port_allocation"]["public_url"])
        self.assertIsNone(report["network"]["port_allocation"]["platform_allocated_port"])
        self.assertNotIn(marker, json.dumps(report))
        self.assertNotIn("PRIVATE_TEST_VALUE", json.dumps(report))

    def test_refresh_failure_keeps_last_report(self):
        with patch.object(web_service, "collect_egress", return_value={"ok": False}):
            with patch.object(web_service, "collect_report", return_value={"synthetic": True}):
                state = web_service.State()
        with patch.object(web_service, "collect_report", side_effect=RuntimeError("test")):
            state.refresh()
        report, _refreshed, error = state.snapshot()
        self.assertEqual(report, {"synthetic": True})
        self.assertEqual(error, "refresh_failed")


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        synthetic = {"report": {"name": "railway-environment", "schema_version": 1},
                     "hostname": "synthetic-host"}
        state = Mock()
        state.snapshot.return_value = (synthetic, "2026-01-01T00:00:00+00:00", None)
        cls.state_patch = patch.object(web_service, "STATE", state)
        cls.state_patch.start()
        cls.server = web_service.BoundedHTTPServer(("127.0.0.1", 0), web_service.Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        cls.state_patch.stop()

    def request(self, method, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_health(self):
        status, headers, body = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True, "service": "railway-env-report"})
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_json(self):
        status, headers, body = self.request("GET", "/report.json")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        self.assertEqual(json.loads(body)["hostname"], "synthetic-host")

    def test_dashboard(self):
        status, headers, body = self.request("GET", "/?test=1")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertIn("Railway 环境报告", body.decode("utf-8"))

    def test_head(self):
        status, headers, body = self.request("HEAD", "/report.json")
        self.assertEqual(status, 200)
        self.assertGreater(int(headers["Content-Length"]), 0)
        self.assertEqual(body, b"")

    def test_not_found_and_write_rejected(self):
        self.assertEqual(self.request("GET", "/missing")[0], 404)
        self.assertEqual(self.request("POST", "/")[0], 405)


if __name__ == "__main__":
    unittest.main()
