"""
Built-in load strategies — how an upload is applied to a portal's target table.

Phase 3 of the portal pipeline (users upload -> admin-defined rules ->
rows land in Fabric). Each portal's config names one strategy in
`load_strategy`; `prepare_target()` runs the strategy's pre-insert step
(delete / truncate / nothing) and the caller then bulk-inserts the staged
rows. A portal can replace that pre-insert step entirely with a
`before_insert` hook (see portal_hooks.py).

  replace_period  Delete rows in the upload period (and the uploaded brands),
                  then insert. Monthly targets.
  truncate_load   Empty the table, then insert. Store master.
  append          Insert only; never deletes. Sales data.
"""

REPLACE_PERIOD = "replace_period"
TRUNCATE_LOAD = "truncate_load"
APPEND = "append"

DEFAULT_STRATEGY = REPLACE_PERIOD

# `needs_period`: config must carry a period_rule and a date_column.
STRATEGIES = {
    REPLACE_PERIOD: {
        "label": "Replace period",
        "needs_period": True,
        "summary": "Deletes existing rows for the upload period (and the brands in the file), then inserts the file.",
    },
    TRUNCATE_LOAD: {
        "label": "Truncate & load",
        "needs_period": False,
        "summary": "Empties the whole table, then inserts the file.",
    },
    APPEND: {
        "label": "Append",
        "needs_period": False,
        "summary": "Inserts the file's rows after the existing rows. Nothing is deleted.",
    },
}


def strategy_of(config: dict) -> str:
    """Strategy named by a portal config. Configs saved before strategies
    existed have no `load_strategy`; they all behaved as replace_period."""
    return (config or {}).get("load_strategy") or DEFAULT_STRATEGY


def check_allowed(strategy: str, restrict_values) -> None:
    """Raise ValueError if this user may not run `strategy`.

    Several brand teams can share one table. truncate_load wipes every
    brand's rows, so a user restricted to certain values must never be able
    to run it. restrict_values is None for admins and unrestricted users.
    """
    if strategy == TRUNCATE_LOAD and restrict_values is not None:
        raise ValueError(
            "This upload replaces the entire table, so it can't be used with a brand "
            "restriction. Ask an administrator to run it."
        )


def prepare_target(strategy: str, cursor, config: dict, period: "dict | None", brands: list,
                   transactional: bool = False) -> int:
    """Run the strategy's pre-insert step. Returns the number of existing
    rows removed (0 for append).

    `transactional=True` is the staged swap: everything here runs inside the
    swap transaction, so truncate_load uses a plain DELETE (which rolls back
    with the rest) instead of TRUNCATE (whose behaviour inside a transaction
    on Fabric isn't something we can count on)."""
    table = config["target_table"]

    if strategy == APPEND:
        return 0

    if strategy == TRUNCATE_LOAD:
        if transactional:
            cursor.execute(f"DELETE FROM {table}")
            return cursor.rowcount if (cursor.rowcount or 0) > 0 else 0
        # TRUNCATE gives no row count, so count first for the audit trail.
        cursor.execute(f"SELECT COUNT(*) FROM {table}")
        existing = int(cursor.fetchone()[0])
        cursor.execute(f"TRUNCATE TABLE {table}")
        return existing

    if strategy == REPLACE_PERIOD:
        date_col = config.get("date_column")
        restrict_col = config.get("restrict_col")
        if not (period and date_col):
            # Validation rejects such a config on save; refuse to silently
            # turn into an append if one slips through.
            raise ValueError("replace_period needs a period rule and a date column.")
        where = [f"[{date_col}] BETWEEN ? AND ?"]
        params = [period["start"], period["end"]]
        if restrict_col and brands:
            where.append(f"[{restrict_col}] IN ({','.join(['?'] * len(brands))})")
            params += brands
        cursor.execute(f"DELETE FROM {table} WHERE {' AND '.join(where)}", params)
        return cursor.rowcount if (cursor.rowcount or 0) > 0 else 0

    raise ValueError(f"Unknown load strategy: {strategy}")
