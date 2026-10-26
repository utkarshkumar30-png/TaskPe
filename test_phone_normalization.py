import atexit
import json
import os
import shutil
import tempfile
import unittest


_TEST_DIR = tempfile.mkdtemp(prefix='taskpe-phone-normalization-')
os.environ['ENVIRONMENT'] = 'development'
os.environ['TASKPE_DB_PATH'] = os.path.join(_TEST_DIR, 'taskpe.db')
os.environ['SMS_PROVIDER'] = 'disabled'
os.environ['TASKPE_WHATSAPP_LINKED_DEVICE_ENABLED'] = '0'

import database
import app as app_module


atexit.register(lambda: shutil.rmtree(_TEST_DIR, ignore_errors=True))


class InternationalRecipientNormalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved_db_path = database.DB_PATH
        cls._saved_env = {
            key: os.environ.get(key)
            for key in ('TASKPE_DB_PATH', 'ENVIRONMENT')
        }
        database.DB_PATH = os.environ['TASKPE_DB_PATH']
        database.initialize_database()

    @classmethod
    def tearDownClass(cls):
        database.DB_PATH = cls._saved_db_path
        for key, value in cls._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_india_us_and_uk_e164_and_explicit_national_regions(self):
        self.assertEqual(database.normalize_recipient_number('+91 98765 43210'), '+919876543210')
        self.assertEqual(database.normalize_recipient_number('9876543210', region='IN'), '+919876543210')
        self.assertEqual(database.normalize_recipient_number('+1 (415) 555-2671'), '+14155552671')
        self.assertEqual(database.normalize_recipient_number('4155552671', region='US'), '+14155552671')
        self.assertEqual(database.normalize_recipient_number('+44 20 7946 0958'), '+442079460958')
        self.assertEqual(database.normalize_recipient_number('020 7946 0958', region='GB'), '+442079460958')

    def test_equivalent_values_deduplicate_and_fingerprint_identically(self):
        first = database._canonical_whatsapp_recipient_snapshot(
            ['+91 98765 43210', '+14155552671'], region='IN'
        )
        second = database._canonical_whatsapp_recipient_snapshot(
            ['9876543210', '+1 415 555 2671'], region='IN'
        )
        self.assertEqual(first, second)

    def test_missing_region_invalid_and_ambiguous_values_fail_closed(self):
        with self.assertRaises(ValueError):
            database.normalize_recipient_number('9876543210')
        with self.assertRaises(ValueError):
            database.normalize_recipient_number('12345', region='US')
        with self.assertRaises(ValueError):
            database.normalize_recipient_number('+999123456789')

    def test_legacy_values_are_not_rewritten_or_treated_as_safe_recipients(self):
        legacy = '919876543210'
        conn = database.get_db_connection()
        try:
            conn.execute(
                'INSERT INTO tasks (title, category, description, payout, template_text, target_numbers, recipient_region, proof_instructions) '
                "VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
                ('Legacy', 'Direct Message', 'Legacy', 1, 'Legacy', json.dumps([legacy]), 'Proof'),
            )
            task = conn.execute('SELECT * FROM tasks ORDER BY id DESC LIMIT 1').fetchone()
            self.assertEqual(app_module.task_recipient_pool(task), [])
            self.assertEqual(
                conn.execute('SELECT target_numbers FROM tasks WHERE id = ?', (task['id'],)).fetchone()[0],
                json.dumps([legacy]),
            )
        finally:
            conn.rollback()
            conn.close()

    def test_opt_out_matches_canonical_and_legacy_storage_without_rewriting(self):
        conn = database.get_db_connection()
        try:
            database.record_whatsapp_opt_out(conn, '+14155552671', region='US')
            self.assertTrue(database.is_recipient_opted_out(conn, '4155552671', region='US'))
            conn.rollback()
            conn.execute(
                "INSERT INTO whatsapp_opt_outs (phone_number, reason) VALUES (?, ?)",
                ('919876543210', 'legacy'),
            )
            result = database.check_recipients_consent_and_opt_out(
                conn, ['+919876543210']
            )
            self.assertIn('+919876543210', result['opted_out'])
        finally:
            conn.rollback()
            conn.close()

    def test_exact_declaration_text_is_required(self):
        conn = database.get_db_connection()
        try:
            with self.assertRaises(ValueError):
                database.create_whatsapp_campaign_consent_declaration(
                    conn, 100001, 100001, 1, ['+14155552671'],
                    declaration_text='browser supplied text', region='US',
                )
        finally:
            conn.rollback()
            conn.close()


if __name__ == '__main__':
    unittest.main()
