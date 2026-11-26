# python-price-monitor

Watches searches on second-hand platforms (OLX, Publi24, eBay, Vinted and Facebook Marketplace) and emails
one digest when a search finds **new listings** or a listing's **price drops**,
with **possible scams flagged** and the reasons spelled out.

- **It works on its own.** It keeps its data in a local SQLite file (or MySQL), creates and upgrades
  its tables itself, and is driven from the command line: add searches from a JSON file, run them,
  and read the results in your inbox. See [Running without the dashboard](#running-without-the-dashboard).
- **A dashboard is optional.** Searches, settings and listings live in the database, so another app
  can manage and browse them. Here, that's the codename-dojo dashboard's Price monitor page; it adds
  editing, a listings table with filters, pins and a price estimate. See
  [Database contract](#database-contract) for what such an app may read and write.
- Each platform is a small module in [`price_monitor/providers/`](price_monitor/providers/).
  Everything else (filtering, change detection, storage, email) is shared.
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
| `EMAIL_SUBJECT`, `EMAIL_SUBJECT_ALL` | The subjects: one email for all products a check found something for (`settings.merge_emails`, the default; *New listing updates*), or one per product (*New listing updates for {product}*, `{product}` filled in). Each is the same every time, with reply headers to match, so they form one conversation (per product). What's new leads the preview |
| `ENVIRONMENT` | Which copy sent the email (`production` on the server, `local` on a laptop), shown in capitals next to the label. Default: the machine's name |
| `EMAIL_FOOTER` | The line under the email |
| `DASHBOARD_URL` | The email's "Open in dashboard" button. Unset: no button |
| `PRICE_DROP_MIN_PCT` | Report drops of at least this many percent (default 3) |
| `TICK_SECONDS` | How often the local loop checks what's due (default 60) |
| `PLATFORMS` | The platforms this copy runs, e.g. `olx,vinted` (default: all). See [Server and laptop](#server-and-laptop) |
| `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DATABASE` | Use MySQL instead of the local `price_monitor.db` SQLite file (set `MYSQL_DATABASE` to switch) |
| `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`, `EBAY_SHIP_TO`, `IMPORT_VAT_PCT`, `IMPORT_FEE_RON` | eBay only: its API keys and how a total delivered price is worked out. See [Platforms](#platforms) |
| `FACEBOOK_COOKIES`, `FACEBOOK_USER_AGENT` | Facebook Marketplace only: a logged-in session, for a server. See [Platforms](#platforms) |
| `CRON_SCHEDULE` | Server only, read by `deploy.sh`: how often cron wakes the monitor (default every 5 minutes) |

[`.env.example`](.env.example) lists them all with examples. On the server, `deploy.sh` builds the
`.env` from your local `.env` plus `.env.production` (the server-only keys: MySQL, cron, and any
you want different there).

**Gmail:** use an [App Password](https://myaccount.google.com/apppasswords)
(it requires 2-step verification), not your normal password, with
`SMTP_HOST=smtp.gmail.com` and `SMTP_PORT=587`.

With no SMTP settings, the email is printed instead of sent.

## Running

```bash
.venv/bin/python -m price_monitor --import searches.example.json   # add searches (once)
.venv/bin/python -m price_monitor --export searches.json           # save every search to a file (a backup)
.venv/bin/python -m price_monitor --rescore                        # every stored listing's risk again, with the current rules
.venv/bin/python -m price_monitor                  # keep running; Ctrl+C to stop
.venv/bin/python -m price_monitor --once           # one check, then exit (what cron runs)
.venv/bin/python -m price_monitor --status         # the searches and their last run
.venv/bin/python -m price_monitor --test-email     # email the latest listings, to check delivery
.venv/bin/python -m price_monitor --resend-all     # clear the stored listings (pinned and excluded ones stay) and email everything that matches now
```

Add `--platforms facebook` to any of these to limit it to some platforms, or
`--search 3,4` to run chosen searches now (ids from `--status`).

Searches run, and appear in the email, in their `sort_order` (lower first).

### The searches file

`--import` adds the searches in a JSON list ([`searches.example.json`](searches.example.json) is a
full one). A search already in the database (same `platform` and `name`) is skipped, so importing
again never duplicates or overwrites anything. Each search:

| Field | |
|---|---|
| `name` | Required. What it's called, also the product it belongs to: searches with the same name on different platforms are one product (one email section, one price estimate) |
| `platform` | `olx` (default), `vinted`, `publi24`, `ebay` or `facebook` |
| `query_url` | Required. The platform's results page with your filters set; see [Platforms](#platforms) |
| `currency` | What the budget and prices below are in: `RON` (default), `EUR`, ... |
| `min_budget`, `max_budget` | What you'd pay: only these are emailed (and listed in a dashboard) |
| `min_price`, `max_price` | What a real one costs, broader than the budget: anything outside isn't stored (games, parts) |
| `store_outside_prices` | `true` to store ads outside the prices too (default `false`) |
| `include_keywords`, `exclude_keywords` | Must contain one of / leave out any of, matched in the title, lowercase |
| `options` | Platform settings, e.g. `{"category_id": 1882}` for OLX |
| `enabled` | Default `true`. Off: it doesn't run |
| `notify_new`, `notify_price_drops` | Email new listings / price drops, default `true` both |
| `show_listings` | For a dashboard: list its listings (default `true`) or only gather them |
| `interval_minutes`, `max_age_days` | Default: `settings.default_interval_minutes` (10) and `default_max_age_days` (30) |
| `sort_order` | Lower first (default 100) |

Budgets and prices are in the search's `currency` (lei by default); they and the ads are compared in lei,
converted at BNR's rates (see [What a search filters](#what-a-search-filters)).

### Backing up the searches

The searches (with every budget, price, keyword and switch) live only in the database, so keep a copy:
`--export FILE` writes them all in the same format `--import` reads, and importing that file into an
empty database brings them back unchanged.

- **On the server, daily:** the cron job `deploy.sh` installs also exports them every night to
  `backups/searches-YYYY-MM-DD.json` in the app's folder, keeping the last 14. That covers a bad edit
  or a broken upgrade.
- **On your computer:** `./deploy.sh --export` runs the export on the server and saves `searches.json`
  here, a copy away from the server. It's gitignored, since this repo is public and the file shows what
  you're searching for and your budgets; keep it in your computer's backups instead.

To restore, into an empty database: `.venv/bin/python -m price_monitor --import searches.json` (on the
server, from a `backups/` file). The export holds the search links and settings only (no passwords or
cookies), and not the listings: the searches gather those again.

### Running without the dashboard

Everything above works with no dashboard: import searches, schedule `--once` (see
[Scheduling elsewhere](#scheduling-elsewhere)) or run the loop, and the emails are the results.
`--status` shows each search's id, whether it's on, and its last run.

To change a search after importing it, edit its row: the columns are in the
[Database contract](#database-contract). With the default SQLite file (`price_monitor.db`, always in the
project folder), from that folder:

```bash
sqlite3 price_monitor.db "UPDATE searches SET max_budget = 2500 WHERE id = 3"
sqlite3 price_monitor.db "UPDATE searches SET enabled = 0 WHERE name = 'PlayStation 5'"
sqlite3 price_monitor.db "UPDATE listings SET excluded_at = CURRENT_TIMESTAMP WHERE id = 42"   # exclude an ad
sqlite3 price_monitor.db "SELECT id, title, price_ron, url FROM listings WHERE search_id = 3 ORDER BY first_seen DESC LIMIT 20"
```

(With MySQL, the same statements in its client.) Changes apply on the next run; no restart needed.

A search's **first run** saves what's there as a baseline, without an email.
After that, each run reports listings it hasn't seen and drops of at least
`PRICE_DROP_MIN_PCT` (compared in the same currency).

### What a search filters

- **Budget:** `min_budget` / `max_budget` in the search's `currency` (RON by default; a car's in EUR stays
  one as the rate moves), the range you'd buy in. Other currencies are converted at the National Bank of
  Romania's daily rates (BNR, `curs.bnr.ro`): fetched at most every 6 hours and kept in `settings`, so
  a failed fetch keeps the last ones; a new set also recalculates every stored listing's `price_ron`.
  No real rates are built in or set by hand: without a BNR rate (before the first fetch ever, or a
  currency BNR doesn't list) a placeholder of 5 lei is used, logged, and corrected by the next fetch. Ads outside
  the budget are still stored (for the price estimate, [docs/price-estimate.md](docs/price-estimate.md))
  but never emailed; the dashboard lists only the ones in the budget. To see the whole market,
  leave the price out of the search's link (OLX's price filter, eBay's `_udlo`/`_udhi`) and set it
  here instead: the platform never returns what its link filters out. Widening the budget later
  doesn't email the ads already stored.
- **Prices:** `min_price` / `max_price` in the search's `currency`, broader than the budget: what a real one of the
  product can cost. Ads outside them (games, controllers, cases at 100 lei; bundles far above) aren't
  stored, so they stay out of the price estimate too, unless `store_outside_prices` is 1 (default 0).
  `NULL`: no bound. (Before v15, `min_price`/`max_price` were the budget; the upgrade renames them.)
- **Must contain** (`include_keywords`): the title has to contain at least one of
  these. Useful on Vinted, whose search also returns loosely related items.
- **Leave out** (`exclude_keywords`): titles containing any of these are skipped.
- **Age** (`max_age_days`, default 30): older ads are stored (for the price estimate and the
  dashboard's age filter) but never emailed, and get item pages only after fresh ones. OLX gives the
  posting date. Vinted doesn't, so it's estimated from the item id (ids only
  grow, about 15 million a day); close for days and weeks.
- **Posted before the search began**: an ad first found after its search's first run, yet posted
  before it (the platform started showing more results, an old ad bumped up), is stored but not
  emailed: it's new to the monitor, not to the platform.
- Ads without a price (swap-only) are skipped.
- Each emailed listing shows when it was posted, in Romanian time (`TIMEZONE` in `.env`, default
  `Europe/Bucharest`).

### Possible scams

Every listing gets a risk level (`low`, `medium`, `high`) from warning signs
([`risk.py`](price_monitor/risk.py)), each worth points. The email shows the level with its signs in
yellow (*"Slight risk"*), orange (*"Possible scam"*) or red (*"Likely scam"*), one per line.
Nothing is hidden: a flag means "look closer, and pay only through the platform".

| Sign | Points | Where it comes from |
|---|---|---|
| Price 35% or more below the search's typical price (the median of its current matches, 5 or more) | 2 (3 at half or less) | Both |
| Seller account under 30 days old (under 7 days: 3) | 2 | OLX |
| Seller rated under 4/5 (3 reviews or more) | 1 | Vinted (item page), eBay, OLX (its ratings service) |
| 2 or more negative reviews, a tenth of them or more (OLX: 1-2 stars) | 1 | Vinted, OLX |
| Asks for payment in advance; sends a payment link or asks for a card code | 3 each | Title and description, in Romanian, English, Polish and Greek |
| Asks for a money transfer (IBAN, Revolut, crypto, gift cards); only ships, no meeting; says they're abroad | 2 each | The same |
| Pushes for a quick sale: "urgent", "doar azi" (today only), "primul venit" (first come), "plec din țară" | 2 | The same |
| Delivers only by its own courier, outside the platform ("livrarea doar prin Fan Courier") | 1 | The same |
| A phone number or email in the ad (to talk off the platform) | 2 | Vinted |
| One photo or none | 1 | Both |
| No buyer protection (the ad doesn't offer the platform's delivery), when there's another sign and the ad doesn't offer to meet ("predare personală", "ridicare locală") | 1 | OLX |

Every reason found in the ad's text quotes the seller's own sentence, as written (diacritics kept, a long
one shortened around the phrase), e.g. *offers to meet in person ("Ridicarea locală este posibilă.")*.

1 point is `low`, 2-3 `medium`, 4 or more `high`. Each kind of phrase counts once; an ad offering to meet
can't be "only ships". Only these derived facts are stored, never who the seller is.

**Good signs** show in a green box between the notes and the warning signs (`risk_good`, with
`good_level` and `good_score`), its heading by level: 1 point *"Some good signs"*, 2-3 *"Good signs"*, 4 or more
*"Very good signs"*. Its green fades as the risk level rises (the faintest on a likely scam). They add no
risk points; the dashboard's deal score counts them.

| Good sign | Points | Where it comes from |
|---|---|---|
| Seller account 2 years old or more (5: 2 points, 10: 3) | 1 | OLX |
| Seller rated 4.5/5 or better, with 5 reviews or more (20 or more: 2 points) | 1 | Vinted, eBay, OLX |
| Buyer protection offered (the platform's delivery) | 1 | OLX |
| Offers to meet in person ("predare personală", "ridicare locală"), quoted | 1 | Title and description |

**Notes** are worth knowing but add no points, and are shown on their own, before the warning signs
(`risk_notes`, a grey *"ⓘ Notes"* box): a seller with no reviews yet, a messaging app in the ad (WhatsApp,
Telegram, Viber, also "wapp" and wa.me / t.me links), and the same photo on other ads of the platform (a
copied photo, or the ad posted again; a photo on more than 10 ads is a placeholder and doesn't count).

The monitor also notes, without points, whether an ad says its price is **negotiable** ("negociabil",
"accept oferte", "preț discutabil") or **fixed** ("preț fix", "nu negociez", or a price then "fix": "Preț 3000 fix"; fixed wins), in
`risk_signals.negotiable` / `fixed_price`. The dashboard uses them, with the price history, for its
negotiation chance and deal score.

A listing marked suspicious in the dashboard (`listings.marked_suspicious_at`) shows as a likely scam whatever its
signs, and its price drops aren't emailed.

OLX's seller ratings (what an ad's page shows, e.g. *"5.0 / 5, 18 ratinguri"*) come from its
public ratings service by the seller's id, looked up once per listing (at most 40 per search per
run, those in the budget first) and kept on it; a seller is looked up once per run. Most OLX
sellers have none: OLX only collects ratings after a purchase with OLX delivery.

Vinted shows the description and the seller's reviews only on the item's page,
so the monitor reads it for listings it hasn't stored yet that pass the
filters (at most 15 per search per run, 1.5 s apart). That page also says when
the item was uploaded, which tightens the age check.

## Platforms

| `platform` | Link to paste as `query_url` | Notes |
|---|---|---|
| `olx` | An OLX.ro results page with your filters set | Uses the JSON endpoint the site loads results from. The category is detected from the page once; set `{"category_id": N}` in `options` to fix it. A text search in the link's path (`/caini/q-samoyed/`) is searched for too. Some filters' values differ from the link's path (the BMW 3 Series is `filter_enum_model` `3-as-sorozat`, not `seria-3`): copy them from a results page with the filter ticked |
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
and the dashboard. The budget and prices compare that total in lei, at BNR's rates.

`facebook`: a Marketplace search with your filters set
(`https://www.facebook.com/marketplace/bucharest/search/?query=...&minPrice=...&daysSinceListed=30`).
It reads the results embedded in the page: one page per run (about 20 listings), sold and pending
ones skipped, posting dates exact, the seller's name not kept, and at most every 30 minutes.
Facebook shows them to logged-out visitors only from some connections (a home one, not the
server), so on the server it uses a logged-in session: put a browser's facebook.com cookies in
`.env.production` as `FACEBOOK_COOKIES="c_user=...; xs=...; datr=..."` (ideally a spare account's:
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
./deploy.sh --export   # save the server's searches to searches.json here (see Backing up the searches)
ssh biolete.ro 'cd apps/price-monitor && .venv/bin/python -m price_monitor --status'
ssh biolete.ro tail -f logs/cron-price-monitor.log
```

- The server copy lives in `~/apps/price-monitor` and runs on Python 3.11 from
  `/opt/alt/python311`. It stores its data in the MySQL database `bioleter_price_monitor`.
- The MySQL login and the cron schedule are in `.env.production` (local, not committed),
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
itself (`settings.schema_version`, now 24). Other apps may read everything and
write only the columns marked ✎. All times are UTC `DATETIME`s. JSON columns hold
text.

**`searches`**: one row per watched search.

| Column | |
|---|---|
| `id`, `created_at`, `updated_at` | `updated_at` is set by whoever edits the row |
| `platform` ✎ | A provider's `name` (`olx`, `vinted`); unknown ones fail with a clear `last_error` |
| `name` ✎, `query_url` ✎ | What it's called; the results page to watch |
| `currency` ✎ | What its budget and prices are in (`RON` by default; e.g. `EUR` for a car): converted to lei at BNR's rates on every check |
| `min_budget` ✎, `max_budget` ✎ | The range to buy in (emailed, listed); in its `currency`, `NULL` for no bound |
| `min_price` ✎, `max_price` ✎ | What a real one costs, broader than the budget: ads outside aren't stored; in its `currency`, `NULL` for no bound |
| `store_outside_prices` ✎ | 1: store ads outside `min_price`/`max_price` too (default 0) |
| `include_keywords` ✎, `exclude_keywords` ✎ | JSON lists of strings |
| `options` ✎ | JSON object of platform-specific settings |
| `enabled` ✎, `interval_minutes` ✎, `max_age_days` ✎ | `NULL` interval or age: the defaults in `settings` |
| `notify_new` ✎, `notify_price_drops` ✎ | What to email about |
| `show_listings` ✎ | Whether the dashboard lists its listings (default 1); 0 still runs and stores them, for the price estimate |
| `sort_order` ✎ | Lower first: the order searches run and appear in the email (default 100) |
| `run_requested_at` ✎ | Set to now for "Run now": runs at the next check, even if disabled |
| `baseline_at` ✎ | `NULL` makes the next run save a fresh baseline without an email |
| `provider_state` | The monitor's own (e.g. a detected OLX category) |
| `last_run_at`, `last_status` (`ok`/`empty`/`error`), `last_error`, `last_result_count`, `last_match_count` | Written after every run |

Other apps may also **delete** `listings` rows (with their `price_history` and `status_history`, as the dashboard's
Listings page does). Nothing else remembers a listing, so the monitor's next run finds it again,
and reports it as new, if its search still matches the ad.

**`listings`**: what each search found and still matches its keywords, **also outside its
budget**: show a search's listings with `price_ron` between its `min_budget` and `max_budget`, to
match the email. Unique on `(search_id, external_id)`. Its text is stored plain (HTML entities decoded,
tags, control and invisible characters removed, [`clean.py`](price_monitor/clean.py)) and its
links are `http(s)` only (an ad without one is skipped); still escape it when showing it.
Its columns: `search_id`, `platform`, `external_id`, `title`, `url`, `image_url`,
`price`, `currency`, `price_ron` (the price in lei the range compares; eBay's total), `location`, `posted_at` (given or estimated),
`first_seen`, `last_seen`, and the scam flags: `risk_level` (`low`/`medium`/`high`
or `NULL`), `risk_score`, `risk_reasons` (JSON list of sentences), `risk_points` (each one's points, a JSON list in the same order; `NULL`
for none, or until `--rescore` for listings stored before v24) and
`risk_signals` (the derived facts they came from), `risk_notes` (JSON list of reasons worth a
mention even when the listing isn't flagged, e.g. "seller has no reviews yet"; show them when not
showing the flag; `NULL`: none), `risk_good` (JSON list of good signs: an old seller account, a
well-rated seller, buyer protection, an offer to meet; `NULL`: none), `good_level` (`low`/`medium`/`high` or
`NULL`) and `good_score` (their points), `price_detail` (how `price` adds up, for
eBay's totals), `description` (the ad's description as the platform gives it, plain text keeping its
line breaks, up to 10,000 characters; `NULL` when the platform gives none, e.g. Facebook, or for ads
stored before v20 until a run reads it again: OLX, Publi24 and eBay at their next run; Vinted reads an
item's page once, so its ads stored before v20 keep none; `--rescore` checks its phrases again), and `pin_order` ✎: pinned in the dashboard, in this order (lower first; `NULL`: not
pinned), `marked_suspicious_at` ✎: when it was marked suspicious in the dashboard (shown as a likely scam; its
price drops aren't emailed; `NULL`: not marked), and `excluded_at` ✎: when it was excluded in the dashboard (left out of price estimates, listed
last; `NULL`: not excluded). The monitor never deletes a pinned or excluded listing, and
`--resend-all` keeps them. An ad posted again under a new id with the same photo as an excluded ad on
its platform (bots repost bad ads this way; Vinted keeps the photo's address) is stored excluded and
not emailed, so excluding one copy bans the rest; un-excluding a copy in the dashboard keeps it.
`duplicate_of`: the earlier ad a new one repeats (`NULL`: none), set when it's stored (unless
`settings.duplicate_detection` is 0): in the same search, posted within `settings.duplicate_within_minutes` (30) of it, by the same seller (a similar title, or the same price: a price tried again
counts too) or with the same photo (whoever posted it, e.g. one seller with two accounts). Sellers post
the same item again to show up first among the newest; a duplicate is stored but never emailed, and
counts once in a dashboard's analysis. `seller_key`: what tells the same seller, a one-way fingerprint
(HMAC-SHA256 of platform and the platform's seller id, keyed with `settings.seller_salt`, made at random
on first use); the id itself is never stored. OLX and Vinted give one; `NULL` elsewhere and for ads
stored before v22.
`inactive_at`: when the ad was found sold or removed (`NULL`: active), and `checked_at`: when it
was last checked. After each run that returns results, the monitor opens the pages of up to 10 stored
ads the run didn't show (they fell off the results or no longer match), each at most every 12 hours:
a missing ad (404, or a redirect away from it) or one its page says is closed gets `inactive_at`; OLX
pages also update the price. An inactive ad that shows up in the results again is active again.

**`price_history`**: `listing_id`, `price`, `currency`, `seen_at`, `reason`. One row when a
listing is first seen (`seen_at` = its `first_seen`), and one for every change. `reason` is `rate` when
only the platform's currency conversion moved the price: Vinted shows a foreign seller's price (in PLN,
EUR, HUF) converted to lei at its daily rate, so it moves a little with the rate. A change under 3% on
such a platform (`converts_currency`), for a seller whose own currency (read from the item page) isn't
the one shown, is marked so; it isn't emailed as a drop, and a dashboard shouldn't treat it as an update.
`NULL`: the seller changed it.

**`status_history`**: `listing_id`, `status`, `seen_at`. A row when the ad was found sold or
removed (`inactive`), when it showed up in the results again after that (`active`), and when its
page showed it uploaded again (`reposted`, at that time: Vinted, whose item pages say when an ad
was last uploaded; the monitor re-reads stored Vinted ads' pages at most every 12 hours for it). A
repost: shown as uploaded within the last week, at least a day and a quarter of its age after it
was posted. Not a warning sign; `posted_at` stays the original.

**`settings`**: a single row, `id = 1`.

| Column | |
|---|---|
| `enabled` ✎ | `0` stops every search |
| `default_interval_minutes` ✎, `default_max_age_days` ✎ | Defaults 10 and 30 |
| `email_to` ✎ | Overrides `EMAIL_TO`; `NULL` to use it |
| `run_requested_at` ✎ | "Run all now": the monitor turns it into a request on every enabled search |
| `last_tick_at` | The last check (cron alive?) |
| `providers` | JSON list of `{name, label, seller_info, runs}`: every platform module, what each shows about a seller (`reviews`, `account_age`), and whether the copy writing here runs it |
| `exchange_rates`, `exchange_rates_at` | The rates to lei in use, JSON `{date, source: "BNR", rates: {EUR: 5.34, ...}}`, and when they were fetched |
| `seller_salt` | The key `listings.seller_key` is made with (random, made once); keep it private |
| `duplicate_detection` ✎ | `1` (default): a new ad repeating one stored (`listings.duplicate_of`) is marked and not emailed; `0`: every ad counts |
| `duplicate_within_minutes` ✎ | How close in time a repeat counts as a duplicate, in minutes (default 30) |
| `prices_in_ron` ✎ | The dashboard's table shows prices in lei (the ad's own under them); `0`: as the ad shows them. Default `1` |
| `merge_emails` ✎ | `1` (default): one email for all products a check found something for; `0`: one per product |
| `email_max_risk` ✎ | The highest risk level emailed: `none` (only listings without one), `low`, `medium` or `high` (all, the default). Listings above it are stored and shown, not emailed |

Deleting a search deletes its listings and their history (foreign keys cascade).
