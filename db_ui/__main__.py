"""Run the local database explorer with python -m db_ui."""

from __future__ import annotations

import json
import os
import secrets
import sys
from datetime import date, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID

import psycopg
from dotenv import load_dotenv
from psycopg import sql
from psycopg.rows import dict_row, tuple_row

STATIC = Path(__file__).parent / "static"
TOKEN = secrets.token_urlsafe(32)
MAX_ROWS = 200

TABLE_DESCRIPTIONS = {
    "warehouse_products": (
        "The latest known record for each product, including its current status and source data."
    ),
    "warehouse_product_versions": (
        "A snapshot is saved here whenever a product is first seen or its source data changes."
    ),
    "warehouse_sync_runs": (
        "One record per synchronization attempt, with progress, outcome, and timing."
    ),
    "warehouse_sync_seen_pages": "The source pages observed during each synchronization run.",
    "warehouse_schema_migrations": "Tracks database migrations that have already been applied.",
}
COLUMN_DESCRIPTIONS = {
    "source_product_id": "Product identifier in the source system.",
    "product_code": "Product code from the source system.",
    "product_name": "Name of the product.",
    "product_model": "Model or variant of the product.",
    "original_code": "Original manufacturer or source code.",
    "duplicate_code": "Alternate or duplicate product code from the source.",
    "brand": "Product brand.",
    "source_columns": "Structured source fields captured for this product, stored as JSON.",
    "row_hash": "Hash of the source data used to detect changes.",
    "integrity_tag": "HMAC signature used to verify the recorded data's integrity.",
    "source_page": "Source page on which the product was observed.",
    "is_active": "Whether the product was present in the latest complete sync.",
    "first_seen_at": "When the product was first recorded.",
    "last_observed_at": "When the product was most recently observed.",
    "last_changed_at": "When the product data last changed.",
    "first_seen_run_id": "Sync run that first recorded this product.",
    "last_seen_run_id": "Sync run that most recently observed this product.",
    "version_id": "Unique identifier for this saved product version.",
    "observed_run_id": "Sync run that recorded this version.",
    "observed_at": "When this record was observed.",
    "run_id": "Unique identifier of the synchronization run.",
    "status": "Current or final outcome of the synchronization run.",
    "expected_pages": "Number of source pages expected for the run.",
    "pages_fetched": "Number of source pages fetched.",
    "rows_seen": "Number of source rows observed.",
    "rows_added": "Number of products recorded for the first time.",
    "rows_changed": "Number of products whose data changed.",
    "rows_unchanged": "Number of products whose data did not change.",
    "rows_deactivated": "Number of products marked inactive after a complete run.",
    "started_at": "When the run started.",
    "finished_at": "When the run finished, if complete.",
    "failure_reason": "Safe failure code for an unsuccessful run, when available.",
    "page": "Source page number observed during the run.",
    "version": "Identifier of the applied migration.",
    "applied_at": "When the migration was applied.",
}


def connect():
    url = os.getenv("DB_UI_DATABASE_URL") or os.getenv("DATABASE_URL")
    if url:
        return psycopg.connect(url, row_factory=dict_row, connect_timeout=5)
    if os.getenv("DB_UI_PGHOST"):
        return psycopg.connect(
            host=os.environ["DB_UI_PGHOST"],
            port=os.getenv("DB_UI_PGPORT", "5432"),
            dbname=os.getenv("POSTGRES_DB"),
            user=os.getenv("POSTGRES_USER"),
            password=os.getenv("POSTGRES_PASSWORD"),
            row_factory=dict_row,
            connect_timeout=5,
        )
    raise ValueError("Set DB_UI_DATABASE_URL or DATABASE_URL to a PostgreSQL connection URL.")


def json_default(value):
    if isinstance(value, datetime | date | Decimal | UUID):
        return str(value)
    if isinstance(value, bytes):
        return "\\x" + value.hex()
    return str(value)


def catalog():
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute("SET LOCAL statement_timeout = '10s'")
        cur.execute("""
            SELECT t.table_schema, t.table_name, t.table_type, c.column_name, c.data_type,
                   c.udt_name, c.is_nullable, c.column_default, c.ordinal_position,
                   pg_catalog.col_description(cls.oid, c.ordinal_position) AS column_comment,
                   pg_catalog.obj_description(cls.oid, 'pg_class') AS table_comment,
                   EXISTS (SELECT 1 FROM information_schema.table_constraints tc
                     JOIN information_schema.key_column_usage ku
                       ON tc.constraint_name = ku.constraint_name
                       AND tc.constraint_schema = ku.constraint_schema
                       AND tc.table_name = ku.table_name
                     WHERE tc.constraint_type = 'PRIMARY KEY' AND ku.table_schema = c.table_schema
                       AND ku.table_name = c.table_name
                       AND ku.column_name = c.column_name) AS primary_key
            FROM information_schema.tables t
            JOIN information_schema.columns c
              ON c.table_schema = t.table_schema AND c.table_name = t.table_name
            JOIN pg_catalog.pg_namespace ns ON ns.nspname = t.table_schema
            JOIN pg_catalog.pg_class cls ON cls.relnamespace = ns.oid AND cls.relname = t.table_name
            WHERE t.table_schema NOT IN ('pg_catalog', 'information_schema')
              AND t.table_schema NOT LIKE 'pg_toast%%' AND t.table_schema NOT LIKE 'pg_temp_%%'
            ORDER BY t.table_schema, t.table_name, c.ordinal_position
        """)
        tables = {}
        for row in cur.fetchall():
            key = (row["table_schema"], row["table_name"])
            if key not in tables:
                tables[key] = {
                    "schema": key[0],
                    "name": key[1],
                    "type": row["table_type"],
                    "description": row["table_comment"]
                    or TABLE_DESCRIPTIONS.get(key[1], "No description available for this table."),
                    "columns": [],
                }
            tables[key]["columns"].append(
                {
                    "name": row["column_name"],
                    "type": row["data_type"]
                    if row["data_type"] != "USER-DEFINED"
                    else row["udt_name"],
                    "nullable": row["is_nullable"] == "YES",
                    "default": row["column_default"],
                    "primary_key": row["primary_key"],
                    "description": row["column_comment"]
                    or COLUMN_DESCRIPTIONS.get(
                        row["column_name"], "No description available for this column."
                    ),
                }
            )
        return list(tables.values())


def run_query(query, page=0, export_format=None):
    if not isinstance(query, str) or not query.strip() or len(query) > 100_000:
        raise ValueError("Enter a SQL statement (up to 100,000 characters).")
    if isinstance(page, bool) or not isinstance(page, int) or page < 0 or page > 1_000_000:
        raise ValueError("Invalid page number.")
    with connect() as conn, conn.cursor(row_factory=tuple_row) as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute("SET LOCAL statement_timeout = '15s'")
        cur.execute(query, prepare=True)
        if not cur.description:
            return {"columns": [], "rows": [], "has_more": False}
        columns = [col.name for col in cur.description]
        if export_format:
            from db_ui.export import export_result

            return export_result(export_format, columns, cur.fetchall())
        if page:
            offset = page * MAX_ROWS
            if offset >= cur.rowcount:
                return {"columns": columns, "rows": [], "has_more": False}
            cur.scroll(offset, mode="absolute")
        values = cur.fetchmany(MAX_ROWS + 1)
        return {"columns": columns, "rows": values[:MAX_ROWS], "has_more": len(values) > MAX_ROWS}


def browse(schema, table, page):
    if (
        not isinstance(schema, str)
        or not isinstance(table, str)
        or not isinstance(page, int)
        or not 0 <= page <= 100000
    ):
        raise ValueError("Invalid table or page.")
    available = {(t["schema"], t["name"]): t for t in catalog()}
    info = available.get((schema, table))
    if info is None:
        raise ValueError("Table is unavailable or you do not have access.")
    keys = [column["name"] for column in info["columns"] if column["primary_key"]]
    ordering = (
        sql.SQL(" ORDER BY ") + sql.SQL(", ").join(map(sql.Identifier, keys))
        if keys
        else sql.SQL("")
    )
    with connect() as conn, conn.cursor(row_factory=tuple_row) as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute("SET LOCAL statement_timeout = '15s'")
        cur.execute(
            sql.SQL("SELECT * FROM {}.{}{} LIMIT %s OFFSET %s").format(
                sql.Identifier(schema), sql.Identifier(table), ordering
            ),
            (51, page * 50),
        )
        columns = [col.name for col in cur.description]
        rows = cur.fetchall()
        return {"columns": columns, "rows": rows[:50], "has_more": len(rows) > 50}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send_data(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def host_ok(self):
        host = self.headers.get("Host", "").split(":", 1)[0].lower()
        return host in {"localhost", "127.0.0.1"}

    def do_GET(self):
        if not self.host_ok():
            return self.send_data(403, b"Forbidden", "text/plain")
        routes = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/style.css": ("style.css", "text/css; charset=utf-8"),
        }
        path = urlparse(self.path).path
        if path == "/api/session":
            return self.send_data(200, json.dumps({"token": TOKEN}).encode(), "application/json")
        if path not in routes:
            return self.send_data(404, b"Not found", "text/plain")
        filename, content_type = routes[path]
        self.send_data(200, (STATIC / filename).read_bytes(), content_type)

    def do_POST(self):
        origin = self.headers.get("Origin", "")
        if (
            not self.host_ok()
            or origin != f"http://{self.headers.get('Host')}"
            or not secrets.compare_digest(self.headers.get("X-DB-UI-Token", ""), TOKEN)
        ):
            return self.send_data(403, b"Forbidden", "text/plain")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 110_000:
                raise ValueError("Request is too large.")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object.")
            if self.path == "/api/catalog":
                result = catalog()
            elif self.path == "/api/query":
                result = run_query(data.get("query"), data.get("page", 0))
            elif self.path == "/api/export":
                export_format = data.get("format")
                if not isinstance(export_format, str) or export_format not in {
                    "csv",
                    "json",
                    "xlsx",
                }:
                    raise ValueError("Choose CSV, JSON, or Excel format.")
                result = run_query(data.get("query"), export_format=export_format)
                if isinstance(result, dict):
                    raise ValueError("This statement did not return a result set.")
                body, content_type = result
                return self.send_data(200, body, content_type)
            elif self.path == "/api/browse":
                result = browse(data.get("schema"), data.get("table"), data.get("page", 0))
            else:
                return self.send_data(404, b"Not found", "text/plain")
            self.send_data(
                200, json.dumps(result, default=json_default).encode(), "application/json"
            )
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_data(400, json.dumps({"error": str(exc)}).encode(), "application/json")
        except psycopg.Error as exc:
            # Driver connection errors can contain credentials; SQL errors are useful to the editor.
            message = (
                exc.diag.message_primary
                if exc.diag and exc.diag.message_primary
                else "Could not connect to PostgreSQL. Check the database settings and permissions."
            )
            self.send_data(400, json.dumps({"error": message}).encode(), "application/json")
        except OSError:
            self.send_data(
                500,
                b'{"error":"Could not reach PostgreSQL. Check that the database is running."}',
                "application/json",
            )


def main():
    load_dotenv(override=False)
    host = os.getenv("DB_UI_BIND", "127.0.0.1")
    port = int(os.getenv("DB_UI_PORT", "8765"))
    print(f"Database explorer: http://localhost:{port}", flush=True)
    try:
        ThreadingHTTPServer((host, port), Handler).serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
