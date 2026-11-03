"""
Publishing an upload to Fabric.

Staged mode (portal config has `staging_table`) — the live table is only ever
touched by ONE transaction, so it ends up with the old data or the new data,
never half of each, and never blank:

  1. STAGE    insert every row into the staging table, tagged with the upload
              id and a batch number. The live table is not touched. A dropped
              connection is retried batch by batch (each retry first removes
              that batch's rows, so a batch that half-arrived can't double).
  2. VERIFY   the staging row count must equal the file's row count.
  3. SWAP     one transaction: the strategy's delete/truncate on the live
              table, INSERT ... SELECT from staging, delete the staging rows,
              COMMIT. Deleting the staging rows inside the same transaction is
              deliberate: if the connection dies at COMMIT we can't know
              whether it landed, but "no staging rows left" proves it did.

Direct mode (no `staging_table`) is the original behaviour — delete, then
insert in batches, each statement auto-committed. It can leave a partial
load if the connection drops, and the job result says so.

Everything here takes a `connect()` factory so it can be exercised against a
stand-in warehouse.
"""
import time

import pyodbc

import load_strategies
import portal_hooks

# SQL Server allows 2100 parameters in one statement; stay under with headroom.
SQL_PARAM_LIMIT = 2000
MAX_BATCH_ROWS = 150
MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2.0
STAGE_META_COLUMNS = ["UPLOAD_ID", "BATCH_NO"]

_TRANSIENT_MARKERS = (
    "08S01", "08001", "08S02", "HYT00", "HYT01", "communication link", "tcp provider",
    "connection reset", "connection is closed", "timeout expired", "broken pipe", "connection was forcibly closed",
)


class PublishError(Exception):
    """A failure with a message fit to show the user.

    `live_changed` says what state the live table is in: False = untouched,
    True = may be partially updated, None = unknown (the connection dropped
    while committing and we could not check).
    """

    def __init__(self, message: str, live_changed=False):
        super().__init__(message)
        self.message = message
        self.live_changed = live_changed


def is_transient(exc: BaseException) -> bool:
    """A network/connection failure worth retrying — not a data or SQL error."""
    if isinstance(exc, (pyodbc.OperationalError, pyodbc.InterfaceError)):
        return True
    text = str(exc).lower()
    return any(m.lower() in text for m in _TRANSIENT_MARKERS)


def batch_rows_for(n_columns: int) -> int:
    """Rows per INSERT so the statement stays under SQL Server's parameter cap.
    (A fixed 150 rows breaks any table wider than ~14 columns.)"""
    return max(1, min(MAX_BATCH_ROWS, SQL_PARAM_LIMIT // max(n_columns, 1)))


def insert_sql(table: str, columns: list, n_rows: int) -> str:
    col_sql = ", ".join(f"[{c}]" for c in columns)
    group = "(" + ", ".join(["?"] * len(columns)) + ")"
    return f"INSERT INTO {table} ({col_sql}) VALUES " + ", ".join([group] * n_rows)


def _short(exc: BaseException, limit: int = 300) -> str:
    text = " ".join(str(exc).split())
    return text if len(text) <= limit else text[:limit] + "…"


def _open(connect, autocommit: bool):
    last = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            conn = connect()
            conn.autocommit = autocommit
            return conn
        except Exception as e:
            last = e
            if attempt < MAX_ATTEMPTS and is_transient(e):
                time.sleep(RETRY_DELAY_SECONDS * attempt)
                continue
            raise
    raise last  # pragma: no cover


def _close(conn):
    try:
        conn.close()
    except Exception:
        pass


def _rollback(conn):
    try:
        conn.rollback()
    except Exception:
        pass


# ── direct mode (and shared bulk insert) ───────────────────────────────────

def bulk_insert(cursor, table: str, columns: list, rows: list, progress=None):
    """Insert rows via multi-row VALUES batches.

    cursor.executemany() with fast_executemany=True was measured against
    Fabric's ODBC endpoint at ~3-7 rows/sec — no faster than a per-row loop —
    so one multi-row INSERT per batch is used instead: one round trip per
    batch regardless of what the driver does internally.
    """
    per = batch_rows_for(len(columns))
    total = len(rows)
    for start in range(0, total, per):
        chunk = rows[start:start + per]
        cursor.execute(insert_sql(table, columns, len(chunk)), [v for row in chunk for v in row])
        if progress:
            progress(min(start + per, total), total)


def _direct(connect, staged, progress):
    config, rows = staged["config"], staged["rows"]
    table = config["target_table"]
    conn = _open(connect, autocommit=True)
    touched = False
    try:
        cursor = conn.cursor()
        removed = portal_hooks.run_before_insert(staged["portal_id"], cursor, _hook_ctx(staged))
        if removed is None:
            removed = load_strategies.prepare_target(
                staged["load_strategy"], cursor, config, staged["period"], staged["brands_in_file"])
        touched = True
        bulk_insert(cursor, table, staged["columns"], rows, progress)
    except PublishError:
        raise
    except Exception as e:
        raise PublishError(
            f"The upload stopped part-way ({_short(e)}). {table} may now hold only part of this file — "
            "upload the file again to complete it." if touched else
            f"The upload could not start ({_short(e)}). The table was not changed.",
            live_changed=touched,
        ) from e
    finally:
        _close(conn)
    return {"deleted": removed, "inserted": len(rows)}


# ── staged mode ────────────────────────────────────────────────────────────

def _hook_ctx(staged):
    return {
        "portal_id": staged["portal_id"], "config": staged["config"], "email": staged["email"],
        "filename": staged.get("filename"), "period": staged["period"],
        "restrict_values": staged.get("restrict_values"), "table": staged["config"]["target_table"],
        "brands": staged["brands_in_file"],
    }


def stage_rows(connect, staging_table: str, columns: list, rows: list, upload_id: str, progress=None) -> int:
    """Load all rows into the staging table; returns how many it now holds for
    this upload. Retries a dropped connection one batch at a time."""
    cols = list(columns) + STAGE_META_COLUMNS
    per = batch_rows_for(len(cols))
    total = len(rows)
    conn = _open(connect, autocommit=True)
    try:
        cursor = conn.cursor()
        # Clean slate for this upload id (a re-run after a crash starts clean).
        cursor.execute(f"DELETE FROM {staging_table} WHERE [UPLOAD_ID] = ?", [upload_id])
        for batch_no, start in enumerate(range(0, total, per)):
            chunk = rows[start:start + per]
            sql = insert_sql(staging_table, cols, len(chunk))
            params = [v for row in chunk for v in (*row, upload_id, batch_no)]
            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    cursor.execute(sql, params)
                    break
                except Exception as e:
                    if not is_transient(e) or attempt == MAX_ATTEMPTS:
                        raise
                    # The batch may or may not have reached the server. Reconnect,
                    # remove any copy of exactly this batch, and send it again.
                    time.sleep(RETRY_DELAY_SECONDS * attempt)
                    _close(conn)
                    conn = _open(connect, autocommit=True)
                    cursor = conn.cursor()
                    cursor.execute(
                        f"DELETE FROM {staging_table} WHERE [UPLOAD_ID] = ? AND [BATCH_NO] = ?", [upload_id, batch_no])
            if progress:
                progress(min(start + per, total), total)
        cursor.execute(f"SELECT COUNT(*) FROM {staging_table} WHERE [UPLOAD_ID] = ?", [upload_id])
        return int(cursor.fetchone()[0])
    finally:
        _close(conn)


def discard_staged(connect, staging_table: str, upload_id: str):
    """Best effort: remove an upload's staging rows. Never raises."""
    try:
        conn = _open(connect, autocommit=True)
        try:
            conn.cursor().execute(f"DELETE FROM {staging_table} WHERE [UPLOAD_ID] = ?", [upload_id])
        finally:
            _close(conn)
    except Exception:
        pass


def _swap_committed(connect, staging_table: str, upload_id: str):
    """After a connection failure at COMMIT: True if the swap landed (its
    staging rows are gone), False if it did not, None if we can't tell."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            conn = _open(connect, autocommit=True)
            try:
                cur = conn.cursor()
                cur.execute(f"SELECT COUNT(*) FROM {staging_table} WHERE [UPLOAD_ID] = ?", [upload_id])
                return int(cur.fetchone()[0]) == 0
            finally:
                _close(conn)
        except Exception:
            time.sleep(RETRY_DELAY_SECONDS * attempt)
    return None


def swap(connect, staged, upload_id: str) -> dict:
    """The one transaction that changes the live table."""
    config = staged["config"]
    target, staging = config["target_table"], config["staging_table"]
    columns, expected = staged["columns"], len(staged["rows"])
    col_sql = ", ".join(f"[{c}]" for c in columns)

    conn = _open(connect, autocommit=False)
    try:
        cursor = conn.cursor()
        removed = portal_hooks.run_before_insert(staged["portal_id"], cursor, _hook_ctx(staged))
        if removed is None:
            removed = load_strategies.prepare_target(
                staged["load_strategy"], cursor, config, staged["period"], staged["brands_in_file"],
                transactional=True)
        cursor.execute(
            f"INSERT INTO {target} ({col_sql}) SELECT {col_sql} FROM {staging} WHERE [UPLOAD_ID] = ?", [upload_id])
        inserted = cursor.rowcount
        if inserted is not None and inserted >= 0 and inserted != expected:
            raise PublishError(
                f"The swap would have loaded {inserted:,} rows instead of {expected:,}, so it was cancelled. "
                "The live table was not changed.", live_changed=False)
        cursor.execute(f"DELETE FROM {staging} WHERE [UPLOAD_ID] = ?", [upload_id])
    except BaseException:
        _rollback(conn)
        _close(conn)
        raise

    try:
        conn.commit()
    except Exception as e:
        _close(conn)
        if not is_transient(e):
            raise
        landed = _swap_committed(connect, staging, upload_id)
        if landed is False:
            raise PublishError(
                "The connection dropped while finishing, and the change did not go through. "
                "The live table was not changed.", live_changed=False) from e
        if landed is None:
            raise PublishError(
                "The connection dropped while finishing and the result could not be confirmed. "
                f"Check {target} before uploading again.", live_changed=None) from e
        # landed is True: the commit reached the server before the connection died.
    else:
        _close(conn)
    return {"deleted": removed, "inserted": expected if inserted is None or inserted < 0 else inserted}


def _friendly_stage_error(e: BaseException, staging_table: str) -> str:
    text = str(e)
    if "invalid object name" in text.lower() or "42S02" in text:
        return (f"The staging table {staging_table} doesn't exist or this login can't use it. An administrator can "
                "create it in Admin console → Portals → Staging. The live table was not changed.")
    if "invalid column name" in text.lower() or "42S22" in text:
        return (f"The staging table {staging_table} is missing a column this portal needs. An administrator can "
                "check it in Admin console → Portals → Staging. The live table was not changed.")
    return f"Loading into the staging table failed: {_short(e)}. The live table was not changed."


def publish(connect, staged: dict, progress=None, set_state=None) -> dict:
    """Run the whole load for one validated upload. `progress(done, total)` and
    `set_state('staging'|'publishing')` report where it is. Returns
    {'deleted', 'inserted', 'staged': bool}; raises PublishError."""
    config, rows = staged["config"], staged["rows"]
    total = len(rows)
    state = set_state or (lambda s: None)
    if total == 0:
        raise PublishError("The file has no data rows, so there is nothing to upload.", live_changed=False)

    staging = config.get("staging_table")
    if not staging:
        state("publishing")
        out = _direct(connect, staged, progress)
        out["staged"] = False
        return out

    upload_id = staged["upload_id"]
    state("staging")
    try:
        held = stage_rows(connect, staging, staged["columns"], rows, upload_id, progress)
    except Exception as e:
        discard_staged(connect, staging, upload_id)
        raise PublishError(_friendly_stage_error(e, staging), live_changed=False) from e
    if held != total:
        discard_staged(connect, staging, upload_id)
        raise PublishError(
            f"Only {held:,} of {total:,} rows reached the staging table. The live table was not changed.",
            live_changed=False)

    state("publishing")
    try:
        out = swap(connect, staged, upload_id)
    except PublishError as e:
        if e.live_changed is False:
            discard_staged(connect, staging, upload_id)
        raise
    except Exception as e:
        discard_staged(connect, staging, upload_id)
        raise PublishError(
            f"Replacing the live data failed and was rolled back: {_short(e)}. The live table was not changed.",
            live_changed=False) from e
    out["staged"] = True
    return out


def verify_loaded(connect, staged: dict):
    """Rows now in the live table carrying this load's timestamp, or the file's
    row count when the portal has no load-timestamp column. Never raises."""
    config = staged["config"]
    load_col = (config.get("audit_columns") or {}).get("load_run_date")
    if not load_col:
        return len(staged["rows"])
    try:
        conn = _open(connect, autocommit=True)
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT COUNT(*) FROM {config['target_table']} WHERE [{load_col}] = ?",
                        [staged["load_run_date"]])
            return int(cur.fetchone()[0])
        finally:
            _close(conn)
    except Exception:
        return None


# ── staging table set-up ───────────────────────────────────────────────────

def _sql_type(data_type, char_len, num_prec, num_scale, dt_prec) -> str:
    t = str(data_type).lower()
    if t in ("varchar", "char", "varbinary", "binary"):
        length = 8000 if char_len in (None, -1) else int(char_len)   # Fabric has no MAX
        return f"{t}({length})"
    if t in ("decimal", "numeric"):
        return f"{t}({int(num_prec)},{int(num_scale or 0)})"
    if t in ("datetime2", "time"):
        return f"{t}({6 if dt_prec is None else int(dt_prec)})"
    return t   # int, bigint, smallint, float, real, bit, date, uniqueidentifier


def build_staging_ddl(staging_table: str, target_meta: list, columns: list) -> str:
    """CREATE TABLE for the staging table: each loaded column with the same
    type as in the live table, plus UPLOAD_ID and BATCH_NO.

    `target_meta` rows are (COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH,
    NUMERIC_PRECISION, NUMERIC_SCALE, DATETIME_PRECISION) from the live
    table's INFORMATION_SCHEMA.COLUMNS."""
    by_name = {str(m[0]).upper(): m for m in target_meta}
    missing = [c for c in columns if c.upper() not in by_name]
    if missing:
        raise ValueError(f"The live table has no column(s): {', '.join(missing)}")
    lines = [f"    [{c}] {_sql_type(*by_name[c.upper()][1:])}" for c in columns]
    lines += ["    [UPLOAD_ID] varchar(40)", "    [BATCH_NO] int"]
    return f"CREATE TABLE {staging_table} (\n" + ",\n".join(lines) + "\n)"


def split_table(name: str):
    schema, table = name.split(".", 1)
    return schema, table
