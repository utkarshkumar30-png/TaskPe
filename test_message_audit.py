"""Regression coverage for the non-invasive per-recipient audit trail."""

import base64
import importlib
import io
import json
import os
import sqlite3
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch


_ROOT = tempfile.mkdtemp(prefix='taskpe-message-audit-')
os.environ['TASKPE_DB_PATH'] = os.path.join(_ROOT, 'audit.db')
os.environ['ENVIRONMENT'] = 'production'
os.environ['SECRET_KEY'] = 'message-audit-test-secret'
os.environ['TASKPE_UPLOAD_FOLDER'] = os.path.join(_ROOT, 'proofs')
os.environ['TASKPE_TASK_ASSET_FOLDER'] = os.path.join(_ROOT, 'assets')

import database

database.init_db()
import app as app_module
from werkzeug.security import generate_password_hash


class MessageAuditTests(unittest.TestCase):
    _PNG = base64.b64decode(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk'
        '+A8AAQUBAScY42YAAAAASUVORK5CYII='
    )

    def setUp(self):
        self.tempdir = tempfile.mkdtemp(prefix='taskpe-message-audit-case-')
        self.db_path = os.path.join(self.tempdir, 'audit.db')
        self.old_db_path = database.DB_PATH
        database.DB_PATH = self.db_path
        os.environ['TASKPE_UPLOAD_FOLDER'] = os.path.join(self.tempdir, 'proofs')
        os.environ['TASKPE_TASK_ASSET_FOLDER'] = os.path.join(self.tempdir, 'assets')
        database.init_db()
        global app_module
        app_module = importlib.reload(app_module)
        app_module.app.config['TESTING'] = True
        self.client = app_module.app.test_client()

        conn = self._conn()
        self.admin_id = self._user(conn, 'Admin', '919999991001', 'admin')
        self.worker_id = self._user(conn, 'Worker', '919999991002', 'user')
        self.worker_two_id = self._user(conn, 'Worker Two', '919999991003', 'user')
        self.numbers = [f'919700000{index:03d}' for index in range(1, 51)]
        self.task_id = conn.execute('''
            INSERT INTO tasks (
                title, category, description, payout, payout_paise,
                template_text, destination_link, target_numbers, campaign_id,
                recipients_per_slot, time_limit_minutes, proof_instructions,
                total_slots, completed_slots, status, reward_per_number_paise
            ) VALUES (?, 'Direct Message', ?, 75, 7500, ?, ?, ?, NULL, 25, 25, ?, 2, 0, 'active', 300)
        ''', (
            'Audit task', 'Audit description', 'Hello audit recipient',
            'https://example.test/audit', json.dumps(self.numbers), 'Upload proof',
        )).lastrowid
        conn.execute('UPDATE tasks SET campaign_id = ? WHERE id = ?', (self.task_id, self.task_id))
        self.assignment_id = self._create_assignment(conn, self.worker_id, self.numbers[:25])
        conn.commit()
        conn.close()

    def tearDown(self):
        database.DB_PATH = self.old_db_path
        shutil.rmtree(self.tempdir, ignore_errors=True)

    def _conn(self):
        return database.get_db_connection()

    def _user(self, conn, name, mobile, role):
        return conn.execute('''
            INSERT INTO users (name, mobile, email, password_hash, role, referral_code)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (name, mobile, f'{mobile}@example.test', generate_password_hash('password'), role, mobile[-6:])).lastrowid

    def _create_assignment(self, conn, user_id, numbers):
        assignment_id = conn.execute('''
            INSERT INTO task_assignments
                (user_id, task_id, campaign_id, campaign_type, assigned_numbers,
                 status, expires_at, slot_released)
            VALUES (?, ?, ?, 'Direct Message', ?, 'in_progress', datetime('now', '+25 minutes'), 0)
        ''', (user_id, self.task_id, self.task_id, json.dumps(numbers))).lastrowid
        conn.executemany('''
            INSERT INTO recipient_allocations
                (campaign_id, phone_number, assignment_id, user_id, status)
            VALUES (?, ?, ?, ?, 'ALLOCATED')
        ''', [(self.task_id, number, assignment_id, user_id) for number in numbers])
        return assignment_id

    def _as(self, user_id):
        with self.client.session_transaction() as session:
            session.clear()
            session['user_id'] = user_id

    def _audit_click(self, recipient):
        return self.client.post(
            f'/api/tasks/{self.task_id}/recipient-audit',
            json={'assignment_id': self.assignment_id, 'recipient_number': recipient},
        )

    def test_expired_in_progress_assignment_cannot_create_audit_record(self):
        self._as(self.worker_id)
        conn = self._conn()
        conn.execute(
            "UPDATE task_assignments SET status = 'in_progress', expires_at = datetime('now', '-1 minute') WHERE id = ?",
            (self.assignment_id,),
        )
        conn.commit()
        conn.close()

        response = self._audit_click(self.numbers[0])

        self.assertEqual(response.status_code, 404)
        conn = self._conn()
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM message_audit_records').fetchone()[0], 0)
        conn.close()

    def test_audit_sync_failure_does_not_break_proof_or_partial_approval(self):
        self._as(self.worker_id)
        self.assertEqual(self._audit_click(self.numbers[0]).status_code, 200)
        real_factory = app_module.database.get_db_connection
        with patch.object(app_module.database, 'get_db_connection') as get_connection:
            def connection_with_failing_audit_update(*args, **kwargs):
                connection = real_factory(*args, **kwargs)
                wrapped = Mock(wraps=connection)

                def execute(sql, params=()):
                    if 'UPDATE message_audit_records' in str(sql):
                        raise sqlite3.OperationalError('simulated audit synchronization failure')
                    return connection.execute(sql, params)

                wrapped.execute.side_effect = execute
                return wrapped

            get_connection.side_effect = connection_with_failing_audit_update
            with patch.object(app_module.ocr_scanner, 'audit_screenshot_proof', return_value={
                'audit_passed': False,
                'audit_badge': 'Audit Flagged: 1/25 valid numbers found (Match: 90%)',
                'numbers_found': 1,
                'total_required': 25,
                'total_targets': 25,
                'invalid_count': 0,
                'invalid_numbers': [],
                'matched_numbers': [self.numbers[0]],
                'match_percentage': 90,
            }):
                response = self.client.post(
                    f'/api/tasks/{self.task_id}/submit',
                    data={'proof_images': (io.BytesIO(self._PNG), 'proof.png')},
                    content_type='multipart/form-data',
                )

        self.assertEqual(response.status_code, 200)
        submission_id = response.get_json()['submission_id']
        conn = self._conn()
        submission = conn.execute(
            'SELECT status, numbers_detected, verified_count FROM submissions WHERE id = ?',
            (submission_id,),
        ).fetchone()
        self.assertEqual(dict(submission), {
            'status': 'pending',
            'numbers_detected': 1,
            'verified_count': 1,
        })
        conn.close()

        self._as(self.admin_id)
        approved = self.client.post(
            f'/api/admin/submissions/{submission_id}/approve-partial',
            json={'valid_count': 1},
        )
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.get_json()['payout'], 3.0)

    def test_click_is_idempotent_and_captures_generated_message(self):
        self._as(self.worker_id)
        recipient = self.numbers[0]
        first = self._audit_click(recipient)
        second = self._audit_click(recipient)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)

        conn = self._conn()
        row = conn.execute('''
            SELECT * FROM message_audit_records
            WHERE assignment_id = ? AND normalized_recipient_number = ?
        ''', (self.assignment_id, recipient)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['task_id'], self.task_id)
        self.assertEqual(row['user_id'], self.worker_id)
        self.assertEqual(row['recipient_number'], recipient)
        self.assertEqual(row['message_text'], 'Hello audit recipient\n\nhttps://example.test/audit')
        self.assertEqual(row['status'], 'OPENED')
        self.assertEqual(row['open_count'], 2)
        self.assertIsNotNone(row['first_opened_at'])
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM message_audit_records').fetchone()[0], 1)
        conn.close()

    def test_proof_approval_and_payout_remain_unchanged_and_report_is_admin_only(self):
        self._as(self.worker_id)
        recipient = self.numbers[0]
        self.assertEqual(self._audit_click(recipient).status_code, 200)
        with patch.object(app_module.ocr_scanner, 'audit_screenshot_proof', return_value={
            'audit_passed': False,
            'audit_badge': 'Audit Flagged: 1/25 valid numbers found (Match: 90%)',
            'numbers_found': 1,
            'total_required': 25,
            'total_targets': 25,
            'invalid_count': 0,
            'invalid_numbers': [],
            'matched_numbers': [recipient],
            'match_percentage': 90,
        }):
            response = self.client.post(
                f'/api/tasks/{self.task_id}/submit',
                data={'proof_images': (io.BytesIO(self._PNG), 'proof.png')},
                content_type='multipart/form-data',
            )
        self.assertEqual(response.status_code, 200)
        submission_id = response.get_json()['submission_id']

        conn = self._conn()
        audit = conn.execute('SELECT submission_id, target_number_record_id FROM message_audit_records').fetchone()
        self.assertEqual(audit['submission_id'], submission_id)
        self.assertIsNotNone(audit['target_number_record_id'])
        self.assertEqual(conn.execute('SELECT status FROM submissions WHERE id = ?', (submission_id,)).fetchone()[0], 'pending')
        conn.close()

        self._as(self.admin_id)
        approved = self.client.post(f'/api/admin/submissions/{submission_id}/approve-partial', json={'valid_count': 1})
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.get_json()['payout'], 3.0)

        report = self.client.get('/api/admin/audit-report.csv')
        self.assertEqual(report.status_code, 200)
        self.assertIn('Penerima', report.text)
        self.assertIn(recipient, report.text)
        self.assertIn('PAID', report.text)

        self._as(self.worker_id)
        self.assertEqual(self.client.get('/api/admin/audit-report.csv').status_code, 403)

    def test_reused_recipient_gets_new_attempt_history(self):
        self._as(self.worker_id)
        reusable_recipient = self.numbers[1]
        verified_recipient = self.numbers[0]
        self.assertEqual(self._audit_click(reusable_recipient).status_code, 200)
        with patch.object(app_module.ocr_scanner, 'audit_screenshot_proof', return_value={
            'audit_passed': False,
            'audit_badge': 'Audit Flagged: 1/25 numbers found (Match: 90%)',
            'numbers_found': 1,
            'total_required': 25,
            'total_targets': 25,
            'invalid_count': 0,
            'invalid_numbers': [],
            'matched_numbers': [verified_recipient],
            'match_percentage': 90,
        }):
            self.client.post(
                f'/api/tasks/{self.task_id}/submit',
                data={'proof_images': (io.BytesIO(self._PNG), 'proof.png')},
                content_type='multipart/form-data',
            )
        conn = self._conn()
        submission_id = conn.execute('SELECT id FROM submissions ORDER BY id DESC LIMIT 1').fetchone()[0]
        conn.close()
        self._as(self.admin_id)
        self.assertEqual(self.client.post(f'/api/admin/submissions/{submission_id}/approve-partial', json={'valid_count': 1}).status_code, 200)

        self._as(self.worker_two_id)
        next_slot = self.client.post(f'/api/tasks/{self.task_id}/take')
        self.assertEqual(next_slot.status_code, 200)
        next_assignment_id = next_slot.get_json()['assignment_id']
        conn = self._conn()
        reused = conn.execute('''
            SELECT id FROM recipient_allocations
            WHERE assignment_id = ? AND phone_number = ?
        ''', (next_assignment_id, reusable_recipient)).fetchone()
        self.assertIsNotNone(reused)
        conn.close()

        self._as(self.worker_two_id)
        clicked = self.client.post(
            f'/api/tasks/{self.task_id}/recipient-audit',
            json={'assignment_id': next_assignment_id, 'recipient_number': reusable_recipient},
        )
        self.assertEqual(clicked.status_code, 200)
        conn = self._conn()
        history = conn.execute('''
            SELECT assignment_id FROM message_audit_records
            WHERE normalized_recipient_number = ? ORDER BY id
        ''', (reusable_recipient,)).fetchall()
        self.assertEqual([row['assignment_id'] for row in history], [self.assignment_id, next_assignment_id])
        conn.close()


if __name__ == '__main__':
    unittest.main()
