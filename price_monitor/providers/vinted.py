"""Vinted, from the search results embedded in its catalog page.

Vinted renders search results on its server and embeds them in the page
(Next.js "flight" data, self.__next_f.push(...) chunks). There is no stable
public API, so this reads that data; if Vinted changes the page, the search's
last_error will say the results could not be found.

query_url: a catalog URL from the browser, filters set, e.g.
  https://www.vinted.ro/catalog?search_text=playstation%205&price_from=1000&currency=RON&order=newest_first
options:   {"price": "total"}  compare the price including the buyer protection fee
           (default), or "item" for the seller's price alone

Posting dates: the results carry none, and "newest first" isn't strict (old items
mix in, some years old). Item ids only grow, though, at a steady pace (~15
million a day in late 2026), so an item's age is estimated from how far its id
is below the newest one, which one extra request to the unfiltered newest
listings gives each run. The pace corrects itself from what the monitor sees
over the days (kept in the search's provider_state). Estimates are close for
days and weeks, rougher for years, which is what an age limit needs.

Note: vinted.ro also shows items from other countries (Poland, Greece, ...),
so the search's exclude keywords may need words in those languages.
"""

import json
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .base import Listing, Provider, ProviderError
from ..risk import contact_in_text

DEFAULT_IDS_PER_DAY = 15_000_000   # measured 2026-11: 2 days ≈ 33M ids, 5 weeks ≈ 534M
CALIBRATE_AFTER = timedelta(days=1)

_PUSH = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')
_ITEMS = re.compile(r'"items":\{"items":\[')


def _with_newest_first(url):
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if not any(k == "order" for k, _ in query):
        query.append(("order", "newest_first"))
    return urlunsplit(parts._replace(query=urlencode(query), fragment=""))


def page_payload(html):
    return "".join(json.loads(chunk) for chunk in _PUSH.findall(html))


def section(payload, name):
    """An item page's section ({"data": ..., "name": name, ...}) as a dict, or None."""
    i = payload.find(f'"name":"{name}","section"')
    if i < 0:
        return None
    start = payload.rfind('{"data":', 0, i)
    try:
        return json.JSONDecoder().raw_decode(payload, start)[0] if start >= 0 else None
    except ValueError:
        return None


# "acum 5 zile" -> 5 days (Vinted's Romanian; "o"/"un" is one)
_UNITS = {"secund": 1 / 86400, "minut": 1 / 1440, "or": 1 / 24, "zi": 1, "săptămân": 7, "saptaman": 7,
          "lun": 30, "an": 365}


def relative_days(text):
    m = re.search(r"acum\s+(o|un|una|\d+)\s+([a-zăâîșț]+)", (text or "").lower())
    if not m:
        return None
    count = 1 if m.group(1) in ("o", "un", "una") else int(m.group(1))
    word = m.group(2)
    for prefix, days in _UNITS.items():
        if word.startswith(prefix):
            return count * days
    return None


def items_from_page(html):
    payload = page_payload(html)
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
    needs_enrich = True
    item_delay = 1.5

    def __init__(self):
        super().__init__()
        self._home = None    # the site whose homepage we visited for session cookies
        self._newest = {}    # site -> (newest item id, when), once per run

    def newest_id(self, base):
        """The newest item id on the site right now (from the unfiltered newest listings)."""
        if base not in self._newest:
            resp = self.session.get(base + "/catalog?order=newest_first", timeout=30)
            resp.raise_for_status()
            items, _ = items_from_page(resp.text)
            ids = [int(i["id"]) for i in items if str(i.get("id", "")).isdigit()]
            self._newest[base] = (max(ids), datetime.utcnow().replace(microsecond=0)) if ids else (None, None)
            self.sleep_between_pages()
        return self._newest[base]

    def enrich(self, listing):
        """The item's page: its description, the seller's reviews, and when it was uploaded
        ("acum 5 săptămâni"); the older of that and the id estimate is kept."""
        import time
        time.sleep(self.item_delay)
        resp = self.session.get(listing.url, timeout=30)
        if resp.status_code in (403, 429):
            raise ProviderError(f"Vinted refused an item page (HTTP {resp.status_code}); possibly rate-limited")
        if resp.status_code == 404:
            return  # sold or removed meanwhile
        resp.raise_for_status()
        payload = page_payload(resp.text)
        description = ((section(payload, "description") or {}).get("data") or {}).get("description") or ""
        seller = (section(payload, "user_info_header") or {}).get("data") or {}
        gallery = (section(payload, "gallery") or {}).get("data") or {}
        signals = listing.signals
        signals["description"] = description
        signals["contact_in_text"] = contact_in_text(description)
        if "feedback_count" in seller:
            signals["seller_reviews"] = int(seller.get("feedback_count") or 0)
            signals["seller_rating"] = float(seller.get("feedback_reputation") or 0)
        photos = gallery.get("photos") or gallery.get("images")
        if isinstance(photos, list):
            signals["photos"] = len(photos)
        self.seller(listing, base=f"{urlsplit(listing.url).scheme}://{urlsplit(listing.url).netloc}")
        attrs = ((section(payload, "attributes") or {}).get("data") or {}).get("attributes") or []
        uploaded = next((a.get("data", {}).get("value") for a in attrs if a.get("code") == "upload_date"), None)
        days = relative_days(uploaded)
        if days is not None:
            from_page = datetime.utcnow().replace(microsecond=0) - timedelta(days=days)
            if listing.posted_at is None or from_page < listing.posted_at:
                listing.posted_at = from_page

    def seller(self, listing, base):
        """The seller's public profile (a small JSON): where they are, for the listing's location
        ("Toruń, Polonia", or just the country), and their reviews, for the scam signals.
        Nothing about who they are is kept."""
        if not listing.seller_id:
            return
        import time
        time.sleep(1)
        resp = self.session.get(f"{base}/api/v2/users/{listing.seller_id}",
                                headers={"Accept": "application/json"}, timeout=30)
        if resp.status_code != 200:
            return  # the item page's review count is still there
        user = (resp.json() or {}).get("user") or {}
        city = (user.get("city") or "").strip()
        country = (user.get("country_title_local") or user.get("country_title") or "").strip()
        listing.location = ", ".join(x for x in (city, country) if x) or None
        if "feedback_count" in user:
            listing.signals["seller_reviews"] = int(user.get("feedback_count") or 0)
            listing.signals["seller_rating"] = float(user.get("feedback_reputation") or 0)
            listing.signals["seller_negative_reviews"] = int(user.get("negative_feedback_count") or 0)

    @staticmethod
    def ids_per_day(state, newest, now):
        """The pace new ids appear at: measured from an anchor kept in the search's state
        (once it's a day old), else the default. The anchor renews monthly."""
        anchor = state.get("id_anchor")
        if anchor:
            then = datetime.fromisoformat(anchor["at"])
            days = (now - then).total_seconds() / 86400
            if days > 31:
                anchor = None
            elif now - then >= CALIBRATE_AFTER and newest > anchor["id"]:
                state["ids_per_day"] = round((newest - anchor["id"]) / days)
        if not anchor:
            state["id_anchor"] = {"id": newest, "at": now.isoformat()}
        return state.get("ids_per_day") or DEFAULT_IDS_PER_DAY

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
        newest, now = self.newest_id(base)
        rate = self.ids_per_day(spec.state, newest, now) if newest else None
        listings = []
        for item in items:
            price = (item.get("totalItemPrice") if use_total else None) or item.get("price") or {}
            amount = price.get("amount")
            posted_at = None
            if rate and str(item.get("id", "")).isdigit():
                behind = max(0, newest - int(item["id"]))
                posted_at = now - timedelta(days=behind / rate)
            listings.append(Listing(
                external_id=str(item["id"]),
                title=(item.get("title") or "").strip(),
                url=base + item["url"] if str(item.get("url", "")).startswith("/") else item.get("url") or "",
                price=float(amount) if amount not in (None, "") else None,
                currency=(price.get("currencyCode") or "").upper() or None,
                image_url=item.get("thumbnailUrl"),
                posted_at=posted_at.replace(microsecond=0) if posted_at else None,
                seller_id=str((item.get("user") or {}).get("id") or "") or None,
            ))
        return listings
