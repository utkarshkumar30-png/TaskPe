import io
import os
import subprocess
import sys
import tempfile
import unittest

_root = tempfile.mkdtemp(prefix='taskpe-security-controls-')
os.environ.setdefault('TASKPE_DB_PATH', os.path.join(_root, 'taskpe.db'))
os.environ.setdefault('ENVIRONMENT', 'development')
os.environ.setdefault('SECRET_KEY', 'security-controls-test-secret')

import app as app_module


class SecurityControlTests(unittest.TestCase):
    def setUp(self):
        app_module.app.config['TESTING'] = True
        self.client = app_module.app.test_client()

    def test_browser_mutations_require_csrf_but_get_remains_available(self):
        response = self.client.post('/api/auth/logout', headers={'Origin': 'http://localhost'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get('/api/auth/csrf').status_code, 200)

    def test_csrf_token_allows_browser_mutation(self):
        token = self.client.get('/api/auth/csrf').get_json()['csrf_token']
        response = self.client.post('/api/auth/logout', headers={
            'Origin': 'http://localhost', 'X-CSRF-Token': token
        })
        self.assertEqual(response.status_code, 200)

    def test_login_bruteforce_is_throttled(self):
        identifier = 'rate-limit-' + next(tempfile._get_candidate_names()) + '@example.test'
        responses = [self.client.post('/api/auth/login', json={
            'identifier': identifier, 'password': 'wrong'
        }) for _ in range(11)]
        self.assertEqual(responses[-1].status_code, 429)

    def test_image_signature_validation_and_manual_payout_mode(self):
        png = app_module.valid_image_content(type('Upload', (), {
            'stream': io.BytesIO(b'\x89PNG\r\n\x1a\nvalid')
        })())
        html = app_module.valid_image_content(type('Upload', (), {
            'stream': io.BytesIO(b'<script>alert(1)</script>')
        })())
        self.assertTrue(png)
        self.assertFalse(html)
        self.assertEqual(app_module.PAYOUT_MODE, 'MANUAL')

    def test_unsupported_payout_mode_fails_startup(self):
        env = os.environ.copy()
        env['PAYOUT_MODE'] = 'AUTO'
        env['ENVIRONMENT'] = 'development'
        env['SECRET_KEY'] = 'security-controls-subprocess-secret'
        result = subprocess.run([sys.executable, '-c', 'import app'], env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Unsupported PAYOUT_MODE', result.stderr + result.stdout)


if __name__ == '__main__':
    unittest.main()
