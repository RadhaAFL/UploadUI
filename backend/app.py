"""
Upload Portal API — config-driven file uploads into Microsoft Fabric Warehouse.
Standalone app (own DuckDB for access control), modeled on the auth/Fabric
conventions proven in SEMANTIC-LAYER and Money Mapping, purpose-built rather
than generic since this app only ever does one thing: validate an uploaded
file against an admin-defined template + period rule, then replace matching
rows in the target Fabric table.
Runs on port 5003, proxied by Apache at /uploadportal-api/
"""
from flask import Flask, jsonify, request
from flask_cors import CORS
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

app = Flask(__name__)
CORS(app)

# ── Local data paths ───────────────────────────────────────────────────────
DATA_ROOT = os.path.join(os.path.dirname(__file__), '..', 'data')
DB_PATH   = os.path.join(DATA_ROOT, 'app.duckdb')

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
        parsed = pd.to_datetime(work[date_col], errors='coerce')
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


def _upload_insert_sql(config: dict):
    cols = [c['key'] for c in config.get('columns', [])]
    audit = config.get('audit_columns') or {}
    audit_cols = [audit[k] for k in ('file_name', 'run_date', 'load_run_date') if audit.get(k)]
    all_cols = cols + audit_cols
    col_sql = ', '.join(f'[{c}]' for c in all_cols)
    placeholders = ', '.join(['?'] * len(all_cols))
    return f"INSERT INTO {config['target_table']} ({col_sql}) VALUES ({placeholders})", cols, audit_cols


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
        with _db_lock:
            con = get_con()
            if _is_admin(email):
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
            "restrict_col": config.get('restrict_col'),
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

        df, filename = _read_upload_file(request.files['file'])
        result = _build_upload_rows(df, config, restrict_values)

        load_run_date = datetime.utcnow().strftime('%Y%m%d%H%M%S')
        run_date_str = date.today().isoformat()
        insert_sql, cols, audit_cols = _upload_insert_sql(config)
        staged_rows = [row + [filename, run_date_str, load_run_date] for row in result['rows']]

        upload_id = uuid.uuid4().hex
        with _upload_lock:
            _upload_staging[upload_id] = {
                "portal_id":  portal_id,
                "email":      email,
                "config":     config,
                "rows":       staged_rows,
                "insert_sql": insert_sql,
                "brands_in_file": result['brands_in_file'],
                "period":     result['period'],
                "load_run_date": load_run_date,
                "created_at": time.time(),
            }

        return jsonify({
            "status":     "ready",
            "upload_id":  upload_id,
            "filename":   filename,
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
        date_col = config.get('date_column')
        restrict_col = config.get('restrict_col')
        period = staged['period']
        brands = staged['brands_in_file']

        conn = None
        try:
            conn = _fab_conn()
            conn.autocommit = True
            cursor = conn.cursor()

            deleted = 0
            if period and date_col:
                where = [f"[{date_col}] BETWEEN ? AND ?"]
                params = [period['start'], period['end']]
                if restrict_col and brands:
                    where.append(f"[{restrict_col}] IN ({','.join(['?'] * len(brands))})")
                    params += brands
                cursor.execute(f"DELETE FROM {table} WHERE {' AND '.join(where)}", params)
                deleted = cursor.rowcount if (cursor.rowcount or 0) > 0 else 0

            try:
                cursor.fast_executemany = True
                cursor.executemany(staged['insert_sql'], staged['rows'])
            except Exception:
                cursor.fast_executemany = False
                for row in staged['rows']:
                    cursor.execute(staged['insert_sql'], row)

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
                json.dumps({"portal_id": staged['portal_id'], "deleted": deleted, "inserted": len(staged['rows'])}),
            ])

        return jsonify({
            "status":    "ok",
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
