"""_resolve_sweep_seconds: UI/CLI sweep cadence from simmer_settings.sweep_interval.

One global sweep loop, so the MIN sweep_interval (minutes) across all rows wins;
minutes → seconds with a 60s floor; falls back to the config default when
Supabase is off (None) or nothing sets it ([] / all null). asyncio_mode=auto.
"""
from __future__ import annotations

from app import simmer_watcher


async def _run(monkeypatch, rows) -> int:
    monkeypatch.setattr(simmer_watcher.simmer_config, "cadence",
                        lambda: {"sweep_seconds": 300})

    async def _fake_select_many(*a, **k):
        return rows

    monkeypatch.setattr(simmer_watcher.supabase_admin, "select_many",
                        _fake_select_many)
    return await simmer_watcher._resolve_sweep_seconds()


async def test_min_across_rows_minutes_to_seconds(monkeypatch):
    assert await _run(monkeypatch, [{"sweep_interval": 5}, {"sweep_interval": 3}]) == 180


async def test_fallback_when_supabase_unconfigured(monkeypatch):
    assert await _run(monkeypatch, None) == 300


async def test_fallback_when_no_rows(monkeypatch):
    assert await _run(monkeypatch, []) == 300


async def test_floor_is_60_seconds(monkeypatch):
    assert await _run(monkeypatch, [{"sweep_interval": 1}]) == 60


async def test_ignores_null_intervals(monkeypatch):
    assert await _run(monkeypatch, [{"sweep_interval": None}, {"sweep_interval": 4}]) == 240
