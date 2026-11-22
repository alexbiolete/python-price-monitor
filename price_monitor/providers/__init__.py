"""Registered platforms. searches.platform must match a provider's `name`."""

from .facebook import FacebookProvider
from .olx import OlxProvider
from .vinted import VintedProvider

PROVIDERS = {p.name: p for p in (OlxProvider, VintedProvider, FacebookProvider)}


def get_provider_class(platform):
    return PROVIDERS.get((platform or "").strip().lower())
