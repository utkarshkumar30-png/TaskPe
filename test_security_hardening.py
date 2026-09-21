import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest


_TEST_ROOT = tempfile.mkdtemp(prefix='taskpe-security-')
os.environ.setdefault('TASKPE_DB_PATH', os.path.join(_TEST_ROOT, 'taskpe.db'))
os.environ.setdefault('ENVIRONMENT', 'development')
os.environ.setdefault('SECRET_KEY', 'test-only-development-secret')

import database
import app as app_module
from werkzeug.security import generate_password_hash


class SecurityHardeningTests(unittest.TestCase):
    def setUp(self):
        global app_module
        database.DB_PATH = os.path.join(_TEST_ROOT, 'taskpe.db')
        os.environ['TASKPE_DB_PATH'] = database.DB_PATH
        if os.path.exists(database.DB_PATH):
            os.remove(database.DB_PATH)
        os.environ['ENVIRONMENT'] = 'production'
        os.environ['SECRET_KEY'] = 'production-test-secret-that-is-not-returned'
        os.environ['TASKPE_UPLOAD_FOLDER'] = os.path.join(_TEST_ROOT, 'proofs')
        os.environ['TASKPE_TASK_ASSET_FOLDER'] = os.path.join(_TEST_ROOT, 'assets')
        os.environ.pop('TASKPE_ADMIN_EMAIL', None)
        os.environ.pop('TASKPE_ADMIN_PASSWORD', None)
        os.environ.pop('TASKPE_ADMIN_MOBILE', None)
        database.initialize_database()
        app_module = importlib.reload(app_module)
        app_module.app.config['TESTING'] = True
        self.client = app_module.app.test_client()
        self.upload_dir = tempfile.mkdtemp(prefix='taskpe-proofs-')
        app_module.app.config['UPLOAD_FOLDER'] = self.upload_dir

    def tearDown(self):
        shutil.rmtree(self.upload_dir, ignore_errors=True)

    def _insert_otp(self, mobile, code='654321', expires="2099-01-01 00:00:00"):
        conn = database.get_db_connection()
        conn.execute('INSERT INTO otps (mobile, otp_code, expires_at) VALUES (?, ?, ?)', (mobile, code, expires))
        conn.commit()
        conn.close()

    def _verify(self, mobile, code='654321'):
        return self.client.post('/api/auth/verify-otp', json={'mobile': mobile, 'otp_code': code})

    def _register(self, mobile, email):
        return self.client.post('/api/auth/register', json={
            'name': 'Verified User', 'mobile': mobile, 'email': email,
            'password': 'safe-password', 'referral_code': ''
        })

    def test_production_initialization_has_no_known_seeded_admin(self):
        conn = database.get_db_connection()
        self.assertIsNone(conn.execute("SELECT id FROM users WHERE email='admin@taskpe.com'").fetchone())
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0], 0)
        conn.close()

    def test_production_configuration_and_demo_paths_are_safe(self):
        self.assertFalse(app_module.IS_DEVELOPMENT)
        self.assertTrue(app_module.app.config['SESSION_COOKIE_HTTPONLY'])
        self.assertTrue(app_module.app.config['SESSION_COOKIE_SECURE'])
        self.assertEqual(app_module.app.config['SESSION_COOKIE_SAMESITE'], 'Lax')
        self.assertFalse(app_module.app.config['DEBUG'])
        self.assertEqual(self.client.post('/api/auth/demo-login', json={'role': 'admin'}).status_code, 404)
        self.assertEqual(self._verify('919999999999', '123456').status_code, 400)
        self.assertNotIn(os.environ['SECRET_KEY'].encode(), self.client.get('/api/auth/me').data)

    def test_production_requires_secret_key_at_startup(self):
        env = os.environ.copy()
        env['ENVIRONMENT'] = 'production'
        env.pop('SECRET_KEY', None)
        env['TASKPE_DB_PATH'] = os.path.join(_TEST_ROOT, 'missing-secret.db')
        result = subprocess.run(
            [sys.executable, '-c', 'import app'], cwd=os.path.dirname(__file__),
            env=env, capture_output=True, text=True
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('SECRET_KEY must be set', result.stderr)

    def test_production_requires_explicit_persistent_paths(self):
        env = os.environ.copy()
        env.update({
            'ENVIRONMENT': 'production',
            'SECRET_KEY': 'production-test-secret',
            'TASKPE_UPLOAD_FOLDER': os.path.join(_TEST_ROOT, 'proofs'),
            'TASKPE_TASK_ASSET_FOLDER': os.path.join(_TEST_ROOT, 'assets'),
        })
        env.pop('TASKPE_DB_PATH', None)
        result = subprocess.run(
            [sys.executable, '-c', 'import app'], cwd=os.path.dirname(__file__),
            env=env, capture_output=True, text=True
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('TASKPE_DB_PATH must be set', result.stderr)

    def test_brevo_provider_requires_complete_production_configuration(self):
        env = os.environ.copy()
        env.update({
            'ENVIRONMENT': 'production',
            'SECRET_KEY': 'production-test-secret',
            'TASKPE_DB_PATH': os.path.join(_TEST_ROOT, 'brevo-config.db'),
            'TASKPE_UPLOAD_FOLDER': os.path.join(_TEST_ROOT, 'brevo-proofs'),
            'TASKPE_TASK_ASSET_FOLDER': os.path.join(_TEST_ROOT, 'brevo-assets'),
            'EMAIL_PROVIDER': 'brevo',
        })
        for key in ('BREVO_API_KEY', 'EMAIL_FROM'):
            env.pop(key, None)
        result = subprocess.run(
            [sys.executable, '-c', 'import app'], cwd=os.path.dirname(__file__),
            env=env, capture_output=True, text=True
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Brevo email provider configuration is incomplete', result.stderr)
        self.assertNotIn('production-test-secret', result.stderr)

    def test_registration_requires_matching_single_use_nonexpired_verification(self):
        mobile_a = '919999999901'
        mobile_b = '919999999902'
        self.assertEqual(self._register(mobile_a, 'without@example.com').status_code, 400)

        self._insert_otp(mobile_a)
        self.assertEqual(self._verify(mobile_a).status_code, 200)
        self.assertEqual(self._register(mobile_b, 'wrong-mobile@example.com').status_code, 400)
        registered = self._register(mobile_a, 'verified@example.com')
        self.assertEqual(registered.status_code, 200)
        self.assertTrue(registered.get_json()['success'])
        self.assertEqual(self._register(mobile_a, 'repeat@example.com').status_code, 400)

        conn = database.get_db_connection()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM wallet_transactions WHERE type='referral_bonus'").fetchone()[0], 1)
        conn.close()

        self._insert_otp(mobile_b, expires='2000-01-01 00:00:00')
        self.assertEqual(self._verify(mobile_b).status_code, 400)
        self.assertEqual(self._register(mobile_b, 'expired@example.com').status_code, 400)

    def test_proof_access_is_owner_or_admin_only(self):
        conn = database.get_db_connection()
        cursor = conn.cursor()
        owner = cursor.execute('''
            INSERT INTO users (name, mobile, email, password_hash, role, referral_code)
            VALUES ('Owner', '919999999911', 'owner@example.com', ?, 'user', 'OWN911')
        ''', (generate_password_hash('password'),)).lastrowid
        other = cursor.execute('''
            INSERT INTO users (name, mobile, email, password_hash, role, referral_code)
            VALUES ('Other', '919999999912', 'other@example.com', ?, 'user', 'OTH912')
        ''', (generate_password_hash('password'),)).lastrowid
        admin = cursor.execute('''
            INSERT INTO users (name, mobile, email, password_hash, role, referral_code)
            VALUES ('Admin', '919999999913', 'admin-test@example.com', ?, 'admin', 'ADM913')
        ''', (generate_password_hash('password'),)).lastrowid
        cursor.execute('''
            INSERT INTO submissions (user_id, task_id, proof_image, payout_amount)
            VALUES (?, 1, 'proof-owner.png', 0)
        ''', (owner,))
        conn.commit()
        conn.close()
        with open(os.path.join(self.upload_dir, 'proof-owner.png'), 'wb') as f:
            f.write(b'proof')

        response = self.client.get('/uploads/proofs/proof-owner.png')
        self.assertEqual(response.status_code, 401)
        response.close()
        with self.client.session_transaction() as session:
            session['user_id'] = other
        response = self.client.get('/uploads/proofs/proof-owner.png')
        self.assertEqual(response.status_code, 403)
        response.close()
        with self.client.session_transaction() as session:
            session['user_id'] = owner
        response = self.client.get('/uploads/proofs/proof-owner.png')
        self.assertEqual(response.status_code, 200)
        response.close()
        with self.client.session_transaction() as session:
            session['user_id'] = admin
        response = self.client.get('/uploads/proofs/proof-owner.png')
        self.assertEqual(response.status_code, 200)
        response.close()
        response = self.client.get('/uploads/proofs/..%2Ftaskpe.db')
        self.assertIn(response.status_code, (404, 308))
        response.close()

    def test_admin_page_and_admin_api_require_an_authenticated_admin(self):
        # admin_required intentionally uses 403 for every denied admin request,
        # including a missing session. This keeps the page and admin APIs on one
        # authorization contract without exposing authentication state.
        response = self.client.get('/admin')
        self.assertEqual(response.status_code, 403)
        response.close()
        self.assertEqual(self.client.get('/api/admin/stats').status_code, 403)
        conn = database.get_db_connection()
        user_id = conn.execute('''
            INSERT INTO users (name, mobile, email, password_hash, role, referral_code)
            VALUES ('Worker', '919999999920', 'worker-admin-page@example.com', ?, 'user', 'WRK920')
        ''', (generate_password_hash('password'),)).lastrowid
        admin_id = conn.execute('''
            INSERT INTO users (name, mobile, email, password_hash, role, referral_code)
            VALUES ('Administrator', '919999999921', 'administrator-page@example.com', ?, 'admin', 'ADM921')
        ''', (generate_password_hash('password'),)).lastrowid
        task = conn.execute("SELECT id FROM tasks ORDER BY id LIMIT 1").fetchone()
        if task:
            task_id = task['id']
        else:
            task_id = conn.execute('''
                INSERT INTO tasks
                    (title, category, description, payout, template_text, target_numbers,
                     recipients_per_slot, time_limit_minutes, proof_instructions, total_slots)
                VALUES ('Security test task', 'Direct Message', 'Security test', 0,
                        'Test message', '[]', 1, 25, 'Upload proof', 1)
            ''').lastrowid
        conn.execute('''
            INSERT INTO submissions (user_id, task_id, proof_image, payout_amount)
            VALUES (?, ?, '', 0)
        ''', (user_id, task_id))
        conn.commit()
        conn.close()
        with self.client.session_transaction() as session:
            session['user_id'] = user_id
        self.assertEqual(self.client.get('/admin/').status_code, 403)
        self.assertEqual(self.client.get('/api/admin/submissions').status_code, 403)
        with self.client.session_transaction() as session:
            session['user_id'] = admin_id
        page = self.client.get('/admin')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'TaskPe Admin Control Center', page.data)
        self.assertEqual(self.client.get('/api/admin/stats').status_code, 200)
        self.assertEqual(self.client.get('/api/admin/submissions').status_code, 200)
        submissions = self.client.get('/api/admin/submissions').get_json()['submissions']
        missing_proof = next(item for item in submissions if item['proof_image'] == '')
        self.assertEqual(missing_proof['proof_urls'], [])
        self.assertIsNone(missing_proof['proof_url'])
        self.assertEqual(self.client.get('/api/admin/withdrawals').status_code, 200)
        self.assertEqual(self.client.get('/api/admin/users').status_code, 200)
        self.assertEqual(self.client.get('/api/admin/settings').status_code, 200)


if __name__ == '__main__':
    unittest.main()
