"""Registered platforms. searches.platform must match a provider's `name`."""

from .ebay import EbayProvider
from .facebook import FacebookProvider
from .olx import OlxProvider
from .publi24 import Publi24Provider
from .vinted import VintedProvider

PROVIDERS = {p.name: p for p in (OlxProvider, Publi24Provider, EbayProvider, VintedProvider, FacebookProvider)}


def get_provider_class(platform):
    return PROVIDERS.get((platform or "").strip().lower())
