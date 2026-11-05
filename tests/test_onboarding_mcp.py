"""MCP tool contract: one submit step, and requestor plus requested_via on every tool."""
import json
import os
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = tempfile.mkdtemp(prefix="vo-mcp-")
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

import mcp_server  # noqa: E402
import onboarding_api as api  # noqa: E402


class _Hook(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode())
        self.server.hits.append(body)
        self.send_response(204)
        self.end_headers()

    def log_message(self, fmt, *args):
        return


class OnboardingMcpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", PORT), _Hook)
        cls.server.hits = []
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_tool_list_and_required_actor_fields(self):
        tools = {tool.name: tool for tool in mcp_server.server._tool_manager.list_tools()}
        self.assertEqual(set(tools), {
            "submit_onboarding_request",
            "record_vendor_research",
            "record_ops_review",
            "record_security_approval",
            "record_manager_decision",
            "get_onboarding_request",
            "get_onboarding_status",
            "get_pending_requests",
        })
        pending = tools["get_pending_requests"].parameters
        self.assertEqual(
            set(pending.get("required") or []),
            {"elapsed", "unit", "requestor", "requested_via"},
        )
        self.assertNotIn("create_onboarding_request", tools)
        self.assertNotIn("submit_vendor_intake", tools)
        for name, tool in tools.items():
            required = set(tool.parameters.get("required") or [])
            self.assertIn("requestor", required, name)
            self.assertIn("requested_via", required, name)
            self.assertIn("requestor", tool.description.lower())
            self.assertIn("requested_via", tool.description)

    def test_submit_completes_intake_and_later_steps_require_actor(self):
        missing = api.submit_onboarding_request("ACME", True, "", "web")
        self.assertFalse(missing["ok"])
        self.assertIn("requestor", missing["error"])

        before = len(self.server.hits)
        created = api.submit_onboarding_request(
            vendor_legal_name="ACME PAYMENTS PVT LTD",
            touches_customer_data=True,
            requestor="Jayesh",
            requested_via="grok",
            purpose="Payments",
            business_owner="Ash",
        )
        self.assertTrue(created["ok"], created)
        rid = created["requestId"]
        self.assertEqual(created["status"], "With Vendor Research + Awaiting Security")
        self.assertEqual(created["requestor"], "Jayesh")
        self.assertEqual(created["requested_via"], "grok")
        self.assertEqual(created["webhook"], "sent")
        events = [hit["event"] for hit in self.server.hits[before:]]
        self.assertEqual(events, ["intake_completed"])
        self.assertNotIn("request_created", events)

        detail = api.get_onboarding_request(rid, "Jayesh", "grok")
        self.assertTrue(detail["intake"]["touches_customer_data"])
        self.assertEqual(detail["steps"]["intake"][0]["requestor"], "Jayesh")
        self.assertEqual(detail["steps"]["intake"][0]["requested_via"], "grok")
        self.assertTrue(detail["steps"]["intake"][0].get("endedAt"))
        self.assertFalse(detail["steps"]["research"][0].get("endedAt"))
        self.assertFalse(detail["steps"]["security"][0].get("endedAt"))

        no_actor = api.get_onboarding_status(rid, "Jayesh", "")
        self.assertFalse(no_actor["ok"])
        status = api.get_onboarding_status(rid, "Jayesh", "grok")
        self.assertEqual(status["status"], created["status"])
        self.assertEqual(status["currentActor"], "Vendor Research + Human - Security")

        research = api.record_vendor_research(
            rid, "335800NT9XZS4VC77O85", "ACTIVE", "ISSUED", "Riya", "grok",
            source="https://search.gleif.org", note="Exact name match")
        self.assertTrue(research["ok"], research)
        self.assertEqual(research["status"], "Awaiting Ops Review")
        self.assertEqual(research["lei"], "335800NT9XZS4VC77O85")

        ops = api.record_ops_review(rid, "Ops Person", "web")
        self.assertTrue(ops["ok"], ops)
        self.assertEqual(ops["status"], "Awaiting Manager Approval")
        self.assertEqual(ops["requestor"], "Ops Person")

        security = api.record_security_approval(rid, "Sec Person", "slack")
        self.assertTrue(security["ok"], security)
        self.assertEqual(security["currentActor"], "Human - Manager")

        decision = api.record_manager_decision(rid, "approve", "Manager", "email", note="Final yes")
        self.assertTrue(decision["ok"], decision)
        self.assertEqual(decision["status"], "Approved")
        final = api.get_onboarding_request(rid, "Jayesh", "grok")
        self.assertEqual(final["steps"]["manager"][0]["requested_via"], "email")
        self.assertEqual(final["lei"], "335800NT9XZS4VC77O85")
        self.assertTrue(final["researchComplete"])

    def test_security_is_rejected_when_customer_data_is_not_touched(self):
        created = api.submit_onboarding_request(
            "NORTHWIND LLC", False, "Mina", "web")
        self.assertTrue(created["ok"], created)
        self.assertEqual(created["status"], "With Vendor Research")
        blocked = api.record_security_approval(created["requestId"], "Mina", "web")
        self.assertFalse(blocked["ok"])
        self.assertIn("touches customer data", blocked["error"])
        bad = api.record_manager_decision(created["requestId"], "maybe", "Mina", "web")
        self.assertIn("approve or reject", bad["error"])

    def test_pending_requests_over_elapsed_time(self):
        missing = api.get_pending_requests(1, "hours", "", "grok")
        self.assertIn("requestor", missing["error"])
        self.assertFalse(api.get_pending_requests(0, "minutes", "Jayesh", "grok")["ok"])
        self.assertIn("minutes or hours", api.get_pending_requests(2, "days", "Jayesh", "grok")["error"])

        now = "2026-11-05 12:00:00 UTC"
        records = [
            _pending("VO-PEND-OPS", "Old Ops Co", "Awaiting Ops Review", "Human - Ops Review",
                     {"ops": "2026-11-05 08:00:00 UTC"}),
            _pending("VO-PEND-NEW", "New Ops Co", "Awaiting Ops Review", "Human - Ops Review",
                     {"ops": "2026-11-05 11:30:00 UTC"}),
            _pending("VO-PEND-SEC", "Security Co", "With Vendor Research + Awaiting Security",
                     "Vendor Research + Human - Security",
                     {"security": "2026-11-05 10:00:00 UTC", "research": "2026-11-05 10:00:00 UTC"}),
            _pending("VO-PEND-BOTH", "Both Gates Co", "Awaiting Ops Review", "Human - Ops Review",
                     {"security": "2026-11-05 09:00:00 UTC", "ops": "2026-11-05 10:30:00 UTC"}),
            _pending("VO-PEND-EXACT", "Exact Hour Co", "Awaiting Manager Approval", "Human - Manager",
                     {"manager": "2026-11-05 11:00:00 UTC"}),
            _pending("VO-PEND-MGR", "Manager Co", "Awaiting Manager Approval", "Human - Manager",
                     {"manager": "2026-11-05 10:59:00 UTC"}),
            _pending("VO-PEND-DONE", "Approved Co", "Approved", "—",
                     {"manager": "2026-11-05 08:00:00 UTC"}, ended={"manager": "2026-11-05 09:00:00 UTC"}),
            {
                "id": "VO-PEND-INTAKE", "vendorName": "Intake Co", "status": "With Vendor Intake",
                "currentActor": "Vendor Intake", "lastUpdated": "2026-11-01 00:00:00 UTC",
                "touchesCustomerData": True, "lei": "",
            },
            {
                "id": "VO-PEND-LEGACY", "vendorName": "Legacy Ops Co", "status": "Awaiting Ops Review",
                "currentActor": "Human - Ops Review", "lastUpdated": "2026-11-05 07:00:00 UTC",
                "touchesCustomerData": False, "lei": "LEGACYLEI",
            },
        ]
        with api.vo.store_lock():
            data = api.vo.load()
            data["requests"].extend(records)
            api.vo.save(data, [])

        found = api.get_pending_requests(1, "hours", "Jayesh", "grok", now=now)
        self.assertTrue(found["ok"], found)
        self.assertEqual(found["unit"], "hours")
        self.assertEqual(found["thresholdMinutes"], 60)
        by_id = {row["requestId"]: row for row in found["vendors"]}
        self.assertEqual(set(by_id), {
            "VO-PEND-OPS", "VO-PEND-SEC", "VO-PEND-BOTH", "VO-PEND-MGR", "VO-PEND-LEGACY",
        })
        self.assertEqual([item["stage"] for item in by_id["VO-PEND-BOTH"]["pending"]],
                         ["Security Approval", "Ops Review"])
        self.assertEqual(by_id["VO-PEND-SEC"]["pending"][0]["stage"], "Security Approval")
        self.assertEqual(by_id["VO-PEND-SEC"]["pending"][0]["elapsed"], "2h")
        self.assertEqual(by_id["VO-PEND-OPS"]["status"], "Awaiting Ops Review")
        self.assertEqual(by_id["VO-PEND-MGR"]["pending"][0]["stage"], "Manager Decision")
        self.assertEqual(by_id["VO-PEND-LEGACY"]["lei"], "LEGACYLEI")
        self.assertEqual(by_id["VO-PEND-LEGACY"]["pending"][0]["pendingSince"], "2026-11-05 07:00:00 UTC")
        self.assertGreater(by_id["VO-PEND-LEGACY"]["pending"][0]["elapsedMinutes"],
                           by_id["VO-PEND-MGR"]["pending"][0]["elapsedMinutes"])
        self.assertEqual(found["vendors"][0]["requestId"], "VO-PEND-LEGACY")

        tighter = api.get_pending_requests(90, "minutes", "Jayesh", "grok", now=now)
        tight_ids = {row["requestId"]: row for row in tighter["vendors"]}
        self.assertNotIn("VO-PEND-MGR", tight_ids)
        self.assertEqual([item["stage"] for item in tight_ids["VO-PEND-BOTH"]["pending"]],
                         ["Security Approval"])
        alias = api.get_pending_requests(1, "hour", "Jayesh", "grok", now=now)
        self.assertEqual(alias["count"], found["count"])


def _pending(rid, name, status, actor, started, ended=None):
    steps = {}
    for key, ts in started.items():
        run = {"startedAt": ts}
        if ended and key in ended:
            run["endedAt"] = ended[key]
        steps[key] = [run]
    return {
        "id": rid,
        "vendorName": name,
        "status": status,
        "currentActor": actor,
        "lastUpdated": "2026-11-05 12:00:00 UTC",
        "touchesCustomerData": "security" in started,
        "lei": "",
        "entityStatus": "",
        "registrationStatus": "",
        "vendorContact": "https://example.test",
        "requestedBy": "Jayesh (grok)",
        "steps": steps,
    }


if __name__ == "__main__":
    unittest.main()
