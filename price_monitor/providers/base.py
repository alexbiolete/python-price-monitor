"""What a platform adapter (provider) looks like.

A provider turns one search into a list of Listings. Everything else
(filtering, change detection, storage, email) is shared. To add a platform,
write a module in this package with a Provider subclass and register it in
providers/__init__.py; its `name` is the value of searches.platform.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit

import requests

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ro-RO,ro;q=0.9,en;q=0.8",
}


class ProviderError(Exception):
    """A problem worth showing as the search's last_error (bad URL, blocked, layout changed)."""


@dataclass
class Listing:
    external_id: str
    title: str
    url: str
    price: Optional[float]      # None: no price (swap-only, "price on request")
    currency: Optional[str]     # ISO code, e.g. RON, EUR
    location: Optional[str] = None
    image_url: Optional[str] = None
    posted_at: Optional[datetime] = None  # when the ad was posted (naive UTC); None: the platform doesn't say
    # Possible-scam signals the platform shows (see risk.py): account_age_days, seller_reviews,
    # seller_rating (0-1), photos, unprotected, contact_in_text, and "description" (text, not stored)
    signals: dict = field(default_factory=dict)
    # The seller's id on the platform, for enrich() to look up (e.g. their location); never stored
    seller_id: Optional[str] = None
    # How price adds up, when it's more than the asking price (eBay: "219 € + 25 € shipping + …")
    price_detail: Optional[str] = None


# A repost: the page shows an upload at most this many days ago, and the ad is older than that by at
# least a day and a quarter of its age (the page's times are coarse: "acum 3 zile", "acum o lună")
REPOST_SHOWN_WITHIN_DAYS = 7


def is_repost(posted_at, uploaded, now):
    """The page's upload is a repost of an older ad (see REPOST_SHOWN_WITHIN_DAYS)"""
    if not posted_at or not uploaded:
        return False
    shown_age = (now - uploaded).total_seconds() / 86400
    gap = (uploaded - posted_at).total_seconds() / 86400
    age = (now - posted_at).total_seconds() / 86400
    return shown_age <= REPOST_SHOWN_WITHIN_DAYS and gap >= max(1, age / 4)


def parse_iso_utc(value):
    """An ISO time with an offset ("2026-11-22T12:56:39+02:00") as naive UTC, or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(microsecond=0)


@dataclass
class Check:
    """What an ad's own page says: gone (sold or removed), and its price when the page shows one
    comparable to the search results' (None: leave the stored price)."""
    gone: bool
    price: Optional[float] = None
    currency: Optional[str] = None
    # When the page says the ad was (re)uploaded, where it says (Vinted), for reposts
    uploaded_at: Optional[datetime] = None


@dataclass
class SearchSpec:
    """The parts of a searches row a provider needs."""
    id: int
    name: str
    query_url: str
    options: dict          # edited in the dashboard (e.g. {"category_id": 1882})
    state: dict            # owned by the monitor (e.g. a detected category id); mutate to save


class Provider:
    name = ""
    label = ""
    # Seconds between pages of one search, and between searches on this platform
    page_delay = 2
    search_delay = 3
    # A search on this platform runs at most this often, whatever its interval (0: no limit)
    min_interval_minutes = 0
    # What the platform shows about a seller, for the scam signals: "reviews" (count, rating),
    # "account_age". The dashboard says what a platform doesn't show
    seller_info = ()

    def __init__(self):
        self.log = logging.getLogger(f"price_monitor.{self.name}")
        self._session = None
        self._sellers = {}  # seller_signals() answers this run, by seller id
        self._sellers_refused = False

    @property
    def session(self):
        """One HTTP session per provider per run (cookies are kept between searches)."""
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update(BROWSER_HEADERS)
            self.prepare_session(self._session)
        return self._session

    def prepare_session(self, session):
        """Hook: e.g. visit the homepage to get session cookies."""

    def sleep_between_pages(self):
        time.sleep(self.page_delay)

    def search(self, spec: SearchSpec) -> list:
        raise NotImplementedError

    # Some platforms show more on an item's own page (its description, the seller's reviews, when
    # it was posted). The runner calls enrich() only for listings it hasn't stored yet and that
    # pass the search's filters, at most max_enrich per search per run, to keep traffic low.
    needs_enrich = False
    max_enrich = 15

    def enrich(self, listing: Listing) -> None:
        """Hook: fill listing.signals (and posted_at) from the item's page."""

    # Some platforms rate sellers through a separate service (OLX). The runner looks a seller up for
    # listings it has no seller rating for yet, at most max_seller_lookups per search per run (in the
    # price range first), and keeps the answer on the listing. A seller is looked up once per run.
    rates_sellers = False
    max_seller_lookups = 40
    seller_lookup_delay = 0.3

    def seller_signals(self, listing: Listing) -> Optional[dict]:
        """The seller's rating as signals (seller_reviews, seller_rating 0-1, seller_negative_reviews),
        or None when it can't be read. Cached per seller for the run; after a refusal (rate limit),
        no more lookups this run."""
        key = listing.seller_id
        if not key or self._sellers_refused:
            return None
        if key not in self._sellers:
            if self._sellers:
                time.sleep(self.seller_lookup_delay)
            try:
                self._sellers[key] = self.fetch_seller_signals(listing)
            except ProviderError as e:
                self.log.warning("Stopped looking up sellers this run: %s", e)
                self._sellers_refused = True
                return None
            except Exception as e:  # one seller doesn't stop the run
                self.log.warning("Couldn't read a seller's rating: %s", e)
                self._sellers[key] = None
        return self._sellers[key]

    def fetch_seller_signals(self, listing: Listing) -> Optional[dict]:
        """Hook for rates_sellers: one lookup. Raise ProviderError when refused."""
        return None

    # Stored ads a search no longer shows are re-checked on their own page (the runner picks them),
    # at most max_check per search per run, check_delay seconds apart
    max_check = 10
    check_delay = 2
    # Also re-read the pages of ads the run did see (each at most every RECHECK_EVERY), for what only
    # the page shows: Vinted's upload time, to notice reposts
    recheck_seen = False

    def check(self, url: str) -> Optional[Check]:
        """Is the ad still up? Default: a 404 or 410 means gone; anything else can't tell (None).
        Providers that can read the page override this."""
        resp = self.fetch_ad(url)
        return Check(gone=True) if resp is None else None

    def fetch_ad(self, url):
        """The ad's page, or None when it's gone (404/410, or redirected away from the ad)."""
        resp = self.session.get(url, timeout=30)
        if resp.status_code in (403, 429):
            raise ProviderError(f"{self.label} refused an ad page (HTTP {resp.status_code}); possibly rate-limited")
        if resp.status_code in (404, 410):
            return None
        resp.raise_for_status()
        if resp.history and not self.same_ad(url, resp.url):
            return None  # sent to a category or home page: the ad is gone
        return resp

    def same_ad(self, url, final_url):
        """After a redirect: still the ad's page? Default: the same path. Override where an ad's
        address changes with its title."""
        return urlsplit(final_url).path == urlsplit(url).path
