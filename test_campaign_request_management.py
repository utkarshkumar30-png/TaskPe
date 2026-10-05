import importlib
import os
import sqlite3
import tempfile
import unittest


ROOT = tempfile.mkdtemp(prefix='taskpe-campaign-crm-')
os.environ['TASKPE_DB_PATH'] = os.path.join(ROOT, 'campaign.db')
os.environ['ENVIRONMENT'] = 'development'
os.environ['SECRET_KEY'] = 'campaign-crm-test-secret'

import database
import app as app_module
from migrate_campaign_inquiries import migrate


class CampaignRequestManagementTests(unittest.TestCase):
    def setUp(self):
        if os.path.exists(database.DB_PATH):
            os.remove(database.DB_PATH)
        database.initialize_database()
        self.app_module = importlib.reload(app_module)
        self.app_module.app.config.update(TESTING=True)
        self.client = self.app_module.app.test_client()
        conn = database.get_db_connection()
        self.admin_id = conn.execute('''
            INSERT INTO users (name, mobile, email, password_hash, role, referral_code)
            VALUES ('CRM Admin', '919800000001', 'crm-admin@example.com', 'hash', 'admin', 'CRMADMIN')
        ''').lastrowid
        self.worker_id = conn.execute('''
            INSERT INTO users (name, mobile, email, password_hash, referral_code)
            VALUES ('CRM Worker', '919800000002', 'crm-worker@example.com', 'hash', 'CRMWORKER')
        ''').lastrowid
        conn.commit()
        conn.close()

    def _session(self, user_id):
        with self.client.session_transaction() as session:
            session['user_id'] = user_id

    def _create_request(self):
        return self.client.post('/api/campaign-inquiries', json={
            'company_name': 'ABC Apps',
            'contact_person': 'Asha Rao',
            'email': 'asha@example.com',
            'phone': '919811111111',
            'task_type': 'App & Website Testing',
            'target_location': 'India',
            'participant_count': 500,
            'campaign_description': 'Test the Android onboarding flow.',
            'estimated_budget': '₹25,000',
        })

    def test_public_creation_and_admin_workflow(self):
        created = self._create_request()
        self.assertEqual(created.status_code, 201)
        inquiry_id = created.get_json()['inquiry_id']
        self.assertEqual(self.client.get('/api/admin/campaign-inquiries').status_code, 403)

        self._session(self.worker_id)
        self.assertEqual(self.client.get('/api/admin/campaign-inquiries').status_code, 403)
        self.assertEqual(self.client.get(f'/api/admin/campaign-inquiries/{inquiry_id}').status_code, 403)

        self._session(self.admin_id)
        listed = self.client.get('/api/admin/campaign-inquiries')
        self.assertEqual(listed.status_code, 200)
        self.assertNotIn('admin_notes', listed.get_json()['inquiries'][0])
        detail = self.client.get(f'/api/admin/campaign-inquiries/{inquiry_id}')
        self.assertEqual(detail.status_code, 200)
        self.assertIsNone(detail.get_json()['inquiry']['admin_notes'])

        for status in ('contacted', 'negotiating', 'approved', 'active', 'completed', 'closed'):
            response = self.client.patch(f'/api/admin/campaign-inquiries/{inquiry_id}', json={
                'status': status,
                'admin_notes': f'Moved to {status}.',
            })
            self.assertEqual(response.status_code, 200)
        saved = self.client.get(f'/api/admin/campaign-inquiries/{inquiry_id}').get_json()['inquiry']
        self.assertEqual(saved['status'], 'closed')
        self.assertEqual(saved['admin_notes'], 'Moved to closed.')

    def test_invalid_status_and_oversized_notes_are_rejected(self):
        inquiry_id = self._create_request().get_json()['inquiry_id']
        self._session(self.admin_id)
        self.assertEqual(self.client.patch(f'/api/admin/campaign-inquiries/{inquiry_id}', json={'status': 'draft'}).status_code, 400)
        self.assertEqual(self.client.patch(f'/api/admin/campaign-inquiries/{inquiry_id}', json={'status': 'new', 'admin_notes': 'x' * 4001}).status_code, 400)

    def test_migration_upgrades_legacy_table_without_losing_rows(self):
        path = os.path.join(ROOT, 'legacy.db')
        conn = sqlite3.connect(path)
        conn.execute('''CREATE TABLE campaign_inquiries (
            id INTEGER PRIMARY KEY AUTOINCREMENT, company_name TEXT NOT NULL,
            contact_person TEXT NOT NULL, email TEXT NOT NULL, phone TEXT,
            task_type TEXT NOT NULL, target_location TEXT, participant_count INTEGER,
            campaign_description TEXT NOT NULL, estimated_budget TEXT,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'contacted', 'closed'))
        )''')
        conn.execute('''CREATE INDEX campaign_inquiries_status_created
                        ON campaign_inquiries(status, created_at)''')
        legacy_rows = [
            (7, 'Legacy One', 'Person One', 'one@example.com', '919800000007', 'Other', 'Delhi', 25, 'Keep one', '₹1,000', '2026-10-01 10:00:00', 'new'),
            (11, 'Legacy Two', 'Person Two', 'two@example.com', None, 'Customer Feedback', 'Mumbai', 40, 'Keep two', '₹2,000', '2026-10-02 11:00:00', 'contacted'),
        ]
        conn.executemany('''
            INSERT INTO campaign_inquiries
                (id, company_name, contact_person, email, phone, task_type,
                 target_location, participant_count, campaign_description,
                 estimated_budget, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', legacy_rows)
        conn.commit()
        conn.close()
        result = migrate(path)
        self.assertEqual(result['status'], 'applied')
        conn = sqlite3.connect(path)
        columns = [row[1] for row in conn.execute('PRAGMA table_info(campaign_inquiries)')]
        self.assertIn('admin_notes', columns)
        active_rows = conn.execute('''
            SELECT id, company_name, contact_person, email, phone, task_type,
                   target_location, participant_count, campaign_description,
                   estimated_budget, created_at, status, admin_notes
            FROM campaign_inquiries ORDER BY id
        ''').fetchall()
        self.assertEqual(len(active_rows), len(legacy_rows))
        self.assertEqual([row[:12] for row in active_rows], legacy_rows)
        self.assertEqual([row[12] for row in active_rows], [None, None])
        legacy_row_count = conn.execute('SELECT COUNT(*) FROM campaign_inquiries_legacy_before_campaign_crm').fetchone()[0]
        self.assertEqual(legacy_row_count, len(legacy_rows))
        active_index = conn.execute('''
            SELECT name FROM pragma_index_list('campaign_inquiries')
            WHERE name = 'campaign_inquiries_status_created'
        ''').fetchone()
        self.assertIsNotNone(active_index)
        index_columns = [row[2] for row in conn.execute("PRAGMA index_info('campaign_inquiries_status_created')")]
        self.assertEqual(index_columns, ['status', 'created_at'])
        for status in ('new', 'contacted', 'negotiating', 'approved', 'active', 'completed', 'closed'):
            conn.execute('UPDATE campaign_inquiries SET status = ? WHERE id = ?', (status, legacy_rows[0][0]))
        conn.commit()
        conn.close()

        migrate(path)
        conn = sqlite3.connect(path)
        second_rows = conn.execute('''
            SELECT id, company_name, contact_person, email, phone, task_type,
                   target_location, participant_count, campaign_description,
                   estimated_budget, created_at, status, admin_notes
            FROM campaign_inquiries ORDER BY id
        ''').fetchall()
        self.assertEqual(len(second_rows), len(legacy_rows))
        self.assertEqual([row[0] for row in second_rows], [legacy_rows[0][0], legacy_rows[1][0]])
        self.assertEqual([row[1:12] for row in second_rows], [
            ('Legacy One', 'Person One', 'one@example.com', '919800000007', 'Other', 'Delhi', 25, 'Keep one', '₹1,000', '2026-10-01 10:00:00', 'closed'),
            ('Legacy Two', 'Person Two', 'two@example.com', None, 'Customer Feedback', 'Mumbai', 40, 'Keep two', '₹2,000', '2026-10-02 11:00:00', 'contacted'),
        ])
        self.assertEqual([row[12] for row in second_rows], [None, None])
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM campaign_inquiries_legacy_before_campaign_crm').fetchone()[0], len(legacy_rows))
        self.assertIsNotNone(conn.execute('''
            SELECT name FROM pragma_index_list('campaign_inquiries')
            WHERE name = 'campaign_inquiries_status_created'
        ''').fetchone())
        conn.close()


if __name__ == '__main__':
    unittest.main()
