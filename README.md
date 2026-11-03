# Vendor Onboarding

Local Flask web app for tracking vendor onboarding requests. It has no login. The dashboard, new-request form, request detail, status lookup, audit trail, and workflow pages all read and write an external tracker store.

The store is JSON plus CSV, not a database inside this repo. `requests.json` is the source of truth, `requests.csv` is rewritten from it, and `audit-log.csv` is appended. The default directory is outside the app and can be changed with `VO_STORE_DIR`.

## Run locally

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
./start.sh
```

Open http://127.0.0.1:8787 (health check: `/healthz`). Stop the app with `./stop.sh`.

`start.sh` runs in the background on `0.0.0.0:8787` by default and writes `app.pid` and `app.log`. Override the bind with `VO_PORT` and `VO_HOST`. If `.venv` is missing, `start.sh` creates it and installs `requirements.txt`.

## Pages

- `/` Dashboard: all requests, status, current actor, LEI, last updated; filter by status, actor, or search.
- `/new` New request: vendor legal name required; ID `VO-YYYYMMDD-NNN` (UTC); routed to Vendor Intake.
- `/request/<id>` Detail: intake, research, notes, record a workflow action, per-request audit trail.
- `/status` Status lookup by ID. `/audit` Full audit trail. `/workflow` Workflow diagram.

## Writes

Writes take an exclusive `flock` on `.store.lock`, then write a temp file and `os.replace` (with an in-place fallback). New requests get `requestedBy: "LOB Requestor (web)"`, `currentActor: "Vendor Intake"`, `status: "With Vendor Intake"`, `manager: "Human - Manager"`, and UTC timestamps `YYYY-MM-DD HH:MM:SS UTC`.

Optional status-change webhooks: copy `webhook.env.example` to `webhook.env` (gitignored) and set `VO_STATUS_WEBHOOK_URL` and `VO_STATUS_WEBHOOK_KEY`.
