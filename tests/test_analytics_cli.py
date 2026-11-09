"""Safe, actionable diagnostics for the analytics maintenance command."""
import contextlib
import io
import unittest
from unittest.mock import patch

from psycopg.errors import AmbiguousColumn, OperationalError

from db_ui.analytics import main


class AnalyticsCliTests(unittest.TestCase):
    def test_database_error_reports_code_without_server_or_connection_details(self):
        output = io.StringIO()
        error = AmbiguousColumn("sensitive row or connection details")
        with (
            patch("sys.argv", ["analytics", "refresh"]),
            patch("db_ui.analytics.connect", side_effect=error),
            contextlib.redirect_stderr(output),
        ):
            result = main()
        self.assertEqual(result, 1)
        self.assertIn("SQLSTATE 42702", output.getvalue())
        self.assertNotIn("sensitive", output.getvalue())

    def test_connection_error_without_sqlstate_is_safe(self):
        output = io.StringIO()
        with (
            patch("sys.argv", ["analytics", "refresh"]),
            patch("db_ui.analytics.connect", side_effect=OperationalError("password=secret")),
            contextlib.redirect_stderr(output),
        ):
            result = main()
        self.assertEqual(result, 1)
        self.assertIn("SQLSTATE unavailable", output.getvalue())
        self.assertNotIn("secret", output.getvalue())
