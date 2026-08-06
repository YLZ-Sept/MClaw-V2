"""SEC EDGAR filings — public Atom feed with contact-UA compliance.

The SEC publishes the ``getcurrent`` Atom feed at a stable URL, but it
enforces its `Accessing EDGAR Data` rules strictly: any request whose
``User-Agent`` does not include a contact e-mail is rejected with a
``403 Forbidden``. We therefore *override* the shared Chrome UA here
via ``make_client(user_agent=...)`` rather than just appending an
``extra_headers`` banner — the previous attempt only added headers, so
the Chrome UA was still the one SEC saw, and every fetch came back as
``auth`` errors in the drawer.

Reference: https://www.sec.gov/os/accessing-edgar-data
"""

from __future__ import annotations

import logging
import re
from typing import Any

from finpulse_fetchers._http import fetch_text, make_client
from finpulse_fetchers.base import BaseFetcher, NormalizedItem
from finpulse_fetchers.rss import parse_feed


_EDGAR_RSS = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcurrent&type=8-K&company=&dateb=&owner=include&count=40&output=atom"
)

# Minimum-viable UA pattern the SEC accepts — "Name email@example.com".
# We keep the check loose (presence of an ``@`` + a dot in the trailing
# token) so operators can paste either a company name or a personal
# handle in front of the contact address.
_EMAIL_RE = re.compile(r"[\w.\-+]+@[\w\-]+\.[\w.\-]+")

logger = logging.getLogger(__name__)


_DEFAULT_UA = "Mclaw fin-pulse contact@mclaw.ai"


class SecEdgarFetcher(BaseFetcher):
    source_id = "sec_edgar"

    async def fetch(self, **_: Any) -> list[NormalizedItem]:
        raw = (self._config.get("sec_edgar.contact") or "").strip()
        # SEC returns 403 when the User-Agent is a bare browser string — it
        # must include a real contact. Fall back to a project placeholder so
        # first-run ingest never 500s; operators should still replace this
        # with their own name + e-mail per sec.gov access policy.
        contact = raw if _EMAIL_RE.search(raw) else _DEFAULT_UA

        async with make_client(
            timeout=self._timeout_sec,
            user_agent=contact,
            extra_headers={"Accept-Encoding": "gzip, deflate"},
        ) as client:
            body = await fetch_text(client, _EDGAR_RSS)
        items = parse_feed(self.source_id, body)
        if not items:
            logger.info("sec_edgar feed returned 0 rows (parser=%s)", "stdlib")
        return items


__all__ = ["SecEdgarFetcher"]
