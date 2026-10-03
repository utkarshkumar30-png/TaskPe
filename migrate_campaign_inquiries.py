"""Create the campaign inquiry table on an explicitly selected SQLite DB.

This migration is additive and idempotent. It never chooses the application's
default database implicitly and never deletes or rewrites existing data.
"""

import argparse
import json
import os
import sqlite3


def migrate(db_path):
    if not db_path:
        raise ValueError('--db is required')
    path = os.path.abspath(db_path)
    if not os.path.isfile(path):
        raise FileNotFoundError(path)

    conn = sqlite3.connect(path, timeout=30)
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS campaign_inquiries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_name TEXT NOT NULL,
                contact_person TEXT NOT NULL,
                email TEXT NOT NULL,
                phone TEXT,
                task_type TEXT NOT NULL,
                target_location TEXT,
                participant_count INTEGER,
                campaign_description TEXT NOT NULL,
                estimated_budget TEXT,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                status TEXT NOT NULL DEFAULT 'new'
                    CHECK (status IN ('new', 'contacted', 'closed'))
            )
        ''')
        conn.execute('''
            CREATE INDEX IF NOT EXISTS campaign_inquiries_status_created
            ON campaign_inquiries(status, created_at)
        ''')
        conn.commit()
        return {'database': path, 'status': 'applied'}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True, help='Explicit SQLite database path')
    args = parser.parse_args()
    print(json.dumps(migrate(args.db), indent=2))


if __name__ == '__main__':
    main()
