import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from werkzeug.security import check_password_hash


ROOT = Path(__file__).resolve().parent
PYTHON = sys.executable


class StartupSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix='taskpe-startup-safety-')
        self.db_path = Path(self.temp_dir.name) / 'taskpe.db'

    def tearDown(self):
        self.temp_dir.cleanup()

    def env(self, *, environment='production', secret='startup-test-secret', **extra):
        values = os.environ.copy()
        values.update({
            'TASKPE_DB_PATH': str(self.db_path),
            'ENVIRONMENT': environment,
            'SECRET_KEY': secret,
            'SMS_PROVIDER': 'disabled',
            'PAYOUT_MODE': 'MANUAL',
            'TRUSTED_PROXY_HOPS': '0',
            'ENABLE_HSTS': '0',
            'TASKPE_UPLOAD_FOLDER': str(Path(self.temp_dir.name) / 'proofs'),
            'TASKPE_TASK_ASSET_FOLDER': str(Path(self.temp_dir.name) / 'assets'),
        })
        values.update(extra)
        return values

    def run_python(self, code, *, env=None):
        return subprocess.run(
            [PYTHON, '-c', code], cwd=ROOT, env=env or self.env(),
            capture_output=True, text=True, check=False,
        )

    def initialize(self, *, environment='production', **extra):
        env = self.env(environment=environment, **extra)
        return subprocess.run(
            [PYTHON, 'initialize_database.py', '--db', str(self.db_path)],
            cwd=ROOT, env=env, capture_output=True, text=True, check=False,
        )

    def sha256(self):
        digest = hashlib.sha256()
        with self.db_path.open('rb') as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(block)
        return digest.hexdigest()

    def test_incomplete_production_schema_fails_without_modifying_database(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute('CREATE TABLE marker (id INTEGER PRIMARY KEY)')
            conn.commit()
        finally:
            conn.close()
        before = self.sha256()

        result = self.run_python('import app')

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Database schema is incomplete', result.stderr)
        self.assertEqual(before, self.sha256())

    def test_missing_production_database_fails_without_creating_file(self):
        self.assertFalse(self.db_path.exists())

        result = self.run_python('import app')

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Database does not exist', result.stderr)
        self.assertFalse(self.db_path.exists())

    def test_fresh_development_database_initializes_and_verifies(self):
        result = self.initialize(environment='development')

        self.assertEqual(result.returncode, 0, result.stderr)
        verify = self.run_python('import database; database.verify_required_schema(); print("ok")', env=self.env(environment='development'))
        self.assertEqual(verify.returncode, 0, verify.stderr)
        self.assertIn('ok', verify.stdout)

    def test_complete_production_database_import_is_read_only(self):
        initialized = self.initialize()
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        before = self.sha256()

        result = self.run_python('import app; print(app.IS_PRODUCTION, app.app.config["DEBUG"])')

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('True False', result.stdout)
        self.assertEqual(before, self.sha256())

    def test_explicit_production_bootstrap_hashes_password_and_is_not_overwritten(self):
        first = self.initialize(
            TASKPE_ADMIN_EMAIL='admin-one@example.test',
            TASKPE_ADMIN_PASSWORD='first-long-test-password',
            TASKPE_ADMIN_MOBILE='919876543210',
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                'SELECT role, password_hash FROM users WHERE role = "admin"'
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 'admin')
        self.assertTrue(check_password_hash(row[1], 'first-long-test-password'))
        self.assertNotEqual(row[1], 'first-long-test-password')

        second = self.initialize(
            TASKPE_ADMIN_EMAIL='admin-two@example.test',
            TASKPE_ADMIN_PASSWORD='second-long-test-password',
            TASKPE_ADMIN_MOBILE='919876543211',
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        conn = sqlite3.connect(self.db_path)
        try:
            admins = conn.execute(
                'SELECT email, password_hash FROM users WHERE role = "admin"'
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(admins), 1)
        self.assertEqual(admins[0][0], 'admin-one@example.test')
        self.assertTrue(check_password_hash(admins[0][1], 'first-long-test-password'))


if __name__ == '__main__':
    unittest.main()
