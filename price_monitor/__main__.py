"""Command line:

  python -m price_monitor                   keep running; tick every TICK_SECONDS (Ctrl+C to stop)
  python -m price_monitor --once            one tick, then exit (what cron runs)
  python -m price_monitor --test-email      email the latest stored listings, to check delivery
  python -m price_monitor --import FILE     add the searches in a JSON file to the database
  python -m price_monitor --status          print the searches and their last run
"""

import json
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from .db import Db
from .emailer import render_digest, send_email
from .providers import PROVIDERS, get_provider_class
from .runner import env_number, tick, to_ron

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
        options = s.get("options") or {}
        if s.get("category_id"):
            options["category_id"] = s["category_id"]
        db.execute(
            "INSERT INTO searches (platform, name, query_url, min_price, max_price, include_keywords,"
            " exclude_keywords, options, enabled, interval_minutes, max_age_days) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (platform, s["name"], url, s.get("min_price"), s.get("max_price"),
             json.dumps(s.get("include_keywords") or [], ensure_ascii=False),
             json.dumps(s.get("exclude_keywords") or s.get("exclude") or [], ensure_ascii=False),
             json.dumps(options) if options else None, 1 if s.get("enabled", True) else 0,
             s.get("interval_minutes"), s.get("max_age_days")))
        added += 1
        log.info("Added %s (%s)", s["name"], platform)
    db.commit()
    log.info("Imported %d searches", added)


def print_status(db):
    settings = db.one("SELECT * FROM settings WHERE id = 1")
    print(f"Monitor {'on' if settings['enabled'] else 'OFF'}; default interval {settings['default_interval_minutes']} min;"
          f" ads up to {settings['default_max_age_days']} days old;"
          f" last tick {settings['last_tick_at'] or 'never'} UTC; database: {db.kind}")
    for s in db.all("SELECT * FROM searches ORDER BY platform, id"):
        n = db.one("SELECT COUNT(*) AS n FROM listings WHERE search_id = ?", (s["id"],))["n"]
        print(f"  #{s['id']:<3} {'on ' if s['enabled'] else 'off'} {s['platform']:<8} {s['name']:<28}"
              f" every {s['interval_minutes'] or settings['default_interval_minutes']} min,"
              f" last run {s['last_run_at'] or 'never'}: {s['last_status'] or '-'}"
              f" ({s['last_match_count'] if s['last_match_count'] is not None else '-'} matching, {n} stored)"
              + (f"\n        error: {s['last_error']}" if s["last_error"] else ""))


def test_email(db):
    eur = env_number("EUR_TO_RON", 4.97)
    reports = []
    for s in db.all("SELECT * FROM searches ORDER BY platform, id"):
        cls = get_provider_class(s["platform"])
        label = cls.label if cls else s["platform"]
        rows = db.all("SELECT * FROM listings WHERE search_id = ? ORDER BY first_seen DESC LIMIT 5", (s["id"],))
        items = [{**r, "platform_label": label, "price_ron": to_ron(r["price"], r["currency"], eur)} for r in rows]
        if items:
            drop = {**items[0], "old_price": items[0]["price"] * 1.1, "drop_pct": 100 / 11}
            reports.append({"search": {**s, "platform_label": label}, "new": items[1:], "drops": [drop]})
    if not reports:
        raise SystemExit("No stored listings yet; run a check first")
    subject, html_body, text_body = render_digest(reports, os.getenv("DASHBOARD_URL"))
    settings = db.one("SELECT email_to FROM settings WHERE id = 1")
    send_email("[TEST] " + subject, html_body, text_body, settings["email_to"] or os.getenv("EMAIL_TO"))


def main(argv):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    db = open_db()
    try:
        if "--import" in argv:
            i = argv.index("--import")
            if i + 1 >= len(argv):
                raise SystemExit("Usage: python -m price_monitor --import FILE")
            import_searches(db, argv[i + 1])
            return 0
        if "--status" in argv:
            print_status(db)
            return 0
        if "--test-email" in argv:
            test_email(db)
            return 0
        if "--once" in argv:
            tick(db)
            return 0
    finally:
        db.close()

    seconds = env_number("TICK_SECONDS", 60)
    log.info("Running; checking what's due every %g s (Ctrl+C to stop). Platforms: %s",
             seconds, ", ".join(sorted(PROVIDERS)))
    try:
        while True:
            try:
                db = open_db()
                try:
                    tick(db)
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
