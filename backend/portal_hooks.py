"""
Per-portal Python hooks — the admin-authored part of a portal's load logic.

A portal's custom Python lives in `backend/portals/<portal_id>.py` and is
deployed through git, never typed into the browser, so it gets code review
and nothing a user or a compromised admin account can submit is ever
executed. Both functions are optional:

    def prepare(df, ctx):
        # Runs right after the file is parsed (headers already upper-cased),
        # before the built-in column/date/period checks. Return the
        # DataFrame to continue with, or raise ValueError("...") to reject
        # the file with that message.

    def before_insert(cursor, ctx):
        # Runs at commit time inside the Fabric connection. REPLACES the
        # built-in strategy's delete/truncate step (it is skipped when this
        # exists). Return the number of existing rows removed.

`ctx` is a dict: portal_id, config, email, filename, period, restrict_values,
table, and (before_insert only) brands.
"""
import importlib.util
import re
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent / "portals"

PORTAL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

# portal_id -> (mtime, module)
_cache: dict = {}


def valid_portal_id(portal_id: str) -> bool:
    return bool(PORTAL_ID_RE.match(portal_id or ""))


def hook_path(portal_id: str) -> Path:
    # The regex forbids separators and dots, so the id cannot leave HOOKS_DIR.
    if not valid_portal_id(portal_id):
        raise ValueError(
            "Portal id must be lowercase letters, digits, '-' or '_' (max 63 chars)."
        )
    return HOOKS_DIR / f"{portal_id}.py"


def has_hooks(portal_id: str) -> bool:
    try:
        return hook_path(portal_id).is_file()
    except ValueError:
        return False


def load_hooks(portal_id: str):
    """Return the portal's hook module, or None if it has no hook file.
    Re-imported when the file's mtime changes, so a deploy needs no restart
    of already-running workers to pick up an edited hook."""
    path = hook_path(portal_id)
    if not path.is_file():
        _cache.pop(portal_id, None)
        return None

    mtime = path.stat().st_mtime
    cached = _cache.get(portal_id)
    if cached and cached[0] == mtime:
        return cached[1]

    module_name = f"portal_hook_{portal_id.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        sys.modules.pop(module_name, None)
        raise RuntimeError(f"Portal hook file {path.name} failed to load: {e}") from e

    _cache[portal_id] = (mtime, module)
    return module


def run_prepare(portal_id: str, df, ctx: dict):
    """Apply the portal's prepare() hook. No hook (or no prepare) = df as-is."""
    module = load_hooks(portal_id)
    fn = getattr(module, "prepare", None) if module else None
    if fn is None:
        return df
    result = fn(df, ctx)
    # Hooks that edit df in place and return nothing are fine.
    return df if result is None else result


def run_before_insert(portal_id: str, cursor, ctx: dict):
    """Apply the portal's before_insert() hook. Returns rows removed, or None
    if the portal has no such hook (caller then runs the built-in strategy)."""
    module = load_hooks(portal_id)
    fn = getattr(module, "before_insert", None) if module else None
    if fn is None:
        return None
    removed = fn(cursor, ctx)
    return int(removed or 0)
