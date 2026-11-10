# Sales and purchasing

The `trade` schema records sales and purchases inferred from stock changes. The source
reports only how many units are in stock, not the transactions, so every row is an
inference from two consecutive syncs of the same product.

| Table | One row per |
|---|---|
| `trade.sales` | Drop in a product's store + TUZ 11 stock between two syncs. |
| `trade.purchases` | Rise in a product's store + TUZ 11 stock, or a new product that arrived with stock. |
| `trade.baseline` | Single row: the sync run whose products are starting stock. |

## How rows are recorded

- Stock is the store (source column "1", `column_10`) plus TUZ 11 (`column_12`). TUZ 6
  (`column_11`) has no agreed meaning and is ignored.
- When a sync saves a product whose stock total differs from the previous sync, a drop is
  written to `trade.sales` and a rise to `trade.purchases`. Moving items between the store and
  TUZ 11 leaves the total unchanged and is not recorded; TUZ 11 down 5 with the store up 3 is
  a sale of 2. `store_before`, `store_after`, `tuz11_before` and `tuz11_after` keep both counts.
- `period_start` is the previous observation and `period_end` the one that showed the change:
  the sale or delivery happened between them. `detected_run_id` is the sync run.
- A product first seen after the baseline with stock above zero is a purchase of that stock
  (`is_new_product`, `period_start` NULL). Products already in the catalog when the module was
  installed are starting stock; on a new database, the products of the first finished sync are.
- A sale is valued at the selling price (`column_06`) listed before the drop:
  `amount = quantity * unit_price`. Currency is unknown. A price that is not a plain dot-decimal
  number, such as `1,200`, leaves `unit_price` and `amount` NULL and keeps `unit_price_raw`. The
  source has no purchase cost, so purchases record quantities only.
- Quantities must be plain numbers; negative counts and decimals with a dot or comma are
  accepted. If either cell is not a number, no row is written for that change.

Database triggers on `warehouse.warehouse_products` write these rows in the same transaction
as the sync, as the analytics triggers do. The sync writer role cannot read or change `trade`.

## Limits

- The sync runs at 09:00 and 21:00 Tbilisi time. Sales and deliveries of the same item within
  one interval cancel out: selling 3 and receiving 5 is recorded as a purchase of 2.
- Customer returns look like purchases. Write-offs, damage, stock corrections and transfers to
  TUZ 6 or other locations look like sales.
- Changes from before the module was installed are not converted. Rows are kept indefinitely
  and are not pruned with old product versions; backups include them.
- On a brand-new database, run the first sync without `--max-pages`. Otherwise products on the
  pages it skipped would count as new-product purchases when a later sync first sees them.

## Setup

Apply migration `0010` locally (rebuild the UI image so it contains the migration):

```bash
docker compose up --build -d postgres db-ui
docker compose exec db-ui python -m db_ui.analytics migrate
```

`uv run python -m warehouse_sync --migrate` works too. In production, apply it with the
migration owner after a tested backup, following the [production runbook](production-runbook.md).
Then install `ops/systemd/warehouse-sync.timer`, which runs the sync at 09:00 and 21:00 Tbilisi
time; the healthcheck alerts after 13 hours without a successful sync. The parts assistant
asks customers to confirm stock older than 13 hours and stops stating it after 25 hours
(`automotive/rules.toml`).

The local explorer lists the `trade` tables with their column descriptions. Any other
reporting account needs its own minimal `SELECT` grant; do not expose `trade` through a public API.

## Example queries

Daily sales, in Tbilisi time:

```sql
SELECT (s.period_end AT TIME ZONE 'Asia/Tbilisi')::date AS day,
       p.product_code, p.product_name, p.brand,
       sum(s.quantity) AS units_sold, sum(s.amount) AS sales_amount
FROM trade.sales s
JOIN warehouse.warehouse_products p USING (source_product_id)
GROUP BY day, s.source_product_id, p.product_code, p.product_name, p.brand
ORDER BY day DESC, units_sold DESC;
```

Daily purchases:

```sql
SELECT (b.period_end AT TIME ZONE 'Asia/Tbilisi')::date AS day,
       p.product_code, p.product_name, p.brand,
       sum(b.quantity) AS units_bought, bool_or(b.is_new_product) AS new_product
FROM trade.purchases b
JOIN warehouse.warehouse_products p USING (source_product_id)
GROUP BY day, b.source_product_id, p.product_code, p.product_name, p.brand
ORDER BY day DESC, units_bought DESC;
```

Best sellers over the last 30 days:

```sql
SELECT p.product_code, p.product_name, p.brand,
       sum(s.quantity) AS units_sold, sum(s.amount) AS sales_amount
FROM trade.sales s
JOIN warehouse.warehouse_products p USING (source_product_id)
WHERE s.period_end >= now() - interval '30 days'
GROUP BY s.source_product_id, p.product_code, p.product_name, p.brand
ORDER BY units_sold DESC
LIMIT 50;
```
