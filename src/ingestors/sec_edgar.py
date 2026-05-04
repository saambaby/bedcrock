"""SEC EDGAR Form 4 ingestor.

Form 4 = officer/director/10%-owner trades. 2-business-day disclosure window —
the fastest reliable insider data we have.

Endpoints used (all free, no key required, must include User-Agent):
- https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=4&dateb=&owner=include&count=40
  — RSS-style listing of recent Form 4 filings (we use the JSON API instead)
- https://data.sec.gov/submissions/CIK{cik}.json — submissions index per filer
- https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no_dashes}/{primary_doc}
  — the actual XML

For ingestion we use the global recent-filings feed:
- https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=4&dateb=&owner=include&count=100&output=atom

Reference:
- https://www.sec.gov/os/accessing-edgar-data
- Form 4 XML schema: https://www.sec.gov/info/edgar/specifications/form34xmltechspec.html

Rate limit: 10 req/sec across all SEC endpoints.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

from src.config import settings
from src.db.models import Action, SignalSource
from src.ingestors.base import BaseIngestor
from src.logging_config import get_logger
from src.schemas import RawSignal

logger = get_logger(__name__)

ATOM_FEED_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcompany&type=4&dateb=&owner=include&count=100&output=atom"
)
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
F4_NS = {"f": "http://www.sec.gov/edgar/document/thirteenf/informationtable"}


class SECForm4Ingestor(BaseIngestor):
    name = "sec_form4"
    source = SignalSource.SEC_FORM4
    interval_seconds = 15 * 60  # poll every 15 minutes during market hours

    async def fetch(self) -> AsyncIterator[RawSignal]:
        headers = {"User-Agent": settings.sec_user_agent, "Accept": "application/atom+xml"}

        # Step 1: Pull the recent-filings atom feed
        feed_resp = await self._http.get(ATOM_FEED_URL, headers=headers)
        feed_resp.raise_for_status()
        root = ET.fromstring(feed_resp.text)

        for entry in root.findall("atom:entry", ATOM_NS):
            try:
                async for signal in self._parse_entry(entry, headers):
                    yield signal
            except Exception as e:
                logger.warning("form4_entry_parse_failed", error=str(e))
                continue

    async def _parse_entry(  # noqa: C901
        self, entry: ET.Element, headers: dict[str, str]
    ) -> AsyncIterator[RawSignal]:
        link_el = entry.find("atom:link", ATOM_NS)
        if link_el is None:
            return
        filing_index_url = link_el.get("href", "")
        if not filing_index_url:
            return

        # The atom entry's <link> points to the filing index page; we need the
        # primary XML document. The convention: same URL with -index.htm replaced
        # by the .xml file listed in the index.
        index_resp = await self._http.get(filing_index_url, headers=headers)
        index_resp.raise_for_status()

        # Crude but reliable: find any href ending in .xml in the index page
        xml_links = re.findall(r'href="([^"]+\.xml)"', index_resp.text)
        if not xml_links:
            return
        # Pick the first one that's not the index XSL
        primary = next((link for link in xml_links if "primary_doc" in link or "form" in link.lower()), xml_links[0])
        if not primary.startswith("http"):
            primary = "https://www.sec.gov" + primary

        xml_resp = await self._http.get(primary, headers=headers)
        xml_resp.raise_for_status()

        # The Form 4 XML doesn't use a single namespace consistently across filers,
        # so we strip namespaces for parsing.
        xml_text = self._strip_namespaces(xml_resp.text)
        f4 = ET.fromstring(xml_text)

        # --- Extract issuer (the company whose stock is being traded) ---
        issuer = f4.find("issuer")
        if issuer is None:
            return
        ticker_el = issuer.find("issuerTradingSymbol")
        ticker = (ticker_el.text or "").strip().upper() if ticker_el is not None else ""
        if not ticker or ticker in ("NONE", "N/A"):
            return

        # --- Extract reporting owner (the insider) ---
        owner = f4.find("reportingOwner")
        owner_id = owner.find("reportingOwnerId") if owner is not None else None
        owner_name = ""
        owner_cik = ""
        if owner_id is not None:
            n = owner_id.find("rptOwnerName")
            c = owner_id.find("rptOwnerCik")
            owner_name = (n.text or "").strip() if n is not None else ""
            owner_cik = (c.text or "").strip() if c is not None else ""
        slug = (owner_name or owner_cik).lower().replace(" ", "-").replace(",", "")[:64]

        # --- Filing date ---
        period_of_report = f4.find("periodOfReport")
        disclosed_at = datetime.now(UTC)  # filed-now approximation
        if period_of_report is not None and period_of_report.text:
            try:
                disclosed_at = datetime.fromisoformat(period_of_report.text + "T00:00:00+00:00")
            except ValueError:
                pass

        accession_root = filing_index_url.rstrip("/").split("/")[-1].replace("-index.htm", "")

        # --- Non-derivative transactions ---
        for tx in f4.findall(".//nonDerivativeTransaction"):
            try:
                signal = self._parse_transaction(
                    tx, ticker, owner_name, slug, owner_cik,
                    accession_root, disclosed_at, derivative=False,
                )
                if signal:
                    yield signal
            except Exception as e:
                logger.debug("form4_tx_skip", error=str(e))

        # Skip derivatives in v0.1 — different value semantics.

    def _parse_transaction(
        self,
        tx: ET.Element,
        ticker: str,
        owner_name: str,
        slug: str,
        owner_cik: str,
        accession_root: str,
        disclosed_at: datetime,
        derivative: bool,
    ) -> RawSignal | None:
        # Code: P=open-market purchase, S=open-market sale, A=grant, etc.
        code_el = tx.find("transactionCoding/transactionCode")
        if code_el is None or not code_el.text:
            return None
        code = code_el.text.strip()
        if code == "P":
            action = Action.BUY
        elif code == "S":
            action = Action.SELL
        else:
            return None  # only open-market trades

        amt_el = tx.find("transactionAmounts/transactionShares/value")
        price_el = tx.find("transactionAmounts/transactionPricePerShare/value")
        date_el = tx.find("transactionDate/value")

        shares = Decimal(amt_el.text) if amt_el is not None and amt_el.text else None
        price = Decimal(price_el.text) if price_el is not None and price_el.text else None
        size_usd = (shares * price) if shares is not None and price is not None else None

        trade_date = None
        if date_el is not None and date_el.text:
            try:
                trade_date = datetime.fromisoformat(date_el.text + "T00:00:00+00:00")
            except ValueError:
                pass

        # External ID: accession + ticker + sequence-ish — collisions impossible
        # because each accession has at most one tx with the same code+date+shares.
        external_id = f"{accession_root}:{ticker}:{code}:{date_el.text if date_el is not None else 'na'}:{shares}"

        return RawSignal(
            source=SignalSource.SEC_FORM4,
            source_external_id=external_id,
            ticker=ticker,
            action=action,
            disclosed_at=disclosed_at,
            trade_date=trade_date,
            trader_slug=slug,
            trader_display_name=owner_name,
            trader_kind="insider",
            size_low_usd=size_usd,
            size_high_usd=size_usd,
            raw={
                "accession": accession_root,
                "owner_cik": owner_cik,
                "transaction_code": code,
                "shares": str(shares) if shares else None,
                "price": str(price) if price else None,
            },
        )

    @staticmethod
    def _strip_namespaces(xml_text: str) -> str:
        """Remove XML namespaces so ElementTree XPath is straightforward."""
        import re
        # Remove xmlns="..." and xmlns:foo="..."
        xml_text = re.sub(r'\sxmlns(:\w+)?="[^"]+"', "", xml_text)
        # Remove namespace prefixes from tags
        xml_text = re.sub(r"<(/?)\w+:", r"<\1", xml_text)
        return xml_text
