import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

ROOT = tempfile.mkdtemp(prefix='taskpe-slots-')
os.environ['TASKPE_DB_PATH'] = os.path.join(ROOT, 'slots.db')
os.environ['ENVIRONMENT'] = 'development'
os.environ['SECRET_KEY'] = 'slot-test-secret'

import database
import app as app_module


class SlotAvailabilityTests(unittest.TestCase):
    def setUp(self):
        os.environ['ENVIRONMENT'] = 'development'
        os.environ['SECRET_KEY'] = 'slot-test-secret'
        if os.path.exists(database.DB_PATH):
            os.remove(database.DB_PATH)
        self.app_module = __import__('importlib').reload(app_module)
        self.app_module.app.config['TESTING'] = True
        conn = database.get_db_connection()
        self.users = []
        for i in range(21):
            self.users.append(conn.execute('''
                INSERT INTO users (name, mobile, email, password_hash, referral_code)
                VALUES (?, ?, ?, 'hash', ?)
            ''', (f'Worker {i}', f'919800000{i:03d}', f'slot{i}@example.com', f'SLOT{i}')).lastrowid)
        numbers = [f'919700{i:07d}' for i in range(500)]
        self.task_id = conn.execute('''
            INSERT INTO tasks (title, category, description, payout, template_text, target_numbers,
                campaign_id, recipients_per_slot, time_limit_minutes, proof_instructions, total_slots, status)
            VALUES ('Slot test', 'Direct Message', 'Slot test', 75, 'Hello', ?, 1, 25, 25, 'Proof', 20, 'active')
        ''', (json.dumps(numbers),)).lastrowid
        conn.commit()
        conn.close()

    def _take(self, user_id):
        client = self.app_module.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = user_id
        return client.post(f'/api/tasks/{self.task_id}/take')

    def _task(self):
        conn = database.get_db_connection()
        row = conn.execute('SELECT * FROM tasks WHERE id = ?', (self.task_id,)).fetchone()
        taken = conn.execute("SELECT COUNT(*) FROM task_assignments WHERE task_id = ? AND status IN ('in_progress','pending_review','paid')", (self.task_id,)).fetchone()[0]
        conn.close()
        return row, taken

    def test_slot_progression_and_limit(self):
        response = self.app_module.app.test_client().get('/api/tasks')
        task = next(t for t in response.get_json()['tasks'] if t['id'] == self.task_id)
        self.assertEqual({task['total_slots'], task['taken_slots'], task['available_slots'], task['completed_slots']}, {20, 0, 20, 0})
        self.assertEqual(self._take(self.users[0]).status_code, 200)
        self.assertEqual(self._take(self.users[1]).status_code, 200)
        for user_id in self.users[2:20]:
            self.assertEqual(self._take(user_id).status_code, 200)
        row, taken = self._task()
        self.assertEqual(taken, 20)
        self.assertEqual(self._take(self.users[20]).status_code, 400)
        self.assertEqual(row['total_slots'], 20)

    def test_cancelled_previous_direct_message_does_not_block_new_task(self):
        conn = database.get_db_connection()
        conn.execute('''
            INSERT INTO task_assignments
                (user_id, task_id, campaign_id, campaign_type, assigned_numbers, status, slot_released)
            VALUES (?, ?, ?, 'Direct Message', '[]', 'cancelled', 1)
        ''', (self.users[0], self.task_id, self.task_id))
        conn.commit()
        conn.close()
        response = self._take(self.users[0])
        self.assertEqual(response.status_code, 200)
        conn = database.get_db_connection()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM task_assignments WHERE user_id = ? AND campaign_type = 'Direct Message' AND status = 'in_progress'", (self.users[0],)).fetchone()[0], 1)
        conn.close()

    def test_concurrent_allocations_do_not_duplicate_targets_or_exceed_slots(self):
        with ThreadPoolExecutor(max_workers=21) as pool:
            responses = list(pool.map(self._take, self.users))
        successes = sum(response.status_code == 200 for response in responses)
        conn = database.get_db_connection()
        taken = conn.execute("SELECT COUNT(*) FROM task_assignments WHERE task_id = ? AND status = 'in_progress'", (self.task_id,)).fetchone()[0]
        allocations = conn.execute('SELECT COUNT(*) FROM recipient_allocations WHERE campaign_id = 1').fetchone()[0]
        unique_allocations = conn.execute('SELECT COUNT(DISTINCT phone_number) FROM recipient_allocations WHERE campaign_id = 1').fetchone()[0]
        conn.close()
        self.assertEqual(successes, 20)
        self.assertEqual(taken, 20)
        self.assertEqual(allocations, 500)
        self.assertEqual(unique_allocations, 500)


if __name__ == '__main__':
    unittest.main()
