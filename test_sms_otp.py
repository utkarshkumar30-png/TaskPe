import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

_ROOT = tempfile.mkdtemp(prefix='taskpe-sms-tests-')
os.environ.setdefault('TASKPE_DB_PATH', os.path.join(_ROOT, 'taskpe.db'))
os.environ.setdefault('ENVIRONMENT', 'development')
os.environ.setdefault('SECRET_KEY', 'test-only-development-secret')

import database
import app as app_module


class Fast2SmsOtpTests(unittest.TestCase):
    def setUp(self):
        self.db_path = os.path.join(_ROOT, f'{self.id().rsplit(".", 1)[-1]}.db')
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        database.DB_PATH = self.db_path
        os.environ['TASKPE_DB_PATH'] = self.db_path
        os.environ['ENVIRONMENT'] = 'development'
        os.environ['SECRET_KEY'] = 'test-only-development-secret'
        os.environ['SMS_PROVIDER'] = 'disabled'
        os.environ.pop('FAST2SMS_API_KEY', None)
        self._reload_app()

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def _reload_app(self):
        global app_module
        app_module = importlib.reload(app_module)
        app_module.app.config['TESTING'] = True
        self.client = app_module.app.test_client()

    def _otp_count(self, mobile):
        conn = database.get_db_connection()
        count = conn.execute('SELECT COUNT(*) FROM otps WHERE mobile=?', (mobile,)).fetchone()[0]
        conn.close()
        return count

    def test_development_returns_stored_otp_and_rate_limits_resends(self):
        first = self.client.post('/api/auth/send-otp', json={'mobile': '9876543210'})
        self.assertEqual(first.status_code, 200)
        otp = first.get_json()['otp_code']
        self.assertRegex(otp, r'^\d{6}$')
        self.assertEqual(self._otp_count('9876543210'), 1)
        self.assertEqual(self.client.post('/api/auth/send-otp', json={'mobile': '9876543210'}).status_code, 429)

    def test_enabled_fast2sms_sends_through_mock_and_production_hides_otp(self):
        os.environ['ENVIRONMENT'] = 'production'
        os.environ['SECRET_KEY'] = 'production-test-secret'
        os.environ['SMS_PROVIDER'] = 'fast2sms'
        os.environ['FAST2SMS_API_KEY'] = 'test-api-key'
        self._reload_app()
        response = MagicMock()
        response.read.return_value = json.dumps({'return': True, 'request_id': 'test-request', 'message': ['Message sent successfully']}).encode()
        response.status = 200
        response.__enter__.return_value = response
        with patch.object(app_module.urllib.request, 'urlopen', return_value=response) as send:
            result = self.client.post('/api/auth/send-otp', json={'mobile': '9876543211'})
        self.assertEqual(result.status_code, 200)
        self.assertNotIn('otp_code', result.get_json())
        self.assertEqual(send.call_count, 1)
        request_obj = send.call_args.args[0]
        self.assertEqual(request_obj.full_url, 'https://www.fast2sms.com/dev/bulkV2')
        self.assertEqual(request_obj.get_header('Authorization'), 'test-api-key')
        self.assertEqual(request_obj.get_header('Content-type'), 'application/json')
        body = json.loads(request_obj.data.decode())
        self.assertEqual(body['route'], 'otp')
        self.assertEqual(body['numbers'], '9876543211')
        self.assertRegex(body['variables_values'], r'^\d{6}$')
        self.assertNotIn('otp_id', body)
        self.assertNotIn('template_id', body)
        self.assertNotIn('otp_expiry', body)
        self.assertNotIn('otp_length', body)
        self.assertNotIn('otp', body)
        self.assertEqual(self._otp_count('9876543211'), 1)

    def test_fast2sms_failure_or_missing_configuration_does_not_store_otp(self):
        os.environ['SMS_PROVIDER'] = 'fast2sms'
        self._reload_app()
        missing = self.client.post('/api/auth/send-otp', json={'mobile': '9876543212'})
        self.assertEqual(missing.status_code, 502)
        self.assertEqual(self._otp_count('9876543212'), 0)

        os.environ['FAST2SMS_API_KEY'] = 'test-api-key'
        self._reload_app()
        with patch.object(app_module.urllib.request, 'urlopen', side_effect=app_module.urllib.error.URLError('offline')):
            failed = self.client.post('/api/auth/send-otp', json={'mobile': '9876543213'})
        self.assertEqual(failed.status_code, 502)
        self.assertEqual(self._otp_count('9876543213'), 0)

    def test_production_fast2sms_requires_complete_configuration(self):
        env = os.environ.copy()
        env['ENVIRONMENT'] = 'production'
        env['SECRET_KEY'] = 'production-test-secret'
        env['SMS_PROVIDER'] = 'fast2sms'
        env.pop('FAST2SMS_API_KEY', None)
        env.pop('FAST2SMS_API_KEY', None)
        env['TASKPE_DB_PATH'] = os.path.join(_ROOT, 'missing-fast2sms-config.db')
        result = subprocess.run(
            [sys.executable, '-c', 'import app'], cwd=os.path.dirname(__file__),
            env=env, capture_output=True, text=True
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('FAST2SMS_API_KEY is required', result.stderr)

    def test_server_side_verification_remains_single_use_and_mobile_bound(self):
        sent = self.client.post('/api/auth/send-otp', json={'mobile': '9876543214'}).get_json()
        self.assertEqual(self.client.post('/api/auth/verify-otp', json={'mobile': '9876543215', 'otp_code': sent['otp_code']}).status_code, 400)
        self.assertEqual(self.client.post('/api/auth/verify-otp', json={'mobile': '9876543214', 'otp_code': '000000'}).status_code, 400)
        self.assertEqual(self.client.post('/api/auth/verify-otp', json={'mobile': '9876543214', 'otp_code': sent['otp_code']}).status_code, 200)
        self.assertEqual(self.client.post('/api/auth/verify-otp', json={'mobile': '9876543214', 'otp_code': sent['otp_code']}).status_code, 400)
        conn = database.get_db_connection()
        verified = conn.execute('SELECT mobile FROM mobile_verifications WHERE mobile=?', ('9876543214',)).fetchone()
        self.assertEqual(verified['mobile'], '9876543214')
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM otps WHERE mobile=?', ('9876543214',)).fetchone()[0], 0)
        conn.close()

    def test_new_otp_is_not_stored_in_plaintext_and_guessing_is_limited(self):
        sent = self.client.post('/api/auth/send-otp', json={'mobile': '9876543216'}).get_json()
        otp = sent['otp_code']
        conn = database.get_db_connection()
        row = conn.execute('SELECT otp_code, otp_hash, attempt_count FROM otps WHERE mobile=?', ('9876543216',)).fetchone()
        conn.close()
        self.assertNotEqual(row['otp_code'], otp)
        self.assertTrue(row['otp_hash'])

        for _ in range(app_module.OTP_MAX_VERIFICATION_ATTEMPTS):
            self.assertEqual(self.client.post('/api/auth/verify-otp', json={
                'mobile': '9876543216', 'otp_code': '000000'
            }).status_code, 400)
        self.assertEqual(self.client.post('/api/auth/verify-otp', json={
            'mobile': '9876543216', 'otp_code': otp
        }).status_code, 400)

    def test_signup_cannot_bypass_mobile_verification(self):
        response = self.client.post('/api/auth/register', json={
            'name': 'Unverified', 'mobile': '9876543217',
            'email': 'unverified@example.com', 'password': 'strong-password',
            'referral_code': ''
        })
        self.assertEqual(response.status_code, 400)
        conn = database.get_db_connection()
        self.assertIsNone(conn.execute('SELECT id FROM users WHERE mobile=?', ('9876543217',)).fetchone())
        conn.close()

    def test_expired_otp_is_rejected(self):
        conn = database.get_db_connection()
        conn.execute("INSERT INTO otps (mobile, otp_code, expires_at) VALUES (?, ?, datetime('now', '-1 minute'))", ('9876543218', '123456'))
        conn.commit()
        conn.close()
        self.assertEqual(self.client.post('/api/auth/verify-otp', json={
            'mobile': '9876543218', 'otp_code': '123456'
        }).status_code, 400)


if __name__ == '__main__':
    unittest.main()
