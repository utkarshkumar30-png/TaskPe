"""Apply the task-type/reward snapshot schema to an explicitly named DB.

This migration never chooses the application's default database implicitly.
"""
import argparse
import json
import os
import sqlite3

import database


def migrate(db_path):
    if not db_path:
        raise ValueError('--db is required')
    path = os.path.abspath(db_path)
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('BEGIN IMMEDIATE')
        before = conn.execute('SELECT COUNT(*) AS count FROM tasks').fetchone()['count']
        history_before = conn.execute(
            "SELECT COUNT(*) AS count FROM sqlite_master WHERE type='table' AND name='finance_settings_history'"
        ).fetchone()['count']
        database.ensure_task_type_and_reward_fields(conn)
        conn.commit()
        columns = [row['name'] for row in conn.execute('PRAGMA table_info(tasks)')]
        return {
            'database': path,
            'task_rows': before,
            'history_table_preexisting': bool(history_before),
            'task_columns': columns,
            'task_type_reward_rows': conn.execute(
                'SELECT COUNT(*) AS count FROM tasks WHERE task_type IS NOT NULL AND reward_per_number_paise IS NOT NULL'
            ).fetchone()['count'],
            'synthetic_history_rows': 0,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True)
    parser.add_argument('--report', action='store_true')
    args = parser.parse_args()
    result = migrate(args.db)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
