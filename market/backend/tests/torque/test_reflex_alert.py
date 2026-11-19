"""The Torque page's Reflex arrival alert: the signals feed + its config."""
from app.routes import torque as troute


async def test_signals_feed_returns_latest_with_outcome(monkeypatch):
    rows = [{"source_event_id": "nr-2", "symbol": "SPY", "state": "confirmed", "direction": "bullish",
             "outcome": "placed", "reason": None, "received_at": "2026-11-19T14:31:00Z"}]
    seen = {}
    async def _recent(limit=20):
        seen["limit"] = limit
        return rows
    monkeypatch.setattr(troute.supabase_admin, "get_recent_news_signals", _recent)
    r = await troute.torque_signals(limit=500)
    assert r == {"signals": rows, "ok": True} and seen["limit"] == 50     # capped


async def test_signals_feed_read_failure_is_flagged(monkeypatch):
    async def _none(limit=20):
        return None
    monkeypatch.setattr(troute.supabase_admin, "get_recent_news_signals", _none)
    assert await troute.torque_signals() == {"signals": [], "ok": False}


def test_alert_style_falls_back_to_fanfare(monkeypatch):
    for style, want in (("siren", "siren"), ("OFF", "off"), ("bogus", "fanfare"), ("", "fanfare")):
        s = troute.get_settings().model_copy(update={"torque_reflex_alert": style})
        monkeypatch.setattr(troute, "get_settings", lambda s=s: s)
        assert troute._alert_style() == want
