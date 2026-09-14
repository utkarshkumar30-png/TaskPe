import importlib
import os
import tempfile
import unittest

ROOT = tempfile.mkdtemp(prefix='taskpe-release-')
os.environ['TASKPE_DB_PATH'] = os.path.join(ROOT, 'release.db')
os.environ['ENVIRONMENT'] = 'development'
os.environ['SECRET_KEY'] = 'release-test-secret'

import database
import app as app_module


class AdminAssignmentReleaseTests(unittest.TestCase):
    def setUp(self):
        if os.path.exists(database.DB_PATH):
            os.remove(database.DB_PATH)
        self.app_module = importlib.reload(app_module)
        self.app_module.app.config['TESTING'] = True
        self.client = self.app_module.app.test_client()
        conn = database.get_db_connection()
        self.admin_id = conn.execute("INSERT INTO users (name,mobile,email,password_hash,role,referral_code) VALUES ('Admin','919910000001','release-admin@example.com','hash','admin','RELADMIN')").lastrowid
        self.worker_id = conn.execute("INSERT INTO users (name,mobile,email,password_hash,role,referral_code) VALUES ('Worker','919910000002','release-worker@example.com','hash','user','RELWORK')").lastrowid
        self.task_id = conn.execute("INSERT INTO tasks (title,category,description,payout,template_text,target_numbers,campaign_id,recipients_per_slot,time_limit_minutes,proof_instructions,total_slots,status) VALUES ('Release task','Direct Message','d','75','Hello','[\"919700000001\"]',1,1,25,'Proof',20,'active')").lastrowid
        self.assignment_id = conn.execute("INSERT INTO task_assignments (user_id,task_id,campaign_id,campaign_type,assigned_numbers,status,slot_released) VALUES (?,?,1,'Direct Message','[\"919700000001\"]','in_progress',0)", (self.worker_id,self.task_id)).lastrowid
        conn.commit(); conn.close()

    def _login(self, user_id):
        with self.client.session_transaction() as session:
            session['user_id'] = user_id

    def test_admin_release_is_idempotent_and_audited_without_ledger(self):
        self._login(self.admin_id)
        body = {'reason': 'Stale development/test worker assignment'}
        first = self.client.post(f'/api/admin/task-assignments/{self.assignment_id}/release', json=body)
        second = self.client.post(f'/api/admin/task-assignments/{self.assignment_id}/release', json=body)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        conn = database.get_db_connection()
        assignment = conn.execute('SELECT status,slot_released FROM task_assignments WHERE id=?', (self.assignment_id,)).fetchone()
        self.assertEqual((assignment['status'], assignment['slot_released']), ('cancelled', 1))
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM admin_assignment_release_audit WHERE assignment_id=?', (self.assignment_id,)).fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM wallet_transactions WHERE user_id=? AND type IN ('task_reward','withdrawal','referral_task_commission')", (self.worker_id,)).fetchone()[0], 0)
        conn.close()

    def test_non_admin_cannot_release_and_paid_assignment_is_blocked(self):
        self._login(self.worker_id)
        self.assertIn(self.client.post(f'/api/admin/task-assignments/{self.assignment_id}/release', json={'reason': 'x'}).status_code, (401, 403))
        self._login(self.admin_id)
        conn = database.get_db_connection(); conn.execute("UPDATE task_assignments SET status='paid' WHERE id=?", (self.assignment_id,)); conn.commit(); conn.close()
        self.assertEqual(self.client.post(f'/api/admin/task-assignments/{self.assignment_id}/release', json={'reason': 'x'}).status_code, 409)


if __name__ == '__main__':
    unittest.main()
