"""Command line:

  python -m price_monitor                   keep running; tick every TICK_SECONDS (Ctrl+C to stop)
  python -m price_monitor --once            one tick, then exit (what cron runs)
  python -m price_monitor --test-email      email the latest stored listings, to check delivery
  python -m price_monitor --import FILE     add the searches in a JSON file to the database
  python -m price_monitor --export FILE     write every search to a JSON file --import reads (a backup)
  python -m price_monitor --delete-old-photos DAYS  delete the saved photos of ads gone for more than DAYS
                                            days (pinned ones kept)
  python -m price_monitor --rescore         work out every stored listing's risk again with the current
                                            rules (after they change; reads nothing from the platforms)
  python -m price_monitor --status          print the searches and their last run
  python -m price_monitor --resend-all      clear the stored listings, then run every search and
                                            email everything that matches now (one full digest)

  --platforms olx,vinted   only these platforms (default: PLATFORMS in .env, else all)
  --search 3,4             only these searches (ids from --status), run now
  e.g. python -m price_monitor --once --platforms facebook
"""

import json
import logging
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from .db import Db, to_db_time, utcnow
from .emailer import by_product, render_digest, send_email
from .providers import PROVIDERS, get_provider_class
from .runner import allowed_platforms, env_number, tick, to_ron

BASE_DIR = Path(__file__).resolve().parent.parent
SQLITE_PATH = BASE_DIR / "price_monitor.db"
log = logging.getLogger("price_monitor")


def open_db():
    load_dotenv(BASE_DIR / ".env", override=True)  # re-read: edits apply without a restart
    return Db(SQLITE_PATH)


def import_searches(db, path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit(f"{path}: expected a JSON list of searches")
    added = 0
    for s in data:
        platform = (s.get("platform") or "olx").lower()
        url = s.get("query_url") or s.get("page_url")
        if not s.get("name") or not url:
            raise SystemExit(f"{path}: every search needs a name and a query_url")
        if get_provider_class(platform) is None:
            log.warning("%s: platform %r has no provider yet; adding it anyway", s["name"], platform)
        if db.one("SELECT id FROM searches WHERE platform = ? AND name = ?", (platform, s["name"])):
            log.info("Skipping %s (%s): already exists", s["name"], platform)
            continue
        currency = (s.get("currency") or "RON").strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise SystemExit(f"{path}: {s['name']}: currency must be a 3-letter code such as RON or EUR, not {currency!r}")
        options = s.get("options") or {}
        if s.get("category_id"):
            options["category_id"] = s["category_id"]
        db.execute(
            "INSERT INTO searches (platform, name, query_url, currency, min_budget, max_budget, min_price, max_price,"
            " store_outside_prices, include_keywords, exclude_keywords, options, enabled, show_listings,"
            " notify_new, notify_price_drops, interval_minutes, max_age_days, sort_order)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (platform, s["name"], url, currency, s.get("min_budget"), s.get("max_budget"), s.get("min_price"), s.get("max_price"),
             1 if s.get("store_outside_prices") else 0,
             json.dumps(s.get("include_keywords") or [], ensure_ascii=False),
             json.dumps(s.get("exclude_keywords") or s.get("exclude") or [], ensure_ascii=False),
             json.dumps(options) if options else None, 1 if s.get("enabled", True) else 0,
             1 if s.get("show_listings", True) else 0,
             1 if s.get("notify_new", True) else 0, 1 if s.get("notify_price_drops", True) else 0,
             s.get("interval_minutes"), s.get("max_age_days"), s.get("sort_order", 100)))
        added += 1
        log.info("Added %s (%s)", s["name"], platform)
    db.commit()
    log.info("Imported %d searches", added)


# The search settings an export keeps, in --import's field names (not the monitor's own state: last
# run, baseline, provider_state); JSON columns are written as JSON
EXPORT_FIELDS = ["name", "platform", "query_url", "currency", "min_budget", "max_budget", "min_price", "max_price",
                 "store_outside_prices", "include_keywords", "exclude_keywords", "options", "enabled", "show_listings",
                 "notify_new", "notify_price_drops", "interval_minutes", "max_age_days", "sort_order"]
BOOL_FIELDS = {"store_outside_prices", "enabled", "show_listings", "notify_new", "notify_price_drops"}
JSON_FIELDS = {"include_keywords": [], "exclude_keywords": [], "options": {}}


def export_searches(db, path):
    """Every search, in --import's format, so `--import FILE` restores them into an empty database"""
    searches = []
    for row in db.all("SELECT * FROM searches ORDER BY sort_order, name, platform"):
        s = {}
        for key in EXPORT_FIELDS:
            value = row.get(key)
            if key in JSON_FIELDS:
                value = json.loads(value) if value else JSON_FIELDS[key]
            elif key in BOOL_FIELDS:
                value = bool(value)
            elif isinstance(value, float) and value.is_integer():
                value = int(value)
            s[key] = value
        searches.append(s)
    Path(path).write_text(json.dumps(searches, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log.info("Exported %d searches to %s", len(searches), path)


def resend_all(db, platforms=None, search_ids=None):
    """Forget what was stored and send it all again, for the searches this copy runs: their
    listings (and price history) are deleted, except pinned ones; they keep a baseline date so
    the run reports everything as new, and that run happens now."""
    allowed = allowed_platforms(platforms)
    ids = [s["id"] for s in db.all("SELECT id, platform FROM searches WHERE enabled = 1")
           if (allowed is None or s["platform"].lower() in allowed) and (not search_ids or s["id"] in search_ids)]
    if not ids:
        raise SystemExit("No enabled searches to resend here")
    marks = ", ".join("?" for _ in ids)
    # Pinned and excluded listings stay (the dashboard's own marks), so they aren't in the email again
    unpinned = f"search_id IN ({marks}) AND pin_order IS NULL AND excluded_at IS NULL"
    n = db.one(f"SELECT COUNT(*) AS n FROM listings WHERE {unpinned}", ids)["n"]
    db.execute(f"DELETE FROM listings WHERE {unpinned}", ids)  # history goes with them
    db.execute(f"UPDATE searches SET baseline_at = COALESCE(baseline_at, ?) WHERE id IN ({marks})",
               [to_db_time(utcnow())] + ids)
    db.commit()
    log.info("Cleared %d stored listings from %d searches; running them now", n, len(ids))
    tick(db, platforms, ids)


def print_status(db):
    settings = db.one("SELECT * FROM settings WHERE id = 1")
    allowed = allowed_platforms()
    print(f"Monitor {'on' if settings['enabled'] else 'OFF'}; default interval {settings['default_interval_minutes']} min;"
          f" ads up to {settings['default_max_age_days']} days old;"
          f" last tick {settings['last_tick_at'] or 'never'} UTC; database: {db.kind}")
    for s in db.all("SELECT * FROM searches ORDER BY sort_order, id"):
        n = db.one("SELECT COUNT(*) AS n FROM listings WHERE search_id = ?", (s["id"],))["n"]
        skipped = allowed is not None and s["platform"].lower() not in allowed
        print(f"  #{s['id']:<3} {'on ' if s['enabled'] else 'off'} {s['platform']:<8} {s['name']:<28}"
              f"{' [not run here: PLATFORMS]' if skipped else ''}"
              f" every {s['interval_minutes'] or settings['default_interval_minutes']} min,"
              f" last run {s['last_run_at'] or 'never'}: {s['last_status'] or '-'}"
              f" ({s['last_match_count'] if s['last_match_count'] is not None else '-'} matching, {n} stored)"
              + (f"\n        error: {s['last_error']}" if s["last_error"] else ""))


def test_email(db):
    reports = []
    for s in db.all("SELECT * FROM searches ORDER BY sort_order, id"):
        cls = get_provider_class(s["platform"])
        label = cls.label if cls else s["platform"]
        rows = db.all("SELECT * FROM listings WHERE search_id = ? ORDER BY first_seen DESC LIMIT 5", (s["id"],))
        items = [{**r, "platform_label": label, "price_ron": r["price_ron"] or to_ron(r["price"], r["currency"])}
                 for r in rows]
        if items:
            drop = {**items[0], "old_price": items[0]["price"] * 1.1, "drop_pct": 100 / 11}
            reports.append({"search": {**s, "platform_label": label}, "new": items[1:], "drops": [drop]})
    if not reports:
        raise SystemExit("No stored listings yet; run a check first")
    settings = db.one("SELECT email_to, merge_emails FROM settings WHERE id = 1")
    for product, group in by_product(reports, bool(settings["merge_emails"])):  # as the real ones
        subject, html_body, text_body = render_digest(group, os.getenv("DASHBOARD_URL"), product)
        send_email("[TEST] " + subject, html_body, text_body, settings["email_to"] or os.getenv("EMAIL_TO"))


def option(argv, name):
    """--name value, or None"""
    if name not in argv:
        return None
    i = argv.index(name)
    if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
        raise SystemExit(f"{name} needs a value")
    return argv[i + 1]


def main(argv):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    platforms = option(argv, "--platforms")
    try:
        search_ids = {int(x) for x in (option(argv, "--search") or "").split(",") if x.strip()} or None
    except ValueError:
        raise SystemExit("--search takes search ids, e.g. --search 3,4 (see --status)")
    db = open_db()
    try:
        if "--import" in argv:
            i = argv.index("--import")
            if i + 1 >= len(argv):
                raise SystemExit("Usage: python -m price_monitor --import FILE")
            import_searches(db, argv[i + 1])
            return 0
        if "--delete-old-photos" in argv:
            from . import thumbs
            from .db import utcnow
            days = option(argv, "--delete-old-photos")
            if not (days or "").isdigit():
                raise SystemExit("Usage: python -m price_monitor --delete-old-photos DAYS (photos of ads gone longer)")
            log.info("Deleted the photos of ads gone for more than %s days: %d", days, thumbs.delete_old(db, int(days), utcnow()))
            return 0
        if "--rescore" in argv:
            from .runner import rescore
            log.info("Re-scored the stored listings: %d changed", rescore(db))
            return 0
        if "--export" in argv:
            i = argv.index("--export")
            if i + 1 >= len(argv):
                raise SystemExit("Usage: python -m price_monitor --export FILE")
            export_searches(db, argv[i + 1])
            return 0
        if "--resend-all" in argv:
            resend_all(db, platforms, search_ids)
            return 0
        if "--status" in argv:
            print_status(db)
            return 0
        if "--test-email" in argv:
            test_email(db)
            return 0
        if "--once" in argv or search_ids:
            tick(db, platforms, search_ids)
            return 0
    finally:
        db.close()

    seconds = env_number("TICK_SECONDS", 60)
    log.info("Running; checking what's due every %g s (Ctrl+C to stop). Platforms: %s",
             seconds, ", ".join(sorted(allowed_platforms(platforms) or PROVIDERS)))
    try:
        while True:
            try:
                db = open_db()
                try:
                    tick(db, platforms)
                finally:
                    db.close()
            except SystemExit as e:
                log.error("%s", e)
            except Exception:
                log.exception("Tick failed; trying again next time")
            time.sleep(seconds)
    except KeyboardInterrupt:
        log.info("Stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
