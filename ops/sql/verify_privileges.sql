-- Execute as warehouse_sync_writer. The first three results must be false;
-- the final result must be true.
SELECT has_schema_privilege(current_user, 'warehouse', 'CREATE') AS can_create_schema_objects;
SELECT has_table_privilege(current_user, 'warehouse.warehouse_products', 'SELECT') AS can_read_products;
SELECT has_table_privilege(current_user, 'warehouse.warehouse_products', 'DELETE') AS can_delete_products;
SELECT has_function_privilege(current_user, 'warehouse.record_product(bigint,integer,text,text,text,text,text,text,text,jsonb,character,character)', 'EXECUTE') AS can_call_writer_function;
