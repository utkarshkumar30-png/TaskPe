import io
import io
import json
import os
import sqlite3
import tempfile
import unittest

os.environ.setdefault('ENVIRONMENT', 'production')
os.environ.setdefault('SECRET_KEY', 'local-business-campaign-test-secret')
_test_dir = tempfile.mkdtemp(prefix='taskpe-business-campaign-', dir=os.getcwd())
os.environ['TASKPE_DB_PATH'] = os.path.join(_test_dir, 'test.db')
os.environ['TASKPE_UPLOAD_FOLDER'] = os.path.join(_test_dir, 'uploads')
os.environ['TASKPE_TASK_ASSET_FOLDER'] = os.path.join(_test_dir, 'assets')
os.environ['TASKPE_WHATSAPP_LINKED_DEVICE_ENABLED'] = '0'

import database
database.init_db()
import app as app_module
from werkzeug.security import generate_password_hash


class BusinessCampaignCreationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        conn = database.get_db_connection()
        cursor = conn.execute(
            "INSERT INTO users (name, email, password_hash, mobile, referral_code, role) VALUES (?, ?, ?, ?, ?, ?)",
            ('Business Owner', 'business-owner@example.com', generate_password_hash('unused'), '14155550123', 'BUSINESS001', 'business'),
        )
        cls.owner_id = cursor.lastrowid
        cursor = conn.execute(
            "INSERT INTO users (name, email, password_hash, mobile, referral_code, role) VALUES (?, ?, ?, ?, ?, ?)",
            ('Ordinary Worker', 'business-worker@example.com', generate_password_hash('unused'), '14155550124', 'WORKER001', 'user'),
        )
        cls.worker_id = cursor.lastrowid
        cursor = conn.execute(
            "INSERT INTO users (name, email, password_hash, mobile, referral_code, role) VALUES (?, ?, ?, ?, ?, ?)",
            ('Administrator', 'business-admin@example.com', generate_password_hash('unused'), '14155550125', 'ADMIN001', 'admin'),
        )
        cls.admin_id = cursor.lastrowid
        cls.client = app_module.app.test_client()
        conn.commit()
        conn.close()

    def setUp(self):
        conn = database.get_db_connection()
        conn.execute('DELETE FROM whatsapp_campaign_consent_declarations WHERE business_user_id = ?', (self.owner_id,))
        conn.execute('DELETE FROM tasks WHERE owner_user_id = ?', (self.owner_id,))
        conn.commit()
        conn.close()
        with self.client.session_transaction() as session:
            session['user_id'] = self.owner_id
            session['_csrf_token'] = 'business-csrf'

    def tearDown(self):
        with self.client.session_transaction() as session:
            session.clear()

    def test_preview_normalizes_counts_and_deduplicates_without_persisting(self):
        response = self.client.post(
            '/api/business/campaigns/recipients/preview',
            json={'target_numbers': '+14155552671\n4155552671\nnot-a-number', 'recipient_region': 'US'},
            headers={'X-CSRF-Token': 'business-csrf'},
        )
        self.assertEqual(response.status_code, 200)
        preview = response.get_json()['preview']
        self.assertEqual(preview['valid_count'], 1)
        self.assertEqual(preview['duplicate_count'], 1)
        self.assertEqual(preview['invalid_count'], 1)
        self.assertEqual(preview['valid_numbers'], ['+14155552671'])
        conn = database.get_db_connection()
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM tasks WHERE owner_user_id = ?', (self.owner_id,)).fetchone()[0], 0)
        finally:
            conn.close()

    def test_creation_binds_owner_and_exact_consent_snapshot(self):
        response = self.client.post(
            '/api/business/campaigns',
            data={
                'title': 'Owner campaign',
                'task_type': 'TEXT_LINK',
                'template_text': 'Hello from the business',
                'description': 'A prepared business campaign',
                'destination_link': 'https://example.com/offers',
                'recipient_region': 'US',
                'target_numbers': '+14155552671\n+442079460958',
                'consent_declaration_confirmed': '1',
            },
            headers={'X-CSRF-Token': 'business-csrf'},
        )
        self.assertEqual(response.status_code, 201)
        task_id = response.get_json()['task_id']
        conn = database.get_db_connection()
        try:
            task = conn.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
            declaration = conn.execute('SELECT * FROM whatsapp_campaign_consent_declarations WHERE task_id = ?', (task_id,)).fetchone()
            self.assertEqual(task['owner_user_id'], self.owner_id)
            self.assertEqual(task['status'], 'pending_review')
            self.assertEqual(task['campaign_review_status'], 'pending_review')
            self.assertEqual(json.loads(task['target_numbers']), ['+14155552671', '+442079460958'])
            self.assertEqual(declaration['business_user_id'], self.owner_id)
            self.assertEqual(json.loads(declaration['normalized_recipient_list']), json.loads(task['target_numbers']))
            self.assertEqual(declaration['declaration_text'], database.SHOP_CONSENT_DECLARATION_DEFAULT_TEXT)
        finally:
            conn.close()

    def _create_campaign(self, title='Approval campaign'):
        response = self.client.post('/api/business/campaigns', data={
            'title': title, 'task_type': 'TEXT_LINK', 'template_text': 'Hello',
            'description': 'Description', 'destination_link': 'https://example.com',
            'recipient_region': 'US', 'target_numbers': '+14155552671',
            'consent_declaration_confirmed': '1',
        }, headers={'X-CSRF-Token': 'business-csrf'})
        self.assertEqual(response.status_code, 201)
        return response.get_json()['task_id']

    def test_only_admin_can_approve_and_approval_is_idempotent(self):
        task_id = self._create_campaign()
        self.assertEqual(self.client.post(
            f'/api/admin/business-campaigns/{task_id}/approve',
            headers={'X-CSRF-Token': 'business-csrf'},
        ).status_code, 403)
        with self.client.session_transaction() as session:
            session['user_id'] = self.admin_id
            session['_csrf_token'] = 'admin-csrf'
        approved = self.client.post(
            f'/api/admin/business-campaigns/{task_id}/approve',
            headers={'X-CSRF-Token': 'admin-csrf'},
        )
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.get_json()['status'], 'approved')
        repeated = self.client.post(
            f'/api/admin/business-campaigns/{task_id}/approve',
            headers={'X-CSRF-Token': 'admin-csrf'},
        )
        self.assertEqual(repeated.status_code, 200)
        self.assertTrue(repeated.get_json()['idempotent'])

    def test_recipient_change_invalidates_approval_snapshot(self):
        task_id = self._create_campaign('Mutable recipients')
        with self.client.session_transaction() as session:
            session['user_id'] = self.admin_id
            session['_csrf_token'] = 'admin-csrf'
        self.assertEqual(self.client.post(
            f'/api/admin/business-campaigns/{task_id}/approve',
            headers={'X-CSRF-Token': 'admin-csrf'},
        ).status_code, 200)
        conn = database.get_db_connection()
        conn.execute('UPDATE tasks SET target_numbers = ? WHERE id = ?', ('["+14155552672"]', task_id))
        conn.commit()
        task = conn.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
        self.assertFalse(app_module.business_campaign_is_approved(conn, task))
        conn.close()

    def test_invalid_numbers_missing_region_and_consent_are_rejected(self):
        base = {
            'title': 'Invalid campaign', 'task_type': 'TEXT_LINK', 'template_text': 'Hello',
            'description': 'Description', 'destination_link': 'https://example.com',
            'target_numbers': '4155550123',
        }
        missing_region = self.client.post('/api/business/campaigns', data=base, headers={'X-CSRF-Token': 'business-csrf'})
        self.assertEqual(missing_region.status_code, 400)
        base['recipient_region'] = 'US'
        missing_consent = self.client.post('/api/business/campaigns', data=base, headers={'X-CSRF-Token': 'business-csrf'})
        self.assertEqual(missing_consent.status_code, 400)
        conn = database.get_db_connection()
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks WHERE title = 'Invalid campaign'").fetchone()[0], 0)
        finally:
            conn.close()

    def test_image_campaign_requires_supported_asset_and_https(self):
        response = self.client.post(
            '/api/business/campaigns',
            data={
                'title': 'Image campaign', 'task_type': 'IMAGE_TEXT_LINK', 'template_text': 'See this',
                'description': 'Image campaign', 'destination_link': 'http://example.com',
                'target_numbers': '+14155552671', 'consent_declaration_confirmed': '1',
                'campaign_image': (io.BytesIO(b'not-an-image'), 'offer.png'),
            },
            headers={'X-CSRF-Token': 'business-csrf'},
            content_type='multipart/form-data',
        )
        self.assertEqual(response.status_code, 400)

    def test_creation_requires_authentication_and_csrf(self):
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.client.post('/api/business/campaigns', data={}).status_code, 401)
        self.setUp()
        self.assertEqual(self.client.post('/api/business/campaigns', data={}).status_code, 403)

    def test_business_authorization_rejects_worker_and_ignores_spoofed_owner(self):
        with self.client.session_transaction() as session:
            session['user_id'] = self.worker_id
            session['_csrf_token'] = 'business-csrf'
        response = self.client.post(
            '/api/business/campaigns/recipients/preview',
            json={'target_numbers': '+14155552671'},
            headers={'X-CSRF-Token': 'business-csrf'},
        )
        self.assertEqual(response.status_code, 403)
        response = self.client.post(
            '/api/business/campaigns',
            data={
                'owner_user_id': self.owner_id,
                'title': 'Spoofed owner campaign',
                'task_type': 'TEXT_LINK',
                'template_text': 'Hello', 'description': 'Description',
                'destination_link': 'https://example.com',
                'recipient_region': 'US', 'target_numbers': '+14155552671',
                'consent_declaration_confirmed': '1',
            },
            headers={'X-CSRF-Token': 'business-csrf'},
        )
        self.assertEqual(response.status_code, 403)

    def test_admin_can_use_business_campaign_boundary(self):
        with self.client.session_transaction() as session:
            session['user_id'] = self.admin_id
            session['_csrf_token'] = 'admin-csrf'
        response = self.client.post(
            '/api/business/campaigns/recipients/preview',
            json={'target_numbers': '+14155552671'},
            headers={'X-CSRF-Token': 'admin-csrf'},
        )
        self.assertEqual(response.status_code, 200)


class ExistingDatabaseMigrationTests(unittest.TestCase):
    def test_owner_column_migrates_existing_tasks_idempotently(self):
        path = os.path.join(_test_dir, 'pre-migration.db')
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute('''
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'Direct Message',
                task_type TEXT NOT NULL DEFAULT 'TEXT_LINK',
                description TEXT NOT NULL, payout REAL NOT NULL, payout_paise INTEGER,
                reward_per_number_paise INTEGER, reward_source TEXT NOT NULL DEFAULT 'legacy_snapshot',
                template_text TEXT, image_path TEXT, destination_link TEXT,
                target_numbers TEXT NOT NULL, recipient_region TEXT,
                campaign_id INTEGER, recipients_per_slot INTEGER NOT NULL DEFAULT 0,
                time_limit_minutes INTEGER NOT NULL DEFAULT 15,
                proof_instructions TEXT NOT NULL DEFAULT '',
                total_slots INTEGER NOT NULL DEFAULT 100,
                completed_slots INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.execute('''
            INSERT INTO tasks
                (title, category, task_type, description, payout, target_numbers,
                 recipients_per_slot, time_limit_minutes, proof_instructions,
                 total_slots, completed_slots, status)
            VALUES ('Legacy task', 'Research', 'TEXT_LINK', 'legacy', 1.0,
                    '["+14155552671"]', 1, 15, 'legacy proof', 1, 0, 'active')
        ''')
        conn.commit()
        database._initialize_database_with_connection(conn)
        database._initialize_database_with_connection(conn)
        row = conn.execute("SELECT title, target_numbers, owner_user_id FROM tasks WHERE title = 'Legacy task'").fetchone()
        self.assertEqual(row['title'], 'Legacy task')
        self.assertEqual(row['target_numbers'], '["+14155552671"]')
        self.assertIsNone(row['owner_user_id'])
        indexes = {r['name'] for r in conn.execute("PRAGMA index_list('tasks')").fetchall()}
        self.assertIn('tasks_owner_status', indexes)
        database.verify_required_schema(conn)
        conn.close()


if __name__ == '__main__':
    unittest.main()
