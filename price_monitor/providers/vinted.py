"""Vinted, from the search results embedded in its catalog page.

Vinted renders search results on its server and embeds them in the page
(Next.js "flight" data, self.__next_f.push(...) chunks). There is no stable
public API, so this reads that data; if Vinted changes the page, the search's
last_error will say the results could not be found.

query_url: a catalog URL from the browser, filters set, e.g.
  https://www.vinted.ro/catalog?search_text=playstation%205&price_from=1000&currency=RON&order=newest_first
options:   {"price": "total"}  compare the price including the buyer protection fee
           (default), or "item" for the seller's price alone

The embedded results carry no posting date, so a search's age limit doesn't
apply here (sorting by newest first keeps the results recent anyway).

Note: vinted.ro also shows items from other countries (Poland, Greece, ...),
so the search's exclude keywords may need words in those languages.
"""

import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .base import Listing, Provider, ProviderError

_PUSH = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')
_ITEMS = re.compile(r'"items":\{"items":\[')


def _with_newest_first(url):
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if not any(k == "order" for k, _ in query):
        query.append(("order", "newest_first"))
    return urlunsplit(parts._replace(query=urlencode(query), fragment=""))


def items_from_page(html):
    payload = "".join(json.loads(chunk) for chunk in _PUSH.findall(html))
    decoder = json.JSONDecoder()
    found, seen = [], set()
    for m in _ITEMS.finditer(payload):
        items, _ = decoder.raw_decode(payload, m.end() - 1)
        for it in items:
            item = it.get("productItem") or it
            if item.get("id") not in seen:
                seen.add(item.get("id"))
                found.append(item)
    return found, bool(payload)


class VintedProvider(Provider):
    name = "vinted"
    label = "Vinted"
    search_delay = 5

    def __init__(self):
        super().__init__()
        self._home = None  # the site whose homepage we visited for session cookies

    def _base(self, url):
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}"

    def search(self, spec):
        parts = urlsplit(spec.query_url)
        if parts.scheme != "https" or not re.fullmatch(r"(www\.)?vinted\.[a-z.]+", parts.netloc) \
                or not parts.path.startswith("/catalog"):
            raise ProviderError("query_url must be a Vinted catalog URL (https://www.vinted.ro/catalog?...)")
        base = self._base(spec.query_url)
        if self._home != base:
            self.session.get(base + "/", timeout=30)
            self._home = base

        resp = self.session.get(_with_newest_first(spec.query_url), timeout=30)
        if resp.status_code in (401, 403, 429):
            raise ProviderError(f"Vinted refused the request (HTTP {resp.status_code}); possibly blocked or rate-limited")
        resp.raise_for_status()
        items, has_payload = items_from_page(resp.text)
        if not items and not has_payload:
            raise ProviderError("Could not find search results in the Vinted page; its layout may have changed")

        use_total = (spec.options.get("price") or "total") != "item"
        listings = []
        for item in items:
            price = (item.get("totalItemPrice") if use_total else None) or item.get("price") or {}
            amount = price.get("amount")
            listings.append(Listing(
                external_id=str(item["id"]),
                title=(item.get("title") or "").strip(),
                url=base + item["url"] if str(item.get("url", "")).startswith("/") else item.get("url") or "",
                price=float(amount) if amount not in (None, "") else None,
                currency=(price.get("currencyCode") or "").upper() or None,
                image_url=item.get("thumbnailUrl"),
            ))
        return listings
