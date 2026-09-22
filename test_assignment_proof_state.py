import base64
import importlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch


ROOT = tempfile.mkdtemp(prefix='taskpe-assignment-proof-state-')
os.environ['TASKPE_DB_PATH'] = os.path.join(ROOT, 'taskpe.db')
os.environ['TASKPE_UPLOAD_FOLDER'] = os.path.join(ROOT, 'proofs')
os.environ['TASKPE_TASK_ASSET_FOLDER'] = os.path.join(ROOT, 'assets')
os.environ['ENVIRONMENT'] = 'development'
os.environ['SECRET_KEY'] = 'assignment-proof-state-test-secret'

import database
import app as app_module
from werkzeug.security import generate_password_hash


class AssignmentProofStateTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.mkdtemp(prefix='taskpe-assignment-proof-state-case-')
        database.DB_PATH = os.path.join(self.tempdir, 'taskpe.db')
        os.environ['TASKPE_DB_PATH'] = database.DB_PATH
        os.environ['TASKPE_UPLOAD_FOLDER'] = os.path.join(self.tempdir, 'proofs')
        os.environ['TASKPE_TASK_ASSET_FOLDER'] = os.path.join(self.tempdir, 'assets')
        database.initialize_database()
        global app_module
        app_module = importlib.reload(app_module)
        app_module.app.config['TESTING'] = True
        self.client = app_module.app.test_client()
        self.worker_id = self._user('Worker', '919999991001', 'worker@example.test', 'user')
        self.admin_id = self._user('Admin', '919999991002', 'admin@example.test', 'admin')
        self.task_id = self._task()
        self._complete_first_slot()
        self._login_as(self.worker_id)

    def tearDown(self):
        shutil.rmtree(self.tempdir, ignore_errors=True)

    def _conn(self):
        return database.get_db_connection()

    def _user(self, name, mobile, email, role):
        conn = self._conn()
        user_id = conn.execute('''
            INSERT INTO users (name, mobile, email, password_hash, role, referral_code)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (name, mobile, email, generate_password_hash('password'), role, mobile[-6:])).lastrowid
        conn.commit()
        conn.close()
        return user_id

    def _task(self):
        numbers = [f'919700{i:07d}' for i in range(72)]
        conn = self._conn()
        task_id = conn.execute('''
            INSERT INTO tasks
                (title, category, description, payout, reward_per_number_paise,
                 template_text, target_numbers, campaign_id, recipients_per_slot,
                 time_limit_minutes, proof_instructions, total_slots, completed_slots, status)
            VALUES (?, 'Direct Message', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
        ''', (
            'Repeated campaign', 'Send the message', 75, 300, 'Hello',
            json.dumps(numbers), 1, 25, 25, 'Upload proof', 3, 1
        )).lastrowid
        conn.commit()
        conn.close()
        return task_id

    def _login_as(self, user_id):
        with self.client.session_transaction() as session:
            session.clear()
            session['user_id'] = user_id

    def _complete_first_slot(self):
        first_numbers = [f'919700{i:07d}' for i in range(22)]
        conn = self._conn()
        assignment_id = conn.execute('''
            INSERT INTO task_assignments
                (user_id, task_id, campaign_id, campaign_type, assigned_numbers, status, completed_at)
            VALUES (?, ?, ?, 'Direct Message', ?, 'paid', CURRENT_TIMESTAMP)
        ''', (self.worker_id, self.task_id, self.task_id, json.dumps(first_numbers))).lastrowid
        conn.execute('''
            INSERT INTO submissions
                (user_id, task_id, assignment_id, proof_image, status, payout_amount,
                 payout_paise, numbers_required, numbers_detected, required_count,
                 verified_count, per_number_rate, per_number_rate_paise)
            VALUES (?, ?, ?, 'slot-1.png', 'approved', '66.00', 6600, 22, 22, 22, 22, '3.00', 300)
        ''', (self.worker_id, self.task_id, assignment_id))
        conn.executemany('''
            INSERT INTO recipient_allocations
                (campaign_id, phone_number, assignment_id, user_id, status)
            VALUES (?, ?, ?, ?, 'COMPLETED')
        ''', [(self.task_id, number, assignment_id, self.worker_id) for number in first_numbers])
        conn.commit()
        conn.close()

    def test_new_slot_uses_assignment_state_and_gets_separate_reward(self):
        task_list = self.client.get('/api/tasks').get_json()['tasks']
        task = next(item for item in task_list if item['id'] == self.task_id)
        self.assertEqual(task['current_assignment_id'], self._latest_assignment_id())
        self.assertEqual(task['current_assignment_status'], 'paid')

        taken = self.client.post(f'/api/tasks/{self.task_id}/take')
        self.assertEqual(taken.status_code, 200)
        second_assignment_id = taken.get_json()['assignment_id']

        detail = self.client.get(f'/api/tasks/{self.task_id}').get_json()['task']
        self.assertEqual(detail['assignment_id'], second_assignment_id)
        self.assertEqual(detail['assignment_status'], 'in_progress')
        self.assertIsNone(detail['user_submission'])
        self.assertEqual(len(detail['target_numbers']), 25)

        with open(os.path.join(os.path.dirname(__file__), 'static', 'js', 'app.js'), encoding='utf-8') as source_file:
            source = source_file.read()
        self.assertIn("if (assignmentIsActive) {", source)
        self.assertIn("subForm.classList.remove('hidden');", source)

        audit = {
            'audit_badge': 'PASS', 'match_percentage': 100,
            'numbers_found': 25, 'total_targets': 25,
            'invalid_count': 0, 'matched_numbers': detail['target_numbers'],
            'invalid_numbers': [], 'audit_passed': True,
        }
        png = base64.b64decode(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk'
            '+A8AAQUBAScY42YAAAAASUVORK5CYII='
        )
        with patch.object(app_module.ocr_scanner, 'audit_screenshot_proof', return_value=audit):
            submitted = self.client.post(
                f'/api/tasks/{self.task_id}/submit',
                data={'proof_images': (io.BytesIO(png), 'slot-2.png'), 'notes': 'slot 2'},
                content_type='multipart/form-data'
            )
        self.assertEqual(submitted.status_code, 200)
        second_submission_id = submitted.get_json()['submission_id']

        conn = self._conn()
        submissions = conn.execute('''
            SELECT id, assignment_id, status, payout_paise
            FROM submissions WHERE task_id = ? ORDER BY id
        ''', (self.task_id,)).fetchall()
        self.assertEqual(len(submissions), 2)
        self.assertEqual(submissions[0]['status'], 'approved')
        self.assertEqual(submissions[0]['payout_paise'], 6600)
        self.assertEqual(submissions[1]['id'], second_submission_id)
        self.assertEqual(submissions[1]['assignment_id'], second_assignment_id)
        conn.close()

        self._login_as(self.admin_id)
        approved = self.client.post(f'/api/admin/submissions/{second_submission_id}/approve')
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.get_json()['payout'], 75.0)

        conn = self._conn()
        second = conn.execute(
            'SELECT status, payout_paise FROM submissions WHERE id = ?',
            (second_submission_id,)
        ).fetchone()
        self.assertEqual(second['status'], 'approved')
        self.assertEqual(second['payout_paise'], 7500)
        reward = conn.execute('''
            SELECT amount_paise FROM wallet_transactions
            WHERE submission_id = ? AND type = 'task_reward'
        ''', (second_submission_id,)).fetchone()
        self.assertEqual(reward['amount_paise'], 7500)
        conn.close()

        self._login_as(self.worker_id)
        third_taken = self.client.post(f'/api/tasks/{self.task_id}/take')
        self.assertEqual(third_taken.status_code, 200)
        third_assignment_id = third_taken.get_json()['assignment_id']
        self.assertNotEqual(third_assignment_id, second_assignment_id)
        third_detail = self.client.get(f'/api/tasks/{self.task_id}').get_json()['task']
        self.assertEqual(third_detail['assignment_id'], third_assignment_id)
        self.assertEqual(third_detail['assignment_status'], 'in_progress')
        self.assertIsNone(third_detail['user_submission'])

    def test_expired_assignment_cannot_submit_proof(self):
        conn = self._conn()
        expired_task_id = conn.execute('''
            INSERT INTO tasks
                (title, category, description, payout, reward_per_number_paise,
                 template_text, target_numbers, campaign_id, recipients_per_slot,
                 time_limit_minutes, proof_instructions, total_slots, status)
            VALUES ('Expired task', 'Expired Campaign', 'Expired', 3, 300,
                    'Hello', '["919799999999"]', 2, 1, 15, 'Upload proof', 1, 'active')
        ''').lastrowid
        assignment_id = conn.execute('''
            INSERT INTO task_assignments
                (user_id, task_id, campaign_id, campaign_type, assigned_numbers, status, expires_at)
            VALUES (?, ?, ?, 'Expired Campaign', '["919799999999"]', 'in_progress', datetime('now', '-1 minute'))
        ''', (self.worker_id, expired_task_id, expired_task_id)).lastrowid
        conn.commit()
        conn.close()

        self._login_as(self.worker_id)
        response = self.client.post(
            f'/api/tasks/{expired_task_id}/submit',
            data={'proof_images': (io.BytesIO(self._png_bytes()), 'expired.png')},
            content_type='multipart/form-data'
        )
        self.assertEqual(response.status_code, 400)
        conn = self._conn()
        self.assertEqual(
            conn.execute('SELECT status FROM task_assignments WHERE id = ?', (assignment_id,)).fetchone()['status'],
            'expired'
        )
        self.assertEqual(
            conn.execute('SELECT COUNT(*) FROM submissions WHERE assignment_id = ?', (assignment_id,)).fetchone()[0],
            0
        )
        conn.close()

    @staticmethod
    def _png_bytes():
        return base64.b64decode(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk'
            '+A8AAQUBAScY42YAAAAASUVORK5CYII='
        )

    def _latest_assignment_id(self):
        conn = self._conn()
        assignment_id = conn.execute('''
            SELECT id FROM task_assignments
            WHERE user_id = ? AND task_id = ? ORDER BY id DESC LIMIT 1
        ''', (self.worker_id, self.task_id)).fetchone()['id']
        conn.close()
        return assignment_id


if __name__ == '__main__':
    unittest.main()
