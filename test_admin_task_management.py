import importlib
import os
import tempfile
import unittest


ROOT = tempfile.mkdtemp(prefix='taskpe-admin-tasks-')
os.environ['TASKPE_DB_PATH'] = os.path.join(ROOT, 'tasks.db')
os.environ['ENVIRONMENT'] = 'development'
os.environ['SECRET_KEY'] = 'admin-task-test-secret'

import database
import app as app_module


class AdminTaskManagementTests(unittest.TestCase):
    def setUp(self):
        if os.path.exists(database.DB_PATH):
            os.remove(database.DB_PATH)
        self.app_module = importlib.reload(app_module)
        self.app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.client = self.app_module.app.test_client()
        conn = database.get_db_connection()
        self.admin_id = conn.execute('''
            INSERT INTO users (name, mobile, email, password_hash, role, referral_code)
            VALUES ('Admin', '919900000001', 'admin-task@example.com', 'hash', 'admin', 'ADMINTASK')
        ''').lastrowid
        self.worker_id = conn.execute('''
            INSERT INTO users (name, mobile, email, password_hash, referral_code)
            VALUES ('Worker', '919900000002', 'worker-task@example.com', 'hash', 'WORKERTASK')
        ''').lastrowid
        self.task_id = conn.execute('''
            INSERT INTO tasks (title, category, description, payout, template_text, target_numbers,
                               campaign_id, recipients_per_slot, time_limit_minutes, proof_instructions,
                               total_slots, status)
            VALUES ('Admin task', 'Direct Message', 'Description', 3, 'Hello', '["919900000099"]',
                    1, 1, 25, 'Upload proof', 1, 'active')
        ''').lastrowid
        conn.commit()
        conn.close()

    def _admin(self):
        with self.client.session_transaction() as session:
            session['user_id'] = self.admin_id

    def test_admin_can_archive_restore_and_list_task(self):
        self._admin()
        listed = self.client.get('/api/admin/tasks')
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()['tasks'][0]['id'], self.task_id)
        self.assertEqual(self.client.post(f'/api/admin/tasks/{self.task_id}/archive').status_code, 200)
        self.assertEqual(self.client.get('/api/tasks').get_json()['tasks'], [])
        self.assertEqual(self.client.post(f'/api/admin/tasks/{self.task_id}/restore').status_code, 200)
        self.assertEqual(len(self.client.get('/api/tasks').get_json()['tasks']), 1)

    def test_admin_can_pause_and_activate_task(self):
        self._admin()
        self.assertEqual(self.client.post(f'/api/admin/tasks/{self.task_id}/pause').status_code, 200)
        self.assertEqual(self.client.get('/api/tasks').get_json()['tasks'], [])
        self.assertEqual(self.client.post(f'/api/admin/tasks/{self.task_id}/activate').status_code, 200)
        self.assertEqual(len(self.client.get('/api/tasks').get_json()['tasks']), 1)

    def test_activate_cannot_bypass_archived_restore(self):
        self._admin()
        self.client.post(f'/api/admin/tasks/{self.task_id}/archive')
        self.assertEqual(self.client.post(f'/api/admin/tasks/{self.task_id}/activate').status_code, 409)

    def test_non_admin_cannot_archive_or_delete(self):
        with self.client.session_transaction() as session:
            session['user_id'] = self.worker_id
        self.assertIn(self.client.post(f'/api/admin/tasks/{self.task_id}/archive').status_code, (401, 403))
        self.assertIn(self.client.delete(f'/api/admin/tasks/{self.task_id}').status_code, (401, 403))

    def test_unused_task_can_be_deleted(self):
        self._admin()
        response = self.client.delete(f'/api/admin/tasks/{self.task_id}')
        self.assertEqual(response.status_code, 200)
        conn = database.get_db_connection()
        self.assertIsNone(conn.execute('SELECT id FROM tasks WHERE id = ?', (self.task_id,)).fetchone())
        conn.close()

    def test_task_with_history_cannot_be_deleted_and_archive_preserves_history(self):
        self._admin()
        conn = database.get_db_connection()
        conn.execute('''
            INSERT INTO task_assignments (user_id, task_id, campaign_id, campaign_type, assigned_numbers)
            VALUES (?, ?, 1, 'Direct Message', '["919900000099"]')
        ''', (self.worker_id, self.task_id))
        conn.commit()
        conn.close()
        self.assertEqual(self.client.delete(f'/api/admin/tasks/{self.task_id}').status_code, 409)
        self.assertEqual(self.client.post(f'/api/admin/tasks/{self.task_id}/archive').status_code, 200)
        conn = database.get_db_connection()
        self.assertIsNotNone(conn.execute('SELECT id FROM task_assignments WHERE task_id = ?', (self.task_id,)).fetchone())
        conn.close()


if __name__ == '__main__':
    unittest.main()
