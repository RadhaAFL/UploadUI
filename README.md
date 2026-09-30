# Upload Portal

Config-driven file uploads into Microsoft Fabric Warehouse: an admin defines
a portal's expected column layout, target table, and upload-period rule once;
a user then logs in, picks that portal, and uploads a `.xlsx`/`.csv` file in
the exact format required. The backend validates the file against the
template before anything is written, then replaces the matching rows in the
target Fabric table.

Standalone React + Flask app — same auth (Azure AD) and data source
(Microsoft Fabric) as the "Arvind Analytics" app
(`C:\Users\7517978\Work\SEMANTIC-LAYER`), but purpose-built rather than
generic: it only ever does uploads, so it skips that app's multi-portal
download/export/KPI machinery entirely.

First portal: **Monthly Store Target Upload**, writing to
`prd.DIM_MNL_XSTORE_SALES_TARGET_MASTER`.

## Manual pre-reqs (not done by this codebase)

1. **Azure AD redirect URI** — add
   `https://automationafl.arvindfashions.com/uploadUI` as an allowed redirect
   URI (on the same App Registration SEMANTIC-LAYER uses, or a new one).
2. **VM deploy** — apply the Apache/systemd config below on the same Ubuntu
   VM that hosts SEMANTIC-LAYER, at a new path (see Deployment).

## Architecture

```text
Microsoft Fabric Warehouse
        |
        | ODBC Driver 18 / pyodbc
        v
Flask API — backend/app.py
        |
        | admins, portals, portal_access, audit_logs
        v
data/app.duckdb

React / Vite frontend
frontend/src
        |
        | /uploadportal-api/*
        v
Apache2 reverse proxy
        |
        v
https://automationafl.arvindfashions.com/uploadUI
```

DuckDB holds only app metadata: who is an admin, which upload templates
("portals") exist and their config, who may use which portal (and with what
brand restriction), and the audit log. No uploaded data is ever kept
locally — every upload is validated in memory and, once confirmed, written
straight to Fabric.

## Project structure

```text
Upload_Portal/
  data/
    app.duckdb                 # Runtime metadata DB, not committed

  backend/
    app.py                     # Flask API: auth, portal config, upload validation, Fabric writes
    requirements.txt

  frontend/
    src/
      AuthWrapper.jsx          # Microsoft login + access gate
      UploadPortalHome.jsx     # Portal (template) selector
      UploadPortal.jsx         # Upload flow: template, period, file picker, preview, confirm
      routing.js               # Minimal history.pushState routing (no router dependency)
      authConfig.js
      logger.js
    vite.config.js             # /uploadUI base, /uploadportal-api proxy in local dev
    package.json

  .env                         # Fabric credentials, not committed
  README.md
```

## Environment variables

Root `.env` is loaded by `backend/app.py`:

```env
FABRIC_DB_HOST=<fabric-host>
FABRIC_DB_PORT=1433
FABRIC_DB_NAME=<fabric-warehouse-name>
FABRIC_DB_USER=<fabric-user>
FABRIC_DB_PASS=<fabric-password>
```

Frontend (`frontend/.env`):

```env
VITE_AZURE_CLIENT_ID=<azure-app-client-id>
VITE_AZURE_TENANT_ID=<azure-tenant-id>
VITE_REDIRECT_PATH=/uploadUI
```

## Local development

### Backend

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Runs on `http://localhost:5003`. On first boot it creates `data/app.duckdb`,
seeds one bootstrap admin (`radhakishan.thakur@arvindfashions.com` — change
`BOOTSTRAP_ADMIN` in `app.py` if that should be someone else), and seeds the
Monthly Store Target Upload portal. Needs real Fabric `.env` credentials and
"ODBC Driver 18 for SQL Server" installed for anything past `/check-access`.

### Frontend

```powershell
cd frontend
npm install
npm run dev
```

Runs on `http://localhost:3000/uploadUI/`, proxying `/uploadportal-api/*` to
`http://localhost:5003`. The registered Azure AD SPA redirect URI is
`http://localhost:3000` — if you need a different local port, add it as an
extra redirect URI on the App Registration too.

### Build

```powershell
cd frontend
npm run build
```

Output: `frontend/dist`.

## Backend API

Base path in production: `/uploadportal-api`

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/check-access?email=` | `{allowed, is_admin}` |
| `GET` | `/my-portals?email=` | Upload templates visible to this user |
| `POST` | `/portals/<id>/access` | Admin: grant/update a user's access + brand restriction |
| `GET` | `/upload-template?portal_id=` | Expected columns + live-computed expected period |
| `POST` | `/upload/start` | multipart: `portal_id, email, file` — validates only, stages result under an `upload_id` |
| `POST` | `/upload/commit` | `{upload_id, email}` — deletes matching period+brand rows, bulk-inserts the staged rows |
| `POST` | `/logs` | Audit log write |

Note the `email` vs. `caller_email` split on admin endpoints that also
carry a target user's email: `caller_email` is always the authenticated
admin performing the action, `email` is whoever else is referenced.
Conflating the two was an actual bug caught during SEMANTIC-LAYER
development — `_require_admin()` here uses the same convention for the same
reason.

## Period-rule engine (config-driven, per portal)

Each portal's `config.period_rule` decides what "the current upload period"
means for that template — this is the answer to "if a user uploads on the
25th, is that this month's data or next month's?" and it is **not**
hardcoded per feature; it is read from the portal's own config so a future
portal can define a completely different cadence without touching
`_period_for_rule()`.

Currently one rule shape is implemented, `unit: "month"`:

```json
"period_rule": { "unit": "month", "cutover_day": 25 }
```

- Upload day `< cutover_day` → the expected period is the **current** month.
- Upload day `>= cutover_day` → the expected period rolls to **next** month.

`_period_for_rule()` returns `{key, label, start, end}` for that window.
`/upload/start` rejects the file (no data is staged) if any row's date
column falls outside `[start, end]` — the error message states both the
expected window and the date range actually found in the file.

To add a portal with a different cadence (weekly, no cutover, always current
month, etc.), extend `_period_for_rule()` with a new `unit` and give that
portal's seeded config the matching `period_rule` — no other code changes.

## Brand-restricted delete-then-insert

On commit, existing rows are deleted only for:

- the resolved period's date range (`config.date_column BETWEEN start AND end`), **and**
- the brands actually present in the uploaded file, intersected with the
  uploader's permitted values (`portal_access.restrict_values` for
  `config.restrict_col`) if they are restricted.

This is deliberate: several brand teams share one target table, so a
restricted upload must never be able to delete another brand's rows for the
same month, even by accident. An admin (or a user with no restriction
configured) can upload for any brand present in their file.

## Deployment (same VM as SEMANTIC-LAYER, new path)

Apache — add alongside the existing `/permissions-api`/`/downloadui` block:

```apache
# Upload Portal API
ProxyTimeout 900
ProxyPass        /uploadportal-api/ http://localhost:5003/ timeout=900 connectiontimeout=30 retry=0
ProxyPassReverse /uploadportal-api/ http://localhost:5003/

# Upload Portal UI
Alias /uploadUI /home/appuser/upload-portal/frontend/dist

<Directory /home/appuser/upload-portal/frontend/dist>
    Options FollowSymLinks
    AllowOverride None
    Require all granted

    RewriteEngine On
    RewriteBase /uploadUI
    RewriteCond %{REQUEST_FILENAME} !-f
    RewriteCond %{REQUEST_FILENAME} !-d
    RewriteRule ^ /uploadUI/index.html [L]

    <Files "index.html">
        Header set Cache-Control "no-cache, must-revalidate"
    </Files>
</Directory>

<Directory /home/appuser/upload-portal/frontend/dist/assets>
    Header set Cache-Control "public, max-age=31536000, immutable"
</Directory>
```

Requires `mod_headers` (`sudo a2enmod headers`).

systemd (`upload-portal-api.service`):

```ini
[Unit]
Description=Upload Portal API
After=network.target

[Service]
User=appuser
WorkingDirectory=/home/appuser/upload-portal/backend
ExecStart=/home/appuser/upload-portal/env/bin/gunicorn --workers 1 --threads 16 --timeout 1200 --bind 127.0.0.1:5003 app:app
Restart=always

[Install]
WantedBy=multi-user.target
```

One gunicorn worker — DuckDB opens `data/app.duckdb` as a single local file,
so only one process may hold it.

```bash
sudo systemctl daemon-reload
sudo systemctl restart upload-portal-api
sudo apachectl configtest
sudo systemctl reload apache2
```

## Files not to commit

```text
.env
data/
frontend/node_modules/
frontend/dist/
backend/.venv/
```
