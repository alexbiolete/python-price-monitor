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

**Findings, 2026-11-25** (checked from the server):

- **Official list prices:** Microsoft's store catalog gives every Xbox's price as data
  (`displaycatalog.mp.microsoft.com/v7.0/products?bigIds=8WJ714N3RBTL&market=DE&languages=de-de`: Series X
  €799.99 in Germany; no Romanian market). PlayStation Direct (Germany) has the PS5 Pro (€899.99) in its page,
  not the Slim. List prices change a few times a year, so typing them in once per product would also do.
- **Pricy** (`pricy.ro`) product pages have every Romanian shop's price in their structured data (Series X:
  3 shops, 6,499.90 to 6,799 lei); its robots.txt allows product pages but not its search, so it needs one
  link per product.
- **eMAG and Compari** refuse the server (511 and 403).
- **Showing it:** "New: €799.99 (≈ 4,270 lei)" next to the price estimate, the suggestion as a share of new,
  a "New" line on the charts, and "% of new" in a listing's price card.
