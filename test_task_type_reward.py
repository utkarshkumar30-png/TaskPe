import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import database
from money import inr_to_paise


ROOT = Path(__file__).resolve().parent


class TaskTypeRewardMigrationTests(unittest.TestCase):
    """Exercise task reward snapshots only on disposable SQLite databases."""

    def setUp(self):
        handle, self.path = tempfile.mkstemp(prefix='taskpe-task-reward-', suffix='.db')
        os.close(handle)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript('''
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO settings VALUES ('per_message_rate', '3');
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT, category TEXT, description TEXT, payout REAL,
                payout_paise INTEGER, template_text TEXT, target_numbers TEXT,
                recipients_per_slot INTEGER, task_type TEXT, image_path TEXT,
                destination_link TEXT, reward_per_number_paise INTEGER,
                reward_source TEXT
            );
        ''')

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    def create_task(self, title, *, task_type='TEXT_LINK', reward_paise=None, recipients=1):
        if reward_paise is None:
            reward_paise = inr_to_paise(self.conn.execute(
                "SELECT value FROM settings WHERE key = 'per_message_rate'"
            ).fetchone()[0])
            source = 'default_at_creation'
        else:
            source = 'admin_override'
        payout_paise = reward_paise * recipients
        task_id = self.conn.execute('''
            INSERT INTO tasks (
                title, category, description, payout, payout_paise,
                template_text, target_numbers, recipients_per_slot,
                task_type, reward_per_number_paise, reward_source
            ) VALUES (?, ?, 'description', ?, ?, 'message', '[]', ?, ?, ?, ?)
        ''', (title, 'IMAGE_TEXT_LINK' if task_type == 'IMAGE_TEXT_LINK' else 'Direct Message',
              payout_paise / 100, payout_paise, recipients, task_type,
              reward_paise, source)).lastrowid
        self.conn.commit()
        return task_id

    def set_default_reward(self, value):
        self.conn.execute("UPDATE settings SET value = ? WHERE key = 'per_message_rate'", (str(value),))
        self.conn.commit()

    def task(self, task_id):
        return self.conn.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()

    def test_default_reward_snapshot_isolated_from_later_default_change(self):
        task_a = self.create_task('Task A')
        self.set_default_reward('4')
        self.assertEqual(self.task(task_a)['reward_per_number_paise'], 300)

    def test_custom_reward_snapshot_isolated_from_default_change(self):
        task_b = self.create_task('Task B', reward_paise=500)
        self.set_default_reward('4')
        self.assertEqual(self.task(task_b)['reward_per_number_paise'], 500)
        self.assertEqual(self.task(task_b)['reward_source'], 'admin_override')

    def test_new_tasks_use_new_default_without_rewriting_old_tasks(self):
        task_a = self.create_task('Task A')
        self.set_default_reward('4')
        task_c = self.create_task('Task C')
        self.assertEqual(self.task(task_a)['reward_per_number_paise'], 300)
        self.assertEqual(self.task(task_c)['reward_per_number_paise'], 400)

    def test_payout_calculation_uses_task_snapshot_not_current_setting(self):
        task_a = self.create_task('Task A', recipients=3)
        self.set_default_reward('4')
        if 'app' not in sys.modules:
            os.environ['TASKPE_DB_PATH'] = self.path
            os.environ['ENVIRONMENT'] = 'development'
            os.environ['SECRET_KEY'] = 'task-reward-test-secret'
            os.environ['SMS_PROVIDER'] = 'disabled'
            database.DB_PATH = self.path
        import app as app_module
        self.assertEqual(app_module.calculate_task_payout_paise(self.conn, self.task(task_a)), 900)

    def test_image_text_link_custom_reward_calculates_correctly(self):
        task_id = self.create_task('Image campaign', task_type='IMAGE_TEXT_LINK', reward_paise=575, recipients=4)
        row = self.task(task_id)
        self.assertEqual(row['task_type'], 'IMAGE_TEXT_LINK')
        self.assertEqual(row['reward_per_number_paise'], 575)
        import app as app_module
        self.assertEqual(app_module.calculate_task_payout_paise(self.conn, row), 2300)

    def test_existing_legacy_tasks_receive_correct_reward_snapshot(self):
        self.conn.execute('''
            INSERT INTO tasks (title, category, payout, payout_paise, recipients_per_slot)
            VALUES ('Legacy', 'Direct Message', 6.50, 650, 2)
        ''')
        self.conn.commit()
        database.ensure_task_type_and_reward_fields(self.conn)
        row = self.task(1)
        self.assertEqual(row['task_type'], 'TEXT_LINK')
        self.assertEqual(row['reward_per_number_paise'], 325)
        self.assertEqual(row['reward_source'], 'legacy_snapshot')

    def test_finance_default_changes_never_rewrite_existing_snapshots(self):
        task_a = self.create_task('Task A')
        self.set_default_reward('4')
        task_b = self.create_task('Task B')
        self.set_default_reward('7')
        database.ensure_task_type_and_reward_fields(self.conn)
        self.assertEqual(self.task(task_a)['reward_per_number_paise'], 300)
        self.assertEqual(self.task(task_b)['reward_per_number_paise'], 400)

    def test_schema_migration_is_idempotent_and_preserves_history_values(self):
        self.conn.execute('''
            INSERT INTO tasks (title, category, payout, payout_paise, recipients_per_slot)
            VALUES ('Legacy', 'Direct Message', 6.50, 650, 2)
        ''')
        self.conn.commit()
        database.ensure_task_type_and_reward_fields(self.conn)
        first = dict(self.task(1))
        database.ensure_task_type_and_reward_fields(self.conn)
        second = dict(self.task(1))
        self.assertEqual(first, second)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)

    def test_explicit_migration_cli_uses_only_supplied_disposable_db(self):
        self.conn.execute('''
            INSERT INTO tasks (title, category, payout, payout_paise, recipients_per_slot)
            VALUES ('Legacy', 'Direct Message', 6.50, 650, 2)
        ''')
        self.conn.commit()
        command = [sys.executable, str(ROOT / 'migrate_task_type_reward.py'), '--db', self.path, '--report']
        first = subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)
        second = subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)
        first_report = json.loads(first.stdout)
        second_report = json.loads(second.stdout)
        self.assertEqual(Path(first_report['database']), Path(self.path).resolve())
        self.assertEqual(first_report['task_rows'], 1)
        self.assertEqual(first_report['synthetic_history_rows'], 0)
        self.assertEqual(second_report['task_rows'], 1)
        self.assertEqual(second_report['task_type_reward_rows'], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)
        self.assertNotEqual(Path(self.path).resolve(), ROOT / 'taskpe.db')


if __name__ == '__main__':
    unittest.main()
