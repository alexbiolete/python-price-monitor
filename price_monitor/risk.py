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
        "achit in avans", "achitati in avans", "achitare in avans", "plata integrala inainte",
        "trimit dupa plata", "trimit dupa ce platesti", "dupa confirmarea platii",
        "payment in advance", "pay in advance", "upfront", "przedplata", "zaliczka", "προκαταβολη",
    ],
    "asks for a money transfer": [
        "western union", "moneygram", "transfer bancar", "virament", "revolut", "paypal friends",
        "iban", "cont bancar", "transferwise", "wise.com", "skrill", "paysafe", "bitcoin", "crypto",
        "usdt", "gift card", "card cadou", "bank transfer", "przelew", "blik",
    ],
    "sends a payment link or code": [
        "link de plata", "link plata", "cod de plata", "link de livrare", "plata prin link", "link pentru plata",
        "link de confirmare", "datele cardului", "date card", "verificare card", "cod sms", "cod de verificare",
        "codul primit", "payment link",
    ],
    "only ships, no meeting": [
        "doar prin curier", "numai prin curier", "doar livrare", "trimit doar", "doar cu livrare",
        "doar expediere", "nu predau personal", "fara predare personala", "nu ma intalnesc", "nu fac intalniri",
        "fara intalnire", "only shipping", "only by courier", "only delivery", "no meetings", "tylko wysyłka",
    ],
    "says they're abroad": [
        "sunt in strainatate", "nu sunt in tara", "sunt plecat din tara", "ma aflu in strainatate",
        "lucrez in strainatate", "plecat in strainatate", "i am abroad", "im abroad",
    ],
}

# Worth a note, no points: talking on a messaging app is common among honest sellers too, but it's where
# deals leave the platform's protection. Shown with the notes (and among a level's reasons).
NOTE_PHRASES = {
    "mentions WhatsApp or Telegram": [
        "whatsapp", "whats app", "wapp", "whatsap", "watsapp", "watsap", "wsp", "telegram", "viber",
        "signal app", "wa.me", "t.me",
    ],
}
# Before, a messaging app was a scored phrase of this kind; stored signals may still carry it
OLD_MESSAGING_REASON = "wants to talk on WhatsApp or Telegram"

# Milder: pushing for a quick sale. Honest sellers write "vand urgent" too, so it's 1 point (alone: a
# slight risk), on top of the signs above. Same matching.
RUSH_PHRASES = {
    "pushes for a quick sale": [
        "urgent", "urgenta", "vand repede", "doar azi", "azi doar", "numai azi", "pana diseara",
        "primul venit", "plec din tara", "asap", "today only", "first come", "pilne", "pilnie", "επειγον",
    ],
}

PHONE = re.compile(r"(?<!\d)(?:\+?\d[\s.\-]?){9,13}(?!\d)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")

# How far below the typical price counts, and the points for it
PRICE_STEPS = [(0.5, 3, "half the typical price or less"), (0.65, 2, None)]
MIN_COMPARABLES = 5

LEVELS = [(4, "high"), (2, "medium"), (1, "low")]  # shown yellow, orange and red


def plain(text):
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def phrase_reasons(title, description, groups=None):
    """One reason per kind of phrase found in the title or description (groups: PHRASES by default)"""
    text = f" {plain(title)} {plain(description)} "
    found = []
    for reason, words in (groups or PHRASES).items():
        for w in words:
            if re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", text):
                found.append(f'{reason} ("{w}")')
                break
    return found


def static_signals(listing, platform):
    """What the listing itself shows, as a small JSON-able dict (stored on the listing)."""
    s = dict(listing.signals or {})
    description = s.pop("description", "")
    s["phrases"] = phrase_reasons(listing.title, description)
    s["rush"] = phrase_reasons(listing.title, description, RUSH_PHRASES)
    s["messaging"] = phrase_reasons(listing.title, description, NOTE_PHRASES)
    return s


def typical_price(prices):
    """The median of comparable prices (RON), or None when there are too few to say."""
    prices = [p for p in prices if p and p > 0]
    return statistics.median(prices) if len(prices) >= MIN_COMPARABLES else None


def assess(signals, price_ron, typical, fmt_price):
    """(level or None, score, reasons, notes) for a listing. A seller with no reviews yet adds its
    point, but alone it gives no level: it's only a note (a grey mention), shown when there's no
    level. A level needs at least one other sign. A messaging app (WhatsApp...) is a note with no points."""
    score, reasons, notes = 0, [], []
    weak = 0  # points that alone give no level

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
        weak += 1
        reasons.append("seller has no reviews")
        notes.append("seller has no reviews yet")
    elif reviews and reviews >= 3 and signals.get("seller_rating") is not None and signals["seller_rating"] < 0.8:
        score += 1
        reasons.append(f"seller rated {signals['seller_rating'] * 5:.1f}/5")

    negative = signals.get("seller_negative_reviews") or 0
    if negative >= 2 and reviews and negative / reviews >= 0.1:
        score += 1
        reasons.append(f"seller has {negative} negative reviews out of {reviews}")

    phrases = [p for p in signals.get("phrases") or [] if not p.startswith(OLD_MESSAGING_REASON)]
    score += 2 * min(len(phrases), 2)
    reasons += phrases

    # A messaging app: a note, no points (also when stored as the old scored phrase)
    old = [p.replace(OLD_MESSAGING_REASON, "mentions WhatsApp or Telegram", 1)
           for p in signals.get("phrases") or [] if p.startswith(OLD_MESSAGING_REASON)]
    for note in (signals.get("messaging") or old)[:1]:
        reasons.append(note)
        notes.append(note)

    rush = signals.get("rush") or []
    if rush:
        score += 1
        reasons += rush

    if signals.get("contact_in_text"):
        score += 2
        reasons.append("puts a phone number or email in the ad, to talk off the platform")

    photos = signals.get("photos")
    if photos is not None and photos <= 1:
        score += 1
        reasons.append("only one photo" if photos == 1 else "no photos")

    if signals.get("unprotected") and score > weak:
        score += 1
        reasons.append("no buyer protection")

    level = next((name for minimum, name in LEVELS if score >= minimum), None) if score > weak else None
    return level, score, reasons, notes


def contact_in_text(text):
    return bool(PHONE.search(text or "") or EMAIL.search(text or ""))
