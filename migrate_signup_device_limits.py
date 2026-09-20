"""Add dual-signup and device-limit schema to an explicitly selected SQLite DB."""

import argparse
import json
import sqlite3
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True)
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    db_path = Path(args.db).resolve()
    report_path = Path(args.report).resolve()
    report = {'database': str(db_path), 'status': 'failed'}
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        conn.execute('BEGIN IMMEDIATE')
        user_columns = {row[1] for row in conn.execute('PRAGMA table_info(users)')}
        if 'signup_method' not in user_columns:
            conn.execute('ALTER TABLE users ADD COLUMN signup_method TEXT')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS device_registrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id_hash TEXT NOT NULL,
                signup_method TEXT NOT NULL CHECK (signup_method IN ('MOBILE', 'EMAIL')),
                user_id INTEGER NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS device_registrations_device_idx ON device_registrations(device_id_hash)')
        conn.execute('CREATE INDEX IF NOT EXISTS device_registrations_method_idx ON device_registrations(signup_method)')
        conn.execute('CREATE INDEX IF NOT EXISTS device_registrations_created_idx ON device_registrations(created_at)')
        conn.execute('CREATE INDEX IF NOT EXISTS device_registrations_user_idx ON device_registrations(user_id)')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS email_verifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                expires_at DATETIME NOT NULL,
                used_at DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS email_verifications_email_idx ON email_verifications(email)')
        conn.execute('CREATE INDEX IF NOT EXISTS email_verifications_expires_idx ON email_verifications(expires_at)')
        conn.commit()
        report['status'] = 'applied'
        return 0
    except Exception as exc:
        conn.rollback()
        report['error'] = f'{type(exc).__name__}: {exc}'
        return 1
    finally:
        conn.close()
        report_path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    raise SystemExit(main())
