import importlib
import os
import shutil
import tempfile
import unittest


ROOT = tempfile.mkdtemp(prefix='taskpe-reset-tests-')
os.environ['TASKPE_DB_PATH'] = os.path.join(ROOT, 'taskpe.db')
os.environ['ENVIRONMENT'] = 'development'
os.environ['SECRET_KEY'] = 'reset-test-secret'
os.environ['SMS_PROVIDER'] = 'disabled'

import database
import app as app_module


class PasswordResetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database.DB_PATH = os.environ['TASKPE_DB_PATH']
        cls.app_module = importlib.reload(app_module)
        cls.app_module.app.config['TESTING'] = True

    def setUp(self):
        self.client = self.app_module.app.test_client()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(ROOT, ignore_errors=True)

    def test_ui_has_reset_flow_and_visibility_controls(self):
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('Forgot Password?', html)
        self.assertIn('form-forgot-password', html)
        self.assertIn('form-reset-password', html)
        self.assertGreaterEqual(html.count('togglePasswordVisibility'), 4)

    def test_unknown_mobile_is_generic_and_known_mobile_can_reset_once(self):
        unknown = self.client.post('/api/auth/forgot-password', json={'mobile': '9876599999'})
        self.assertEqual(unknown.status_code, 200)
        self.assertEqual(unknown.get_json()['message'], 'If the account exists, a verification code has been sent.')
        self.assertEqual(self.client.post('/api/auth/forgot-password/verify', json={
            'mobile': '9876599999', 'challenge_token': unknown.get_json()['challenge_token'], 'otp_code': '123456',
        }).status_code, 400)

        sent = self.client.post('/api/auth/forgot-password', json={'mobile': '9876512345'})
        self.assertEqual(sent.status_code, 200)
        self.assertNotIn('User does not exist', sent.get_json()['message'])
        otp = sent.get_json()['otp_code']
        self.assertTrue(sent.get_json()['challenge_token'])
        verified = self.client.post('/api/auth/forgot-password/verify', json={
            'mobile': '9876512345', 'challenge_token': sent.get_json()['challenge_token'], 'otp_code': otp,
        })
        self.assertEqual(verified.status_code, 200)
        token = verified.get_json()['reset_token']
        self.assertTrue(token)

        reset = self.client.post('/api/auth/reset-password', json={
            'reset_token': token,
            'new_password': 'new-secure-password',
            'confirm_password': 'new-secure-password',
        })
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(self.client.post('/api/auth/reset-password', json={
            'reset_token': token,
            'new_password': 'another-password',
            'confirm_password': 'another-password',
        }).status_code, 400)

        login = self.client.post('/api/auth/login', json={
            'identifier': '9876512345', 'password': 'new-secure-password',
        })
        self.assertEqual(login.status_code, 200)

    def test_reset_rejects_mismatch_and_weak_password(self):
        sent = self.client.post('/api/auth/forgot-password', json={'mobile': '9876523456'})
        token = self.client.post('/api/auth/forgot-password/verify', json={
            'mobile': '9876523456', 'challenge_token': sent.get_json()['challenge_token'], 'otp_code': sent.get_json()['otp_code'],
        }).get_json()['reset_token']
        mismatch = self.client.post('/api/auth/reset-password', json={
            'reset_token': token, 'new_password': 'short', 'confirm_password': 'different',
        })
        self.assertEqual(mismatch.status_code, 400)
        weak = self.client.post('/api/auth/reset-password', json={
            'reset_token': token, 'new_password': 'short', 'confirm_password': 'short',
        })
        self.assertEqual(weak.status_code, 400)

    def test_registered_mobile_with_disabled_sms_fails_without_server_error(self):
        global app_module
        old_environment = os.environ.get('ENVIRONMENT')
        old_secret = os.environ.get('SECRET_KEY')
        old_sms_provider = os.environ.get('SMS_PROVIDER')
        old_upload_folder = os.environ.get('TASKPE_UPLOAD_FOLDER')
        old_task_asset_folder = os.environ.get('TASKPE_TASK_ASSET_FOLDER')
        production_proofs = os.path.join(ROOT, 'production-proofs')
        production_assets = os.path.join(ROOT, 'production-assets')
        try:
            os.environ['ENVIRONMENT'] = 'production'
            os.environ['SECRET_KEY'] = 'password-reset-production-test-secret'
            os.environ['SMS_PROVIDER'] = 'disabled'
            os.environ['TASKPE_UPLOAD_FOLDER'] = production_proofs
            os.environ['TASKPE_TASK_ASSET_FOLDER'] = production_assets
            database.DB_PATH = os.environ['TASKPE_DB_PATH']
            conn = database.get_db_connection()
            conn.execute('''
                INSERT OR IGNORE INTO users
                    (name, mobile, email, password_hash, role, referral_code)
                VALUES (?, ?, ?, ?, 'user', ?)
            ''', ('Registered Reset User', '9876500001', 'reset-disabled@example.test',
                  'test-hash', 'RESETDISABLED'))
            conn.commit()
            conn.close()
            app_module = importlib.reload(app_module)
            app_module.app.config['TESTING'] = True
            response = app_module.app.test_client().post(
                '/api/auth/forgot-password', json={'mobile': '9876500001'}
            )
            self.assertEqual(response.status_code, 503)
            payload = response.get_json()
            self.assertFalse(payload['success'])
            self.assertNotIn('otp_code', payload)
            self.assertNotIn('challenge_token', payload)
        finally:
            for name, value in {
                'ENVIRONMENT': old_environment,
                'SECRET_KEY': old_secret,
                'SMS_PROVIDER': old_sms_provider,
                'TASKPE_UPLOAD_FOLDER': old_upload_folder,
                'TASKPE_TASK_ASSET_FOLDER': old_task_asset_folder,
            }.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            database.DB_PATH = os.environ['TASKPE_DB_PATH']
            app_module = importlib.reload(app_module)
            app_module.app.config['TESTING'] = True


if __name__ == '__main__':
    unittest.main()
