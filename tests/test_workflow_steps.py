"""Step timing, Vendor Intake form completion, and workflow statistics."""
import json
import os
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = tempfile.mkdtemp(prefix="vo-steps-")
os.environ["VO_STORE_DIR"] = ROOT
os.environ["VO_SECRET"] = "test-secret"


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


PORT = _free_port()
os.environ["VO_STATUS_WEBHOOK_URL"] = f"http://127.0.0.1:{PORT}/hook"
os.environ["VO_STATUS_WEBHOOK_KEY"] = "test-key"
Path(ROOT, "requests.json").write_text(json.dumps({"requests": [], "auditLog": []}) + "\n", encoding="utf-8")

import app as vo  # noqa: E402


class _Hook(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.server.hits.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "body": json.loads(body.decode("utf-8")),
        })
        self.send_response(204)
        self.end_headers()

    def log_message(self, fmt, *args):
        return


class StepTimingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", PORT), _Hook)
        cls.server.hits = []
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_store_is_isolated(self):
        self.assertEqual(vo.STORE_DIR, ROOT)
        self.assertNotIn("store-bbb722d7", vo.STORE_DIR)

    def test_historical_durations_and_cancelled_security(self):
        """Two real shapes: a clean approval, and a security path that was cancelled."""
        clean_audit = [
            {"timestamp": "2026-11-03 17:26:20 UTC", "requestId": "VO-A", "actor": "LOB Requestor (web)",
             "action": "Created request", "toStatus": "With Vendor Intake", "details": "Vendor: A"},
            {"timestamp": "2026-11-03 17:27:55 UTC", "requestId": "VO-A", "actor": "Vendor Intake (web)",
             "action": "Intake complete — no customer data", "toStatus": "With Vendor Research",
             "details": "touches_customer_data=no; routed to Vendor Research"},
            {"timestamp": "2026-11-03 17:29:11 UTC", "requestId": "VO-A", "actor": "Vendor Research (web)",
             "action": "Research complete → Ops Review", "toStatus": "Awaiting Ops Review",
             "details": "Research complete"},
            {"timestamp": "2026-11-03 17:29:59 UTC", "requestId": "VO-A", "actor": "Human - Ops Review (web)",
             "action": "Ops Review approved → Manager", "toStatus": "Awaiting Manager Approval",
             "details": "Ops Review approved"},
            {"timestamp": "2026-11-03 17:30:55 UTC", "requestId": "VO-A", "actor": "Human - Manager (web)",
             "action": "Manager final APPROVE", "toStatus": "Approved", "details": "Final approval"},
        ]
        messy_audit = [
            {"timestamp": "2026-11-03 08:25:08 UTC", "requestId": "VO-B", "actor": "Vendor Onboarding Orchestrator",
             "action": "Created request", "toStatus": "With Vendor Intake", "details": "Vendor: B"},
            {"timestamp": "2026-11-03 08:25:51 UTC", "requestId": "VO-B", "actor": "Vendor Intake",
             "action": "Intake complete; parallel route", "toStatus": "With Vendor Research + Awaiting Security",
             "details": "touches_customer_data=yes; routed to Vendor Research and Human - Security"},
            {"timestamp": "2026-11-03 08:26:40 UTC", "requestId": "VO-B", "actor": "Vendor Research",
             "action": "Research complete", "toStatus": "Research done · Awaiting Security",
             "details": "LEI checked"},
            {"timestamp": "2026-11-03 08:46:54 UTC", "requestId": "VO-B", "actor": "Vendor Intake",
             "action": "Intake refreshed from LOB", "toStatus": "With Vendor Research",
             "details": "touches_customer_data=no; Security path cancelled; routed to Vendor Research"},
            {"timestamp": "2026-11-03 08:48:09 UTC", "requestId": "VO-B", "actor": "Vendor Research",
             "action": "Research reconfirmed", "toStatus": "Awaiting Ops Review",
             "details": "Routed to Human - Ops Review"},
        ]
        clean = {"id": "VO-A", "touchesCustomerData": False}
        messy = {"id": "VO-B", "touchesCustomerData": False}
        clean_steps = vo.derive_steps_from_audit(clean_audit)
        messy_steps = vo.derive_steps_from_audit(messy_audit)
        self.assertEqual(vo.duration_seconds(clean_steps["intake"][0]), 95)
        self.assertEqual(vo.duration_seconds(clean_steps["research"][0]), 76)
        self.assertEqual(vo.duration_seconds(clean_steps["ops"][0]), 48)
        self.assertEqual(vo.duration_seconds(clean_steps["manager"][0]), 56)
        self.assertEqual(vo.duration_seconds(clean_steps["lob"][0]), 0)
        self.assertNotIn("security", clean_steps)
        self.assertEqual(len(messy_steps["research"]), 2)
        self.assertEqual(vo.duration_seconds(messy_steps["research"][0]), 49)
        self.assertEqual(vo.duration_seconds(messy_steps["research"][1]), 75)
        self.assertNotIn("security", messy_steps)
        self.assertEqual(vo.duration_seconds(messy_steps["intake"][0]), 43)
        ops = messy_steps["ops"][0]
        self.assertTrue(ops.get("startedAt"))
        self.assertFalse(ops.get("endedAt"))

        stats = {row["label"]: row for row in vo.step_stats(
            [clean, messy], clean_audit + messy_audit)}
        self.assertEqual(stats["Vendor Intake form"]["count"], 2)
        self.assertEqual(stats["Vendor Intake form"]["min"], "43s")
        self.assertEqual(stats["Vendor Intake form"]["max"], "1m 35s")
        self.assertEqual(stats["Vendor Intake form"]["mean"], "1m 9s")
        self.assertEqual(stats["Vendor Research"]["count"], 3)
        self.assertEqual(stats["Security Approval"]["count"], 0)
        self.assertEqual(stats["Security Approval"]["mean"], "—")
        self.assertEqual(stats["Ops Review"]["count"], 1)
        self.assertEqual(stats["Manager Approval"]["count"], 1)
        self.assertEqual(clean_steps["intake"][0]["requestor"], "Vendor Intake")
        self.assertEqual(clean_steps["intake"][0]["requested_via"], "web")

    def test_intake_form_completes_step_and_calls_webhook(self):
        client = vo.app.test_client()
        before = len(self.server.hits)
        created = client.post("/new", data={
            "vendorName": "EXAMPLE DEVTOOLS PVT LTD",
            "vendorContact": "https://example.test",
            "requestor": "Jayesh V",
            "requested_via": "web",
            "purpose": "Environment check",
        })
        self.assertEqual(created.status_code, 302)
        rid = created.headers["Location"].rstrip("/").split("/")[-1]
        self.assertTrue(rid.startswith("VO-"))

        detail = client.get(f"/request/{rid}")
        self.assertEqual(detail.status_code, 200)
        page = detail.get_data(as_text=True)
        self.assertIn("Vendor Intake form", page)
        self.assertIn("Submit Vendor Intake form", page)
        self.assertIn("n-active", page)
        record = vo.find(vo.load(), rid)
        self.assertEqual(vo.flow_states(record, [])["states"]["intake"], "active")
        self.assertEqual(record["steps"]["lob"][0]["requestor"], "Jayesh V")
        self.assertEqual(record["steps"]["lob"][0]["requested_via"], "web")
        self.assertTrue(record["steps"]["lob"][0].get("endedAt"))
        self.assertFalse(record["steps"]["intake"][0].get("endedAt"))

        missing = client.post(f"/request/{rid}/intake", data={
            "vendor_legal_name": "EXAMPLE DEVTOOLS PVT LTD",
            "touches_customer_data": "",
            "requestor": "Asha",
            "requested_via": "web",
        })
        self.assertEqual(missing.status_code, 400)
        self.assertTrue(vo.intake_is_open(vo.find(vo.load(), rid)))

        done = client.post(f"/request/{rid}/intake", data={
            "vendor_legal_name": "EXAMPLE DEVTOOLS PVT LTD",
            "vendor_country": "India",
            "website": "https://example.test",
            "touches_customer_data": "no",
            "business_owner": "Env Setup",
            "purpose": "Cloud agent hello-world",
            "requester": "Jayesh V",
            "requestor": "Asha",
            "requested_via": "slack",
            "notes": "No customer data",
        }, follow_redirects=True)
        self.assertEqual(done.status_code, 200)
        body = done.get_data(as_text=True)
        self.assertIn("Vendor Intake step is completed", body)
        self.assertIn("Webhook bot notified.", body)
        self.assertIn("Vendor Intake form submitted", body)
        self.assertNotIn("Submit Vendor Intake form", body)
        self.assertIn("Asha", body)
        self.assertIn("slack", body)

        record = vo.find(vo.load(), rid)
        self.assertFalse(vo.intake_is_open(record))
        flow = vo.flow_states(record, vo.merged_audit(rid))
        self.assertEqual(flow["states"]["intake"], "done")
        self.assertEqual(flow["states"]["research"], "active")
        self.assertEqual(flow["states"]["security"], "na")
        self.assertIs(record["touchesCustomerData"], False)
        intake_run = record["steps"]["intake"][0]
        self.assertEqual(intake_run["requestor"], "Asha")
        self.assertEqual(intake_run["requested_via"], "slack")
        self.assertTrue(intake_run.get("startedAt"))
        self.assertTrue(intake_run.get("endedAt"))
        self.assertFalse(record["steps"]["research"][0].get("endedAt"))
        self.assertNotIn("security", record["steps"])

        hits = [hit for hit in self.server.hits[before:] if hit["body"].get("event") == "intake_completed"]
        self.assertEqual(len(hits), 1)
        hook = hits[0]
        self.assertEqual(hook["auth"], "Bearer test-key")
        self.assertEqual(hook["body"]["requestId"], rid)
        self.assertEqual(hook["body"]["requestor"], "Asha")
        self.assertEqual(hook["body"]["requested_via"], "slack")
        self.assertEqual(hook["body"]["step"], "intake")
        self.assertIs(hook["body"]["touchesCustomerData"], False)

        again = client.post(f"/request/{rid}/intake", data={
            "vendor_legal_name": "EXAMPLE DEVTOOLS PVT LTD",
            "touches_customer_data": "yes",
            "requestor": "Asha",
            "requested_via": "web",
        }, follow_redirects=True)
        self.assertIn("already submitted", again.get_data(as_text=True))
        self.assertIs(vo.find(vo.load(), rid)["touchesCustomerData"], False)
        intake_hits = [hit for hit in self.server.hits if hit["body"].get("event") == "intake_completed"]
        self.assertEqual(len(intake_hits), 1)

        research = client.post(f"/request/{rid}/action", data={
            "transition": "research_done",
            "requestor": "Riya",
            "requested_via": "web",
        }, follow_redirects=True)
        self.assertEqual(research.status_code, 200)
        record = vo.find(vo.load(), rid)
        research_run = record["steps"]["research"][0]
        self.assertEqual(research_run["requestor"], "Riya")
        self.assertEqual(research_run["requested_via"], "web")
        self.assertTrue(research_run.get("endedAt"))
        self.assertFalse(record["steps"]["ops"][0].get("endedAt"))
        status_hits = [
            hit for hit in self.server.hits
            if hit["body"].get("requestId") == rid and hit["body"].get("event") == "status_changed"
        ]
        self.assertEqual(len(status_hits), 1)
        self.assertEqual(status_hits[0]["body"]["step"], "research")

        blocked = client.post(f"/request/{rid}/action", data={"transition": "intake_no_cd"})
        self.assertEqual(blocked.status_code, 302)

        page = client.get("/workflow")
        html = page.get_data(as_text=True)
        self.assertIn("Step statistics", html)
        self.assertIn("Vendor Intake form", html)
        self.assertIn(">Executions<", html)
        self.assertIn(">Mean<", html)
        self.assertIn(">Min<", html)
        self.assertIn(">Max<", html)
        stats = {row["label"]: row for row in vo.step_stats(vo.load()["requests"], vo.merged_audit())}
        self.assertGreaterEqual(stats["Vendor Intake form"]["count"], 1)
        self.assertGreaterEqual(stats["LOB Requestor"]["count"], 1)
        self.assertNotEqual(stats["Vendor Intake form"]["mean"], "—")


if __name__ == "__main__":
    unittest.main()
