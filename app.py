"""Vendor Onboarding — local internal web app.

Source of truth: the tracker store (requests.json primary; requests.csv and
audit-log.csv kept in sync). Override with VO_STORE_DIR.
"""
import csv
import fcntl
import io
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib import error as urlerror
from urllib import request as urlrequest

from flask import Flask, abort, flash, redirect, render_template, request, url_for

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STORE_DIR = os.environ.get(
    "VO_STORE_DIR",
    "/cursor/stores/store-bbb722d7-fc2a-49c1-9ed4-c36f05810eb8/vendor-onboarding",
)
JSON_PATH = os.path.join(STORE_DIR, "requests.json")
CSV_PATH = os.path.join(STORE_DIR, "requests.csv")
AUDIT_PATH = os.path.join(STORE_DIR, "audit-log.csv")
LOCK_PATH = os.path.join(APP_DIR, ".store.lock")

REQ_COLS = [
    ("Request ID", "id"), ("Created At (UTC)", "createdAt"), ("Vendor Name", "vendorName"),
    ("Vendor Contact", "vendorContact"), ("Requested By", "requestedBy"),
    ("Current Actor", "currentActor"), ("Status", "status"),
    ("Last Updated (UTC)", "lastUpdated"), ("Last Reminder (UTC)", "lastReminder"),
    ("Manager", "manager"), ("Notes", "notes"), ("LEI", "lei"),
    ("Entity Status", "entityStatus"), ("Registration Status", "registrationStatus"),
]
AUDIT_COLS = [
    ("Timestamp (UTC)", "timestamp"), ("Request ID", "requestId"), ("Actor", "actor"),
    ("Action", "action"), ("From Status", "fromStatus"), ("To Status", "toStatus"),
    ("Details", "details"),
]

# Workflow transitions offered on the request detail page.
# Rule: Security approval goes to Human - Manager (NOT Ops Review).
TRANSITIONS = {
    "intake_no_cd": dict(label="Intake complete — no customer data", actor="Vendor Intake",
                         next_actor="Vendor Research", status="With Vendor Research",
                         details="touches_customer_data=no; routed to Vendor Research"),
    "intake_cd": dict(label="Intake complete — touches customer data", actor="Vendor Intake",
                      next_actor="Vendor Research + Human - Security",
                      status="With Vendor Research + Awaiting Security",
                      details="touches_customer_data=yes; routed to Vendor Research and Human - Security"),
    "research_done": dict(label="Research complete → Ops Review", actor="Vendor Research",
                          next_actor="Human - Ops Review", status="Awaiting Ops Review",
                          details="Research complete; routed to Human - Ops Review"),
    "ops_approved": dict(label="Ops Review approved → Manager", actor="Human - Ops Review",
                         next_actor="Human - Manager", status="Awaiting Manager Approval",
                         details="Ops Review approved; routed to Human - Manager"),
    "security_approved": dict(label="Security approved → Manager", actor="Human - Security",
                              next_actor="Human - Manager", status="Awaiting Manager Approval",
                              details="Security approved; routed to Human - Manager (per workflow rule)"),
    "manager_approved": dict(label="Manager final APPROVE", actor="Human - Manager",
                             next_actor="—", status="Approved",
                             details="Final approval by Human - Manager"),
    "manager_rejected": dict(label="Manager final REJECT", actor="Human - Manager",
                             next_actor="—", status="Rejected",
                             details="Rejected by Human - Manager"),
}

app = Flask(__name__)
app.secret_key = os.environ.get("VO_SECRET", "local-demo-only")

WEBHOOK_URL = os.environ.get("VO_STATUS_WEBHOOK_URL", "").strip()
WEBHOOK_KEY = os.environ.get("VO_STATUS_WEBHOOK_KEY", "").strip()
_CFG = os.path.join(APP_DIR, "webhook.env")


def _load_webhook_cfg():
    """Optional local webhook.env: VO_STATUS_WEBHOOK_URL=... and VO_STATUS_WEBHOOK_KEY=..."""
    global WEBHOOK_URL, WEBHOOK_KEY
    if WEBHOOK_URL and WEBHOOK_KEY:
        return
    if not os.path.isfile(_CFG):
        return
    try:
        with open(_CFG, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k == "VO_STATUS_WEBHOOK_URL" and v and not WEBHOOK_URL:
                    WEBHOOK_URL = v
                elif k == "VO_STATUS_WEBHOOK_KEY" and v and not WEBHOOK_KEY:
                    WEBHOOK_KEY = v
    except OSError:
        pass


_load_webhook_cfg()


def push_status_change(payload):
    """Best-effort real-time push to the Grok Bot webhook routine. Never blocks the request."""
    if not WEBHOOK_URL or not WEBHOOK_KEY:
        return
    body = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(
        WEBHOOK_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {WEBHOOK_KEY}",
        },
    )
    try:
        with urlrequest.urlopen(req, timeout=5) as resp:
            resp.read()
    except Exception as exc:  # noqa: BLE001 — never fail the user-facing write
        try:
            with open(os.path.join(APP_DIR, "app.log"), "a", encoding="utf-8") as log:
                log.write(f"[webhook] push failed: {exc}\n")
        except OSError:
            pass



def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


@contextmanager
def store_lock():
    with open(LOCK_PATH, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _atomic_write(path, text):
    d = os.path.dirname(path)
    try:
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=os.path.basename(path))
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        # Fallback for mounts that don't support rename-over: write in place.
        try:
            os.unlink(tmp)
        except Exception:
            pass
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)


def load():
    with open(JSON_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    data.setdefault("requests", [])
    data.setdefault("auditLog", [])
    return data


def load_audit_csv():
    if not os.path.exists(AUDIT_PATH):
        return []
    with open(AUDIT_PATH, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    return [{k: r.get(h, "") for h, k in AUDIT_COLS} for r in rows]


def merged_audit(request_id=None):
    seen, out = set(), []
    data = load()
    for src, rows in (("json", data["auditLog"]), ("csv", load_audit_csv())):
        for r in rows:
            key = (r.get("timestamp"), r.get("requestId"), r.get("action"))
            if key in seen:
                continue
            seen.add(key)
            out.append(dict(r, _src=src))
    if request_id:
        out = [r for r in out if r.get("requestId") == request_id]
    out.sort(key=lambda r: r.get("timestamp") or "")
    return out


def save(data, new_audit_rows):
    """Write requests.json, rewrite requests.csv, append audit rows to audit-log.csv."""
    _atomic_write(JSON_PATH, json.dumps(data, indent=2) + "\n")
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([h for h, _ in REQ_COLS])
    for r in data["requests"]:
        w.writerow(["" if r.get(k) is None else r.get(k) for _, k in REQ_COLS])
    _atomic_write(CSV_PATH, buf.getvalue())
    if new_audit_rows:
        exists = os.path.exists(AUDIT_PATH) and os.path.getsize(AUDIT_PATH) > 0
        with open(AUDIT_PATH, "a", encoding="utf-8", newline="") as fh:
            if exists:
                fh.seek(0, os.SEEK_END)
            w = csv.writer(fh, lineterminator="\n")
            if not exists:
                w.writerow([h for h, _ in AUDIT_COLS])
            for a in new_audit_rows:
                w.writerow([a.get(k, "") for _, k in AUDIT_COLS])


def next_id(data):
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    pat = re.compile(rf"^VO-{day}-(\d+)$")
    nums = [int(m.group(1)) for r in data["requests"] if (m := pat.match(r.get("id", "")))]
    return f"VO-{day}-{(max(nums) + 1 if nums else 1):03d}"


def find(data, rid):
    return next((r for r in data["requests"] if r.get("id") == rid), None)


def status_class(s):
    s = (s or "").lower()
    if "approved" == s:
        return "ok"
    if "reject" in s:
        return "bad"
    if "security" in s:
        return "warn"
    if "manager" in s:
        return "mgr"
    if "ops" in s:
        return "ops"
    if "research" in s:
        return "res"
    return "intake"


app.jinja_env.globals.update(status_class=status_class, store_dir=STORE_DIR)


INTAKE_FIELDS = [
    ("vendor_legal_name", "Vendor legal name"), ("vendor_country", "Vendor country"),
    ("website", "Website"), ("touches_customer_data", "Touches customer data"),
    ("customer_data_details", "Customer data details"),
    ("justification_for_access", "Justification for access"),
    ("business_owner", "Business owner"), ("target_start_date", "Target start date"),
    ("purpose", "Purpose"), ("pilot_length_days", "Pilot length (days)"),
    ("requester", "Requester"),
]


def _fmt(v):
    if v is None or v == "":
        return "—"
    if v is True:
        return "Yes"
    if v is False:
        return "No"
    return v


def intake_rows(r):
    """All intake fields (known order first, then any extras). Falls back to request hints."""
    intake = r.get("intake") or {}
    hints = r.get("requestHints") or {}
    rows = []
    for k, label in INTAKE_FIELDS:
        v = intake.get(k)
        if v in (None, "") and k in hints:
            v = hints[k]
        if v in (None, "") and k == "vendor_legal_name":
            v = r.get("vendorName")
        if v in (None, "") and k == "touches_customer_data":
            v = r.get("touchesCustomerData")
        rows.append((label, _fmt(v)))
    known = {k for k, _ in INTAKE_FIELDS}
    for k, v in intake.items():
        if k not in known:
            rows.append((k.replace("_", " ").capitalize(), _fmt(v)))
    return rows


def flow_states(r, audit):
    """Per-step state for the merged workflow diagram: done | active | pending | na | rejected."""
    s = (r.get("status") or "").lower()
    a = (r.get("currentActor") or "").lower()
    acts = " | ".join((x.get("action") or "").lower() for x in audit)
    tcd = r.get("touchesCustomerData")
    if tcd is None and isinstance(r.get("intake"), dict) and "touches_customer_data" in r["intake"]:
        v = r["intake"]["touches_customer_data"]
        tcd = v if isinstance(v, bool) else str(v).strip().lower() in ("yes", "true", "y", "1")
    final = s in ("approved", "rejected")
    rejected = s == "rejected"

    st = {"lob": "done"}
    intake_done = tcd is not None or bool(r.get("intake")) or (s and "intake" not in s)
    st["intake"] = "done" if intake_done else "active"
    st["decision"] = "done" if tcd is not None else "pending"

    if tcd is False:
        st["security"] = "na"
    elif tcd is True:
        if r.get("securityApproved") or "security approved" in acts or final:
            st["security"] = "done"
        elif "security" in s or "security" in a:
            st["security"] = "active"
        else:
            st["security"] = "pending"
    else:
        st["security"] = "pending"

    if r.get("researchComplete") or final:
        st["research"] = "done"
    elif "research" in s or "research" in a:
        st["research"] = "active"
    else:
        st["research"] = "pending"

    ops_done = (r.get("opsApproved") or "ops review approved" in acts or final
                or ("manager" in s and tcd is not True))
    if ops_done:
        st["ops"] = "done"
    elif "ops" in s or "ops review" in a:
        st["ops"] = "active"
    else:
        st["ops"] = "pending"

    if final:
        st["manager"] = "rejected" if rejected else "done"
        st["end"] = "rejected" if rejected else "done"
    else:
        st["manager"] = "active" if ("manager" in s or a.endswith("manager")) else "pending"
        st["end"] = "pending"
    return {"states": st, "tcd": tcd, "endLabel": "Rejected" if rejected else ("Approved" if final else "Done")}


@app.route("/")
def dashboard():
    data = load()
    reqs = data["requests"]
    statuses = sorted({r.get("status", "") for r in reqs if r.get("status")})
    actors = sorted({r.get("currentActor", "") for r in reqs if r.get("currentActor")})
    fs, fa, q = request.args.get("status", ""), request.args.get("actor", ""), request.args.get("q", "").strip()
    shown = [r for r in reqs
             if (not fs or r.get("status") == fs)
             and (not fa or r.get("currentActor") == fa)
             and (not q or q.lower() in (r.get("vendorName", "") + r.get("id", "")).lower())]
    shown = sorted(shown, key=lambda r: r.get("lastUpdated", ""), reverse=True)
    counts = {}
    for r in reqs:
        counts[r.get("status", "")] = counts.get(r.get("status", ""), 0) + 1
    return render_template("dashboard.html", reqs=shown, total=len(reqs), statuses=statuses,
                           actors=actors, fs=fs, fa=fa, q=q, counts=counts, view="dashboard")


@app.route("/new", methods=["GET", "POST"])
def new_request():
    if request.method == "GET":
        return render_template("new.html", view="new", form={})
    f = request.form
    name = f.get("vendorName", "").strip()
    if not name:
        flash("Vendor legal name is required.", "error")
        return render_template("new.html", view="new", form=f), 400
    with store_lock():
        data = load()
        rid = next_id(data)
        ts = now_utc()
        rec = {
            "id": rid, "createdAt": ts, "vendorName": name,
            "vendorContact": f.get("vendorContact", "").strip(),
            "requestedBy": "LOB Requestor (web)", "currentActor": "Vendor Intake",
            "status": "With Vendor Intake", "lastUpdated": ts, "lastReminder": "",
            "manager": "Human - Manager",
            "notes": (f.get("notes", "").strip() or "Created via web app; routed to Vendor Intake."),
            "lei": "", "entityStatus": "", "registrationStatus": "",
            "touchesCustomerData": None, "researchComplete": False,
        }
        hints = {k: f.get(k, "").strip() for k in ("vendor_country", "business_owner", "purpose", "requester")}
        hints = {k: v for k, v in hints.items() if v}
        if hints:
            rec["requestHints"] = hints
        audit = {"timestamp": ts, "requestId": rid, "actor": "LOB Requestor (web)",
                 "action": "Created request", "fromStatus": "", "toStatus": "With Vendor Intake",
                 "details": f"Vendor: {name}; routed to Vendor Intake"}
        data["requests"].append(rec)
        data["auditLog"].append(audit)
        save(data, [audit])
    push_status_change({
        "event": "request_created",
        "requestId": rid,
        "vendorName": name,
        "fromStatus": "",
        "toStatus": "With Vendor Intake",
        "currentActor": "Vendor Intake",
        "lastUpdated": ts,
        "notes": rec.get("notes", ""),
    })
    flash(f"Created {rid} and routed to Vendor Intake.", "ok")
    return redirect(url_for("detail", rid=rid))


@app.route("/request/<rid>")
def detail(rid):
    data = load()
    r = find(data, rid)
    if not r:
        abort(404)
    aud = merged_audit(rid)
    return render_template("detail.html", r=r, audit=aud, transitions=TRANSITIONS,
                           flow=flow_states(r, aud), intake_rows=intake_rows(r), view="dashboard")


@app.route("/request/<rid>/action", methods=["POST"])
def action(rid):
    key = request.form.get("transition", "")
    note = request.form.get("note", "").strip()
    t = TRANSITIONS.get(key)
    if not t:
        flash("Pick a workflow action.", "error")
        return redirect(url_for("detail", rid=rid))
    with store_lock():
        data = load()
        r = find(data, rid)
        if not r:
            abort(404)
        ts = now_utc()
        frm = r.get("status", "")
        r.update(currentActor=t["next_actor"], status=t["status"], lastUpdated=ts)
        if key == "intake_no_cd":
            r["touchesCustomerData"] = False
        elif key == "intake_cd":
            r["touchesCustomerData"] = True
        elif key == "research_done":
            r["researchComplete"] = True
        elif key == "security_approved":
            r["securityApproved"] = True
        elif key == "ops_approved":
            r["opsApproved"] = True
        r["notes"] = f"{t['label']}." + (f" {note}" if note else "")
        audit = {"timestamp": ts, "requestId": rid, "actor": f"{t['actor']} (web)",
                 "action": t["label"], "fromStatus": frm, "toStatus": t["status"],
                 "details": t["details"] + (f". Note: {note}" if note else "")}
        data["auditLog"].append(audit)
        save(data, [audit])
        vendor = r.get("vendorName", "")
        notes = r.get("notes", "")
        actor = r.get("currentActor", "")
        to_status = t["status"]
    push_status_change({
        "event": "status_changed",
        "requestId": rid,
        "vendorName": vendor,
        "fromStatus": frm,
        "toStatus": to_status,
        "currentActor": actor,
        "lastUpdated": ts,
        "notes": notes,
        "action": t["label"],
    })
    flash(f"{rid}: {t['label']}.", "ok")
    return redirect(url_for("detail", rid=rid))


@app.route("/status")
def status():
    rid = request.args.get("id", "").strip().upper()
    r = find(load(), rid) if rid else None
    return render_template("status.html", rid=rid, r=r, view="status")


@app.route("/audit")
def audit():
    rid = request.args.get("id", "").strip()
    rows = merged_audit(rid or None)
    rows.reverse()
    ids = sorted({x.get("requestId", "") for x in merged_audit() if x.get("requestId")})
    return render_template("audit.html", rows=rows, rid=rid, ids=ids, view="audit")


@app.route("/workflow")
def workflow():
    return render_template("workflow.html", view="workflow", flow=None)


@app.route("/healthz")
def healthz():
    return {"ok": True, "store": STORE_DIR, "requests": len(load()["requests"])}


if __name__ == "__main__":
    app.run(host=os.environ.get("VO_HOST", "0.0.0.0"), port=int(os.environ.get("VO_PORT", "8787")))
