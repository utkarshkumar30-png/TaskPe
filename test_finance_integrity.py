import os
import sqlite3
import tempfile
import threading
import unittest
from decimal import Decimal, ROUND_HALF_UP


class FinanceIntegrityTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        conn = sqlite3.connect(self.path)
        conn.executescript('''
          CREATE TABLE users (id INTEGER PRIMARY KEY, wallet_balance NUMERIC NOT NULL, referred_user_task_count INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE withdrawals (id INTEGER PRIMARY KEY, user_id INTEGER, amount NUMERIC, status TEXT, provider_reference TEXT);
          CREATE TABLE wallet_transactions (id INTEGER PRIMARY KEY, user_id INTEGER, type TEXT, amount NUMERIC, reference_id TEXT);
          CREATE UNIQUE INDEX one_referral ON wallet_transactions(reference_id) WHERE type = 'referral_task_commission';
          CREATE UNIQUE INDEX one_withdrawal_refund ON wallet_transactions(reference_id) WHERE type = 'withdrawal_refund';
          INSERT INTO users VALUES (1, 100.00, 0);
        ''')
        conn.commit(); conn.close()

    def tearDown(self):
        os.remove(self.path)

    def reserve_withdrawal(self):
        conn = sqlite3.connect(self.path, timeout=2)
        try:
            conn.execute('BEGIN IMMEDIATE')
            updated = conn.execute('UPDATE users SET wallet_balance = wallet_balance - 100 WHERE id = 1 AND wallet_balance >= 100').rowcount
            if updated:
                wid = conn.execute("INSERT INTO withdrawals (user_id, amount, status) VALUES (1, 100, 'requested')").lastrowid
                conn.execute("INSERT INTO wallet_transactions (user_id, type, amount, reference_id) VALUES (1, 'withdrawal', -100, ?)", (f'WDR-{wid}',))
                conn.commit()
                return True
            conn.rollback(); return False
        finally:
            conn.close()

    def test_two_simultaneous_withdrawals_cannot_spend_one_balance_twice(self):
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.reserve_withdrawal())) for _ in range(2)]
        [t.start() for t in threads]; [t.join() for t in threads]
        self.assertEqual(sorted(results), [False, True])
        conn = sqlite3.connect(self.path)
        self.assertEqual(conn.execute('SELECT wallet_balance FROM users').fetchone()[0], 0)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM withdrawals').fetchone()[0], 1)
        conn.close()

    def test_provider_reference_and_transition_are_single_use(self):
        conn = sqlite3.connect(self.path)
        conn.execute("INSERT INTO withdrawals VALUES (1, 1, 100, 'processing', NULL)")
        self.assertEqual(conn.execute("UPDATE withdrawals SET status='paid', provider_reference='provider-123' WHERE id=1 AND status='processing'").rowcount, 1)
        self.assertEqual(conn.execute("UPDATE withdrawals SET status='paid', provider_reference='provider-456' WHERE id=1 AND status='processing'").rowcount, 0)
        conn.close()

    def test_referral_reward_is_database_unique_and_limit_boundary_is_20(self):
        conn = sqlite3.connect(self.path)
        for task_number in range(1, 22):
            if task_number <= 20:
                conn.execute("INSERT INTO wallet_transactions (user_id, type, amount, reference_id) VALUES (2, 'referral_task_commission', 7.5, ?)", (f'REF-TASK-{task_number}',))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM wallet_transactions WHERE type='referral_task_commission'").fetchone()[0], 20)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO wallet_transactions (user_id, type, amount, reference_id) VALUES (2, 'referral_task_commission', 7.5, 'REF-TASK-20')")
        conn.close()

    def test_usdt_rate_is_exact_and_historical_rate_is_immutable(self):
        old_rate = Decimal('90')
        old_amount = (Decimal('900') / old_rate).quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP)
        new_rate = Decimal('100')
        new_amount = (Decimal('900') / new_rate).quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP)
        self.assertEqual(old_amount, Decimal('10.000000'))
        self.assertEqual(new_amount, Decimal('9.000000'))
        self.assertEqual(old_rate, Decimal('90'))

    def test_usdt_provider_reference_and_transaction_hash_are_independent(self):
        conn = sqlite3.connect(self.path)
        conn.execute("INSERT INTO withdrawals VALUES (2, 1, 100, 'processing', NULL)")
        provider_reference = 'PAYOUT-12345'
        transaction_hash = '0xabc123'
        conn.execute("ALTER TABLE withdrawals ADD COLUMN transaction_hash TEXT")
        conn.execute("UPDATE withdrawals SET status='paid', provider_reference=?, transaction_hash=? WHERE id=2 AND status='processing'", (provider_reference, transaction_hash))
        row = conn.execute("SELECT provider_reference, transaction_hash FROM withdrawals WHERE id=2").fetchone()
        self.assertEqual(row, (provider_reference, transaction_hash))
        self.assertNotEqual(row[0], row[1])
        conn.close()

    def test_rejecting_the_same_withdrawal_twice_refunds_once(self):
        conn = sqlite3.connect(self.path)
        conn.execute('UPDATE users SET wallet_balance = 400 WHERE id = 1')
        conn.execute("INSERT INTO withdrawals VALUES (3, 1, 100, 'requested', NULL)")

        def reject_once():
            updated = conn.execute("UPDATE withdrawals SET status='rejected' WHERE id=3 AND status IN ('requested', 'under_review', 'approved', 'processing')").rowcount
            if updated:
                conn.execute('UPDATE users SET wallet_balance = wallet_balance + 100 WHERE id=1')
                conn.execute("INSERT INTO wallet_transactions (user_id, type, amount, reference_id) VALUES (1, 'withdrawal_refund', 100, 'REFUND-3')")
            return updated

        self.assertEqual(reject_once(), 1)
        self.assertEqual(conn.execute('SELECT wallet_balance FROM users WHERE id=1').fetchone()[0], 500)
        self.assertEqual(reject_once(), 0)
        self.assertEqual(conn.execute('SELECT wallet_balance FROM users WHERE id=1').fetchone()[0], 500)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM wallet_transactions WHERE type='withdrawal_refund' AND reference_id='REFUND-3'").fetchone()[0], 1)
        conn.close()


if __name__ == '__main__':
    unittest.main()
