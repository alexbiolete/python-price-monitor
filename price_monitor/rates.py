"""Exchange rates to RON, for the searches' min/max price (which are in lei), listings.price_ron
and eBay's handling fee.

Live: the National Bank of Romania's daily reference rates (BNR, curs.bnr.ro), fetched at most
every REFRESH_EVERY and kept in settings (exchange_rates), so a failed fetch keeps the last ones.
A rate in .env as <CURRENCY>_TO_RON (e.g. EUR_TO_RON=5.0) overrides the live one; without either,
the rough DEFAULTS below.
"""

import json
import logging
import os
import re
from datetime import timedelta

import requests

from .db import from_db_time, to_db_time

log = logging.getLogger("price_monitor")

DEFAULTS = {"RON": 1.0, "EUR": 4.97, "USD": 4.45, "GBP": 5.85, "PLN": 1.16, "CHF": 5.30}
BNR_URL = "https://curs.bnr.ro/nbrfxrates.xml"
REFRESH_EVERY = timedelta(hours=6)  # BNR publishes once a working day, around 13:00 Romanian time

_RATE = re.compile(r'<Rate currency="([A-Z]{3})"(?:\s+multiplier="(\d+)")?\s*>([\d.]+)</Rate>')
_DATE = re.compile(r'<Cube date="(\d{4}-\d{2}-\d{2})"')

_live = {}  # the rates in use this run (from settings), by currency


def rate_to_ron(currency):
    """Lei per unit of this currency, or None if unknown."""
    currency = (currency or "RON").upper()
    raw = os.getenv(f"{currency}_TO_RON")
    try:
        if raw:
            return float(raw)
    except ValueError:
        pass
    return _live.get(currency) or DEFAULTS.get(currency)


def to_ron(amount, currency):
    rate = rate_to_ron(currency)
    return amount * rate if rate else amount


def parse_bnr(xml):
    """{currency: lei per unit} and the rates' date, from BNR's XML (None, None when unreadable)."""
    rates = {c: float(v) / int(m or 1) for c, m, v in _RATE.findall(xml or "")}
    date = _DATE.search(xml or "")
    return (rates or None), (date.group(1) if date else None)


def refresh(db, now):
    """Use the stored rates; fetch BNR's when they're missing or older than REFRESH_EVERY, store
    them, and work out every stored listing's price in lei again with them."""
    global _live
    row = db.one("SELECT exchange_rates, exchange_rates_at FROM settings WHERE id = 1") or {}
    try:
        _live = json.loads(row.get("exchange_rates") or "{}").get("rates") or {}
    except ValueError:
        _live = {}
    fetched = from_db_time(row.get("exchange_rates_at"))
    if fetched and now - fetched < REFRESH_EVERY:
        return
    try:
        resp = requests.get(BNR_URL, timeout=20, headers={"User-Agent": "price-monitor"})
        resp.raise_for_status()
        rates, date = parse_bnr(resp.text)
        if not rates:
            raise ValueError("no rates in the response")
    except Exception as e:  # the last rates stay in use
        log.warning("Couldn't fetch BNR's exchange rates (%s); using %s", e,
                    "the last ones" if _live else "the defaults")
        db.execute("UPDATE settings SET exchange_rates_at = ? WHERE id = 1", (to_db_time(now),))  # retry later
        db.commit()
        return
    _live = rates
    db.execute("UPDATE settings SET exchange_rates = ?, exchange_rates_at = ? WHERE id = 1",
               (json.dumps({"date": date, "source": "BNR", "rates": rates}), to_db_time(now)))
    # Listings' prices in lei with the new rates (those in lei don't change)
    for currency in {r["currency"] for r in db.all("SELECT DISTINCT currency FROM listings WHERE currency <> 'RON'")}:
        rate = rate_to_ron(currency)
        if rate:
            db.execute("UPDATE listings SET price_ron = price * ? WHERE currency = ?", (rate, currency))
    db.commit()
    log.info("Exchange rates from BNR (%s): 1 EUR = %s lei", date, rates.get("EUR"))
