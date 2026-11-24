"""Exchange rates to RON, for the searches' min/max price (which are in lei), listings.price_ron
and eBay's handling fee.

The National Bank of Romania's daily reference rates (BNR, curs.bnr.ro), fetched at most every
REFRESH_EVERY and kept in settings (exchange_rates), so a failed fetch keeps the last ones. No rates
are built in or configured: until the first fetch succeeds, a price in another currency has no
value in lei (None).
"""

import json
import logging
import re
from datetime import timedelta

import requests

from .db import from_db_time, to_db_time

log = logging.getLogger("price_monitor")

BNR_URL = "https://curs.bnr.ro/nbrfxrates.xml"
REFRESH_EVERY = timedelta(hours=6)  # BNR publishes once a working day, around 13:00 Romanian time

_RATE = re.compile(r'<Rate currency="([A-Z]{3})"(?:\s+multiplier="(\d+)")?\s*>([\d.]+)</Rate>')
_DATE = re.compile(r'<Cube date="(\d{4}-\d{2}-\d{2})"')

_live = {}  # the rates in use this run (from settings), by currency


def rate_to_ron(currency):
    """Lei per unit of this currency, or None if unknown (no rates fetched yet, or not one BNR lists)."""
    currency = (currency or "RON").upper()
    return 1.0 if currency == "RON" else _live.get(currency)


def to_ron(amount, currency):
    """The amount in lei, or None when there's no rate for the currency."""
    rate = rate_to_ron(currency)
    return amount * rate if rate else None


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
        # Tried again on the next run; meanwhile the last ones stay in use
        log.warning("Couldn't fetch BNR's exchange rates (%s); %s", e,
                    "using the last ones" if _live else "prices in other currencies wait until they're fetched")
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
