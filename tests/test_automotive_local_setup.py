"""Local setup scope, credential isolation and failure redaction (no real database)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg
from dotenv import dotenv_values
from psycopg.conninfo import conninfo_to_dict

from automotive.local_setup import LocalSetupError, configure_local, local_admin_url

ADMIN = 'postgresql://admin:fixture@127.0.0.1:55432/test_catalog'


class LocalSetupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env_path = Path(self.directory.name) / '.env'
        self.env_path.write_text(
            f'DATABASE_URL={ADMIN}\nWAREHOUSE_COOKIE=preserved-fixture\n'
            'AUTOMOTIVE_VIN_HMAC_KEY=preserved-existing-vin-cache-key-123456\n'
        )

    def test_remote_service_hostaddr_and_missing_urls_cannot_be_used_for_setup(self):
        for url in ('', 'postgresql://admin:fixture@remote.example/test',
                    'service=production',
                    'host=127.0.0.1 hostaddr=203.0.113.1 dbname=test user=admin',
                    'host=/tmp dbname=test user=admin'):
            with self.subTest(url=url):
                with self.assertRaises(LocalSetupError):
                    local_admin_url({'DATABASE_URL': url})
        info = conninfo_to_dict(local_admin_url({'DATABASE_URL': ADMIN}))
        self.assertEqual(info['hostaddr'], '127.0.0.1')

    @patch('automotive.local_setup.psycopg.connect')
    def test_existing_or_partial_credentials_are_preserved_without_admin_connection(self, connect):
        with self.env_path.open('a') as target:
            target.write('AUTOMOTIVE_DATABASE_URL=existing-private-reader\n')
        before = self.env_path.read_bytes()
        with self.assertRaisesRegex(LocalSetupError, 'preserved'):
            configure_local(self.env_path)
        self.assertEqual(self.env_path.read_bytes(), before)
        connect.assert_not_called()

    @patch('automotive.local_setup.psycopg.connect')
    def test_connection_failure_is_safe_and_leaves_env_unchanged(self, connect):
        connect.side_effect = psycopg.OperationalError('password=private')
        before = self.env_path.read_bytes()
        with self.assertRaises(LocalSetupError) as error:
            configure_local(self.env_path)
        self.assertNotIn('password=private', str(error.exception))
        self.assertEqual(self.env_path.read_bytes(), before)

    @patch('automotive.local_setup.psycopg.connect')
    def test_unmigrated_catalog_fails_before_any_grants_or_credentials(self, connect):
        connection = connect.return_value.__enter__.return_value
        connection.execute.return_value.fetchone.return_value = (False,)
        before = self.env_path.read_bytes()
        with self.assertRaisesRegex(LocalSetupError, 'not migrated'):
            configure_local(self.env_path)
        self.assertEqual(self.env_path.read_bytes(), before)
        self.assertEqual(connection.execute.call_count, 2)  # lock and preflight reads only

    @patch('automotive.local_setup.psycopg.connect')
    def test_pending_warehouse_migrations_are_not_applied_automatically(self, connect):
        connection = connect.return_value.__enter__.return_value
        connection.execute.return_value.fetchone.return_value = (True,)
        connection.execute.return_value.__iter__.return_value = iter([('0001_initial',)])
        before = self.env_path.read_bytes()
        with patch('automotive.local_setup.apply_migrations_on_connection') as migrate:
            with self.assertRaisesRegex(LocalSetupError, 'Earlier warehouse migrations'):
                configure_local(self.env_path)
        migrate.assert_not_called()
        self.assertEqual(self.env_path.read_bytes(), before)

    @patch('automotive.local_setup.apply_migrations_on_connection')
    @patch('automotive.local_setup._require_catalog')
    @patch('automotive.local_setup.psycopg.connect')
    def test_credentials_are_isolated_saved_privately_and_existing_values_survive(
        self, connect, require_catalog, migrate,
    ):
        connection = MagicMock()
        connect.return_value.__enter__.return_value = connection
        connection.pgconn.encrypt_password.return_value = b'SCRAM-SHA-256$fixture-verifier'
        with patch('sys.stdout') as output:
            result = configure_local(self.env_path)
        self.assertTrue(result['database_configured'])
        self.assertFalse(result['anthropic_configured'])
        output.write.assert_not_called()
        values = dotenv_values(self.env_path)
        runtime = conninfo_to_dict(values['AUTOMOTIVE_DATABASE_URL'])
        enrichment = conninfo_to_dict(values['AUTOMOTIVE_ENRICHMENT_DATABASE_URL'])
        self.assertNotEqual(runtime['user'], 'admin')
        self.assertNotEqual(runtime['user'], enrichment['user'])
        self.assertNotEqual(runtime['password'], enrichment['password'])
        self.assertEqual(runtime['dbname'], 'test_catalog')
        self.assertEqual(values['WAREHOUSE_COOKIE'], 'preserved-fixture')
        self.assertEqual(values['AUTOMOTIVE_VIN_HMAC_KEY'],
                         'preserved-existing-vin-cache-key-123456')
        self.assertGreaterEqual(len(values['AUTOMOTIVE_OPERATOR_TOKEN']), 32)
        self.assertEqual(self.env_path.stat().st_mode & 0o777, 0o600)
        statements = '\n'.join(call.args[0].as_string() for call in
                               connection.execute.call_args_list
                               if hasattr(call.args[0], 'as_string'))
        self.assertNotIn(runtime['password'], statements)
        self.assertNotIn(enrichment['password'], statements)
        require_catalog.assert_called_once_with(connection)
        self.assertEqual(migrate.call_args.kwargs['through'], '0006_automotive')


if __name__ == '__main__':
    unittest.main()
