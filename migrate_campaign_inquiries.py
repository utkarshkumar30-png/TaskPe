"""Upgrade campaign inquiries for the admin request-management workflow.

The migration is explicit, transactional, idempotent, and data-preserving.
It never selects the application's default database implicitly.
"""

import argparse
import json
import os
import re
import sqlite3


STATUSES = ('new', 'contacted', 'negotiating', 'approved', 'active', 'completed', 'closed')


def has_current_status_constraint(create_sql):
    normalized = re.sub(r'\s+', ' ', (create_sql or '').lower())
    match = re.search(r'check\s*\(\s*status\s+in\s*\(([^)]*)\)\s*\)', normalized)
    if not match:
        return False
    values = tuple(re.findall(r"'([^']+)'", match.group(1)))
    return set(values) == set(STATUSES) and len(values) == len(STATUSES)


def ensure_active_status_index(conn):
    index_name = 'campaign_inquiries_status_created'
    index_row = conn.execute(
        "SELECT tbl_name FROM sqlite_master WHERE type = 'index' AND name = ?",
        (index_name,),
    ).fetchone()
    if index_row and index_row['tbl_name'] != 'campaign_inquiries':
        conn.execute(f'DROP INDEX "{index_name}"')
    active_indexes = {
        row['name'] for row in conn.execute("PRAGMA index_list('campaign_inquiries')")
    }
    if index_name not in active_indexes:
        conn.execute(
            f'CREATE INDEX "{index_name}" ON campaign_inquiries(status, created_at)'
        )


def migrate(db_path):
    if not db_path:
        raise ValueError('--db is required')
    path = os.path.abspath(db_path)
    if not os.path.isfile(path):
        raise FileNotFoundError(path)

    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('BEGIN IMMEDIATE')
        table = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'campaign_inquiries'").fetchone()
        if not table:
            conn.execute('''
                CREATE TABLE campaign_inquiries (
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
                    admin_notes TEXT,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    status TEXT NOT NULL DEFAULT 'new'
                        CHECK (status IN ('new', 'contacted', 'negotiating', 'approved', 'active', 'completed', 'closed'))
                )
            ''')
        else:
            columns = {row['name'] for row in conn.execute('PRAGMA table_info(campaign_inquiries)')}
            if 'admin_notes' not in columns:
                conn.execute('ALTER TABLE campaign_inquiries ADD COLUMN admin_notes TEXT')
            if not has_current_status_constraint(table['sql']):
                conn.execute('ALTER TABLE campaign_inquiries RENAME TO campaign_inquiries_legacy')
                conn.execute('''
                    CREATE TABLE campaign_inquiries (
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
                        admin_notes TEXT,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        status TEXT NOT NULL DEFAULT 'new'
                            CHECK (status IN ('new', 'contacted', 'negotiating', 'approved', 'active', 'completed', 'closed'))
                    )
                ''')
                conn.execute('''
                    INSERT INTO campaign_inquiries
                        (id, company_name, contact_person, email, phone, task_type,
                         target_location, participant_count, campaign_description,
                         estimated_budget, admin_notes, created_at, status)
                    SELECT id, company_name, contact_person, email, phone, task_type,
                           target_location, participant_count, campaign_description,
                           estimated_budget, admin_notes, created_at, status
                    FROM campaign_inquiries_legacy
                ''')
                # Retain the original table as a recoverable, data-preserving
                # snapshot; no existing table or row is deleted.
                conn.execute('ALTER TABLE campaign_inquiries_legacy RENAME TO campaign_inquiries_legacy_before_campaign_crm')
        ensure_active_status_index(conn)
        conn.commit()
        return {'database': path, 'status': 'applied', 'supported_statuses': list(STATUSES)}
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
    raise SystemExit(main())
