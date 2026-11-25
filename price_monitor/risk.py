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
    "delivers only by courier, outside the platform": [
        "doar prin fan courier", "numai prin fan courier", "doar cu fan courier", "doar prin fan", "doar prin sameday",
        "doar prin cargus", "doar prin dpd", "doar prin gls", "doar prin posta", "doar prin easybox",
        "livrarea este disponibila doar prin", "livrare doar prin", "expediez doar prin",
    ],
    "says they're abroad": [
        "sunt in strainatate", "nu sunt in tara", "sunt plecat din tara", "ma aflu in strainatate",
        "lucrez in strainatate", "plecat in strainatate", "i am abroad", "im abroad",
    ],
}

# The points each kind adds: what takes the deal off the platform's protection, or asks for money first,
# weighs most; a courier of their own (with a meeting still possible) least
PHRASE_POINTS = {
    "asks for payment in advance": 3,
    "sends a payment link or code": 3,
    "asks for a money transfer": 2,
    "only ships, no meeting": 2,
    "says they're abroad": 2,
    "delivers only by courier, outside the platform": 1,
}

# Offers to meet (a handover in person, a local pickup): then the missing buyer protection isn't held
# against it (the buyer can see it before paying), and "only ships" can't be right
MEET_PHRASES = {
    "offers to meet": [
        "predare personala", "predarea personala", "predare in mana", "predarea in mana", "predare pe loc",
        "ridicare locala", "ridicarea locala", "ridicare personala", "ridicarea personala", "se poate ridica",
        "ridicare din", "ridicarea din", "ma pot intalni", "intalnire in", "vin la tine", "testare la predare",
        "verificare la predare", "personal pickup", "local pickup", "pick up in person", "odbior osobisty",
        "odbiór osobisty", "παραδοση χερι",
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

# Pushing for a quick sale ("urgent"): 2 points, a possible scam on its own. Same matching.
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
    s["meets"] = bool(phrase_reasons(listing.title, description, MEET_PHRASES))
    return s


def with_title_phrases(signals, title):
    """Stored signals (read from an item page once, e.g. Vinted's) plus what the current phrase lists find
    in the title: a kind already there stays as it was (it may have come from the description, which
    isn't stored), a kind the title shows now is added. So new rules reach listings stored before them."""
    s = dict(signals)
    s["meets"] = bool(s.get("meets")) or bool(phrase_reasons(title, "", MEET_PHRASES))
    for key, groups in (("phrases", PHRASES), ("rush", RUSH_PHRASES), ("messaging", NOTE_PHRASES)):
        have = list(s.get(key) or [])
        kinds = {r.split(" (", 1)[0] for r in have}
        have += [r for r in phrase_reasons(title, "", groups) if r.split(" (", 1)[0] not in kinds]
        s[key] = have
    return s


def typical_price(prices):
    """The median of comparable prices (RON), or None when there are too few to say."""
    prices = [p for p in prices if p and p > 0]
    return statistics.median(prices) if len(prices) >= MIN_COMPARABLES else None


RUSH_POINTS = 2


def assess(signals, price_ron, typical, fmt_price):
    """(level or None, score, reasons, notes) for a listing. reasons: the warning signs, each with points,
    which give the level; notes: worth knowing, no points (no reviews yet, a messaging app, the same
    photo on other ads), shown on their own, before the warning signs."""
    score, reasons, notes = 0, [], []

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
        notes.append("seller has no reviews yet")
    elif reviews and reviews >= 3 and signals.get("seller_rating") is not None and signals["seller_rating"] < 0.8:
        score += 1
        reasons.append(f"seller rated {signals['seller_rating'] * 5:.1f}/5")

    negative = signals.get("seller_negative_reviews") or 0
    if negative >= 2 and reviews and negative / reviews >= 0.1:
        score += 1
        reasons.append(f"seller has {negative} negative reviews out of {reviews}")

    meets = bool(signals.get("meets"))
    for phrase in signals.get("phrases") or []:
        kind = phrase.split(" (", 1)[0]
        if kind == OLD_MESSAGING_REASON or (meets and kind == "only ships, no meeting"):
            continue  # a messaging app is a note (below); offering to meet contradicts "only ships"
        score += PHRASE_POINTS.get(kind, 2)
        reasons.append(phrase)

    for phrase in (signals.get("rush") or [])[:1]:
        score += RUSH_POINTS
        reasons.append(phrase)

    if signals.get("contact_in_text"):
        score += 2
        reasons.append("puts a phone number or email in the ad, to talk off the platform")

    photos = signals.get("photos")
    if photos is not None and photos <= 1:
        score += 1
        reasons.append("only one photo" if photos == 1 else "no photos")

    if signals.get("unprotected") and score > 0 and not meets:
        score += 1
        reasons.append("no buyer protection")

    # Notes: a messaging app (also when stored as the old scored phrase), the same photo on other ads
    old = [p.replace(OLD_MESSAGING_REASON, "mentions WhatsApp or Telegram", 1)
           for p in signals.get("phrases") or [] if p.startswith(OLD_MESSAGING_REASON)]
    notes += (signals.get("messaging") or old)[:1]
    same_photo = signals.get("same_photo_ads") or 0
    if same_photo:
        notes.append(f"same photo as {same_photo} other ad{'s' if same_photo != 1 else ''} (copied or reposted)")

    level = next((name for minimum, name in LEVELS if score >= minimum), None)
    return level, score, reasons, notes


def contact_in_text(text):
    return bool(PHONE.search(text or "") or EMAIL.search(text or ""))
