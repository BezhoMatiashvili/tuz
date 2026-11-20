# Garage 1886: search engines

What the site does for Google and Bing, and what only the owner can do outside it.
Researched on 2026-11-20 from Google in the owner's own browser (Georgian, `gl=ge`).

## What the searches showed

- **The map pack decides local searches.** For `ავტოსერვისი საბურთალო`, `BMW სერვისი
  თბილისი` and `ავტონაწილები თბილისი` Google shows three businesses from Google Maps first,
  with their stars, then directories (yell.ge, 08.ge, mze.ge, 2gis.ge, junto.ge, servio.ge,
  fixup.ge, myparts.ge, biznesebi.ge), then the shops' own sites. A new domain will not
  win the general searches for a long time; the Maps listing and the long-tail parts
  pages can win early.
- **A Google Business Profile already exists:** "ფარეხი 1886 GARAGE", 5.0 from 24
  reviews, category ავტოსახელოსნო, 17 პეტრე ქავთარაძის ქუჩა, 0186. On 2026-11-20 it was
  **not claimed by the owner** and had **no website**.
- **TuzMotors still lists this address and phone as its Saburtalo branch.** Garage 1886
  is that branch renamed (owner's answer, 2026-11-20). tuzmotors.ge's structured data
  (`#saburtalo`) gives Petre Kavtaradze St 17 and +995 596 33 33 74. Google's AI answer
  for `ზეთის შეცვლა საბურთალო` names "TuzMotors, Saburtalo branch, Kavtaradze 17".
- The sites that rank with their own pages (tuzmotors.ge, tegetamotors.ge, autopia.ge)
  have one page per service or category. Ours was one page, and its 8 700 items in stock
  were visible only to the browser script.

## What the site does (automotive/site_seo.py)

| Address | What |
|---|---|
| `/parts`, `/en/parts` | every make with at least 5 items in stock, with counts |
| `/parts/{make}` | that make's items in stock (name, brand, fits, price), 60 a page (`?page=2`), and its models with at least 5 items |
| `/parts/{make}/{model}` | the same for one model, e.g. `/parts/mercedes-benz/w211` |
| `/sitemap.xml` | the home pages and every parts page, each with its other language (`hreflang`) and the stock date as `lastmod` |

- Rendered on the server from the in-memory catalogue (no database query per visitor),
  with the same four fields as the public search. Shop codes, quantities and warehouses
  are never on these pages. A page with too few items, an unknown make or a page number
  past the end is a plain 404; while the catalogue is still loading after a start it is a
  503 with `Retry-After`, so a crawler comes back instead of dropping the page.
- Each page has its own title and description, a canonical address, `hreflang` pairs, a
  `BreadcrumbList`, the shop's phone, address and hours, and a link to the full search.
  Titles in Georgian add the make's Georgian spelling (`Toyota (ტოიოტა) ნაწილები
  თბილისში`); the spellings are `LOCAL_NAMES` and need a native speaker's check.
- The landing page links to the top 18 makes under the part search and to `/parts` from
  the footer, so the pages are reachable by plain links.
- The landing page's structured data (built in `business_data`) is a `WebSite` (site
  name "Garage 1886", also "ფარეხი 1886") and, once a phone or an address is stored, an
  `AutoRepair` + `AutoPartsStore` with the address, city, postal code, coordinates,
  opening hours, map link and social profiles, only as far as the dashboard holds them.
  Google's own star rating is deliberately not repeated there (self-made review markup
  breaks Google's rules); no `Product` markup either, for the same reason.
- Dashboard > Texts > Contact and brand has three new fields for this: **City**,
  **Postal code** and **Coordinates for Google**, and each opening-hours row has **Days
  for Google** (`Mo-Sa`, `Mo-Fr, Su`). A row without it is shown on the page but left out
  of the structured data.
- Dashboard > Texts > Search engines holds the home page's title and description.

On 2026-11-20 the dev database was given: the new title and description (they were the
untouched setup text), City თბილისი / Tbilisi, Postal code 0186 (from the Google profile),
Coordinates 41.75903, 44.71058 (from tuzmotors.ge's own data for this branch) and `Mo-Sa`
on the Monday to Saturday row. The audit log names them `claude-seo`. Check them once in
the dashboard.

Lighthouse (mobile, 2026-11-20, local copy): SEO, accessibility and best practices 100
on `/` and `/parts/toyota`; home page LCP 1.8 s with Slow 4G and 4x CPU, CLS 0.

## The owner's checklist (outside the code)

In order of effect. Nothing here was done for you: each one needs your accounts.

1. **Claim the Google Business Profile.** Search `ფარეხი 1886 GARAGE`, click "ფლობთ ამ
   ბიზნესს?" (Own this business?) and verify. Then:
   - add the website `https://garage1886.ge/` once the site is live;
   - categories: primary "Auto repair shop", add "Auto parts store", "Oil change service",
     "Brake shop", "Car inspection station" only if true;
   - services with short descriptions, the opening hours, photos of the shop (the murals,
     the bays, the parts shelves), and the products tab for oils;
   - make sure the name, address and phone are exactly what the site shows.
2. **Ask happy customers for Google reviews** with the profile's review link (on a card
   at the counter, in the Messenger bot's goodbye). Answer every review. The stars and
   their count are the strongest map-pack signal you control.
3. **Fix the TuzMotors overlap.** On tuzmotors.ge change the Saburtalo branch to say it
   is now Garage 1886 and link to `https://garage1886.ge/` (its JSON-LD `#saburtalo`
   entry too). If a separate "TuzMotors Saburtalo" listing exists on Google Maps, mark it
   as moved or ask Google to merge it, so the same address and phone are not two
   businesses. Point the old Saburtalo service pages there if they only served this
   branch. A link from tuzmotors.ge is the strongest link the new domain can get.
4. **Google Search Console** for `garage1886.ge` (Domain property, DNS TXT record), submit
   `https://garage1886.ge/sitemap.xml`, then watch Pages and Performance. **Bing
   Webmaster Tools** can import it from Search Console in one step.
5. **Directories**, with the same name, address, phone and the website: yell.ge, 08.ge,
   2gis.ge, servio.ge, fixup.ge, myparts.ge (services), biznesebi.ge, junto.ge, Apple
   Maps (Apple Business Connect), Yandex Maps.
6. **Social profiles**: put `https://garage1886.ge/` on the Facebook page and the
   Instagram account (@garage_1886), and enter the Instagram address in the dashboard
   (Contact and brand > Instagram) so it joins the structured data.

## When deploying

- `AUTOMOTIVE_PUBLIC_ORIGINS=https://garage1886.ge`. Canonical links, `hreflang`, the
  sitemap and the structured data all use the first origin listed.
- Make the proxy send `www.garage1886.ge` and plain `http://` to
  `https://garage1886.ge/` with a 301, so Google sees one address per page.
- Check after going live: `https://garage1886.ge/robots.txt` names the sitemap, and
  Google's Rich Results Test on `/` finds the business and on `/parts/toyota` the
  breadcrumbs.

## Not done, and why

- **Service pages** (`/servisebi/...`) were not made: each service has one sentence, and
  a page of one sentence does not rank. With 150 to 300 words per service from the shop
  (what is done, how long, from what price), they are a small addition to this module.
- **A page per part** was not made: the public data has no stable id to give one an
  address, and Google would see thousands of near-identical pages.
- English item names: the catalogue writes most names in Georgian, so `/en/parts` pages
  list Georgian names under English headings. `hreflang` tells Google they are the same
  page in two languages.
