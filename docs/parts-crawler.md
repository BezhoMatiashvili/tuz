# Parts catalog crawler

`parts_crawler` copies public vehicle and parts data into the private `parts_catalog`
schema so parts can be found by code, the vehicles that share a part can be listed, and
alternative parts can be offered. Every row is a source assertion, never reviewed
`VERIFIED` evidence.

## Sources

| Source | How it is read | What it gives |
|---|---|---|
| MANN-FILTER API (`mann_api`) | The JSON endpoint the site's own pages call, `GET /api/graphql/catalog-prod` | All 12,152 filters with OE and competitor numbers (about 82 requests), then vehicle types with engine code, capacity, power and the filters that fit. **robots.txt disallows this path**; see *Robots exemption*. |
| MANN-FILTER pages (`mann`) | `en.sitemap-commerce.xml`, then product pages | OE numbers and main applications from JSON-LD. Superseded by `mann_api`; kept as the fallback that stays inside robots.txt. |
| CatCar (`catcar`) | Catalog steps per brand (see *CatCar*) | Vehicle taxonomy with frame, model and engine codes for Toyota/Lexus, Nissan/Infiniti, Mitsubishi, Subaru, Honda, Hyundai, Kia, Mazda (USA), BMW/Mini, Mercedes/Smart, Audi/VW/Seat/Skoda. OEM part numbers for vehicles you seed. 10 s between requests. |
| NGK UK part finder (`ngk`) | product type → make → model → year | Engine, dates, NGK part number and stock number. The site has no JSON API. |
| RockAuto (`rockauto`) | Catalog tree pages | **Paused.** The site dropped this network address after about 620 pages. Nothing tries to get around that. |
| RealOEM (`realoem`), Partsouq (`partsouq`) | `import-har` only | Cloudflare challenges automated clients, and RealOEM's robots.txt excludes AI crawlers. Import only pages you browsed yourself. Left out of `--source all`. |
| NHTSA vPIC (`import-vpic`) | Official PostgreSQL dump → schema `vpic` | Offline VIN decoding (`SELECT * FROM vpic.spVinDecode('...')`), plus `parts_catalog.wmi` (14,191 VIN prefixes → make, manufacturer, country) and road-vehicle make/model pairs. |
| fueleconomy.gov (`import-fueleconomy`) | One public zip, `vehicles.csv.zip` | 50,647 US configurations 1984 onward with displacement, cylinders, gearbox, drive and fuel. |
| Wikidata (`import-wikidata`) | Public SPARQL endpoint, paged | About 13,500 car models and their makers with names in English, Georgian and Russian → `parts_catalog.vehicle_names`. |
| Year/make/model CSV (`import-ymm`) | `rokasmen/car-audio-fitment-api` `data/vehicles.csv` | 64,820 rows, MIT licence. Vehicle list only. |
| FAPI format (`import-fapi`) | `manufacturer_list`, `product_list`, `analog_list` | For the purchasable full cross-reference set only. The GitHub sample is licensed for evaluation and has been removed from the database (`purge-source --source fapi`). |

Not sources: 7zap (its terms forbid bots, manual extraction and building databases; its
data is sold through the Levam API), TecDoc and Auto Care (licensed products).

## Setup

```bash
docker compose up -d postgres
uv run python -m parts_crawler setup-local   # migrations through 0011 + a crawler-only login
```

`setup-local` accepts only a loopback `DATABASE_URL`, applies the role provisioning
script and every migration through `0011_vehicle_detail`, and writes
`PARTS_CRAWLER_DATABASE_URL` to `.env` (mode 0600) without printing it. The login has the
`parts_crawler` capability only: full access to `parts_catalog`, read access to
`automotive.part_numbers` and no access to `warehouse` tables. The automotive runtime
role reads `sources`, `parts`, `vehicles`, `part_fitments`, `part_cross_refs`,
`vehicle_names` and `wmi`.

Optional settings (`.env`): `PARTS_CRAWLER_CONTACT` (added to the User-Agent),
`PARTS_CRAWLER_MIN_DELAY_SECONDS` (default 5, minimum 2), `PARTS_CRAWLER_JITTER_SECONDS`,
`PARTS_CRAWLER_MAX_ATTEMPTS` (default 6), `PARTS_CRAWLER_DATA_DIR` (default `data/crawl`),
`PARTS_CRAWLER_ROBOTS_EXEMPT` (empty by default).

## Running

```bash
uv run python -m parts_crawler run                          # every automatic source
uv run python -m parts_crawler run --source mann_api,ngk,catcar
uv run python -m parts_crawler run --max-pages 50           # short test run
uv run python -m parts_crawler run --plain                  # one line per source every 30 s
uv run python -m parts_crawler status                       # totals, progress, checkpoints
```

On a terminal `run` shows a live table: per source a progress bar and percentage, pending
and finished pages, pages per minute, stored parts, vehicles, fitments and cross
references, an ETA and the last error with its age. Progress is measured against the
pages found so far; catalog trees grow while they are explored, so the percentage can
fall before it rises. Importers show a bar with rows per second and time remaining.

### Log

Everything goes to `data/crawl/crawl.log` (rotated at 10 MB, five files): every request
with status, duration and size; each source starting and stopping with its reason; a
progress line per source or importer once a minute; warnings and tracebacks. Follow a
background run with `tail -f data/crawl/crawl.log`. Warnings also appear on the terminal
above the progress display.

### Stopping and continuing

Stop with **Ctrl+C** or `kill <pid>`. The crawl finishes the page in flight, releases
its leases, records the run and exits; an importer rolls back its current batch. Run the
same command again and it continues:

- **Crawl.** The queue lives in PostgreSQL. Each page's records, its child links and its
  queue state commit in one transaction. If the process dies without warning (power
  loss, `kill -9`), the next run takes a per-source lock and immediately re-queues the
  pages the dead run had leased. A page that crashes the crawler every time runs out
  of attempts and is marked failed instead of blocking the source.
- **One process per source.** A second `run` on a source that is already being crawled
  does not start it (`already running in another process`), so the request rate to a
  site can never double.
- **Importers.** Each batch commits together with a checkpoint
  (`parts_catalog.import_checkpoints`); `status` lists them. A rerun skips what is
  already loaded. `--restart` ignores the checkpoint.
- **Downloads.** An interrupted download keeps its `.part` file and continues from
  there when the server supports ranges.
- **Bad records.** A record the database rejects fails that one page
  (`STORE_ERROR`), not the source; the raw page stays indexed for `reparse`.
- **robots.txt outages.** If a site's robots.txt cannot be read, the source waits and
  retries; queued URLs are not marked as disallowed.

A second Ctrl+C forces an immediate exit; nothing is lost, the page in flight is simply
fetched again.

Other commands:

| Command | Effect |
|---|---|
| `seed --source X [--codes N]` | Queue the start pages again (duplicates are ignored) |
| `seed-vehicles --source catcar …` | Queue part groups and part numbers for stored vehicles (see *CatCar*) |
| `requeue-failed --source X [--include-skipped]` | Give failed (and optionally skipped) URLs a fresh set of attempts |
| `pause --source X` / `resume --source X` | Stop or restart a source; `resume` also clears `blocked` |
| `reparse --source X` | Rerun the parser over stored pages without any network access |
| `reset-queue --source X [--yes]` | Show, and with `--yes` forget, one source's queue; stored data and raw pages stay |
| `purge-source --source X [--yes]` | Show, and with `--yes` delete, everything one source stored |
| `import-har --source X --har file.har` | Parse pages from a HAR recorded in a normal browser |

## CatCar

All CatCar catalogs share one URL scheme (`/<brand>/?l=<base64 state>`), but each brand
has its own sequence of steps. The adapter follows links to a higher step, tabs and
pagination, and never the breadcrumb links back up (those made the first version wander
without reaching a part). Each brand has a profile in `parts_crawler/sources/catcar.py`
and a parser test against a trimmed real page.

A normal run crawls taxonomy only: markets, models and modifications, stored as vehicles
with model code, engine code and production years. Part groups are **not** opened,
because one vehicle costs hundreds to thousands of requests at 10 seconds each.

To collect part numbers, seed the vehicles you care about:

```bash
uv run python -m parts_crawler seed-vehicles --make TOYOTA --model COROLLA --code NDE180 --dry-run
uv run python -m parts_crawler seed-vehicles --make TOYOTA --model COROLLA --code NDE180 --limit 3
uv run python -m parts_crawler run --source catcar
```

`--groups` is a regular expression on group names. The default, `maintenance`, selects
filters, brakes, plugs, belts, wipers, clutch, bearings and similar wear parts in
English and Russian. `--all-groups` takes everything. Group names are matched as the
catalog prints them, so check them first (Toyota's engine oil filter is
`ФИЛЬТР МАСЛЯНЫЙ`). Seeding a vehicle again with another filter re-reads its group
pages; part-number requests already made are not repeated.

### Every vehicle automatically

```bash
uv run python -m parts_crawler seed-vehicles --all --dry-run   # how many, how long
uv run python -m parts_crawler seed-vehicles --all             # queue what is stored now
uv run python -m parts_crawler run --source catcar --auto-parts   # keep queueing new ones
```

Parts for every stored variant would take years at 10 seconds a request (20,000 Toyota
variants alone are about two years). `--all` therefore queues one vehicle per family,
meaning the same model, frame and engine, and takes the newest of each first. Within the
wear-part groups it requests part numbers only for rows whose name looks like a wear
part (filter, pad, disc, plug, belt, blade, absorber, clutch, bearing, pump,
thermostat); `--parts all` lifts that. `run --auto-parts` repeats the queueing every 30
minutes, so vehicles discovered later are included without another command.

While seeded vehicles are waiting, the CatCar worker takes turns: one part request, one
page that discovers vehicles. Neither waits for the other to finish.

Group and part names come in the language of the catalog data, which is Russian for some
brands. Mazda is read for the USA market only.

## Robots exemption

`mann_api` requests a path that the site's robots.txt disallows for crawlers. It runs
only when the operator sets `PARTS_CRAWLER_ROBOTS_EXEMPT=mann_api`; otherwise the source
reports `not started` and its queue is left untouched. The exemption covers
`/api/graphql/` on that host only. Everything else stays in force: the 5 second pace, the
honest User-Agent, the daily budget, and the stop on any 403, 429 or challenge. `run`
prints the exemption when it starts, `status` lists it, and every run records it in
`parts_catalog.crawl_runs`.

## Safeguards

- **robots.txt** is read per host (24 h cache) with wildcard and longest-match support.
  Rules for `*` apply; `Crawl-delay` raises the per-host delay. The only exception is
  the operator-set exemption above.
- **Pacing**: one worker per host, at least 5 s plus jitter between requests to that
  host. Different sites run in parallel.
- **Retries**: 429, 5xx, 408, 401/403, timeouts and network errors are retried with
  exponential backoff (2 s, 8 s, 32 s, ~2 min, ~8.5 min, then 30 min, ±20%), honouring
  `Retry-After`. After six attempts the URL is `failed`. 404/410 fail at once.
- **Circuit breaker**: ten consecutive failures on a host pause it for 30 minutes; the
  third trip marks the source `paused` until `resume`.
- **Bot protection**: a Cloudflare or captcha challenge marks the source `blocked`.
  There is no challenge solving, browser automation, disguised User-Agent or proxy
  rotation to get around it.
- **Daily budget** per source (`parts_catalog.sources.daily_budget`, default 15,000;
  CatCar 8,000). Change it with SQL as the migration owner.
- **Raw pages** are stored gzip-compressed under `data/crawl/pages/<source>/`
  (git-ignored), so a parser fix only needs `reparse`.

## Recording a HAR for blocked sites

1. Open the site in your normal browser, open DevTools → Network, enable *Preserve log*.
2. Browse the catalog pages you want; each page you open is captured.
3. *Save all as HAR with content*.
4. `uv run python -m parts_crawler import-har --source partsouq --har capture.har`
5. Delete the HAR file. It can contain cookies and tokens; only response bodies of
   successful catalog pages are stored, never request headers.

Check a site's terms before importing from it.

## Bulk imports

```bash
uv run python -m parts_crawler import-vpic            # downloads ~77 MB, restores schema vpic
uv run python -m parts_crawler import-vpic --lookups-only   # only refresh wmi and make/models
uv run python -m parts_crawler import-fueleconomy     # --refresh downloads the file again
uv run python -m parts_crawler import-wikidata        # --languages en,ka,ru
uv run python -m parts_crawler import-ymm
```

`import-vpic` uses the local admin `DATABASE_URL` (loopback only) because it creates the
`vpic` schema. The restore runs in one transaction, so an interrupted restore leaves the
previous schema in place.

## Expected size and duration

| Job | Requests | Time |
|---|---|---|
| `import-fueleconomy`, `import-vpic --lookups-only` | 1 download | under a minute |
| `import-wikidata` | about 70 | about 7 minutes |
| `mann_api` products and cross references | about 82 | about 8 minutes |
| `mann_api` vehicle tree | 100,000 or more | about a week at the daily budget |
| `catcar` taxonomy, all brands | tens of thousands | days to weeks; popular makes first |
| `catcar` parts for one seeded vehicle, maintenance groups | a few hundred | about an hour |

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/parts/catalog?code=...&brand=...&limit=50` | Crawled parts with that code, the vehicles they fit (with model and engine codes) and their cross references |
| GET | `/api/v1/parts/alternatives?code=...&brand=...&limit=50` | Alternative parts with evidence, ranked by our stock |

Everything is `PROVISIONAL`. The same part type does not guarantee interchangeability;
notes such as axle, trim or production dates must be checked.

## Tests

```bash
uv run python -m unittest tests.test_parts_crawler_http tests.test_parts_crawler_sources \
    tests.test_parts_crawler_catalogs tests.test_parts_catalog_api -v
WAREHOUSE_TEST_ADMIN_DATABASE_URL=... uv run python -m unittest tests.test_parts_crawler_queue
```

Parser tests use trimmed real pages in `tests/fixtures/parts_crawler/`. The queue test
creates and drops its own temporary database.
