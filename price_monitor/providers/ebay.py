"""eBay, through its official Browse API (eBay blocks its search pages from servers).

Needs a free eBay developer account (developer.ebay.com): create a Production keyset
and put its App ID and Cert ID in .env as EBAY_CLIENT_ID and EBAY_CLIENT_SECRET.
The monitor gets an application token itself (client credentials).

query_url: an eBay search from the browser, e.g.
  https://www.ebay.de/sch/i.html?_nkw=xbox+series+x+konsole&_udlo=200&_udhi=600
  The site picks the marketplace (ebay.de -> EBAY_DE, prices in EUR); _nkw the
  keywords; _udlo/_udhi a price range in that currency; _sacat a category.
options:   {"ship_to": "RO"}      where it's delivered (default EBAY_SHIP_TO, else RO)
           {"duty_pct": 0}        customs duty for items from outside the EU (consoles: 0)
           {"auctions": true}     include auctions (default: buy-it-now and best offer only)

The listing's price is the total to get it delivered: price + shipping to ship_to
+, for items shipped from outside the EU, customs duty and import VAT on all of
that (IMPORT_VAT_PCT, 21 for Romania) and IMPORT_FEE_RON (a courier's handling
fee, default 0). The breakdown is in price_detail.
"""

import base64
import os
import time
from urllib.parse import parse_qs, urlsplit

from ..rates import rate_to_ron
from .base import Listing, Provider, ProviderError, parse_iso_utc

API = "https://api.ebay.com"
SCOPE = "https://api.ebay.com/oauth/api_scope"

MARKETPLACES = {
    "ebay.com": "EBAY_US", "ebay.de": "EBAY_DE", "ebay.co.uk": "EBAY_GB", "ebay.it": "EBAY_IT",
    "ebay.fr": "EBAY_FR", "ebay.es": "EBAY_ES", "ebay.at": "EBAY_AT", "ebay.nl": "EBAY_NL",
    "ebay.pl": "EBAY_PL", "ebay.ie": "EBAY_IE", "ebay.be": "EBAY_BE", "ebay.ch": "EBAY_CH",
    "ebay.ca": "EBAY_CA", "ebay.com.au": "EBAY_AU",
}

EU = {"AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR", "HU", "IE", "IT",
      "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK"}

# Country names as the other platforms show them (Vinted's are Romanian)
COUNTRIES = {
    "RO": "România", "DE": "Germania", "AT": "Austria", "IT": "Italia", "FR": "Franța", "ES": "Spania",
    "NL": "Țările de Jos", "BE": "Belgia", "PL": "Polonia", "CZ": "Cehia", "HU": "Ungaria",
    "BG": "Bulgaria", "GR": "Grecia", "PT": "Portugalia", "IE": "Irlanda", "GB": "Regatul Unit",
    "US": "SUA", "CA": "Canada", "CN": "China", "HK": "Hong Kong", "JP": "Japonia", "KR": "Coreea de Sud",
    "AU": "Australia", "CH": "Elveția", "TR": "Turcia", "UA": "Ucraina", "MD": "Republica Moldova",
}

SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£"}


def money(amount, currency):
    return f"{amount:,.0f} {SYMBOLS.get(currency, currency)}".replace(",", ".")


def landed_cost(price, shipping, currency, from_country, duty_pct=0.0):
    """(total, detail): price + shipping, plus duty, import VAT and a handling fee when it comes
    from outside the EU."""
    base = price + shipping
    parts = [money(price, currency)]
    if shipping:
        parts.append(f"{money(shipping, currency)} shipping")
    total = base
    if from_country and from_country not in EU:
        vat_pct = float(os.getenv("IMPORT_VAT_PCT") or 21)
        duty = base * duty_pct / 100
        vat = (base + duty) * vat_pct / 100
        fee_ron = float(os.getenv("IMPORT_FEE_RON") or 0)
        rate = rate_to_ron(currency)
        fee = fee_ron / rate if fee_ron and rate else 0
        total = base + duty + vat + fee
        if duty:
            parts.append(f"{money(duty, currency)} duty ({duty_pct:g}%)")
        parts.append(f"{money(vat, currency)} import VAT ({vat_pct:g}%)")
        if fee:
            parts.append(f"{money(fee, currency)} handling")
    return round(total, 2), " + ".join(parts)


class EbayProvider(Provider):
    name = "ebay"
    label = "eBay"
    seller_info = ("reviews",)
    search_delay = 2

    def __init__(self):
        super().__init__()
        self._token, self._token_until = None, 0

    def token(self):
        if self._token and time.time() < self._token_until - 60:
            return self._token
        client_id, secret = os.getenv("EBAY_CLIENT_ID"), os.getenv("EBAY_CLIENT_SECRET")
        if not client_id or not secret:
            raise ProviderError("eBay needs API keys: set EBAY_CLIENT_ID and EBAY_CLIENT_SECRET "
                                "(a free Production keyset from developer.ebay.com)")
        auth = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
        resp = self.session.post(f"{API}/identity/v1/oauth2/token", timeout=30,
                                 headers={"Authorization": f"Basic {auth}",
                                          "Content-Type": "application/x-www-form-urlencoded"},
                                 data={"grant_type": "client_credentials", "scope": SCOPE})
        if resp.status_code in (400, 401):
            raise ProviderError("eBay refused the API keys (EBAY_CLIENT_ID / EBAY_CLIENT_SECRET)")
        resp.raise_for_status()
        data = resp.json()
        self._token, self._token_until = data["access_token"], time.time() + int(data.get("expires_in", 7200))
        return self._token

    def search(self, spec):
        parts = urlsplit(spec.query_url)
        host = parts.netloc.lower().removeprefix("www.")
        marketplace = MARKETPLACES.get(host)
        if not marketplace or not parts.path.startswith("/sch/"):
            raise ProviderError("query_url must be an eBay search (e.g. https://www.ebay.de/sch/i.html?_nkw=...)")
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        keywords = (query.get("_nkw") or "").replace("+", " ").strip()
        if not keywords:
            raise ProviderError("The eBay link has no search words (_nkw)")
        ship_to = (spec.options.get("ship_to") or os.getenv("EBAY_SHIP_TO") or "RO").upper()

        filters = [f"deliveryCountry:{ship_to}"]
        if not spec.options.get("auctions"):
            filters.append("buyingOptions:{FIXED_PRICE|BEST_OFFER}")
        lo, hi = query.get("_udlo"), query.get("_udhi")
        if lo or hi:
            currency = {"EBAY_US": "USD", "EBAY_GB": "GBP", "EBAY_PL": "PLN", "EBAY_CH": "CHF",
                        "EBAY_CA": "CAD", "EBAY_AU": "AUD"}.get(marketplace, "EUR")
            filters += [f"price:[{lo or ''}..{hi or ''}]", f"priceCurrency:{currency}"]
        params = {"q": keywords, "limit": "100", "sort": "newlyListed", "filter": ",".join(filters)}
        if query.get("_sacat") and query["_sacat"] != "0":
            params["category_ids"] = query["_sacat"]

        resp = self.session.get(f"{API}/buy/browse/v1/item_summary/search", params=params, timeout=30, headers={
            "Authorization": f"Bearer {self.token()}",
            "X-EBAY-C-MARKETPLACE-ID": marketplace,
            "X-EBAY-C-ENDUSERCTX": f"contextualLocation=country%3D{ship_to}",
            "Accept": "application/json",
        })
        if resp.status_code == 429:
            raise ProviderError("eBay's API limit was reached (5,000 calls a day); try a longer interval")
        if resp.status_code >= 400:
            errors = (resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}).get("errors") or []
            raise ProviderError("eBay API error: " + ("; ".join(e.get("message", "") for e in errors) or f"HTTP {resp.status_code}"))

        duty_pct = float(spec.options.get("duty_pct") or 0)
        listings = []
        for item in resp.json().get("itemSummaries") or []:
            listings.append(self.parse(item, ship_to, duty_pct))
        return listings

    @staticmethod
    def parse(item, ship_to="RO", duty_pct=0.0):
        price = item.get("price") or {}
        currency = (price.get("currency") or "EUR").upper()
        amount = float(price["value"]) if price.get("value") not in (None, "") else None
        # The cheapest way to get it to ship_to; none listed: it doesn't ship there
        costs = [float((o.get("shippingCost") or {}).get("value") or 0) for o in item.get("shippingOptions") or []
                 if (o.get("shippingCost") or {}).get("currency", currency).upper() == currency]
        shipping = min(costs) if costs else 0.0
        place = item.get("itemLocation") or {}
        country = (place.get("country") or "").upper()
        location = ", ".join(x for x in (place.get("city"), COUNTRIES.get(country, country)) if x) or None

        total, detail = (landed_cost(amount, shipping, currency, country, duty_pct) if amount is not None
                         else (None, None))
        seller = item.get("seller") or {}
        signals = {"description": item.get("shortDescription") or ""}
        if seller.get("feedbackScore") is not None:
            signals["seller_reviews"] = int(seller.get("feedbackScore") or 0)
        if seller.get("feedbackPercentage") not in (None, ""):
            signals["seller_rating"] = float(seller["feedbackPercentage"]) / 100
        images = [item.get("image")] + (item.get("additionalImages") or [])
        signals["photos"] = sum(1 for i in images if i and i.get("imageUrl"))

        return Listing(
            external_id=str(item.get("legacyItemId") or item.get("itemId")),
            title=(item.get("title") or "").strip(),
            url=item.get("itemWebUrl") or "",
            price=total,
            currency=currency,
            location=location,
            image_url=(item.get("image") or {}).get("imageUrl"),
            posted_at=parse_iso_utc(item.get("itemCreationDate")),
            signals=signals,
            price_detail=detail if detail and detail != money(amount or 0, currency) else None,
        )
