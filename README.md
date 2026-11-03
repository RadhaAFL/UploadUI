# Upload Portal

Config-driven file uploads into Microsoft Fabric Warehouse, in three layers:

1. **Users upload.** A user picks a portal, downloads its sample template,
   and uploads a `.xlsx`/`.csv` in exactly that format. The page states what
   the upload will do to the table before they confirm.
2. **Admins define the rules.** In the Admin console (`/uploadUI/admin`) an
   admin creates a portal: target table, expected columns, load strategy,
   period rule, and who may use it. Portal-specific Python lives in
   `backend/portals/<portal-id>.py` and ships through git.
3. **Data lands in Fabric.** The backend validates the file in memory, then
   applies the portal's load strategy (replace period / truncate & load /
   append) and bulk-inserts the rows.

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
    app.py                     # Flask API: auth, portal admin, upload validation, Fabric writes
    load_strategies.py         # replace_period / truncate_load / append / upsert
    portal_config.py           # Validates admin-submitted portal configs (SQL identifiers!)
    portal_hooks.py            # Loads backend/portals/<id>.py hooks
    publisher.py               # Staged publish (stage -> verify -> one-transaction swap) + direct fallback
    portals/                   # Per-portal Python hooks, one file per portal id
      _example_hooks.py        # Template — never loaded for a real portal
    requirements.txt

  frontend/
    src/
      AuthWrapper.jsx          # Microsoft login + access gate
      UploadPortalHome.jsx     # Portal (template) selector
      UploadPortal.jsx         # Upload flow: template, sample download, period, file picker, preview, confirm
      AdminConsole.jsx         # Admin only: create/edit portals, load rules, access
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
| `GET` | `/upload-template?portal_id=` | Expected columns, load strategy + live-computed expected period |
| `GET` | `/portals/<id>/sample-template?format=xlsx\|csv` | Downloadable template (header row only; the worked example is on the xlsx Instructions sheet) |
| `GET` | `/admin/portals` | Admin: every portal, incl. inactive, with hook status |
| `POST` | `/admin/portals` | Admin: create a portal `{id, name, description, config}` |
| `PUT` | `/admin/portals/<id>` | Admin: update a portal (`is_active` re-enables it) |
| `DELETE` | `/admin/portals/<id>` | Admin: deactivate (soft delete) |
| `GET`/`DELETE` | `/admin/portals/<id>/access` | Admin: list / remove (`?email=`) a user's access |
| `GET` | `/admin/strategies` | Admin: available load strategies |
| `POST` | `/upload/start` | multipart: `portal_id, email, file` — validates only, stages result under an `upload_id` |
| `POST` | `/upload/commit` | `{upload_id, email}` — starts the publish as a background job, returns `202 {job_id}`. `409` if another upload to the portal is running (the validated file is kept) |
| `GET` | `/upload/status?job_id=&email=` | Progress / outcome of a job: `state` (`starting`/`staging`/`publishing`/`done`/`failed`), rows, `live_changed` (`no`/`yes`/`unknown`) |
| `GET`/`POST` | `/admin/portals/<id>/staging` | Admin: inspect Fabric for the portal's staging table + its DDL / create it |
| `POST` | `/logs` | Audit log write |

Note the `email` vs. `caller_email` split on admin endpoints that also
carry a target user's email: `caller_email` is always the authenticated
admin performing the action, `email` is whoever else is referenced.
Conflating the two was an actual bug caught during SEMANTIC-LAYER
development — `_require_admin()` here uses the same convention for the same
reason.

## Load strategies

Each portal's `config.load_strategy` decides how an upload is applied to its
table. The strategy runs at commit time, right before the bulk insert.

| Strategy | What it does | Typical use |
|---|---|---|
| `replace_period` | Deletes rows in the upload period (and the brands in the file), then inserts. Needs `period_rule` + `date_column`. | Monthly targets |
| `truncate_load` | `TRUNCATE TABLE`, then inserts. Blocked for users with a brand restriction, since it wipes every brand's rows. | Store master |
| `upsert` | Deletes the rows whose `key_columns` (e.g. `STORE_CODE` + `BRAND`) appear in the file, then inserts the file: matches are replaced, new keys are added, everything else is left alone. A file that repeats a key, or has a blank key value, is rejected. With a brand restriction, the restriction column must be one of the keys. | Store status masters |
| `append` | Inserts only. Never deletes. | Sales data |

Portals saved before strategies existed have no `load_strategy` and behave as
`replace_period`. The strategy used is recorded in the audit log and shown in
the commit result.

Validation happens fully in memory before anything is written. How the rows
then reach Fabric depends on whether the portal has a staging table — see
**Staged publishing** below. Without one, the delete/truncate and the insert
are separate autocommit statements, so a failure part-way leaves the live
table partly loaded (or, for `truncate_load`, empty).

## Staged publishing

Set `staging_table` on a portal (Admin console → Portals → *Safe loading*) and
an upload never changes the live table until it is complete:

1. **Accept.** `/upload/commit` returns at once and the load runs in a
   background thread; the page polls `/upload/status`. A closed tab or dropped
   browser connection doesn't affect the load, and re-opening the portal shows
   the running job again.
2. **Stage.** Rows are inserted into the staging table tagged `UPLOAD_ID` +
   `BATCH_NO`. A dropped Fabric connection is retried batch by batch — each
   retry first deletes that batch's rows, so a batch that half-arrived can't
   be doubled.
3. **Verify.** The staging row count must equal the file's row count.
4. **Swap — one transaction:** the strategy's delete (a plain `DELETE` even for
   `truncate_load`, so it rolls back with the rest), `INSERT … SELECT` from
   staging, delete the staging rows, `COMMIT`. If the connection dies *at*
   `COMMIT` the outcome is unknowable from the client, so the staging rows are
   deleted inside the same transaction: after reconnecting, "no staging rows
   left" proves it committed.

The result is that the live table holds the old data or the new data, never
half of each and never blank. On any failure the user is told whether the
live table changed (`live_changed`: `no` / `yes` / `unknown`).

**One upload per portal at a time.** A second user is rejected with a message
naming who is uploading and asking them to try again; their validated file is
kept, so pressing Confirm again once the portal is free works. The lock is
in-process, which is why the API must run as a **single gunicorn worker**.
A server restart mid-upload marks the job failed at start-up (and clears its
staging rows).

### Setting up a staging table

The app's Fabric login needs permission to `CREATE TABLE` (and `CREATE SCHEMA`
for a new schema), or an admin can run the DDL by hand. In the console:
Portals → pick the portal → set *Staging table* (e.g. `stg.DIM_STORE_MASTER`) →
**Save** → **Check in Fabric** (shows what exists and the exact DDL, built from
the live table's own column types) → **Create staging table**. A staging table
is the live table's loaded columns plus `UPLOAD_ID varchar(40)` and
`BATCH_NO int`; it must be a different table from the live one.

**Not yet exercised against a real Fabric warehouse.** The behaviour above is
tested against a stand-in warehouse (DuckDB) with injected failures; Fabric's
own transaction semantics (`BEGIN`/`COMMIT` around `DELETE` + `INSERT … SELECT`)
should be confirmed on a small non-production table before relying on it.

### Dates in text files

pandas guesses month-first, so a text date such as `01-08-2026` is read as
8 January, and nothing catches that on an `append` portal (no period check).
Set `date_format` (e.g. `%d-%m-%Y`) in the portal config when its files carry
day-first text dates. Real Excel date cells are unaffected.

## Portal Python hooks

For logic a built-in strategy can't express, add `backend/portals/<portal-id>.py`
(the file name must equal the portal id). It ships through git and is never
edited from the browser, so any change gets reviewed before it can run on the
server. Both functions are optional; see `backend/portals/_example_hooks.py`.

```python
def prepare(df, ctx):            # after parsing, before the built-in checks
    ...                          # return df, or raise ValueError("shown to the user")

def before_insert(cursor, ctx):  # at commit; replaces the strategy's delete/truncate
    ...                          # return how many existing rows were removed
```

The Admin console shows whether a portal's hook file exists. Hook files are
re-read when they change, but a hook that fails to import fails the upload
with a clear error rather than silently falling back.

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

## Brand-restricted delete-then-insert (`replace_period`)

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
so only one process may hold it, and the per-portal upload lock is in-process.
Uploads run on background threads inside that worker.

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
