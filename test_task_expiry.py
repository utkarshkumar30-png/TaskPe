import os
import sqlite3
import tempfile
import unittest

from expire_task_assignments import expire_assignments


class TaskExpiryRunnerTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix='.db')
        os.close(handle)
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript('''
            CREATE TABLE tasks (id INTEGER PRIMARY KEY, time_limit_minutes INTEGER NOT NULL);
            CREATE TABLE task_assignments (
                id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL, status TEXT NOT NULL,
                assigned_at DATETIME, started_at DATETIME, expires_at DATETIME,
                slot_released INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO tasks VALUES (1, 25);
        ''')
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    def assignment(self, assignment_id):
        return self.conn.execute(
            'SELECT status, slot_released FROM task_assignments WHERE id = ?', (assignment_id,)
        ).fetchone()

    def test_expiry_and_repeat_are_idempotent(self):
        self.conn.execute('''
            INSERT INTO task_assignments (id, task_id, status, assigned_at, started_at, expires_at)
            VALUES (1, 1, 'in_progress', datetime('now', '-30 minutes'), datetime('now', '-30 minutes'), datetime('now', '-5 minutes'))
        ''')
        self.conn.commit()
        self.assertEqual(expire_assignments(self.path), 1)
        self.assertEqual(self.assignment(1), ('expired', 1))
        self.assertEqual(expire_assignments(self.path), 0)
        self.assertEqual(self.assignment(1), ('expired', 1))

    def test_active_assignment_is_preserved(self):
        self.conn.execute('''
            INSERT INTO task_assignments (id, task_id, status, assigned_at, started_at, expires_at)
            VALUES (2, 1, 'in_progress', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, datetime('now', '+20 minutes'))
        ''')
        self.conn.commit()
        self.assertEqual(expire_assignments(self.path), 0)
        self.assertEqual(self.assignment(2), ('in_progress', 0))

    def test_legacy_assignment_uses_task_duration_for_backfill(self):
        self.conn.execute('''
            INSERT INTO task_assignments (id, task_id, status, assigned_at, started_at, expires_at)
            VALUES (3, 1, 'in_progress', datetime('now', '-30 minutes'), NULL, NULL)
        ''')
        self.conn.commit()
        self.assertEqual(expire_assignments(self.path), 1)
        self.assertEqual(self.assignment(3), ('expired', 1))


if __name__ == '__main__':
    unittest.main()
