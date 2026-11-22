"""OLX.ro, through the JSON endpoint its own website loads results from.

query_url: a listing page URL from the browser, filters set, e.g.
  https://www.olx.ro/electronice-si-electrocasnice/laptop-calculator-gaming/playstation/?currency=RON&search%5Bfilter_enum_model%5D%5B0%5D=playstation_5
options:   {"category_id": 1878}  optional; auto-detected from the page otherwise
"""

import re
from collections import Counter
from datetime import datetime
from urllib.parse import parse_qsl, urlsplit

from .base import Listing, Provider, ProviderError, parse_iso_utc

API_URL = "https://www.olx.ro/api/v1/offers/"
PAGE_LIMIT = 40
MAX_PAGES = 3

_SEARCH_KEY = re.compile(r"^search\[([^\]]+)\](.*)$")
_CATEGORY_PATTERNS = [
    re.compile(r'\\*"category_id\\*"\s*:\s*\\*"?(\d+)'),
    re.compile(r'\\*"categoryId\\*"\s*:\s*\\*"?(\d+)'),
    re.compile(r'\\*"category\\*"\s*:\s*\{\s*\\*"id\\*"\s*:\s*\\*"?(\d+)'),
    re.compile(r'[?&]category_id=(\d+)'),
]


def page_url_to_params(page_url):
    """Convert a listing page URL's query string into /api/v1/offers params."""
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
    if not has_sort:
        params.append(("sort_by", "created_at:desc"))
    return params


class OlxProvider(Provider):
    name = "olx"
    label = "OLX"

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
        # OLX delivery (protected payment) off. A phone number in an OLX ad is normal (OLX shows
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
        )
