import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = tempfile.mkdtemp(prefix='taskpe-recipient-release-')
os.environ['TASKPE_DB_PATH'] = os.path.join(ROOT, 'release.db')
os.environ['ENVIRONMENT'] = 'development'
os.environ['SECRET_KEY'] = 'recipient-release-test-secret'
os.environ['SMS_PROVIDER'] = 'disabled'

import database
import app as app_module


class RecipientAllocationReleaseTests(unittest.TestCase):
    def setUp(self):
        database.DB_PATH = os.path.join(ROOT, 'release.db')
        if os.path.exists(database.DB_PATH):
            os.remove(database.DB_PATH)
        database.init_db()
        conn = database.get_db_connection()
        self.workers = []
        for index in range(2):
            self.workers.append(conn.execute('''
                INSERT INTO users (name, mobile, email, password_hash, referral_code)
                VALUES (?, ?, ?, 'hash', ?)
            ''', (f'Release Worker {index}', f'91980000010{index}', f'release{index}@example.com', f'REL{index}')).lastrowid)
        numbers = [f'91970000000{index}' for index in range(5)]
        self.task_id = conn.execute('''
            INSERT INTO tasks (title, category, description, payout, template_text,
                target_numbers, campaign_id, recipients_per_slot, time_limit_minutes,
                proof_instructions, total_slots, status)
            VALUES ('Release campaign', 'Direct Message', 'd', 6, 'm', ?, ?, 2, 25, 'p', 2, 'active')
        ''', (json.dumps(numbers), 9001)).lastrowid
        conn.commit()
        conn.close()
        self.numbers = numbers

    def _assignment(self, user_id, status='in_progress', expires_at=None):
        conn = database.get_db_connection()
        assignment_id = conn.execute('''
            INSERT INTO task_assignments
                (user_id, task_id, campaign_id, campaign_type, assigned_numbers,
                 status, expires_at, slot_released)
            VALUES (?, ?, 9001, 'Direct Message', ?, ?, ?, 0)
        ''', (user_id, self.task_id, json.dumps(self.numbers), status, expires_at)).lastrowid
        conn.executemany('''
            INSERT INTO recipient_allocations
                (campaign_id, phone_number, assignment_id, user_id, status)
            VALUES (9001, ?, ?, ?, 'ALLOCATED')
        ''', [(number, assignment_id, user_id) for number in self.numbers])
        conn.commit()
        conn.close()
        return assignment_id

    def _take(self, user_id):
        client = app_module.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = user_id
        return client.post(f'/api/tasks/{self.task_id}/take')

    def test_submission_settlement_releases_unverified_and_reuses_only_released(self):
        assignment_id = self._assignment(self.workers[0], status='paid')
        conn = database.get_db_connection()
        for index, number in enumerate(self.numbers):
            conn.execute('''
                INSERT INTO target_number_records
                    (assignment_id, task_id, campaign_id, user_id, target_number,
                     normalized_target_number, status)
                VALUES (?, ?, 9001, ?, ?, ?, ?)
            ''', (assignment_id, self.task_id, self.workers[0], number, number,
                  'verified' if index < 3 else 'assigned'))
        app_module.settle_recipient_allocations_for_approval(conn, assignment_id)
        conn.execute("UPDATE task_assignments SET slot_released = 1 WHERE id = ?", (assignment_id,))
        conn.commit()
        states = dict(conn.execute('SELECT phone_number, status FROM recipient_allocations').fetchall())
        self.assertEqual([states[number] for number in self.numbers[:3]], ['COMPLETED'] * 3)
        self.assertEqual([states[number] for number in self.numbers[3:]], ['RELEASED'] * 2)
        conn.close()

        response = self._take(self.workers[1])
        self.assertEqual(response.status_code, 200, response.get_json())
        conn = database.get_db_connection()
        current = {row['phone_number'] for row in conn.execute('''
            SELECT phone_number FROM recipient_allocations
            WHERE assignment_id != ? AND status = 'ALLOCATED'
        ''', (assignment_id,))}
        self.assertEqual(current, set(self.numbers[3:]))
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM recipient_allocations').fetchone()[0], 7)
        conn.close()

    def test_expiry_releases_every_allocation_and_next_worker_can_take_slot(self):
        assignment_id = self._assignment(self.workers[0], expires_at='2000-01-01 00:00:00')
        conn = database.get_db_connection()
        app_module.release_expired_assignments(conn)
        assignment = conn.execute('SELECT status, slot_released FROM task_assignments WHERE id = ?', (assignment_id,)).fetchone()
        states = [row[0] for row in conn.execute('SELECT status FROM recipient_allocations WHERE assignment_id = ? ORDER BY id', (assignment_id,))]
        self.assertEqual((assignment['status'], assignment['slot_released']), ('expired', 1))
        self.assertEqual(states, ['RELEASED'] * 5)
        conn.close()
        response = self._take(self.workers[1])
        self.assertEqual(response.status_code, 200, response.get_json())

    def test_different_campaign_can_reuse_number_immediately(self):
        first_assignment = self._assignment(self.workers[0], status='paid')
        conn = database.get_db_connection()
        conn.execute("UPDATE recipient_allocations SET status = 'COMPLETED' WHERE assignment_id = ?", (first_assignment,))
        second_task = conn.execute('''
            INSERT INTO tasks (title, category, description, payout, template_text,
                target_numbers, campaign_id, recipients_per_slot, time_limit_minutes,
                proof_instructions, total_slots, status)
            VALUES ('Second campaign', 'Direct Message', 'd', 2, 'm', ?, 9002, 1, 25, 'p', 1, 'active')
        ''', (json.dumps([self.numbers[0]]),)).lastrowid
        conn.commit()
        conn.close()
        client = app_module.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = self.workers[1]
        response = client.post(f'/api/tasks/{second_task}/take')
        self.assertEqual(response.status_code, 200, response.get_json())
        conn = database.get_db_connection()
        self.assertEqual(conn.execute('''
            SELECT COUNT(*) FROM recipient_allocations
            WHERE campaign_id = 9002 AND phone_number = ? AND status = 'ALLOCATED'
        ''', (self.numbers[0],)).fetchone()[0], 1)
        conn.close()

    def test_new_text_task_uses_target_numbers_without_target_records(self):
        response = self._take(self.workers[0])
        self.assertEqual(response.status_code, 200, response.get_json())
        conn = database.get_db_connection()
        self.assertEqual(conn.execute('''
            SELECT COUNT(*) FROM recipient_allocations
            WHERE campaign_id = 9001 AND status = 'ALLOCATED'
        ''').fetchone()[0], 2)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM target_number_records').fetchone()[0], 0)
        conn.close()

    def test_new_image_text_link_task_uses_target_numbers_without_target_records(self):
        conn = database.get_db_connection()
        image_task = conn.execute('''
            INSERT INTO tasks (title, category, task_type, description, payout, template_text,
                target_numbers, campaign_id, recipients_per_slot, time_limit_minutes,
                proof_instructions, total_slots, status)
            VALUES ('Image campaign', 'IMAGE_TEXT_LINK', 'IMAGE_TEXT_LINK', 'd', 4, 'm', ?, 9010, 4, 25, 'p', 1, 'active')
        ''', (json.dumps(self.numbers[:4]),)).lastrowid
        conn.commit()
        conn.close()
        client = app_module.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = self.workers[0]
        response = client.post(f'/api/tasks/{image_task}/take')
        self.assertEqual(response.status_code, 200, response.get_json())
        conn = database.get_db_connection()
        self.assertEqual(conn.execute('''
            SELECT COUNT(*) FROM recipient_allocations
            WHERE campaign_id = 9010 AND status = 'ALLOCATED'
        ''').fetchone()[0], 4)
        self.assertEqual(conn.execute('''
            SELECT COUNT(*) FROM target_number_records WHERE task_id = ?
        ''', (image_task,)).fetchone()[0], 0)
        conn.close()
    def test_migration_cli_preserves_rows_and_uses_explicit_disposable_path(self):
        handle, path = tempfile.mkstemp(prefix='taskpe-allocation-migration-', suffix='.db')
        os.close(handle)
        try:
            conn = sqlite3.connect(path)
            conn.executescript('''
                CREATE TABLE task_assignments (id INTEGER PRIMARY KEY);
                CREATE TABLE users (id INTEGER PRIMARY KEY);
                CREATE TABLE recipient_allocations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    campaign_id INTEGER NOT NULL, phone_number TEXT NOT NULL,
                    assignment_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                    last_contacted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(campaign_id, phone_number)
                );
                INSERT INTO recipient_allocations (campaign_id, phone_number, assignment_id, user_id)
                VALUES (1, '919700000001', 1, 1);
            ''')
            conn.commit()
            conn.close()
            command = [sys.executable, 'migrate_recipient_allocation_history.py', '--db', path]
            first = subprocess.run(command, cwd=os.path.dirname(__file__), check=True, capture_output=True, text=True)
            second = subprocess.run(command, cwd=os.path.dirname(__file__), check=True, capture_output=True, text=True)
            report = json.loads(first.stdout)
            self.assertEqual(report['allocation_rows_before'], 1)
            self.assertEqual(report['allocation_rows_after'], 1)
            self.assertTrue(report['active_unique_index'])
            self.assertIn('allocation_rows_after', second.stdout)
            conn = sqlite3.connect(path)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM recipient_allocations').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT status FROM recipient_allocations').fetchone()[0], 'ALLOCATED')
            conn.close()
        finally:
            os.remove(path)


if __name__ == '__main__':
    unittest.main()
