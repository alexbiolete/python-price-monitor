"""Registered platforms. searches.platform must match a provider's `name`."""

from .olx import OlxProvider
from .vinted import VintedProvider

PROVIDERS = {p.name: p for p in (OlxProvider, VintedProvider)}


def get_provider_class(platform):
    return PROVIDERS.get((platform or "").strip().lower())
