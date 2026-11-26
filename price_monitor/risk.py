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

from . import clean

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
    "offers to meet in person": [
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

# Whether the price is open to an offer, for the dashboard's negotiation chance (no risk points): the seller
# says so, or says it's fixed. Same matching.
NEGOTIABLE_PHRASES = {
    "says the price is negotiable": [
        "negociabil", "negociabila", "negociez", "se negociaza", "pret negociabil", "accept oferte", "astept oferte",
        "ofertati", "pret discutabil", "discutabil", "negotiable", "open to offers", "make an offer", "or best offer",
        "do negocjacji", "do negocjacji", "cena do negocjacji", "συζητησιμη",
    ],
}
FIXED_PRICE_PHRASES = {
    "says the price is fixed": [
        "pret fix", "pret fixx", "pretul este fix", "pret ferm", "fara negociere", "fara negocieri", "nu negociez",
        "nu se negociaza", "nenegociabil", "non negociabil", "firm price", "price is firm", "no offers",
        "cena stala", "cena stała", "δεν συζητιεται",
        # A price, then "fix": "Preț 3000 fix", "3.000 lei fix", "2500€ fix", "3000 ron fixx" (not "fixat")
        re.compile(r"(?<![a-z0-9])\d[\d.,]*\s*(?:lei|ron|eur|euro|€|\$)?\s*fixx?(?![a-z])"),
    ],
}

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


QUOTE_MAX = 100  # characters of the ad's sentence quoted with a reason


def _sentences(title, description):
    """The title and the description's sentences (split after . ! ? and at line breaks), as written"""
    parts = [title or ""] + re.split(r"(?<=[.!?])\s+|\n+", clean.lines(description))
    return [p.strip() for p in parts if p and p.strip()]


def _quote(sentence, phrase):
    """The ad's own words around a phrase: its sentence, as written (diacritics kept), shortened around the
    phrase when long; double quotes in it made single, so the reason's own quotes stay clear"""
    text = re.sub(r"\s+", " ", sentence).replace('"', "'")
    if len(text) <= QUOTE_MAX:
        return text
    at = plain(text).find(phrase)  # plain() keeps the length for Romanian (one letter per letter)
    start = max(0, min(at - QUOTE_MAX // 3, len(text) - QUOTE_MAX)) if at >= 0 else 0
    end = start + QUOTE_MAX
    # Between words: from the start of a word, to the end of one
    if start:
        start = text.find(" ", start) + 1 or start
    if end < len(text):
        end = text.rfind(" ", start, end) if text.rfind(" ", start, end) > start else end
    return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")


def _find(phrase, flat):
    """Where a phrase is in a sentence's plain text: a word or words, whole (not inside other words), or a
    compiled pattern for what words can't say ("3000 fix"). Returns the matched text, or None."""
    if isinstance(phrase, re.Pattern):
        m = phrase.search(flat)
        return m.group(0).strip() if m else None
    return phrase if re.search(r"(?<![a-z])" + re.escape(phrase) + r"(?![a-z])", flat) else None


def phrase_reasons(title, description, groups=None):
    """One reason per kind of phrase found in the title or description (groups: PHRASES by default), with
    the ad's own words quoted: 'offers to meet in person ("Ridicarea locală este posibilă.")'"""
    sentences = [(sentence, f" {plain(sentence)} ") for sentence in _sentences(title, description)]
    found = []
    for reason, words in (groups or PHRASES).items():
        hit = next(((sentence, m) for w in words for sentence, flat in sentences for m in [_find(w, flat)] if m), None)
        if hit:
            found.append(f'{reason} ("{_quote(*hit)}")')
    return found


def static_signals(listing, platform):
    """What the listing itself shows, as a small JSON-able dict (stored on the listing)."""
    s = dict(listing.signals or {})
    description = s.pop("description", "")
    s["phrases"] = phrase_reasons(listing.title, description)
    s["rush"] = phrase_reasons(listing.title, description, RUSH_PHRASES)
    s["messaging"] = phrase_reasons(listing.title, description, NOTE_PHRASES)
    s["meets"] = phrase_reasons(listing.title, description, MEET_PHRASES)  # e.g. 'offers to meet in person ("predare personala")'
    s["negotiable"] = phrase_reasons(listing.title, description, NEGOTIABLE_PHRASES)
    s["fixed_price"] = phrase_reasons(listing.title, description, FIXED_PRICE_PHRASES)
    if s["fixed_price"]:
        s["negotiable"] = []  # "nu negociez" also holds "negociez": saying it's fixed wins
    return s


# Markup in a quote stored before descriptions were cleaned ("…fiscale<br />"), also cut off ("…<b…")
QUOTED_TAG = re.compile(r"<[^<>]*>|<[a-zA-Z/][^<>\"]*(?=…)")


def _without_markup(reason):
    """A stored reason with no markup left in its quote (for listings stored before clean.lines)"""
    if not isinstance(reason, str) or "<" not in reason:
        return reason
    reason = re.sub(r"\s+", " ", QUOTED_TAG.sub(" ", reason))
    return re.sub(r'\s+(…?"\))$', r"\1", reason.replace('(" ', '("'))


def with_current_phrases(signals, title, description=None):
    """Stored signals plus what the current phrase lists find. With the ad's stored description (v20),
    every phrase kind is worked out again from the title and description. Without it (stored before
    v20, or read from an item page once, e.g. Vinted's), a kind already there stays as it was (it may
    have come from the description) and a kind the title shows now is added. So new rules reach
    listings stored before them."""
    s = dict(signals)
    if description:
        s["meets"] = phrase_reasons(title, description, MEET_PHRASES)
    else:
        # Offers to meet: stored as the phrase found (a list; before, a bool), else what the title shows
        meets = s.get("meets")
        s["meets"] = [_without_markup(r) for r in meets] if isinstance(meets, list) and meets else \
            phrase_reasons(title, "", MEET_PHRASES) or bool(meets)
    for key, groups in (("phrases", PHRASES), ("rush", RUSH_PHRASES), ("messaging", NOTE_PHRASES),
                        ("negotiable", NEGOTIABLE_PHRASES), ("fixed_price", FIXED_PRICE_PHRASES)):
        if description:
            s[key] = phrase_reasons(title, description, groups)
            continue
        have = [_without_markup(r) for r in s.get(key) or []]
        kinds = {r.split(" (", 1)[0] for r in have}
        have += [r for r in phrase_reasons(title, "", groups) if r.split(" (", 1)[0] not in kinds]
        s[key] = have
    if s.get("fixed_price"):
        s["negotiable"] = []
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


# Good signs score too, in steps: an older seller account counts for more (from this many years, points)
ACCOUNT_YEARS_POINTS = [(10, 3), (5, 2), (2, 1)]
# 1 point: some good signs, 2-3: good signs, 4 or more: very good signs
GOOD_LEVELS = [(4, "high"), (2, "medium"), (1, "low")]


def good_signs(signals):
    """(level or None, points, reasons) for the good signs a listing shows: an old seller account (1 point
    from 2 years, 2 from 5, 3 from 10), a well-rated seller (4.5/5 or more with 5 reviews: 1, with 20: 2),
    buyer protection offered (1), an offer to meet in person (1). Shown in green, between the notes and
    the warning signs; the dashboard's deal score counts them too."""
    points, good = 0, []
    age = signals.get("account_age_days")
    if age is not None:
        years = age // 365
        step = next((p for minimum, p in ACCOUNT_YEARS_POINTS if years >= minimum), 0)
        if step:
            points += step
            good.append(f"seller account {years} years old")
    reviews, rating = signals.get("seller_reviews") or 0, signals.get("seller_rating")
    if reviews >= 5 and rating is not None and rating >= 0.9:
        points += 2 if reviews >= 20 else 1
        good.append(f"seller rated {rating * 5:.1f}/5 ({reviews} reviews)")
    if signals.get("unprotected") is False:
        points += 1
        good.append("offers buyer protection")
    meets = signals.get("meets")
    if isinstance(meets, list) and meets:
        points += 1
        good.append(meets[0])  # 'offers to meet in person ("predare personala")'
    elif meets is True:
        points += 1
        good.append("offers to meet in person")  # stored before the phrase was kept
    level = next((name for minimum, name in GOOD_LEVELS if points >= minimum), None)
    return level, points, good


def contact_in_text(text):
    return bool(PHONE.search(text or "") or EMAIL.search(text or ""))
