import os
import sqlite3
import tempfile
import threading
import unittest

import database


class WithdrawalIdentifierUniquenessTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        conn = sqlite3.connect(self.path)
        conn.executescript('''
            CREATE TABLE withdrawals (
                id INTEGER PRIMARY KEY,
                status TEXT NOT NULL,
                currency TEXT NOT NULL DEFAULT 'INR',
                provider_reference TEXT,
                transaction_hash TEXT
            );
        ''')
        database.ensure_withdrawal_identifier_indexes(conn)
        conn.commit()
        conn.close()

    def tearDown(self):
        os.remove(self.path)

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def test_unique_identifiers_are_accepted_and_duplicates_are_rejected(self):
        conn = self.connect()
        conn.execute("INSERT INTO withdrawals VALUES (1, 'processing', 'INR', 'PROVIDER123', NULL)")
        conn.execute("INSERT INTO withdrawals VALUES (2, 'processing', 'USDT', NULL, 'HASH123')")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO withdrawals VALUES (3, 'processing', 'INR', 'PROVIDER123', NULL)")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO withdrawals VALUES (4, 'processing', 'USDT', NULL, 'HASH123')")
        conn.close()

    def test_empty_and_null_identifiers_do_not_collide(self):
        conn = self.connect()
        conn.execute("INSERT INTO withdrawals VALUES (1, 'processing', 'INR', NULL, NULL)")
        conn.execute("INSERT INTO withdrawals VALUES (2, 'processing', 'INR', NULL, '')")
        conn.execute("INSERT INTO withdrawals VALUES (3, 'processing', 'INR', '', NULL)")
        conn.execute("INSERT INTO withdrawals VALUES (4, 'processing', 'INR', '', '')")
        conn.commit()
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM withdrawals').fetchone()[0], 4)
        conn.close()

    def test_collision_rolls_back_paid_transition(self):
        conn = self.connect()
        conn.execute("INSERT INTO withdrawals VALUES (1, 'paid', 'INR', 'PROVIDER123', NULL)")
        conn.execute("INSERT INTO withdrawals VALUES (2, 'processing', 'INR', NULL, NULL)")
        conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("UPDATE withdrawals SET status='paid', provider_reference='PROVIDER123' WHERE id=2 AND status='processing'")
        conn.rollback()
        self.assertEqual(conn.execute('SELECT status FROM withdrawals WHERE id=2').fetchone()[0], 'processing')
        conn.close()

    def test_concurrent_attempts_cannot_create_duplicate_provider_reference(self):
        barrier = threading.Barrier(2)
        results = []

        def insert_reference(row_id):
            conn = self.connect()
            try:
                barrier.wait()
                conn.execute('BEGIN IMMEDIATE')
                conn.execute(
                    "INSERT INTO withdrawals (id, status, currency, provider_reference) VALUES (?, 'paid', 'INR', ?)",
                    (row_id, 'CONCURRENT-REF'),
                )
                conn.commit()
                results.append('accepted')
            except sqlite3.IntegrityError:
                conn.rollback()
                results.append('rejected')
            finally:
                conn.close()

        threads = [threading.Thread(target=insert_reference, args=(row_id,)) for row_id in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), ['accepted', 'rejected'])
        conn = self.connect()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM withdrawals WHERE provider_reference='CONCURRENT-REF'").fetchone()[0], 1)
        conn.close()

    def test_migration_reports_existing_collisions_before_creating_indexes(self):
        conn = self.connect()
        conn.execute("DROP INDEX withdrawal_provider_reference_unique")
        conn.execute("DROP INDEX withdrawal_transaction_hash_unique")
        conn.execute("INSERT INTO withdrawals VALUES (1, 'paid', 'INR', 'DUPLICATE', NULL)")
        conn.execute("INSERT INTO withdrawals VALUES (2, 'paid', 'INR', 'DUPLICATE', NULL)")
        conn.commit()
        collisions = database.withdrawal_identifier_collisions(conn)
        self.assertEqual(collisions['provider_reference'][0]['identifier'], 'DUPLICATE')
        with self.assertRaises(RuntimeError):
            database.ensure_withdrawal_identifier_indexes(conn)
        self.assertEqual(conn.execute('PRAGMA index_list(withdrawals)').fetchall(), [])
        conn.close()


if __name__ == '__main__':
    unittest.main()
