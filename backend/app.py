"""
Upload Portal API — config-driven file uploads into Microsoft Fabric Warehouse.
Standalone app (own DuckDB for access control), modeled on the auth/Fabric
conventions proven in SEMANTIC-LAYER and Money Mapping, purpose-built rather
than generic since this app only ever does one thing: validate an uploaded
file against an admin-defined template + period rule, then replace matching
rows in the target Fabric table.
Runs on port 5003, proxied by Apache at /uploadportal-api/
"""
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
import io
import json
import math
import os
import pandas as pd
import pyodbc
import re
import threading
import time
import traceback
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

import duckdb

import load_strategies
import portal_config
import portal_hooks
import publisher

app = Flask(__name__)
CORS(app)

# ── Local data paths ───────────────────────────────────────────────────────
DATA_ROOT = os.path.join(os.path.dirname(__file__), '..', 'data')
DB_PATH   = os.path.join(DATA_ROOT, 'app.duckdb')
os.makedirs(DATA_ROOT, exist_ok=True)  # data/ is gitignored — won't exist on a fresh clone

# Single persistent DuckDB connection + lock — same convention as
# SEMANTIC-LAYER's get_con()/_db_lock and Money Mapping's app.py
# (DuckDB files are single-writer).
_db_lock = threading.Lock()
_db_con: "duckdb.DuckDBPyConnection | None" = None


def get_con() -> "duckdb.DuckDBPyConnection":
    global _db_con
    if _db_con is None:
        _db_con = duckdb.connect(DB_PATH)
    return _db_con


# In-memory staging for validated uploads: upload_id -> parsed rows ready
# for commit. Mirrors SEMANTIC-LAYER's export-job pattern (no durable job
# store yet — acceptable for a low-traffic internal tool).
_upload_lock = threading.Lock()
_upload_staging = {}
UPLOAD_SESSION_TTL_SECONDS = 30 * 60


# ── Microsoft Fabric Warehouse (SQL Server / ODBC) ────────────────────────
load_dotenv(Path(__file__).resolve().parent.parent / '.env')

_FAB_HOST = os.environ.get('FABRIC_DB_HOST', '')
_FAB_PORT = int(os.environ.get('FABRIC_DB_PORT', 1433))
_FAB_DB   = os.environ.get('FABRIC_DB_NAME', '')
_FAB_USER = os.environ.get('FABRIC_DB_USER', '')
_FAB_PASS = os.environ.get('FABRIC_DB_PASS', '')


def _fab_conn():
    """Open a new ODBC connection to Microsoft Fabric. One connection per
    request, closed in a finally block by each caller."""
    return pyodbc.connect(
        f"Driver={{ODBC Driver 18 for SQL Server}};"
        f"Server={_FAB_HOST},{_FAB_PORT};Database={_FAB_DB};"
        "Authentication=ActiveDirectoryPassword;"
        f"UID={_FAB_USER};PWD={_FAB_PASS};"
        "Encrypt=yes;TrustServerCertificate=no;",
        timeout=120,
    )


BOOTSTRAP_ADMIN = "radhakishan.thakur@arvindfashions.com"


# ── Monthly Store Target Upload portal config (seed) ──────────────────────
# Structure/columns match the admin-provided template (e.g. Oct_2026.xlsx).
# period_rule: uploads on/after the 25th of the month belong to next month's
# target period; before the 25th they belong to the current month. Each
# portal can define its own cadence/cutover — this is not hardcoded.
MONTHLY_STORE_TARGET_UPLOAD_CONFIG = {
    "target_table": "prd.DIM_MNL_XSTORE_SALES_TARGET_MASTER",
    "load_strategy": "replace_period",
    "date_column": "BUSINESS_DATE",
    "restrict_col": "BRAND",
    "period_rule": {"unit": "month", "cutover_day": 25},
    "columns": [
        {"key": "STORE_CODE",    "label": "Store Code",    "numeric": False},
        {"key": "BRAND",         "label": "Brand",         "numeric": False},
        {"key": "PARTNER",       "label": "Partner",       "numeric": False},
        {"key": "ORDER_TYPE",    "label": "Order Type",    "numeric": False},
        {"key": "TARGET_AMOUNT", "label": "Target Amount", "numeric": True},
        {"key": "MRP",           "label": "MRP",           "numeric": True},
        {"key": "DISCOUNT",      "label": "Discount",      "numeric": True},
        {"key": "TAX",           "label": "Tax",           "numeric": True},
        {"key": "COGS",          "label": "COGS",          "numeric": True},
        {"key": "BUSINESS_DATE", "label": "Business Date", "numeric": False},
    ],
    "audit_columns": {
        "file_name":     "FILE_NAME",
        "run_date":      "RUN_DATE",
        "load_run_date": "LOAD_RUN_DATE",
    },
}


def init_db():
    con = get_con()
    con.execute("CREATE TABLE IF NOT EXISTS admins (email VARCHAR PRIMARY KEY)")
    con.execute("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            id      INTEGER,
            ts      TIMESTAMP,
            email   VARCHAR,
            name    VARCHAR,
            action  VARCHAR,
            details VARCHAR
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS portals (
            id          VARCHAR PRIMARY KEY,
            name        VARCHAR,
            description VARCHAR,
            config      VARCHAR,
            created_at  TIMESTAMP,
            is_active   BOOLEAN DEFAULT TRUE
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS portal_access (
            portal_id        VARCHAR,
            email            VARCHAR,
            restrict_values  VARCHAR,
            PRIMARY KEY (portal_id, email)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS upload_jobs (
            id            VARCHAR PRIMARY KEY,
            portal_id     VARCHAR,
            email         VARCHAR,
            filename      VARCHAR,
            mode          VARCHAR,
            state         VARCHAR,
            rows_total    INTEGER,
            rows_done     INTEGER,
            deleted       INTEGER,
            inserted      INTEGER,
            verified      INTEGER,
            message       VARCHAR,
            live_changed  VARCHAR,
            period_label  VARCHAR,
            brands        VARCHAR,
            started_at    TIMESTAMP,
            finished_at   TIMESTAMP
        )
    """)

    if con.execute("SELECT COUNT(*) FROM admins").fetchone()[0] == 0:
        con.execute("INSERT INTO admins VALUES (?)", [BOOTSTRAP_ADMIN])

    if con.execute("SELECT COUNT(*) FROM portals WHERE id='monthly-store-target-upload'").fetchone()[0] == 0:
        now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        con.execute(
            "INSERT INTO portals VALUES (?,?,?,?,?,?)",
            [
                'monthly-store-target-upload',
                'Monthly Store Target Upload',
                'Upload monthly store-level sales targets in the standard template',
                json.dumps(MONTHLY_STORE_TARGET_UPLOAD_CONFIG),
                now,
                True,
            ]
        )
        # Bootstrap admin gets unrestricted access to the seeded portal so
        # local testing works immediately after first boot.
        con.execute(
            "INSERT OR IGNORE INTO portal_access VALUES (?,?,?)",
            ['monthly-store-target-upload', BOOTSTRAP_ADMIN, json.dumps([])]
        )


def _is_admin(email: str) -> bool:
    email = (email or '').strip().lower()
    if not email:
        return False
    with _db_lock:
        row = get_con().execute(
            "SELECT 1 FROM admins WHERE LOWER(email)=?", [email]
        ).fetchone()
    return row is not None


def _require_admin():
    """Reads 'caller_email' rather than 'email' — some admin endpoints also
    carry a target user's 'email' in the same request, and conflating the
    two would let the admin-check accidentally run against the target."""
    if request.method in ('POST', 'PUT', 'PATCH'):
        body = request.get_json(silent=True) or {}
        email = body.get('caller_email', '') or request.args.get('caller_email', '')
    else:
        email = request.args.get('caller_email', '')
    if not _is_admin(email):
        return jsonify({"error": "Admin access required"}), 403
    return None


def _valid_view_name(view: str) -> bool:
    """Allow only schema.TableName format to prevent SQL injection."""
    return bool(re.match(r'^[a-zA-Z0-9_]+\.[a-zA-Z0-9_]+$', (view or '').strip()))


def _load_portal(portal_id: str) -> dict:
    with _db_lock:
        row = get_con().execute(
            "SELECT id, name, description, config FROM portals WHERE id=? AND is_active=TRUE",
            [portal_id]
        ).fetchone()
    if not row:
        raise ValueError(f"Portal not found: {portal_id}")
    return {"id": row[0], "name": row[1], "description": row[2], "config": json.loads(row[3])}


def _load_user_restrict_values(portal_id: str, email: str, restrict_col: str):
    """Return the caller's permitted values for restrict_col on this portal.
    None means unrestricted (admin, or no restriction configured for this
    user). Raises ValueError if the user has no access row for this portal.
    """
    if _is_admin(email):
        return None
    with _db_lock:
        pa = get_con().execute(
            "SELECT restrict_values FROM portal_access WHERE portal_id=? AND LOWER(email)=?",
            [portal_id, email]
        ).fetchone()
    if not pa:
        raise ValueError("You do not have access to this portal.")
    raw = json.loads(pa[0]) if pa[0] else []
    if isinstance(raw, dict):
        values = raw.get(restrict_col) or raw.get(str(restrict_col).upper()) or []
    else:
        values = raw or []
    return values or None


# ── Period-rule engine (config-driven, per portal) ────────────────────────

def _period_for_rule(period_rule: "dict | None", today=None) -> "dict | None":
    """Derive the expected upload period from a portal's period_rule config.
    Currently supports unit='month': uploads before cutover_day belong to
    the current month, uploads on/after it roll to next month.
    """
    if not period_rule:
        return None
    today = today or date.today()
    unit = period_rule.get('unit', 'month')
    if unit == 'month':
        cutover = int(period_rule.get('cutover_day', 1) or 1)
        year, month = today.year, today.month
        if today.day >= cutover:
            month += 1
            if month > 12:
                month = 1
                year += 1
        start = date(year, month, 1)
        end = date(year + 1, 1, 1) - timedelta(days=1) if month == 12 \
            else date(year, month + 1, 1) - timedelta(days=1)
        return {
            "unit": "month",
            "cutover_day": cutover,
            "key": start.strftime('%Y_%m'),
            "label": start.strftime('%B %Y'),
            "start": start.isoformat(),
            "end": end.isoformat(),
        }
    raise ValueError(f"Unsupported period unit: {unit}")


# ── File parsing + validation ─────────────────────────────────────────────

def _read_upload_file(file_storage):
    """Parse an uploaded .xlsx/.xlsm/.csv into a DataFrame with upper-cased headers."""
    filename = (file_storage.filename or '').strip()
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if ext in ('xlsx', 'xlsm'):
        df = pd.read_excel(file_storage, dtype=object, engine='openpyxl')
    elif ext == 'csv':
        df = pd.read_csv(file_storage, dtype=object, keep_default_na=False, na_values=[''])
    else:
        raise ValueError("Unsupported file type. Upload a .xlsx or .csv file matching the portal template.")
    df.columns = [str(c).strip().upper() for c in df.columns]
    return df, filename


def _cell_to_str(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ''
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else str(v)
    return str(v).strip()


def _target_float(val):
    if val is None:
        return None
    text = str(val).strip()
    if not text:
        return None
    text = text.replace(',', '').replace('%', '')
    try:
        return float(text)
    except ValueError:
        return None


# SQL Server's float holds 0 and magnitudes from ~2.23e-308 to ~1.80e+308.
# NaN, +/-Infinity and anything smaller than that are rejected with error 8023.
_SQL_FLOAT_MIN = 2.2250738585072014e-308


def _sql_float_or_none(v):
    """A clean float SQL Server will accept, else None (stored as NULL)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    if math.isnan(f) or math.isinf(f):
        return None
    if f != 0.0 and abs(f) < _SQL_FLOAT_MIN:
        return None
    return f


def _upload_numeric(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    return _target_float(v)


def _build_upload_rows(df, config: dict, restrict_values):
    """Validate an uploaded DataFrame against a portal's config and convert
    it into typed rows ready for insert. Raises ValueError on any validation
    failure (missing columns, bad dates, out-of-period rows, disallowed
    restricted values) — nothing is staged if this raises.
    """
    columns_config = config.get('columns', [])
    keys = [c['key'] for c in columns_config]
    missing = [k for k in keys if k not in df.columns]
    if missing:
        raise ValueError(f"Missing required column(s): {', '.join(missing)}")

    work = df[keys].copy()
    date_col = config.get('date_column')
    restrict_col = config.get('restrict_col')
    numeric_keys = {c['key'] for c in columns_config if c.get('numeric')}

    bad_dates = 0
    if date_col:
        # Without an explicit format pandas guesses month-first, so a text
        # date like 01-08-2026 silently becomes 8 January. A portal whose
        # files carry day-first text dates sets `date_format` (e.g. %d-%m-%Y);
        # real Excel date cells parse correctly either way.
        date_format = config.get('date_format') or None
        parsed = pd.to_datetime(
            work[date_col], format=date_format, errors='coerce',
            dayfirst=bool(date_format and date_format.lstrip().startswith('%d')),  # silences pandas' dayfirst warning
        )
        bad_dates = int(parsed.isna().sum())
        work[date_col] = parsed.dt.date
    if bad_dates:
        raise ValueError(
            f"{bad_dates} row(s) have a missing or unreadable {date_col} value. Fix the file and re-upload."
        )

    null_counts = {}
    for key in keys:
        if key == date_col:
            continue
        if key in numeric_keys:
            converted = work[key].apply(_upload_numeric)
            # Built by hand as an object column on purpose: Series.apply/map
            # turn None into NaN as soon as the column also holds floats, and
            # a NaN that reaches the driver fails the whole insert batch with
            # "not a valid instance of data type float" (SQL Server error
            # 8023). Every blank or unusable value must arrive as None (NULL).
            cleaned, forced = [], []
            for v in converted.tolist():
                ok = _sql_float_or_none(v)
                if ok is None and v is not None and not (isinstance(v, float) and math.isnan(v)):
                    forced.append(v)   # real data the warehouse can't hold; a plain blank is expected
                cleaned.append(ok)
            work[key] = pd.Series(cleaned, index=work.index, dtype=object)
            if forced:
                print(f"Upload validate: {len(forced)} {key} value(s) can't be stored as a SQL Server "
                      f"float; loading as NULL. Examples: {forced[:3]}", flush=True)
            null_counts[key] = int(work[key].isna().sum())
        else:
            work[key] = work[key].apply(_cell_to_str)

    period = _period_for_rule(config.get('period_rule'))
    if period and date_col:
        start = date.fromisoformat(period['start'])
        end = date.fromisoformat(period['end'])
        out_of_range = work[(work[date_col] < start) | (work[date_col] > end)]
        if len(out_of_range):
            raise ValueError(
                f"{len(out_of_range)} row(s) fall outside the expected upload period "
                f"({period['label']}: {period['start']} to {period['end']}). "
                f"File contains dates from {work[date_col].min()} to {work[date_col].max()}."
            )

    brands_in_file = []
    if restrict_col and restrict_col in work.columns:
        brands_in_file = sorted({v for v in work[restrict_col].tolist() if v})
        if restrict_values is not None:
            disallowed = sorted(set(brands_in_file) - set(restrict_values))
            if disallowed:
                raise ValueError(
                    f"File contains {restrict_col} value(s) you are not permitted to upload: {', '.join(disallowed)}"
                )

    rows = work[keys].values.tolist()
    preview = [
        {k: (v.isoformat() if isinstance(v, date) else v) for k, v in zip(keys, r)}
        for r in rows[:10]
    ]

    return {
        "rows": rows,
        "row_count": len(rows),
        "brands_in_file": brands_in_file,
        "null_counts": null_counts,
        "preview": preview,
        "period": period,
    }


def _upload_insert_columns(config: dict):
    cols = [c['key'] for c in config.get('columns', [])]
    audit = config.get('audit_columns') or {}
    audit_cols = [audit[k] for k in ('file_name', 'run_date', 'load_run_date') if audit.get(k)]
    return cols + audit_cols, cols, audit_cols


# ── Upload jobs: the publish to Fabric runs in the background ──────────────
# The browser only starts a job and polls it, so a closed tab or a dropped
# connection to the browser can't interrupt a load half-way. One job per
# portal at a time (this app runs a single gunicorn worker — see README — so
# an in-process lock is enough).

_busy_lock = threading.Lock()
_portal_busy: dict = {}          # portal_id -> {"job_id", "email", "since"}

_JOB_FIELDS = ("state", "rows_total", "rows_done", "deleted", "inserted", "verified",
               "message", "live_changed", "finished_at")
_JOB_SELECT = ("id, portal_id, email, filename, mode, state, rows_total, rows_done, deleted, inserted, "
               "verified, message, live_changed, period_label, brands, started_at, finished_at")


def _job_dict(r) -> dict:
    return {
        "job_id": r[0], "portal_id": r[1], "email": r[2], "filename": r[3], "mode": r[4], "state": r[5],
        "rows_total": r[6], "rows_done": r[7], "deleted": r[8], "inserted": r[9], "verified": r[10],
        "message": r[11], "live_changed": r[12], "period_label": r[13],
        "brands": json.loads(r[14]) if r[14] else [],
        "started_at": str(r[15]) if r[15] else None, "finished_at": str(r[16]) if r[16] else None,
        "finished": r[5] in ('done', 'failed'),
    }


def _update_job(job_id: str, **fields):
    bad = set(fields) - set(_JOB_FIELDS)
    if bad:
        raise ValueError(f"unknown job field(s): {sorted(bad)}")
    sets = ', '.join(f"{k}=?" for k in fields)
    with _db_lock:
        get_con().execute(f"UPDATE upload_jobs SET {sets} WHERE id=?", [*fields.values(), job_id])


def _get_job(job_id: str):
    with _db_lock:
        row = get_con().execute(f"SELECT {_JOB_SELECT} FROM upload_jobs WHERE id=?", [job_id]).fetchone()
    return _job_dict(row) if row else None


def _utcnow_str() -> str:
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')


def _try_acquire_portal(portal_id: str, job_id: str, email: str):
    """Claim the portal for a job. Returns None if claimed, else the holder."""
    with _busy_lock:
        holder = _portal_busy.get(portal_id)
        if holder:
            return dict(holder)
        _portal_busy[portal_id] = {"job_id": job_id, "email": email, "since": time.time()}
        return None


def _release_portal(portal_id: str, job_id: str):
    with _busy_lock:
        if (_portal_busy.get(portal_id) or {}).get("job_id") == job_id:
            _portal_busy.pop(portal_id, None)


def _busy_view(portal_id: str, email: str = ''):
    """Who holds the portal right now, shaped for the UI; None if free."""
    with _busy_lock:
        holder = _portal_busy.get(portal_id)
        holder = dict(holder) if holder else None
    if not holder:
        return None
    minutes = max(0, int((time.time() - holder["since"]) // 60))
    mine = bool(email) and holder["email"] == email.strip().lower()
    ago = "less than a minute ago" if minutes < 1 else f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    message = (f"You already have an upload running for this portal (started {ago})." if mine else
               f"{holder['email']} is uploading to this portal right now (started {ago}). "
               "Please try again in a few minutes.")
    return {"job_id": holder["job_id"], "by": holder["email"], "mine": mine, "minutes": minutes, "message": message}


def _run_upload_job(job_id: str, staged: dict):
    """Thread body: publish one validated upload and record what happened."""
    portal_id, email = staged['portal_id'], staged['email']
    total = len(staged['rows'])
    last_write = [0.0]

    def progress(done, _total):
        now = time.time()
        if done >= _total or now - last_write[0] >= 0.5:   # don't write the job row on every batch
            last_write[0] = now
            _update_job(job_id, rows_done=int(done))

    def set_state(state):
        _update_job(job_id, state=state, rows_done=total if state == 'publishing' and staged['config'].get('staging_table') else 0)

    try:
        result = publisher.publish(_fab_conn, staged, progress=progress, set_state=set_state)
        verified = publisher.verify_loaded(_fab_conn, staged)
        _update_job(job_id, state='done', rows_done=total, deleted=int(result['deleted'] or 0),
                    inserted=int(result['inserted']), verified=verified, live_changed='yes', finished_at=_utcnow_str())
        _audit(email, 'upload_commit', {
            "portal_id": portal_id, "strategy": staged['load_strategy'], "staged": result['staged'],
            "deleted": int(result['deleted'] or 0), "inserted": int(result['inserted'])})
    except publisher.PublishError as e:
        live = {False: 'no', True: 'yes', None: 'unknown'}[e.live_changed]
        _update_job(job_id, state='failed', message=e.message, live_changed=live, finished_at=_utcnow_str())
        _audit(email, 'upload_failed', {"portal_id": portal_id, "live_changed": live, "error": e.message})
    except Exception as e:
        print("Upload job failed unexpectedly:", traceback.format_exc(), flush=True)
        _update_job(job_id, state='failed', message=f"Unexpected error: {e}", live_changed='unknown',
                    finished_at=_utcnow_str())
        _audit(email, 'upload_failed', {"portal_id": portal_id, "live_changed": "unknown", "error": str(e)})
    finally:
        _release_portal(portal_id, job_id)


def _sweep_interrupted_jobs():
    """At start-up: a job still 'running' belongs to a process that died. Mark
    it failed, saying what state the live table is in, and clear its staging rows."""
    with _db_lock:
        con = get_con()
        orphans = con.execute(
            "SELECT id, portal_id, state FROM upload_jobs WHERE state NOT IN ('done','failed')").fetchall()
        for job_id, _pid, state in orphans:
            if state == 'staging':
                msg, live = ("The server restarted while this upload was loading. The live table was not changed — "
                             "please upload the file again."), 'no'
            else:
                msg, live = ("The server restarted while this upload was being applied. Check the table before "
                             "uploading again."), 'unknown'
            con.execute("UPDATE upload_jobs SET state='failed', message=?, live_changed=?, finished_at=? WHERE id=?",
                        [msg, live, _utcnow_str(), job_id])
    staging_orphans = []
    for job_id, portal_id, state in orphans:
        if state != 'staging':
            continue
        with _db_lock:
            row = get_con().execute("SELECT config FROM portals WHERE id=?", [portal_id]).fetchone()
        table = (json.loads(row[0]).get('staging_table') if row else None)
        if table:
            staging_orphans.append((table, job_id))
    if staging_orphans and _FAB_HOST:
        def clean():
            for table, job_id in staging_orphans:
                publisher.discard_staged(_fab_conn, table, job_id)
        threading.Thread(target=clean, daemon=True, name="staging-cleanup").start()
    return len(orphans)


def _cleanup_expired_uploads():
    now = time.time()
    with _upload_lock:
        expired = [k for k, v in _upload_staging.items() if now - v.get('created_at', 0) > UPLOAD_SESSION_TTL_SECONDS]
        for k in expired:
            _upload_staging.pop(k, None)


# ── Auth / portal-list endpoints ───────────────────────────────────────────

@app.route('/check-access', methods=['GET'])
def check_access():
    try:
        email = request.args.get('email', '').strip().lower()
        if not email:
            return jsonify({"allowed": False}), 400
        if _is_admin(email):
            return jsonify({"allowed": True, "is_admin": True})
        with _db_lock:
            hit = get_con().execute("""
                SELECT 1 FROM portal_access pa
                JOIN portals p ON p.id = pa.portal_id
                WHERE p.is_active=TRUE AND LOWER(pa.email)=?
                LIMIT 1
            """, [email]).fetchone()
        return jsonify({"allowed": bool(hit), "is_admin": False})
    except Exception as e:
        return jsonify({"error": str(e), "allowed": False}), 500


@app.route('/my-portals', methods=['GET'])
def my_portals():
    try:
        email = request.args.get('email', '').strip().lower()
        if not email:
            return jsonify({"error": "email required"}), 400
        is_admin = _is_admin(email)  # acquires/releases _db_lock on its own — must not nest it below
        with _db_lock:
            con = get_con()
            if is_admin:
                rows = con.execute(
                    "SELECT id, name, description, config FROM portals WHERE is_active=TRUE ORDER BY created_at"
                ).fetchall()
                portals = [{
                    "id": r[0], "name": r[1], "description": r[2], "config": json.loads(r[3]),
                    "restrict_values": [], "is_admin": True,
                } for r in rows]
            else:
                rows = con.execute("""
                    SELECT p.id, p.name, p.description, p.config, pa.restrict_values
                    FROM portals p
                    JOIN portal_access pa ON pa.portal_id = p.id
                    WHERE p.is_active=TRUE AND LOWER(pa.email)=?
                    ORDER BY p.created_at
                """, [email]).fetchall()
                portals = [{
                    "id": r[0], "name": r[1], "description": r[2], "config": json.loads(r[3]),
                    "restrict_values": json.loads(r[4]) if r[4] else [], "is_admin": False,
                } for r in rows]
        return jsonify({"portals": portals})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/portals/<portal_id>/access', methods=['POST'])
def set_portal_access(portal_id):
    """Admin: grant/update access to a portal. Takes `email` or a list in
    `emails` (the console's "comma-separated for multiple"); every address
    gets the same restrict_values. Re-granting an existing user updates them."""
    err = _require_admin()
    if err: return err
    try:
        body = request.get_json() or {}
        raw = body.get('emails') if isinstance(body.get('emails'), list) else [body.get('email', '')]
        emails = list(dict.fromkeys(str(e).strip().lower() for e in raw if str(e).strip()))
        values = [str(v).strip() for v in (body.get('restrict_values') or []) if str(v).strip()]
        if not emails:
            return jsonify({"error": "email required"}), 400
        invalid = [e for e in emails if not _EMAIL_RE.match(e) or len(e) > 254]
        if invalid:
            return jsonify({"error": f"Not a valid email address: {', '.join(invalid[:5])}"}), 400
        with _db_lock:
            con = get_con()
            if not con.execute("SELECT 1 FROM portals WHERE id=?", [portal_id]).fetchone():
                return jsonify({"error": f"Portal not found: {portal_id}"}), 404
            for email in emails:
                con.execute("DELETE FROM portal_access WHERE portal_id=? AND LOWER(email)=?", [portal_id, email])
                con.execute("INSERT INTO portal_access VALUES (?,?,?)", [portal_id, email, json.dumps(values)])
        _audit(str(body.get('caller_email', '')).strip().lower(), 'portal_access_set',
               {"portal_id": portal_id, "emails": emails, "restrict_values": values})
        return jsonify({"status": "ok", "count": len(emails)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Admin: portal definitions (Phase 2) ────────────────────────────────────

def _audit(email: str, action: str, details: dict):
    with _db_lock:
        con = get_con()
        next_id = con.execute("SELECT COALESCE(MAX(id),0)+1 FROM audit_logs").fetchone()[0]
        con.execute("INSERT INTO audit_logs VALUES (?,?,?,?,?,?)", [
            next_id, datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), email, '', action, json.dumps(details),
        ])


def _portal_admin_view(row, access_count: int) -> dict:
    portal_id = row[0]
    config = json.loads(row[3])
    return {
        "id": portal_id, "name": row[1], "description": row[2], "config": config,
        "created_at": str(row[4]), "is_active": bool(row[5]),
        "load_strategy": load_strategies.strategy_of(config),
        "staged": bool(config.get('staging_table')),
        "access_count": access_count,
        "hook_file": f"backend/portals/{portal_id}.py",
        "has_hooks": portal_hooks.has_hooks(portal_id),
    }


_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


@app.route('/admin/admins', methods=['GET'])
def admin_list_admins():
    err = _require_admin()
    if err: return err
    try:
        with _db_lock:
            rows = get_con().execute("SELECT LOWER(email) FROM admins ORDER BY LOWER(email)").fetchall()
        return jsonify({"admins": [r[0] for r in rows]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/admin/admins', methods=['POST'])
def admin_add_admin():
    err = _require_admin()
    if err: return err
    try:
        body = request.get_json() or {}
        caller = str(body.get('caller_email', '')).strip().lower()
        email = str(body.get('email', '')).strip().lower()
        if not _EMAIL_RE.match(email) or len(email) > 254:
            return jsonify({"error": "Enter a valid email address."}), 400
        with _db_lock:
            con = get_con()
            if con.execute("SELECT 1 FROM admins WHERE LOWER(email)=?", [email]).fetchone():
                return jsonify({"error": f"{email} is already an administrator."}), 409
            con.execute("INSERT INTO admins VALUES (?)", [email])
        _audit(caller, 'admin_add', {"email": email})
        return jsonify({"status": "ok", "email": email}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/admin/admins', methods=['DELETE'])
def admin_remove_admin():
    """An admin cannot remove themselves: with removal limited to *other*
    admins, at least one admin (the caller) always remains, so the portal
    can never be locked out."""
    err = _require_admin()
    if err: return err
    try:
        caller = request.args.get('caller_email', '').strip().lower()
        email = request.args.get('email', '').strip().lower()
        if not email:
            return jsonify({"error": "email required"}), 400
        if email == caller:
            return jsonify({"error": "You can't remove your own admin access. Ask another administrator to do it."}), 400
        with _db_lock:
            con = get_con()
            if not con.execute("SELECT 1 FROM admins WHERE LOWER(email)=?", [email]).fetchone():
                return jsonify({"error": f"{email} is not an administrator."}), 404
            con.execute("DELETE FROM admins WHERE LOWER(email)=?", [email])
        _audit(caller, 'admin_remove', {"email": email})
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/admin/logs', methods=['GET'])
def admin_logs():
    """Newest-first audit trail for the Activity Log tab. Filters: `action`
    (exact), `q` (substring of the user's email or the details), `limit`."""
    err = _require_admin()
    if err: return err
    try:
        action = request.args.get('action', '').strip()
        q = request.args.get('q', '').strip().lower()
        try:
            limit = max(1, min(int(request.args.get('limit', 200)), 1000))
        except ValueError:
            limit = 200
        like = f"%{q}%"
        with _db_lock:
            con = get_con()
            rows = con.execute("""
                SELECT id, ts, email, name, action, details FROM audit_logs
                WHERE (? = '' OR action = ?)
                  AND (? = '' OR LOWER(email) LIKE ? OR LOWER(details) LIKE ?)
                ORDER BY id DESC LIMIT ?
            """, [action, action, q, like, like, limit]).fetchall()
            actions = [r[0] for r in con.execute("SELECT DISTINCT action FROM audit_logs ORDER BY action").fetchall()]
        return jsonify({
            "actions": actions,
            "logs": [{"id": r[0], "ts": str(r[1]), "email": r[2], "name": r[3], "action": r[4], "details": r[5]} for r in rows],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/admin/strategies', methods=['GET'])
def admin_strategies():
    err = _require_admin()
    if err: return err
    return jsonify({"strategies": [
        {"id": k, "label": v["label"], "summary": v["summary"], "needs_period": v["needs_period"]}
        for k, v in load_strategies.STRATEGIES.items()
    ]})


@app.route('/admin/portals', methods=['GET'])
def admin_list_portals():
    err = _require_admin()
    if err: return err
    try:
        with _db_lock:
            con = get_con()
            rows = con.execute(
                "SELECT id, name, description, config, created_at, is_active FROM portals ORDER BY created_at"
            ).fetchall()
            counts = dict(con.execute("SELECT portal_id, COUNT(*) FROM portal_access GROUP BY portal_id").fetchall())
        return jsonify({"portals": [_portal_admin_view(r, counts.get(r[0], 0)) for r in rows]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/admin/portals', methods=['POST'])
def admin_create_portal():
    err = _require_admin()
    if err: return err
    try:
        body = request.get_json() or {}
        caller = str(body.get('caller_email', '')).strip().lower()
        portal_id = str(body.get('id', '')).strip()
        name = str(body.get('name', '')).strip()
        if not portal_hooks.valid_portal_id(portal_id):
            return jsonify({"error": "Portal id must be lowercase letters, digits, '-' or '_' (max 63 chars)."}), 400
        if portal_id == 'admin':  # the frontend serves the admin console at /admin
            return jsonify({"error": "'admin' is a reserved portal id."}), 400
        if not name:
            return jsonify({"error": "name is required"}), 400
        config = portal_config.validate_portal_config(body.get('config'))

        with _db_lock:
            con = get_con()
            if con.execute("SELECT 1 FROM portals WHERE id=?", [portal_id]).fetchone():
                return jsonify({"error": f"A portal with id '{portal_id}' already exists."}), 409
            con.execute("INSERT INTO portals VALUES (?,?,?,?,?,?)", [
                portal_id, name, str(body.get('description', '')).strip(), json.dumps(config),
                datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), True,
            ])
        _audit(caller, 'portal_create', {"portal_id": portal_id, "strategy": config['load_strategy']})
        return jsonify({"status": "ok", "id": portal_id}), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/admin/portals/<portal_id>', methods=['PUT'])
def admin_update_portal(portal_id):
    err = _require_admin()
    if err: return err
    try:
        body = request.get_json() or {}
        caller = str(body.get('caller_email', '')).strip().lower()
        name = str(body.get('name', '')).strip()
        if not name:
            return jsonify({"error": "name is required"}), 400
        config = portal_config.validate_portal_config(body.get('config'))

        with _db_lock:
            con = get_con()
            existing = con.execute("SELECT is_active FROM portals WHERE id=?", [portal_id]).fetchone()
            if not existing:
                return jsonify({"error": f"Portal not found: {portal_id}"}), 404
            is_active = bool(body['is_active']) if 'is_active' in body else bool(existing[0])
            con.execute(
                "UPDATE portals SET name=?, description=?, config=?, is_active=? WHERE id=?",
                [name, str(body.get('description', '')).strip(), json.dumps(config), is_active, portal_id],
            )
        _audit(caller, 'portal_update', {"portal_id": portal_id, "strategy": config['load_strategy'], "is_active": is_active})
        return jsonify({"status": "ok"})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/admin/portals/<portal_id>', methods=['DELETE'])
def admin_deactivate_portal(portal_id):
    """Soft delete: hides the portal from every user list but keeps its
    config and access rows so it can be switched back on (PUT is_active)."""
    err = _require_admin()
    if err: return err
    try:
        with _db_lock:
            con = get_con()
            if not con.execute("SELECT 1 FROM portals WHERE id=?", [portal_id]).fetchone():
                return jsonify({"error": f"Portal not found: {portal_id}"}), 404
            con.execute("UPDATE portals SET is_active=FALSE WHERE id=?", [portal_id])
        _audit(request.args.get('caller_email', '').strip().lower(), 'portal_deactivate', {"portal_id": portal_id})
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/admin/portals/<portal_id>/access', methods=['GET'])
def admin_list_access(portal_id):
    err = _require_admin()
    if err: return err
    try:
        with _db_lock:
            rows = get_con().execute(
                "SELECT email, restrict_values FROM portal_access WHERE portal_id=? ORDER BY email", [portal_id]
            ).fetchall()
        return jsonify({"access": [
            {"email": r[0], "restrict_values": json.loads(r[1]) if r[1] else []} for r in rows
        ]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/admin/portals/<portal_id>/access', methods=['DELETE'])
def admin_remove_access(portal_id):
    err = _require_admin()
    if err: return err
    try:
        email = request.args.get('email', '').strip().lower()
        if not email:
            return jsonify({"error": "email required"}), 400
        with _db_lock:
            get_con().execute("DELETE FROM portal_access WHERE portal_id=? AND LOWER(email)=?", [portal_id, email])
        _audit(request.args.get('caller_email', '').strip().lower(), 'portal_access_remove',
               {"portal_id": portal_id, "email": email})
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Admin: staging table set-up ────────────────────────────────────────────

_FABRIC_COLUMNS_SQL = (
    "SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE, DATETIME_PRECISION "
    "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=? AND TABLE_NAME=? ORDER BY ORDINAL_POSITION")


def _load_portal_config_any(portal_id: str) -> dict:
    """A portal's config whether or not it is active (admin screens need both)."""
    with _db_lock:
        row = get_con().execute("SELECT config FROM portals WHERE id=?", [portal_id]).fetchone()
    if not row:
        raise ValueError(f"Portal not found: {portal_id}")
    return json.loads(row[0])


def _fabric_columns(cursor, table: str) -> list:
    schema, name = publisher.split_table(table)
    cursor.execute(_FABRIC_COLUMNS_SQL, [schema, name])
    return [tuple(r) for r in cursor.fetchall()]


def _staging_plan(config: dict) -> dict:
    """Inspect Fabric and work out what the portal's staging table needs.
    Raises on a connection failure."""
    target = config['target_table']
    suggested = f"stg.{publisher.split_table(target)[1]}"
    name = config.get('staging_table') or suggested
    loaded_cols = _upload_insert_columns(config)[0]
    conn = _fab_conn()
    try:
        cur = conn.cursor()
        target_meta = _fabric_columns(cur, target)
        staging_meta = _fabric_columns(cur, name)
        cur.execute("SELECT COUNT(*) FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME=?",
                    [publisher.split_table(name)[0]])
        schema_exists = int(cur.fetchone()[0]) > 0
    finally:
        conn.close()

    plan = {"staging_table": name, "configured": bool(config.get('staging_table')), "suggested": suggested,
            "target_found": bool(target_meta), "staging_exists": bool(staging_meta),
            "schema_exists": schema_exists, "missing_columns": [], "statements": []}
    if staging_meta:
        have = {str(m[0]).upper() for m in staging_meta}
        plan["missing_columns"] = [c for c in loaded_cols + publisher.STAGE_META_COLUMNS if c.upper() not in have]
    elif target_meta:
        try:
            ddl = publisher.build_staging_ddl(name, target_meta, loaded_cols)
            plan["statements"] = ([] if schema_exists else [f"CREATE SCHEMA {publisher.split_table(name)[0]}"]) + [ddl]
        except ValueError as e:
            plan["ddl_error"] = str(e)
    return plan


@app.route('/admin/portals/<portal_id>/staging', methods=['GET'])
def admin_staging_status(portal_id):
    """What the portal's staging table looks like in Fabric right now, and the
    DDL that would create it. Read-only."""
    err = _require_admin()
    if err: return err
    try:
        config = _load_portal_config_any(portal_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    try:
        return jsonify(_staging_plan(config))
    except Exception as e:
        return jsonify({"fabric_error": str(e), "configured": bool(config.get('staging_table')),
                        "staging_table": config.get('staging_table')})


@app.route('/admin/portals/<portal_id>/staging', methods=['POST'])
def admin_create_staging(portal_id):
    """Create the portal's staging table (and its schema if missing) using the
    app's own Fabric login. Needs the table name saved on the portal first."""
    err = _require_admin()
    if err: return err
    try:
        config = _load_portal_config_any(portal_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    if not config.get('staging_table'):
        return jsonify({"error": "Save a staging table name on the portal first."}), 400
    caller = str((request.get_json(silent=True) or {}).get('caller_email', '')).strip().lower()
    try:
        plan = _staging_plan(config)
    except Exception as e:
        return jsonify({"error": f"Could not reach Fabric: {e}"}), 502
    if plan["staging_exists"]:
        return jsonify({"error": f"{plan['staging_table']} already exists.", **plan}), 409
    if not plan["target_found"]:
        return jsonify({"error": f"Could not find the live table {config['target_table']} in Fabric."}), 400
    if plan.get("ddl_error"):
        return jsonify({"error": plan["ddl_error"]}), 400
    conn = None
    try:
        conn = _fab_conn()
        conn.autocommit = True
        cur = conn.cursor()
        for stmt in plan["statements"]:
            cur.execute(stmt)
    except Exception as e:
        return jsonify({"error": f"Fabric refused the statement: {e}", "statements": plan["statements"]}), 500
    finally:
        if conn is not None:
            conn.close()
    _audit(caller, 'staging_create', {"portal_id": portal_id, "staging_table": plan["staging_table"]})
    return jsonify({"status": "created", "statements": plan["statements"], "staging_table": plan["staging_table"]})


# ── Sample template download (Phase 1) ─────────────────────────────────────

def _example_value(col: dict, config: dict, period: "dict | None") -> str:
    if col.get('example'):
        return col['example']
    if col['key'] == config.get('date_column'):
        day = date.fromisoformat(period['start']) if period else date.today()
        return day.strftime(config.get('date_format') or '%Y-%m-%d')
    return '1234.50' if col.get('numeric') else 'Text'


def _sample_template_file(portal: dict, fmt: str):
    """Build the downloadable template. The data sheet holds the header row
    only — an example row there could be uploaded by mistake as real data —
    so the worked example lives on a separate Instructions sheet."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    config = portal['config']
    columns = config.get('columns', [])
    keys = [c['key'] for c in columns]

    if fmt == 'csv':
        return (','.join(keys) + '\n').encode('utf-8'), 'text/csv'

    period = _period_for_rule(config.get('period_rule'))
    strategy = load_strategies.strategy_of(config)

    wb = Workbook()
    ws = wb.active
    ws.title = 'Template'   # first sheet: the only one the uploader reads
    ws.append(keys)
    header_fill = PatternFill('solid', fgColor='1F4E78')
    for cell in ws[1]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = header_fill
    for i, key in enumerate(keys, start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = max(14, len(key) + 4)
    ws.freeze_panes = 'A2'

    info = wb.create_sheet('Instructions')
    info.append([portal['name']])
    info['A1'].font = Font(bold=True, size=14)
    info.append([])
    info.append(['How to use'])
    info['A3'].font = Font(bold=True)
    info.append(['1. Fill in the Template sheet starting at row 2. Do not rename, reorder or add columns.'])
    info.append(['2. Save and upload the file in the portal.'])
    info.append([f"What the upload does: {load_strategies.STRATEGIES[strategy]['summary']}"])
    if period:
        info.append([f"Expected period right now: {period['label']} ({period['start']} to {period['end']}). "
                     f"Uploads on or after day {period['cutover_day']} of the month count toward the next month."])
    info.append([])
    info.append(['Column', 'Meaning', 'Type', 'Example'])
    for cell in info[info.max_row]:
        cell.font = Font(bold=True)
    for c in columns:
        kind = 'Number' if c.get('numeric') else ('Date' if c['key'] == config.get('date_column') else 'Text')
        info.append([c['key'], c.get('description') or c.get('label') or c['key'], kind,
                     _example_value(c, config, period)])
    info.column_dimensions['A'].width = 22
    info.column_dimensions['B'].width = 46
    info.column_dimensions['C'].width = 10
    info.column_dimensions['D'].width = 18
    for row in info.iter_rows():
        for cell in row:
            cell.alignment = Alignment(wrap_text=False, vertical='top')

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


@app.route('/portals/<portal_id>/sample-template', methods=['GET'])
def sample_template(portal_id):
    try:
        fmt = request.args.get('format', 'xlsx').strip().lower()
        if fmt not in ('xlsx', 'csv'):
            return jsonify({"error": "format must be xlsx or csv"}), 400
        portal = _load_portal(portal_id)
        data, mimetype = _sample_template_file(portal, fmt)
        return send_file(io.BytesIO(data), mimetype=mimetype, as_attachment=True,
                         download_name=f"{portal_id}_template.{fmt}")
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Upload endpoints ────────────────────────────────────────────────────

@app.route('/upload-template', methods=['GET'])
def get_upload_template():
    """Return a portal's expected columns and the live-computed expected
    period, so the UI can show 'what to upload' before a file is picked."""
    try:
        portal_id = request.args.get('portal_id', '').strip()
        portal = _load_portal(portal_id)
        config = portal.get('config') or {}
        return jsonify({
            "portal_id":    portal_id,
            "target_table": config.get('target_table'),
            "columns":      config.get('columns', []),
            "date_column":  config.get('date_column'),
            "date_format":  config.get('date_format'),
            "restrict_col": config.get('restrict_col'),
            "load_strategy": load_strategies.strategy_of(config),
            "staged":       bool(config.get('staging_table')),
            "busy":         _busy_view(portal_id, request.args.get('email', '')),
            "period_rule":  config.get('period_rule'),
            "period":       _period_for_rule(config.get('period_rule')),
        })
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/upload/start', methods=['POST'])
def start_upload():
    """Parse + validate an uploaded file against the portal's template and
    period rule. Nothing is written to Fabric here — a valid file is staged
    in memory under an upload_id for a follow-up /upload/commit."""
    try:
        _cleanup_expired_uploads()

        portal_id = request.form.get('portal_id', '').strip()
        email = request.form.get('email', '').strip().lower()
        if not portal_id:
            return jsonify({"error": "portal_id is required"}), 400
        if not email:
            return jsonify({"error": "email is required"}), 400
        if 'file' not in request.files or not request.files['file'].filename:
            return jsonify({"error": "file is required"}), 400

        portal = _load_portal(portal_id)
        config = portal.get('config') or {}
        if not _valid_view_name(config.get('target_table', '')):
            return jsonify({"error": "Portal target_table is misconfigured"}), 500

        busy = _busy_view(portal_id, email)
        if busy:   # say so before they wait on a file that can't be published yet
            return jsonify({"error": busy["message"], "busy": busy}), 409

        restrict_col = config.get('restrict_col')
        restrict_values = _load_user_restrict_values(portal_id, email, restrict_col) if restrict_col else None
        strategy = load_strategies.strategy_of(config)
        load_strategies.check_allowed(strategy, restrict_values)

        df, filename = _read_upload_file(request.files['file'])
        hook_ctx = {
            "portal_id": portal_id, "config": config, "email": email, "filename": filename,
            "period": _period_for_rule(config.get('period_rule')),
            "restrict_values": restrict_values, "table": config['target_table'],
        }
        df = portal_hooks.run_prepare(portal_id, df, hook_ctx)
        result = _build_upload_rows(df, config, restrict_values)
        if not result['row_count']:
            # An empty file would still delete the period/table under replace_period or truncate_load.
            raise ValueError("The file has no data rows, so there is nothing to upload.")

        load_run_date = datetime.utcnow().strftime('%Y%m%d%H%M%S')
        run_date_str = date.today().isoformat()
        all_cols, cols, audit_cols = _upload_insert_columns(config)
        # Append exactly the audit values the portal has columns for, in the
        # same order _upload_insert_columns() lists them; a portal may name
        # only some of them (or none), and a value without a column makes the
        # INSERT's parameter count disagree with its placeholders.
        audit_values = {'file_name': filename, 'run_date': run_date_str, 'load_run_date': load_run_date}
        audit_cfg = config.get('audit_columns') or {}
        extras = [audit_values[k] for k in ('file_name', 'run_date', 'load_run_date') if audit_cfg.get(k)]
        staged_rows = [row + extras for row in result['rows']]

        upload_id = uuid.uuid4().hex
        with _upload_lock:
            _upload_staging[upload_id] = {
                "portal_id":  portal_id,
                "columns":    all_cols,
                "email":      email,
                "config":     config,
                "rows":       staged_rows,
                "brands_in_file": result['brands_in_file'],
                "period":     result['period'],
                "load_run_date": load_run_date,
                "filename":   filename,
                "restrict_values": restrict_values,
                "load_strategy": strategy,
                "created_at": time.time(),
            }

        return jsonify({
            "status":     "ready",
            "upload_id":  upload_id,
            "filename":   filename,
            "load_strategy": strategy,
            "staged":     bool(config.get('staging_table')),
            "row_count":  result['row_count'],
            "brands_in_file": result['brands_in_file'],
            "period":     result['period'],
            "null_counts": result['null_counts'],
            "preview":    result['preview'],
        })
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        print("Upload validate failed:", traceback.format_exc(), flush=True)
        return jsonify({"error": str(e)}), 500


@app.route('/upload/commit', methods=['POST'])
def commit_upload():
    """Start publishing a previously validated upload. Returns 202 at once with
    a job id; the load runs in the background (see publisher.py) and the page
    polls /upload/status. Only one upload per portal runs at a time — a second
    one gets a 409 and its validated file is kept, so Confirm can be pressed
    again once the portal is free."""
    try:
        body = request.get_json() or {}
        upload_id = str(body.get('upload_id', '')).strip()
        email = str(body.get('email', '')).strip().lower()
        if not upload_id:
            return jsonify({"error": "upload_id is required"}), 400

        with _upload_lock:
            staged = _upload_staging.get(upload_id)
        if not staged or (time.time() - staged.get('created_at', 0) > UPLOAD_SESSION_TTL_SECONDS):
            return jsonify({"error": "Upload session expired or not found. Please re-upload the file."}), 404
        if email and staged['email'] != email:
            return jsonify({"error": "This upload session belongs to a different user."}), 403

        portal_id = staged['portal_id']
        holder = _try_acquire_portal(portal_id, upload_id, staged['email'])
        if holder:
            view = _busy_view(portal_id, staged['email']) or {
                "message": "Another upload to this portal is in progress. Please try again in a few minutes."}
            return jsonify({"error": view["message"], "busy": view}), 409

        with _upload_lock:
            staged = _upload_staging.pop(upload_id, None)
        if staged is None:     # a double-click got there first
            _release_portal(portal_id, upload_id)
            return jsonify({"error": "This upload was already submitted."}), 409

        config = staged['config']
        staged['upload_id'] = upload_id
        staged['load_strategy'] = staged.get('load_strategy') or load_strategies.strategy_of(config)
        mode = 'staged' if config.get('staging_table') else 'direct'
        period = staged['period']
        try:
            with _db_lock:
                get_con().execute(
                    "INSERT INTO upload_jobs (id, portal_id, email, filename, mode, state, rows_total, rows_done, "
                    "period_label, brands, started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [upload_id, portal_id, staged['email'], staged.get('filename'), mode, 'starting',
                     len(staged['rows']), 0, (period or {}).get('label'),
                     json.dumps(staged['brands_in_file']), _utcnow_str()])
            threading.Thread(target=_run_upload_job, args=(upload_id, staged), daemon=True,
                             name=f"upload-{upload_id[:8]}").start()
        except Exception:
            _release_portal(portal_id, upload_id)
            raise

        return jsonify({"status": "started", "job_id": upload_id, "mode": mode,
                        "rows_total": len(staged['rows'])}), 202
    except Exception as e:
        print("Upload commit failed to start:", traceback.format_exc(), flush=True)
        return jsonify({"error": str(e)}), 500


@app.route('/upload/status', methods=['GET'])
def upload_status():
    """Progress / outcome of an upload job. Visible to the user who started it
    and to admins."""
    try:
        job_id = request.args.get('job_id', '').strip()
        email = request.args.get('email', '').strip().lower()
        job = _get_job(job_id) if job_id else None
        if not job:
            return jsonify({"error": "Upload not found."}), 404
        if job['email'] != email and not _is_admin(email):
            return jsonify({"error": "This upload belongs to a different user."}), 403
        job['load_strategy'] = None
        with _db_lock:
            row = get_con().execute("SELECT config FROM portals WHERE id=?", [job['portal_id']]).fetchone()
        if row:
            cfg = json.loads(row[0])
            job['load_strategy'] = load_strategies.strategy_of(cfg)
            job['table'] = cfg.get('target_table')
        return jsonify(job)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Audit log endpoints ───────────────────────────────────────────────────

@app.route('/logs', methods=['POST'])
def insert_log():
    try:
        body = request.get_json() or {}
        if not body:
            return jsonify({"error": "No data"}), 400
        with _db_lock:
            con = get_con()
            next_id = con.execute("SELECT COALESCE(MAX(id),0)+1 FROM audit_logs").fetchone()[0]
            con.execute("INSERT INTO audit_logs VALUES (?,?,?,?,?,?)", [
                next_id, datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
                body.get('email', ''), body.get('name', ''),
                body.get('action', ''), json.dumps(body.get('details', {})),
            ])
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


init_db()
_sweep_interrupted_jobs()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5003)
