import os
import shutil
import sqlite3
import tempfile
import unittest


_ROOT = tempfile.mkdtemp(prefix='taskpe-target-records-')
os.environ['TASKPE_DB_PATH'] = os.path.join(_ROOT, 'targets.db')
os.environ['ENVIRONMENT'] = 'production'
os.environ['SECRET_KEY'] = 'target-record-test-secret'

import database
from app import finalize_target_records_for_approval, get_reusable_target_numbers


class TargetNumberRecordTests(unittest.TestCase):
    def setUp(self):
        database.DB_PATH = os.path.join(_ROOT, 'targets.db')
        if os.path.exists(database.DB_PATH):
            os.remove(database.DB_PATH)
        database.init_db()
        conn = database.get_db_connection()
        user_id = conn.execute('''
            INSERT INTO users (name, mobile, email, password_hash, referral_code)
            VALUES ('Worker', '919876543210', 'target-worker@example.com', 'hash', 'TARGET1')
        ''').lastrowid
        task_id = conn.execute('''
            INSERT INTO tasks (title, category, description, payout, template_text, target_numbers, campaign_id,
                               recipients_per_slot, time_limit_minutes, proof_instructions, total_slots)
            VALUES ('Target task', 'Direct Message', 'Target test', 75, 'Hello', '[]', 1, 25, 25, 'Upload proof', 1)
        ''').lastrowid
        assignment_id = conn.execute('''
            INSERT INTO task_assignments (user_id, task_id, campaign_id, campaign_type, assigned_numbers)
            VALUES (?, ?, ?, 'Direct Message', ?)
        ''', (user_id, task_id, task_id, '[]')).lastrowid
        conn.commit()
        conn.close()
        self.user_id = user_id
        self.task_id = task_id
        self.assignment_id = assignment_id

    def tearDown(self):
        if os.path.exists(database.DB_PATH):
            os.remove(database.DB_PATH)

    def _insert_records(self, count=25):
        conn = database.get_db_connection()
        rows = []
        for index in range(count):
            raw = f'+91 98765 {index:05d}'
            rows.append((self.assignment_id, self.task_id, self.task_id, self.user_id, raw,
                         database.normalize_target_number(raw), 'assigned', 2.5))
        conn.executemany('''
            INSERT INTO target_number_records
                (assignment_id, task_id, campaign_id, user_id, target_number,
                 normalized_target_number, status, reward_per_number)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', rows)
        conn.commit()
        return conn

    def _insert_submission(self, conn, payout=0):
        return conn.execute('''
            INSERT INTO submissions
                (user_id, task_id, assignment_id, proof_image, notes, status,
                 admin_feedback, payout_amount, numbers_detected, numbers_required,
                 required_count, verified_count, per_number_rate)
            VALUES (?, ?, ?, '', '', 'pending', '', ?, 0, 25, 25, 0, 2.5)
        ''', (self.user_id, self.task_id, self.assignment_id, payout)).lastrowid

    def test_twenty_five_individual_records_are_identifiable_and_normalized(self):
        conn = self._insert_records()
        records = conn.execute('''
            SELECT target_number, normalized_target_number, status, reward_per_number
            FROM target_number_records WHERE assignment_id = ? ORDER BY id
        ''', (self.assignment_id,)).fetchall()
        self.assertEqual(len(records), 25)
        self.assertEqual(records[0]['normalized_target_number'], '919876500000')
        self.assertEqual(records[0]['status'], 'assigned')
        self.assertEqual(records[0]['reward_per_number'], 2.5)
        self.assertEqual(len({row['normalized_target_number'] for row in records}), 25)
        conn.close()

    def test_duplicate_normalized_target_for_assignment_is_rejected(self):
        conn = self._insert_records(1)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute('''
                INSERT INTO target_number_records
                    (assignment_id, task_id, campaign_id, user_id, target_number, normalized_target_number)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (self.assignment_id, self.task_id, self.task_id, self.user_id,
                  '098765000000', '919876500000'))
        conn.rollback()
        conn.close()

    def test_status_and_paid_amount_are_independent_per_number(self):
        conn = self._insert_records()
        conn.execute("UPDATE target_number_records SET status = 'verified', verified_at = CURRENT_TIMESTAMP WHERE id <= 20")
        conn.execute("UPDATE target_number_records SET status = 'paid', paid_amount = ?, paid_at = CURRENT_TIMESTAMP WHERE id <= 10", (2.5,))
        conn.execute("UPDATE target_number_records SET status = 'remaining' WHERE id > 20")
        conn.commit()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM target_number_records WHERE status = 'verified'").fetchone()[0], 10)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM target_number_records WHERE status = 'paid'").fetchone()[0], 10)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM target_number_records WHERE status = 'remaining'").fetchone()[0], 5)
        self.assertEqual(conn.execute("SELECT paid_amount FROM target_number_records WHERE status = 'paid' LIMIT 1").fetchone()[0], 2.5)
        conn.close()

    def test_migration_is_idempotent(self):
        database.init_db()
        database.init_db()
        conn = database.get_db_connection()
        self.assertIsNotNone(conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'target_number_records'").fetchone())
        self.assertIsNotNone(conn.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'target_number_records_assignment_status'").fetchone())
        conn.close()

    def test_partial_approval_preserves_exact_remaining_targets(self):
        conn = self._insert_records()
        conn.execute("UPDATE target_number_records SET status = 'verified' WHERE id <= 20")
        conn.commit()
        finalize_target_records_for_approval(conn, self.assignment_id, 20, 2.5)
        conn.commit()
        paid = {row[0] for row in conn.execute("SELECT target_number FROM target_number_records WHERE status = 'paid'")}
        remaining = {row[0] for row in conn.execute("SELECT target_number FROM target_number_records WHERE status = 'remaining'")}
        self.assertEqual(len(paid), 20)
        self.assertEqual(len(remaining), 5)
        self.assertEqual(remaining, {f'+91 98765 {index:05d}' for index in range(20, 25)})
        conn.close()

    def test_full_approval_leaves_no_remaining_targets(self):
        conn = self._insert_records()
        conn.execute("UPDATE target_number_records SET status = 'verified'")
        conn.commit()
        finalize_target_records_for_approval(conn, self.assignment_id, 25, 2.5)
        conn.commit()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM target_number_records WHERE status = 'remaining'").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM target_number_records WHERE status = 'paid'").fetchone()[0], 25)
        conn.close()

    def test_reusable_selector_returns_remaining_only_and_preserves_history(self):
        conn = self._insert_records()
        conn.execute("UPDATE target_number_records SET status = 'remaining' WHERE id > 20")
        conn.execute("UPDATE target_number_records SET status = 'paid' WHERE id = 1")
        conn.commit()
        candidates = [f'9198765{index:05d}' for index in range(25)]
        reusable = get_reusable_target_numbers(conn, campaign_id=99, candidate_numbers=candidates, limit=10)
        self.assertEqual(len(reusable), 5)
        self.assertNotIn('919876500000', reusable)
        old_assignment = conn.execute("SELECT assignment_id FROM target_number_records WHERE status = 'paid' LIMIT 1").fetchone()[0]
        self.assertEqual(old_assignment, self.assignment_id)
        conn.close()

    def test_remaining_target_bypasses_cooldown(self):
        conn = self._insert_records()
        conn.execute("UPDATE target_number_records SET status = 'remaining' WHERE id = 21")
        conn.execute('''
            INSERT INTO recipient_allocations
                (campaign_id, phone_number, assignment_id, user_id, last_contacted_at)
            VALUES (10, '919876500020', ?, ?, CURRENT_TIMESTAMP)
        ''', (self.assignment_id, self.user_id))
        conn.commit()
        reusable = get_reusable_target_numbers(conn, campaign_id=99, candidate_numbers=['919876500020'])
        self.assertEqual(reusable, ['919876500020'])
        conn.close()

    def test_remaining_target_can_be_reused_immediately_by_another_worker(self):
        conn = self._insert_records()
        conn.execute("UPDATE target_number_records SET status = 'remaining' WHERE id = 21")
        other_id = conn.execute('''
            INSERT INTO users (name, mobile, email, password_hash, referral_code)
            VALUES ('Worker Two', '919876543211', 'target-worker-two@example.com', 'hash', 'TARGET2')
        ''').lastrowid
        reusable = get_reusable_target_numbers(conn, campaign_id=99, candidate_numbers=['919876500020'], user_id=other_id)
        self.assertEqual(reusable, ['919876500020'])
        conn.close()

    def test_remaining_target_still_cannot_be_double_allocated(self):
        conn = self._insert_records()
        conn.execute("UPDATE target_number_records SET status = 'remaining' WHERE id = 21")
        other_id = conn.execute('''
            INSERT INTO users (name, mobile, email, password_hash, referral_code)
            VALUES ('Worker Three', '919876543212', 'target-worker-three@example.com', 'hash', 'TARGET3')
        ''').lastrowid
        other_assignment = conn.execute('''
            INSERT INTO task_assignments (user_id, task_id, campaign_id, campaign_type, assigned_numbers, status)
            VALUES (?, ?, 99, 'Broadcast', '["919876500020"]', 'in_progress')
        ''', (other_id, self.task_id)).lastrowid
        conn.execute('''
            INSERT INTO target_number_records
                (assignment_id, task_id, campaign_id, user_id, target_number, normalized_target_number, status)
            VALUES (?, ?, 99, ?, '919876500020', '919876500020', 'assigned')
        ''', (other_assignment, self.task_id, other_id))
        conn.commit()
        self.assertEqual(get_reusable_target_numbers(conn, campaign_id=101, candidate_numbers=['919876500020']), [])
        conn.close()

    def test_target_payments_are_unique_and_idempotent(self):
        conn = self._insert_records()
        conn.execute("UPDATE target_number_records SET status = 'verified' WHERE id <= 20")
        submission_id = self._insert_submission(conn)
        conn.commit()
        finalize_target_records_for_approval(
            conn, self.assignment_id, 20, 2.5, submission_id=submission_id,
            task_id=self.task_id, user_id=self.user_id
        )
        conn.commit()
        # A retry of the same target finalization is a no-op for already-paid
        # targets and cannot create duplicate payment identities.
        finalize_target_records_for_approval(
            conn, self.assignment_id, 20, 2.5, submission_id=submission_id,
            task_id=self.task_id, user_id=self.user_id
        )
        conn.commit()
        rows = conn.execute('SELECT * FROM target_payments WHERE submission_id = ?', (submission_id,)).fetchall()
        self.assertEqual(len(rows), 20)
        self.assertEqual(len({row['target_number_record_id'] for row in rows}), 20)
        self.assertTrue(all(row['amount'] == 2.5 and row['rate'] == 2.5 for row in rows))
        conn.close()

    def test_target_payment_failure_rolls_back_target_status(self):
        conn = self._insert_records(1)
        conn.execute("UPDATE target_number_records SET status = 'verified' WHERE id = 1")
        submission_id = self._insert_submission(conn)
        conn.commit()
        # Force the target-payment uniqueness failure inside the transaction.
        conn.execute('''
            INSERT INTO target_payments
                (target_number_record_id, assignment_id, task_id, user_id, submission_id,
                 target_number, normalized_target_number, amount, rate, payment_reference)
            VALUES (1, ?, ?, ?, ?, '+91 98765 00000', '919876500000', 2.5, 2.5, 'TARGET-PAY-1')
        ''', (self.assignment_id, self.task_id, self.user_id, submission_id))
        conn.commit()
        conn.execute('BEGIN IMMEDIATE')
        with self.assertRaises(sqlite3.IntegrityError):
            finalize_target_records_for_approval(
                conn, self.assignment_id, 1, 2.5, submission_id=submission_id,
                task_id=self.task_id, user_id=self.user_id
            )
        conn.rollback()
        self.assertEqual(conn.execute('SELECT status FROM target_number_records WHERE id = 1').fetchone()[0], 'verified')
        conn.close()

    def test_exact_twenty_two_of_twenty_five_leaves_three_reusable(self):
        conn = self._insert_records(25)
        conn.execute("UPDATE target_number_records SET status = 'verified' WHERE id <= 22")
        submission_id = self._insert_submission(conn)
        conn.commit()
        finalize_target_records_for_approval(
            conn, self.assignment_id, 22, 3.0, submission_id=submission_id,
            task_id=self.task_id, user_id=self.user_id
        )
        conn.commit()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM target_number_records WHERE status = 'paid'").fetchone()[0], 22)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM target_number_records WHERE status = 'remaining'").fetchone()[0], 3)
        self.assertEqual(conn.execute("SELECT SUM(amount) FROM target_payments WHERE submission_id = ?", (submission_id,)).fetchone()[0], 66.0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM target_payments WHERE submission_id = ?", (submission_id,)).fetchone()[0], 22)
        conn.close()

    def test_paid_target_record_is_immutable(self):
        conn = self._insert_records(1)
        conn.execute("UPDATE target_number_records SET status = 'paid', paid_amount = 2.5 WHERE id = 1")
        conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE target_number_records SET assignment_id = ? WHERE id = 1", (999,))
        conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE target_number_records SET status = 'remaining' WHERE id = 1")
        conn.rollback()
        self.assertEqual(conn.execute('SELECT status, assignment_id FROM target_number_records WHERE id = 1').fetchone()['status'], 'paid')
        conn.close()

    def test_campaign_allocation_is_unique_but_cross_campaign_reuse_is_allowed(self):
        conn = database.get_db_connection()
        conn.execute('''
            INSERT INTO recipient_allocations (campaign_id, phone_number, assignment_id, user_id)
            VALUES (1, '919876500000', ?, ?)
        ''', (self.assignment_id, self.user_id))
        conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute('''
                INSERT INTO recipient_allocations (campaign_id, phone_number, assignment_id, user_id)
                VALUES (1, '919876500000', ?, ?)
            ''', (self.assignment_id, self.user_id))
        conn.rollback()
        # A different campaign has a distinct allocation history and is valid.
        conn.execute('''
            INSERT INTO recipient_allocations (campaign_id, phone_number, assignment_id, user_id)
            VALUES (2, '919876500000', ?, ?)
        ''', (self.assignment_id, self.user_id))
        conn.commit()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM recipient_allocations WHERE phone_number = '919876500000'").fetchone()[0], 2)
        conn.close()


if __name__ == '__main__':
    unittest.main()
