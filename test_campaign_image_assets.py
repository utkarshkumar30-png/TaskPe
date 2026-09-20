import io
import importlib
import json
import os
import tempfile
import unittest

ROOT = tempfile.mkdtemp(prefix='taskpe-campaign-assets-')
os.environ['TASKPE_DB_PATH'] = os.path.join(ROOT, 'assets.db')
os.environ['ENVIRONMENT'] = 'development'
os.environ['SECRET_KEY'] = 'campaign-assets-test-secret'
os.environ['SMS_PROVIDER'] = 'disabled'

import app as app_module
import database


class CampaignImageAssetTests(unittest.TestCase):
    PNG = b'\x89PNG\r\n\x1a\n' + b'campaign-test-image'

    def setUp(self):
        global app_module
        os.environ['ENVIRONMENT'] = 'development'
        os.environ['SECRET_KEY'] = 'campaign-assets-test-secret'
        database.DB_PATH = os.path.join(ROOT, 'assets.db')
        if os.path.exists(database.DB_PATH):
            os.remove(database.DB_PATH)
        database.init_db()
        app_module = importlib.reload(app_module)
        self.asset_folder = tempfile.mkdtemp(prefix='task-assets-')
        self.proof_folder = tempfile.mkdtemp(prefix='proof-assets-')
        app_module.app.config['TASK_ASSET_FOLDER'] = self.asset_folder
        app_module.app.config['UPLOAD_FOLDER'] = self.proof_folder
        conn = database.get_db_connection()
        self.worker_id = conn.execute('''
            INSERT INTO users (name, mobile, email, password_hash, referral_code, role)
            VALUES ('Worker', '919800000001', 'asset-worker@example.com', 'hash', 'ASSETW', 'user')
        ''').lastrowid
        self.admin_id = conn.execute('''
            INSERT INTO users (name, mobile, email, password_hash, referral_code, role)
            VALUES ('Admin', '919800000002', 'asset-admin@example.com', 'hash', 'ASSETADMIN', 'admin')
        ''').lastrowid
        conn.commit()
        conn.close()

    def login(self, user_id):
        client = app_module.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = user_id
        return client

    def test_image_task_creation_saves_to_task_asset_folder(self):
        client = self.login(self.admin_id)
        response = client.post('/api/admin/tasks/create', data={
            'title': 'Image campaign',
            'category': 'IMAGE_TEXT_LINK',
            'task_type': 'IMAGE_TEXT_LINK',
            'description': 'Send the image',
            'template_text': 'Please see this offer',
            'target_numbers': '919700000001',
            'recipients_per_slot': '1',
            'total_slots': '1',
            'time_limit_minutes': '15',
            'proof_instructions': 'Upload proof',
            'use_default_reward': '1',
            'destination_link': 'https://example.com',
            'campaign_image': (io.BytesIO(self.PNG), 'campaign.png'),
        }, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 200, response.get_json())
        conn = database.get_db_connection()
        image_path = conn.execute('SELECT image_path FROM tasks WHERE id = ?', (response.get_json()['task_id'],)).fetchone()[0]
        conn.close()
        self.assertTrue(image_path.startswith('campaign_'))
        self.assertTrue(os.path.isfile(os.path.join(self.asset_folder, image_path)))
        self.assertFalse(os.path.exists(os.path.join(self.proof_folder, image_path)))

    def _create_task(self, status='active'):
        filename = 'campaign_test.png'
        with open(os.path.join(self.asset_folder, filename), 'wb') as image:
            image.write(self.PNG)
        conn = database.get_db_connection()
        task_id = conn.execute('''
            INSERT INTO tasks (title, category, task_type, description, payout, template_text,
                target_numbers, campaign_id, recipients_per_slot, time_limit_minutes,
                proof_instructions, total_slots, status, image_path, destination_link)
            VALUES ('Image campaign', 'IMAGE_TEXT_LINK', 'IMAGE_TEXT_LINK', 'd', 3, 'Message', ?, ?, 1, 15, 'p', 1, ?, ?, 'https://example.com')
        ''', (json.dumps(['919700000001']), 7000, status, filename)).lastrowid
        conn.commit()
        conn.close()
        return task_id, filename

    def test_protected_route_serves_asset_and_task_api_returns_url(self):
        task_id, filename = self._create_task()
        client = self.login(self.worker_id)
        self.assertEqual(client.post(f'/api/tasks/{task_id}/take').status_code, 200)
        detail = client.get(f'/api/tasks/{task_id}')
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.get_json()['task']['image_url'], f'/uploads/task-assets/{filename}')
        served = client.get(f'/uploads/task-assets/{filename}')
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.data, self.PNG)

    def test_image_task_detail_preserves_worker_workflow_fields(self):
        task_id, filename = self._create_task()
        client = self.login(self.worker_id)
        self.assertEqual(client.post(f'/api/tasks/{task_id}/take').status_code, 200)

        response = client.get(f'/api/tasks/{task_id}')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        task = payload['task']
        self.assertEqual(task['task_type'], 'IMAGE_TEXT_LINK')
        self.assertEqual(task['template_text'], 'Message')
        self.assertEqual(task['destination_link'], 'https://example.com')
        self.assertEqual(task['image_url'], f'/uploads/task-assets/{filename}')
        self.assertEqual(task['target_numbers'], ['919700000001'])

    def test_asset_route_requires_auth_and_rejects_invalid_filename(self):
        task_id, filename = self._create_task()
        anonymous = app_module.app.test_client()
        self.assertEqual(anonymous.get(f'/uploads/task-assets/{filename}').status_code, 401)
        client = self.login(self.worker_id)
        self.assertEqual(client.get('/uploads/task-assets/not_campaign.png').status_code, 404)
        self.assertEqual(client.get('/uploads/task-assets/campaign_../secret').status_code, 404)

    def test_inactive_task_hidden_from_workers_but_available_to_admin(self):
        task_id, filename = self._create_task(status='archived')
        worker = self.login(self.worker_id)
        self.assertEqual(worker.get(f'/uploads/task-assets/{filename}').status_code, 404)
        admin = self.login(self.admin_id)
        self.assertEqual(admin.get(f'/uploads/task-assets/{filename}').status_code, 200)

    def test_proof_route_still_uses_proof_folder(self):
        source = open(os.path.join(os.path.dirname(__file__), 'app.py'), encoding='utf-8').read()
        self.assertIn("return send_from_directory(app.config['UPLOAD_FOLDER'], filename)", source)
        self.assertIn("return send_from_directory(app.config['TASK_ASSET_FOLDER'], filename)", source)

    def test_image_detail_has_protected_download_control(self):
        template = open(os.path.join(os.path.dirname(__file__), 'templates', 'index.html'), encoding='utf-8').read()
        script = open(os.path.join(os.path.dirname(__file__), 'static', 'js', 'app.js'), encoding='utf-8').read()
        self.assertIn('id="detail-image-download"', template)
        self.assertIn('download="taskpe-campaign-image"', template)
        self.assertIn("imageDownload.href = task.image_url", script)
        self.assertIn('Download Image', template)


if __name__ == '__main__':
    unittest.main()
