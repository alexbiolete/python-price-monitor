"""Listing text as it's stored: other people's ads, so cleaned on the way in.

Whatever shows them (the email, the dashboard) still escapes it; this is a second line: no
markup, control characters or odd links in the database to begin with.
"""

import html
import re

TAG = re.compile(r"<[^<>]*>")
# Control characters (tabs and newlines become spaces below), and invisible ones
CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")
SPACE = re.compile(r"\s+")
WEB_URL = re.compile(r"^https?://[^\s<>\"']+$", re.IGNORECASE)


def text(value, limit=None):
    """Plain text: entities decoded, tags removed, one line. None stays None."""
    if value is None:
        return None
    value = TAG.sub(" ", html.unescape(str(value)))
    value = SPACE.sub(" ", CONTROL.sub(" ", value)).strip()
    return value[:limit] if limit else value


def url(value):
    """A web link (http or https), or None for anything else (javascript:, data:, ...)."""
    value = (value or "").strip()
    return value if WEB_URL.match(value) else None


def listing(l):
    """Cleans a Listing in place; False when it has no usable link (it's skipped)."""
    l.url = url(l.url)
    if not l.url:
        return False
    l.image_url = url(l.image_url)
    l.title = text(l.title) or ""
    l.location = text(l.location)
    l.price_detail = text(l.price_detail)
    return True
