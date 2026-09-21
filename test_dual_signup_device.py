import importlib
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock


ROOT = tempfile.mkdtemp(prefix='taskpe-dual-signup-')
os.environ['TASKPE_DB_PATH'] = os.path.join(ROOT, 'taskpe.db')
os.environ['ENVIRONMENT'] = 'development'
os.environ['SECRET_KEY'] = 'dual-signup-test-secret'
os.environ['SMS_PROVIDER'] = 'disabled'
os.environ['EMAIL_PROVIDER'] = 'disabled'

import database
import app as app_module

database.DB_PATH = os.environ['TASKPE_DB_PATH']
app_module = importlib.reload(app_module)
app_module.app.config['TESTING'] = True


class DualSignupDeviceTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(ROOT, ignore_errors=True)

    def setUp(self):
        self.client = app_module.app.test_client()

    def create_email(self, index):
        email = f'device-email-{index}@example.test'
        token = self.client.post('/api/auth/send-email-verification', json={'email': email}).get_json()['verification_token']
        self.assertEqual(self.client.post('/api/auth/verify-email', json={
            'email': email, 'verification_token': token,
        }).status_code, 200)
        return self.client.post('/api/auth/register-email', json={
            'name': f'Email User {index}', 'email': email,
            'password': 'secure-password', 'referral_code': '',
            'verification_token': token,
        })

    def test_email_signup_verification_is_hashed_and_single_use(self):
        email = 'verified@example.test'
        sent = self.client.post('/api/auth/send-email-verification', json={'email': email}).get_json()
        token = sent['verification_token']
        self.assertEqual(self.client.post('/api/auth/verify-email', json={
            'email': email, 'verification_token': 'wrong-token',
        }).status_code, 400)
        created = self.client.post('/api/auth/register-email', json={
            'name': 'Verified Email', 'email': email,
            'password': 'secure-password', 'referral_code': '',
            'verification_token': token,
        })
        self.assertEqual(created.status_code, 200)
        self.assertEqual(self.client.post('/api/auth/register-email', json={
            'name': 'Replay', 'email': 'replay@example.test',
            'password': 'secure-password', 'referral_code': '',
            'verification_token': token,
        }).status_code, 400)
        conn = database.get_db_connection()
        row = conn.execute('SELECT token_hash FROM email_verifications WHERE email = ?', (email,)).fetchone()
        self.assertNotEqual(row['token_hash'], token)
        conn.close()

    def test_disabled_email_provider_is_safe(self):
        self.assertIsInstance(app_module.email_provider, app_module.DisabledEmailProvider)
        self.assertTrue(app_module.send_email_verification('disabled@example.test', 'test-token'))

    def test_brevo_email_provider_invokes_https_transport(self):
        provider = app_module.BrevoEmailProvider()
        response = MagicMock(status=201)
        response.getcode.return_value = 201
        response.read.return_value = b'{"messageId":"brevo-message-id"}'
        response.__enter__.return_value = response
        with patch.object(app_module, 'BREVO_API_KEY', 'test-brevo-key'), \
             patch.object(app_module, 'EMAIL_FROM', 'support@example.test'), \
             patch.object(app_module, 'EMAIL_FROM_NAME', 'TaskPe'), \
             patch.object(app_module.urllib.request, 'urlopen', return_value=response) as urlopen:
            self.assertTrue(provider.send_verification('recipient@example.test', 'token-value'))
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, app_module.BREVO_API_URL)
        self.assertEqual(request.get_header('Api-key'), 'test-brevo-key')
        body = json.loads(request.data.decode('utf-8'))
        self.assertEqual(body['sender']['email'], 'support@example.test')
        self.assertEqual(body['sender']['name'], 'TaskPe')
        self.assertEqual(body['to'][0]['email'], 'recipient@example.test')
        self.assertIn('token-value', body['textContent'])

    def test_brevo_provider_failure_is_reported_without_exposing_credentials(self):
        provider = app_module.BrevoEmailProvider()
        with patch.object(app_module.urllib.request, 'urlopen', side_effect=app_module.urllib.error.URLError('transport failed')):
            self.assertFalse(provider.send_verification('recipient@example.test', 'secret-token'))

    def test_brevo_provider_requires_message_id(self):
        provider = app_module.BrevoEmailProvider()
        response = MagicMock(status=201)
        response.getcode.return_value = 201
        response.read.return_value = b'{"messageId":""}'
        response.__enter__.return_value = response
        with patch.object(app_module.urllib.request, 'urlopen', return_value=response):
            self.assertFalse(provider.send_verification('recipient@example.test', 'token-value'))

    def test_email_provider_failure_does_not_create_account(self):
        email = 'provider-failure@example.test'
        with patch.object(app_module.email_provider, 'send_verification', return_value=False):
            response = self.client.post('/api/auth/send-email-verification', json={'email': email})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('verification_token', response.get_json())
        conn = database.get_db_connection()
        self.assertIsNone(conn.execute('SELECT id FROM users WHERE email = ?', (email,)).fetchone())
        conn.close()

    def test_email_device_limit_is_two(self):
        self.assertEqual(self.create_email(1).status_code, 200)
        self.assertEqual(self.create_email(2).status_code, 200)
        blocked = self.create_email(3)
        self.assertEqual(blocked.status_code, 429)
        self.assertIn('maximum number of email accounts', blocked.get_json()['message'])

    def test_mobile_device_limit_is_four(self):
        for index in range(4):
            mobile = f'9876543{index:03d}'
            sent = self.client.post('/api/auth/send-otp', json={'mobile': mobile}).get_json()
            response = self.client.post('/api/auth/verify-otp', json={'mobile': mobile, 'otp_code': sent['otp_code']})
            self.assertEqual(response.status_code, 200)
            created = self.client.post('/api/auth/register', json={
                'name': f'Mobile User {index}', 'mobile': mobile,
                'email': f'mobile-{index}@example.test', 'password': 'secure-password',
                'referral_code': '',
            })
            self.assertEqual(created.status_code, 200)
        mobile = '9876543004'
        sent = self.client.post('/api/auth/send-otp', json={'mobile': mobile}).get_json()
        self.assertEqual(self.client.post('/api/auth/verify-otp', json={
            'mobile': mobile, 'otp_code': sent['otp_code'],
        }).status_code, 200)
        blocked = self.client.post('/api/auth/register', json={
            'name': 'Blocked Mobile', 'mobile': mobile,
            'email': 'blocked-mobile@example.test', 'password': 'secure-password',
            'referral_code': '',
        })
        self.assertEqual(blocked.status_code, 429)
        self.assertIn('maximum number of mobile accounts', blocked.get_json()['message'])


if __name__ == '__main__':
    unittest.main()
