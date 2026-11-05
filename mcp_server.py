"""Vendor Master MCP server for the Grok bot.

Streamable HTTP (the Grok bot connects to this URL):

    .venv/bin/python mcp_server.py

Default endpoint: http://127.0.0.1:8788/mcp
Override with VO_MCP_HOST, VO_MCP_PORT, and VO_MCP_PATH.
Every request must send Authorization: Bearer <token>. The token is
VO_MCP_TOKEN when that variable is set, otherwise 2026-str-mcp.
The server reads and writes the same tracker as the web app (VO_STORE_DIR).
There is no create-onboarding-request tool. submit_onboarding_request creates
the request and completes Vendor Intake together. Every tool requires
requestor and requested_via.
"""
import os
import secrets
from typing import Annotated

from pydantic import Field

from mcp.server.mcpserver import MCPServer

import onboarding_api as api

MCP_HOST = os.environ.get("VO_MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("VO_MCP_PORT", "8788"))
MCP_PATH = os.environ.get("VO_MCP_PATH", "/mcp")
if not MCP_PATH.startswith("/"):
    MCP_PATH = "/" + MCP_PATH
DEFAULT_MCP_TOKEN = "2026-str-mcp"


def mcp_token():
    """Bearer token required on /mcp. VO_MCP_TOKEN replaces the built-in default."""
    value = os.environ.get("VO_MCP_TOKEN", "").strip()
    return value or DEFAULT_MCP_TOKEN


def _authorization_header(scope):
    for name, value in scope.get("headers") or ():
        if name.lower() == b"authorization":
            return value.decode("latin-1")
    return ""


def _bearer_matches(header, expected):
    scheme, _, presented = header.strip().partition(" ")
    if scheme.lower() != "bearer":
        return False
    presented = presented.strip()
    return len(presented) == len(expected) and secrets.compare_digest(presented, expected)


class RequireMcpToken:
    """Reject MCP HTTP requests that omit the configured bearer token."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        if _bearer_matches(_authorization_header(scope), mcp_token()):
            await self.app(scope, receive, send)
            return
        body = b'{"error":"unauthorized"}'
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b"Bearer"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body})

server = MCPServer(
    name="vendor-master",
    instructions=(
        "Vendor Master is the system of record for vendors. Submit one onboarding "
        "request to create the vendor record and complete Vendor Intake. Then record "
        "research, ops review, security approval when the vendor touches customer data, "
        "and the manager decision. Call get_pending_requests with an elapsed time in "
        "minutes or hours to list vendors waiting on Ops Review, Security Approval, or "
        "Manager Decision longer than that. Every tool requires requestor and requested_via."
    ),
)

Requestor = Annotated[str, Field(description="Person performing this step. Required.")]
RequestedVia = Annotated[str, Field(
    description="Channel used for this step, such as web, slack, email, or grok. Required.")]
RequestId = Annotated[str, Field(description="Onboarding request id, for example VO-20261104-001.")]


@server.tool(description=(
    "Submit a vendor onboarding request and complete Vendor Intake in the same step. "
    "Call this once when the requestor has the vendor legal name and whether the vendor "
    "touches customer data. Creates the request id, records requestor and requested_via, "
    "marks Vendor Intake completed, and routes the request to Vendor Research. "
    "If the vendor touches customer data, Security Approval opens as well. "
    "Notifies the configured status webhook."
))
def submit_onboarding_request(
    vendor_legal_name: Annotated[str, Field(description="Vendor legal name.")],
    touches_customer_data: Annotated[bool, Field(description="True when the vendor will touch customer data.")],
    requestor: Requestor,
    requested_via: RequestedVia,
    vendor_country: Annotated[str, Field(description="Vendor country.")] = "",
    website: Annotated[str, Field(description="Vendor website or contact.")] = "",
    customer_data_details: Annotated[str, Field(description="What customer data is touched.")] = "",
    justification_for_access: Annotated[str, Field(description="Why the vendor needs access.")] = "",
    business_owner: Annotated[str, Field(description="Business owner for this vendor.")] = "",
    target_start_date: Annotated[str, Field(description="Target start date, YYYY-MM-DD.")] = "",
    purpose: Annotated[str, Field(description="Why the vendor is being onboarded.")] = "",
    pilot_length_days: Annotated[str, Field(description="Pilot length in days.")] = "",
    requester: Annotated[str, Field(description="Person who asked for this vendor.")] = "",
    notes: Annotated[str, Field(description="Optional note stored on the request.")] = "",
) -> dict:
    return api.submit_onboarding_request(
        vendor_legal_name, touches_customer_data, requestor, requested_via,
        vendor_country, website, customer_data_details, justification_for_access,
        business_owner, target_start_date, purpose, pilot_length_days, requester, notes,
    )


@server.tool(description=(
    "Save GLEIF research and complete Vendor Research. Call this when the LEI check is done. "
    "Requires the request id, LEI, entity status, registration status, requestor, and requested_via. "
    "Marks Vendor Research completed and routes the request to Ops Review."
))
def record_vendor_research(
    request_id: RequestId,
    lei: Annotated[str, Field(description="Legal Entity Identifier from GLEIF.")],
    entity_status: Annotated[str, Field(description="GLEIF entity status, such as ACTIVE.")],
    registration_status: Annotated[str, Field(description="GLEIF registration status, such as ISSUED.")],
    requestor: Requestor,
    requested_via: RequestedVia,
    source: Annotated[str, Field(description="Link or citation for the GLEIF check.")] = "",
    note: Annotated[str, Field(description="Research note.")] = "",
) -> dict:
    return api.record_vendor_research(
        request_id, lei, entity_status, registration_status, requestor, requested_via, source, note)


@server.tool(description=(
    "Record Ops Review approval and complete that step. Call this when Human - Ops Review "
    "has approved the vendor. Requires the request id, requestor, and requested_via. "
    "Routes the request to Manager Approval."
))
def record_ops_review(
    request_id: RequestId,
    requestor: Requestor,
    requested_via: RequestedVia,
    note: Annotated[str, Field(description="Optional ops review note.")] = "",
) -> dict:
    return api.record_ops_review(request_id, requestor, requested_via, note)


@server.tool(description=(
    "Record Security Approval and complete that step. Call this only when the request "
    "touches customer data and Human - Security has approved it. Requires the request id, "
    "requestor, and requested_via. Routes that path to Manager Approval and does not send "
    "the request to Ops Review."
))
def record_security_approval(
    request_id: RequestId,
    requestor: Requestor,
    requested_via: RequestedVia,
    note: Annotated[str, Field(description="Optional security note.")] = "",
) -> dict:
    return api.record_security_approval(request_id, requestor, requested_via, note)


@server.tool(description=(
    "Record the final Manager decision and complete that step. Call this when Human - Manager "
    "approves or rejects the vendor. Requires the request id, decision (approve or reject), "
    "requestor, and requested_via. Sets the status to Approved or Rejected."
))
def record_manager_decision(
    request_id: RequestId,
    decision: Annotated[str, Field(description="approve or reject.")],
    requestor: Requestor,
    requested_via: RequestedVia,
    note: Annotated[str, Field(description="Optional manager note.")] = "",
) -> dict:
    return api.record_manager_decision(request_id, decision, requestor, requested_via, note)


@server.tool(description=(
    "Get the full onboarding request. Call this before updating a request or when you need "
    "intake, research, step timing, the current actor, and the audit trail. Requires the "
    "request id, requestor, and requested_via."
))
def get_onboarding_request(
    request_id: RequestId,
    requestor: Requestor,
    requested_via: RequestedVia,
) -> dict:
    return api.get_onboarding_request(request_id, requestor, requested_via)


@server.tool(description=(
    "Get the current status of one request. Call this for a quick lookup when the full "
    "record is not needed. Requires the request id, requestor, and requested_via. Returns "
    "the status, current actor, and last updated time."
))
def get_onboarding_status(
    request_id: RequestId,
    requestor: Requestor,
    requested_via: RequestedVia,
) -> dict:
    return api.get_onboarding_status(request_id, requestor, requested_via)


@server.tool(description=(
    "List vendors waiting on Ops Review, Security Approval, or Manager Decision for longer "
    "than the elapsed time. Pass elapsed as a number and unit as minutes or hours. "
    "The wait is measured from when that stage opened. A vendor waiting on more than one "
    "of these stages includes each stage that is over the elapsed time. "
    "Requires requestor and requested_via. Does not change any request."
))
def get_pending_requests(
    elapsed: Annotated[float, Field(description="Amount of time the vendor has been waiting. Only longer waits are returned.")],
    unit: Annotated[str, Field(description="minutes or hours.")],
    requestor: Requestor,
    requested_via: RequestedVia,
) -> dict:
    return api.get_pending_requests(elapsed, unit, requestor, requested_via)


def serve():
    """Serve MCP over streamable HTTP. Blocks until the process stops."""
    import uvicorn

    starlette_app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        host=MCP_HOST,
    )
    starlette_app.add_middleware(RequireMcpToken)
    config = uvicorn.Config(
        starlette_app,
        host=MCP_HOST,
        port=MCP_PORT,
        log_level=server.settings.log_level.lower(),
    )
    uvicorn.Server(config).run()


def client_url(host=None):
    """URL a bot on this machine uses. 0.0.0.0 is the bind address, not a client host."""
    shown = host or MCP_HOST
    if shown in ("0.0.0.0", "::"):
        shown = "127.0.0.1"
    return f"http://{shown}:{MCP_PORT}{MCP_PATH}"


if __name__ == "__main__":
    serve()
