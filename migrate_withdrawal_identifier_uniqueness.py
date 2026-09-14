"""Safely add unique payout-identifier indexes to an explicit SQLite database."""

import argparse
import json
import sqlite3
from pathlib import Path

import database


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True, help='Explicit SQLite database path')
    parser.add_argument('--report', required=True, help='JSON report output path')
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    report_path = Path(args.report).resolve()
    report = {'database': str(db_path), 'status': 'failed', 'collisions': {}}
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('BEGIN IMMEDIATE')
        collisions = database.withdrawal_identifier_collisions(conn)
        report['collisions'] = collisions
        if collisions:
            report['status'] = 'blocked_existing_collisions'
            conn.rollback()
            return 2
        database.ensure_withdrawal_identifier_indexes(conn)
        conn.commit()
        report['status'] = 'applied'
        return 0
    except Exception as exc:
        conn.rollback()
        report['error'] = f'{type(exc).__name__}: {exc}'
        return 1
    finally:
        conn.close()
        report_path.write_text(json.dumps(report, indent=2, default=str) + '\n', encoding='utf-8')


if __name__ == '__main__':
    raise SystemExit(main())
