"""Tests for the explorer's query and browsing boundaries."""

import unittest
from unittest.mock import MagicMock, patch

from db_ui import __main__ as explorer


class ExplorerTests(unittest.TestCase):
    def connection(self):
        connection = MagicMock()
        cursor = MagicMock()
        connection.__enter__.return_value = connection
        connection.cursor.return_value.__enter__.return_value = cursor
        return connection, cursor

    def test_query_uses_single_prepared_read_only_statement_and_caps_results(self):
        connection, cursor = self.connection()
        cursor.description = [MagicMock(name="column")]
        cursor.description[0].name = "value"
        cursor.fetchmany.return_value = [(n,) for n in range(201)]
        with patch.object(explorer, "connect", return_value=connection):
            result = explorer.run_query("SELECT value FROM example")
        self.assertEqual(cursor.execute.call_args_list[0].args[0], "SET TRANSACTION READ ONLY")
        self.assertEqual(
            cursor.execute.call_args_list[1].args[0], "SET LOCAL statement_timeout = '15s'"
        )
        self.assertEqual(cursor.execute.call_args_list[2].kwargs, {"prepare": True})
        self.assertEqual(len(result["rows"]), 200)
        self.assertTrue(result["has_more"])

    def test_query_paginates_and_export_fetches_full_result(self):
        connection, cursor = self.connection()
        cursor.description = [MagicMock(name="column")]
        cursor.description[0].name = "value"
        cursor.rowcount = 250
        cursor.fetchmany.return_value = [(200,), (201,)]
        cursor.fetchall.return_value = [(n,) for n in range(250)]
        with patch.object(explorer, "connect", return_value=connection):
            result = explorer.run_query("SELECT value FROM example", 1)
            body, content_type = explorer.run_query(
                "SELECT value FROM example", export_format="json"
            )
        cursor.scroll.assert_called_once_with(200, mode="absolute")
        self.assertEqual(result["rows"], [(200,), (201,)])
        self.assertFalse(result["has_more"])
        import json

        self.assertEqual(len(json.loads(body)["rows"]), 250)
        self.assertTrue(content_type.startswith("application/json"))

    def test_export_formats_preserve_values_and_prevent_spreadsheet_formulas(self):
        import csv
        import io
        import json
        import zipfile
        from xml.etree import ElementTree

        from db_ui.export import export_result

        columns = ["name", "name"]
        rows = [("=1+1", 'comma, quote " & <')]
        body, _ = export_result("csv", columns, rows)
        self.assertEqual(
            list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))[1], ["'=1+1", rows[0][1]]
        )
        body, _ = export_result("json", columns, rows)
        self.assertEqual(json.loads(body), {"columns": columns, "rows": [list(rows[0])]})
        body, _ = export_result("xlsx", columns, rows)
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            xml = archive.read("xl/worksheets/sheet1.xml")
            tree = ElementTree.fromstring(xml)
            self.assertEqual(
                [
                    node.text
                    for node in tree.iter(
                        "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t"
                    )
                ],
                [*columns, *rows[0]],
            )
            self.assertNotIn(b"<f>", xml)

    def test_browse_quotes_identifiers_and_orders_primary_key(self):
        connection, cursor = self.connection()
        cursor.description = [MagicMock(name="column")]
        cursor.description[0].name = "id"
        cursor.fetchall.return_value = [(1,), (2,)]
        table = {
            "schema": "warehouse",
            "name": 'strange"table',
            "columns": [{"name": "id", "primary_key": True}],
        }
        with (
            patch.object(explorer, "connect", return_value=connection),
            patch.object(explorer, "catalog", return_value=[table]),
        ):
            result = explorer.browse("warehouse", 'strange"table', 2)
        statement, parameters = cursor.execute.call_args_list[2].args
        self.assertEqual(
            statement.as_string(),
            'SELECT * FROM "warehouse"."strange""table" ORDER BY "id" LIMIT %s OFFSET %s',
        )
        self.assertEqual(parameters, (51, 100))
        self.assertEqual(result["rows"], [(1,), (2,)])

    def test_rejects_unknown_tables_and_invalid_queries(self):
        with patch.object(explorer, "catalog", return_value=[]):
            with self.assertRaises(ValueError):
                explorer.browse("public", "missing", 0)
        with self.assertRaises(ValueError):
            explorer.run_query(" ")
        with self.assertRaises(ValueError):
            explorer.run_query("SELECT 1", -1)
        with self.assertRaises(ValueError):
            explorer.browse("public", "table", -1)


if __name__ == "__main__":
    unittest.main()
