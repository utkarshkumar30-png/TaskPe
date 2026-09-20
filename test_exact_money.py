import json
import shutil
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from money import (
    bps_of_paise,
    inr_to_paise,
    micro_to_usdt,
    paise_to_inr,
    percentage_to_bps,
    usdt_to_micro,
)
from migrate_exact_money import migrate


class ExactMoneyTests(unittest.TestCase):
    @staticmethod
    def _restore_legacy_money_columns(copy):
        """Make only the disposable test copy exercise first-run conversion."""
        columns = (
            ('users', 'wallet_balance_paise'),
            ('users', 'total_earned_paise'),
            ('tasks', 'payout_paise'),
            ('submissions', 'payout_paise'),
            ('submissions', 'per_number_rate_paise'),
            ('wallet_transactions', 'amount_paise'),
            ('wallet_transactions', 'balance_after_paise'),
            ('withdrawals', 'amount_paise'),
            ('withdrawals', 'usdt_amount_micro'),
            ('withdrawals', 'usdt_inr_rate_scaled'),
            ('target_number_records', 'reward_per_number_paise'),
            ('target_number_records', 'paid_amount_paise'),
            ('target_payments', 'amount_paise'),
            ('target_payments', 'rate_paise'),
        )
        conn = sqlite3.connect(copy)
        try:
            for table, column in columns:
                conn.execute(f'UPDATE {table} SET {column} = NULL')
            conn.commit()
        finally:
            conn.close()

    def test_inr_conversion_is_exact(self):
        self.assertEqual(inr_to_paise(Decimal('0.10')) + inr_to_paise(Decimal('0.20')), 30)
        self.assertEqual(paise_to_inr(30), Decimal('0.30'))

    def test_task_rewards_are_exact(self):
        rate = inr_to_paise('3.00')
        self.assertEqual(rate * 20, 6000)
        self.assertEqual(rate * 25, 7500)

    def test_referral_percentages_use_basis_points(self):
        self.assertEqual(bps_of_paise(300, percentage_to_bps('10')), 30)
        self.assertEqual(bps_of_paise(300, percentage_to_bps('7.5')), 23)
        self.assertEqual(bps_of_paise(300, percentage_to_bps('12.5')), 38)

    def test_usdt_precision_is_six_decimal_places(self):
        micro = usdt_to_micro('0.035000')
        self.assertEqual(micro, 35000)
        self.assertEqual(micro_to_usdt(micro), Decimal('0.035'))
        with self.assertRaises(ValueError):
            usdt_to_micro('0.0000001')

    def test_inr_precision_rejects_unrepresentable_values(self):
        with self.assertRaises(ValueError):
            inr_to_paise('0.001')

    def test_migration_populates_exact_columns_without_deleting_history(self):
        source = Path(__file__).with_name('taskpe.db')
        with tempfile.TemporaryDirectory(prefix='taskpe-exact-money-') as directory:
            copy = Path(directory) / 'copy.db'
            shutil.copy2(source, copy)
            self._restore_legacy_money_columns(copy)
            check = sqlite3.connect(copy)
            try:
                before = check.execute('SELECT COUNT(*) FROM wallet_transactions').fetchone()[0]
            finally:
                check.close()
            report = migrate(str(copy))
            conn = sqlite3.connect(copy)
            try:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM wallet_transactions').fetchone()[0], before)
                self.assertEqual(conn.execute('SELECT wallet_balance_paise FROM users WHERE id=1').fetchone()[0], 2000)
                self.assertTrue(any(row['table'] == 'users' and row['status'] == 'converted' for row in report))
            finally:
                conn.close()

    def test_migration_is_idempotent(self):
        source = Path(__file__).with_name('taskpe.db')
        with tempfile.TemporaryDirectory(prefix='taskpe-exact-money-') as directory:
            copy = Path(directory) / 'copy.db'
            shutil.copy2(source, copy)
            migrate(str(copy))
            second = migrate(str(copy))
            self.assertFalse(any(row['status'] == 'converted' for row in second))

    def test_historical_duplicate_task_reward_records_are_preserved(self):
        source = Path(__file__).with_name('taskpe.db')
        with tempfile.TemporaryDirectory(prefix='taskpe-exact-money-') as directory:
            copy = Path(directory) / 'copy.db'
            shutil.copy2(source, copy)
            check = sqlite3.connect(copy)
            try:
                before = check.execute("SELECT COUNT(*) FROM wallet_transactions WHERE reference_id='TASK-1'").fetchone()[0]
            finally:
                check.close()
            migrate(str(copy))
            check = sqlite3.connect(copy)
            try:
                after = check.execute("SELECT COUNT(*) FROM wallet_transactions WHERE reference_id='TASK-1'").fetchone()[0]
            finally:
                check.close()
            self.assertEqual(before, 2)
            self.assertEqual(after, 2)


if __name__ == '__main__':
    unittest.main()
