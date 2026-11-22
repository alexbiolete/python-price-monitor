"""Rough exchange rates to RON, for the searches' min/max price (which are in lei) and
eBay's handling fee. Override any of them in .env as <CURRENCY>_TO_RON, e.g. EUR_TO_RON=4.97."""

import os

DEFAULTS = {"RON": 1.0, "EUR": 4.97, "USD": 4.45, "GBP": 5.85, "PLN": 1.16, "CHF": 5.30}


def rate_to_ron(currency):
    """Lei per unit of this currency, or None if unknown."""
    currency = (currency or "RON").upper()
    raw = os.getenv(f"{currency}_TO_RON")
    try:
        return float(raw) if raw else DEFAULTS.get(currency)
    except ValueError:
        return DEFAULTS.get(currency)


def to_ron(amount, currency):
    rate = rate_to_ron(currency)
    return amount * rate if rate else amount
