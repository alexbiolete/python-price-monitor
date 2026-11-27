"""Small copies of the ads' photos (listing_thumbs), so a listing keeps its photo after the platform
deletes the ad and its files. 80x80 WebP, about 2-3 KB each.

Saved for the listings worth keeping a face for: in their search's budget, pinned, or marked
suspicious. A run saves a few (PER_RUN, within TIME_BUDGET), newest first, so the ones found
before this existed fill in over a few runs. A photo that fails is tried again a day later,
MAX_TRIES times in all.

Old ones deleted by the rule the dashboard uses too (README, "listing_thumbs"): the photos of ads
gone, not seen for more than N days (settings.thumbnail_purge_days), pinned ones kept. By hand (the
dashboard's Settings, or --delete-old-photos), or on every run when settings.thumbnail_auto_purge
is on (off by default).
"""

import io
import logging
import time
from datetime import timedelta

import requests

from .db import to_db_time
from .providers.base import BROWSER_HEADERS

log = logging.getLogger("price_monitor")

SIZE = 80  # px, square: the dashboard shows them at 40px, so sharp on a retina screen
QUALITY = 75
PER_RUN = 40
TIME_BUDGET = 40  # seconds a run spends on them at most (the cron gives a run 280)
MAX_TRIES = 3
MAX_BYTES = 8 * 1024 * 1024  # a photo bigger than this isn't a listing's photo


def make_thumb(data):
    """The photo's middle square, SIZE px, as WebP bytes"""
    from PIL import Image, ImageOps  # only here: the rest of the monitor runs without Pillow

    with Image.open(io.BytesIO(data)) as img:
        img = ImageOps.exif_transpose(img).convert("RGB")
        img = ImageOps.fit(img, (SIZE, SIZE), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, "WEBP", quality=QUALITY, method=6)
        return out.getvalue()


def fetch(url, session):
    resp = session.get(url, timeout=8, stream=True)
    resp.raise_for_status()
    data = resp.raw.read(MAX_BYTES + 1, decode_content=True)
    if len(data) > MAX_BYTES:
        raise ValueError("photo too large")
    return data


def wanted(db, now, limit=PER_RUN):
    """Listings that should have a photo saved and don't (or one that failed, a day ago, fewer than
    MAX_TRIES times): in the budget, pinned or marked suspicious; newest first"""
    from .runner import search_limits  # its budget in lei, as process() reads it

    retry_before = to_db_time(now - timedelta(days=1))
    picked = []
    for s in db.all("SELECT * FROM searches"):
        lo, hi = search_limits(s, "min_budget", "max_budget")
        budget = " AND ".join(x for x in (lo is not None and "l.price_ron >= ?", hi is not None and "l.price_ron <= ?") if x) or "1 = 1"
        params = [s["id"]] + [x for x in (lo, hi) if x is not None] + [MAX_TRIES, retry_before, limit]
        picked += db.all(
            "SELECT l.id, l.image_url FROM listings l LEFT JOIN listing_thumbs t ON t.listing_id = l.id"
            " WHERE l.search_id = ? AND l.image_url IS NOT NULL"
            f" AND (l.pin_order IS NOT NULL OR l.marked_suspicious_at IS NOT NULL OR ({budget}))"
            " AND (t.listing_id IS NULL OR (t.data IS NULL AND t.tries < ? AND t.saved_at < ?))"
            " ORDER BY l.id DESC LIMIT ?", tuple(params))
    picked.sort(key=lambda r: -r["id"])
    return picked[:limit]


def save_missing(db, now):
    """Saves up to PER_RUN photos (within TIME_BUDGET); returns (saved, failed)"""
    settings = db.one("SELECT thumbnails FROM settings WHERE id = 1") or {}
    if not settings.get("thumbnails", 1):
        return 0, 0
    rows = wanted(db, now)
    if not rows:
        return 0, 0
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)
    started, saved, failed = time.monotonic(), 0, 0
    stamp = to_db_time(now)
    for r in rows:
        if time.monotonic() - started > TIME_BUDGET:
            break
        try:
            data = make_thumb(fetch(r["image_url"], session))
        except Exception as e:  # gone, blocked, not an image: tried again later
            log.debug("Photo of listing %s not saved: %s", r["id"], e)
            data = None
            failed += 1
        else:
            saved += 1
        existing = db.one("SELECT tries FROM listing_thumbs WHERE listing_id = ?", (r["id"],))
        if existing is None:
            db.execute("INSERT INTO listing_thumbs (listing_id, data, tries, saved_at) VALUES (?, ?, ?, ?)",
                       (r["id"], data, 0 if data else 1, stamp))
        else:
            db.execute("UPDATE listing_thumbs SET data = ?, tries = ?, saved_at = ? WHERE listing_id = ?",
                       (data, existing["tries"] + (0 if data else 1), stamp, r["id"]))
        db.commit()
        time.sleep(0.2)  # gentle on the platforms' image servers
    return saved, failed


def delete_old(db, days, now):
    """Deletes the photos of ads not seen for more than `days` days (gone from the platform), pinned
    ones kept; returns how many"""
    before = to_db_time(now - timedelta(days=days))
    ids = [r["listing_id"] for r in db.all(
        "SELECT t.listing_id FROM listing_thumbs t JOIN listings l ON l.id = t.listing_id"
        " WHERE l.last_seen < ? AND l.pin_order IS NULL", (before,))]
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        db.execute(f"DELETE FROM listing_thumbs WHERE listing_id IN ({', '.join('?' for _ in chunk)})", tuple(chunk))
    db.commit()
    return len(ids)


def auto_delete_old(db, now):
    """Old photos deleted on every run, when the settings turn it on (off by default)"""
    s = db.one("SELECT thumbnail_auto_purge, thumbnail_purge_days FROM settings WHERE id = 1") or {}
    if s.get("thumbnail_auto_purge") and s.get("thumbnail_purge_days"):
        return delete_old(db, int(s["thumbnail_purge_days"]), now)
    return 0
