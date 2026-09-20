import os
import importlib
import tempfile
import unittest
from werkzeug.security import generate_password_hash


ROOT = tempfile.mkdtemp(prefix='taskpe-login-transition-')
TEST_DB_PATH = os.path.join(ROOT, 'taskpe.db')
os.environ['TASKPE_DB_PATH'] = TEST_DB_PATH
os.environ['ENVIRONMENT'] = 'development'
os.environ['SECRET_KEY'] = 'login-transition-test-secret'

import database
import app as app_module

database.DB_PATH = os.environ['TASKPE_DB_PATH']
app_module = importlib.reload(app_module)
app = app_module.app


class LoginTransitionTests(unittest.TestCase):
    def setUp(self):
        database.DB_PATH = TEST_DB_PATH
        conn = database.get_db_connection()
        if not conn.execute("SELECT 1 FROM users WHERE email = ?", ('earner@taskpe.com',)).fetchone():
            conn.execute('''
                INSERT INTO users (name, mobile, email, password_hash, role, referral_code)
                VALUES (?, ?, ?, ?, 'user', ?)
            ''', ('Rohan Sharma', '9876512345', 'earner@taskpe.com', generate_password_hash('user123'), 'ROHAN100'))
        if not conn.execute("SELECT 1 FROM users WHERE email = ?", ('legacy-mobile@example.com',)).fetchone():
            conn.execute('''
                INSERT INTO users (name, mobile, email, password_hash, role, referral_code)
                VALUES (?, ?, ?, ?, 'user', ?)
            ''', ('Legacy Mobile User', '919876598765', 'legacy-mobile@example.com', generate_password_hash('legacy-password'), 'LEGACY1'))
        conn.commit()
        conn.close()
        app.config['TESTING'] = True
        self.client = app.test_client()

    def test_valid_login_returns_authenticated_user_without_reload(self):
        response = self.client.post('/api/auth/login', json={
            'identifier': '9876512345', 'password': 'user123',
        })
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertIn('user', payload)
        self.assertEqual(self.client.get('/api/auth/me').get_json()['logged_in'], True)

        script_path = os.path.join(os.path.dirname(__file__), 'static', 'js', 'app.js')
        with open(script_path, encoding='utf-8') as script_file:
            script = script_file.read()
        success_path = script[script.index('if (data.success) {'):script.index('function togglePasswordVisibility')]
        self.assertIn('currentUser = data.user;', success_path)
        self.assertIn('closeAuthModal();', success_path)
        self.assertIn("navigate('tasks');", success_path)

    def test_invalid_login_does_not_authenticate(self):
        response = self.client.post('/api/auth/login', json={
            'identifier': '9876512345', 'password': 'wrong-password',
        })
        self.assertEqual(response.status_code, 401)
        self.assertFalse(response.get_json()['success'])
        self.assertFalse(self.client.get('/api/auth/me').get_json()['logged_in'])

    def test_mobile_login_accepts_local_and_indian_country_prefix_forms(self):
        for identifier in ('+919876512345', '919876512345'):
            response = self.client.post('/api/auth/login', json={
                'identifier': identifier, 'password': 'user123',
            })
            self.assertEqual(response.status_code, 200, identifier)
            self.client.post('/api/auth/logout')

    def test_legacy_country_prefixed_mobile_accepts_local_form(self):
        response = self.client.post('/api/auth/login', json={
            'identifier': '9876598765', 'password': 'legacy-password',
        })
        self.assertEqual(response.status_code, 200)

    def test_email_login_and_mobile_password_verification_remain_strict(self):
        email_login = self.client.post('/api/auth/login', json={
            'identifier': 'earner@taskpe.com', 'password': 'user123',
        })
        self.assertEqual(email_login.status_code, 200)
        self.client.post('/api/auth/logout')

        wrong_password = self.client.post('/api/auth/login', json={
            'identifier': '+919876598765', 'password': 'wrong-password',
        })
        self.assertEqual(wrong_password.status_code, 401)
        unknown_mobile = self.client.post('/api/auth/login', json={
            'identifier': '+919999999999', 'password': 'user123',
        })
        self.assertEqual(unknown_mobile.status_code, 401)

    def test_logout_rotates_csrf_and_second_login_succeeds(self):
        first_token = self.client.get('/api/auth/csrf').get_json()['csrf_token']
        self.assertEqual(self.client.post('/api/auth/login', json={
            'identifier': '9876512345', 'password': 'user123',
        }, headers={'X-CSRF-Token': first_token, 'Origin': 'https://staging.taskpe.test'}).status_code, 200)
        self.assertEqual(self.client.post('/api/auth/logout', headers={
            'X-CSRF-Token': first_token, 'Origin': 'https://staging.taskpe.test',
        }).status_code, 200)
        second_token = self.client.get('/api/auth/csrf').get_json()['csrf_token']
        self.assertNotEqual(first_token, second_token)
        self.assertEqual(self.client.post('/api/auth/login', json={
            'identifier': '9876512345', 'password': 'user123',
        }, headers={'X-CSRF-Token': second_token, 'Origin': 'https://staging.taskpe.test'}).status_code, 200)
        self.assertEqual(self.client.post('/api/auth/login', json={
            'identifier': '9876512345', 'password': 'user123',
        }, headers={'X-CSRF-Token': first_token, 'Origin': 'https://staging.taskpe.test'}).status_code, 403)

    def test_csrf_rejection_remains_enabled(self):
        response = self.client.post('/api/auth/login', json={
            'identifier': '9876512345', 'password': 'user123',
        }, headers={'X-CSRF-Token': 'intentionally-invalid', 'Origin': 'https://staging.taskpe.test'})
        self.assertEqual(response.status_code, 403)


if __name__ == '__main__':
    unittest.main()
