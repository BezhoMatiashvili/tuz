-- Destructive: deletes every recorded sale and purchase. Take a tested backup first;
-- this history cannot be rebuilt once old product versions are pruned.
DROP TRIGGER IF EXISTS trade_stock_change ON warehouse.warehouse_products;
DROP TRIGGER IF EXISTS trade_new_product ON warehouse.warehouse_products;
DROP TRIGGER IF EXISTS trade_baseline_run ON warehouse.warehouse_sync_runs;
DROP SCHEMA IF EXISTS trade CASCADE;
