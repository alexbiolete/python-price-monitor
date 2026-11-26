# Price estimate

A price suggestion for a product, from the listings the monitor already collects. Built in the
codename-dojo dashboard (Price monitor → Analysis; `GET /price-monitor/estimate`), which reads
the stored listings; nothing runs on the monitor's side.

## Decided

- **What counts:** the stored listings of a product (the searches with its name, on every
  platform or one picked; an ad two of them found counts once), also outside their budgets. A command here
  (`python -m price_monitor estimate`) can come later, with the same rules.
- **Keep every ad a search finds, also outside its budget**, with the scam flags, so the
  estimate sees the whole market. Emails and the dashboard still show only the ads inside the
  search's current range.
- **Fair prices:** a warning level, "at most" as on the listings (all, up to possible scams, up
  to warning signs, none); by default up to warning signs, so possible and likely scams are out.
- **Two kinds of prices:**
  - *asking*: active listings;
  - *likely sold*: listings that went inactive (`inactive_at`) after being up for at least a
    minimum number of days. One taken down sooner may be a scam the platform removed, or a
    mistake.
- **The result:** one suggested price, large, with the usual range under it in smaller text
  (e.g. **1.550 lei**, then "most between 1.400 and 1.700 lei"). The number is the median
  of the fair prices; the range is the middle half (25th to 75th percentile). It's the likely sold
  prices once there are 3 of them, else the asking ones; both are shown side by side.
- **Settings:** two optional limits on what counts as sold, each with a checkbox (both on by
  default): went inactive in the last N days (15), and was up at least N days (3). Both off:
  every ad found sold or removed counts.
- **Only what's stored:** inactive ads are only the ones the monitor saved while they were up, so
  the tool says so where you ask for a price: suggestions are best after monitoring the product
  for a while (a couple of weeks) before posting your ad.

## Later

- **Matching the same product:** listings with the same title can still differ in condition,
  storage, bundles or "for parts". Ignored for now: a search's include/exclude keywords are the
  only filter.
