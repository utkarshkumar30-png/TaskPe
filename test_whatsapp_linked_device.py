import os
import tempfile
import unittest
import io
import json
import urllib.error
from unittest.mock import patch

os.environ.setdefault('ENVIRONMENT', 'production')
os.environ.setdefault('SECRET_KEY', 'local-test-secret-for-whatsapp-routes')
_db_dir = tempfile.mkdtemp(prefix='taskpe-wa-flask-', dir=os.getcwd())
os.environ['TASKPE_DB_PATH'] = os.path.join(_db_dir, 'test.db')
os.environ['TASKPE_UPLOAD_FOLDER'] = os.path.join(_db_dir, 'uploads')
os.environ['TASKPE_TASK_ASSET_FOLDER'] = os.path.join(_db_dir, 'assets')
os.environ['TASKPE_WHATSAPP_LINKED_DEVICE_ENABLED'] = '0'
os.environ.setdefault('TASKPE_WA_SERVICE_SHARED_SECRET', 'local-test-secret-that-is-at-least-32-characters')

import database
database.DB_PATH = os.path.join(_db_dir, 'test.db')
database.init_db()
import app as app_module
from werkzeug.security import generate_password_hash


class WhatsAppRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # A preceding isolated WhatsApp module may have removed its temporary
        # database while leaving database.DB_PATH pointing at it.
        cls._old_db_path = database.DB_PATH
        database.DB_PATH = os.path.join(_db_dir, 'test.db')
        cls._old_test_transport_enabled = app_module.WHATSAPP_TEST_TRANSPORT_ENABLED
        cls._old_consent_gate_available = app_module.WHATSAPP_CONSENT_GATE_AVAILABLE
        app_module.WHATSAPP_TEST_TRANSPORT_ENABLED = False
        app_module.WHATSAPP_CONSENT_GATE_AVAILABLE = False
        conn = database.get_db_connection()
        cls.user_ids = []
        for index in range(2):
            cursor = conn.execute(
                "INSERT INTO users (name, email, password_hash, mobile, referral_code, role) VALUES (?, ?, ?, ?, ?, ?)",
                (f'WA Test {index}', f'wa{index}@example.com', generate_password_hash('not-used'), f'91999999999{index}', f'WATEST{index}', 'admin' if index == 0 else 'user'),
            )
            cls.user_ids.append(cursor.lastrowid)
        conn.commit()
        conn.close()
        cls.client = app_module.app.test_client()
        cls.old_enabled = app_module.WHATSAPP_LINKED_DEVICE_ENABLED

    @classmethod
    def tearDownClass(cls):
        app_module.WHATSAPP_LINKED_DEVICE_ENABLED = cls.old_enabled
        app_module.WHATSAPP_TEST_TRANSPORT_ENABLED = cls._old_test_transport_enabled
        app_module.WHATSAPP_CONSENT_GATE_AVAILABLE = cls._old_consent_gate_available
        database.DB_PATH = cls._old_db_path

    def as_user(self, user_id):
        with self.client.session_transaction() as session:
            session['user_id'] = user_id
            session['_csrf_token'] = 'test-csrf-token'

    def tearDown(self):
        with self.client.session_transaction() as session:
            session.clear()
        app_module.WHATSAPP_LINKED_DEVICE_ENABLED = False

    def test_unauthenticated_routes_reject(self):
        self.assertEqual(self.client.get('/api/whatsapp/connection/status').status_code, 401)
        self.assertEqual(self.client.post('/api/whatsapp/connection/qr').status_code, 401)
        self.assertEqual(self.client.post('/api/whatsapp/connection/disconnect').status_code, 401)

    def test_disabled_feature_returns_404(self):
        self.as_user(self.user_ids[0])
        self.assertEqual(self.client.get('/api/whatsapp/connection/status').status_code, 404)
        self.assertEqual(self.client.post('/api/whatsapp/connection/qr', headers={'X-CSRF-Token': 'test-csrf-token'}).status_code, 404)

    def test_platform_opt_out_uses_node_authority_before_flask_mirror(self):
        self.as_user(self.user_ids[0])
        with patch.object(app_module, '_call_whatsapp_service', return_value=(
            {'success': True, 'status': 'applied', 'request_id': 'optout-route-001', 'version': 1}, 200
        )) as call:
            response = self.client.post(
                '/api/whatsapp/opt-outs',
                json={
                    'phone_number': '+14155550123',
                    'request_id': 'optout-route-001',
                    'reason': 'user_opt_out',
                },
                headers={'X-CSRF-Token': 'test-csrf-token'},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['status'], 'applied')
        call.assert_called_once()
        self.assertTrue(call.call_args.kwargs['allow_when_disabled'])
        conn = database.get_db_connection()
        try:
            self.assertIsNotNone(conn.execute(
                'SELECT 1 FROM whatsapp_opt_outs WHERE phone_number = ?', ('+14155550123',)
            ).fetchone())
        finally:
            conn.close()

    def test_platform_opt_out_requires_admin_authorization(self):
        self.as_user(self.user_ids[1])
        response = self.client.post(
            '/api/whatsapp/opt-outs',
            json={'phone_number': '+14155550123', 'request_id': 'ordinary-user-optout-001'},
            headers={'X-CSRF-Token': 'test-csrf-token'},
        )
        self.assertEqual(response.status_code, 403)

    def test_campaign_discovery_requires_authentication_and_feature_flag(self):
        self.assertEqual(self.client.get('/api/whatsapp/campaigns/eligible').status_code, 401)
        self.as_user(self.user_ids[0])
        self.assertEqual(self.client.get('/api/whatsapp/campaigns/eligible').status_code, 404)

    def _insert_discovery_task(self, campaign_id, numbers, task_type='TEXT_LINK'):
        conn = database.get_db_connection()
        numbers = [database.normalize_recipient_number(number, region='IN') for number in numbers]
        cursor = conn.execute('''
            INSERT INTO tasks
                (title, category, task_type, description, payout, template_text,
                 target_numbers, recipient_region, campaign_id, recipients_per_slot, time_limit_minutes,
                 proof_instructions, total_slots, completed_slots, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'IN', ?, ?, ?, ?, ?, ?, ?)
        ''', (
            f'Discovery campaign {campaign_id}', 'WhatsApp Tasks', task_type,
            'Read-only discovery fixture', 1.0, 'Do not expose this template',
            json.dumps(numbers), campaign_id, 25, 25, 'No live proof', 1, 0, 'active',
        ))
        conn.commit()
        task_id = cursor.lastrowid
        conn.close()
        return task_id

    def test_campaign_discovery_returns_advisory_counts_without_recipient_data(self):
        task_id = self._insert_discovery_task(88001, ['919111111111', '919222222222', '919333333333'])
        normalized_numbers = [database.normalize_recipient_number(number, region='IN') for number in ['919111111111', '919222222222', '919333333333']]
        conn = database.get_db_connection()
        assignment = conn.execute('''
            INSERT INTO task_assignments
                (user_id, task_id, campaign_id, campaign_type, assigned_numbers, status)
            VALUES (?, ?, ?, 'WhatsApp Tasks', ?, 'in_progress')
        ''', (self.user_ids[0], task_id, 88001, json.dumps([normalized_numbers[0]])))
        assignment_id = assignment.lastrowid
        conn.execute('''
            INSERT INTO recipient_allocations
                (campaign_id, phone_number, assignment_id, user_id, status)
            VALUES (?, ?, ?, ?, 'ALLOCATED')
        ''', (88001, normalized_numbers[0], assignment_id, self.user_ids[0]))
        conn.execute('''
            INSERT INTO whatsapp_campaign_recipient_claims
                (campaign_id, task_id, recipient_number, plan_id, plan_recipient_id, state)
            VALUES (?, ?, ?, ?, ?, 'claimed')
        ''', (88001, task_id, normalized_numbers[1], 999001, 999001,))
        conn.commit()
        conn.close()
        self.as_user(self.user_ids[0])
        app_module.WHATSAPP_LINKED_DEVICE_ENABLED = True
        with patch.object(app_module, '_call_whatsapp_service', return_value=({
            'success': True, 'worker_id': self.user_ids[0], 'quota_day': '2026-10-17',
            'limit': 80, 'reserved': 10, 'remaining': 70, 'timezone': 'Asia/Kolkata',
        }, 200)) as call:
            response = self.client.get('/api/whatsapp/campaigns/eligible')
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body['success'])
        self.assertTrue(body['advisory'])
        self.assertTrue(body['consent_opt_out_enforced'])
        campaign = next(item for item in body['campaigns'] if item['task_id'] == task_id)
        self.assertEqual(campaign['source_recipient_count'], 3)
        self.assertEqual(campaign['eligible_recipient_count'], 1)
        self.assertEqual(campaign['exclusion_counts']['allocated_same_campaign'], 1)
        self.assertEqual(campaign['exclusion_counts']['claimed_same_campaign'], 1)
        self.assertEqual(campaign['max_selectable_quantity'], 1)
        self.assertNotIn('919111111111', response.get_data(as_text=True))
        self.assertNotIn('919222222222', response.get_data(as_text=True))
        self.assertNotIn('Do not expose this template', response.get_data(as_text=True))
        call.assert_called_once_with('GET', f'/v1/users/{self.user_ids[0]}/quota', self.user_ids[0])

    def test_campaign_discovery_uses_authenticated_worker_and_reports_quota_failures(self):
        task_id = self._insert_discovery_task(88002, ['919444444444'])
        self.as_user(self.user_ids[1])
        app_module.WHATSAPP_LINKED_DEVICE_ENABLED = True
        with patch.object(app_module, '_call_whatsapp_service', return_value=({'success': False}, 503)) as call:
            response = self.client.get('/api/whatsapp/campaigns/eligible?worker_id=1')
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body['quota'], {'status': 'unavailable'})
        campaign = next(item for item in body['campaigns'] if item['task_id'] == task_id)
        self.assertEqual(campaign['availability'], 'quota_unavailable')
        self.assertIsNone(campaign['max_selectable_quantity'])
        call.assert_called_once_with('GET', f'/v1/users/{self.user_ids[1]}/quota', self.user_ids[1])

    def test_campaign_discovery_rejects_malformed_quota_without_guessing(self):
        self._insert_discovery_task(88003, ['919555555555'])
        self.as_user(self.user_ids[0])
        app_module.WHATSAPP_LINKED_DEVICE_ENABLED = True
        with patch.object(app_module, '_call_whatsapp_service', return_value=({
            'success': True, 'worker_id': self.user_ids[0], 'quota_day': '2026-10-17',
            'limit': 80, 'reserved': 'not-a-number', 'remaining': 80, 'timezone': 'Asia/Kolkata',
        }, 200)):
            response = self.client.get('/api/whatsapp/campaigns/eligible')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['quota'], {'status': 'unavailable'})

    def test_campaign_prepare_requires_auth_csrf_and_feature_flag(self):
        response = self.client.post('/api/whatsapp/campaigns/prepare', json={
            'campaign_id': 1, 'quantity': 20, 'idempotency_key': 'prepare-auth-test',
        })
        self.assertEqual(response.status_code, 401)
        self.as_user(self.user_ids[0])
        response = self.client.post('/api/whatsapp/campaigns/prepare', json={
            'campaign_id': 1, 'quantity': 20, 'idempotency_key': 'prepare-csrf-test',
        })
        self.assertEqual(response.status_code, 403)
        response = self.client.post('/api/whatsapp/campaigns/prepare', json={
            'campaign_id': 1, 'quantity': 20, 'idempotency_key': 'prepare-disabled-test',
        }, headers={'X-CSRF-Token': 'test-csrf-token'})
        self.assertEqual(response.status_code, 404)

    def test_campaign_prepare_reserves_without_sending_and_is_idempotent(self):
        self._insert_discovery_task(88011, ['919111111111', '919222222222', '919333333333'])
        self.as_user(self.user_ids[0])
        app_module.WHATSAPP_LINKED_DEVICE_ENABLED = True
        quota = {'status': 'available', 'remaining': 80, 'limit': 80,
                 'reserved': 0, 'quota_day': '2026-10-17', 'timezone': 'Asia/Kolkata'}
        with patch.object(app_module, '_worker_whatsapp_quota_status', return_value=quota), \
             patch.object(app_module, 'reserve_campaign_send_plan_with_node', return_value={'status': 'reserved'}) as reserve, \
             patch.object(app_module, '_call_whatsapp_service') as service:
            payload = {'campaign_id': 88011, 'quantity': 2, 'idempotency_key': 'prepare-idempotent-test'}
            first = self.client.post('/api/whatsapp/campaigns/prepare', json=payload, headers={'X-CSRF-Token': 'test-csrf-token'})
            second = self.client.post('/api/whatsapp/campaigns/prepare', json=payload, headers={'X-CSRF-Token': 'test-csrf-token'})
        self.assertEqual(first.status_code, 200, first.get_json())
        self.assertEqual(second.status_code, 200, second.get_json())
        self.assertTrue(second.get_json()['idempotent'])
        self.assertEqual(reserve.call_count, 2)
        service.assert_not_called()
        conn = database.get_db_connection()
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM whatsapp_campaign_plans WHERE plan_key = ?', (payload['idempotency_key'],)).fetchone()[0], 1)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM whatsapp_campaign_plan_recipients WHERE plan_id = (SELECT id FROM whatsapp_campaign_plans WHERE plan_key = ?)', (payload['idempotency_key'],)).fetchone()[0], 2)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM whatsapp_campaign_recipient_claims WHERE plan_id = (SELECT id FROM whatsapp_campaign_plans WHERE plan_key = ?) AND state = 'claimed'", (payload['idempotency_key'],)).fetchone()[0], 2)
        conn.close()

    def test_campaign_prepare_rejects_invalid_quantity_and_unavailable_quota(self):
        self._insert_discovery_task(88012, ['919111111112', '919222222223'])
        self._insert_discovery_task(88014, ['919111111115'], task_type='VIDEO_TEXT_LINK')
        self.as_user(self.user_ids[0])
        app_module.WHATSAPP_LINKED_DEVICE_ENABLED = True
        quota = {'status': 'available', 'remaining': 80, 'limit': 80,
                 'reserved': 0, 'quota_day': '2026-10-17', 'timezone': 'Asia/Kolkata'}
        with patch.object(app_module, '_worker_whatsapp_quota_status', return_value=quota):
            response = self.client.post('/api/whatsapp/campaigns/prepare', json={
                'campaign_id': 88012, 'quantity': 3, 'idempotency_key': 'prepare-too-many',
            }, headers={'X-CSRF-Token': 'test-csrf-token'})
        self.assertEqual(response.status_code, 400)
        with patch.object(app_module, '_worker_whatsapp_quota_status', return_value={'status': 'unavailable'}):
            response = self.client.post('/api/whatsapp/campaigns/prepare', json={
                'campaign_id': 88012, 'quantity': 1, 'idempotency_key': 'prepare-quota-unavailable',
            }, headers={'X-CSRF-Token': 'test-csrf-token'})
        self.assertEqual(response.status_code, 503)
        with patch.object(app_module, '_worker_whatsapp_quota_status', return_value={
            'status': 'available', 'remaining': 1, 'limit': 80, 'reserved': 79,
            'quota_day': '2026-10-17', 'timezone': 'Asia/Kolkata',
        }):
            response = self.client.post('/api/whatsapp/campaigns/prepare', json={
                'campaign_id': 88012, 'quantity': 2, 'idempotency_key': 'prepare-quota-exhausted',
            }, headers={'X-CSRF-Token': 'test-csrf-token'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()['status'], 'quota_exceeded')
        conn = database.get_db_connection()
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM whatsapp_campaign_plans WHERE plan_key = ?', ('prepare-quota-exhausted',)).fetchone()[0], 0)
        conn.close()
        response = self.client.post('/api/whatsapp/campaigns/prepare', json={
            'campaign_id': 88014, 'quantity': 1, 'idempotency_key': 'prepare-unsupported',
        }, headers={'X-CSRF-Token': 'test-csrf-token'})
        self.assertEqual(response.status_code, 404)

    def test_campaign_prepare_retains_claims_on_uncertain_node_result(self):
        self._insert_discovery_task(88013, ['919111111114'])
        self.as_user(self.user_ids[0])
        app_module.WHATSAPP_LINKED_DEVICE_ENABLED = True
        quota = {'status': 'available', 'remaining': 80, 'limit': 80,
                 'reserved': 0, 'quota_day': '2026-10-17', 'timezone': 'Asia/Kolkata'}
        with patch.object(app_module, '_worker_whatsapp_quota_status', return_value=quota), \
             patch.object(app_module, 'reserve_campaign_send_plan_with_node', return_value={'status': 'unknown', 'recovery_required': True}):
            response = self.client.post('/api/whatsapp/campaigns/prepare', json={
                'campaign_id': 88013, 'quantity': 1, 'idempotency_key': 'prepare-uncertain',
            }, headers={'X-CSRF-Token': 'test-csrf-token'})
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()['recovery_required'])

    def test_campaign_send_is_auth_csrf_and_consent_gated_without_node_call(self):
        response = self.client.post('/api/whatsapp/campaigns/send', json={'plan_id': 1})
        self.assertEqual(response.status_code, 401)
        self.as_user(self.user_ids[0])
        response = self.client.post(
            '/api/whatsapp/campaigns/send',
            json={'plan_id': 1},
            headers={'X-CSRF-Token': 'test-csrf-token'},
        )
        self.assertEqual(response.status_code, 404)
        app_module.WHATSAPP_LINKED_DEVICE_ENABLED = True
        with patch.object(app_module, '_call_whatsapp_service') as service:
            response = self.client.post('/api/whatsapp/campaigns/send', json={'plan_id': 1})
            self.assertEqual(response.status_code, 403)
            response = self.client.post(
                '/api/whatsapp/campaigns/send',
                json={'plan_id': 1},
                headers={'X-CSRF-Token': 'test-csrf-token'},
            )
        self.assertEqual(response.status_code, 412)
        self.assertEqual(response.get_json()['status'], 'consent_required')
        service.assert_not_called()

    def test_internal_send_boundary_uses_persisted_reserved_batch_and_sanitizes_ack(self):
        plan = {'id': 101, 'batch_id': 'batch-send-boundary-001', 'batch_request_fingerprint': 'a' * 64, 'dispatch_state': 'not_started', 'dispatch_operation_id': None, 'sender_worker_id': self.user_ids[0]}
        rows = [{'position': 1, 'ledger_message_id': 'ledger-message-1'}]
        response = {
            'success': True,
            'batch_id': plan['batch_id'],
            'items': [{'position': 1, 'message_id': 'ledger-message-1', 'status': 'acknowledged'}],
        }
        with patch.object(app_module, 'WHATSAPP_CONSENT_GATE_AVAILABLE', True), \
             patch.object(app_module, '_campaign_batch_plan_row', return_value=plan), \
             patch.object(app_module, '_validate_campaign_send_plan_for_dispatch', return_value=(plan, rows)), \
             patch.object(app_module, '_campaign_batch_adapter_call', return_value=(response, 200)) as adapter, \
             patch.object(app_module, '_claim_campaign_dispatch', return_value={
                 'claimed': True, 'state': 'in_progress', 'operation_id': 'operation-1',
             }), \
             patch.object(app_module, '_project_campaign_send_outcomes', return_value={
                 'acknowledged': 1, 'failed': 0, 'uncertain': 0, 'not_sendable': 0,
             }) as project:
            result = app_module.send_reserved_whatsapp_campaign_batch(
                object(), 41, self.user_ids[0], now='2026-10-17 12:00:00',
            )
        self.assertEqual(result, {
            'status': 'acknowledged', 'delivered': False,
            'acknowledged': 1, 'failed': 0, 'uncertain': 0, 'not_sendable': 0,
        })
        self.assertEqual(adapter.call_count, 1)
        self.assertEqual(adapter.call_args.args, (
            None, 'send', self.user_ids[0],
            f'/v1/users/{self.user_ids[0]}/campaign-batches/{plan["batch_id"]}/send',
        ))
        self.assertIn('X-TaskPe-Dispatch-Authorization', adapter.call_args.kwargs.get('extra_headers', {}))
        project.assert_called_once()

    def test_dispatcher_consent_gate_blocks_direct_invocation_before_node(self):
        with patch.object(app_module, 'WHATSAPP_CONSENT_GATE_AVAILABLE', False), \
             patch.object(app_module, '_campaign_batch_adapter_call') as adapter:
            result = app_module.send_reserved_whatsapp_campaign_batch(
                object(), 999, self.user_ids[0], now='2026-10-17 12:00:00',
            )
        self.assertEqual(result, {
            'status': 'consent_required', 'recovery_required': False, 'delivered': False,
        })
        adapter.assert_not_called()

    def test_dispatch_interruption_keeps_batch_non_dispatchable_and_recovers_by_lookup(self):
        start = {
            'id': 103, 'batch_id': 'batch-send-boundary-003', 'batch_request_fingerprint': 'b' * 64, 'dispatch_state': 'not_started',
            'dispatch_operation_id': None, 'sender_worker_id': self.user_ids[0],
        }
        in_progress = {**start, 'dispatch_state': 'in_progress', 'dispatch_operation_id': 'operation-3'}
        rows = [{'position': 1, 'ledger_message_id': 'ledger-message-3'}]
        with patch.object(app_module, 'WHATSAPP_CONSENT_GATE_AVAILABLE', True), \
             patch.object(app_module, '_campaign_batch_plan_row', side_effect=[start, in_progress]), \
             patch.object(app_module, '_validate_campaign_send_plan_for_dispatch', return_value=(start, rows)), \
             patch.object(app_module, '_claim_campaign_dispatch', return_value={
                 'claimed': True, 'state': 'in_progress', 'operation_id': 'operation-3',
             }), \
             patch.object(app_module, '_campaign_batch_adapter_call', side_effect=RuntimeError('simulated interruption')) as adapter, \
             patch.object(app_module, '_mark_campaign_dispatch_unknown', side_effect=RuntimeError('database unavailable')):
            first = app_module.send_reserved_whatsapp_campaign_batch(
                object(), 43, self.user_ids[0], now='2026-10-17 12:00:00',
            )
        self.assertEqual(first['status'], 'recovery_required')
        self.assertEqual(adapter.call_count, 1)
        self.assertEqual(adapter.call_args.args[1], 'send')

        with patch.object(app_module, 'WHATSAPP_CONSENT_GATE_AVAILABLE', True), \
             patch.object(app_module, '_campaign_batch_plan_row', return_value=in_progress), \
             patch.object(app_module, '_campaign_batch_recipient_rows', return_value=rows), \
             patch.object(app_module, '_validate_campaign_send_plan_for_dispatch', return_value=(in_progress, rows)), \
             patch.object(app_module, '_campaign_batch_adapter_call', return_value=({'success': False}, 503)) as lookup:
            recovered = app_module.send_reserved_whatsapp_campaign_batch(
                object(), 43, self.user_ids[0], now='2026-10-17 12:01:00',
            )
        self.assertEqual(recovered['status'], 'recovery_required')
        lookup.assert_called_once()
        self.assertEqual(lookup.call_args.args[1], 'lookup')

    def test_concurrent_dispatch_claim_allows_only_one_send_call(self):
        plan = {
            'id': 104, 'batch_id': 'batch-send-boundary-004', 'batch_request_fingerprint': 'c' * 64, 'dispatch_state': 'not_started',
            'dispatch_operation_id': None, 'sender_worker_id': self.user_ids[0],
        }
        in_progress = {**plan, 'dispatch_state': 'in_progress', 'dispatch_operation_id': 'operation-4'}
        rows = [{'position': 1, 'ledger_message_id': 'ledger-message-4'}]
        response = {'success': True, 'batch_id': plan['batch_id'], 'items': [
            {'position': 1, 'message_id': 'ledger-message-4', 'status': 'acknowledged'},
        ]}
        with patch.object(app_module, 'WHATSAPP_CONSENT_GATE_AVAILABLE', True), \
             patch.object(app_module, '_campaign_batch_plan_row', side_effect=[plan, in_progress, in_progress]), \
             patch.object(app_module, '_validate_campaign_send_plan_for_dispatch', return_value=(plan, rows)), \
             patch.object(app_module, '_campaign_batch_recipient_rows', return_value=rows), \
             patch.object(app_module, '_claim_campaign_dispatch', side_effect=[
                 {'claimed': True, 'state': 'in_progress', 'operation_id': 'operation-4'},
                 {'claimed': False, 'state': 'in_progress', 'operation_id': 'operation-4'},
             ]), \
             patch.object(app_module, '_campaign_batch_adapter_call', side_effect=[
                 (response, 200), ({'success': False}, 503),
             ]) as adapter, \
             patch.object(app_module, '_project_campaign_send_outcomes', return_value={
                 'acknowledged': 1, 'failed': 0, 'uncertain': 0, 'not_sendable': 0,
             }):
            first = app_module.send_reserved_whatsapp_campaign_batch(
                object(), 44, self.user_ids[0], now='2026-10-17 12:00:00',
            )
            second = app_module.send_reserved_whatsapp_campaign_batch(
                object(), 44, self.user_ids[0], now='2026-10-17 12:00:01',
            )
        self.assertEqual(first['status'], 'acknowledged')
        self.assertEqual(second['status'], 'recovery_required')
        self.assertEqual([call.args[1] for call in adapter.call_args_list], ['send', 'lookup'])

    def test_dispatch_schema_has_additive_durable_claim_columns(self):
        conn = database.get_db_connection()
        columns = {row[1] for row in conn.execute('PRAGMA table_info(whatsapp_campaign_plans)')}
        conn.close()
        self.assertTrue({'dispatch_state', 'dispatch_operation_id', 'dispatch_started_at'} <= columns)

    def test_internal_send_boundary_marks_uncertain_and_never_retries_terminal_plan(self):
        plan = {'id': 102, 'batch_id': 'batch-send-boundary-002', 'batch_request_fingerprint': 'd' * 64, 'dispatch_state': 'not_started', 'dispatch_operation_id': None, 'sender_worker_id': self.user_ids[0]}
        rows = [{'position': 1, 'ledger_message_id': 'ledger-message-2'}]
        conn = object()
        response = {
            'success': True,
            'batch_id': plan['batch_id'],
            'items': [{'position': 1, 'message_id': 'ledger-message-2', 'status': 'uncertain'}],
        }
        with patch.object(app_module, 'WHATSAPP_CONSENT_GATE_AVAILABLE', True), \
             patch.object(app_module, '_campaign_batch_plan_row', return_value=plan), \
             patch.object(app_module, '_validate_campaign_send_plan_for_dispatch', return_value=(plan, rows)), \
             patch.object(app_module, '_campaign_batch_adapter_call', return_value=(response, 200)), \
             patch.object(app_module, '_claim_campaign_dispatch', return_value={
                 'claimed': True, 'state': 'in_progress', 'operation_id': 'operation-2',
             }), \
             patch.object(app_module, '_project_campaign_send_outcomes', return_value={
                 'acknowledged': 0, 'failed': 0, 'uncertain': 1, 'not_sendable': 0,
             }), \
             patch.object(app_module, '_mark_campaign_dispatch_unknown') as mark_unknown:
            result = app_module.send_reserved_whatsapp_campaign_batch(
                conn, 42, self.user_ids[0], now='2026-10-17 12:00:00',
            )
        self.assertEqual(result['status'], 'recovery_required')
        self.assertFalse(result['delivered'])
        mark_unknown.assert_called_once_with(conn, 42, 'operation-2', '2026-10-17 12:00:00')
        conn = database.get_db_connection()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM whatsapp_campaign_recipient_claims WHERE plan_id = (SELECT id FROM whatsapp_campaign_plans WHERE plan_key = ?) AND state = 'claimed'", ('prepare-uncertain',)).fetchone()[0], 1)
        conn.close()

    def test_uncertain_plan_recovers_after_campaign_is_paused(self):
        self._insert_discovery_task(88015, ['919111111116'])
        self.as_user(self.user_ids[0])
        app_module.WHATSAPP_LINKED_DEVICE_ENABLED = True
        quota = {'status': 'available', 'remaining': 80, 'limit': 80,
                 'reserved': 0, 'quota_day': '2026-10-17', 'timezone': 'Asia/Kolkata'}
        payload = {'campaign_id': 88015, 'quantity': 1, 'idempotency_key': 'prepare-paused-recovery'}
        with patch.object(app_module, '_worker_whatsapp_quota_status', return_value=quota), \
             patch.object(app_module, 'reserve_campaign_send_plan_with_node', side_effect=[
                 {'status': 'unknown', 'recovery_required': True},
                 {'status': 'reserved'},
             ]) as reserve:
            first = self.client.post('/api/whatsapp/campaigns/prepare', json=payload, headers={'X-CSRF-Token': 'test-csrf-token'})
            conn = database.get_db_connection()
            conn.execute("UPDATE tasks SET status = 'paused' WHERE campaign_id = 88015")
            conn.commit()
            conn.close()
            second = self.client.post('/api/whatsapp/campaigns/prepare', json=payload, headers={'X-CSRF-Token': 'test-csrf-token'})
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 200, second.get_json())
        self.assertEqual(reserve.call_count, 2)

    def test_paused_campaign_cannot_create_a_new_plan(self):
        task_id = self._insert_discovery_task(88016, ['919111111117'])
        conn = database.get_db_connection()
        conn.execute("UPDATE tasks SET status = 'paused' WHERE id = ?", (task_id,))
        conn.commit()
        conn.close()
        self.as_user(self.user_ids[0])
        app_module.WHATSAPP_LINKED_DEVICE_ENABLED = True
        with patch.object(app_module, '_worker_whatsapp_quota_status') as quota:
            response = self.client.post('/api/whatsapp/campaigns/prepare', json={
                'campaign_id': 88016, 'quantity': 1, 'idempotency_key': 'prepare-paused-new',
            }, headers={'X-CSRF-Token': 'test-csrf-token'})
        self.assertEqual(response.status_code, 404)
        quota.assert_not_called()

    def test_worker_cannot_recover_another_workers_plan(self):
        self._insert_discovery_task(88017, ['919111111118'])
        payload = {'campaign_id': 88017, 'quantity': 1, 'idempotency_key': 'prepare-other-worker'}
        app_module.WHATSAPP_LINKED_DEVICE_ENABLED = True
        quota = {'status': 'available', 'remaining': 80, 'limit': 80,
                 'reserved': 0, 'quota_day': '2026-10-17', 'timezone': 'Asia/Kolkata'}
        self.as_user(self.user_ids[0])
        with patch.object(app_module, '_worker_whatsapp_quota_status', return_value=quota), \
             patch.object(app_module, 'reserve_campaign_send_plan_with_node', return_value={'status': 'unknown', 'recovery_required': True}):
            self.assertEqual(self.client.post('/api/whatsapp/campaigns/prepare', json=payload, headers={'X-CSRF-Token': 'test-csrf-token'}).status_code, 202)
        self.as_user(self.user_ids[1])
        with patch.object(app_module, 'reserve_campaign_send_plan_with_node') as reserve:
            response = self.client.post('/api/whatsapp/campaigns/prepare', json=payload, headers={'X-CSRF-Token': 'test-csrf-token'})
        self.assertEqual(response.status_code, 409)
        reserve.assert_not_called()

    def test_ui_persists_pending_key_by_worker_campaign_and_quantity(self):
        with open(os.path.join(os.path.dirname(__file__), 'static', 'js', 'app.js'), encoding='utf-8') as handle:
            source = handle.read()
        self.assertIn('sessionStorage.getItem(storageKey)', source)
        self.assertIn('sessionStorage.setItem(storageKey, idempotencyKey)', source)
        self.assertIn('sessionStorage.removeItem(storageKey)', source)
        self.assertIn('taskpe-wa-plan-v1:${workerScope}:${Number(campaignId)}:${selected}', source)

    def test_worker_ui_restores_connection_card_without_adding_send_behavior(self):
        with open(os.path.join(os.path.dirname(__file__), 'templates', 'index.html'), encoding='utf-8') as handle:
            template = handle.read()
        with open(os.path.join(os.path.dirname(__file__), 'static', 'js', 'app.js'), encoding='utf-8') as handle:
            source = handle.read()
        self.assertIn('id="whatsapp-connection-card"', template)
        self.assertIn('id="whatsapp-connection-request-qr"', template)
        self.assertIn('id="whatsapp-connection-disconnect"', template)
        self.assertIn("/api/whatsapp/connection/status", source)
        self.assertIn("/api/whatsapp/connection/qr", source)
        self.assertIn("/api/whatsapp/connection/disconnect", source)
        self.assertIn('Automated campaign sending remains disabled', template)
        self.assertNotIn("/api/whatsapp/campaigns/send", source)

    def test_csrf_required_for_state_change(self):
        self.as_user(self.user_ids[0])
        app_module.WHATSAPP_LINKED_DEVICE_ENABLED = True
        with patch.object(app_module, '_call_whatsapp_service') as call:
            missing = self.client.post('/api/whatsapp/connection/qr', json={'user_id': self.user_ids[1]})
            invalid = self.client.post('/api/whatsapp/connection/disconnect', headers={'X-CSRF-Token': 'wrong-token'})
        self.assertEqual(missing.status_code, 403)
        self.assertEqual(invalid.status_code, 403)
        call.assert_not_called()

    def test_browser_user_id_is_ignored_for_service_identity(self):
        self.as_user(self.user_ids[0])
        app_module.WHATSAPP_LINKED_DEVICE_ENABLED = True
        with patch.object(app_module, '_call_whatsapp_service', return_value=({'success': True}, 200)) as call:
            response = self.client.post('/api/whatsapp/connection/qr', json={'user_id': self.user_ids[1]}, headers={'X-CSRF-Token': 'test-csrf-token'})
        self.assertEqual(response.status_code, 200)
        call.assert_called_once_with('POST', f'/v1/users/{self.user_ids[0]}/qr', self.user_ids[0])

    def test_disconnect_route_uses_authenticated_worker(self):
        self.as_user(self.user_ids[0])
        app_module.WHATSAPP_LINKED_DEVICE_ENABLED = True
        with patch.object(app_module, '_call_whatsapp_service', return_value=({'success': True, 'cleanup': True}, 200)) as call:
            response = self.client.post('/api/whatsapp/connection/disconnect', headers={'X-CSRF-Token': 'test-csrf-token'})
        self.assertEqual(response.status_code, 200)
        call.assert_called_once_with('POST', f'/v1/users/{self.user_ids[0]}/disconnect', self.user_ids[0])

    def test_service_url_allowlist_rejects_non_loopback_or_noncanonical_urls(self):
        old_url = app_module.WHATSAPP_SERVICE_URL
        old_port = os.environ.get('TASKPE_WA_SERVICE_PORT')
        try:
            os.environ['TASKPE_WA_SERVICE_PORT'] = '8765'
            allowed = 'http://127.0.0.1:8765'
            app_module.WHATSAPP_SERVICE_URL = allowed
            self.assertEqual(app_module._validated_whatsapp_service_url(), allowed)
            for invalid in (
                'https://127.0.0.1:8765',
                'http://localhost:8765',
                'http://127.0.0.1:8766',
                'http://user:pass@127.0.0.1:8765',
                'http://127.0.0.1:8765/private',
                'http://127.0.0.1:8765/?redirect=1',
                'http://127.0.0.1:8765/#fragment',
            ):
                app_module.WHATSAPP_SERVICE_URL = invalid
                self.assertIsNone(app_module._validated_whatsapp_service_url(), invalid)
        finally:
            app_module.WHATSAPP_SERVICE_URL = old_url
            if old_port is None:
                os.environ.pop('TASKPE_WA_SERVICE_PORT', None)
            else:
                os.environ['TASKPE_WA_SERVICE_PORT'] = old_port

    def test_redirect_is_not_followed_and_returns_safe_failure(self):
        old_enabled = app_module.WHATSAPP_LINKED_DEVICE_ENABLED
        old_url = app_module.WHATSAPP_SERVICE_URL
        old_secret = app_module.WHATSAPP_SERVICE_SHARED_SECRET
        old_port = os.environ.get('TASKPE_WA_SERVICE_PORT')
        try:
            app_module.WHATSAPP_LINKED_DEVICE_ENABLED = True
            app_module.WHATSAPP_SERVICE_URL = 'http://127.0.0.1:8765'
            app_module.WHATSAPP_SERVICE_SHARED_SECRET = 'local-test-secret-that-is-at-least-32-characters'
            os.environ['TASKPE_WA_SERVICE_PORT'] = '8765'
            redirect = urllib.error.HTTPError(
                'http://127.0.0.1:8765/v1/users/1/qr', 302, 'redirect rejected', {}, io.BytesIO()
            )
            with patch.object(app_module._WHATSAPP_SERVICE_OPENER, 'open', side_effect=redirect) as opener:
                result, status = app_module._call_whatsapp_service('POST', '/v1/users/1/qr', 1)
            self.assertEqual(status, 302)
            self.assertFalse(result['success'])
            opener.assert_called_once()
        finally:
            app_module.WHATSAPP_LINKED_DEVICE_ENABLED = old_enabled
            app_module.WHATSAPP_SERVICE_URL = old_url
            app_module.WHATSAPP_SERVICE_SHARED_SECRET = old_secret
            if old_port is None:
                os.environ.pop('TASKPE_WA_SERVICE_PORT', None)
            else:
                os.environ['TASKPE_WA_SERVICE_PORT'] = old_port


if __name__ == '__main__':
    unittest.main()
