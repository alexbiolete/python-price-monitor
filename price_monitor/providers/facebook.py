"""Facebook Marketplace, logged out, from the results embedded in its search page.

Facebook shows Marketplace searches to logged-out visitors only from some
connections: a home connection works, a server's (datacenter) doesn't; there it
redirects to the login page. So this module is meant to run on your own
computer, with PLATFORMS=facebook there and Facebook left out of the server's
PLATFORMS. It never logs in.

query_url: a Marketplace search from the browser, filters set, e.g.
  https://www.facebook.com/marketplace/bucharest/search/?query=playstation%205&minPrice=1500&maxPrice=2500&daysSinceListed=30
  ("newest first" is added when the link doesn't choose an order)

One results page per run (about 20-25 listings), no item pages. Sold and pending
listings are skipped. The seller's name, which the page includes, isn't kept.
"""

import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .base import Listing, Provider, ProviderError

_LISTING = re.compile(r'"listing":\{"__typename":"GroupCommerceProductItem"')
_CURRENCY = {"lei": "RON", "ron": "RON", "€": "EUR", "eur": "EUR", "$": "USD", "usd": "USD"}

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}


def _newest_first(url):
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if not any(k == "sortBy" for k, _ in query):
        query.append(("sortBy", "creation_time_descend"))
    return urlunsplit(parts._replace(query=urlencode(query), fragment=""))


def _currency(formatted):
    words = re.findall(r"[^\d\s.,]+", (formatted or "").lower())
    return next((_CURRENCY[w] for w in words if w in _CURRENCY), "RON")


def listings_from_page(html):
    decoder = json.JSONDecoder()
    found, seen = [], set()
    for m in _LISTING.finditer(html):
        try:
            item, _ = decoder.raw_decode(html, m.start() + len('"listing":'))
        except ValueError:
            continue
        if item.get("id") and item["id"] not in seen:
            seen.add(item["id"])
            found.append(item)
    return found


class FacebookProvider(Provider):
    name = "facebook"
    label = "Facebook"
    search_delay = 10

    def search(self, spec):
        parts = urlsplit(spec.query_url)
        if parts.netloc not in ("www.facebook.com", "facebook.com", "web.facebook.com") \
                or not parts.path.startswith("/marketplace"):
            raise ProviderError("query_url must be a Marketplace search "
                                "(https://www.facebook.com/marketplace/<city>/search/?query=...)")
        resp = self.session.get(_newest_first(spec.query_url), headers=HEADERS, timeout=30)
        if "/login" in urlsplit(resp.url).path or resp.status_code in (400, 401, 403):
            raise ProviderError("Facebook wants a login from this connection (it does from servers). "
                                "Run Facebook searches from a home connection: PLATFORMS=facebook there.")
        if resp.status_code == 429:
            raise ProviderError("Facebook is rate-limiting this connection; try a longer interval")
        resp.raise_for_status()

        items = listings_from_page(resp.text)
        if not items and "marketplace" not in resp.text[:200000].lower():
            raise ProviderError("Could not find search results in the Facebook page; its layout may have changed")

        listings = []
        for item in items:
            if item.get("is_sold") or item.get("is_pending") or item.get("is_hidden"):
                continue
            price = item.get("listing_price") or {}
            amount = price.get("amount")
            place = (item.get("location") or {}).get("reverse_geocode") or {}
            location = ((place.get("city_page") or {}).get("display_name")
                        or ", ".join(x for x in (place.get("city"), place.get("state")) if x) or None)
            created = item.get("creation_time")
            listings.append(Listing(
                external_id=str(item["id"]),
                title=(item.get("marketplace_listing_title") or item.get("custom_title") or "").strip(),
                url=f"https://www.facebook.com/marketplace/item/{item['id']}/",
                price=float(amount) if amount not in (None, "", "0", "0.00") else None,
                currency=_currency(price.get("formatted_amount")),
                location=location,
                image_url=(((item.get("primary_listing_photo") or {}).get("image") or {}).get("uri")),
                posted_at=(datetime.fromtimestamp(int(created), tz=timezone.utc).replace(tzinfo=None)
                           if created else None),
                signals={"description": ""},
            ))
        return listings
