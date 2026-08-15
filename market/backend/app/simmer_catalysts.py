"""SEC EDGAR catalyst detector — the hard gate for Simmer.

Authority: `docs/simmer.md`, "Catalyst detection — the hard gate".

Selling a credit spread through an unpriced binary event is the fastest way to
lose more than the position was ever going to earn. This module is the veto: it
watches EDGAR in near-real-time, maps filings to tickers, and classifies them
into HARD BLOCK (refuse the position outright) vs ELEVATED RISK (down-weight).

Three things about this module are load-bearing and easy to get wrong:

1. ⚠️ **Key on acceptance time, NOT filing date.**
   An 8-K accepted at 17:25 ET carries the *NEXT* business day's `filingDate`.
   The 17:25–17:30 ET cluster is exactly the earnings-8-K wave that gaps the
   underlying at tomorrow's open. Filing-date logic therefore misses precisely
   the filings that matter most, and does so silently. Everything here keys on
   the Atom `<updated>` element / `acceptanceDateTime`, and `filing_date` is
   carried only as a diagnostic. See `EdgarFiling.acceptance_at` and
   `impact_session_date()`.

2. **Item numbers come free from the feed.** The `getcurrent` Atom `<summary>`
   spells the 8-K Items out in plain English:
       <b>Filed:</b> 2026-08-15 <b>AccNo:</b> 0001104659-26-097364
       <br>Item 2.02: Results of Operations and Financial Condition
   No document fetching, no XBRL, no full-text search. `efts.sec.gov` lags by
   hours and is backfill-only — never live alerting.

3. **Compliance is non-negotiable.** ≤10 requests/second, a declared
   `User-Agent: EdgeLane <contact-email>`, and `Accept-Encoding: gzip`. Getting
   this wrong gets the IP blocked, not throttled. `data.sec.gov` also does not
   support CORS, which is why this lives in the backend and not the browser.

Layering follows the house style (see `tradier_client.py`): every parser here is
a pure function over text/JSON so the whole classification path is testable
without a network, and `EdgarClient` is a thin async-httpx shell with structured
errors on top of them.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import httpx


# --- Endpoints -------------------------------------------------------------

SEC_WWW_BASE = "https://www.sec.gov"
SEC_DATA_BASE = "https://data.sec.gov"

#: Live feed. `getcurrent` is the only EDGAR surface with sub-minute latency.
CURRENT_FEED_PATH = "/cgi-bin/browse-edgar"

#: CIK→ticker map. 10,391 entries as of 2026-08; ~1 MB, so it is cached.
COMPANY_TICKERS_URL = f"{SEC_WWW_BASE}/files/company_tickers.json"

#: Per-company backfill. Columnar `filings.recent` with a parallel `items` array.
SUBMISSIONS_URL_TMPL = SEC_DATA_BASE + "/submissions/CIK{cik10}.json"

SOURCE = "sec_edgar"
EASTERN = ZoneInfo("America/New_York")

#: SEC's published ceiling. We stay strictly at or under it.
MAX_REQUESTS_PER_SEC = 10.0

#: The map changes on the order of once a day (new listings, ticker changes).
TICKER_MAP_TTL_SEC = 24 * 3600


# --- Classification tables -------------------------------------------------
#
# HARD BLOCK: refuse the position. These are not "risky", they are regime
# changes in the underlying's price distribution. A short premium position
# through any of them is uncompensated.

HARD_BLOCK_ITEMS: frozenset[str] = frozenset({
    "4.02",   # Non-reliance on previously issued financials (restatement). −20%+ gaps.
    "1.03",   # Bankruptcy or receivership.
    "3.01",   # Delisting notice / failure to satisfy a listing rule.
    "4.01",   # Change of certifying accountant (auditor resignation).
})

#: Form types that block on their own, regardless of any Item numbers.
#: NT 10-K / NT 10-Q are SEVERELY underrated: a late-filing notification very
#: often precedes a 4.02 restatement or a 3.01 delisting by days.
#: 424B4 / 424B5 are priced offerings / shelf takedowns — they file after hours
#: and gap small caps overnight.
HARD_BLOCK_FORMS: frozenset[str] = frozenset({
    "NT 10-K", "NT 10-Q", "424B4", "424B5",
})

#: Down-weight, do not block.
ELEVATED_ITEMS: frozenset[str] = frozenset({
    "1.01", "1.02",   # Entry into / termination of a material agreement
    "2.01",           # Completion of an acquisition or disposition
    "2.05", "2.06",   # Exit/disposal costs; material impairment
    "5.02",           # Departure/election of directors or principal officers
    "7.01",           # Reg FD — very often guidance
    "8.01",           # Other events: biotech readouts and legal outcomes land here
})

#: SC 13G is deliberately absent — passive stake, mostly noise. SC 13D is an
#: activist and is not.
ELEVATED_FORMS: frozenset[str] = frozenset({
    "SC 13D",                # activist stake
    "SC TO-T", "SC 14D9",    # tender offer / target response
    "PREM14A", "DEFM14A",    # merger proxy
})

#: Item 2.02 is GROUND TRUTH that earnings actually happened — the one fact in
#: this whole domain that isn't a vendor estimate. The earnings-calendar layer
#: uses it to repair vendor dates (see `earnings_confirmations`).
EARNINGS_ITEM = "2.02"

#: Human-readable reasons, for the `detail` JSON and operator-facing messages.
ITEM_LABELS: dict[str, str] = {
    "1.01": "Entry into a material definitive agreement",
    "1.02": "Termination of a material definitive agreement",
    "1.03": "Bankruptcy or receivership",
    "2.01": "Completion of acquisition or disposition of assets",
    "2.02": "Results of operations and financial condition (earnings)",
    "2.05": "Costs associated with exit or disposal activities",
    "2.06": "Material impairments",
    "3.01": "Notice of delisting or failure to satisfy a listing rule",
    "4.01": "Changes in registrant's certifying accountant",
    "4.02": "Non-reliance on previously issued financial statements",
    "5.02": "Departure/election of directors or principal officers",
    "7.01": "Regulation FD disclosure",
    "8.01": "Other events",
}

FORM_LABELS: dict[str, str] = {
    "NT 10-K": "Late-filing notification (annual)",
    "NT 10-Q": "Late-filing notification (quarterly)",
    "424B4": "Priced offering prospectus",
    "424B5": "Shelf takedown prospectus",
    "SC 13D": "Activist stake disclosure",
    "SC TO-T": "Third-party tender offer",
    "SC 14D9": "Tender offer solicitation/recommendation",
    "PREM14A": "Preliminary merger proxy",
    "DEFM14A": "Definitive merger proxy",
}


# --- Exceptions ------------------------------------------------------------

class EdgarError(RuntimeError):
    """Base class for all SEC EDGAR client errors."""


class EdgarTimeoutError(EdgarError):
    """Network timeout (read or connect)."""


class EdgarRateLimitError(EdgarError):
    """403/429 — EDGAR throttled or blocked us.

    SEC returns 403 with an HTML body ("Your Request Originates from an
    Undeclared Automated Tool") when the User-Agent is missing or the rate cap
    was exceeded. Treat it as a hard stop, not a retryable blip: retrying an
    undeclared-tool 403 is what escalates a throttle into an IP block.
    """

    def __init__(self, message: str, retry_after_sec: float = 0.0):
        super().__init__(message)
        self.retry_after_sec = retry_after_sec


# --- Data model ------------------------------------------------------------

@dataclass(frozen=True)
class EdgarFiling:
    """One filing, normalized. Produced by the pure parsers below."""

    accession: str                       # 0001104659-26-097364
    cik: str                             # zero-padded to 10 digits, always
    form_type: str                       # "8-K", "NT 10-Q", "424B5", ...
    #: AUTHORITATIVE timestamp. Timezone-aware (America/New_York) whenever the
    #: source supplied an offset. Never substitute filing_date for this.
    acceptance_at: datetime | None = None
    #: Diagnostic only. For an after-hours filing this is TOMORROW. Comparing it
    #: against "today" is the bug this module exists to prevent.
    filing_date: date | None = None
    items: tuple[str, ...] = ()          # ("2.02", "9.01")
    company: str = ""
    link: str = ""

    @property
    def acceptance_et(self) -> datetime | None:
        """`acceptance_at` in Eastern time (naive-safe: assumes ET if naive)."""
        return _to_eastern(self.acceptance_at)


@dataclass(frozen=True)
class Classification:
    """Verdict for one filing."""

    blocking: bool
    elevated: bool
    reasons: tuple[str, ...] = ()        # human-readable, block reasons first
    block_codes: tuple[str, ...] = ()    # item numbers / form types that blocked
    elevated_codes: tuple[str, ...] = ()

    @property
    def severity(self) -> str:
        if self.blocking:
            return "block"
        if self.elevated:
            return "elevated"
        return "info"


@dataclass(frozen=True)
class Catalyst:
    """A `simmer_catalysts` row, ready to persist."""

    id: str                              # accession number (see `catalyst_id`)
    symbol: str
    event_type: str = "sec_filing"
    event_date: date | None = None       # ET calendar date of ACCEPTANCE
    session: str = "unknown"             # bmo | dmh | amc | unknown
    confirmed: bool = True               # a filing is a fact, never an estimate
    blocking: bool = False
    detail: str = "{}"                   # JSON
    source: str = SOURCE
    acceptance_at: datetime | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "event_type": self.event_type,
            "event_date": self.event_date,
            "session": self.session,
            "confirmed": self.confirmed,
            "blocking": self.blocking,
            "detail": self.detail,
            "source": self.source,
            # DuckDB TIMESTAMP is naive; normalize to ET so every row in the
            # table is on the same clock as the market it describes.
            "acceptance_at": _naive_eastern(self.acceptance_at),
        }

    @property
    def detail_obj(self) -> dict[str, Any]:
        try:
            return json.loads(self.detail)
        except (TypeError, ValueError):
            return {}


@dataclass(frozen=True)
class EarningsConfirmation:
    """Item 2.02 sighting — proof that earnings happened, for calendar repair.

    `impact_date` is the session the market can first react in: an AMC filing
    moves tomorrow's open, not today's close.
    """

    symbol: str
    accession: str
    acceptance_at: datetime | None
    event_date: date | None              # ET date of acceptance
    impact_date: date | None             # session that actually gaps
    session: str                         # bmo | dmh | amc | unknown


# --- Time helpers ----------------------------------------------------------

def _to_eastern(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        # EDGAR timestamps without an offset are Eastern by convention.
        return dt.replace(tzinfo=EASTERN)
    return dt.astimezone(EASTERN)


def _naive_eastern(dt: datetime | None) -> datetime | None:
    et = _to_eastern(dt)
    return et.replace(tzinfo=None) if et is not None else None


def parse_edgar_datetime(raw: str | None) -> datetime | None:
    """Parse an EDGAR timestamp into an aware datetime.

    Handles the three shapes EDGAR emits:
      * Atom `<updated>`:            2026-08-14T17:25:31-04:00
      * acceptanceDateTime (JSON):   2026-08-14T17:25:31.000Z
      * legacy space-separated:      2026-08-14 17:25:31
    Naive results are tagged Eastern (EDGAR's house timezone).
    """
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    s = s.replace(" ", "T", 1) if ("T" not in s and " " in s) else s
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        m = re.match(r"(\d{4}-\d{2}-\d{2})(?:T(\d{2}:\d{2}:\d{2}))?", s)
        if not m:
            return None
        try:
            dt = datetime.fromisoformat(f"{m.group(1)}T{m.group(2) or '00:00:00'}")
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=EASTERN)


def parse_acceptance_datetime(raw: str | None) -> datetime | None:
    """Parse `acceptanceDateTime` from data.sec.gov — which lies about its zone.

    The submissions JSON formats acceptance times as `2026-08-14T17:32:14.000Z`,
    but the value is **Eastern**, not UTC; the `Z` is spurious. Apple's 16:31
    earnings 8-K really is 16:31 ET, and reading it as UTC would move it to
    12:31 ET — turning an AMC gap-maker into a mid-session non-event, which is
    the exact misclassification this module exists to prevent.

    So: for this one field, a trailing `Z` is DISCARDED and the timestamp is
    tagged Eastern. An explicit numeric offset (as the Atom feed sends) is still
    honored, because that one is real.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if s.endswith("Z"):
        s = s[:-1]
    dt = parse_edgar_datetime(s)
    if dt is None:
        return None
    return dt.astimezone(EASTERN) if dt.tzinfo else dt.replace(tzinfo=EASTERN)


def parse_edgar_date(raw: str | None) -> date | None:
    """Parse a `YYYY-MM-DD` EDGAR date. Diagnostic use only — see the module
    docstring on why filing_date must not drive any gate."""
    if not raw:
        return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(raw))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def session_for(acceptance_at: datetime | None) -> str:
    """Classify an acceptance timestamp into bmo / dmh / amc.

    This is the whole point of keying on acceptance: an `amc` filing carries
    TOMORROW's filing_date, so anything comparing filing_date to "today" treats
    the most explosive filings of the day as if they were not today's news.
    """
    et = _to_eastern(acceptance_at)
    if et is None:
        return "unknown"
    if et.weekday() >= 5:               # weekend acceptance → next open reacts
        return "amc"
    hm = et.hour * 60 + et.minute
    if hm < 9 * 60 + 30:
        return "bmo"
    if hm < 16 * 60:
        return "dmh"
    return "amc"


def impact_session_date(acceptance_at: datetime | None) -> date | None:
    """The first session that can price this filing.

    bmo/dmh → same day. amc (and weekends) → the next weekday. Exchange
    holidays are not modeled here; erring one session early is the safe
    direction for a veto.
    """
    et = _to_eastern(acceptance_at)
    if et is None:
        return None
    d = et.date()
    if session_for(acceptance_at) != "amc":
        return d
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


# --- Pure parsers: CIK / ticker map ----------------------------------------

def pad_cik(value: Any) -> str:
    """Zero-pad a CIK to the 10 digits every SEC path expects.

    `company_tickers.json` stores `cik_str` as an **int** (320193), while
    `data.sec.gov/submissions/CIK##########.json` demands `0000320193`. Every
    lookup in this module goes through here so the two can never drift.
    """
    if value is None:
        return ""
    s = str(value).strip().upper()
    if s.startswith("CIK"):
        s = s[3:]
    digits = re.sub(r"\D", "", s)
    if not digits:
        return ""
    return digits.zfill(10)[-10:] if len(digits) > 10 else digits.zfill(10)


def parse_company_tickers(payload: Any) -> dict[str, list[str]]:
    """Build cik10 → [tickers] from `company_tickers.json`.

    The file is a dict keyed by row index ("0", "1", ...) whose values are
    {cik_str: int, ticker: str, title: str}. A list of the same records is also
    accepted. One CIK can carry several tickers (share classes such as
    GOOG/GOOGL); order is preserved, so the file's primary listing stays first.
    """
    rows: Iterable[Any]
    if isinstance(payload, Mapping):
        rows = payload.values()
    elif isinstance(payload, (list, tuple)):
        rows = payload
    else:
        return {}

    out: dict[str, list[str]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        cik = pad_cik(row.get("cik_str", row.get("cik")))
        ticker = str(row.get("ticker") or "").strip().upper()
        if not cik or not ticker:
            continue
        bucket = out.setdefault(cik, [])
        if ticker not in bucket:
            bucket.append(ticker)
    return out


def parse_company_titles(payload: Any) -> dict[str, str]:
    """cik10 → registrant name, for operator-facing detail."""
    rows: Iterable[Any]
    if isinstance(payload, Mapping):
        rows = payload.values()
    elif isinstance(payload, (list, tuple)):
        rows = payload
    else:
        return {}
    out: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        cik = pad_cik(row.get("cik_str", row.get("cik")))
        title = str(row.get("title") or "").strip()
        if cik and title and cik not in out:
            out[cik] = title
    return out


# --- Pure parsers: the Atom feed -------------------------------------------

_ITEM_RE = re.compile(r"Item[\s ]+(\d{1,2}\.\d{2})")
_ACCNO_RE = re.compile(r"(\d{10}-\d{2}-\d{6})")
_TITLE_CIK_RE = re.compile(r"\((\d{4,10})\)")
_HREF_CIK_RE = re.compile(r"/data/(\d{1,10})[/-]")
_FILED_RE = re.compile(r"Filed:\s*</?b?>?\s*(\d{4}-\d{2}-\d{2})", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def parse_summary_items(summary: str | None) -> tuple[str, ...]:
    """Extract 8-K Item numbers from the Atom `<summary>`.

    The feed spells them out in English, which is why no document fetch is
    needed anywhere in this module:

        <b>Filed:</b> 2026-08-15 <b>AccNo:</b> 0001104659-26-097364
        <br>Item 2.02: Results of Operations and Financial Condition
        <br>Item 9.01: Financial Statements and Exhibits

    Returns items in feed order, de-duplicated.
    """
    if not summary:
        return ()
    text = _TAG_RE.sub(" ", str(summary))
    seen: list[str] = []
    for m in _ITEM_RE.finditer(text):
        code = normalize_item(m.group(1))
        if code and code not in seen:
            seen.append(code)
    return tuple(seen)


def normalize_item(raw: str | None) -> str:
    """`2.2` / ` 2.02 ` / `Item 2.02` → `2.02`. Bad input → ""."""
    if raw is None:
        return ""
    s = str(raw).strip()
    s = re.sub(r"^item\s*", "", s, flags=re.I)
    m = re.match(r"^(\d{1,2})\.(\d{1,2})$", s)
    if not m:
        return ""
    return f"{int(m.group(1))}.{int(m.group(2)):02d}"


def normalize_form(raw: str | None) -> str:
    """Canonical form type: upper-cased, whitespace-collapsed.

    EDGAR emits both `NT 10-Q` and (rarely) `NT10-Q`; both must hit the
    hard-block list. Amendments (`8-K/A`, `NT 10-K/A`) keep their suffix — the
    classifier strips it when matching.
    """
    if not raw:
        return ""
    s = re.sub(r"\s+", " ", str(raw)).strip().upper()
    m = re.match(r"^NT\s*(10-[KQ].*)$", s)
    if m:
        s = f"NT {m.group(1)}"
    return s


def _base_form(form_type: str) -> str:
    """Drop an amendment suffix: `8-K/A` → `8-K`. An amended restatement notice
    is still a restatement notice."""
    return normalize_form(form_type).split("/A", 1)[0].strip()


def parse_atom_entry(entry_xml: Any) -> EdgarFiling | None:
    """Turn one `<entry>` element into an `EdgarFiling`."""
    def _text(tag: str) -> str:
        el = entry_xml.find(tag)
        if el is None:
            el = entry_xml.find(f"{{http://www.w3.org/2005/Atom}}{tag}")
        return (el.text or "").strip() if el is not None and el.text else ""

    title = _text("title")
    summary = _text("summary")
    updated = _text("updated")
    entry_id = _text("id")

    href = ""
    for link in list(entry_xml.iter()):
        tag = link.tag.rsplit("}", 1)[-1]
        if tag == "link" and link.get("href"):
            href = link.get("href") or ""
            break

    form_type = ""
    for cat in list(entry_xml.iter()):
        tag = cat.tag.rsplit("}", 1)[-1]
        if tag == "category" and (cat.get("label") or "").lower() == "form type":
            form_type = normalize_form(cat.get("term"))
            break
    if not form_type and " - " in title:
        form_type = normalize_form(title.split(" - ", 1)[0])

    # Accession: `<id>urn:tag:sec.gov,2008:accession-number=0001104659-26-097364`
    # with the summary's AccNo as a fallback.
    acc_m = _ACCNO_RE.search(entry_id) or _ACCNO_RE.search(summary)
    accession = acc_m.group(1) if acc_m else ""

    # CIK: the parenthesised value in the title is the filer; the Archives href
    # is the fallback (and is NOT zero-padded there, hence pad_cik).
    cik_m = _TITLE_CIK_RE.search(title)
    cik = pad_cik(cik_m.group(1)) if cik_m else ""
    if not cik:
        href_m = _HREF_CIK_RE.search(href)
        cik = pad_cik(href_m.group(1)) if href_m else ""

    company = ""
    if " - " in title:
        rest = title.split(" - ", 1)[1]
        company = re.sub(r"\s*\([^)]*\)\s*$", "", rest).strip()
        company = re.sub(r"\s*\(\d{4,10}\)\s*", " ", company).strip()

    filed_m = _FILED_RE.search(summary) or re.search(r"Filed:\s*(\d{4}-\d{2}-\d{2})", summary)

    if not accession and not cik:
        return None

    return EdgarFiling(
        accession=accession,
        cik=cik,
        form_type=form_type,
        # <updated> IS the acceptance timestamp for the getcurrent feed. This is
        # the value every downstream gate keys on.
        acceptance_at=parse_edgar_datetime(updated),
        filing_date=parse_edgar_date(filed_m.group(1) if filed_m else None),
        items=parse_summary_items(summary),
        company=company,
        link=href,
    )


def parse_atom_feed(xml_text: str) -> list[EdgarFiling]:
    """Parse a `getcurrent` Atom document into filings.

    Malformed individual entries are skipped rather than failing the sweep —
    one bad entry must never cost us the other 99.
    """
    if not xml_text or not xml_text.strip():
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise EdgarError(f"EDGAR atom feed: unparseable XML — {e!s}") from e

    out: list[EdgarFiling] = []
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] != "entry":
            continue
        try:
            filing = parse_atom_entry(el)
        except Exception:
            continue
        if filing is not None:
            out.append(filing)
    return out


# --- Pure parsers: data.sec.gov submissions --------------------------------

def parse_submissions(payload: Any, limit: int | None = None) -> list[EdgarFiling]:
    """Parse `data.sec.gov/submissions/CIK##########.json`.

    `filings.recent` is COLUMNAR: parallel arrays, one index per filing, with
    `items` as a comma-separated string per row. `acceptanceDateTime` is present
    here and is once again the authoritative field — `filingDate` in the very
    same record will read as the next business day for after-hours filings.
    Note its bogus `Z` suffix; see `parse_acceptance_datetime`.
    """
    if not isinstance(payload, Mapping):
        return []
    cik = pad_cik(payload.get("cik"))
    company = str(payload.get("name") or "").strip()
    recent = ((payload.get("filings") or {}) if isinstance(payload.get("filings"), Mapping)
              else {}).get("recent")
    if not isinstance(recent, Mapping):
        return []

    accs = list(recent.get("accessionNumber") or [])
    forms = list(recent.get("form") or [])
    accept = list(recent.get("acceptanceDateTime") or [])
    filed = list(recent.get("filingDate") or [])
    items = list(recent.get("items") or [])
    docs = list(recent.get("primaryDocument") or [])

    n = len(accs)
    if limit is not None:
        n = min(n, max(0, int(limit)))

    def _at(seq: list, i: int) -> Any:
        return seq[i] if i < len(seq) else None

    out: list[EdgarFiling] = []
    for i in range(n):
        raw_items = _at(items, i) or ""
        parsed_items = tuple(
            c for c in (normalize_item(p) for p in str(raw_items).split(",")) if c
        )
        accession = str(_at(accs, i) or "")
        link = ""
        doc = _at(docs, i)
        if accession and doc:
            link = (f"{SEC_WWW_BASE}/Archives/edgar/data/{int(cik or 0)}/"
                    f"{accession.replace('-', '')}/{doc}")
        out.append(EdgarFiling(
            accession=accession,
            cik=cik,
            form_type=normalize_form(_at(forms, i)),
            acceptance_at=parse_acceptance_datetime(_at(accept, i)),
            filing_date=parse_edgar_date(_at(filed, i)),
            items=parsed_items,
            company=company,
            link=link,
        ))
    return out


# --- Classification --------------------------------------------------------

def classify(form_type: str | None, items: Sequence[str] | None = None) -> Classification:
    """Verdict for a (form, items) pair. Pure — no I/O, no clock.

    Blocking wins over elevated: a filing carrying both 4.02 and 7.01 blocks.
    """
    form = _base_form(form_type or "")
    codes = tuple(c for c in (normalize_item(i) for i in (items or ())) if c)

    block_codes: list[str] = []
    elevated_codes: list[str] = []
    reasons: list[str] = []

    if form in HARD_BLOCK_FORMS:
        block_codes.append(form)
        reasons.append(f"{form}: {FORM_LABELS.get(form, 'hard-block form')}")
    elif form in ELEVATED_FORMS:
        elevated_codes.append(form)
        reasons.append(f"{form}: {FORM_LABELS.get(form, 'elevated-risk form')}")

    for code in codes:
        if code in HARD_BLOCK_ITEMS:
            block_codes.append(code)
            reasons.insert(0, f"Item {code}: {ITEM_LABELS.get(code, 'hard-block item')}")
        elif code in ELEVATED_ITEMS:
            elevated_codes.append(code)
            reasons.append(f"Item {code}: {ITEM_LABELS.get(code, 'elevated-risk item')}")

    return Classification(
        blocking=bool(block_codes),
        elevated=bool(elevated_codes),
        reasons=tuple(reasons),
        block_codes=tuple(block_codes),
        elevated_codes=tuple(elevated_codes),
    )


def classify_filing(filing: EdgarFiling) -> Classification:
    return classify(filing.form_type, filing.items)


def is_earnings_filing(filing: EdgarFiling) -> bool:
    """True when the filing carries Item 2.02.

    2.02 is the only *fact* in the earnings-date domain — every calendar vendor
    number is a projection until a 2.02 lands. Exposed so the earnings-calendar
    layer can repair vendor dates and dock a vendor's reliability score when it
    missed one.
    """
    return EARNINGS_ITEM in tuple(filing.items or ())


# --- Filing → Catalyst rows ------------------------------------------------

def catalyst_id(accession: str, ticker: str, multi: bool) -> str:
    """Primary key for `simmer_catalysts`.

    The accession number alone, as designed — except when one CIK maps to
    several tickers (share classes like GOOG/GOOGL), where a single accession
    legitimately produces several rows and the bare accession would collide.
    """
    acc = (accession or "").strip()
    if not acc:
        return f"{SOURCE}:{ticker}:unknown"
    return f"{acc}:{ticker}" if multi else acc


def filing_to_catalysts(filing: EdgarFiling,
                        tickers: Sequence[str],
                        classification: Classification | None = None) -> list[Catalyst]:
    """One row per ticker mapped to the filer's CIK. Empty list if none."""
    syms = [str(t).strip().upper() for t in (tickers or []) if str(t).strip()]
    if not syms:
        return []
    cls = classification or classify_filing(filing)
    multi = len(syms) > 1
    et = filing.acceptance_et
    detail = {
        "form": filing.form_type,
        "items": list(filing.items),
        "blocking_codes": list(cls.block_codes),
        "elevated_codes": list(cls.elevated_codes),
        "severity": cls.severity,
        "reasons": list(cls.reasons),
        "earnings_ground_truth": is_earnings_filing(filing),
        "company": filing.company,
        "cik": filing.cik,
        "accession": filing.accession,
        "link": filing.link,
        # Kept purely so an operator can SEE the acceptance/filing skew that
        # motivates this module. Nothing reads it as a gate input.
        "filing_date": filing.filing_date.isoformat() if filing.filing_date else None,
        "impact_date": (impact_session_date(filing.acceptance_at).isoformat()
                        if filing.acceptance_at else None),
    }
    payload = json.dumps(detail, separators=(",", ":"), sort_keys=True)
    session = session_for(filing.acceptance_at)
    return [
        Catalyst(
            id=catalyst_id(filing.accession, sym, multi),
            symbol=sym,
            event_type="sec_filing",
            # ET date of ACCEPTANCE — never filing_date.
            event_date=et.date() if et else None,
            session=session,
            confirmed=True,     # it is in EDGAR; it is not a vendor's guess
            blocking=cls.blocking,
            detail=payload,
            source=SOURCE,
            acceptance_at=filing.acceptance_at,
        )
        for sym in syms
    ]


def filings_to_catalysts(filings: Iterable[EdgarFiling],
                         ticker_map: Mapping[str, Sequence[str]],
                         include_info: bool = True) -> list[Catalyst]:
    """Map a batch of filings to catalyst rows.

    An unmapped CIK is SKIPPED, never fatal: most of the getcurrent feed is
    funds, trusts, and foreign private issuers that have no listed ticker, and
    the feed is the live safety path — it must not be brittle.

    `include_info=False` keeps only blocking/elevated rows (the ones that can
    actually change a decision).
    """
    out: list[Catalyst] = []
    for f in filings or []:
        if not isinstance(f, EdgarFiling):
            continue
        cik = pad_cik(f.cik)
        if not cik:
            continue
        tickers = ticker_map.get(cik) if ticker_map else None
        if not tickers:
            continue                      # unknown CIK → skip, do not crash
        cls = classify_filing(f)
        if not include_info and not (cls.blocking or cls.elevated):
            continue
        out.extend(filing_to_catalysts(f, tickers, cls))
    return out


def earnings_confirmations(filings: Iterable[EdgarFiling],
                           ticker_map: Mapping[str, Sequence[str]],
                           ) -> list[EarningsConfirmation]:
    """Item 2.02 sightings, resolved to tickers — GROUND TRUTH for the calendar.

    Use to repair vendor earnings dates: if a 2.02 lands for a ticker whose
    calendar didn't predict it, that vendor just lost a reliability point for
    that name. Unmapped CIKs are skipped.
    """
    out: list[EarningsConfirmation] = []
    for f in filings or []:
        if not isinstance(f, EdgarFiling) or not is_earnings_filing(f):
            continue
        tickers = ticker_map.get(pad_cik(f.cik)) if ticker_map else None
        for sym in (tickers or []):
            et = f.acceptance_et
            out.append(EarningsConfirmation(
                symbol=str(sym).upper(),
                accession=f.accession,
                acceptance_at=f.acceptance_at,
                event_date=et.date() if et else None,
                impact_date=impact_session_date(f.acceptance_at),
                session=session_for(f.acceptance_at),
            ))
    return out


# --- Rate limiting ---------------------------------------------------------

class _RateLimiter:
    """Async token bucket capped at `rps` requests per rolling second.

    SEC's published ceiling is 10 req/s. This is a compliance control, not an
    optimization — exceeding it gets the IP blocked outright.
    """

    def __init__(self, rps: float = MAX_REQUESTS_PER_SEC):
        self.rps = max(0.1, float(rps))
        self._lock = asyncio.Lock()
        self._stamps: list[float] = []

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._stamps = [t for t in self._stamps if now - t < 1.0]
                if len(self._stamps) < self.rps:
                    self._stamps.append(now)
                    return
                sleep_for = 1.0 - (now - self._stamps[0])
                await asyncio.sleep(max(0.01, sleep_for))


# --- Client ----------------------------------------------------------------

def build_user_agent(contact: str) -> str:
    """`User-Agent: EdgeLane <contact-email>` — SEC requires a real contact.

    A missing or generic UA earns a 403 with an "Undeclared Automated Tool"
    body, so a placeholder is used rather than sending nothing at all.
    """
    c = (contact or "").strip()
    return f"EdgeLane {c}" if c else "EdgeLane contact-unset@example.invalid"


def resolve_contact_email(settings: Any = None) -> str:
    """Contact address for the UA, configurable via settings.

    Order: `sec_contact_email` → `support_email` → `contact_from_email`. Read
    with getattr so the backend config can add the key without this module
    hard-depending on it.
    """
    if settings is None:
        try:
            from .config import get_settings
            settings = get_settings()
        except Exception:
            return ""
    for key in ("sec_contact_email", "support_email", "contact_from_email"):
        val = getattr(settings, key, "") or ""
        val = str(val).strip()
        if val:
            m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", val)
            return m.group(0) if m else val
    return ""


class EdgarClient:
    """Async SEC EDGAR client.

    Same shape as `TradierClient`: lazily constructed `httpx.AsyncClient`,
    structured errors, one retry on transient failures, explicit timeouts.

    Compliance is enforced here, not by the caller:
      * `User-Agent: EdgeLane <contact>` on every request
      * `Accept-Encoding: gzip` (SEC asks for it; the payloads are large)
      * ≤10 req/s via `_RateLimiter`
    """

    def __init__(self,
                 contact_email: str | None = None,
                 timeout: float = 20.0,
                 max_rps: float = MAX_REQUESTS_PER_SEC,
                 www_base: str = SEC_WWW_BASE,
                 data_base: str = SEC_DATA_BASE,
                 settings: Any = None):
        self.contact_email = (contact_email or resolve_contact_email(settings) or "").strip()
        self.user_agent = build_user_agent(self.contact_email)
        self.timeout = timeout
        self.www_base = www_base.rstrip("/")
        self.data_base = data_base.rstrip("/")
        self._limiter = _RateLimiter(min(float(max_rps), MAX_REQUESTS_PER_SEC))
        self._client: httpx.AsyncClient | None = None
        self._ticker_map: dict[str, list[str]] = {}
        self._ticker_titles: dict[str, str] = {}
        self._ticker_map_at: float = 0.0

    # -- plumbing --

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept-Encoding": "gzip, deflate",
                },
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(self, url: str, params: dict[str, Any] | None = None,
                   accept: str = "application/json",
                   _retried: bool = False) -> httpx.Response:
        await self._limiter.acquire()
        client = await self._get_client()
        try:
            resp = await client.get(url, params=params or None,
                                    headers={"Accept": accept})
        except httpx.TimeoutException as e:
            raise EdgarTimeoutError(
                f"EDGAR GET {url}: timed out after {self.timeout}s ({e!s})"
            ) from e
        except httpx.RequestError as e:
            if not _retried:
                await asyncio.sleep(2.0)
                return await self._get(url, params, accept, _retried=True)
            raise EdgarError(f"EDGAR GET {url}: network — {e!s}") from e

        if resp.status_code in (403, 429):
            # 403 here means "undeclared automated tool" or a rate block. Do NOT
            # retry — retrying is what turns a throttle into an IP ban.
            retry_after = 0.0
            ra = resp.headers.get("Retry-After")
            if ra:
                try:
                    retry_after = max(0.0, float(ra))
                except (TypeError, ValueError):
                    retry_after = 0.0
            raise EdgarRateLimitError(
                f"EDGAR GET {url}: HTTP {resp.status_code} — throttled/blocked. "
                f"UA={self.user_agent!r}. Body: {resp.text[:200]}",
                retry_after_sec=retry_after,
            )
        if 500 <= resp.status_code < 600:
            if not _retried:
                await asyncio.sleep(2.0)
                return await self._get(url, params, accept, _retried=True)
            raise EdgarError(
                f"EDGAR GET {url}: HTTP {resp.status_code} (after retry) — {resp.text[:300]}"
            )
        if resp.status_code >= 400:
            raise EdgarError(f"EDGAR GET {url}: HTTP {resp.status_code} — {resp.text[:300]}")
        return resp

    # -- endpoints --

    async def fetch_current_filings(self, form_type: str = "8-K",
                                    count: int = 100) -> list[EdgarFiling]:
        """The live path: `getcurrent` Atom, newest filings first.

        `<summary>` carries the Item numbers in English and `<updated>` carries
        the ACCEPTANCE timestamp, so one request per sweep is the entire live
        catalyst feed — no document fetching at all.
        """
        params = {
            "action": "getcurrent",
            "type": form_type,
            "output": "atom",
            "count": int(max(1, min(int(count), 100))),
        }
        resp = await self._get(f"{self.www_base}{CURRENT_FEED_PATH}", params,
                               accept="application/atom+xml")
        return parse_atom_feed(resp.text)

    async def fetch_ticker_map(self, force: bool = False
                               ) -> dict[str, list[str]]:
        """CIK→[tickers] from `company_tickers.json`, cached for 24 h.

        ~10.4k entries and ~1 MB, so refetching it per sweep would be both slow
        and a waste of the 10 req/s budget. `cik_str` arrives as an int and is
        zero-padded to 10 digits by `parse_company_tickers`.
        """
        fresh = (time.time() - self._ticker_map_at) < TICKER_MAP_TTL_SEC
        if self._ticker_map and fresh and not force:
            return self._ticker_map
        resp = await self._get(COMPANY_TICKERS_URL)
        try:
            payload = resp.json()
        except ValueError as e:
            raise EdgarError(f"EDGAR company_tickers.json: bad JSON — {e!s}") from e
        mapping = parse_company_tickers(payload)
        if not mapping:
            raise EdgarError("EDGAR company_tickers.json: parsed 0 entries")
        self._ticker_map = mapping
        self._ticker_titles = parse_company_titles(payload)
        self._ticker_map_at = time.time()
        return self._ticker_map

    async def resolve_cik(self, symbol: str) -> str:
        """ticker → zero-padded CIK. Empty string when unlisted/unknown."""
        mapping = await self.fetch_ticker_map()
        want = str(symbol or "").strip().upper()
        for cik, tickers in mapping.items():
            if want in tickers:
                return cik
        return ""

    async def fetch_submissions(self, cik: str | int) -> dict:
        """Raw `data.sec.gov/submissions/CIK##########.json` payload.

        data.sec.gov has sub-second processing delay and does NOT support CORS —
        this call can only ever be made server-side.
        """
        cik10 = pad_cik(cik)
        if not cik10:
            raise EdgarError(f"EDGAR submissions: unusable CIK {cik!r}")
        resp = await self._get(SUBMISSIONS_URL_TMPL.format(cik10=cik10))
        try:
            return resp.json()
        except ValueError as e:
            raise EdgarError(f"EDGAR submissions CIK{cik10}: bad JSON — {e!s}") from e

    async def fetch_company_filings(self, cik: str | int,
                                    limit: int = 40) -> list[EdgarFiling]:
        """Recent filings for one CIK — the backfill path for a newly watched
        symbol (the live feed only shows what filed since the last sweep)."""
        return parse_submissions(await self.fetch_submissions(cik), limit=limit)

    async def backfill_symbol(self, symbol: str, limit: int = 40) -> list[Catalyst]:
        """Catalyst rows for one ticker, from its own submissions file.

        Returns [] for an unlisted symbol rather than raising — a watchlist can
        legitimately hold an index or ETF with no EDGAR registrant.
        """
        sym = str(symbol or "").strip().upper()
        cik = await self.resolve_cik(sym)
        if not cik:
            return []
        filings = await self.fetch_company_filings(cik, limit=limit)
        return filings_to_catalysts(filings, {cik: [sym]})


# --- Sweep + persistence ---------------------------------------------------

_CATALYST_COLS = (
    "id", "symbol", "event_type", "event_date", "session", "confirmed",
    "blocking", "detail", "source", "acceptance_at",
)


def persist_catalysts(db: Any, catalysts: Sequence[Catalyst]) -> int:
    """Write catalyst rows to `simmer_catalysts`. Returns rows written.

    `id` is the accession number, so INSERT OR REPLACE makes a re-sweep of the
    same feed window idempotent — the getcurrent feed intentionally overlaps
    between sweeps.

    Synchronous (DuckDB is); call from a background loop via
    `asyncio.to_thread`, or use `persist_catalysts_async`.
    """
    rows = [c.to_row() for c in (catalysts or []) if c and c.symbol and c.id]
    if not rows:
        return 0

    # connect() migrates under the Database lock, so it MUST happen before we
    # take that (non-reentrant) lock ourselves.
    conn = db.connect()
    lock = getattr(db, "_lock", None)
    cols = ", ".join(_CATALYST_COLS)
    ph = ", ".join("?" for _ in _CATALYST_COLS)
    params = [[r[c] for c in _CATALYST_COLS] for r in rows]
    with (lock if lock is not None else nullcontext()):
        conn.executemany(
            f"INSERT OR REPLACE INTO simmer_catalysts ({cols}, created_at) "
            f"VALUES ({ph}, now())",
            params,
        )
    return len(rows)


async def persist_catalysts_async(db: Any, catalysts: Sequence[Catalyst]) -> int:
    """`persist_catalysts` off the event loop — all DuckDB writes go via
    to_thread, per the watcher-loop conventions in docs/simmer.md."""
    return await asyncio.to_thread(persist_catalysts, db, list(catalysts or []))


def fetch_recent_catalysts(db: Any, symbol: str, lookback_days: int = 5,
                           blocking_only: bool = False) -> list[dict]:
    """Read back catalysts for one symbol, newest acceptance first.

    Ordered and filtered by `acceptance_at`, not `event_date` — an AMC 8-K
    accepted at 17:25 belongs to the session that hasn't opened yet.
    """
    conn = db.connect()
    lock = getattr(db, "_lock", None)
    sql = (
        f"SELECT {', '.join(_CATALYST_COLS)} FROM simmer_catalysts "
        "WHERE symbol = ? AND acceptance_at >= ? "
        + ("AND blocking " if blocking_only else "")
        + "ORDER BY acceptance_at DESC"
    )
    cutoff = datetime.now(EASTERN).replace(tzinfo=None) - timedelta(days=int(lookback_days))
    with (lock if lock is not None else nullcontext()):
        cur = conn.execute(sql, [str(symbol).upper(), cutoff])
        rows = cur.fetchall()
    return [dict(zip(_CATALYST_COLS, r)) for r in rows]


@dataclass
class SweepResult:
    """What one EDGAR sweep saw. Cheap enough to log every time."""

    fetched: int = 0            # entries in the feed
    mapped: int = 0             # catalyst rows produced (CIK resolved to a ticker)
    unmapped: int = 0           # filings whose CIK has no listed ticker (normal)
    blocking: int = 0
    elevated: int = 0
    persisted: int = 0
    earnings: list[EarningsConfirmation] = field(default_factory=list)
    error: str | None = None


async def sweep_current_filings(client: EdgarClient,
                                db: Any = None,
                                form_type: str = "8-K",
                                count: int = 100,
                                include_info: bool = False) -> SweepResult:
    """One live sweep: fetch → map → classify → persist.

    Two requests at most (the ticker map is cached for 24 h), well inside the
    10 req/s budget. `db=None` runs the sweep without persisting, which is what
    the tests and a dry-run operator call want.
    """
    result = SweepResult()
    filings = await client.fetch_current_filings(form_type=form_type, count=count)
    result.fetched = len(filings)
    ticker_map = await client.fetch_ticker_map()

    result.unmapped = sum(1 for f in filings if not ticker_map.get(pad_cik(f.cik)))
    catalysts = filings_to_catalysts(filings, ticker_map, include_info=include_info)
    result.mapped = len(catalysts)
    result.blocking = sum(1 for c in catalysts if c.blocking)
    result.elevated = sum(
        1 for c in catalysts
        if not c.blocking and c.detail_obj.get("severity") == "elevated"
    )
    result.earnings = earnings_confirmations(filings, ticker_map)

    if db is not None and catalysts:
        result.persisted = await persist_catalysts_async(db, catalysts)
    return result
