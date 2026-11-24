# python-price-monitor

Watches searches on second-hand platforms (OLX, Publi24, eBay, Vinted and Facebook Marketplace) and emails
one digest when a search finds **new listings** or a listing's **price drops**,
with **possible scams flagged** and the reasons spelled out.

- Each platform is a small module in [`price_monitor/providers/`](price_monitor/providers/).
  Everything else (filtering, change detection, storage, email) is shared.
- Searches, intervals and the on/off switch live in the database, so another
  app can manage them. Here, that's the codename-dojo dashboard's Price monitor
  page. See [Database contract](#database-contract).
- Runs from cron on the server (one check every 5 minutes, and each search's
  own interval decides whether it runs), or as a loop on your computer.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # Windows: .venv\Scripts\pip install -r requirements.txt
cp .env.example .env                        # then fill it in
```

`.env`:

| Key | Meaning |
|---|---|
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS` | Mail server, port 587 with STARTTLS |
| `EMAIL_TO` | Recipient(s), comma-separated. The dashboard can override it (`settings.email_to`) |
| `EMAIL_TITLE` | The title at the top of the email |
| `EMAIL_FROM_NAME`, `EMAIL_FROM_ADDRESS` | The sender shown in the inbox. The address defaults to `SMTP_USER` |
| `EMAIL_KICKER` | The small label above the heading (default "Price monitor"; empty: none). It stays out of the inbox preview |
| `EMAIL_SUBJECT_PREFIX` | Optional: a start for the subject |
| `ENVIRONMENT` | Which copy sent the email (`production` on the server, `local` on a laptop), shown in capitals next to the label. Default: the machine's name |
| `EMAIL_FOOTER` | The line under the email |
| `DASHBOARD_URL` | The email's "Open in dashboard" button. Unset: no button |
| `PRICE_DROP_MIN_PCT` | Report drops of at least this many percent (default 3) |
| `TICK_SECONDS` | How often the local loop checks what's due (default 60) |
| `PLATFORMS` | The platforms this copy runs, e.g. `olx,vinted` (default: all). See [Server and laptop](#server-and-laptop) |
| `MYSQL_*` | Use MySQL instead of the local `price_monitor.db` SQLite file |

**Gmail:** use an [App Password](https://myaccount.google.com/apppasswords)
(it requires 2-step verification), not your normal password, with
`SMTP_HOST=smtp.gmail.com` and `SMTP_PORT=587`.

With no SMTP settings, the email is printed instead of sent.

## Running

```bash
.venv/bin/python -m price_monitor --import searches.example.json   # add searches (once)
.venv/bin/python -m price_monitor                  # keep running; Ctrl+C to stop
.venv/bin/python -m price_monitor --once           # one check, then exit (what cron runs)
.venv/bin/python -m price_monitor --status         # the searches and their last run
.venv/bin/python -m price_monitor --test-email     # email the latest listings, to check delivery
.venv/bin/python -m price_monitor --resend-all     # clear the stored listings (pinned ones stay) and email everything that matches now
```

Add `--platforms facebook` to any of these to limit it to some platforms, or
`--search 3,4` to run chosen searches now (ids from `--status`).

Searches run, and appear in the email, in their `sort_order` (lower first).

A search's **first run** saves what's there as a baseline, without an email.
After that, each run reports listings it hasn't seen and drops of at least
`PRICE_DROP_MIN_PCT` (compared in the same currency).

### What a search filters

- **Price:** `min_price` / `max_price` in RON. Other currencies are converted at the National Bank of
  Romania's daily rates (BNR, `curs.bnr.ro`): fetched at most every 6 hours and kept in `settings`, so
  a failed fetch keeps the last ones; a new set also recalculates every stored listing's `price_ron`.
  No real rates are built in or set by hand: without a BNR rate (before the first fetch ever, or a
  currency BNR doesn't list) a placeholder of 5 lei is used, logged, and corrected by the next fetch. Ads outside
  the range are still stored (for the price estimate, [docs/price-estimate.md](docs/price-estimate.md))
  but never emailed; the dashboard shows only the ones in the range. To see the whole market,
  leave the price out of the search's link (OLX's price filter, eBay's `_udlo`/`_udhi`) and set it
  here instead: the platform never returns what its link filters out. Widening the range later
  doesn't email the ads already stored.
- **Must contain** (`include_keywords`): the title has to contain at least one of
  these. Useful on Vinted, whose search also returns loosely related items.
- **Leave out** (`exclude_keywords`): titles containing any of these are skipped.
- **Age** (`max_age_days`, default 30): older ads are skipped. OLX gives the
  posting date. Vinted doesn't, so it's estimated from the item id (ids only
  grow, about 15 million a day); close for days and weeks.
- Ads without a price (swap-only) are skipped.

### Possible scams

Every listing gets a risk level (`low`, `medium`, `high`) from warning signs
([`risk.py`](price_monitor/risk.py)). The email shows each with its reasons, in yellow
(*"Warning sign"*), orange (*"Possible scam"*) or red, e.g. *"⚠ Likely scam: seller account
3 days old; only one photo"*.
Nothing is hidden: a flag means "look closer, and pay only through the platform".

| Sign | Points | Where it comes from |
|---|---|---|
| Price 35% or more below the search's typical price (the median of its current matches, 5 or more) | 2 (3 at half or less) | Both |
| Seller account under 30 days old (under 7 days: 3) | 2 | OLX |
| Seller with no reviews, or rated under 4/5 | 1 | Vinted (item page), eBay, OLX (its ratings service) |
| 2 or more negative reviews, a tenth of them or more (OLX: 1-2 stars) | 1 | Vinted, OLX |
| Phrases: payment in advance, money transfers, WhatsApp/Telegram, payment links, "only by courier", "I'm abroad" | 2 each, up to 2 kinds | OLX description; Vinted title and description |
| A phone number or email in the ad (to talk off the platform) | 2 | Vinted |
| One photo or none | 1 | Both |
| OLX delivery off, when there's another sign | 1 | OLX |

1 point is `low`, 2-3 `medium`, 4 or more `high`. Only these derived facts are
stored, never who the seller is.

A seller with no reviews yet adds its point, but alone it gives no level: only a grey
*"ⓘ Seller has no reviews yet"* under the listing (`risk_notes`). With any other sign it counts
toward the level, and the level's reasons include it.

OLX's seller ratings (what an ad's page shows, e.g. *"5.0 / 5, 18 ratinguri"*) come from its
public ratings service by the seller's id, looked up once per listing (at most 40 per search per
run, those in the price range first) and kept on it; a seller is looked up once per run. Most OLX
sellers have none: OLX only collects ratings after a purchase with OLX delivery.

Vinted shows the description and the seller's reviews only on the item's page,
so the monitor reads it for listings it hasn't stored yet that pass the
filters (at most 15 per search per run, 1.5 s apart). That page also says when
the item was uploaded, which tightens the age check.
## Platforms

| `platform` | Link to paste as `query_url` | Notes |
|---|---|---|
| `olx` | An OLX.ro results page with your filters set | Uses the JSON endpoint the site loads results from. The category is detected from the page once; set `{"category_id": N}` in `options` to fix it |
| `vinted` | A Vinted catalog page (`https://www.vinted.ro/catalog?...`) | Reads the results embedded in the page, and the item page for new matches (see Possible scams). Its "newest first" isn't strict, so ages are estimated from item ids, and the item page's upload date wins when it's older. Prices include the buyer protection fee (`{"price": "item"}` in `options` for the seller's price). vinted.ro also shows items from Poland, Greece and more, so add keywords in those languages where needed |

`publi24`: a Publi24 search (`https://www.publi24.ro/anunturi/?q=playstation+5`). It reads the
results page: one page (20 listings) with price, place, date and photo count, and each ad's
description from the page's structured data, for the scam phrases.

`ebay`: an eBay search (`https://www.ebay.de/sch/i.html?_nkw=xbox+series+x+konsole&_udlo=200&_udhi=650`).
eBay blocks its search pages from servers, so this uses its official **Browse API**: create a free
Production keyset at [developer.ebay.com](https://developer.ebay.com) and set `EBAY_CLIENT_ID`
(App ID) and `EBAY_CLIENT_SECRET` (Cert ID). The link's site picks the marketplace (ebay.de: EUR),
`_nkw` the words, `_udlo`/`_udhi` a price range on the item, `_sacat` a category. Buy-it-now and
best-offer only (`{"auctions": true}` in `options` adds auctions). A listing's **price is the total
delivered** to `EBAY_SHIP_TO` (default `RO`): price + the cheapest shipping there, plus, for items
shipped from outside the EU, customs duty (`{"duty_pct": N}` in `options`, 0 for consoles), import
VAT on all of that (`IMPORT_VAT_PCT`, 21) and a courier's fee (`IMPORT_FEE_RON`, 0). The breakdown
("320 € + 45 € shipping + 77 € import VAT (21%)") is in `price_detail`, under the price in the email
and the dashboard. Min/max compare that total in lei, at BNR's rates.

`facebook`: a Marketplace search with your filters set
(`https://www.facebook.com/marketplace/bucharest/search/?query=...&minPrice=...&daysSinceListed=30`).
It reads the results embedded in the page: one page per run (about 20 listings), sold and pending
ones skipped, posting dates exact, the seller's name not kept, and at most every 30 minutes.
Facebook shows them to logged-out visitors only from some connections (a home one, not the
server), so on the server it uses a logged-in session: put a browser's facebook.com cookies in
`.env.server` as `FACEBOOK_COOKIES="c_user=...; xs=...; datr=..."` (ideally a spare account's:
automated use is against Facebook's terms, and a session used from a datacenter may get security
checks or a lock), then `./deploy.sh`. When the session expires or Facebook asks for a check, the
search's error says so in the dashboard.

### Server and laptop

Each copy runs only the platforms in its `PLATFORMS`, so nothing runs twice. The server runs
`olx,publi24,vinted` from cron (eBay waits for API keys; add `facebook` only with `FACEBOOK_COOKIES` set); a laptop has
`PLATFORMS=none` and runs things only by hand, Facebook included:

```bash
.venv/bin/python -m price_monitor --once --platforms facebook             # Facebook, logged out, from home
.venv/bin/python -m price_monitor --once --platforms facebook --search 2  # one search
```

A laptop keeps its own data in `price_monitor.db` and sends its own email (marked `local`).
"Run all now" in the dashboard becomes a request on every search, so each copy runs its own share.

### Adding a platform

1. Write `price_monitor/providers/<name>.py` with a `Provider` subclass: set `name`
   (the value of `searches.platform`) and `label`, and implement
   `search(spec) -> list[Listing]` (see [`base.py`](price_monitor/providers/base.py)).
   Fill `Listing.signals` with what the platform shows about scam risk, and set
   `needs_enrich` and `enrich()` if that needs the item's own page.
   Raise `ProviderError` for problems worth showing in the dashboard.
2. Register it in [`providers/__init__.py`](price_monitor/providers/__init__.py).
3. Deploy. On its next check the monitor reports it in `settings.providers`, and the
   dashboard offers it. Until then, pick "Custom…" there and type the name.

## Running on the biolete.ro host

```bash
./deploy.sh            # copy code + config, set up the venv, install the cron job
./deploy.sh --remove   # remove the cron job
./deploy.sh --resend   # deploy, then clear the stored listings and email everything that matches now
ssh biolete.ro 'cd apps/price-monitor && .venv/bin/python -m price_monitor --status'
ssh biolete.ro tail -f logs/cron-price-monitor.log
```

- The server copy lives in `~/apps/price-monitor` and runs on Python 3.11 from
  `/opt/alt/python311`. It stores its data in the MySQL database `bioleter_price_monitor`.
- The MySQL login and the cron schedule are in `.env.server` (local, not committed),
  which `deploy.sh` adds to the server's `.env`. Locally the monitor uses SQLite.
- Re-run `./deploy.sh` after changing the code or `.env`. Searches and settings
  live in the database, so changing them needs no deploy.
- Don't loop locally against the same searches while cron runs on the server:
  they keep separate data, so both would email you about the same ads.

### Scheduling elsewhere

Run `--once` every 5 minutes:

- **Linux / macOS (cron):** `*/5 * * * * cd /path/to/price-monitor && .venv/bin/python -m price_monitor --once >> monitor.log 2>&1`
- **Windows (Task Scheduler):** a task that repeats every 5 minutes indefinitely,
  running `C:\path\to\price-monitor\.venv\Scripts\python.exe` with the arguments
  `-m price_monitor --once`, starting in the project folder.

## Database contract

The monitor owns the schema. It creates the tables on start and adds new columns
itself (`settings.schema_version`, now 10). Other apps may read everything and
write only the columns marked ✎. All times are UTC `DATETIME`s. JSON columns hold
text.

**`searches`**: one row per watched search.

| Column | |
|---|---|
| `id`, `created_at`, `updated_at` | `updated_at` is set by whoever edits the row |
| `platform` ✎ | A provider's `name` (`olx`, `vinted`); unknown ones fail with a clear `last_error` |
| `name` ✎, `query_url` ✎ | What it's called; the results page to watch |
| `min_price` ✎, `max_price` ✎ | RON, `NULL` for no bound |
| `include_keywords` ✎, `exclude_keywords` ✎ | JSON lists of strings |
| `options` ✎ | JSON object of platform-specific settings |
| `enabled` ✎, `interval_minutes` ✎, `max_age_days` ✎ | `NULL` interval or age: the defaults in `settings` |
| `notify_new` ✎, `notify_price_drops` ✎ | What to email about |
| `sort_order` ✎ | Lower first: the order searches run and appear in the email (default 100) |
| `run_requested_at` ✎ | Set to now for "Run now": runs at the next check, even if disabled |
| `baseline_at` ✎ | `NULL` makes the next run save a fresh baseline without an email |
| `provider_state` | The monitor's own (e.g. a detected OLX category) |
| `last_run_at`, `last_status` (`ok`/`empty`/`error`), `last_error`, `last_result_count`, `last_match_count` | Written after every run |

Other apps may also **delete** `listings` rows (with their `price_history` and `status_history`, as the dashboard's
Listings page does). Nothing else remembers a listing, so the monitor's next run finds it again,
and reports it as new, if its search still matches the ad.

**`listings`**: what each search found and still matches its keywords, **also outside its price
range**: show a search's listings with `price_ron` between its `min_price` and `max_price`, to match
the email. Unique on `(search_id, external_id)`. Its text is stored plain (HTML entities decoded,
tags, control and invisible characters removed, [`clean.py`](price_monitor/clean.py)) and its
links are `http(s)` only (an ad without one is skipped); still escape it when showing it.
Its columns: `search_id`, `platform`, `external_id`, `title`, `url`, `image_url`,
`price`, `currency`, `price_ron` (the price in lei the range compares; eBay's total), `location`, `posted_at` (given or estimated),
`first_seen`, `last_seen`, and the scam flags: `risk_level` (`low`/`medium`/`high`
or `NULL`), `risk_score`, `risk_reasons` (JSON list of sentences) and
`risk_signals` (the derived facts they came from), `risk_notes` (JSON list of reasons worth a
mention even when the listing isn't flagged, e.g. "seller has no reviews yet"; show them when not
showing the flag; `NULL`: none), `price_detail` (how `price` adds up, for
eBay's totals), and `pin_order` ✎: pinned in the dashboard, in this order (lower first; `NULL`: not
pinned). The monitor never deletes a pinned listing, and `--resend-all` keeps them.
`inactive_at`: when the ad was found sold or removed (`NULL`: active), and `checked_at`: when it
was last checked. After each run that returns results, the monitor opens the pages of up to 10 stored
ads the run didn't show (they fell off the results or no longer match), each at most every 12 hours:
a missing ad (404, or a redirect away from it) or one its page says is closed gets `inactive_at`; OLX
pages also update the price. An inactive ad that shows up in the results again is active again.

**`price_history`**: `listing_id`, `price`, `currency`, `seen_at`. One row when a
listing is first seen (`seen_at` = its `first_seen`), and one for every change.

**`status_history`**: `listing_id`, `status`, `seen_at`. A row when the ad was found sold or
removed (`inactive`), and when it showed up in the results again after that (`active`).

**`settings`**: a single row, `id = 1`.

| Column | |
|---|---|
| `enabled` ✎ | `0` stops every search |
| `default_interval_minutes` ✎, `default_max_age_days` ✎ | Defaults 10 and 30 |
| `email_to` ✎ | Overrides `EMAIL_TO`; `NULL` to use it |
| `run_requested_at` ✎ | "Run all now": the monitor turns it into a request on every enabled search |
| `last_tick_at` | The last check (cron alive?) |
| `providers` | JSON list of `{name, label}`: the platforms the copy writing here runs |
| `exchange_rates`, `exchange_rates_at` | The rates to lei in use, JSON `{date, source: "BNR", rates: {EUR: 5.34, ...}}`, and when they were fetched |
| `prices_in_ron` ✎ | The dashboard's table shows prices in lei (the ad's own under them); `0`: as the ad shows them. Default `1` |

Deleting a search deletes its listings and their history (foreign keys cascade).
