"""What a platform adapter (provider) looks like.

A provider turns one search into a list of Listings. Everything else
(filtering, change detection, storage, email) is shared. To add a platform,
write a module in this package with a Provider subclass and register it in
providers/__init__.py; its `name` is the value of searches.platform.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

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

    def __init__(self):
        self.log = logging.getLogger(f"price_monitor.{self.name}")
        self._session = None

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
