"""
EXAMPLE portal hook file — copy to `backend/portals/<portal-id>.py` and edit.

The file name must match the portal id exactly (lowercase, digits, '-' or
'_'). This one starts with an underscore, so it is never loaded for any real
portal. Both functions are optional; delete the one you don't need.
See backend/portal_hooks.py for the full contract.

Hooks are reviewed code that ships through git. Never build SQL by pasting
file values into strings — pass them as `?` parameters.
"""


def prepare(df, ctx):
    """Clean or check the uploaded DataFrame before the built-in validation.

    Column headers are already upper-cased. Return the DataFrame to keep
    going, or raise ValueError("message") to reject the whole file — the
    message is shown to the user as-is.
    """
    # Example: tidy store codes the way the Fabric table stores them.
    df["STORE_CODE"] = df["STORE_CODE"].astype(str).str.strip()

    # Example: refuse a file that lists the same store twice.
    dupes = df["STORE_CODE"][df["STORE_CODE"].duplicated()].unique()
    if len(dupes):
        raise ValueError(f"Duplicate store code(s) in file: {', '.join(dupes[:5])}")

    return df


def before_insert(cursor, ctx):
    """Replace the built-in delete/truncate step for this portal.

    `cursor` is an open Fabric cursor; `ctx["table"]` is the target table and
    `ctx["brands"]` the brands present in the file. Return how many existing
    rows you removed. Remove this function to use the portal's configured
    load strategy instead.
    """
    cursor.execute(f"DELETE FROM {ctx['table']} WHERE [STORE_CODE] IS NULL")
    return max(cursor.rowcount or 0, 0)
