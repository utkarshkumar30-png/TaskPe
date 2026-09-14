"""Idempotent exact-money migration.

Usage (always against a copied database first):
  python migrate_exact_money.py --db taskpe.db.exact_money_test

The script never deletes or rewrites legacy financial columns.  It adds exact
integer columns, converts every existing value deterministically, and aborts
the transaction if any value exceeds the declared precision.
"""

import argparse
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

from money import inr_to_paise, usdt_to_micro, rate_paise_per_unit, percentage_to_bps


REPORT_FIELDS = ('table', 'record_id', 'legacy_value', 'exact_value', 'status')


def add_column(conn, table, column):
    cols = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
    if column not in cols:
        conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} INTEGER')


def convert_rows(conn, table, source, target, converter, report):
    rows = conn.execute(f'SELECT id, {source} AS legacy, {target} AS exact FROM {table}').fetchall()
    for row in rows:
        if row['legacy'] is None:
            report.append({'table': table, 'record_id': row['id'], 'legacy_value': None, 'exact_value': None, 'status': 'skipped_null'})
            continue
        if row['exact'] is not None:
            report.append({'table': table, 'record_id': row['id'], 'legacy_value': row['legacy'], 'exact_value': row['exact'], 'status': 'already_migrated'})
            continue
        exact = converter(row['legacy'])
        conn.execute(f'UPDATE {table} SET {target} = ? WHERE id = ?', (exact, row['id']))
        report.append({'table': table, 'record_id': row['id'], 'legacy_value': row['legacy'], 'exact_value': exact, 'status': 'converted'})


def migrate(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    report = []
    try:
        conn.execute('BEGIN IMMEDIATE')
        for table, column in (
            ('users', 'wallet_balance_paise'), ('users', 'total_earned_paise'),
            ('tasks', 'payout_paise'), ('submissions', 'payout_paise'),
            ('submissions', 'per_number_rate_paise'),
            ('wallet_transactions', 'amount_paise'), ('wallet_transactions', 'balance_after_paise'),
            ('withdrawals', 'amount_paise'), ('withdrawals', 'usdt_amount_micro'),
            ('withdrawals', 'usdt_inr_rate_scaled'),
            ('target_number_records', 'reward_per_number_paise'), ('target_number_records', 'paid_amount_paise'),
            ('target_payments', 'amount_paise'), ('target_payments', 'rate_paise'),
        ):
            add_column(conn, table, column)

        convert_rows(conn, 'users', 'wallet_balance', 'wallet_balance_paise', inr_to_paise, report)
        convert_rows(conn, 'users', 'total_earned', 'total_earned_paise', inr_to_paise, report)
        convert_rows(conn, 'tasks', 'payout', 'payout_paise', inr_to_paise, report)
        convert_rows(conn, 'submissions', 'payout_amount', 'payout_paise', inr_to_paise, report)
        convert_rows(conn, 'submissions', 'per_number_rate', 'per_number_rate_paise', inr_to_paise, report)
        convert_rows(conn, 'wallet_transactions', 'amount', 'amount_paise', inr_to_paise, report)
        convert_rows(conn, 'wallet_transactions', 'balance_after', 'balance_after_paise', inr_to_paise, report)
        convert_rows(conn, 'withdrawals', 'amount', 'amount_paise', inr_to_paise, report)
        convert_rows(conn, 'withdrawals', 'usdt_amount', 'usdt_amount_micro', usdt_to_micro, report)
        convert_rows(conn, 'withdrawals', 'usdt_inr_rate', 'usdt_inr_rate_scaled', rate_paise_per_unit, report)
        convert_rows(conn, 'target_number_records', 'reward_per_number', 'reward_per_number_paise', inr_to_paise, report)
        convert_rows(conn, 'target_number_records', 'paid_amount', 'paid_amount_paise', inr_to_paise, report)
        convert_rows(conn, 'target_payments', 'amount', 'amount_paise', inr_to_paise, report)
        convert_rows(conn, 'target_payments', 'rate', 'rate_paise', inr_to_paise, report)

        # Settings are text today; retain them and record exact scaled values.
        conn.execute('''CREATE TABLE IF NOT EXISTS exact_money_settings (
            key TEXT PRIMARY KEY, value_scaled INTEGER NOT NULL, scale INTEGER NOT NULL,
            kind TEXT NOT NULL, migrated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )''')
        settings = conn.execute('SELECT key, value FROM settings').fetchall()
        converters = {
            'per_message_rate': (inr_to_paise, 100, 'inr'),
            'minimum_withdrawal_amount': (inr_to_paise, 100, 'inr'),
            'usdt_inr_rate': (rate_paise_per_unit, 100, 'inr_per_usdt'),
            'referral_commission_percent': (percentage_to_bps, 100, 'percent_bps'),
        }
        for row in settings:
            if row['key'] not in converters:
                continue
            converter, scale, kind = converters[row['key']]
            exact = converter(row['value'])
            existing = conn.execute('SELECT value_scaled FROM exact_money_settings WHERE key = ?', (row['key'],)).fetchone()
            conn.execute('''INSERT INTO exact_money_settings(key,value_scaled,scale,kind)
                            VALUES(?,?,?,?)
                            ON CONFLICT(key) DO UPDATE SET value_scaled=excluded.value_scaled,
                            scale=excluded.scale, kind=excluded.kind''', (row['key'], exact, scale, kind))
            report.append({'table': 'settings', 'record_id': row['key'], 'legacy_value': row['value'], 'exact_value': exact, 'status': 'already_migrated' if existing else 'converted'})

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True, help='Copied SQLite database path')
    parser.add_argument('--report', help='Optional JSON report path')
    args = parser.parse_args()
    report = migrate(args.db)
    output = args.report or f'{args.db}.exact-money-report.json'
    Path(output).write_text(json.dumps(report, indent=2, default=str), encoding='utf-8')
    print(json.dumps({'database': str(Path(args.db).resolve()), 'report': str(Path(output).resolve()), 'records': len(report), 'converted': sum(r['status'] == 'converted' for r in report)}, indent=2))


if __name__ == '__main__':
    main()
