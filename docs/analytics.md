# Warehouse analytics

The `analytics` schema is a Kimball-style star for the **inventory data actually
available**. It contains no sales or revenue facts: a listed price does not mean a
sale took place. Dates are UTC, and no currency is inferred.

| Table | Grain | Meaning |
|---|---|---|
| `dim_product` | One source product | Surrogate key and **Type 1** attributes; historical observations join to the latest product name, brand and other attributes. |
| `dim_date` | One UTC date | Calendar attributes for slicing observations and syncs. |
| `fact_product_current` | One current source product | Latest known price and active state. |
| `fact_product_observation` | One saved source product version | A product's first observation or a change, not every sync or a daily snapshot. |
| `fact_sync_run` | One sync run | Its status, timestamps and counters. |

Source `column_06` is treated as selling price because it occupies the same
position as the selling-price heading in the supplied source screenshot. The
endpoint does not include headers, so **confirm this mapping against the source
before relying on it**. Both price facts retain the exact text in
`selling_price_raw`. `selling_price` is numeric only for plain decimal text
using a dot; blanks, currency symbols, commas, and other ambiguous formats
become NULL rather than a guessed number. Review the following before analysis:

```sql
SELECT selling_price_raw, count(*)
FROM analytics.fact_product_current
WHERE selling_price IS NULL AND selling_price_raw IS NOT NULL
GROUP BY selling_price_raw ORDER BY count(*) DESC;
```

The migration creates transactional triggers on the three source tables. New and
changed products, versions, sync runs, deactivations, and source deletions are
reflected in the star in the same transaction. The sync writer gains no direct
access to analytics. A trigger failure rolls back the source write, so check
refresh behavior and sync throughput with representative data before production.
The source prunes old versions after 24 months, and deleting a source version
also deletes its observation fact. History already pruned before initial
backfill cannot be reconstructed. Type 1 dimensions do not retain past names
or brands; observations record changes, not uninterrupted daily price history.

## Local Docker setup

The Compose database is the existing local development database. Rebuild the UI
image so it includes the migration and maintenance command, then apply pending
migrations and run the initial backfill:

```bash
docker compose up --build -d postgres db-ui
docker compose exec db-ui python -m db_ui.analytics migrate
docker compose exec db-ui python -m db_ui.analytics refresh
```

Open the UI, click refresh beside **DATABASE**, and explore the `analytics`
schema. The Compose database owner is used for maintenance. If the UI connects
with another account, it needs access to `analytics` to see the tables; grant a
separate reporting role only the minimum SELECT privileges needed. Do not expose
this private schema through a public API.

`refresh` is owner-only and idempotent: it reconciles fact rows with the source
and can safely be rerun after a failed backfill. It locks the three source tables
against writes until it completes. Schedule it outside sync windows, especially
for a large database; test duration on a representative copy. Source reads can
continue. Incremental triggers work immediately after migration, but existing
rows appear only after the first successful refresh. The migration runner uses
tracked forward-only migrations. Production migration and refresh must use the
separate migration owner and follow the production runbook, not the local
Compose credentials. Take and test a backup before deployment.

Example current prices:

```sql
SELECT p.product_code, p.product_name, p.brand,
       f.selling_price, f.selling_price_raw, f.is_active
FROM analytics.fact_product_current f
JOIN analytics.dim_product p USING (product_key)
ORDER BY p.product_name
LIMIT 100;
```

Example observed price history:

```sql
SELECT p.product_code, p.product_name, o.observed_at,
       o.selling_price, o.selling_price_raw
FROM analytics.fact_product_observation o
JOIN analytics.dim_product p USING (product_key)
WHERE p.product_code = 'YOUR_PRODUCT_CODE'
ORDER BY o.observed_at;
```
