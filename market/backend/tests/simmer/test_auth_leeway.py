"""JWT clock-skew tolerance — a fresh token whose iat is a few seconds ahead of
the verifier's clock (routine on WSL2/Docker VMs) must still verify. Regression
for the live 'Invalid or expired session' 401 on every sign-in."""
import time
import jwt as _jwt
import pytest
from app import auth


def _hs_token(secret, iat_offset):
    now = int(time.time())
    return _jwt.encode(
        {"sub": "u-1", "email": "a@b.co", "aud": "authenticated",
         "iat": now + iat_offset, "exp": now + 3600},
        secret, algorithm="HS256")


def test_future_iat_within_leeway_ok(monkeypatch):
    from app import config
    s = config.Settings(supabase_jwt_secret="testsecret", supabase_url="https://x.co")
    monkeypatch.setattr(config, "_cached", s)
    # 10s into the future — inside the 30s leeway
    payload = auth._decode(_hs_token("testsecret", 10))
    assert payload["email"] == "a@b.co"


def test_far_future_iat_still_rejected(monkeypatch):
    from app import config
    s = config.Settings(supabase_jwt_secret="testsecret", supabase_url="https://x.co")
    monkeypatch.setattr(config, "_cached", s)
    with pytest.raises(Exception):
        auth._decode(_hs_token("testsecret", 300))   # 5 min — real skew, reject


# ── Rate-limit hardening: /auth/* keys on IP, not the (attacker-controlled) bearer ──
def test_auth_ratelimit_keys_on_ip_not_bearer():
    from types import SimpleNamespace
    from app import ratelimit as rl

    def req(path, bearer, ip):
        return SimpleNamespace(
            url=SimpleNamespace(path=path),
            headers={"authorization": f"Bearer {bearer}", "cf-connecting-ip": ip},
            client=SimpleNamespace(host=ip))

    # A brute-forcer rotating the bearer on /auth/login must land in ONE bucket.
    a = rl._client_key(req("/auth/login", "rotA", "9.9.9.9"))
    b = rl._client_key(req("/auth/login", "rotB", "9.9.9.9"))
    assert a == b == "auth-ip:9.9.9.9"
    # Different IPs are still independent (real users aren't collateral).
    c = rl._client_key(req("/auth/login", "rotA", "8.8.8.8"))
    assert c != a
    # Non-auth paths keep the per-session bearer keying.
    assert rl._client_key(req("/simmer/status", "tok", "9.9.9.9")).startswith("u:")
