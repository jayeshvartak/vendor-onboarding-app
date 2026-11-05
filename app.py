"""Vendor Master — system of record for vendors.

Source of truth: the tracker store (requests.json primary; requests.csv and
audit-log.csv kept in sync). Override with VO_STORE_DIR.
"""
import csv
import io
import json
import os
import re
import secrets
import tempfile

try:
    import fcntl
except ImportError:  # Windows has no fcntl
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib import error as urlerror
from urllib import parse as urlparse
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
WEBHOOKS_PATH = os.path.join(STORE_DIR, "webhooks.json")
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


def _read_saved_webhooks():
    if not os.path.isfile(WEBHOOKS_PATH):
        return []
    try:
        with open(WEBHOOKS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    rows = data.get("webhooks") if isinstance(data, dict) else None
    return [row for row in rows or [] if isinstance(row, dict)]


def registered_webhooks():
    """Environment webhook first, then webhooks saved from the Configuration page.

    The same URL is listed once. Saved rows do not include the bearer key in this list;
    `webhook_targets` is what the sender uses.
    """
    targets = webhook_targets()
    public = []
    for row in targets:
        public.append({
            "id": row["id"],
            "name": row["name"],
            "url": row["url"],
            "key_mask": mask_secret(row["key"]),
            "source": row["source"],
            "createdAt": row.get("createdAt", ""),
        })
    return public


def webhook_targets():
    """Webhooks that receive status posts, including the bearer key."""
    found = []
    seen = set()

    def add(row):
        url = (row.get("url") or "").strip()
        key = (row.get("key") or "").strip()
        if not url or not key or url in seen:
            return
        seen.add(url)
        found.append({
            "id": row.get("id") or "environment",
            "name": row.get("name") or "Webhook",
            "url": url,
            "key": key,
            "source": row.get("source") or "saved",
            "createdAt": row.get("createdAt") or "",
        })

    if WEBHOOK_URL and WEBHOOK_KEY:
        add({
            "id": "environment",
            "name": "Environment",
            "url": WEBHOOK_URL,
            "key": WEBHOOK_KEY,
            "source": "environment",
        })
    for row in _read_saved_webhooks():
        add({
            "id": row.get("id") or "",
            "name": row.get("name") or "",
            "url": row.get("url") or "",
            "key": row.get("key") or "",
            "source": "saved",
            "createdAt": row.get("createdAt") or "",
        })
    return found


def mask_secret(value):
    text = value or ""
    if not text:
        return ""
    if len(text) <= 4:
        return "•" * len(text)
    return "••••••••" + text[-4:]


def _valid_webhook_url(url):
    parts = urlparse.urlparse(url)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def _webhook_input(name, url, key):
    name = _clean(name, 120)
    url = _clean(url, 2000)
    key = (key or "").strip()
    if not url:
        return None, "Webhook URL is required."
    if not _valid_webhook_url(url):
        return None, "Webhook URL must start with http:// or https://."
    if not key:
        return None, "Webhook bearer key is required."
    if len(key) > 500:
        return None, "Webhook bearer key is too long."
    return {"name": name, "url": url, "key": key}, None


def save_webhook(name, url, key):
    """Append one webhook. Returns (row, error)."""
    fields, err = _webhook_input(name, url, key)
    if err:
        return None, err
    name, url, key = fields["name"], fields["url"], fields["key"]
    with store_lock():
        rows = _read_saved_webhooks()
        known = {(row.get("url") or "").strip() for row in rows}
        known.update(row["url"] for row in webhook_targets())
        if url in known:
            return None, "That webhook URL is already registered."
        if not name:
            name = urlparse.urlparse(url).netloc or "Webhook"
        row = {
            "id": "wh-" + secrets.token_hex(4),
            "name": name,
            "url": url,
            "key": key,
            "createdAt": now_utc(),
        }
        rows.append(row)
        _atomic_write(WEBHOOKS_PATH, json.dumps({"webhooks": rows}, indent=2) + "\n")
    return row, None


def delete_webhook(webhook_id):
    webhook_id = _clean(webhook_id, 40)
    if not webhook_id or webhook_id == "environment":
        return False
    with store_lock():
        rows = _read_saved_webhooks()
        kept = [row for row in rows if row.get("id") != webhook_id]
        if len(kept) == len(rows):
            return False
        _atomic_write(WEBHOOKS_PATH, json.dumps({"webhooks": kept}, indent=2) + "\n")
    return True


def deliver_webhook(url, key, payload):
    """POST one payload. Returns (ok, detail) where detail is the HTTP status or the error."""
    body = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )
    try:
        with urlrequest.urlopen(req, timeout=5) as resp:
            resp.read()
            return True, resp.status
    except urlerror.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except urlerror.URLError as exc:
        return False, str(getattr(exc, "reason", exc))
    except Exception as exc:  # noqa: BLE001 — caller decides how to surface this
        return False, str(exc)


def webhook_test_payload():
    return {
        "event": "webhook_test",
        "requestId": "",
        "vendorName": "",
        "fromStatus": "",
        "toStatus": "",
        "message": "Test from Vendor Master configuration. This webhook is not registered yet.",
        "sentAt": now_utc(),
    }


def push_status_change(payload):
    """Best-effort push to every registered webhook. Never fails the user-facing write.

    Returns "sent" when every webhook accepts the post, "skipped" when none are
    registered, or "failed" when any post fails.
    """
    targets = webhook_targets()
    if not targets:
        return "skipped"
    failed = False
    for target in targets:
        ok, detail = deliver_webhook(target["url"], target["key"], payload)
        if ok:
            continue
        failed = True
        try:
            with open(os.path.join(APP_DIR, "app.log"), "a", encoding="utf-8") as log:
                log.write(f"[webhook] push failed for {target['url']}: {detail}\n")
        except OSError:
            pass
    return "failed" if failed else "sent"



def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


@contextmanager
def store_lock():
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    with open(LOCK_PATH, "a+b") as fh:
        if fcntl is not None:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
        else:
            if fh.seek(0, os.SEEK_END) == 0:
                fh.write(b"\0")
                fh.flush()
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)


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
    if not os.path.isfile(JSON_PATH):
        os.makedirs(STORE_DIR, exist_ok=True)
        _atomic_write(JSON_PATH, json.dumps({"requests": [], "auditLog": []}, indent=2) + "\n")
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


def dashboard_status_label(status):
    """Dashboard names for the summary cards. Stored statuses are unchanged."""
    text = status or ""
    low = text.lower()
    if low == "with vendor intake" or "awaiting security" in low:
        return "Awaiting Security Approval"
    if text == "Awaiting Manager Approval":
        return "Awaiting Manager Decision"
    return text


# key, label, include the card even when the count is zero
DASHBOARD_CARDS = (
    ("awaiting-security-approval", "Awaiting Security Approval", True),
    ("with-vendor-research", "With Vendor Research", False),
    ("awaiting-ops-review", "Awaiting Ops Review", False),
    ("awaiting-manager-decision", "Awaiting Manager Decision", True),
    ("approved", "Approved", False),
    ("rejected", "Rejected", False),
)
_DASHBOARD_LABELS = {key: label for key, label, _always in DASHBOARD_CARDS}


def dashboard_cards(reqs):
    grouped = {label: 0 for _key, label, _always in DASHBOARD_CARDS}
    extras = {}
    for record in reqs:
        label = dashboard_status_label(record.get("status", ""))
        if label in grouped:
            grouped[label] += 1
        elif label:
            extras[label] = extras.get(label, 0) + 1
    cards = []
    for key, label, always in DASHBOARD_CARDS:
        count = grouped[label]
        if count or always:
            cards.append({"key": key, "label": label, "count": count})
    for label in sorted(extras):
        cards.append({"key": label, "label": label, "count": extras[label]})
    return cards


def dashboard_matches(record, key):
    if not key:
        return True
    label = _DASHBOARD_LABELS.get(key, key)
    return dashboard_status_label(record.get("status", "")) == label


app.jinja_env.globals.update(
    status_class=status_class,
    status_label=dashboard_status_label,
    store_dir=STORE_DIR,
)


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


# Timed workflow steps. "intake" is the Vendor Intake form.
STEP_DEFS = [
    ("lob", "LOB Requestor"),
    ("intake", "Vendor Intake form"),
    ("research", "Vendor Research"),
    ("security", "Security Approval"),
    ("ops", "Ops Review"),
    ("manager", "Manager Approval"),
]


def parse_ts(value):
    if not value:
        return None
    text = str(value).strip()
    if text.endswith(" UTC"):
        text = text[:-4].strip()
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _clean(value, limit=200):
    return (value or "").strip()[:limit]


def _runs(record, key):
    steps = record.setdefault("steps", {})
    runs = steps.get(key)
    if runs is None:
        runs = []
        steps[key] = runs
    elif isinstance(runs, dict):
        runs = [runs]
        steps[key] = runs
    return runs


def open_step(record, key, ts, requestor="", requested_via=""):
    """Start a step run. A run that is already open is left as-is."""
    runs = _runs(record, key)
    if runs and not runs[-1].get("endedAt"):
        cur = runs[-1]
    else:
        cur = {"startedAt": ts}
        runs.append(cur)
    if requestor and not cur.get("requestor"):
        cur["requestor"] = requestor
    if requested_via and not cur.get("requested_via"):
        cur["requested_via"] = requested_via
    return cur


def close_step(record, key, ts, requestor="", requested_via="", only_if_open=False):
    """End the open run. With only_if_open, do nothing when the step is not in progress."""
    runs = _runs(record, key)
    if runs and not runs[-1].get("endedAt"):
        cur = runs[-1]
        if not cur.get("startedAt"):
            cur["startedAt"] = ts
    elif only_if_open:
        return None
    else:
        cur = {"startedAt": ts}
        runs.append(cur)
    cur["endedAt"] = ts
    if requestor:
        cur["requestor"] = requestor
    if requested_via:
        cur["requested_via"] = requested_via
    return cur


def cancel_open_step(record, key):
    """Drop an in-progress run that the workflow cancelled (for example Security N/A)."""
    runs = _runs(record, key)
    if runs and not runs[-1].get("endedAt"):
        runs.pop()
    if not runs:
        record.get("steps", {}).pop(key, None)


def _actor_parts(actor):
    actor = (actor or "").strip()
    low = actor.lower()
    if low.endswith("(web)"):
        name = actor[: -len("(web)")].strip() or actor
        return name, "web"
    if "orchestrator" in low:
        return actor or "orchestrator", "orchestrator"
    return actor or "unknown", "unknown"


def _route_after_intake(record, ts, details, to_status):
    open_step(record, "research", ts)
    details_l = (details or "").lower()
    to_l = (to_status or "").lower()
    if "touches_customer_data=no" in details_l or "security path cancelled" in details_l:
        cancel_open_step(record, "security")
    elif "touches_customer_data=yes" in details_l or "awaiting security" in to_l:
        open_step(record, "security", ts)


def apply_audit_to_steps(record, entry):
    """Replay one audit row onto step runs. Used for requests saved before step timing existed."""
    ts = entry.get("timestamp") or ""
    if not parse_ts(ts):
        return
    action = (entry.get("action") or "").lower()
    details = entry.get("details") or ""
    to_status = entry.get("toStatus") or ""
    name, via = _actor_parts(entry.get("actor"))
    if action.startswith("created request"):
        close_step(record, "lob", ts, name, via)
        open_step(record, "intake", ts)
        return
    if "intake refreshed" in action:
        close_step(record, "intake", ts, name, via, only_if_open=True)
        _route_after_intake(record, ts, details, to_status)
        return
    if "intake complete" in action:
        close_step(record, "intake", ts, name, via)
        _route_after_intake(record, ts, details, to_status)
        return
    if "research complete" in action or "research reconfirmed" in action:
        close_step(record, "research", ts, name, via, only_if_open=True)
        if "ops" in to_status.lower():
            open_step(record, "ops", ts)
        return
    if "security approved" in action:
        close_step(record, "security", ts, name, via)
        if "manager" in to_status.lower():
            open_step(record, "manager", ts)
        return
    if "ops review approved" in action:
        close_step(record, "ops", ts, name, via)
        if "manager" in to_status.lower():
            open_step(record, "manager", ts)
        return
    if "manager final" in action:
        close_step(record, "manager", ts, name, via)


def derive_steps_from_audit(audit_rows):
    scratch = {"steps": {}}
    for entry in sorted(audit_rows or [], key=lambda row: row.get("timestamp") or ""):
        apply_audit_to_steps(scratch, entry)
    return scratch["steps"]


def audit_for(data, rid):
    rows = [row for row in data.get("auditLog", []) if row.get("requestId") == rid]
    rows.sort(key=lambda row: row.get("timestamp") or "")
    return rows


def ensure_steps(record, audit_rows):
    """Backfill step timing from the audit log the first time a legacy request is updated."""
    if record.get("steps"):
        return
    record["steps"] = derive_steps_from_audit(audit_rows)


def record_step_transition(record, key, ts, requestor, requested_via):
    """Open and close steps for a workflow action recorded in this app."""
    if key in ("intake_no_cd", "intake_cd"):
        close_step(record, "intake", ts, requestor, requested_via)
        open_step(record, "research", ts)
        if key == "intake_cd":
            open_step(record, "security", ts)
        else:
            cancel_open_step(record, "security")
    elif key == "research_done":
        close_step(record, "research", ts, requestor, requested_via)
        open_step(record, "ops", ts)
    elif key == "security_approved":
        close_step(record, "security", ts, requestor, requested_via)
        open_step(record, "manager", ts)
    elif key == "ops_approved":
        close_step(record, "ops", ts, requestor, requested_via)
        open_step(record, "manager", ts)
    elif key in ("manager_approved", "manager_rejected"):
        close_step(record, "manager", ts, requestor, requested_via)


def resolved_steps(record, audit_rows):
    """Stored step runs win per step; everything else is derived from the audit log."""
    derived = derive_steps_from_audit(audit_rows)
    stored = record.get("steps") or {}
    out = {}
    for key, _label in STEP_DEFS:
        runs = stored.get(key)
        if runs:
            seq = runs if isinstance(runs, list) else [runs]
            out[key] = [dict(item) for item in seq if isinstance(item, dict)]
        else:
            out[key] = [dict(item) for item in derived.get(key, [])]
    return out


def duration_seconds(run):
    start, end = parse_ts(run.get("startedAt")), parse_ts(run.get("endedAt"))
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds())


def fmt_duration(seconds):
    if seconds is None:
        return "—"
    total = int(round(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def step_rows_for(record, audit_rows):
    steps = resolved_steps(record, audit_rows)
    tcd = record.get("touchesCustomerData")
    rows = []
    for key, label in STEP_DEFS:
        runs = steps.get(key) or []
        if key == "security" and tcd is False and not runs:
            rows.append({
                "key": key, "label": label, "run": 1, "indexed": False, "started": "—", "ended": "—",
                "duration": "N/A", "requestor": "—", "requested_via": "—", "state": "na",
            })
            continue
        if not runs:
            rows.append({
                "key": key, "label": label, "run": 1, "indexed": False, "started": "—", "ended": "—",
                "duration": "—", "requestor": "—", "requested_via": "—", "state": "pending",
            })
            continue
        indexed = len(runs) > 1
        for index, run in enumerate(runs, 1):
            seconds = duration_seconds(run)
            if run.get("endedAt"):
                state, duration = "done", fmt_duration(seconds)
            elif run.get("startedAt"):
                state, duration = "active", "In progress"
            else:
                state, duration = "pending", "—"
            rows.append({
                "key": key,
                "label": label,
                "run": index,
                "indexed": indexed,
                "started": run.get("startedAt") or "—",
                "ended": run.get("endedAt") or "—",
                "duration": duration,
                "requestor": run.get("requestor") or "—",
                "requested_via": run.get("requested_via") or "—",
                "state": state,
            })
    return rows


def step_stats(requests, audit_rows):
    by_id = {}
    for row in audit_rows or []:
        by_id.setdefault(row.get("requestId"), []).append(row)
    durations = {key: [] for key, _label in STEP_DEFS}
    for record in requests or []:
        resolved = resolved_steps(record, by_id.get(record.get("id"), []))
        for key, _label in STEP_DEFS:
            for run in resolved.get(key) or []:
                seconds = duration_seconds(run)
                if seconds is None:
                    continue
                durations[key].append(seconds)
    stats = []
    for key, label in STEP_DEFS:
        samples = durations[key]
        if not samples:
            stats.append({"key": key, "label": label, "count": 0, "mean": "—", "min": "—", "max": "—"})
            continue
        stats.append({
            "key": key,
            "label": label,
            "count": len(samples),
            "mean": fmt_duration(sum(samples) / len(samples)),
            "min": fmt_duration(min(samples)),
            "max": fmt_duration(max(samples)),
            "mean_s": sum(samples) / len(samples),
            "min_s": min(samples),
            "max_s": max(samples),
        })
    return stats


def intake_is_open(record):
    """True while the Vendor Intake form has not been submitted."""
    status = (record.get("status") or "").lower()
    tcd = record.get("touchesCustomerData")
    intake_done = tcd is not None or bool(record.get("intake")) or (status and "intake" not in status)
    return not intake_done


def _who(form, fallback):
    requestor = _clean(form.get("requestor")) or fallback
    requested_via = _clean(form.get("requested_via")) or "web"
    return requestor, requested_via


def _webhook_note(result):
    if result == "sent":
        return "Webhook bot notified."
    if result == "failed":
        return "Webhook bot could not be reached."
    return "No webhook bot is configured."


def intake_form_defaults(record, overlay=None):
    intake = record.get("intake") or {}
    hints = record.get("requestHints") or {}

    def pick(*values):
        for value in values:
            if value not in (None, ""):
                return value
        return ""

    tcd = intake.get("touches_customer_data", record.get("touchesCustomerData"))
    if tcd is True:
        tcd_val = "yes"
    elif tcd is False:
        tcd_val = "no"
    else:
        tcd_val = ""
    form = {
        "vendor_legal_name": pick(intake.get("vendor_legal_name"), record.get("vendorName")),
        "vendor_country": pick(intake.get("vendor_country"), hints.get("vendor_country")),
        "website": pick(intake.get("website"), record.get("vendorContact")),
        "touches_customer_data": tcd_val,
        "customer_data_details": pick(intake.get("customer_data_details")),
        "justification_for_access": pick(intake.get("justification_for_access")),
        "business_owner": pick(intake.get("business_owner"), hints.get("business_owner")),
        "target_start_date": pick(intake.get("target_start_date")),
        "purpose": pick(intake.get("purpose"), hints.get("purpose")),
        "pilot_length_days": pick(intake.get("pilot_length_days")),
        "requester": pick(intake.get("requester"), hints.get("requester")),
        "requestor": pick(hints.get("requester")),
        "requested_via": "web",
        "notes": "",
    }
    if overlay:
        for key in form:
            if key in overlay:
                form[key] = overlay.get(key) if overlay.get(key) is not None else ""
    return form


def render_detail(record, code=200, form=None):
    aud = merged_audit(record["id"])
    intake_open = intake_is_open(record)
    transitions = {k: v for k, v in TRANSITIONS.items() if not k.startswith("intake_")}
    body = render_template(
        "detail.html",
        r=record,
        audit=aud,
        transitions=transitions,
        flow=flow_states(record, aud),
        intake_rows=intake_rows(record),
        step_rows=step_rows_for(record, aud),
        intake_open=intake_open,
        intake_form=intake_form_defaults(record, form),
        view="dashboard",
    )
    return body if code == 200 else (body, code)


@app.route("/")
def dashboard():
    data = load()
    reqs = data["requests"]
    cards = dashboard_cards(reqs)
    actors = sorted({r.get("currentActor", "") for r in reqs if r.get("currentActor")})
    fs, fa, q = request.args.get("status", ""), request.args.get("actor", ""), request.args.get("q", "").strip()
    shown = [r for r in reqs
             if dashboard_matches(r, fs)
             and (not fa or r.get("currentActor") == fa)
             and (not q or q.lower() in (r.get("vendorName", "") + r.get("id", "")).lower())]
    shown = sorted(shown, key=lambda r: r.get("lastUpdated", ""), reverse=True)
    return render_template("dashboard.html", reqs=shown, total=len(reqs), cards=cards,
                           actors=actors, fs=fs, fa=fa, q=q, view="dashboard")


def _intake_payload(form):
    """Map the Vendor Intake form onto the stored intake object. Unknown yes/no is omitted."""
    payload = {}
    for key, _label in INTAKE_FIELDS:
        raw = _clean(form.get(key), 2000)
        if key == "touches_customer_data":
            flag = raw.lower()
            if flag in ("yes", "true", "y", "1"):
                payload[key] = True
            elif flag in ("no", "false", "n", "0"):
                payload[key] = False
            continue
        if key == "pilot_length_days" and raw.isdigit():
            payload[key] = int(raw)
            continue
        if raw:
            payload[key] = raw
    return payload


@app.route("/new", methods=["GET", "POST"])
def new_request():
    if request.method == "GET":
        return render_template("new.html", view="new", form={"requested_via": "web"})
    f = request.form
    name = _clean(f.get("vendorName"), 300)
    if not name:
        flash("Vendor legal name is required.", "error")
        return render_template("new.html", view="new", form=f), 400
    requestor, requested_via = _who(f, "LOB Requestor")
    with store_lock():
        data = load()
        rid = next_id(data)
        ts = now_utc()
        rec = {
            "id": rid, "createdAt": ts, "vendorName": name,
            "vendorContact": _clean(f.get("vendorContact"), 300),
            "requestedBy": f"{requestor} ({requested_via})",
            "currentActor": "Vendor Intake",
            "status": "With Vendor Intake", "lastUpdated": ts, "lastReminder": "",
            "manager": "Human - Manager",
            "notes": (_clean(f.get("notes"), 2000) or "Created via web app; routed to the Vendor Intake form."),
            "lei": "", "entityStatus": "", "registrationStatus": "",
            "touchesCustomerData": None, "researchComplete": False,
        }
        hints = {k: _clean(f.get(k)) for k in ("vendor_country", "business_owner", "purpose")}
        if requestor and requestor != "LOB Requestor":
            hints["requester"] = requestor
        hints = {k: v for k, v in hints.items() if v}
        if hints:
            rec["requestHints"] = hints
        close_step(rec, "lob", ts, requestor, requested_via)
        open_step(rec, "intake", ts)
        audit = {
            "timestamp": ts, "requestId": rid, "actor": f"{requestor} ({requested_via})",
            "action": "Created request", "fromStatus": "", "toStatus": "With Vendor Intake",
            "details": (
                f"Vendor: {name}; routed to Vendor Intake form. "
                f"requestor={requestor}; requested_via={requested_via}"
            ),
            "requestor": requestor, "requested_via": requested_via,
        }
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
        "requestor": requestor,
        "requested_via": requested_via,
        "step": "lob",
    })
    flash(f"Created {rid} and routed to the Vendor Intake form.", "ok")
    return redirect(url_for("detail", rid=rid))


@app.route("/request/<rid>")
def detail(rid):
    data = load()
    r = find(data, rid)
    if not r:
        abort(404)
    return render_detail(r)


@app.route("/request/<rid>/intake", methods=["POST"])
def submit_intake(rid):
    """Save the Vendor Intake form, complete that step, and notify the webhook bot."""
    f = request.form
    name = _clean(f.get("vendor_legal_name"), 300)
    touches = _intake_payload(f).get("touches_customer_data", None)
    if not name:
        flash("Vendor legal name is required.", "error")
        data = load()
        r = find(data, rid)
        if not r:
            abort(404)
        return render_detail(r, 400, f)
    if touches is None:
        flash("Say whether the vendor touches customer data.", "error")
        data = load()
        r = find(data, rid)
        if not r:
            abort(404)
        return render_detail(r, 400, f)
    requestor, requested_via = _who(f, "Vendor Intake")
    with store_lock():
        data = load()
        r = find(data, rid)
        if not r:
            abort(404)
        if not intake_is_open(r):
            flash(f"{rid}: Vendor Intake form was already submitted.", "error")
            return redirect(url_for("detail", rid=rid))
        ts = now_utc()
        frm = r.get("status", "")
        key = "intake_cd" if touches else "intake_no_cd"
        t = TRANSITIONS[key]
        intake = _intake_payload(f)
        intake["vendor_legal_name"] = name
        r["vendorName"] = name
        website = intake.get("website")
        if website:
            r["vendorContact"] = website
        r["intake"] = intake
        r["touchesCustomerData"] = touches
        r.update(currentActor=t["next_actor"], status=t["status"], lastUpdated=ts)
        note = _clean(f.get("notes"), 2000)
        r["notes"] = t["label"] + (f" {note}" if note else "")
        ensure_steps(r, audit_for(data, rid))
        run = close_step(r, "intake", ts, requestor, requested_via)
        open_step(r, "research", ts)
        if touches:
            open_step(r, "security", ts)
        else:
            cancel_open_step(r, "security")
        audit = {
            "timestamp": ts, "requestId": rid, "actor": f"{requestor} ({requested_via})",
            "action": t["label"], "fromStatus": frm, "toStatus": t["status"],
            "details": (
                f"{t['details']}. requestor={requestor}; requested_via={requested_via}"
                + (f". Note: {note}" if note else "")
            ),
            "requestor": requestor, "requested_via": requested_via,
        }
        data["auditLog"].append(audit)
        save(data, [audit])
        payload = {
            "event": "intake_completed",
            "requestId": rid,
            "vendorName": r.get("vendorName", ""),
            "fromStatus": frm,
            "toStatus": t["status"],
            "currentActor": r.get("currentActor", ""),
            "lastUpdated": ts,
            "notes": r.get("notes", ""),
            "action": t["label"],
            "step": "intake",
            "startedAt": (run or {}).get("startedAt", ""),
            "endedAt": ts,
            "requestor": requestor,
            "requested_via": requested_via,
            "touchesCustomerData": touches,
            "intake": intake,
        }
    result = push_status_change(payload)
    flash(f"{rid}: Vendor Intake form submitted. The Vendor Intake step is completed. {_webhook_note(result)}", "ok")
    return redirect(url_for("detail", rid=rid))


@app.route("/request/<rid>/action", methods=["POST"])
def action(rid):
    key = request.form.get("transition", "")
    note = _clean(request.form.get("note"), 2000)
    t = TRANSITIONS.get(key)
    if not t or key.startswith("intake_"):
        flash("Pick a workflow action. Submit the Vendor Intake form to complete intake.", "error")
        return redirect(url_for("detail", rid=rid))
    requestor, requested_via = _who(request.form, t["actor"])
    with store_lock():
        data = load()
        r = find(data, rid)
        if not r:
            abort(404)
        ts = now_utc()
        frm = r.get("status", "")
        r.update(currentActor=t["next_actor"], status=t["status"], lastUpdated=ts)
        if key == "research_done":
            r["researchComplete"] = True
        elif key == "security_approved":
            r["securityApproved"] = True
        elif key == "ops_approved":
            r["opsApproved"] = True
        r["notes"] = f"{t['label']}." + (f" {note}" if note else "")
        ensure_steps(r, audit_for(data, rid))
        record_step_transition(r, key, ts, requestor, requested_via)
        audit = {
            "timestamp": ts, "requestId": rid, "actor": f"{requestor} ({requested_via})",
            "action": t["label"], "fromStatus": frm, "toStatus": t["status"],
            "details": (
                f"{t['details']}. requestor={requestor}; requested_via={requested_via}"
                + (f". Note: {note}" if note else "")
            ),
            "requestor": requestor, "requested_via": requested_via,
        }
        data["auditLog"].append(audit)
        save(data, [audit])
        vendor = r.get("vendorName", "")
        notes = r.get("notes", "")
        actor = r.get("currentActor", "")
        to_status = t["status"]
        step_name = {
            "research_done": "research",
            "security_approved": "security",
            "ops_approved": "ops",
            "manager_approved": "manager",
            "manager_rejected": "manager",
        }.get(key, "")
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
        "step": step_name,
        "requestor": requestor,
        "requested_via": requested_via,
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
    data = load()
    return render_template(
        "workflow.html",
        view="workflow",
        flow=None,
        stats=step_stats(data.get("requests", []), merged_audit()),
    )


def mcp_endpoint():
    """Streamable HTTP URL for a bot on this machine."""
    import mcp_server
    return mcp_server.client_url()


def mcp_client_config():
    import mcp_server
    return json.dumps({
        "mcpServers": {
            "vendor-master": {
                "url": mcp_endpoint(),
                "headers": {"Authorization": "Bearer " + mcp_server.mcp_token()},
            }
        }
    }, indent=2)


MCP_TOOLS = (
    ("submit_onboarding_request", "Create the request and complete Vendor Intake together."),
    ("record_vendor_research", "Save the LEI check and complete Vendor Research."),
    ("record_ops_review", "Record Ops Review approval."),
    ("record_security_approval", "Record Security Approval when the vendor touches customer data."),
    ("record_manager_decision", "Record the manager approve or reject decision."),
    ("get_onboarding_request", "Read the full request, steps, and audit."),
    ("get_onboarding_status", "Read the current status and actor."),
    ("get_pending_requests", "List vendors waiting on Ops Review, Security Approval, or Manager Decision longer than an elapsed time."),
)


def _configuration_page(form=None, code=200):
    import mcp_server
    body = render_template(
        "configuration.html",
        view="configuration",
        webhooks=registered_webhooks(),
        form=form or {"name": "", "url": ""},
        mcp_config=mcp_client_config(),
        mcp_url=mcp_endpoint(),
        mcp_token=mcp_server.mcp_token(),
        mcp_tools=MCP_TOOLS,
    )
    return body if code == 200 else (body, code)


def _posted_webhook_form():
    return {
        "name": request.form.get("name", ""),
        "url": request.form.get("url", ""),
        "key": request.form.get("key", ""),
    }


@app.route("/configuration", methods=["GET", "POST"])
def configuration():
    if request.method == "GET":
        return _configuration_page()
    posted = _posted_webhook_form()
    if request.form.get("action") == "test":
        fields, err = _webhook_input(posted["name"], posted["url"], posted["key"])
        if err:
            flash(err, "error")
            return _configuration_page(posted, 400)
        ok, detail = deliver_webhook(fields["url"], fields["key"], webhook_test_payload())
        if ok:
            flash(f"Test webhook accepted (HTTP {detail}). It was not registered.", "ok")
            return _configuration_page(posted)
        flash(f"Test webhook failed ({detail}). It was not registered.", "error")
        return _configuration_page(posted, 400)
    _row, err = save_webhook(posted["name"], posted["url"], posted["key"])
    if err:
        flash(err, "error")
        return _configuration_page({"name": posted["name"], "url": posted["url"], "key": ""}, 400)
    flash("Webhook registered.", "ok")
    return redirect(url_for("configuration"))


@app.route("/configuration/webhooks/<webhook_id>/delete", methods=["POST"])
def delete_configuration_webhook(webhook_id):
    if delete_webhook(webhook_id):
        flash("Webhook removed.", "ok")
    else:
        flash("That webhook is not a saved registration.", "error")
    return redirect(url_for("configuration"))


@app.route("/healthz")
def healthz():
    return {"ok": True, "store": STORE_DIR, "requests": len(load()["requests"])}


def _start_mcp_http():
    """Run the streamable HTTP MCP server beside the web app."""
    import threading
    import mcp_server

    threading.Thread(target=mcp_server.serve, name="vendor-master-mcp", daemon=True).start()


if __name__ == "__main__":
    _start_mcp_http()
    app.run(host=os.environ.get("VO_HOST", "0.0.0.0"), port=int(os.environ.get("VO_PORT", "8787")))
