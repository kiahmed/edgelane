"""Pin the settings route to the simmer_settings columns in migration 0010.

The failure mode this guards: `sanitize_settings` emits a key that is not a
column (or GET selects one). Nothing errors locally — PostgREST 400s the whole
upsert at runtime, so settings silently stop persisting, and the route tests
can't see it because they run without Supabase and assert only the sanitized
echo. Parsing the migration makes the schema itself the test oracle, so any
future rename on either side fails loudly here.
"""
from __future__ import annotations

import re
from pathlib import Path

from app import simmer_config
from app.routes.simmer import sanitize_settings

_MIGRATION = (
    Path(__file__).resolve().parents[3].parents[0]  # market/backend -> repo root
    / "supabase" / "migrations" / "0010_simmer.sql"
)


def _settings_columns() -> set[str]:
    """Column names of public.simmer_settings, parsed from the migration DDL."""
    sql = _MIGRATION.read_text(encoding="utf-8")
    m = re.search(
        r"create table if not exists public\.simmer_settings\s*\((.*?)\n\);",
        sql, re.S | re.I,
    )
    assert m, "simmer_settings CREATE TABLE not found in 0010_simmer.sql"
    cols: set[str] = set()
    _SQL_TYPES = {"uuid", "text", "date", "int", "integer", "boolean", "jsonb",
                  "numeric", "timestamptz", "timestamp", "text[]"}
    for raw in m.group(1).splitlines():
        line = raw.strip()
        if not line or line.startswith("--") or line.lower().startswith("constraint"):
            continue
        parts = line.split()
        # A column line is `name TYPE ...`; CHECK-continuation lines (`and …`,
        # `short_delta_max > …`) have no type token in second position.
        type_tok = parts[1].lower().rstrip(",").split("(")[0] if len(parts) >= 2 else ""
        if type_tok in _SQL_TYPES and re.fullmatch(r"[a-z_][a-z0-9_]*", parts[0]):
            cols.add(parts[0])
    assert "user_id" in cols and len(cols) > 5, f"suspicious parse: {sorted(cols)}"
    return cols


# A payload exercising every settable knob the route supports.
_FULL_PAYLOAD = {
    "min_score": 75,
    "min_iv_percentile": 50,
    "short_delta_min": 0.20,
    "short_delta_max": 0.35,
    "min_dte": 10,
    "max_dte": 40,
    "max_concurrent": 3,
    "structures_enabled": ["bull_put"],
    "regime_strictness": "strict",
    "notify_email": True,
    "gate_overrides": {},
}


def test_sanitize_settings_emits_only_real_columns():
    cols = _settings_columns()
    emitted = set(sanitize_settings(_FULL_PAYLOAD))
    ghosts = emitted - cols
    assert not ghosts, (
        f"sanitize_settings emits key(s) with no simmer_settings column: "
        f"{sorted(ghosts)} — the PostgREST upsert would 400 and nothing "
        f"would persist. Columns: {sorted(cols)}"
    )


def test_get_select_list_only_real_columns():
    cols = _settings_columns()
    src = Path("app/routes/simmer.py").read_text(encoding="utf-8")
    m = re.search(
        r'"simmer_settings",\s*"user_id",[^,]+,\s*((?:"[a-z0-9_,]+"\s*)+)\)',
        src,
    )
    assert m, "GET /simmer/settings select list not found"
    select_cols = set(re.sub(r'[\s"]', "", m.group(1)).strip(",").split(","))
    ghosts = select_cols - cols
    assert not ghosts, (
        f"GET /simmer/settings selects nonexistent column(s): {sorted(ghosts)} "
        f"— PostgREST returns 400 and the endpoint serves an empty object."
    )


def test_user_clamps_keys_are_real_columns():
    cols = _settings_columns()
    ghosts = set(simmer_config.USER_CLAMPS) - cols
    assert not ghosts, (
        f"USER_CLAMPS keys are user-facing and must match simmer_settings "
        f"columns; ghosts: {sorted(ghosts)}"
    )


def test_every_tunable_column_is_clampable_or_validated():
    """Reverse direction: a tunable column added to the migration should get a
    clamp or explicit validation, not silently pass through."""
    handled = set(sanitize_settings(_FULL_PAYLOAD)) | {
        "user_id", "risk_profile", "prefs", "created_at", "updated_at",
    }
    unhandled = _settings_columns() - handled
    assert not unhandled, (
        f"simmer_settings column(s) the route neither clamps nor validates: "
        f"{sorted(unhandled)} — add them to sanitize_settings deliberately."
    )
