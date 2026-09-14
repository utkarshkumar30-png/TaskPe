import os
import sqlite3
import tempfile
import unittest

import database


class FinanceSettingsHistoryTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript('''
            CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, role TEXT);
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE exact_money_settings (
                key TEXT PRIMARY KEY, value_scaled INTEGER NOT NULL,
                scale INTEGER NOT NULL, kind TEXT NOT NULL,
                migrated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO users VALUES (7, 'Admin', 'admin');
            INSERT INTO settings VALUES ('per_message_rate', '3');
        ''')
        database.ensure_finance_settings_history(self.conn)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    def record_change(self, old_value, new_value, changed_by=7):
        try:
            self.conn.execute('BEGIN IMMEDIATE')
            current = self.conn.execute("SELECT value FROM settings WHERE key='per_message_rate'").fetchone()['value']
            if current != new_value:
                self.conn.execute("UPDATE settings SET value=? WHERE key='per_message_rate'", (new_value,))
                self.conn.execute('''
                    INSERT INTO finance_settings_history
                        (setting_name, old_value, new_value, changed_by, unit)
                    VALUES ('per_message_rate', ?, ?, ?, 'INR/message')
                ''', (old_value, new_value, changed_by))
            self.conn.commit()
        except sqlite3.Error:
            self.conn.rollback()
            raise

    def test_change_records_values_identity_and_timestamp(self):
        self.record_change('3', '3.5')
        row = self.conn.execute('SELECT * FROM finance_settings_history').fetchone()
        self.assertEqual(row['setting_name'], 'per_message_rate')
        self.assertEqual(row['old_value'], '3')
        self.assertEqual(row['new_value'], '3.5')
        self.assertEqual(row['changed_by'], 7)
        self.assertIsNotNone(row['changed_at'])

    def test_unchanged_setting_creates_no_history(self):
        self.record_change('3', '3')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM finance_settings_history').fetchone()[0], 0)

    def test_history_insert_failure_rolls_back_setting_update(self):
        self.conn.execute('''
            CREATE TRIGGER fail_finance_history_insert
            BEFORE INSERT ON finance_settings_history
            BEGIN SELECT RAISE(ABORT, 'simulated history failure'); END
        ''')
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.record_change('3', '3.5')
        self.assertEqual(self.conn.execute("SELECT value FROM settings WHERE key='per_message_rate'").fetchone()[0], '3')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM finance_settings_history').fetchone()[0], 0)

    def test_history_is_immutable(self):
        self.record_change('3', '3.5')
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE finance_settings_history SET new_value='99' WHERE id=1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute('DELETE FROM finance_settings_history WHERE id=1')
        self.conn.rollback()

    def test_multiple_changes_are_separate_and_historical_value_remains(self):
        self.record_change('3', '3.5')
        self.record_change('3.5', '3.8')
        rows = self.conn.execute('SELECT old_value, new_value FROM finance_settings_history ORDER BY id').fetchall()
        self.assertEqual([(row['old_value'], row['new_value']) for row in rows], [('3', '3.5'), ('3.5', '3.8')])
        self.assertEqual(self.conn.execute("SELECT value FROM settings WHERE key='per_message_rate'").fetchone()[0], '3.8')

    def test_history_indexes_exist_for_setting_time_and_admin(self):
        indexes = {row[1] for row in self.conn.execute('PRAGMA index_list(finance_settings_history)')}
        self.assertTrue({'finance_settings_history_setting_name', 'finance_settings_history_changed_at', 'finance_settings_history_changed_by'} <= indexes)


if __name__ == '__main__':
    unittest.main()
