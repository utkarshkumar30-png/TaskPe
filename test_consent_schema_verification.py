import os
import shutil
import sqlite3
import tempfile
import unittest


TEST_ROOT = tempfile.mkdtemp(prefix='taskpe-consent-schema-')
os.environ['ENVIRONMENT'] = 'development'
os.environ['TASKPE_DB_PATH'] = os.path.join(TEST_ROOT, 'taskpe.db')

import database


class ConsentSchemaVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database.DB_PATH = os.environ['TASKPE_DB_PATH']
        database.initialize_database()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TEST_ROOT, ignore_errors=True)

    def _connection(self):
        conn = sqlite3.connect(database.DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    def test_valid_schema_passes_and_is_read_only(self):
        conn = self._connection()
        before = conn.total_changes
        self.assertTrue(database.verify_required_schema(conn))
        self.assertEqual(conn.total_changes, before)
        conn.close()

    def test_each_runtime_consent_table_is_required(self):
        for table in (
            'shop_consent_declarations',
            'recipient_consent_records',
            'whatsapp_opt_outs',
            'whatsapp_opt_out_operations',
        ):
            conn = self._connection()
            conn.execute(f'DROP TABLE {table}')
            problems = database.schema_problems(conn)
            self.assertIn(table, problems['tables'])
            conn.close()
            database.initialize_database()

    def test_each_runtime_consent_index_is_required(self):
        for index_name in (
            'shop_consent_declarations_uploader',
            'recipient_consent_phone_status',
            'whatsapp_opt_outs_phone_unique',
            'whatsapp_opt_out_operations_actor',
            'whatsapp_campaign_consent_lookup',
        ):
            conn = self._connection()
            conn.execute(f'DROP INDEX {index_name}')
            problems = database.schema_problems(conn)
            self.assertIn(index_name, problems['indexes'])
            conn.close()
            database.initialize_database()

    def test_schema_verifier_does_not_mutate_database(self):
        conn = self._connection()
        before = conn.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        database.verify_required_schema(conn)
        after = conn.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        self.assertEqual([tuple(row) for row in before], [tuple(row) for row in after])
        conn.close()


if __name__ == '__main__':
    unittest.main()
