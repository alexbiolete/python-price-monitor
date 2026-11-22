"""Publi24.ro, from its search results page (server-rendered HTML).

query_url: a Publi24 search from the browser, filters set, e.g.
  https://www.publi24.ro/anunturi/?q=playstation+5
  https://www.publi24.ro/anunturi/electronice/console-jocuri/?q=ps5

One page per run (20 listings). Each card gives the title, link, photo and photo
count, price, place and date ("azi 15:17", "ieri 09:02", "12 noiembrie"); the
page's structured data adds each ad's description, for the scam phrases.
"""

import html
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from .base import Listing, Provider, ProviderError

_CARD = re.compile(r'<div class="article-item[^"]*"\s+data-articleid="([^"]+)"(.*?)(?=<div class="article-item|\Z)', re.S)
_MONTHS = {"ianuarie": 1, "februarie": 2, "martie": 3, "aprilie": 4, "mai": 5, "iunie": 6, "iulie": 7,
           "august": 8, "septembrie": 9, "octombrie": 10, "noiembrie": 11, "decembrie": 12}
_CURRENCIES = {"ron": "RON", "lei": "RON", "eur": "EUR", "€": "EUR", "euro": "EUR", "usd": "USD", "$": "USD"}


def _text(fragment):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment or ""))).strip()


def _field(card, cls):
    m = re.search(r'class="' + cls + r'[^"]*"[^>]*>(.*?)</(?:p|span|div)>', card, re.S)
    return _text(m.group(1)) if m else ""


def parse_price(text):
    """("1.500 RON", "4,300 RON") -> (1500.0 / 4300.0, "RON"); (None, None) without a number
    ("Schimb", "La cerere"). Dots, spaces and a comma before exactly 3 digits group thousands;
    a comma before 1-2 digits is the decimal point."""
    m = re.search(r"\d[\d.,\s]*", text or "")
    if not m:
        return None, None
    number = m.group(0).strip().replace(" ", "").replace(".", "")
    number = re.sub(r",(?=\d{3}(?!\d))", "", number).replace(",", ".")
    try:
        amount = float(number)
    except ValueError:
        return None, None
    unit = re.sub(r"[\d.,\s]", " ", text).lower().split()
    currency = next((_CURRENCIES[u] for u in unit if u in _CURRENCIES), "RON")
    return (amount if amount > 0 else None), currency


def parse_date(text, now=None):
    """Publi24's dates, in Romania's time ("azi 15:17", "ieri 09:02", "12 noiembrie [2025]"), as
    naive UTC. Romania is UTC+2 in winter and UTC+3 in summer; an hour off doesn't matter for an
    age limit in days."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    local_now = now + timedelta(hours=2)
    text = (text or "").lower().strip()
    hm = re.search(r"(\d{1,2}):(\d{2})", text)
    hour, minute = (int(hm.group(1)), int(hm.group(2))) if hm else (12, 0)
    if text.startswith("azi"):
        day = local_now.date()
    elif text.startswith("ieri"):
        day = (local_now - timedelta(days=1)).date()
    else:
        m = re.search(r"(\d{1,2})\s+([a-zăâîșț]+)\.?(?:\s+(\d{4}))?", text)
        if not m or m.group(2)[:3] not in {k[:3] for k in _MONTHS}:
            return None
        month = next(v for k, v in _MONTHS.items() if k.startswith(m.group(2)[:3]))
        year = int(m.group(3)) if m.group(3) else local_now.year
        try:
            day = datetime(year, month, int(m.group(1))).date()
        except ValueError:
            return None
        if not m.group(3) and day > local_now.date():
            day = day.replace(year=year - 1)  # "28 decembrie" seen in January
    return datetime(day.year, day.month, day.day, hour, minute) - timedelta(hours=2)


class Publi24Provider(Provider):
    name = "publi24"
    label = "Publi24"
    search_delay = 3

    def search(self, spec):
        parts = urlsplit(spec.query_url)
        if parts.netloc not in ("www.publi24.ro", "publi24.ro") or not parts.path.startswith("/anunturi"):
            raise ProviderError("query_url must be a Publi24 search (https://www.publi24.ro/anunturi/?q=...)")
        resp = self.session.get(spec.query_url, timeout=30)
        if resp.status_code in (403, 429):
            raise ProviderError(f"Publi24 refused the request (HTTP {resp.status_code}); possibly rate-limited")
        resp.raise_for_status()
        page = resp.text

        # Descriptions from the page's structured data, by title
        descriptions = {}
        m = re.search(r'<script type="application/ld\+json">(.*?)</script>', page, re.S)
        if m:
            try:
                data = json.loads(m.group(1), strict=False)
                for el in data.get("itemListElement") or []:
                    if el.get("name"):
                        descriptions[_text(el["name"])] = html.unescape(el.get("description") or "")
            except ValueError:
                pass

        cards = _CARD.findall(page)
        if not cards and "article-item" not in page:
            raise ProviderError("Could not find search results in the Publi24 page; its layout may have changed")

        listings = []
        for article_id, card in cards:
            link = re.search(r'<h2 class="article-title">\s*<a href="([^"]+)"[^>]*>(.*?)</a>', card, re.S)
            if not link:
                continue
            title = _text(link.group(2))
            # A reduced price shows the old one too; the new one is what it costs
            price, currency = parse_price(_field(card, "new-price") or _field(card, "article-price"))
            image = re.search(r'<img[^>]+src="([^"]+)"', card)
            photos = _field(card, "article-img-count-number")
            place = _field(card, "article-location")
            listings.append(Listing(
                external_id=article_id,
                title=title,
                url=link.group(1),
                price=price,
                currency=currency,
                location=f"{place}, România" if place else "România",
                image_url=image.group(1) if image else None,
                posted_at=parse_date(_field(card, "article-date")),
                signals={"description": descriptions.get(title, ""),
                         **({"photos": int(photos)} if photos.isdigit() else {})},
            ))
        return listings
