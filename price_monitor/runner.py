"""One tick: run the searches that are due, save what changed, email one digest.

Cron (or the local loop) calls tick() often, e.g. every 5 minutes; the
database decides what actually runs:
  settings.enabled = 0          -> nothing runs
  settings.run_requested_at set -> every enabled search runs now ("Run all now"): it's turned
                                   into a request on each search, so every copy of the monitor
                                   (server, laptop) runs its own share
  searches.run_requested_at set -> that search runs now, even if disabled
  otherwise                     -> enabled searches whose interval has passed

PLATFORMS (in .env, or --platforms) limits a copy of the monitor to some
platforms: e.g. olx,vinted on the server and facebook on a laptop, so nothing
runs twice. --search runs chosen searches now.
"""

import json
import logging
import os
import time
from datetime import timedelta

from .db import from_db_time, json_list, json_object, to_db_time, utcnow
from . import clean, rates, risk
from .emailer import fmt_price, render_digest, send_email
from .providers import PROVIDERS, get_provider_class
from .providers.base import ProviderError, SearchSpec

log = logging.getLogger("price_monitor")

DUE_SLACK = timedelta(seconds=60)  # a 10-minute interval still runs on a tick that's a few seconds early


def env_number(key, default):
    raw = os.getenv(key)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise SystemExit(f"{key} in .env must be a number, got {raw!r}")
    if value <= 0:
        raise SystemExit(f"{key} in .env must be positive, got {raw!r}")
    return value


def to_ron(price, currency):
    """Lei, for the searches' min/max (rates.py: BNR's rates); None without a rate for the currency"""
    return rates.to_ron(price, currency)


def allowed_platforms(platforms=None):
    """The platforms this copy runs: --platforms, else PLATFORMS in .env, else all. Lowercase set,
    or None for all."""
    raw = platforms if platforms is not None else os.getenv("PLATFORMS")
    names = {p.strip().lower() for p in (raw or "").split(",") if p.strip()}
    return names or None


def due_searches(searches, settings, now):
    run_all = settings["run_requested_at"] is not None
    default_interval = settings["default_interval_minutes"] or 10
    due = []
    for s in searches:
        if s["run_requested_at"] is not None:
            due.append(s)
        elif not s["enabled"]:
            continue
        elif run_all:
            due.append(s)
        else:
            last = from_db_time(s["last_run_at"])
            cls = get_provider_class(s["platform"])
            minutes = max(s["interval_minutes"] or default_interval, cls.min_interval_minutes if cls else 0)
            interval = timedelta(minutes=minutes)
            if last is None or now - last >= interval - DUE_SLACK:
                due.append(s)
    return due


SELLER_KEYS = ("seller_reviews", "seller_rating", "seller_negative_reviews")
SKIP_MEMO = 1000  # old items whose page we've read, remembered per search so it isn't read again


def process(db, search, listings, platform_label, now, drop_min_pct, max_age_days=None,
            provider=None, spec=None):
    """Store matching listings, flag possible scams; return what to report. Listings outside the
    search's price range are stored too (for the price estimate), but never reported: the
    dashboard shows the ones whose price_ron is in the range."""
    is_baseline = search["baseline_at"] is None
    oldest = now - timedelta(days=max_age_days) if max_age_days else None
    includes = [k.lower() for k in json_list(search["include_keywords"]) if k.strip()]
    excludes = [k.lower() for k in json_list(search["exclude_keywords"]) if k.strip()]
    lo, hi = search["min_price"], search["max_price"]
    stamp = to_db_time(now)
    state = spec.state if spec is not None else {}
    skipped_old = set(state.get("skipped_old") or [])
    stats = {"matching": 0, "filtered": 0, "out_of_range": 0, "no_price": 0, "no_rate": 0, "too_old": 0, "pages": 0,
             "sellers": 0}
    fmt = lambda amount: fmt_price(amount, "RON")  # noqa: E731

    # 1. The search's filters. The price range only decides what's reported: ads outside it are kept
    candidates, seen = [], set()
    for l in listings:
        if l.price is None or not l.currency:
            stats["no_price"] += 1
            continue
        if l.external_id in seen:
            continue  # the same ad on two pages (promoted ads repeat)
        if not clean.listing(l):
            stats["filtered"] += 1  # no web link to it
            continue
        seen.add(l.external_id)
        if l.external_id in skipped_old or (oldest and l.posted_at and l.posted_at < oldest):
            stats["too_old"] += 1  # older than the age limit (ads without a date are kept)
            continue
        price_ron = to_ron(l.price, l.currency)
        if price_ron is None:
            stats["no_rate"] += 1  # no exchange rate yet: not stored, so it's new once there is one
            continue
        title = l.title.lower()
        if (includes and not any(k in title for k in includes)) or any(k in title for k in excludes):
            stats["filtered"] += 1
            continue
        in_range = (lo is None or price_ron >= lo) and (hi is None or price_ron <= hi)
        candidates.append((l, price_ron, in_range))
    # In the range first, so they get the item pages a run may read (max_enrich)
    candidates.sort(key=lambda c: not c[2])

    # 2. What's typical here: the median of everything in the range (for "far below the typical price")
    typical = risk.typical_price([p for _, p, in_range in candidates if in_range])
    rows = {r["external_id"]: r for r in db.all(
        "SELECT id, external_id, price, currency, risk_signals, pin_order, inactive_at FROM listings WHERE search_id = ?",
        (search["id"],))}

    new, drops = [], []
    budget = provider.max_enrich if provider is not None and provider.needs_enrich else 0
    seller_budget = provider.max_seller_lookups if provider is not None and provider.rates_sellers else 0
    for l, price_ron, in_range in candidates:
        row = rows.get(l.external_id)
        stored_signals = json_object(row["risk_signals"]) if row and row["risk_signals"] else None

        # 3. The item's own page, for platforms that need it: new listings, or stored ones not read yet
        if provider is not None and provider.needs_enrich and stored_signals is None:
            if budget <= 0:
                if row is None:
                    continue  # read it on a later run, before it's reported
            else:
                budget -= 1
                stats["pages"] += 1
                provider.enrich(l)
                clean.listing(l)  # the item page may have added to it (e.g. the location)
                if oldest and l.posted_at and l.posted_at < oldest:
                    stats["too_old"] += 1
                    skipped_old.add(l.external_id)
                    if row is not None and row["pin_order"] is None:  # pinned ones stay
                        db.execute("DELETE FROM listings WHERE id = ?", (row["id"],))
                    continue
        if stored_signals is not None and provider is not None and provider.needs_enrich:
            signals = stored_signals  # read from its page before; the page isn't read again
        else:
            signals = risk.static_signals(l, search["platform"])
            if provider is not None and provider.rates_sellers:
                # The seller's rating: kept from an earlier run, else looked up (within the budget)
                kept = {k: v for k, v in (stored_signals or {}).items() if k in SELLER_KEYS}
                if "seller_reviews" in kept:
                    signals.update(kept)
                elif seller_budget > 0 and l.seller_id:
                    seller_budget -= 1
                    stats["sellers"] += 1
                    signals.update(provider.seller_signals(l) or {})
        level, score, reasons, notes = risk.assess(signals, price_ron, typical, fmt)
        stats["matching" if in_range else "out_of_range"] += 1
        item = {"title": l.title, "url": l.url, "image_url": l.image_url, "location": l.location,
                "price": l.price, "currency": l.currency, "price_ron": price_ron,
                "platform_label": platform_label, "risk_level": level, "risk_reasons": reasons,
                "risk_notes": notes, "price_detail": l.price_detail}
        risk_values = (level, score, json.dumps(reasons, ensure_ascii=False), json.dumps(signals, ensure_ascii=False),
                       json.dumps(notes, ensure_ascii=False) if notes else None)

        if row is None:
            cur = db.execute(
                "INSERT INTO listings (search_id, platform, external_id, title, url, image_url, price, currency,"
                " location, posted_at, first_seen, last_seen, risk_level, risk_score, risk_reasons, risk_signals,"
                " risk_notes, price_detail, price_ron) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (search["id"], search["platform"], l.external_id, l.title[:500], l.url, l.image_url,
                 l.price, l.currency, (l.location or None) and l.location[:200], to_db_time(l.posted_at),
                 stamp, stamp) + risk_values + ((l.price_detail or None) and l.price_detail[:300], price_ron))
            db.execute("INSERT INTO price_history (listing_id, price, currency, seen_at) VALUES (?, ?, ?, ?)",
                       (cur.lastrowid, l.price, l.currency, stamp))
            if in_range and not is_baseline and search["notify_new"]:
                new.append(item)
            continue

        if row["inactive_at"] is not None:  # found sold or removed before, and it's back
            db.execute("INSERT INTO status_history (listing_id, status, seen_at) VALUES (?, 'active', ?)",
                       (row["id"], stamp))
        if l.price != row["price"] or l.currency != row["currency"]:
            db.execute("INSERT INTO price_history (listing_id, price, currency, seen_at) VALUES (?, ?, ?, ?)",
                       (row["id"], l.price, l.currency, stamp))
            if l.currency == row["currency"] and row["price"] > 0:
                drop_pct = (row["price"] - l.price) / row["price"] * 100
                if in_range and drop_pct >= drop_min_pct and not is_baseline and search["notify_price_drops"]:
                    drops.append({**item, "old_price": row["price"], "drop_pct": drop_pct})
        db.execute(
            "UPDATE listings SET title = ?, url = ?, image_url = ?, price = ?, currency = ?,"
            " location = COALESCE(?, location), posted_at = COALESCE(?, posted_at), last_seen = ?, inactive_at = NULL,"
            " risk_level = ?, risk_score = ?,"
            " risk_reasons = ?, risk_signals = ?, risk_notes = ?, price_detail = ?, price_ron = ? WHERE id = ?",
            (l.title[:500], l.url, l.image_url, l.price, l.currency, (l.location or None) and l.location[:200],
             to_db_time(l.posted_at), stamp) + risk_values + ((l.price_detail or None) and l.price_detail[:300], price_ron, row["id"]))

    # Stored before price_ron existed (v8) and not in this run's results: worked out from their price
    for r in db.all("SELECT id, price, currency FROM listings WHERE search_id = ? AND price_ron IS NULL",
                    (search["id"],)):
        price_ron = to_ron(r["price"], r["currency"])
        if price_ron is not None:
            db.execute("UPDATE listings SET price_ron = ? WHERE id = ?", (price_ron, r["id"]))

    if skipped_old:
        state["skipped_old"] = sorted(skipped_old)[-SKIP_MEMO:]
    if is_baseline:
        db.execute("UPDATE searches SET baseline_at = ? WHERE id = ?", (stamp, search["id"]))
    return new, drops, stats, is_baseline


RECHECK_EVERY = timedelta(hours=12)  # an ad a search stopped showing is re-checked at most this often


def recheck(db, search, provider, now):
    """Stored ads this run didn't see (they fell off the results, or no longer match): read their
    own pages, at most provider.max_check, the longest unchecked first. A sold or removed ad gets
    inactive_at; one still up gets its price updated where the page shows it. Returns
    (checked, gone, repriced)."""
    stamp = to_db_time(now)
    rows = db.all(
        "SELECT id, url, price, currency FROM listings WHERE search_id = ? AND inactive_at IS NULL"
        " AND last_seen < ? AND (checked_at IS NULL OR checked_at < ?)"
        " ORDER BY checked_at IS NOT NULL, checked_at, last_seen LIMIT %d" % provider.max_check,
        (search["id"], stamp, to_db_time(now - RECHECK_EVERY)))
    checked = gone = repriced = 0
    for i, row in enumerate(rows):
        if i:
            time.sleep(provider.check_delay)
        try:
            result = provider.check(row["url"])
        except ProviderError as e:  # refused (rate limit): the rest wait for a later run
            log.warning("[%s] %s: stopped re-checking ads: %s", provider.label, search["name"], e)
            break
        except Exception as e:  # one bad page doesn't stop the others
            log.warning("[%s] %s: couldn't check %s: %s", provider.label, search["name"], row["url"], e)
            continue
        checked += 1
        db.execute("UPDATE listings SET checked_at = ? WHERE id = ?", (stamp, row["id"]))
        if result is None:
            continue
        if result.gone:
            gone += 1
            db.execute("UPDATE listings SET inactive_at = ? WHERE id = ?", (stamp, row["id"]))
            db.execute("INSERT INTO status_history (listing_id, status, seen_at) VALUES (?, 'inactive', ?)",
                       (row["id"], stamp))
        elif result.price and result.currency and (result.price != row["price"] or result.currency != row["currency"]):
            repriced += 1
            db.execute("UPDATE listings SET price = ?, currency = ?, price_ron = ? WHERE id = ?",
                       (result.price, result.currency, to_ron(result.price, result.currency), row["id"]))
            db.execute("INSERT INTO price_history (listing_id, price, currency, seen_at) VALUES (?, ?, ?, ?)",
                       (row["id"], result.price, result.currency, stamp))
    return checked, gone, repriced


def tick(db, platforms=None, search_ids=None):
    """One check. platforms: limit to these (see allowed_platforms); search_ids: run these now.
    Skipped when another check is running on the same database."""
    if not db.try_lock():
        log.info("Another check is running on this database; skipping this one")
        return 0
    try:
        return _tick(db, platforms, search_ids)
    finally:
        db.unlock()


def _tick(db, platforms, search_ids):
    drop_min_pct = env_number("PRICE_DROP_MIN_PCT", 3)
    now = utcnow()
    allowed = allowed_platforms(platforms)

    settings = db.one("SELECT * FROM settings WHERE id = 1")
    # The platforms this copy runs, for the dashboard to offer
    db.execute("UPDATE settings SET providers = ?, last_tick_at = ? WHERE id = 1",
               (json.dumps([{"name": c.name, "label": c.label} for c in PROVIDERS.values()
                            if allowed is None or c.name in allowed]), to_db_time(now)))
    db.commit()
    rates.refresh(db, now)  # BNR's exchange rates, at most every few hours
    if not settings["enabled"] and not search_ids:
        log.info("The monitor is turned off in settings; nothing to do")
        return 0

    # "Run all now": a request on every enabled search, so each copy runs its own share
    if settings["run_requested_at"] is not None:
        db.execute("UPDATE searches SET run_requested_at = ? WHERE enabled = 1",
                   (to_db_time(from_db_time(settings["run_requested_at"])),))
        db.execute("UPDATE settings SET run_requested_at = NULL WHERE id = 1 AND run_requested_at = ?",
                   (to_db_time(from_db_time(settings["run_requested_at"])),))
        db.commit()
        settings = {**settings, "run_requested_at": None}

    searches = [s for s in db.all("SELECT * FROM searches ORDER BY sort_order, id")
                if allowed is None or s["platform"].lower() in allowed]
    if search_ids:
        due = [s for s in searches if s["id"] in search_ids]
        missing = set(search_ids) - {s["id"] for s in due}
        if missing:
            log.warning("Not run: no search %s here%s", ", ".join(f"#{i}" for i in sorted(missing)),
                        f" (this copy runs {', '.join(sorted(allowed))})" if allowed else "")
    else:
        due = due_searches(searches, settings, now)
    if not due:
        log.info("No searches due (%d here%s)", len(searches),
                 f", platforms: {', '.join(sorted(allowed))}" if allowed else "")

    providers, last_platform, reports = {}, None, []
    for s in due:
        cls = get_provider_class(s["platform"])
        status, error, result_count, match_count, state_json = "error", None, None, None, s["provider_state"]
        label = cls.label if cls else s["platform"]
        try:
            if cls is None:
                raise ProviderError(f"Unknown platform {s['platform']!r}; this monitor supports: "
                                    + ", ".join(sorted(PROVIDERS)))
            provider = providers.setdefault(cls.name, cls())
            if last_platform == cls.name:
                time.sleep(provider.search_delay)  # be polite between searches on one site
            last_platform = cls.name
            try:
                spec = SearchSpec(s["id"], s["name"], s["query_url"],
                                  json_object(s["options"]), json_object(s["provider_state"]))
            except (ValueError, TypeError):
                raise ProviderError("options is not valid JSON")

            listings = provider.search(spec)
            max_age = s.get("max_age_days") or settings.get("default_max_age_days")
            new, drops, stats, baseline = process(db, s, listings, label, now, drop_min_pct, max_age,
                                                  provider, spec)
            state_json = json.dumps(spec.state) if spec.state else None
            result_count, match_count = len(listings), stats["matching"]
            status = "ok" if listings else "empty"
            error = None if listings else "The platform returned no results; check the search URL and filters"
            flagged = sum(1 for x in new + drops if x["risk_level"])
            log.info("[%s] %s: %d results, %d matching, %d outside the price range (kept), %d filtered out,"
                     " %d older than %s days, %d without price, %d without an exchange rate, %d item pages read,"
                     " %d sellers looked up%s",
                     label, s["name"], len(listings), stats["matching"], stats["out_of_range"], stats["filtered"],
                     stats["too_old"],
                     max_age or "∞", stats["no_price"], stats["no_rate"], stats["pages"], stats["sellers"],
                     " (baseline saved, no email)" if baseline
                     else f"; {len(new)} new, {len(drops)} price drops, {flagged} flagged")
            if listings:  # a failed or empty run says nothing about the stored ads
                checked, gone, repriced = recheck(db, s, provider, now)
                if checked:
                    log.info("[%s] %s: re-checked %d ads it no longer shows: %d gone, %d with a new price",
                             label, s["name"], checked, gone, repriced)
            if new or drops:
                reports.append({"search": {**s, "platform_label": label}, "new": new, "drops": drops})
        except ProviderError as e:
            error = str(e)
            log.error("[%s] %s: %s", label, s["name"], error)
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            log.exception("[%s] %s failed; continuing with the others", label, s["name"])

        db.execute(
            "UPDATE searches SET provider_state = ?, last_run_at = ?, last_status = ?, last_error = ?,"
            " last_result_count = ?, last_match_count = ? WHERE id = ?",
            (state_json, to_db_time(now), status, (error or None) and error[:2000],
             result_count, match_count, s["id"]))
        if s["run_requested_at"] is not None:  # clear only the request we served
            db.execute("UPDATE searches SET run_requested_at = NULL WHERE id = ? AND run_requested_at = ?",
                       (s["id"], to_db_time(from_db_time(s["run_requested_at"]))))
        db.commit()

    if reports:
        to = settings["email_to"] or os.getenv("EMAIL_TO")
        subject, html_body, text_body = render_digest(reports, os.getenv("DASHBOARD_URL"))
        try:
            send_email(subject, html_body, text_body, to)
        except Exception:
            log.exception("Failed to send the email")
    elif due:
        log.info("Nothing to report")
    return len(due)
