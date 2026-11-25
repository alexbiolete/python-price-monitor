"""Exchange rates to RON, for the searches' min/max price (which are in lei), listings.price_ron
and eBay's handling fee.

The National Bank of Romania's daily reference rates (BNR, curs.bnr.ro), fetched at most every
REFRESH_EVERY and kept in settings (exchange_rates), so a failed fetch keeps the last ones. No real
rates are built in or configured; only PLACEHOLDER_RATE, as a very last resort.
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

# NOT A REAL EXCHANGE RATE. A deliberately round, unrealistic placeholder (lei per unit of any
# currency) used only when there's no BNR rate for it: before the very first fetch ever succeeded,
# or for a currency BNR doesn't list. Prices worked out with it are wrong; the next successful fetch
# recalculates every stored listing's price_ron with the real rates. Each use is logged.
PLACEHOLDER_RATE = 5.0

_RATE = re.compile(r'<Rate currency="([A-Z]{3})"(?:\s+multiplier="(\d+)")?\s*>([\d.]+)</Rate>')
_DATE = re.compile(r'<Cube date="(\d{4}-\d{2}-\d{2})"')

_live = {}  # the rates in use this run (from settings), by currency
_warned = set()  # currencies the placeholder was used for this run, logged once each


def rate_to_ron(currency):
    """Lei per unit of this currency: BNR's rate, else PLACEHOLDER_RATE (not a real rate; logged)."""
    currency = (currency or "RON").upper()
    if currency == "RON":
        return 1.0
    if currency in _live:
        return _live[currency]
    if currency not in _warned:
        _warned.add(currency)
        log.warning("No BNR exchange rate for %s; using the placeholder %s lei (not a real rate) until there is one",
                    currency, PLACEHOLDER_RATE)
    return PLACEHOLDER_RATE


def to_ron(amount, currency):
    return amount * rate_to_ron(currency)


def parse_bnr(xml):
    """{currency: lei per unit} and the rates' date, from BNR's XML (None, None when unreadable)."""
    rates = {c: float(v) / int(m or 1) for c, m, v in _RATE.findall(xml or "")}
    date = _DATE.search(xml or "")
    return (rates or None), (date.group(1) if date else None)


def load(db):
    """Use the stored rates (nothing fetched); returns the settings row they came from."""
    global _live
    row = db.one("SELECT exchange_rates, exchange_rates_at FROM settings WHERE id = 1") or {}
    try:
        _live = json.loads(row.get("exchange_rates") or "{}").get("rates") or {}
    except ValueError:
        _live = {}
    return row


def refresh(db, now):
    """Use the stored rates; fetch BNR's when they're missing or older than REFRESH_EVERY, store
    them, and work out every stored listing's price in lei again with them."""
    global _live
    row = load(db)
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
                    "using the last ones" if _live else "using the placeholder rate until they're fetched")
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


# Platforms that show a seller's price converted from the seller's own currency (Vinted: a Polish item in
# lei at Vinted's daily rate), so the shown price moves a little when the rate does. A provider says so
# with converts_currency; listed here too for the v16 upgrade, which marks such changes already stored.
RATE_PLATFORMS = ["vinted"]
# A change this small on such a platform is the rate moving (daily rates move well under 1%), not the
# seller: a seller rarely changes a price by under 2%
RATE_CHANGE_MAX_PCT = 3


def rate_change(old, new):
    """The price moved by under RATE_CHANGE_MAX_PCT: a currency conversion, where the platform converts"""
    return bool(old) and old > 0 and abs(new - old) / old * 100 < RATE_CHANGE_MAX_PCT
