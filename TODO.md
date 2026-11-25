# To do

## Shop prices and stock

*Noted 2026-11-25.*

Also watch shops (Altex, eMAG, Flanco, PC Garage), next to the second-hand platforms:

- A "shop" kind of search that watches **specific product pages** (one link per product and shop, e.g. the
  Altex and eMAG pages for "PS5 Slim Disc"), not search results, which would bring back accessories.
- Checked a few times a day; price and stock stored with history (a shop product is a listing whose price
  changes and that can go out of stock and come back).
- Emails on price drops and **restocks** (e.g. the PS5 Pro: out of stock at Altex at 5,299.90 lei while
  eMAG marketplace sellers ask about 9,000 lei).
- "New in shops: 3,999 lei (eMAG, in stock)" next to the price estimate and in the dashboard's searches
  table, to compare used prices with new ones (and to set a search's max price from it).

**First step:** a feasibility test, fetching one product page per shop from the server, to see which answer
with usable price and stock data.

Things to watch for:

- **Blocking:** eMAG uses bot protection and may refuse the server, as eBay's search pages did. Altex,
  Flanco and PC Garage load prices from their own APIs, which tend to be easier.
- **Marketplace sellers:** on eMAG one product has several sellers at very different prices; prefer
  "sold by eMAG" (or the cheapest trusted seller) over resellers.
- **Terms of use:** shops generally don't allow scraping; keep it to a few requests a day on a handful of
  product pages.
