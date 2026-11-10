# Warehouse → PostgreSQL synchroniser

ეს პროექტი მხოლოდ კითხულობს Garage-ის მარაგების endpoint-ს და წერს ახალ PostgreSQL `warehouse` schema-ში. ძველი PHP აპლიკაციის კოდი **არ იცვლება** და სინქრონიზატორს წყაროში write capability არ აქვს.

მონაცემი ინახება მხოლოდ ტექსტური, სტრუქტურირებული `source_columns` JSONB ველით. HTML ფრაგმენტი (`source_row_html`) აღარ იწერება და არ რენდერდება. ცვლილებაზე იქმნება ვერსია; ყველა ახალი ვერსია ხელმოწერილია HMAC-SHA-256-ით, რომლის გასაღები PostgreSQL-ში არ არსებობს.

ეს არის დაცული deployment baseline და არა „100% უსაფრთხოების“ დაპირება. Production release საჭიროებს [ASVS Level 2 evidence matrix](docs/asvs-l2-evidence.md), დამოუკიდებელ authenticated security review-ს და იზოლირებულ restore drill-ს.

## ლოკალური განვითარება

საჭიროა Python 3.12 (`.python-version`), Docker Compose და PostgreSQL client მხოლოდ თუ migration-ის ხელით შემოწმება გინდა.

Python 3.14 გარემოდან გადასასვლელად გაუშვი `uv sync --python 3.12 --extra dev --locked`.
Python 3.12-ს აქვს locked `lxml` dependency-ის მზა wheel და არ სჭირდება მისი source-დან აგება.

```bash
cp .env.example .env
# .env-ში ჩაწერე მხოლოდ local WAREHOUSE_COOKIE და WAREHOUSE_HMAC_KEY
docker compose up -d postgres
uv sync --extra dev --locked
uv run python -m warehouse_sync --migrate
uv run python -m warehouse_sync --dry-run
uv run python -m warehouse_sync --max-pages 1 --verbose
```

`compose.yaml` მხოლოდ `127.0.0.1:55432`-ს ხსნის; ის production artifact არ არის. `.env` gitignored-ია და გამოიყენება მხოლოდ development-ში. Production რეჟიმი `.env`-ს საერთოდ არ კითხულობს.

პირველი სრული ლოკალური იმპორტი:

```bash
uv run python -m warehouse_sync --verbose
```

სრული გაშვება ჯერ ყველა გვერდს ინახავს, შემდეგ კი წყაროს გვერდებს ხელახლა
ამოწმებს; ამიტომ მხოლოდ პირველ გავლაზე დაახლოებით ორჯერ მეტი source request
იგზავნება. დროებითი ქსელის/DB შეცდომისას შეზღუდული რაოდენობით ხდება retry;
დაკავშირების დაკარგვის შემდეგ scan თავიდან იწყება, ხოლო უკვე ჩაწერილი გვერდები
რჩება ბაზაში. ცარიელი/არასწორი გვერდი, სხვადასხვა გვერდზე გამეორებული ID ან
მეორე შემოწმებისას შეცვლილი მონაცემი აჩერებს ან თავიდან იწყებს scan-ს.

წყაროს snapshot API არ აქვს, ამიტომ ორი თანმიმდევრული შემოწმებაც ვერ ამტკიცებს
100%-იან სისრულეს. ავტომატური product deactivation გამორთულია; სრული scan-ის
შემდეგაც DB run-ის სტატუსია `partial`, ხოლო terminal აჩვენებს verification-ის
შედეგს. დაკარგული products დამოუკიდებლად გადაამოწმე წყაროსთან.

სინქრონიზაციისთვის HMAC გასაღები სავალდებულოა. ლოკალურად მისი გენერირება შესაძლებელია ასე:

```bash
python -c "import base64,secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())"
```

## უსაფრთხოების მოდელი

- Production მხოლოდ `WAREHOUSE_APP_ENV=production`-ით იწყება და უარყოფს HTTP წყაროს, browser cookie-ს, ცარიელ credential-ს, დაუშვებელ source/database host-სა და `sslmode=verify-full`-ის გარეშე DB კავშირს.
- წყაროსთვის გამოიყენება ცალკე read-only integration user; password, DB URL და HMAC key მოდის მხოლოდ root-owned `systemd` encrypted credential file-ებიდან.
- Worker იყენებს მხოლოდ `warehouse_sync_writer` როლს. მას არ აქვს table `SELECT`, `DELETE`, DDL ან სხვა application schema-ის უფლება; ის იძახებს ვიწრო `SECURITY DEFINER` database ფუნქციებს.
- `warehouse_backup_restore_operator`-ს აქვს მხოლოდ backup/restore-თვის საჭირო წაკითხვა და დაცული, 10,000-record-იანი 24-თვიანი history-retention ფუნქცია. Migration owner ცალკეა.
- sync failure-ში DB ინახავს მხოლოდ safe failure code-ს; CLI, ჟურნალსა და email alert-ში არ გადადის URL, cookie ან driver error text.

## ტესტები

```bash
uv run python -m unittest discover -s tests -v
uv run ruff check .
uv run bandit -q -r warehouse_sync
uv run pip-audit
```

მოკ-წყაროს და PostgreSQL privilege integration tests CI-ში ეშვება disposable service-ით. ლოკალურ sandbox-ში, სადაც loopback listener აკრძალულია, network mock test მიზანმიმართულად skip-დება.

## Production

Production გარემოს ხელით გაშვება არ არის საკმარისი. მიჰყევი [production runbook-ს](docs/production-runbook.md): Supabase-ის Frankfurt პროექტი, IP allowlist, WireGuard, TLS proxy, private Data API schema, systemd sandbox, encrypted off-host backups, alerting და GitHub environment approval არის release gate-ები.

`ops/` შეიცავს დასაყენებელ შაბლონებს და არა აქტიურ cloud ცვლილებებს. Placeholder-ები უნდა ჩანაცვლდეს რეალური მნიშვნელობებით უსაფრთხო deployment პროცესში; მათით გაშვებული systemd network sandbox განზრახ ვერ დაუკავშირდება ქსელს.

## Local database explorer

Start the database and the explorer together:

```bash
cp -n .env.example .env  # only needed if you don't already have .env
docker compose up --build -d postgres db-ui
```

Open [http://localhost:8766](http://localhost:8766). The explorer connects to the Compose PostgreSQL database using the `POSTGRES_*` settings in `.env`. If the database is new, apply the migrations using the local development instructions above; the UI does not create tables or import data. Stop the services with `docker compose stop db-ui postgres`. To change the host port, set `DB_UI_HOST_PORT` in `.env` or run, for example, `DB_UI_HOST_PORT=8767 docker compose up -d db-ui`.

Choose a table to browse its rows, inspect an individual row, or see its columns and descriptions. The SQL editor suggests PostgreSQL keywords and visible schema, table, and column names; use Tab to accept a suggestion and Ctrl+Enter (or Cmd+Enter) to run a statement. Query results show 200 rows per page, and table browsing shows 50 rows per page. Use the pagination controls to navigate and export all returned rows as Excel, CSV, or JSON. SQL limits still apply to the output; queries are rerun for each page and export, so use ORDER BY for consistent paging and expect changes if data is updated. JSON exports contain column names and row arrays, preserving duplicate column names. CSV exports prefix formula-like values with an apostrophe for spreadsheet safety. Table and column descriptions come from PostgreSQL comments when available, with built-in explanations for the warehouse schema.

You can also run the explorer without Docker with `uv run python -m db_ui`. It reads `DB_UI_DATABASE_URL`, or falls back to `DATABASE_URL` from `.env`. Both ways bind the host-facing service to localhost. The editor executes a single statement in a read-only transaction with a 15-second timeout; for a database beyond local development, use a dedicated least-privilege read-only database role. Read-only transactions are not a substitute for restricting function permissions, and the Compose database user is intended for local development only. The existing sync writer role intentionally has no table SELECT access and cannot be used to browse data.

### Analytical star schema

For the inventory and price-observation star schema, incremental triggers,
initial backfill, example queries, and important data assumptions, see
[Warehouse analytics](docs/analytics.md).

### Sales and purchasing

The sync runs at 09:00 and 21:00 Tbilisi time. Between two syncs, a drop in a product's
store + TUZ 11 stock is recorded in `trade.sales` and a rise in `trade.purchases`. See
[Sales and purchasing](docs/sales-purchasing.md) for the rules, limits and example queries.

## Automotive intelligence

A separate Georgian-first application now reads the existing catalog, identifies vehicles
with vPIC, and evaluates imported evidence for fitments and oil eligibility. Run
`uv run python -m automotive` after configuring its independent credentials and applying
migration `0006` with the migration-owner connection. Chat uses the server-side Anthropic
SDK; technical facts always come from backend records.

For an existing local warehouse, `uv run python -m automotive --setup-local` creates
the independent database credentials and missing local secrets in `.env`, without
changing warehouse products. Then restart the application. VIN decoding can work
without its database cache; inventory needs the database, and chat needs your own
`ANTHROPIC_API_KEY`. The interface reports missing configuration.

The chat reads like a messenger: it understands small talk, cars typed in words and short
answers, and replies with the car, how many parts were found and where they are. Measure
it with `uv run python -m automotive --evaluate-chat mock` (offline; also run as a test) or
`--evaluate-chat live --vins FILE --judge` against the configured catalog and providers.

See [automotive setup and evidence rules](docs/automotive.md) and
[implementation acceptance evidence](IMPLEMENTATION_PROGRESS.md). Live database/provider
verification is still required before operational use.

### Garage 1886 site

The same application serves the garage's public website at `/` (Georgian) and `/en`, a
staff sign-in at `/login` (never linked from the site) and a dashboard at `/admin` with two
roles, `employee` and `admin`. The staff workspace above moved to `/staff` and, like
`/api/v1`, needs a staff session.

```bash
uv run python -m automotive.site_setup --setup-site
uv run python -m automotive.site_setup --create-user NAME --role admin
uv run python -m automotive
```

Routes, roles, the security model, settings and what is still required before the site is
reachable from the internet are in [docs/garage-site.md](docs/garage-site.md).

The **VIN-ით ნაწილის კოდები · Motorium** panel and
`POST /api/v1/vehicles/part-codes` wrap the observed public Motorium assistant and product
metadata. No local Anthropic key is needed for this source. Results stay provisional,
with separate MPN, supplier SKU and OEM-labeled codes. See the
[Motorium protocol and live acceptance notes](docs/integrations/motorium.md).
