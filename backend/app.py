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
            work[key] = work[key].apply(_upload_numeric)
            # Defensive: _upload_numeric should only ever produce None or a
            # float, but a stray value (e.g. an Excel error-cell string
            # like '#DIV/0!' that survives str->float conversion attempts)
            # must never reach the ODBC layer, where it fails the whole
            # batch with an opaque "not a valid instance of data type
            # float" error. Force anything else to NULL and log what it was.
            bad = work[key].apply(lambda v: v is not None and not isinstance(v, float))
            if bad.any():
                examples = work.loc[bad, key].head(3).tolist()
                print(f"Upload validate: {int(bad.sum())} {key} value(s) were not cleanly "
                      f"numeric after conversion; forcing to NULL. Examples: {examples}", flush=True)
                work.loc[bad, key] = None
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


def _bulk_insert_rows(cursor, table: str, columns: list, rows: list, batch_size: int = 150):
    """Insert rows via multi-row VALUES batches.

    cursor.executemany() with fast_executemany=True was measured against
    Fabric's ODBC endpoint at ~3-7 rows/sec — no faster than a plain
    per-row loop, meaning the driver silently isn't getting a server-side
    bulk-RPC speedup through this path (a known pyodbc/driver-combination
    gap, not something Python can detect ahead of time). A single
    multi-row INSERT statement per batch sidesteps that entirely: it's one
    network round trip per `batch_size` rows regardless of what the driver
    does internally. SQL Server caps a statement at 2100 parameters;
    batch_size is kept well under that for any reasonable column count.
    """
    if not rows:
        return
    col_sql = ', '.join(f'[{c}]' for c in columns)
    n_cols = len(columns)
    total = len(rows)
    inserted = 0
    for i in range(0, total, batch_size):
        chunk = rows[i:i + batch_size]
        placeholders = ', '.join('(' + ', '.join(['?'] * n_cols) + ')' for _ in chunk)
        params = [v for row in chunk for v in row]
        cursor.execute(f"INSERT INTO {table} ({col_sql}) VALUES {placeholders}", params)
        inserted += len(chunk)
        if inserted % (batch_size * 20) == 0 or inserted == total:
            print(f"Upload commit: inserted {inserted}/{total} rows into {table}", flush=True)


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
    """Admin: grant/update a user's access to a portal."""
    err = _require_admin()
    if err: return err
    try:
        body = request.get_json() or {}
        email = body.get('email', '').strip().lower()
        values = body.get('restrict_values', [])
        if not email:
            return jsonify({"error": "email required"}), 400
        with _db_lock:
            con = get_con()
            con.execute("DELETE FROM portal_access WHERE portal_id=? AND LOWER(email)=?", [portal_id, email])
            con.execute("INSERT INTO portal_access VALUES (?,?,?)", [portal_id, email, json.dumps(values)])
        return jsonify({"status": "ok"})
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

        load_run_date = datetime.utcnow().strftime('%Y%m%d%H%M%S')
        run_date_str = date.today().isoformat()
        all_cols, cols, audit_cols = _upload_insert_columns(config)
        staged_rows = [row + [filename, run_date_str, load_run_date] for row in result['rows']]

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
    """Commit a previously validated upload: delete existing rows for the
    same period + brand(s), then bulk-insert the staged rows into Fabric."""
    try:
        body = request.get_json() or {}
        upload_id = str(body.get('upload_id', '')).strip()
        email = str(body.get('email', '')).strip().lower()
        if not upload_id:
            return jsonify({"error": "upload_id is required"}), 400

        with _upload_lock:
            staged = _upload_staging.pop(upload_id, None)
        if not staged or (time.time() - staged.get('created_at', 0) > UPLOAD_SESSION_TTL_SECONDS):
            return jsonify({"error": "Upload session expired or not found. Please re-upload the file."}), 404
        if email and staged['email'] != email:
            return jsonify({"error": "This upload session belongs to a different user."}), 403

        config = staged['config']
        table = config['target_table']
        period = staged['period']
        brands = staged['brands_in_file']
        strategy = staged.get('load_strategy') or load_strategies.strategy_of(config)

        conn = None
        try:
            conn = _fab_conn()
            conn.autocommit = True
            cursor = conn.cursor()

            # A portal hook's before_insert() replaces the built-in strategy's
            # delete/truncate step; without one the configured strategy runs.
            hook_ctx = {
                "portal_id": staged['portal_id'], "config": config, "email": staged['email'],
                "filename": staged.get('filename'), "period": period,
                "restrict_values": staged.get('restrict_values'), "table": table, "brands": brands,
            }
            deleted = portal_hooks.run_before_insert(staged['portal_id'], cursor, hook_ctx)
            if deleted is None:
                deleted = load_strategies.prepare_target(strategy, cursor, config, period, brands)

            _bulk_insert_rows(cursor, table, staged['columns'], staged['rows'])

            verified = len(staged['rows'])
            load_run_col = (config.get('audit_columns') or {}).get('load_run_date')
            if load_run_col:
                cursor.execute(f"SELECT COUNT(*) FROM {table} WHERE [{load_run_col}]=?", [staged['load_run_date']])
                verified = int(cursor.fetchone()[0])
        finally:
            if conn is not None:
                conn.close()

        with _db_lock:
            next_id = get_con().execute("SELECT COALESCE(MAX(id),0)+1 FROM audit_logs").fetchone()[0]
            get_con().execute("INSERT INTO audit_logs VALUES (?,?,?,?,?,?)", [
                next_id, datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), email, '', 'upload_commit',
                json.dumps({"portal_id": staged['portal_id'], "strategy": strategy,
                            "deleted": deleted, "inserted": len(staged['rows'])}),
            ])

        return jsonify({
            "status":    "ok",
            "load_strategy": strategy,
            "deleted":   deleted,
            "inserted":  len(staged['rows']),
            "verified":  verified,
            "period":    period,
            "brands":    brands,
            "table":     table,
        })
    except Exception as e:
        print("Upload commit failed:", traceback.format_exc(), flush=True)
        return jsonify({
            "error": str(e),
            "hint": "Check Fabric schema/table name, INSERT/DELETE permission, and ODBC credentials.",
        }), 500


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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5003)
