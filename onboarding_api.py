"""Store operations for the Vendor Master MCP tools.

Submitting an onboarding request creates the record and completes Vendor Intake
in one step. There is no separate create-request tool. Every call requires
requestor and requested_via.
"""
import math
from datetime import datetime, timezone

import app as vo


# Stages a caller can ask about. The wait starts when that step opens.
PENDING_STAGES = (
    ("ops", "Ops Review", "awaiting ops review"),
    ("security", "Security Approval", "awaiting security"),
    ("manager", "Manager Decision", "awaiting manager approval"),
)


def _actor(requestor, requested_via):
    requestor = vo._clean(requestor)
    requested_via = vo._clean(requested_via)
    if not requestor:
        return None, {"ok": False, "error": "requestor is required"}
    if not requested_via:
        return None, {"ok": False, "error": "requested_via is required"}
    return (requestor, requested_via), None


def _bool(value):
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in ("yes", "true", "y", "1"):
        return True
    if text in ("no", "false", "n", "0"):
        return False
    return None


def _find(data, request_id):
    rid = vo._clean(request_id, 40).upper()
    if not rid:
        return None, {"ok": False, "error": "request_id is required"}
    record = vo.find(data, rid)
    if not record:
        return None, {"ok": False, "error": f"Request {rid} was not found"}
    return record, None


def _status_body(record, requestor, requested_via):
    return {
        "ok": True,
        "requestId": record.get("id"),
        "vendorName": record.get("vendorName", ""),
        "status": record.get("status", ""),
        "currentActor": record.get("currentActor", ""),
        "lastUpdated": record.get("lastUpdated", ""),
        "touchesCustomerData": record.get("touchesCustomerData"),
        "requestor": requestor,
        "requested_via": requested_via,
    }


def _full_body(record, requestor, requested_via):
    audit = vo.merged_audit(record.get("id"))
    body = _status_body(record, requestor, requested_via)
    body.update({
        "createdAt": record.get("createdAt", ""),
        "requestedBy": record.get("requestedBy", ""),
        "notes": record.get("notes", ""),
        "lei": record.get("lei") or "",
        "entityStatus": record.get("entityStatus") or "",
        "registrationStatus": record.get("registrationStatus") or "",
        "researchComplete": bool(record.get("researchComplete")),
        "intake": record.get("intake") or {},
        "research": record.get("research") or {},
        "steps": vo.resolved_steps(record, audit),
        "audit": [
            {
                "timestamp": row.get("timestamp", ""),
                "actor": row.get("actor", ""),
                "action": row.get("action", ""),
                "fromStatus": row.get("fromStatus", ""),
                "toStatus": row.get("toStatus", ""),
                "details": row.get("details", ""),
                "requestor": row.get("requestor", ""),
                "requested_via": row.get("requested_via", ""),
            }
            for row in audit
        ],
    })
    return body


def _apply(data, record, key, requestor, requested_via, note=""):
    ts = vo.now_utc()
    previous = record.get("status", "")
    transition = vo.TRANSITIONS[key]
    record.update(currentActor=transition["next_actor"], status=transition["status"], lastUpdated=ts)
    if key == "research_done":
        record["researchComplete"] = True
    elif key == "security_approved":
        record["securityApproved"] = True
    elif key == "ops_approved":
        record["opsApproved"] = True
    record["notes"] = transition["label"] + (f" {note}" if note else "")
    vo.ensure_steps(record, vo.audit_for(data, record["id"]))
    vo.record_step_transition(record, key, ts, requestor, requested_via)
    audit = {
        "timestamp": ts,
        "requestId": record["id"],
        "actor": f"{requestor} ({requested_via})",
        "action": transition["label"],
        "fromStatus": previous,
        "toStatus": transition["status"],
        "details": (
            f"{transition['details']}. requestor={requestor}; requested_via={requested_via}"
            + (f". Note: {note}" if note else "")
        ),
        "requestor": requestor,
        "requested_via": requested_via,
    }
    data["auditLog"].append(audit)
    return previous, transition, ts, audit


def submit_onboarding_request(
    vendor_legal_name,
    touches_customer_data,
    requestor,
    requested_via,
    vendor_country="",
    website="",
    customer_data_details="",
    justification_for_access="",
    business_owner="",
    target_start_date="",
    purpose="",
    pilot_length_days="",
    requester="",
    notes="",
):
    """Create the request and complete Vendor Intake together."""
    who, err = _actor(requestor, requested_via)
    if err:
        return err
    requestor, requested_via = who
    name = vo._clean(vendor_legal_name, 300)
    if not name:
        return {"ok": False, "error": "vendor_legal_name is required"}
    touches = _bool(touches_customer_data)
    if touches is None:
        return {"ok": False, "error": "touches_customer_data is required (yes or no)"}
    form = {
        "vendor_legal_name": name,
        "vendor_country": vendor_country,
        "website": website,
        "touches_customer_data": "yes" if touches else "no",
        "customer_data_details": customer_data_details,
        "justification_for_access": justification_for_access,
        "business_owner": business_owner,
        "target_start_date": target_start_date,
        "purpose": purpose,
        "pilot_length_days": "" if pilot_length_days is None else str(pilot_length_days),
        "requester": requester,
    }
    intake = vo._intake_payload(form)
    intake["vendor_legal_name"] = name
    note = vo._clean(notes, 2000)
    key = "intake_cd" if touches else "intake_no_cd"
    transition = vo.TRANSITIONS[key]
    with vo.store_lock():
        data = vo.load()
        rid = vo.next_id(data)
        ts = vo.now_utc()
        record = {
            "id": rid,
            "createdAt": ts,
            "vendorName": name,
            "vendorContact": intake.get("website", ""),
            "requestedBy": f"{requestor} ({requested_via})",
            "currentActor": transition["next_actor"],
            "status": transition["status"],
            "lastUpdated": ts,
            "lastReminder": "",
            "manager": "Human - Manager",
            "notes": transition["label"] + (f" {note}" if note else ""),
            "lei": "",
            "entityStatus": "",
            "registrationStatus": "",
            "touchesCustomerData": touches,
            "researchComplete": False,
            "intake": intake,
        }
        hints = {k: vo._clean(form.get(k)) for k in ("vendor_country", "business_owner", "purpose", "requester")}
        hints = {k: v for k, v in hints.items() if v}
        if hints:
            record["requestHints"] = hints
        vo.close_step(record, "lob", ts, requestor, requested_via)
        vo.close_step(record, "intake", ts, requestor, requested_via)
        vo.open_step(record, "research", ts)
        if touches:
            vo.open_step(record, "security", ts)
        created = {
            "timestamp": ts, "requestId": rid, "actor": f"{requestor} ({requested_via})",
            "action": "Submitted onboarding request", "fromStatus": "", "toStatus": transition["status"],
            "details": (
                f"Vendor: {name}; Vendor Intake completed with the onboarding request. "
                f"{transition['details']}. requestor={requestor}; requested_via={requested_via}"
                + (f". Note: {note}" if note else "")
            ),
            "requestor": requestor, "requested_via": requested_via,
        }
        data["requests"].append(record)
        data["auditLog"].append(created)
        vo.save(data, [created])
        payload = {
            "event": "intake_completed",
            "requestId": rid,
            "vendorName": name,
            "fromStatus": "",
            "toStatus": transition["status"],
            "currentActor": transition["next_actor"],
            "lastUpdated": ts,
            "notes": record["notes"],
            "action": "Submitted onboarding request",
            "step": "intake",
            "startedAt": ts,
            "endedAt": ts,
            "requestor": requestor,
            "requested_via": requested_via,
            "touchesCustomerData": touches,
            "intake": intake,
        }
    webhook = vo.push_status_change(payload)
    return {
        "ok": True,
        "requestId": rid,
        "status": transition["status"],
        "currentActor": transition["next_actor"],
        "touchesCustomerData": touches,
        "webhook": webhook,
        "requestor": requestor,
        "requested_via": requested_via,
    }


def record_vendor_research(
    request_id,
    lei,
    entity_status,
    registration_status,
    requestor,
    requested_via,
    source="",
    note="",
):
    """Save GLEIF research and complete Vendor Research."""
    who, err = _actor(requestor, requested_via)
    if err:
        return err
    requestor, requested_via = who
    lei = vo._clean(lei, 40)
    entity_status = vo._clean(entity_status, 80)
    registration_status = vo._clean(registration_status, 80)
    if not lei or not entity_status or not registration_status:
        return {"ok": False, "error": "lei, entity_status, and registration_status are required"}
    with vo.store_lock():
        data = vo.load()
        record, err = _find(data, request_id)
        if err:
            return err
        record["lei"] = lei
        record["entityStatus"] = entity_status
        record["registrationStatus"] = registration_status
        record["research"] = {
            "lei": lei,
            "entityStatus": entity_status,
            "registrationStatus": registration_status,
            "source": vo._clean(source, 500),
            "note": vo._clean(note, 2000),
        }
        previous, transition, ts, _audit = _apply(
            data, record, "research_done", requestor, requested_via, vo._clean(note, 2000))
        vo.save(data, [data["auditLog"][-1]])
        vendor = record.get("vendorName", "")
        notes = record.get("notes", "")
        actor = record.get("currentActor", "")
        to_status = transition["status"]
        rid = record["id"]
    webhook = vo.push_status_change({
        "event": "status_changed",
        "requestId": rid,
        "vendorName": vendor,
        "fromStatus": previous,
        "toStatus": to_status,
        "currentActor": actor,
        "lastUpdated": ts,
        "notes": notes,
        "action": transition["label"],
        "step": "research",
        "requestor": requestor,
        "requested_via": requested_via,
        "lei": lei,
        "entityStatus": entity_status,
        "registrationStatus": registration_status,
    })
    return {
        "ok": True,
        "requestId": rid,
        "status": to_status,
        "currentActor": actor,
        "lei": lei,
        "webhook": webhook,
        "requestor": requestor,
        "requested_via": requested_via,
    }


def _complete(request_id, key, requestor, requested_via, note, step, extra_check=None):
    who, err = _actor(requestor, requested_via)
    if err:
        return err
    requestor, requested_via = who
    with vo.store_lock():
        data = vo.load()
        record, err = _find(data, request_id)
        if err:
            return err
        if extra_check:
            blocked = extra_check(record)
            if blocked:
                return blocked
        previous, transition, ts, _audit = _apply(
            data, record, key, requestor, requested_via, vo._clean(note, 2000))
        vo.save(data, [data["auditLog"][-1]])
        vendor = record.get("vendorName", "")
        notes = record.get("notes", "")
        actor = record.get("currentActor", "")
        to_status = transition["status"]
        rid = record["id"]
        label = transition["label"]
    webhook = vo.push_status_change({
        "event": "status_changed",
        "requestId": rid,
        "vendorName": vendor,
        "fromStatus": previous,
        "toStatus": to_status,
        "currentActor": actor,
        "lastUpdated": ts,
        "notes": notes,
        "action": label,
        "step": step,
        "requestor": requestor,
        "requested_via": requested_via,
    })
    return {
        "ok": True,
        "requestId": rid,
        "status": to_status,
        "currentActor": actor,
        "webhook": webhook,
        "requestor": requestor,
        "requested_via": requested_via,
    }


def record_ops_review(request_id, requestor, requested_via, note=""):
    """Record Ops Review approval and route the request to Manager Approval."""
    return _complete(request_id, "ops_approved", requestor, requested_via, note, "ops")


def record_security_approval(request_id, requestor, requested_via, note=""):
    """Record Security Approval. Only requests that touch customer data have this step."""
    def _guard(record):
        if record.get("touchesCustomerData") is not True:
            return {
                "ok": False,
                "error": "Security Approval applies only when the vendor touches customer data",
            }
        return None
    return _complete(request_id, "security_approved", requestor, requested_via, note, "security", _guard)


def record_manager_decision(request_id, decision, requestor, requested_via, note=""):
    """Record the final Manager approve or reject decision."""
    who, err = _actor(requestor, requested_via)
    if err:
        return err
    choice = vo._clean(decision, 20).lower()
    if choice in ("approve", "approved"):
        key = "manager_approved"
    elif choice in ("reject", "rejected"):
        key = "manager_rejected"
    else:
        return {"ok": False, "error": "decision must be approve or reject"}
    return _complete(request_id, key, requestor, requested_via, note, "manager")


def get_onboarding_request(request_id, requestor, requested_via):
    """Return the full onboarding request, including intake, research, steps, and audit."""
    who, err = _actor(requestor, requested_via)
    if err:
        return err
    requestor, requested_via = who
    data = vo.load()
    record, err = _find(data, request_id)
    if err:
        return err
    return _full_body(record, requestor, requested_via)


def get_onboarding_status(request_id, requestor, requested_via):
    """Return the current status, actor, and last update for one request."""
    who, err = _actor(requestor, requested_via)
    if err:
        return err
    requestor, requested_via = who
    data = vo.load()
    record, err = _find(data, request_id)
    if err:
        return err
    return _status_body(record, requestor, requested_via)


def _threshold(elapsed, unit):
    if isinstance(elapsed, bool):
        return None, {"ok": False, "error": "elapsed must be a number of minutes or hours"}
    try:
        amount = float(elapsed)
    except (TypeError, ValueError):
        return None, {"ok": False, "error": "elapsed must be a number of minutes or hours"}
    if not math.isfinite(amount) or amount <= 0:
        return None, {"ok": False, "error": "elapsed must be greater than zero"}
    name = vo._clean(unit, 20).lower()
    if name in ("minute", "minutes", "min", "mins", "m"):
        return (amount, "minutes", amount), None
    if name in ("hour", "hours", "hr", "hrs", "h"):
        return (amount, "hours", amount * 60), None
    return None, {"ok": False, "error": "unit must be minutes or hours"}


def _status_says(record, marker):
    return marker in (record.get("status") or "").lower()


def _pending_stages(record, audit):
    """Open Ops, Security, or Manager steps. Status is the fallback when that step was never timed."""
    steps = vo.resolved_steps(record, audit)
    hits = []
    for key, label, marker in PENDING_STAGES:
        runs = [run for run in (steps.get(key) or []) if isinstance(run, dict)]
        if runs and not runs[-1].get("endedAt"):
            started = runs[-1].get("startedAt") or record.get("lastUpdated") or ""
            hits.append((label, started))
            continue
        if runs:
            continue
        if _status_says(record, marker):
            hits.append((label, record.get("lastUpdated") or record.get("createdAt") or ""))
    return hits


def _vendor_pending(record, audit, current, threshold_minutes):
    pending = []
    for label, started in _pending_stages(record, audit):
        start = vo.parse_ts(started)
        if start is None:
            continue
        seconds = (current - start).total_seconds()
        if seconds <= threshold_minutes * 60:
            continue
        pending.append({
            "stage": label,
            "pendingSince": started,
            "elapsedMinutes": round(seconds / 60, 1),
            "elapsed": vo.fmt_duration(seconds),
        })
    pending.sort(key=lambda item: item["elapsedMinutes"], reverse=True)
    if not pending:
        return None
    return {
        "requestId": record.get("id"),
        "vendorName": record.get("vendorName", ""),
        "status": record.get("status", ""),
        "currentActor": record.get("currentActor", ""),
        "touchesCustomerData": record.get("touchesCustomerData"),
        "lei": record.get("lei") or "",
        "entityStatus": record.get("entityStatus") or "",
        "registrationStatus": record.get("registrationStatus") or "",
        "vendorContact": record.get("vendorContact") or "",
        "requestedBy": record.get("requestedBy") or "",
        "lastUpdated": record.get("lastUpdated") or "",
        "pending": pending,
    }


def get_pending_requests(elapsed, unit, requestor, requested_via, now=None):
    """Vendors waiting on Ops Review, Security Approval, or Manager Decision longer than elapsed."""
    who, err = _actor(requestor, requested_via)
    if err:
        return err
    requestor, requested_via = who
    parsed, err = _threshold(elapsed, unit)
    if err:
        return err
    amount, unit_name, threshold_minutes = parsed
    if isinstance(now, datetime):
        current = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    else:
        current = vo.parse_ts(now or vo.now_utc())
    data = vo.load()
    vendors = []
    for record in data.get("requests", []):
        audit = vo.merged_audit(record.get("id"))
        detail = _vendor_pending(record, audit, current, threshold_minutes)
        if detail:
            vendors.append(detail)
    vendors.sort(key=lambda item: item["pending"][0]["elapsedMinutes"], reverse=True)
    return {
        "ok": True,
        "elapsed": amount,
        "unit": unit_name,
        "thresholdMinutes": threshold_minutes,
        "asOf": current.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "count": len(vendors),
        "vendors": vendors,
        "requestor": requestor,
        "requested_via": requested_via,
    }
