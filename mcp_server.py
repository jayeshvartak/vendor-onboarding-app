"""Vendor onboarding MCP server for the Grok bot.

Run over stdio (the usual MCP client connection):

    .venv/bin/python mcp_server.py

The server reads and writes the same tracker as the web app (VO_STORE_DIR).
There is no create-onboarding-request tool. submit_onboarding_request creates
the request and completes Vendor Intake together. Every tool requires
requestor and requested_via.
"""
from typing import Annotated

from pydantic import Field

from mcp.server.mcpserver import MCPServer

import onboarding_api as api

server = MCPServer(
    name="vendor-onboarding",
    instructions=(
        "Vendor onboarding tracker. Submit one onboarding request to create it and "
        "complete Vendor Intake. Then record research, ops review, security approval "
        "when the vendor touches customer data, and the manager decision. "
        "Every tool requires requestor and requested_via."
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


if __name__ == "__main__":
    server.run(transport="stdio")
