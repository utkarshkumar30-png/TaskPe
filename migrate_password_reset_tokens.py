"""Create the password-reset token table on an explicitly selected SQLite DB."""

import argparse
import json
import sqlite3
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True, help='Explicit SQLite database path')
    parser.add_argument('--report', required=True, help='JSON report output path')
    args = parser.parse_args()
    db_path = Path(args.db).resolve()
    report_path = Path(args.report).resolve()
    report = {'database': str(db_path), 'status': 'failed'}
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                mobile TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                expires_at DATETIME NOT NULL,
                used_at DATETIME,
                verified_at DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        ''')
        columns = {row[1] for row in conn.execute('PRAGMA table_info(password_reset_tokens)')}
        if 'verified_at' not in columns:
            conn.execute('ALTER TABLE password_reset_tokens ADD COLUMN verified_at DATETIME')
        conn.execute('CREATE INDEX IF NOT EXISTS password_reset_tokens_user_idx ON password_reset_tokens(user_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS password_reset_tokens_mobile_idx ON password_reset_tokens(mobile)')
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
