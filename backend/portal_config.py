"""
Validation + normalisation for admin-submitted portal configs.

A portal's config is interpolated into SQL (table and column names go inside
[brackets], which parameters can't cover), so anything an admin saves is
checked against strict identifier patterns here. The seeded config is code
we wrote; everything arriving over the API goes through
validate_portal_config() before it is stored.
"""
import re

import load_strategies

TABLE_RE = re.compile(r"^[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")
# File headers are upper-cased on read and compared to `key`, so keys must
# already be upper-case to ever match.
COLUMN_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
AUDIT_COLUMN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
AUDIT_FIELDS = ("file_name", "run_date", "load_run_date")
MAX_COLUMNS = 100


def _text(value, max_len: int) -> str:
    return str(value if value is not None else "").strip()[:max_len]


def validate_portal_config(config) -> dict:
    """Return a cleaned copy of `config`; raise ValueError listing every
    problem found (so the admin UI can show them all at once)."""
    errors = []
    if not isinstance(config, dict):
        raise ValueError("config must be an object")

    out = {}

    table = _text(config.get("target_table"), 130)
    if not TABLE_RE.match(table):
        errors.append("target_table must look like schema.TableName (letters, digits, underscore).")
    out["target_table"] = table

    strategy = config.get("load_strategy") or load_strategies.DEFAULT_STRATEGY
    if strategy not in load_strategies.STRATEGIES:
        errors.append(f"load_strategy must be one of: {', '.join(load_strategies.STRATEGIES)}.")
    out["load_strategy"] = strategy

    # Optional: where an upload is loaded first, before the live table is swapped
    # in one transaction (see publisher.py). Must be a different table.
    staging = _text(config.get("staging_table"), 130)
    if staging:
        if not TABLE_RE.match(staging):
            errors.append("staging_table must look like schema.TableName (letters, digits, underscore).")
        elif staging.lower() == table.lower():
            errors.append("staging_table must be a different table from target_table.")
        else:
            out["staging_table"] = staging

    # ── columns ──
    raw_columns = config.get("columns")
    columns, keys = [], []
    if not isinstance(raw_columns, list) or not raw_columns:
        errors.append("At least one column is required.")
    elif len(raw_columns) > MAX_COLUMNS:
        errors.append(f"At most {MAX_COLUMNS} columns are supported.")
    else:
        for i, c in enumerate(raw_columns, start=1):
            key = _text((c or {}).get("key") if isinstance(c, dict) else "", 64).upper()
            if not COLUMN_KEY_RE.match(key):
                errors.append(f"Column {i}: key must be letters/digits/underscore, starting with a letter.")
                continue
            if key in keys:
                errors.append(f"Column key {key} is listed twice.")
                continue
            keys.append(key)
            col = {"key": key, "label": _text(c.get("label"), 80) or key, "numeric": bool(c.get("numeric"))}
            if _text(c.get("example"), 120):
                col["example"] = _text(c.get("example"), 120)
            if _text(c.get("description"), 240):
                col["description"] = _text(c.get("description"), 240)
            columns.append(col)
    out["columns"] = columns

    # ── column references ──
    date_col = _text(config.get("date_column"), 64).upper()
    if date_col and date_col not in keys:
        errors.append(f"date_column {date_col} is not one of the columns.")
    if date_col:
        out["date_column"] = date_col

    date_format = _text(config.get("date_format"), 30)
    if date_format:
        if "%" not in date_format:
            errors.append("date_format must be a Python date format such as %d-%m-%Y.")
        elif not date_col:
            errors.append("date_format needs a date_column.")
        else:
            out["date_format"] = date_format

    restrict_col = _text(config.get("restrict_col"), 64).upper()
    if restrict_col and restrict_col not in keys:
        errors.append(f"restrict_col {restrict_col} is not one of the columns.")
    if restrict_col:
        out["restrict_col"] = restrict_col

    # ── period rule ──
    rule = config.get("period_rule")
    if rule:
        if not isinstance(rule, dict) or rule.get("unit", "month") != "month":
            errors.append("period_rule.unit must be 'month'.")
        else:
            try:
                cutover = int(rule.get("cutover_day", 1))
                if not 1 <= cutover <= 31:
                    raise ValueError
                out["period_rule"] = {"unit": "month", "cutover_day": cutover}
            except (TypeError, ValueError):
                errors.append("period_rule.cutover_day must be a whole number from 1 to 31.")

    if load_strategies.STRATEGIES.get(strategy, {}).get("needs_period"):
        if "period_rule" not in out:
            errors.append(f"{strategy} needs a period rule.")
        if not date_col:
            errors.append(f"{strategy} needs a date_column to know which rows to replace.")

    # ── audit columns (stamped on every inserted row) ──
    audit_in = config.get("audit_columns")
    if audit_in:
        audit = {}
        for field in AUDIT_FIELDS:
            name = _text((audit_in or {}).get(field), 64) if isinstance(audit_in, dict) else ""
            if not name:
                continue
            if not AUDIT_COLUMN_RE.match(name):
                errors.append(f"audit_columns.{field} is not a valid column name.")
            else:
                audit[field] = name
        if audit:
            out["audit_columns"] = audit

    if errors:
        raise ValueError(" ".join(errors))
    return out
