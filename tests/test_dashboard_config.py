"""Dashboard status cards and the Configuration page."""
import json
import os
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = tempfile.mkdtemp(prefix="vo-config-")
os.environ["VO_STORE_DIR"] = ROOT
os.environ["VO_SECRET"] = "test-secret"


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


PORT = _free_port()
os.environ["VO_STATUS_WEBHOOK_URL"] = f"http://127.0.0.1:{PORT}/env"
os.environ["VO_STATUS_WEBHOOK_KEY"] = "env-secret-key"
Path(ROOT, "requests.json").write_text(json.dumps({
    "requests": [
        {"id": "VO-1", "vendorName": "Intake Co", "requestedBy": "A", "currentActor": "Vendor Intake",
         "status": "With Vendor Intake", "lastUpdated": "2026-11-04 01:00:00 UTC", "lei": "",
         "entityStatus": "", "registrationStatus": ""},
        {"id": "VO-2", "vendorName": "Security Co", "requestedBy": "B", "currentActor": "Human - Security",
         "status": "With Vendor Research + Awaiting Security", "lastUpdated": "2026-11-04 02:00:00 UTC",
         "lei": "", "entityStatus": "", "registrationStatus": ""},
        {"id": "VO-3", "vendorName": "Ops Co", "requestedBy": "C", "currentActor": "Human - Ops Review",
         "status": "Awaiting Ops Review", "lastUpdated": "2026-11-04 03:00:00 UTC", "lei": "",
         "entityStatus": "", "registrationStatus": ""},
        {"id": "VO-4", "vendorName": "Manager Co", "requestedBy": "D", "currentActor": "Human - Manager",
         "status": "Awaiting Manager Approval", "lastUpdated": "2026-11-04 04:00:00 UTC", "lei": "",
         "entityStatus": "", "registrationStatus": ""},
        {"id": "VO-5", "vendorName": "Done Co", "requestedBy": "E", "currentActor": "—",
         "status": "Approved", "lastUpdated": "2026-11-04 05:00:00 UTC", "lei": "",
         "entityStatus": "", "registrationStatus": ""},
    ],
    "auditLog": [],
}) + "\n", encoding="utf-8")

import app as vo  # noqa: E402


class _Hook(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.server.hits.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "body": self.rfile.read(length).decode(),
        })
        self.send_response(204)
        self.end_headers()

    def log_message(self, fmt, *args):
        return


class DashboardConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", PORT), _Hook)
        cls.server.hits = []
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.client = vo.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_dashboard_replaces_vendor_intake_and_adds_manager_decision(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertNotIn("With Vendor Intake", html)
        self.assertIn(">Awaiting Security Approval<", html)
        self.assertIn(">Awaiting Manager Decision<", html)
        self.assertIn('href="?status=awaiting-security-approval"><b>2</b>', html)
        self.assertIn('href="?status=awaiting-manager-decision"><b>1</b>', html)
        self.assertIn('href="?status=awaiting-ops-review"><b>1</b>', html)
        self.assertIn('href="?status=approved"><b>1</b>', html)
        self.assertIn('href="?status=with-vendor-research"><b>1</b>', html)

        research = self.client.get("/?status=with-vendor-research")
        body = research.get_data(as_text=True)
        self.assertIn("Security Co", body)
        self.assertNotIn("Intake Co", body)
        self.assertNotIn("Ops Co", body)

        security = self.client.get("/?status=awaiting-security-approval")
        body = security.get_data(as_text=True)
        self.assertIn("Intake Co", body)
        self.assertIn("Security Co", body)
        self.assertNotIn("Manager Co", body)
        self.assertNotIn("Done Co", body)

        manager = self.client.get("/?status=awaiting-manager-decision")
        body = manager.get_data(as_text=True)
        self.assertIn("Manager Co", body)
        self.assertNotIn("Intake Co", body)
        self.assertIn("Awaiting Manager Decision", body)
        self.assertNotIn("Awaiting Manager Approval", body)

    def test_open_pages_reload_when_the_tracker_changes(self):
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn("/static/live.js", home.get_data(as_text=True))
        detail = self.client.get("/request/VO-1")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("Workflow progress", detail.get_data(as_text=True))
        self.assertIn("/static/live.js", detail.get_data(as_text=True))
        self.assertNotIn("/static/live.js", self.client.get("/new").get_data(as_text=True))

        first = self.client.get("/live.json")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.headers["Cache-Control"], "no-store")
        token = first.get_json()["revision"]
        self.assertTrue(token)
        again = self.client.get("/live.json").get_json()["revision"]
        self.assertEqual(again, token)

        with vo.store_lock():
            data = vo.load()
            data["requests"][0]["status"] = "Awaiting Ops Review"
            data["requests"][0]["lastUpdated"] = "2026-11-05 06:00:00 UTC"
            vo.save(data, [])
        changed = self.client.get("/live.json").get_json()["revision"]
        self.assertNotEqual(changed, token)
        self.assertIn("Awaiting Ops Review", self.client.get("/request/VO-1").get_data(as_text=True))

    def test_configuration_lists_adds_and_removes_webhooks(self):
        page = self.client.get("/configuration")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("Registered webhooks", html)
        self.assertIn(f"http://127.0.0.1:{PORT}/env", html)
        self.assertNotIn("env-secret-key", html)
        self.assertIn("••••••••", html)
        self.assertLess(html.index("Workflow"), html.index("Configuration"))
        self.assertIn("vendor-master", html)
        self.assertIn("http://127.0.0.1:8788/mcp", html)
        self.assertIn("streamable HTTP", html)
        self.assertNotIn("stdio", html)
        self.assertIn("2026-str-mcp", html)
        self.assertIn("Authorization", html)
        self.assertIn("requestor", html)
        self.assertIn("requested_via", html)
        self.assertIn("submit_onboarding_request", html)
        self.assertIn("Connected MCP clients", html)
        self.assertIn("No MCP clients are connected.", html)
        self.assertIn("notifications/message", html)

        bad = self.client.post("/configuration", data={"name": "Nope", "url": "ftp://files.example", "key": "k"})
        self.assertEqual(bad.status_code, 400)
        self.assertIn("http:// or https://", bad.get_data(as_text=True))

        added = self.client.post("/configuration", data={
            "name": "Ops bot",
            "url": f"http://127.0.0.1:{PORT}/saved",
            "key": "saved-secret-key",
        }, follow_redirects=True)
        self.assertEqual(added.status_code, 200)
        body = added.get_data(as_text=True)
        self.assertIn("Ops bot", body)
        self.assertIn(f"http://127.0.0.1:{PORT}/saved", body)
        self.assertNotIn("saved-secret-key", body)
        self.assertIn("saved", body)

        dup = self.client.post("/configuration", data={
            "name": "Again",
            "url": f"http://127.0.0.1:{PORT}/saved",
            "key": "other",
        })
        self.assertEqual(dup.status_code, 400)

        before = len(self.server.hits)
        result = vo.push_status_change({"event": "status_changed", "requestId": "VO-1"})
        self.assertEqual(result, "sent")
        hits = self.server.hits[before:]
        self.assertEqual(sorted(hit["path"] for hit in hits), ["/env", "/saved"])
        self.assertEqual({hit["auth"] for hit in hits}, {"Bearer env-secret-key", "Bearer saved-secret-key"})

        saved = next(row for row in vo.registered_webhooks() if row["source"] == "saved")
        removed = self.client.post(
            f"/configuration/webhooks/{saved['id']}/delete",
            follow_redirects=True,
        )
        self.assertEqual(removed.status_code, 200)
        self.assertNotIn("Ops bot", removed.get_data(as_text=True))
        self.assertIn(f"http://127.0.0.1:{PORT}/env", removed.get_data(as_text=True))

    def test_missing_tracker_file_is_created(self):
        os.rename(vo.JSON_PATH, vo.JSON_PATH + ".held")
        try:
            data = vo.load()
            self.assertEqual(data["requests"], [])
            self.assertEqual(data["auditLog"], [])
        finally:
            os.replace(vo.JSON_PATH + ".held", vo.JSON_PATH)

    def test_webhook_test_button_delivers_without_registering(self):
        page = self.client.get("/configuration")
        form = page.get_data(as_text=True).split("<h2>Add webhook</h2>", 1)[1]
        self.assertLess(form.index("Test webhook"), form.index(">Add webhook<"))

        before = len(self.server.hits)
        saved_before = [row["url"] for row in vo._read_saved_webhooks()]
        ok = self.client.post("/configuration", data={
            "action": "test",
            "name": "Probe",
            "url": f"http://127.0.0.1:{PORT}/probe",
            "key": "probe-secret",
        })
        self.assertEqual(ok.status_code, 200)
        body = ok.get_data(as_text=True)
        self.assertIn("Test webhook accepted", body)
        self.assertIn("was not registered", body)
        self.assertNotIn(">Probe<", body)
        self.assertEqual([row["url"] for row in vo._read_saved_webhooks()], saved_before)
        hit = self.server.hits[before]
        self.assertEqual(hit["path"], "/probe")
        self.assertEqual(hit["auth"], "Bearer probe-secret")
        self.assertEqual(json.loads(hit["body"])["event"], "webhook_test")

        failed = self.client.post("/configuration", data={
            "action": "test",
            "name": "Down",
            "url": "http://127.0.0.1:9/missing",
            "key": "probe-secret",
        })
        self.assertEqual(failed.status_code, 400)
        self.assertIn("Test webhook failed", failed.get_data(as_text=True))
        self.assertEqual([row["url"] for row in vo._read_saved_webhooks()], saved_before)

    def test_configuration_shows_vo_mcp_token(self):
        os.environ["VO_MCP_TOKEN"] = "from-env-token"
        try:
            html = self.client.get("/configuration").get_data(as_text=True)
            self.assertIn("from-env-token", html)
            config = json.loads(vo.mcp_client_config())
            header = config["mcpServers"]["vendor-master"]["headers"]["Authorization"]
            self.assertEqual(header, "Bearer from-env-token")
        finally:
            os.environ.pop("VO_MCP_TOKEN", None)
        config = json.loads(vo.mcp_client_config())
        header = config["mcpServers"]["vendor-master"]["headers"]["Authorization"]
        self.assertEqual(header, "Bearer 2026-str-mcp")


if __name__ == "__main__":
    unittest.main()
