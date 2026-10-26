"""Explicitly remove the historical single-declaration-per-task constraint.

This migration is data-preserving and must be run against an explicitly named
disposable or approved database. It is never called by application startup or
deploy.sh.
"""

import argparse
import json
import os
import sqlite3


TABLE = 'whatsapp_campaign_consent_declarations'
ARCHIVE = 'whatsapp_campaign_consent_declarations_before_history'


def migrate(db_path):
    path = os.path.abspath(db_path)
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('BEGIN IMMEDIATE')
        table = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (TABLE,),
        ).fetchone()
        if not table:
            raise RuntimeError(f'{TABLE} does not exist.')
        if 'TASK_ID INTEGER NOT NULL UNIQUE' not in (table['sql'] or '').upper():
            conn.commit()
            return {'database': path, 'status': 'already_applied'}

        conn.execute('DROP INDEX IF EXISTS whatsapp_campaign_consent_lookup')
        conn.execute(f'ALTER TABLE {TABLE} RENAME TO {ARCHIVE}')
        conn.execute('''
            CREATE TABLE whatsapp_campaign_consent_declarations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                campaign_id INTEGER NOT NULL,
                business_user_id INTEGER NOT NULL,
                declaration_text TEXT NOT NULL,
                normalized_recipient_list TEXT NOT NULL,
                recipient_count INTEGER NOT NULL CHECK (recipient_count > 0),
                recipient_fingerprint TEXT NOT NULL CHECK (length(recipient_fingerprint) = 64),
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (task_id) REFERENCES tasks(id),
                FOREIGN KEY (business_user_id) REFERENCES users(id)
            )
        ''')
        conn.execute('''
            INSERT INTO whatsapp_campaign_consent_declarations
                (id, task_id, campaign_id, business_user_id, declaration_text,
                 normalized_recipient_list, recipient_count, recipient_fingerprint, created_at)
            SELECT id, task_id, campaign_id, business_user_id, declaration_text,
                   normalized_recipient_list, recipient_count, recipient_fingerprint, created_at
            FROM whatsapp_campaign_consent_declarations_before_history
        ''')
        conn.execute('''
            CREATE INDEX whatsapp_campaign_consent_lookup
            ON whatsapp_campaign_consent_declarations(business_user_id, task_id, campaign_id)
        ''')
        conn.commit()
        return {
            'database': path,
            'status': 'applied',
            'historical_table': ARCHIVE,
            'declaration_count': conn.execute(
                'SELECT COUNT(*) FROM whatsapp_campaign_consent_declarations'
            ).fetchone()[0],
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
