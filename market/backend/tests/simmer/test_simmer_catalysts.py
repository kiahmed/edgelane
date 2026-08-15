"""SEC EDGAR catalyst detector — parsing, classification, and persistence.

Everything here runs off checked-in fixtures; nothing touches the network. The
`getcurrent` fixture (`fixtures/current_filings.atom`) is a realistic mixed
sweep containing the cases that actually matter:

  * an after-hours earnings 8-K whose FILING DATE is the next business day
    (Friday 17:32 ET -> filed Monday) — the failure this module exists to stop
  * every hard-block item and form
  * elevated-risk items/forms, plus SC 13G which must stay noise
  * an unlisted registrant (no ticker) that must be skipped, not fatal
  * a junk entry with neither accession nor CIK
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from app import simmer_catalysts as sc

ET = ZoneInfo("America/New_York")
FIXTURES = Path(__file__).parent / "fixtures"


# --- fixtures --------------------------------------------------------------

@pytest.fixture(scope="module")
def atom_text() -> str:
    return (FIXTURES / "current_filings.atom").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def filings(atom_text) -> list[sc.EdgarFiling]:
    return sc.parse_atom_feed(atom_text)


@pytest.fixture(scope="module")
def tickers_payload() -> dict:
    return json.loads((FIXTURES / "company_tickers.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def ticker_map(tickers_payload) -> dict[str, list[str]]:
    return sc.parse_company_tickers(tickers_payload)


def by_acc(filings: list[sc.EdgarFiling], accession: str) -> sc.EdgarFiling:
    for f in filings:
        if f.accession == accession:
            return f
    raise AssertionError(f"accession {accession} not in fixture")


# --- Atom parsing ----------------------------------------------------------

def test_feed_parses_every_real_entry_and_drops_junk(filings):
    # 10 <entry> elements; the last one has no accession and no CIK.
    assert len(filings) == 9
    assert all(f.accession or f.cik for f in filings)


def test_item_numbers_come_from_the_summary(filings):
    aapl = by_acc(filings, "0000320193-26-000081")
    assert aapl.items == ("2.02", "9.01")
    assert aapl.form_type == "8-K"
    assert aapl.company == "Apple Inc."
    # No document was fetched to learn any of that.
    assert aapl.link.endswith("-index.htm")


def test_item_extraction_handles_no_items_and_multiple(filings):
    assert by_acc(filings, "0001777001-26-000003").items == ()      # NT 10-Q
    assert by_acc(filings, "0001899001-26-000012").items == ("4.02", "8.01")


@pytest.mark.parametrize("summary,expected", [
    ("<br>Item 2.02: Results of Operations", ("2.02",)),
    ("Item 2.02: x<br>Item 9.01: y<br>Item 2.02: dup", ("2.02", "9.01")),
    ("<b>Filed:</b> 2026-08-17 <b>AccNo:</b> 0000320193-26-000081", ()),
    ("", ()),
    (None, ()),
])
def test_parse_summary_items(summary, expected):
    assert sc.parse_summary_items(summary) == expected


def test_normalize_item_pads_the_minor_number():
    assert sc.normalize_item("2.2") == "2.02"
    assert sc.normalize_item(" Item 4.02 ") == "4.02"
    assert sc.normalize_item("9.01") == "9.01"
    assert sc.normalize_item("garbage") == ""
    assert sc.normalize_item(None) == ""


def test_malformed_xml_raises_structured_error():
    with pytest.raises(sc.EdgarError):
        sc.parse_atom_feed("<feed><entry></feed>")


def test_empty_feed_is_empty_not_an_error():
    assert sc.parse_atom_feed("") == []
    assert sc.parse_atom_feed("   ") == []


# --- Acceptance vs filing date (the whole point) ---------------------------

def test_acceptance_not_filing_date_for_after_hours_8k(filings):
    """Friday 17:32 ET acceptance; EDGAR stamps filingDate = Monday.

    Keying on filing_date would file tonight's earnings 8-K under Monday and
    let a spread ride through the Monday-open gap it causes.
    """
    aapl = by_acc(filings, "0000320193-26-000081")

    assert aapl.acceptance_at == datetime(2026, 8, 14, 17, 32, 14, tzinfo=ET)
    assert aapl.filing_date == date(2026, 8, 17)          # THREE days later
    assert aapl.acceptance_et.date() != aapl.filing_date  # the skew, explicitly

    assert sc.session_for(aapl.acceptance_at) == "amc"
    # First session that can price it: Monday (Friday PM + weekend).
    assert sc.impact_session_date(aapl.acceptance_at) == date(2026, 8, 17)


def test_catalyst_event_date_is_the_acceptance_date(filings, ticker_map):
    aapl = by_acc(filings, "0000320193-26-000081")
    (cat,) = sc.filing_to_catalysts(aapl, ["AAPL"])
    assert cat.event_date == date(2026, 8, 14)            # acceptance, not filed
    assert cat.detail_obj["filing_date"] == "2026-08-17"  # diagnostic only
    assert cat.detail_obj["impact_date"] == "2026-08-17"
    assert cat.session == "amc"
    assert cat.confirmed is True


@pytest.mark.parametrize("hhmm,session", [
    ((6, 5), "bmo"),
    ((9, 29), "bmo"),
    ((9, 30), "dmh"),
    ((11, 15), "dmh"),
    ((15, 59), "dmh"),
    ((16, 0), "amc"),
    ((17, 25), "amc"),   # the earnings-wave cluster
    ((17, 32), "amc"),
    ((21, 0), "amc"),
])
def test_session_for(hhmm, session):
    dt = datetime(2026, 8, 12, hhmm[0], hhmm[1], tzinfo=ET)  # a Wednesday
    assert sc.session_for(dt) == session


def test_session_and_impact_handle_the_weekend():
    sat = datetime(2026, 8, 15, 10, 0, tzinfo=ET)
    assert sc.session_for(sat) == "amc"
    assert sc.impact_session_date(sat) == date(2026, 8, 17)
    wed_bmo = datetime(2026, 8, 12, 7, 0, tzinfo=ET)
    assert sc.impact_session_date(wed_bmo) == date(2026, 8, 12)   # same session


def test_session_for_unknown_timestamp():
    assert sc.session_for(None) == "unknown"
    assert sc.impact_session_date(None) is None


def test_atom_offset_is_honored_and_converted_to_eastern():
    dt = sc.parse_edgar_datetime("2026-08-14T21:32:14+00:00")
    assert dt == datetime(2026, 8, 14, 21, 32, 14, tzinfo=timezone.utc)
    assert sc._to_eastern(dt).hour == 17
    assert sc.session_for(dt) == "amc"


def test_acceptance_datetime_z_suffix_is_eastern_not_utc():
    """data.sec.gov stamps `Z` on a value that is actually Eastern.

    Reading 17:32Z as UTC would move the filing to 13:32 ET and reclassify an
    AMC gap-maker as a mid-session non-event.
    """
    dt = sc.parse_acceptance_datetime("2026-08-14T17:32:14.000Z")
    assert dt == datetime(2026, 8, 14, 17, 32, 14, tzinfo=ET)
    assert sc.session_for(dt) == "amc"
    assert sc.parse_acceptance_datetime(None) is None
    assert sc.parse_acceptance_datetime("junk") is None
    # A real numeric offset is still respected.
    assert sc.parse_acceptance_datetime("2026-08-14T21:32:14+00:00").hour == 17


def test_naive_timestamps_are_treated_as_eastern():
    dt = sc.parse_edgar_datetime("2026-08-14 17:32:14")
    assert dt == datetime(2026, 8, 14, 17, 32, 14, tzinfo=ET)
    assert sc.parse_edgar_datetime("") is None
    assert sc.parse_edgar_datetime(None) is None
    assert sc.parse_edgar_datetime("not-a-date") is None


# --- CIK handling ----------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (320193, "0000320193"),          # company_tickers.json ships an INT
    ("320193", "0000320193"),
    ("0000320193", "0000320193"),
    ("CIK0000320193", "0000320193"),
    (1652044, "0001652044"),
    (1, "0000000001"),
    (None, ""),
    ("", ""),
    ("not-a-cik", ""),
])
def test_pad_cik(raw, expected):
    assert sc.pad_cik(raw) == expected


def test_ticker_map_zero_pads_and_keeps_share_classes(ticker_map):
    assert ticker_map["0000320193"] == ["AAPL"]
    assert ticker_map["0001652044"] == ["GOOGL", "GOOG"]   # order preserved
    assert all(len(k) == 10 and k.isdigit() for k in ticker_map)


def test_ticker_map_accepts_a_list_payload():
    mapping = sc.parse_company_tickers(
        [{"cik_str": 320193, "ticker": "aapl", "title": "Apple Inc."}]
    )
    assert mapping == {"0000320193": ["AAPL"]}
    assert sc.parse_company_tickers(None) == {}
    assert sc.parse_company_tickers("junk") == {}


def test_feed_ciks_are_padded_from_the_title(filings):
    assert by_acc(filings, "0000789019-26-000031").cik == "0000789019"
    assert by_acc(filings, "0009999001-26-000002").cik == "0009999001"


# --- Classification --------------------------------------------------------

@pytest.mark.parametrize("item", ["4.02", "1.03", "3.01", "4.01"])
def test_hard_block_items(item):
    cls = sc.classify("8-K", [item])
    assert cls.blocking is True
    assert cls.severity == "block"
    assert item in cls.block_codes


@pytest.mark.parametrize("form", ["NT 10-K", "NT 10-Q", "424B4", "424B5"])
def test_hard_block_forms(form):
    cls = sc.classify(form, [])
    assert cls.blocking is True
    assert form in cls.block_codes


@pytest.mark.parametrize("item", ["1.01", "1.02", "2.01", "2.05", "2.06",
                                  "5.02", "7.01", "8.01"])
def test_elevated_items_do_not_block(item):
    cls = sc.classify("8-K", [item])
    assert cls.blocking is False
    assert cls.elevated is True
    assert cls.severity == "elevated"


@pytest.mark.parametrize("form", ["SC 13D", "SC TO-T", "SC 14D9",
                                  "PREM14A", "DEFM14A"])
def test_elevated_forms_do_not_block(form):
    cls = sc.classify(form, [])
    assert (cls.blocking, cls.elevated) == (False, True)


def test_sc_13g_is_passive_noise():
    cls = sc.classify("SC 13G", [])
    assert (cls.blocking, cls.elevated, cls.severity) == (False, False, "info")


def test_item_202_alone_is_neither_blocking_nor_elevated():
    """2.02 is a scheduled, priced event handled by the earnings layer — the
    filing itself is ground truth, not a hard block."""
    cls = sc.classify("8-K", ["2.02", "9.01"])
    assert (cls.blocking, cls.elevated) == (False, False)


def test_blocking_wins_over_elevated_and_is_reported_first():
    cls = sc.classify("8-K", ["8.01", "4.02"])
    assert cls.blocking is True
    assert cls.elevated is True
    assert cls.block_codes == ("4.02",)
    assert cls.elevated_codes == ("8.01",)
    assert cls.reasons[0].startswith("Item 4.02")


def test_amended_hard_block_form_still_blocks():
    assert sc.classify("NT 10-K/A", []).blocking is True
    assert sc.normalize_form("NT10-Q") == "NT 10-Q"
    assert sc.classify("nt 10-q", []).blocking is True


def test_unknown_form_and_items_are_info():
    cls = sc.classify("10-Q", ["9.01"])
    assert (cls.blocking, cls.elevated, cls.severity) == (False, False, "info")
    assert sc.classify(None, None).severity == "info"


def test_classification_of_the_real_feed(filings):
    verdicts = {f.accession: sc.classify_filing(f) for f in filings}
    assert verdicts["0001899001-26-000012"].blocking is True   # 4.02
    assert verdicts["0001777001-26-000003"].blocking is True   # NT 10-Q
    assert verdicts["0001555001-26-000045"].blocking is True   # 424B5
    assert verdicts["0000789019-26-000031"].elevated is True   # 7.01
    assert verdicts["0001444001-26-000009"].elevated is True   # SC 13D
    assert verdicts["0001333001-26-000021"].severity == "info"  # SC 13G
    assert verdicts["0000320193-26-000081"].severity == "info"  # 2.02 earnings


# --- Filing -> catalyst rows ----------------------------------------------

def test_unknown_cik_is_skipped_not_fatal(filings, ticker_map):
    cats = sc.filings_to_catalysts(filings, ticker_map)
    symbols = {c.symbol for c in cats}
    # The municipal trust (CIK 0009999001) has no ticker and simply vanishes.
    assert "0009999001" not in ticker_map
    assert symbols == {"AAPL", "GOOGL", "GOOG", "MSFT", "NSPH", "LATE",
                       "SHLF", "ACTV", "PASV"}
    assert all(c.symbol for c in cats)


def test_empty_ticker_map_yields_no_rows_and_no_crash(filings):
    assert sc.filings_to_catalysts(filings, {}) == []
    assert sc.filings_to_catalysts([], {"0000320193": ["AAPL"]}) == []
    assert sc.filings_to_catalysts([None, "junk"], {"0000320193": ["AAPL"]}) == []


def test_catalyst_id_is_the_accession_number(filings, ticker_map):
    cats = sc.filings_to_catalysts(filings, ticker_map)
    aapl = next(c for c in cats if c.symbol == "AAPL")
    assert aapl.id == "0000320193-26-000081"


def test_share_classes_get_distinct_ids(filings, ticker_map):
    cats = [c for c in sc.filings_to_catalysts(filings, ticker_map)
            if c.symbol in ("GOOG", "GOOGL")]
    assert len(cats) == 2
    assert {c.id for c in cats} == {"0001652044-26-000064:GOOGL",
                                    "0001652044-26-000064:GOOG"}


def test_blocking_flag_and_detail_payload(filings, ticker_map):
    cats = {c.symbol: c for c in sc.filings_to_catalysts(filings, ticker_map)}
    assert cats["NSPH"].blocking is True
    assert cats["LATE"].blocking is True
    assert cats["SHLF"].blocking is True
    assert cats["MSFT"].blocking is False
    assert cats["PASV"].blocking is False

    detail = cats["NSPH"].detail_obj
    assert detail["form"] == "8-K"
    assert detail["items"] == ["4.02", "8.01"]
    assert detail["blocking_codes"] == ["4.02"]
    assert detail["severity"] == "block"
    assert detail["cik"] == "0001899001"
    assert cats["NSPH"].event_type == "sec_filing"
    assert cats["NSPH"].source == "sec_edgar"


def test_include_info_false_keeps_only_actionable_rows(filings, ticker_map):
    cats = sc.filings_to_catalysts(filings, ticker_map, include_info=False)
    symbols = {c.symbol for c in cats}
    assert symbols == {"NSPH", "LATE", "SHLF", "MSFT", "ACTV"}
    assert "PASV" not in symbols     # SC 13G filtered
    assert "AAPL" not in symbols     # plain 2.02 is not a risk row


# --- Earnings ground truth -------------------------------------------------

def test_is_earnings_filing(filings):
    assert sc.is_earnings_filing(by_acc(filings, "0000320193-26-000081")) is True
    assert sc.is_earnings_filing(by_acc(filings, "0000789019-26-000031")) is False


def test_earnings_confirmations_expose_the_repairable_date(filings, ticker_map):
    confs = sc.earnings_confirmations(filings, ticker_map)
    by_sym = {c.symbol: c for c in confs}
    assert set(by_sym) == {"AAPL", "GOOGL", "GOOG"}

    aapl = by_sym["AAPL"]
    assert aapl.accession == "0000320193-26-000081"
    assert aapl.session == "amc"
    assert aapl.event_date == date(2026, 8, 14)     # it happened Friday PM
    assert aapl.impact_date == date(2026, 8, 17)    # it prices Monday


def test_earnings_confirmations_skip_unknown_ciks(filings):
    assert sc.earnings_confirmations(filings, {}) == []


# --- data.sec.gov submissions (backfill path) ------------------------------

def test_parse_submissions_columnar():
    payload = json.loads(
        (FIXTURES / "submissions_CIK0000320193.json").read_text(encoding="utf-8")
    )
    rows = sc.parse_submissions(payload)
    assert len(rows) == 3
    assert [r.form_type for r in rows] == ["8-K", "10-Q", "8-K"]
    assert rows[0].cik == "0000320193"              # "320193" in the file
    assert rows[0].items == ("2.02", "9.01")        # comma-separated string
    assert rows[1].items == ()
    assert rows[2].items == ("5.02",)
    assert rows[0].company == "Apple Inc."


def test_parse_submissions_keys_on_acceptance_datetime():
    payload = json.loads(
        (FIXTURES / "submissions_CIK0000320193.json").read_text(encoding="utf-8")
    )
    first = sc.parse_submissions(payload)[0]
    # Same skew as the Atom feed: acceptanceDateTime is Friday 21:32Z = 17:32 ET,
    # filingDate is the following Monday.
    assert first.acceptance_et.date() == date(2026, 8, 14)
    assert first.filing_date == date(2026, 8, 17)
    assert sc.session_for(first.acceptance_at) == "amc"


def test_parse_submissions_limit_and_bad_payloads():
    payload = json.loads(
        (FIXTURES / "submissions_CIK0000320193.json").read_text(encoding="utf-8")
    )
    assert len(sc.parse_submissions(payload, limit=2)) == 2
    assert sc.parse_submissions({}) == []
    assert sc.parse_submissions(None) == []
    assert sc.parse_submissions({"filings": {"recent": None}}) == []


# --- Persistence -----------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    from app.db import Database
    d = Database(tmp_path / "simmer_test.duckdb")
    d.connect()
    yield d
    d.close()


def test_persist_writes_rows(db, filings, ticker_map):
    cats = sc.filings_to_catalysts(filings, ticker_map)
    written = sc.persist_catalysts(db, cats)
    assert written == len(cats)

    conn = db.connect()
    with db._lock:
        n, = conn.execute("SELECT count(*) FROM simmer_catalysts").fetchone()
        row = conn.execute(
            "SELECT symbol, blocking, session, event_date, acceptance_at, source "
            "FROM simmer_catalysts WHERE id = ?",
            ["0001899001-26-000012"],
        ).fetchone()
    assert n == len(cats)
    symbol, blocking, session, event_date, acceptance_at, source = row
    assert (symbol, blocking, session, source) == ("NSPH", True, "amc", "sec_edgar")
    assert event_date == date(2026, 8, 14)
    # Stored naive, on the ET clock — the same clock as the market it describes.
    assert acceptance_at == datetime(2026, 8, 14, 18, 2, 11)


def test_persist_is_idempotent_across_overlapping_sweeps(db, filings, ticker_map):
    cats = sc.filings_to_catalysts(filings, ticker_map)
    sc.persist_catalysts(db, cats)
    sc.persist_catalysts(db, cats)          # feed windows deliberately overlap
    conn = db.connect()
    with db._lock:
        n, = conn.execute("SELECT count(*) FROM simmer_catalysts").fetchone()
    assert n == len(cats)


def test_persist_empty_is_a_noop(db):
    assert sc.persist_catalysts(db, []) == 0
    assert sc.persist_catalysts(db, None) == 0


async def test_persist_async_and_readback(db, filings, ticker_map):
    cats = sc.filings_to_catalysts(filings, ticker_map)
    assert await sc.persist_catalysts_async(db, cats) == len(cats)

    # Wide lookback: the fixture is dated and the readback filters on
    # acceptance_at, not event_date.
    rows = sc.fetch_recent_catalysts(db, "NSPH", lookback_days=100_000)
    assert [r["id"] for r in rows] == ["0001899001-26-000012"]
    assert rows[0]["blocking"] is True

    blocking_only = sc.fetch_recent_catalysts(db, "PASV", lookback_days=100_000,
                                              blocking_only=True)
    assert blocking_only == []


# --- Client: compliance + wiring (httpx MockTransport, no network) ---------

def test_user_agent_declares_edgelane_and_a_contact():
    assert sc.build_user_agent("ops@example.com") == "EdgeLane ops@example.com"
    # Never send an empty UA — SEC 403s undeclared tools.
    assert sc.build_user_agent("").startswith("EdgeLane ")
    assert sc.build_user_agent(None).startswith("EdgeLane ")


def test_contact_email_is_configurable_with_fallbacks():
    class S:
        sec_contact_email = "edgar@edgelane.test"
        support_email = "help@edgelane.test"
    assert sc.resolve_contact_email(S()) == "edgar@edgelane.test"

    class S2:
        sec_contact_email = ""
        support_email = "help@edgelane.test"
    assert sc.resolve_contact_email(S2()) == "help@edgelane.test"

    class S3:
        contact_from_email = "EdgeLane <onboarding@resend.dev>"
    assert sc.resolve_contact_email(S3()) == "onboarding@resend.dev"

    class S4:
        pass
    assert sc.resolve_contact_email(S4()) == ""


async def test_client_sends_required_compliance_headers():
    client = sc.EdgarClient(contact_email="ops@example.com")
    try:
        h = (await client._get_client()).headers
        assert h["User-Agent"] == "EdgeLane ops@example.com"
        assert "gzip" in h["Accept-Encoding"]
    finally:
        await client.close()


async def test_rate_limiter_caps_throughput():
    limiter = sc._RateLimiter(rps=3)
    t0 = time.monotonic()
    for _ in range(6):                      # 2 seconds' worth at 3/s
        await limiter.acquire()
    assert time.monotonic() - t0 >= 0.9
    assert sc._RateLimiter(rps=99).rps == 99   # cap is applied by EdgarClient


def test_client_never_exceeds_the_sec_ceiling():
    assert sc.EdgarClient(contact_email="x@y.z", max_rps=1000)._limiter.rps == 10.0
    assert sc.MAX_REQUESTS_PER_SEC == 10.0


def _mock_client(handler, **kw) -> sc.EdgarClient:
    c = sc.EdgarClient(contact_email="ops@example.com", **kw)
    c._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"User-Agent": c.user_agent, "Accept-Encoding": "gzip, deflate"},
    )
    return c


async def test_fetch_and_sweep_end_to_end(db, atom_text, tickers_payload):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        assert request.headers["User-Agent"].startswith("EdgeLane ")
        assert "gzip" in request.headers["Accept-Encoding"]
        if "browse-edgar" in request.url.path:
            assert request.url.params["action"] == "getcurrent"
            assert request.url.params["output"] == "atom"
            return httpx.Response(200, text=atom_text)
        if "company_tickers" in request.url.path:
            return httpx.Response(200, json=tickers_payload)
        return httpx.Response(404, text="no")

    client = _mock_client(handler)
    try:
        result = await sc.sweep_current_filings(client, db, include_info=False)
    finally:
        await client.close()

    assert result.fetched == 9
    assert result.unmapped == 1                 # the municipal trust
    assert result.blocking == 3                 # 4.02, NT 10-Q, 424B5
    assert result.elevated == 2                 # 7.01, SC 13D
    assert result.persisted == result.mapped == 5
    assert {c.symbol for c in result.earnings} == {"AAPL", "GOOGL", "GOOG"}

    # Ticker map is cached: a second sweep must not refetch it.
    before = len(seen)
    client2 = _mock_client(handler)
    try:
        client2._ticker_map = client._ticker_map
        client2._ticker_map_at = time.time()
        await sc.sweep_current_filings(client2, None)
    finally:
        await client2.close()
    assert sum(1 for u in seen[before:] if "company_tickers" in u) == 0


async def test_403_undeclared_tool_raises_rate_limit_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, text="Your Request Originates from an Undeclared Automated Tool",
            headers={"Retry-After": "600"},
        )

    client = _mock_client(handler)
    try:
        with pytest.raises(sc.EdgarRateLimitError) as ei:
            await client.fetch_current_filings()
    finally:
        await client.close()
    assert ei.value.retry_after_sec == 600.0


async def test_backfill_unlisted_symbol_returns_empty(tickers_payload):
    def handler(request: httpx.Request) -> httpx.Response:
        if "company_tickers" in request.url.path:
            return httpx.Response(200, json=tickers_payload)
        raise AssertionError("must not hit submissions for an unlisted symbol")

    client = _mock_client(handler)
    try:
        assert await client.backfill_symbol("SPX") == []
        assert await client.resolve_cik("AAPL") == "0000320193"
    finally:
        await client.close()


async def test_backfill_symbol_uses_the_padded_submissions_path(tickers_payload):
    payload = json.loads(
        (FIXTURES / "submissions_CIK0000320193.json").read_text(encoding="utf-8")
    )
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.path)
        if "company_tickers" in request.url.path:
            return httpx.Response(200, json=tickers_payload)
        return httpx.Response(200, json=payload)

    client = _mock_client(handler)
    try:
        cats = await client.backfill_symbol("AAPL")
    finally:
        await client.close()

    assert "/submissions/CIK0000320193.json" in hits   # zero-padded to 10 digits
    assert [c.symbol for c in cats] == ["AAPL", "AAPL", "AAPL"]
    assert cats[0].event_date == date(2026, 8, 14)     # acceptance, not filed
    assert cats[2].detail_obj["elevated_codes"] == ["5.02"]


async def test_bad_ticker_map_payload_is_a_structured_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = _mock_client(handler)
    try:
        with pytest.raises(sc.EdgarError, match="0 entries"):
            await client.fetch_ticker_map()
    finally:
        await client.close()


async def test_server_error_retries_once_then_raises(monkeypatch):
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, text="unavailable")

    real_sleep = asyncio.sleep

    async def _fast(_seconds):
        await real_sleep(0)               # keep the backoff instant

    monkeypatch.setattr(asyncio, "sleep", _fast)
    client = _mock_client(handler)
    try:
        with pytest.raises(sc.EdgarError):
            await client.fetch_current_filings()
    finally:
        await client.close()
    assert len(calls) == 2                # original + one retry
