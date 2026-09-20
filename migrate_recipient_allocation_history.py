"""Migrate recipient allocations to append-only allocation events.

Always provide an explicit disposable or approved database path with --db.
"""
import argparse
import json
import os
import sqlite3

import database


def migrate(db_path):
    path = os.path.abspath(db_path)
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('BEGIN IMMEDIATE')
        before = conn.execute('SELECT COUNT(*) AS count FROM recipient_allocations').fetchone()['count']
        database.ensure_recipient_allocation_history(conn)
        conn.commit()
        return {
            'database': path,
            'allocation_rows_before': before,
            'allocation_rows_after': conn.execute('SELECT COUNT(*) FROM recipient_allocations').fetchone()[0],
            'active_unique_index': bool(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='recipient_allocations_one_active'"
            ).fetchone()),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True)
    args = parser.parse_args()
    print(json.dumps(migrate(args.db), indent=2))


if __name__ == '__main__':
    main()
