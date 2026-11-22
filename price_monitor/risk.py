"""Possible-scam warning signs on a listing.

Heuristics, not verdicts: a flag means "look closer before paying". Signals
come from what the platform shows (providers fill Listing.signals) plus one
the monitor works out itself: a price far below the search's typical price.

Only derived facts are stored (e.g. "account 6 days old"), never who the
seller is.
"""

import re
import statistics
import unicodedata

# Words that move a deal off the platform's protection, or ask for money first.
# Matched without diacritics, in lowercase. Grouped so one listing gets one reason per kind.
PHRASES = {
    "asks for payment in advance": [
        "plata in avans", "platesti in avans", "plata anticipat", "avans", "plata inainte",
        "payment in advance", "pay in advance", "upfront", "przedplata",
    ],
    "asks for a money transfer": [
        "western union", "moneygram", "transfer bancar", "virament", "revolut", "paypal friends",
        "bank transfer", "przelew",
    ],
    "wants to talk on WhatsApp or Telegram": ["whatsapp", "whats app", "telegram", "viber", "signal app"],
    "sends a payment link or code": ["link de plata", "link plata", "cod de plata", "payment link", "link de livrare"],
    "only ships, no meeting": [
        "doar prin curier", "numai prin curier", "doar livrare", "trimit doar", "doar cu livrare",
        "only shipping", "only by courier",
    ],
    "says they're abroad": ["sunt in strainatate", "nu sunt in tara", "sunt plecat din tara", "i am abroad", "im abroad"],
}

PHONE = re.compile(r"(?<!\d)(?:\+?\d[\s.\-]?){9,13}(?!\d)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")

# How far below the typical price counts, and the points for it
PRICE_STEPS = [(0.5, 3, "half the typical price or less"), (0.65, 2, None)]
MIN_COMPARABLES = 5

LEVELS = [(4, "high"), (2, "medium"), (1, "low")]


def plain(text):
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def phrase_reasons(title, description):
    text = f" {plain(title)} {plain(description)} "
    found = []
    for reason, words in PHRASES.items():
        for w in words:
            if re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", text):
                found.append(f'{reason} ("{w}")')
                break
    return found


def static_signals(listing, platform):
    """What the listing itself shows, as a small JSON-able dict (stored on the listing)."""
    s = dict(listing.signals or {})
    s["phrases"] = phrase_reasons(listing.title, s.pop("description", ""))
    return s


def typical_price(prices):
    """The median of comparable prices (RON), or None when there are too few to say."""
    prices = [p for p in prices if p and p > 0]
    return statistics.median(prices) if len(prices) >= MIN_COMPARABLES else None


def assess(signals, price_ron, typical, fmt_price):
    """(level or None, score, reasons) for a listing."""
    score, reasons = 0, []

    if typical:
        ratio = price_ron / typical
        for limit, points, _ in PRICE_STEPS:
            if ratio <= limit:
                score += points
                reasons.append(f"{round((1 - ratio) * 100)}% below the typical price ({fmt_price(typical)})")
                break

    age = signals.get("account_age_days")
    if age is not None:
        if age < 7:
            score += 3
            reasons.append(f"seller account {age} day{'s' if age != 1 else ''} old")
        elif age < 30:
            score += 2
            reasons.append(f"seller account {age} days old")

    reviews = signals.get("seller_reviews")
    if reviews == 0:
        score += 1
        reasons.append("seller has no reviews")
    elif reviews and reviews >= 3 and signals.get("seller_rating") is not None and signals["seller_rating"] < 0.8:
        score += 1
        reasons.append(f"seller rated {signals['seller_rating'] * 5:.1f}/5")

    negative = signals.get("seller_negative_reviews") or 0
    if negative >= 2 and reviews and negative / reviews >= 0.1:
        score += 1
        reasons.append(f"seller has {negative} negative reviews out of {reviews}")

    phrases = signals.get("phrases") or []
    score += 2 * min(len(phrases), 2)
    reasons += phrases

    if signals.get("contact_in_text"):
        score += 2
        reasons.append("puts a phone number or email in the ad, to talk off the platform")

    photos = signals.get("photos")
    if photos is not None and photos <= 1:
        score += 1
        reasons.append("only one photo" if photos == 1 else "no photos")

    if signals.get("unprotected") and score > 0:
        score += 1
        reasons.append("no protected payment (OLX delivery off)")

    level = next((name for minimum, name in LEVELS if score >= minimum), None)
    return level, score, reasons


def contact_in_text(text):
    return bool(PHONE.search(text or "") or EMAIL.search(text or ""))
