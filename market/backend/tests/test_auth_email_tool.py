"""tools/auth_email.py — exact-match, atomic grant (no network)."""
import importlib.util
from pathlib import Path

import pytest

_P = Path(__file__).resolve().parents[3] / "tools" / "auth_email.py"
_spec = importlib.util.spec_from_file_location("auth_email", _P)
ae = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ae)

ENV = {"SUPABASE_PROJECT_REF": "ref", "SUPABASE_ACCESS_TOKEN": "tok"}
UID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def sql(monkeypatch):
    calls, answers = [], []
    def _run(ref, tok, q):
        calls.append(q)
        return answers.pop(0) if answers else []
    monkeypatch.setattr(ae.sa, "run_sql", _run)
    return calls, answers


def test_underscore_email_is_an_exact_match_not_a_pattern(sql):
    calls, answers = sql
    answers += [[{"id": UID, "tools_enabled": []}], [{"tools_enabled": ["simmer"]}]]
    assert ae.grant(ENV, "j_doe@x.com", "simmer", False) == "granted simmer"
    assert "lower(email) = lower('j_doe@x.com')" in calls[0] and "like" not in calls[0].lower()
    assert "array_append(tools_enabled, 'simmer')" in calls[1] and f"id = '{UID}'" in calls[1]
    assert "not ('simmer' = any(tools_enabled))" in calls[1]


@pytest.mark.parametrize("bad", ["*@x.com", "j%@x.com", "a'b@x.com", "a b@x.com", "nope"])
def test_wildcard_and_unsafe_emails_are_rejected(bad):
    assert not ae._EMAIL.match(bad)


def test_more_than_one_profile_refuses(sql):
    calls, answers = sql
    answers += [[{"id": UID, "tools_enabled": []}, {"id": UID.replace("1", "9"), "tools_enabled": []}]]
    assert ae.grant(ENV, "a@x.com", "market", False).startswith("grant REFUSED")
    assert len(calls) == 1                         # nothing written


def test_grant_by_invite_id_and_already_present(sql):
    calls, answers = sql
    answers += [[{"id": UID, "tools_enabled": ["market"]}]]
    assert ae.grant(ENV, "a@x.com", "market", False, user_id=UID) == "already has market"
    assert f"id = '{UID}'" in calls[0] and len(calls) == 1


def test_no_profile_and_reflex_no_grant(sql):
    assert ae.grant(ENV, "a@x.com", "simmer", False).startswith("grant pending")
    assert ae.grant(ENV, "a@x.com", None, False).startswith("no grant needed")
