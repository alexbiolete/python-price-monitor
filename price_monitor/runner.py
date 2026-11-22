"""One tick: run the searches that are due, save what changed, email one digest.

Cron (or the local loop) calls tick() often, e.g. every 5 minutes; the
database decides what actually runs:
  settings.enabled = 0          -> nothing runs
  settings.run_requested_at set -> every enabled search runs now ("Run now")
  searches.run_requested_at set -> that search runs now, even if disabled
  otherwise                     -> enabled searches whose interval has passed
"""

import json
import logging
import os
import time
from datetime import timedelta

from .db import from_db_time, json_list, json_object, to_db_time, utcnow
from . import risk
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


def to_ron(price, currency, eur_to_ron):
    return price * eur_to_ron if currency == "EUR" else price


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
            interval = timedelta(minutes=s["interval_minutes"] or default_interval)
            if last is None or now - last >= interval - DUE_SLACK:
                due.append(s)
    return due


SKIP_MEMO = 1000  # old items whose page we've read, remembered per search so it isn't read again


def process(db, search, listings, platform_label, now, eur_to_ron, drop_min_pct, max_age_days=None,
            provider=None, spec=None):
    """Store matching listings, flag possible scams; return what to report."""
    is_baseline = search["baseline_at"] is None
    oldest = now - timedelta(days=max_age_days) if max_age_days else None
    includes = [k.lower() for k in json_list(search["include_keywords"]) if k.strip()]
    excludes = [k.lower() for k in json_list(search["exclude_keywords"]) if k.strip()]
    lo, hi = search["min_price"], search["max_price"]
    stamp = to_db_time(now)
    state = spec.state if spec is not None else {}
    skipped_old = set(state.get("skipped_old") or [])
    stats = {"matching": 0, "filtered": 0, "no_price": 0, "too_old": 0, "pages": 0}
    fmt = lambda amount: fmt_price(amount, "RON")  # noqa: E731

    # 1. The search's filters
    candidates, seen = [], set()
    for l in listings:
        if l.price is None or not l.currency:
            stats["no_price"] += 1
            continue
        if l.external_id in seen:
            continue  # the same ad on two pages (promoted ads repeat)
        seen.add(l.external_id)
        if l.external_id in skipped_old or (oldest and l.posted_at and l.posted_at < oldest):
            stats["too_old"] += 1  # older than the age limit (ads without a date are kept)
            continue
        price_ron = to_ron(l.price, l.currency, eur_to_ron)
        title = l.title.lower()
        if (includes and not any(k in title for k in includes)) or any(k in title for k in excludes) \
                or (lo is not None and price_ron < lo) or (hi is not None and price_ron > hi):
            stats["filtered"] += 1
            continue
        candidates.append((l, price_ron))

    # 2. What's typical here: the median of everything that matches (for "far below the typical price")
    typical = risk.typical_price([p for _, p in candidates])
    rows = {r["external_id"]: r for r in db.all(
        "SELECT id, external_id, price, currency, risk_signals FROM listings WHERE search_id = ?", (search["id"],))}

    new, drops = [], []
    budget = provider.max_enrich if provider is not None and provider.needs_enrich else 0
    for l, price_ron in candidates:
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
                if oldest and l.posted_at and l.posted_at < oldest:
                    stats["too_old"] += 1
                    skipped_old.add(l.external_id)
                    if row is not None:
                        db.execute("DELETE FROM listings WHERE id = ?", (row["id"],))
                    continue
        if stored_signals is not None and provider is not None and provider.needs_enrich:
            signals = stored_signals  # read from its page before; the page isn't read again
        else:
            signals = risk.static_signals(l, search["platform"])
        level, score, reasons = risk.assess(signals, price_ron, typical, fmt)
        stats["matching"] += 1
        item = {"title": l.title, "url": l.url, "image_url": l.image_url, "location": l.location,
                "price": l.price, "currency": l.currency, "price_ron": price_ron,
                "platform_label": platform_label, "risk_level": level, "risk_reasons": reasons}
        risk_values = (level, score, json.dumps(reasons, ensure_ascii=False), json.dumps(signals, ensure_ascii=False))

        if row is None:
            cur = db.execute(
                "INSERT INTO listings (search_id, platform, external_id, title, url, image_url, price, currency,"
                " location, posted_at, first_seen, last_seen, risk_level, risk_score, risk_reasons, risk_signals)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (search["id"], search["platform"], l.external_id, l.title[:500], l.url, l.image_url,
                 l.price, l.currency, (l.location or None) and l.location[:200], to_db_time(l.posted_at),
                 stamp, stamp) + risk_values)
            db.execute("INSERT INTO price_history (listing_id, price, currency, seen_at) VALUES (?, ?, ?, ?)",
                       (cur.lastrowid, l.price, l.currency, stamp))
            if not is_baseline and search["notify_new"]:
                new.append(item)
            continue

        if l.price != row["price"] or l.currency != row["currency"]:
            db.execute("INSERT INTO price_history (listing_id, price, currency, seen_at) VALUES (?, ?, ?, ?)",
                       (row["id"], l.price, l.currency, stamp))
            if l.currency == row["currency"] and row["price"] > 0:
                drop_pct = (row["price"] - l.price) / row["price"] * 100
                if drop_pct >= drop_min_pct and not is_baseline and search["notify_price_drops"]:
                    drops.append({**item, "old_price": row["price"], "drop_pct": drop_pct})
        db.execute(
            "UPDATE listings SET title = ?, url = ?, image_url = ?, price = ?, currency = ?,"
            " location = COALESCE(?, location), posted_at = COALESCE(?, posted_at), last_seen = ?,"
            " risk_level = ?, risk_score = ?,"
            " risk_reasons = ?, risk_signals = ? WHERE id = ?",
            (l.title[:500], l.url, l.image_url, l.price, l.currency, (l.location or None) and l.location[:200],
             to_db_time(l.posted_at), stamp) + risk_values + (row["id"],))

    if skipped_old:
        state["skipped_old"] = sorted(skipped_old)[-SKIP_MEMO:]
    if is_baseline:
        db.execute("UPDATE searches SET baseline_at = ? WHERE id = ?", (stamp, search["id"]))
    return new, drops, stats, is_baseline


def tick(db):
    eur_to_ron = env_number("EUR_TO_RON", 4.97)
    drop_min_pct = env_number("PRICE_DROP_MIN_PCT", 3)
    now = utcnow()

    settings = db.one("SELECT * FROM settings WHERE id = 1")
    db.execute("UPDATE settings SET providers = ?, last_tick_at = ? WHERE id = 1",
               (json.dumps([{"name": c.name, "label": c.label} for c in PROVIDERS.values()]), to_db_time(now)))
    db.commit()
    if not settings["enabled"]:
        log.info("The monitor is turned off in settings; nothing to do")
        return 0

    searches = db.all("SELECT * FROM searches ORDER BY sort_order, id")
    due = due_searches(searches, settings, now)
    if not due:
        log.info("No searches due (%d configured)", len(searches))

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
            new, drops, stats, baseline = process(db, s, listings, label, now, eur_to_ron, drop_min_pct, max_age,
                                                  provider, spec)
            state_json = json.dumps(spec.state) if spec.state else None
            result_count, match_count = len(listings), stats["matching"]
            status = "ok" if listings else "empty"
            error = None if listings else "The platform returned no results; check the search URL and filters"
            flagged = sum(1 for x in new + drops if x["risk_level"] in ("medium", "high"))
            log.info("[%s] %s: %d results, %d matching, %d filtered out, %d older than %s days, %d without price,"
                     " %d item pages read%s",
                     label, s["name"], len(listings), stats["matching"], stats["filtered"], stats["too_old"],
                     max_age or "∞", stats["no_price"], stats["pages"],
                     " (baseline saved, no email)" if baseline
                     else f"; {len(new)} new, {len(drops)} price drops, {flagged} flagged")
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

    if settings["run_requested_at"] is not None:
        db.execute("UPDATE settings SET run_requested_at = NULL WHERE id = 1 AND run_requested_at = ?",
                   (to_db_time(from_db_time(settings["run_requested_at"])),))
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
