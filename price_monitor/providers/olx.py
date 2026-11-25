"""OLX.ro, through the JSON endpoint its own website loads results from.

query_url: a listing page URL from the browser, filters set, e.g.
  https://www.olx.ro/electronice-si-electrocasnice/laptop-calculator-gaming/playstation/?currency=RON&search%5Bfilter_enum_model%5D%5B0%5D=playstation_5
options:   {"category_id": 1878}  optional; auto-detected from the page otherwise
"""

import re
from collections import Counter
from datetime import datetime
from urllib.parse import parse_qsl, unquote, urlsplit

from .base import Check, Listing, Provider, ProviderError, parse_iso_utc

API_URL = "https://www.olx.ro/api/v1/offers/"
# Sellers' ratings, as an ad's page shows them ("5.0 / 5, 18 ratinguri"): OLX's ratings service,
# public, by the seller's uuid (which the offers above include)
RATINGS_URL = "https://rating-cdn.css.olx.io/ratings/v1/public/olxro/user/{uuid}/score/details"
PAGE_LIMIT = 40
MAX_PAGES = 3

_IS_ACTIVE = re.compile(r'\\*"isActive\\*"\s*:\s*(true|false)')
_REGULAR_PRICE = re.compile(r'\\*"regularPrice\\*"\s*:\s*\{\\*"value\\*"\s*:\s*([\d.]+)\s*,\s*\\*"currencyCode\\*"\s*:\s*\\*"([A-Z]+)')
_SEARCH_KEY = re.compile(r"^search\[([^\]]+)\](.*)$")
# A text search, in the page's path: /caini/q-samoyed/, /q-playstation-5/ (dashes for spaces)
_PATH_QUERY = re.compile(r"/q-([^/]+)/?$")
_CATEGORY_PATTERNS = [
    re.compile(r'\\*"category_id\\*"\s*:\s*\\*"?(\d+)'),
    re.compile(r'\\*"categoryId\\*"\s*:\s*\\*"?(\d+)'),
    re.compile(r'\\*"category\\*"\s*:\s*\{\s*\\*"id\\*"\s*:\s*\\*"?(\d+)'),
    re.compile(r'[?&]category_id=(\d+)'),
]


def page_url_to_params(page_url):
    """Convert a listing page URL's query string, and the text searched for in its path
    (/q-samoyed/), into /api/v1/offers params."""
    parts = urlsplit(page_url)  # the fragment is ignored
    params, has_sort = [], False
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key == "page":
            continue
        m = _SEARCH_KEY.match(key)
        if m:
            name, rest = m.groups()
            if name == "order":
                params.append(("sort_by", value))
                has_sort = True
                continue
            key = name + rest  # search[filter_enum_model][0] -> filter_enum_model[0]
        params.append((key, value))
    # The text searched for: the path's /q-.../ unless the query string already gives it
    m = _PATH_QUERY.search(parts.path)
    if m and not any(key in ("query", "q") for key, _ in params):
        params.append(("query", unquote(m.group(1)).replace("-", " ").strip()))
    if not has_sort:
        params.append(("sort_by", "created_at:desc"))
    return params


class OlxProvider(Provider):
    name = "olx"
    label = "OLX"
    seller_info = ("reviews", "account_age")

    def category_id(self, spec):
        if spec.options.get("category_id"):
            return int(spec.options["category_id"])
        path = urlsplit(spec.query_url).path
        if spec.state.get("category_id") and spec.state.get("category_path") == path:
            return int(spec.state["category_id"])

        self.log.info("[%s] Detecting category_id from %s", spec.name, spec.query_url)
        resp = self.session.get(spec.query_url, timeout=30)
        resp.raise_for_status()
        counts = Counter()
        for pattern in _CATEGORY_PATTERNS:
            counts.update(pattern.findall(resp.text))
        counts.pop("0", None)
        if not counts:
            raise ProviderError(
                "Could not detect the OLX category_id. Open the search page in a browser, open "
                "DevTools > Network, filter by 'api/v1/offers', reload, and copy the category_id "
                'parameter into this search\'s options as {"category_id": N}.')
        category_id, hits = counts.most_common(1)[0]
        self.log.info("[%s] Detected category_id=%s (%d matches; candidates: %s)",
                      spec.name, category_id, hits, dict(counts.most_common(5)))
        spec.state.update(category_id=int(category_id), category_path=path)
        return int(category_id)

    def search(self, spec):
        if not spec.query_url.startswith("https://www.olx.ro/"):
            raise ProviderError("query_url must be an olx.ro listing page (https://www.olx.ro/...)")
        params = page_url_to_params(spec.query_url) + [("category_id", str(self.category_id(spec)))]

        raw = []
        for page in range(MAX_PAGES):
            if page:
                self.sleep_between_pages()
            resp = self.session.get(
                API_URL, params=params + [("offset", str(page * PAGE_LIMIT)), ("limit", str(PAGE_LIMIT))],
                headers={"Accept": "application/json"}, timeout=30)
            if resp.status_code in (401, 403, 429):
                raise ProviderError(f"OLX refused the request (HTTP {resp.status_code}); possibly blocked or rate-limited")
            resp.raise_for_status()
            data = resp.json().get("data") or []
            raw.extend(data)
            if len(data) < PAGE_LIMIT:
                break
        return [self.parse(o) for o in raw]

    def same_ad(self, url, final_url):
        """Ad addresses end in "-ID<code>.html"; the words before it follow the title"""
        ids = [re.search(r"-ID(\w+)\.html", u) for u in (url, final_url)]
        return all(ids) and ids[0].group(1) == ids[1].group(1)

    def check(self, url):
        """The ad's page says whether it's active ("isActive") and its price ("regularPrice")."""
        resp = self.fetch_ad(url)
        if resp is None:
            return Check(gone=True)
        active = _IS_ACTIVE.search(resp.text)
        if active is None:
            return None  # layout changed: can't tell
        if active.group(1) == "false":
            return Check(gone=True)
        price = _REGULAR_PRICE.search(resp.text)
        if price is None:
            return Check(gone=False)
        currency = price.group(2)
        return Check(gone=False, price=float(price.group(1)), currency="RON" if currency == "LEI" else currency)

    @staticmethod
    def parse(offer):
        price = currency = None
        for p in offer.get("params") or []:
            if p.get("key") == "price":
                value = p.get("value") or {}
                price, currency = value.get("value"), value.get("currency")
                break
        if currency:
            currency = "RON" if currency.upper() == "LEI" else currency.upper()
        image = None
        photos = offer.get("photos") or []
        if photos and photos[0].get("link"):
            image = photos[0]["link"].replace("{width}", "400").replace("{height}", "300")
        signals = {"photos": len(photos), "description": offer.get("description") or ""}
        created = parse_iso_utc((offer.get("user") or {}).get("created"))
        if created:
            signals["account_age_days"] = max(0, (datetime.utcnow() - created).days)
        contact = offer.get("contact") or {}
        # No buyer protection: the ad doesn't offer OLX's delivery, which carries it. A phone number in an OLX ad is normal (OLX shows
        # phones itself), so it isn't a signal here; WhatsApp and the like are caught by the phrases
        signals["unprotected"] = not contact.get("courier")
        return Listing(
            external_id=str(offer["id"]),
            title=(offer.get("title") or "").strip(),
            url=offer.get("url") or "",
            price=float(price) if price not in (None, "", 0) else None,
            currency=currency,
            # "City, România", like Vinted's "City, Country" (OLX.ro is Romania only)
            location=", ".join(x for x in (((offer.get("location") or {}).get("city") or {}).get("name"), "România") if x),
            image_url=image,
            posted_at=parse_iso_utc(offer.get("created_time")),
            signals=signals,
            seller_id=(offer.get("user") or {}).get("uuid") or None,
        )

    rates_sellers = True

    def fetch_seller_signals(self, listing):
        resp = self.session.get(
            RATINGS_URL.format(uuid=listing.seller_id), params={"includeDeletedRatings": "false"},
            headers={"Accept": "application/json", "Accept-Language": "ro", "X-Platform-Type": "desktop",
                     "Origin": "https://www.olx.ro", "Referer": listing.url}, timeout=20)
        if resp.status_code in (401, 403, 429):
            raise ProviderError(f"OLX refused a seller rating (HTTP {resp.status_code}); possibly rate-limited")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        if resp.status_code == 204 or not resp.content:
            return {"seller_reviews": 0, "seller_negative_reviews": 0}  # no ratings yet
        data = resp.json()
        count = (data.get("ratings") or {}).get("totalCount")
        if count is None:
            return None
        stars = data.get("details") or {}
        signals = {"seller_reviews": int(count),
                   "seller_negative_reviews": int(stars.get("stars_1") or 0) + int(stars.get("stars_2") or 0)}
        if count and data.get("value") is not None:
            signals["seller_rating"] = float(data["value"]) / 5  # stars out of 5, as 0-1
        return signals
