"""Migration-owner-only repair for observations written before HMAC hardening."""

from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

from warehouse_sync.integrity import version_integrity_tag


def sign_unsigned_observations(
    database_url: str, hmac_key: bytes, *, batch_size: int = 1_000
) -> int:
    """Sign legacy rows in bounded transactions using a key never stored in PostgreSQL."""
    signed = 0
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        while True:
            with connection.transaction():
                versions = connection.execute(
                    """
                    SELECT version_id, source_product_id, observed_run_id, source_page,
                           source_columns, row_hash
                    FROM warehouse.warehouse_product_versions
                    WHERE integrity_tag IS NULL
                    ORDER BY version_id
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                    """,
                    (batch_size,),
                ).fetchall()
                for version in versions:
                    tag = version_integrity_tag(
                        hmac_key,
                        source_product_id=version["source_product_id"],
                        observed_run_id=version["observed_run_id"],
                        source_page=version["source_page"],
                        source_columns=version["source_columns"],
                        row_hash=version["row_hash"],
                    )
                    connection.execute(
                        """
                        UPDATE warehouse.warehouse_product_versions
                        SET integrity_tag = %s WHERE version_id = %s
                        """,
                        (tag, version["version_id"]),
                    )
                signed += len(versions)
            if not versions:
                break

        with connection.transaction():
            products = connection.execute(
                """
                SELECT source_product_id, last_seen_run_id, source_page, source_columns, row_hash
                FROM warehouse.warehouse_products
                WHERE integrity_tag IS NULL
                FOR UPDATE
                """
            ).fetchall()
            for product in products:
                tag = version_integrity_tag(
                    hmac_key,
                    source_product_id=product["source_product_id"],
                    observed_run_id=product["last_seen_run_id"],
                    source_page=product["source_page"],
                    source_columns=product["source_columns"],
                    row_hash=product["row_hash"],
                )
                connection.execute(
                    """
                    UPDATE warehouse.warehouse_products
                    SET integrity_tag = %s WHERE source_product_id = %s
                    """,
                    (tag, product["source_product_id"]),
                )
    return signed
