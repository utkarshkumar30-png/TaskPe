import os
import sqlite3
import unittest

os.environ.setdefault('ENVIRONMENT', 'development')
os.environ.setdefault('SECRET_KEY', 'test-only-development-secret')

from app import credit_referrer_for_task


class RealReferralLimitTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript('''
            CREATE TABLE users (id INTEGER PRIMARY KEY, referred_by INTEGER, referred_user_task_count INTEGER DEFAULT 0, wallet_balance REAL DEFAULT 0, total_earned REAL DEFAULT 0);
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE wallet_transactions (id INTEGER PRIMARY KEY, user_id INTEGER, type TEXT, amount REAL, balance_after REAL, reference_id TEXT, description TEXT);
            CREATE UNIQUE INDEX one_referral ON wallet_transactions(reference_id) WHERE type='referral_task_commission';
            INSERT INTO users VALUES (1, NULL, 0, 0, 0);
            INSERT INTO users VALUES (2, 1, 0, 0, 0);
            INSERT INTO settings VALUES ('referral_limit_tasks', '20');
            INSERT INTO settings VALUES ('referral_commission_percent', '10');
        ''')

    def tearDown(self):
        self.conn.close()

    def test_real_referral_credit_function_stops_after_twentieth_task(self):
        cursor = self.conn.cursor()
        worker = cursor.execute('SELECT * FROM users WHERE id=2').fetchone()
        for task_number in range(1, 22):
            cursor.execute('UPDATE users SET referred_user_task_count=? WHERE id=2', (task_number,))
            credit_referrer_for_task(cursor, worker, 75, task_number)
        self.assertEqual(cursor.execute("SELECT COUNT(*) FROM wallet_transactions WHERE type='referral_task_commission'").fetchone()[0], 20)
        self.assertEqual(cursor.execute('SELECT wallet_balance FROM users WHERE id=1').fetchone()[0], 150)
        # The unique ledger reference still prevents a duplicate credit.
        credit_referrer_for_task(cursor, worker, 75, 20)
        self.assertEqual(cursor.execute("SELECT COUNT(*) FROM wallet_transactions WHERE type='referral_task_commission'").fetchone()[0], 20)


if __name__ == '__main__':
    unittest.main()
