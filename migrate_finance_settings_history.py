"""Create the immutable finance-settings history table on an explicit SQLite DB."""

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
    report = {'database': str(db_path), 'status': 'failed', 'existing_history_rows': 0}
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        conn.execute('BEGIN IMMEDIATE')
        try:
            report['existing_history_rows'] = conn.execute(
                'SELECT COUNT(*) FROM finance_settings_history'
            ).fetchone()[0]
        except sqlite3.OperationalError:
            report['existing_history_rows'] = 0
        database.ensure_finance_settings_history(conn)
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
