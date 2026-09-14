"""Pass 3 real Flask-route regression coverage.

Run with the installed Flask/Werkzeug environment, for example:
    py -3.14 -m unittest test_pass3_routes.py -v
"""
import importlib
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('ENVIRONMENT', 'production')
os.environ.setdefault('SECRET_KEY', 'pass3-test-secret-not-for-production')

import database
import app as app_module
from werkzeug.security import generate_password_hash


class Pass3RouteTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.mkdtemp(prefix='taskpe-pass3-')
        self.old_db_path = database.DB_PATH
        self.old_environment = os.environ.get('ENVIRONMENT')
        self.old_secret = os.environ.get('SECRET_KEY')
        os.environ['ENVIRONMENT'] = 'production'
        os.environ['SECRET_KEY'] = 'pass3-test-secret-not-for-production'
        database.DB_PATH = os.path.join(self.tempdir, 'taskpe-test.db')
        database.init_db()
        global app_module
        app_module = importlib.reload(app_module)
        app_module.app.config['TESTING'] = True
        self.client = app_module.app.test_client()
        self.admin_id = self._user('Admin', '919999991001', 'admin-route@example.com', 'admin', 0)
        self.referrer_id = self._user('Referrer', '919999991002', 'ref-route@example.com', 'user', 0)
        self.worker_id = self._user('Worker', '919999991003', 'worker-route@example.com', 'user', 500, self.referrer_id)

    def tearDown(self):
        database.DB_PATH = self.old_db_path
        if self.old_environment is None:
            os.environ.pop('ENVIRONMENT', None)
        else:
            os.environ['ENVIRONMENT'] = self.old_environment
        if self.old_secret is None:
            os.environ.pop('SECRET_KEY', None)
        else:
            os.environ['SECRET_KEY'] = self.old_secret
        shutil.rmtree(self.tempdir, ignore_errors=True)

    def _conn(self):
        return database.get_db_connection()

    def _user(self, name, mobile, email, role, balance, referred_by=None):
        conn = self._conn()
        cursor = conn.execute('''
            INSERT INTO users (name, mobile, email, password_hash, role, wallet_balance, total_earned, referral_code, referred_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (name, mobile, email, generate_password_hash('password'), role, balance, balance, mobile[-6:], referred_by))
        conn.commit()
        ident = cursor.lastrowid
        conn.close()
        return ident

    def _as(self, user_id):
        with self.client.session_transaction() as session:
            session.clear()
            session['user_id'] = user_id

    def _task_and_submission(self, payout=75, required=25, campaign_type='Direct Message', verified=None):
        conn = self._conn()
        cursor = conn.cursor()
        task_id = cursor.execute('''
            INSERT INTO tasks (title, category, description, payout, template_text, target_numbers, campaign_id,
                               recipients_per_slot, time_limit_minutes, proof_instructions, total_slots, completed_slots, status)
            VALUES ('Route task', 'Direct Message', 'Description', ?, 'Template', '["919999990001"]', 1, ?, 25, 'Proof', 10, 1, 'active')
        ''', (payout, required)).lastrowid
        assignment_id = cursor.execute('''
            INSERT INTO task_assignments (user_id, task_id, campaign_id, campaign_type, assigned_numbers, status)
            VALUES (?, ?, ?, ?, '["919999990001"]', 'pending_review')
        ''', (self.worker_id, task_id, task_id, campaign_type)).lastrowid
        submission_id = cursor.execute('''
            INSERT INTO submissions (user_id, task_id, assignment_id, proof_image, status, payout_amount,
                                     numbers_required, numbers_detected)
            VALUES (?, ?, ?, 'proof.png', 'pending', ?, ?, ?)
        ''', (self.worker_id, task_id, assignment_id, payout, required, required if verified is None else verified)).lastrowid
        conn.commit()
        conn.close()
        return task_id, assignment_id, submission_id

    def _withdrawal(self, amount=180, address='TAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'):
        self._as(self.worker_id)
        response = self.client.post('/api/wallet/withdraw', json={
            'amount': amount, 'payout_method': 'usdt', 'usdt_network': 'TRC20',
            'usdt_wallet_address': address
        })
        return response

    def test_usdt_request_validation_preservation_and_hash_payment(self):
        self.assertEqual(self._withdrawal(address='not-an-address').status_code, 400)
        # Address syntax is also validated for the formats supported by the app.
        self.assertTrue(app_module.valid_usdt_address('TRC20', 'TAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'))
        self.assertTrue(app_module.valid_usdt_address('ERC20', '0x' + 'a' * 40))
        self.assertTrue(app_module.valid_usdt_address('BEP20', '0x' + 'b' * 40))

        response = self._withdrawal()
        self.assertEqual(response.status_code, 200)
        conn = self._conn()
        wd = conn.execute('SELECT * FROM withdrawals ORDER BY id DESC LIMIT 1').fetchone()
        self.assertEqual(wd['currency'], 'USDT')
        self.assertEqual(str(wd['usdt_amount']), '2')
        self.assertEqual(str(wd['usdt_inr_rate']), '90')
        self.assertEqual(wd['usdt_network'], 'TRC20')
        self.assertEqual(wd['usdt_wallet_address'], 'TAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA')
        self.assertIsNone(wd['provider_reference'])
        self.assertIsNone(wd['transaction_hash'])
        wd_id = wd['id']
        conn.execute("UPDATE withdrawals SET status='processing' WHERE id=?", (wd_id,))
        conn.execute("UPDATE settings SET value='100' WHERE key='usdt_inr_rate'")
        conn.commit()
        conn.close()

        self._as(self.admin_id)
        self.assertEqual(self.client.post(f'/api/admin/withdrawals/{wd_id}/paid', json={'provider_reference': 'PAYOUT-1'}).status_code, 400)
        paid = self.client.post(f'/api/admin/withdrawals/{wd_id}/paid', json={
            'provider_reference': 'PAYOUT-1', 'transaction_hash': '0xchainhash'
        })
        self.assertEqual(paid.status_code, 200)
        conn = self._conn()
        wd = conn.execute('SELECT * FROM withdrawals WHERE id=?', (wd_id,)).fetchone()
        self.assertEqual(wd['status'], 'paid')
        self.assertEqual(wd['provider_reference'], 'PAYOUT-1')
        self.assertEqual(wd['transaction_hash'], '0xchainhash')
        self.assertNotEqual(wd['provider_reference'], wd['transaction_hash'])
        self.assertIsNone(wd['utr_number'])
        self.assertEqual(str(wd['usdt_inr_rate']), '90')  # historical lock
        conn.close()

    def test_full_and_partial_approval_are_single_payments_with_referral(self):
        _, assignment_id, sub_id = self._task_and_submission()
        self._as(self.admin_id)
        self.assertEqual(self.client.post(f'/api/admin/submissions/{sub_id}/approve').status_code, 200)
        self.assertIn(self.client.post(f'/api/admin/submissions/{sub_id}/approve').status_code, (400, 409))
        conn = self._conn()
        self.assertEqual(conn.execute('SELECT status FROM submissions WHERE id=?', (sub_id,)).fetchone()[0], 'approved')
        self.assertEqual(conn.execute('SELECT status FROM task_assignments WHERE id=?', (assignment_id,)).fetchone()[0], 'paid')
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM wallet_transactions WHERE reference_id=?", (f'SUB-{sub_id}',)).fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM wallet_transactions WHERE reference_id=?", (f'REF-TASK-{sub_id}',)).fetchone()[0], 1)
        conn.close()

        _, _, partial_id = self._task_and_submission(payout=75, required=25)
        partial = self.client.post(f'/api/admin/submissions/{partial_id}/approve-partial', json={'valid_count': 20})
        self.assertEqual(partial.status_code, 200)
        conn = self._conn()
        self.assertEqual(conn.execute('SELECT payout_amount FROM submissions WHERE id=?', (partial_id,)).fetchone()[0], 60)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM wallet_transactions WHERE reference_id=?", (f'PARTIAL-SUB-{partial_id}',)).fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM wallet_transactions WHERE reference_id=?", (f'REF-TASK-{partial_id}',)).fetchone()[0], 1)
        conn.close()

    def test_failed_approval_rolls_back_and_reversal_reverses_referral_once(self):
        _, _, failed_id = self._task_and_submission()
        self._as(self.admin_id)
        with patch.object(app_module, 'credit_referrer_for_task', side_effect=RuntimeError('simulated ledger failure')):
            with self.assertRaises(RuntimeError):
                self.client.post(f'/api/admin/submissions/{failed_id}/approve')
        conn = self._conn()
        self.assertEqual(conn.execute('SELECT status FROM submissions WHERE id=?', (failed_id,)).fetchone()[0], 'pending')
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM wallet_transactions WHERE reference_id=?", (f'SUB-{failed_id}',)).fetchone()[0], 0)
        conn.close()

        _, _, approved_id = self._task_and_submission(campaign_type='Broadcast')
        self.assertEqual(self.client.post(f'/api/admin/submissions/{approved_id}/approve').status_code, 200)
        reversed_response = self.client.post(f'/api/admin/submissions/{approved_id}/reverse', json={'reason': 'audit correction'})
        self.assertEqual(reversed_response.status_code, 200)
        self.assertEqual(self.client.post(f'/api/admin/submissions/{approved_id}/reverse', json={'reason': 'again'}).status_code, 409)
        conn = self._conn()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM wallet_transactions WHERE reference_id=?", (f'REVERSAL-SUB-{approved_id}',)).fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM wallet_transactions WHERE reference_id=?", (f'REF-REVERSAL-{approved_id}',)).fetchone()[0], 1)
        conn.close()

    def test_withdrawal_rejection_refunds_once_and_migration_is_idempotent(self):
        self._as(self.worker_id)
        response = self.client.post('/api/wallet/withdraw', json={'amount': 100, 'payout_method': 'upi', 'upi_id': 'worker@upi'})
        self.assertEqual(response.status_code, 200)
        conn = self._conn()
        wd_id = conn.execute('SELECT id FROM withdrawals ORDER BY id DESC LIMIT 1').fetchone()[0]
        self.assertEqual(conn.execute('SELECT wallet_balance FROM users WHERE id=?', (self.worker_id,)).fetchone()[0], 400)
        conn.close()
        self._as(self.admin_id)
        self.assertEqual(self.client.post(f'/api/admin/withdrawals/{wd_id}/reject', json={'reason': 'invalid'}).status_code, 200)
        self.assertEqual(self.client.post(f'/api/admin/withdrawals/{wd_id}/reject', json={'reason': 'again'}).status_code, 400)
        conn = self._conn()
        self.assertEqual(conn.execute('SELECT wallet_balance FROM users WHERE id=?', (self.worker_id,)).fetchone()[0], 500)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM wallet_transactions WHERE reference_id=? AND type='withdrawal_refund'", (f'REFUND-{wd_id}',)).fetchone()[0], 1)
        users_before = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        conn.close()
        database.init_db()
        conn = self._conn()
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM users').fetchone()[0], users_before)
        self.assertIsNotNone(conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='one_referral_reversal_per_submission'").fetchone())
        conn.close()

    def test_phase3_threshold_and_verified_count_control_approval(self):
        self._as(self.admin_id)
        _, assignment_id, below_id = self._task_and_submission(campaign_type='Threshold19', verified=19)
        before_conn = self._conn()
        before = before_conn.execute('SELECT wallet_balance FROM users WHERE id=?', (self.worker_id,)).fetchone()[0]
        before_conn.close()
        self.assertEqual(self.client.post(f'/api/admin/submissions/{below_id}/approve').status_code, 400)
        conn = self._conn()
        self.assertEqual(conn.execute('SELECT status FROM submissions WHERE id=?', (below_id,)).fetchone()[0], 'pending')
        self.assertEqual(conn.execute('SELECT status FROM task_assignments WHERE id=?', (assignment_id,)).fetchone()[0], 'pending_review')
        self.assertEqual(conn.execute('SELECT wallet_balance FROM users WHERE id=?', (self.worker_id,)).fetchone()[0], before)
        conn.close()

        for verified, expected in ((20, 60), (21, 63), (24, 72), (25, 75)):
            _, assignment_id, submission_id = self._task_and_submission(
                campaign_type=f'Threshold{verified}', verified=verified
            )
            response = self.client.post(f'/api/admin/submissions/{submission_id}/approve')
            self.assertEqual(response.status_code, 200, response.get_json())
            conn = self._conn()
            row = conn.execute('SELECT payout_amount, approved_valid_count FROM submissions WHERE id=?', (submission_id,)).fetchone()
            self.assertEqual(row['payout_amount'], expected)
            self.assertEqual(row['approved_valid_count'], verified)
            self.assertEqual(conn.execute('SELECT status FROM task_assignments WHERE id=?', (assignment_id,)).fetchone()[0], 'paid')
            conn.close()

    def test_partial_approval_cannot_manufacture_verified_targets(self):
        self._as(self.admin_id)
        _, _, submission_id = self._task_and_submission(campaign_type='ThresholdPartial', verified=18)
        response = self.client.post(f'/api/admin/submissions/{submission_id}/approve-partial', json={'valid_count': 20})
        self.assertEqual(response.status_code, 400)
        conn = self._conn()
        self.assertEqual(conn.execute('SELECT status FROM submissions WHERE id=?', (submission_id,)).fetchone()[0], 'pending')
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM wallet_transactions WHERE reference_id=?', (f'PARTIAL-SUB-{submission_id}',)).fetchone()[0], 0)
        conn.close()


if __name__ == '__main__':
    unittest.main()
