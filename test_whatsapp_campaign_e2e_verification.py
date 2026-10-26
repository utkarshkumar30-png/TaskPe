import unittest
import os
import json
import time
import uuid
import tempfile
import shutil
import hashlib
import sqlite3
import subprocess
import urllib.request
import urllib.error
from unittest.mock import patch, MagicMock

# Isolated test environment setup
_TEST_DIR = tempfile.mkdtemp(prefix='taskpe-wa-e2e-')
_FLASK_DB = os.path.join(_TEST_DIR, 'flask.sqlite3')
_NODE_DB = os.path.join(_TEST_DIR, 'node_ledger.sqlite3')
_ASSET_DIR = os.path.join(_TEST_DIR, 'assets')
_SESSIONS_DIR = os.path.join(_TEST_DIR, 'sessions')
_SECRET = 'super-secret-key-that-is-at-least-32-chars-long'

os.makedirs(_ASSET_DIR, exist_ok=True)
os.makedirs(_SESSIONS_DIR, exist_ok=True)

# Valid test PNG image
_TEST_PNG = bytes.fromhex('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489')
with open(os.path.join(_ASSET_DIR, 'test_promo.png'), 'wb') as f:
    f.write(_TEST_PNG)

os.environ['ENVIRONMENT'] = 'development'
os.environ['TASKPE_DB_PATH'] = _FLASK_DB
os.environ['TASKPE_TASK_ASSET_FOLDER'] = _ASSET_DIR
os.environ['TASKPE_UPLOAD_FOLDER'] = os.path.join(_TEST_DIR, 'uploads')
os.environ['SECRET_KEY'] = 'flask-test-secret-key'
os.environ['TASKPE_WHATSAPP_LINKED_DEVICE_ENABLED'] = '0'
os.environ['TASKPE_TEST_FAKE_WHATSAPP_TRANSPORT'] = '1'
os.environ['TASKPE_WHATSAPP_CONSENT_GATE_ENABLED'] = '1'
os.environ['TASKPE_WA_LEDGER_DB_PATH'] = _NODE_DB
os.environ['TASKPE_WA_SERVICE_SHARED_SECRET'] = _SECRET
os.environ['TASKPE_WA_DAILY_QUOTA_LIMIT'] = '80'
os.environ['TASKPE_WA_CTA_BUTTON_LABEL'] = 'Shop Now'
os.environ['TASKPE_WA_CAMPAIGN_BATCH_HTTP_SEND_ENABLED'] = '0'
os.environ['TASKPE_WA_ALLOW_UNSUPPORTED_FILESYSTEM'] = '1'

import database
database.DB_PATH = _FLASK_DB
database.init_db()
import app as app_module
from werkzeug.security import generate_password_hash


class WhatsAppCampaignE2EVerification(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # This module may be collected after another WhatsApp module has
        # imported app.py. Reapply only the development fake-transport and
        # consent globals; the real linked-device and batch-send environment
        # flags remain disabled.
        cls._old_test_transport_enabled = app_module.WHATSAPP_TEST_TRANSPORT_ENABLED
        cls._old_consent_gate_available = app_module.WHATSAPP_CONSENT_GATE_AVAILABLE
        cls._old_db_path = database.DB_PATH
        app_module.WHATSAPP_TEST_TRANSPORT_ENABLED = True
        app_module.WHATSAPP_CONSENT_GATE_AVAILABLE = True
        database.DB_PATH = _FLASK_DB
        cls.conn = sqlite3.connect(_FLASK_DB)
        cls.conn.row_factory = sqlite3.Row

        # Owner / Worker account
        cls.owner_mobile = '+919296532474'
        cursor = cls.conn.execute('''
            INSERT INTO users (name, email, password_hash, mobile, referral_code, role)
            VALUES (?, ?, ?, ?, ?, 'user')
        ''', ('Utkarsh Kumar (Owner)', 'owner@taskpe.com', generate_password_hash('pass123'), cls.owner_mobile, 'OWNER01'))
        cls.worker_id = cursor.lastrowid

        # Secondary worker for permission testing
        cursor = cls.conn.execute('''
            INSERT INTO users (name, email, password_hash, mobile, referral_code, role)
            VALUES (?, ?, ?, ?, ?, 'user')
        ''', ('Other Worker', 'other@taskpe.com', generate_password_hash('pass123'), '919999999999', 'OTHER01'))
        cls.other_worker_id = cursor.lastrowid

        # Destination HTTPS link
        cls.shop_url = 'https://taskpe.in/shop/deals?promo=diwali2026'

        # Create active text campaign
        cursor = cls.conn.execute('''
            INSERT INTO tasks (
                title, category, task_type, description, payout, template_text,
                destination_link, target_numbers, recipient_region, campaign_id, recipients_per_slot,
                time_limit_minutes, proof_instructions, total_slots, completed_slots, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            'Diwali Mega Offer', 'WhatsApp Tasks', 'TEXT_LINK',
            'Send festive offer to owner test account', 2.50,
            'Diwali Special: Shop the best deals today!', cls.shop_url,
            json.dumps([cls.owner_mobile]), 'IN', 101, 1, 30, 'Screenshot proof', 10, 0, 'active',
        ))
        cls.task_id = cursor.lastrowid
        database.create_whatsapp_campaign_consent_declaration(
            cls.conn, task_id=cls.task_id, campaign_id=101,
            business_user_id=cls.worker_id, phone_numbers=[cls.owner_mobile], region='IN',
        )

        cls.conn.commit()
        cls.client = app_module.app.test_client()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.conn.close()
        finally:
            app_module.WHATSAPP_TEST_TRANSPORT_ENABLED = cls._old_test_transport_enabled
            app_module.WHATSAPP_CONSENT_GATE_AVAILABLE = cls._old_consent_gate_available
            database.DB_PATH = cls._old_db_path
            shutil.rmtree(_TEST_DIR, ignore_errors=True)

    def setUp(self):
        self.conn.execute('DELETE FROM whatsapp_campaign_recipient_claims')
        self.conn.execute('DELETE FROM whatsapp_campaign_plan_recipients')
        self.conn.execute('DELETE FROM whatsapp_campaign_plans')
        self.conn.execute('DELETE FROM recipient_allocations')
        self.conn.execute('DELETE FROM task_assignments')
        self.conn.commit()

    def test_01_cta_button_and_content_resolver(self):
        """Verify server content resolver builds interactive message with 'Shop Now' button and HTTPS destination."""
        node_script = f'''
        import path from 'node:path';
        import assert from 'node:assert';
        import {{ createTaskPeCampaignContentResolver, buildMessageContent }} from './server.js';

        const resolver = createTaskPeCampaignContentResolver({{
            databasePath: {json.dumps(_FLASK_DB)},
            assetRoot: {json.dumps(_ASSET_DIR)},
            buttonLabel: 'Shop Now'
        }});

        const content = await resolver({{
            batch: {{ task_id: {self.task_id}, campaign_id: 101 }},
            item: {{ content_type: 'text', payload_reference: 'template:v1' }}
        }});

        assert.equal(content.text, 'Diwali Special: Shop the best deals today!');
        assert.equal(content.ctaUrl, {json.dumps(self.shop_url)});
        assert.equal(content.buttonLabel, 'Shop Now');

        const message = buildMessageContent(content.type, content);
        assert.ok(message.interactiveMessage, 'Message must be interactive');
        const button = message.interactiveMessage.nativeFlowMessage.buttons[0];
        assert.equal(button.name, 'cta_url');
        const params = JSON.parse(button.buttonParamsJson);
        assert.equal(params.display_text, 'Shop Now');
        assert.equal(params.url, {json.dumps(self.shop_url)});
        console.log('CTA_VERIFIED_OK');
        '''
        proc = subprocess.run(['node', '--input-type=module', '-e', node_script], capture_output=True, text=True, cwd=os.getcwd())
        self.assertEqual(proc.returncode, 0, f"Node resolver failed: {proc.stderr}")
        self.assertIn('CTA_VERIFIED_OK', proc.stdout)

    def test_02_signed_dispatch_authorization_end_to_end(self):
        """Verify token generation, signature verification, and ledger authorization consumption."""
        plan = app_module.create_campaign_send_plan(
            self.conn, self.task_id, self.worker_id, 'test-plan-auth-e2e',
            quantity=1, content_reference=f'task:{self.task_id}:text_link:v1', content_type='text'
        )
        plan_dict = dict(self.conn.execute('SELECT * FROM whatsapp_campaign_plans WHERE id = ?', (plan['plan_id'],)).fetchone())
        
        token = app_module.create_dispatch_authorization_token(
            plan_dict, self.worker_id, _SECRET, operation_id='op-auth-e2e-001'
        )
        self.assertTrue(token.startswith('v1.'))

        # Node verifies valid token
        verify_script = f'''
        import assert from 'node:assert';
        import {{ verifyDispatchAuthorizationToken, deriveDispatchAuthorizationKey }} from './server.js';
        import {{ initializeLedger, createLedgerRepository }} from './whatsapp_ledger.js';

        const token = {json.dumps(token)};
        const secret = {json.dumps(_SECRET)};
        const res = verifyDispatchAuthorizationToken(token, secret);
        assert.ok(res.valid, 'Token should be valid: ' + res.message);
        assert.equal(res.payload.worker_id, {self.worker_id});
        assert.equal(res.payload.batch_id, {json.dumps(plan_dict['batch_id'])});

        // Verify invalid signature rejected
        const tampered = token.slice(0, -4) + '0000';
        const badSig = verifyDispatchAuthorizationToken(tampered, secret);
        assert.strictEqual(badSig.valid, false);

        // Verify wrong secret rejected
        const badSecret = verifyDispatchAuthorizationToken(token, 'wrong-secret-that-is-at-least-32-chars-long');
        assert.strictEqual(badSecret.valid, false);

        // Verify expired token rejected
        const expiredToken = token;
        const expiredCheck = verifyDispatchAuthorizationToken(expiredToken, secret, {{ now: Date.now() + 100000 }});
        assert.strictEqual(expiredCheck.valid, false);
        assert.equal(expiredCheck.statusCode, 401);

        console.log('AUTH_TOKEN_VERIFIED_OK');
        '''
        proc = subprocess.run(['node', '--input-type=module', '-e', verify_script], capture_output=True, text=True, cwd=os.getcwd())
        self.assertEqual(proc.returncode, 0, f"Node auth verification failed: {proc.stderr}")
        self.assertIn('AUTH_TOKEN_VERIFIED_OK', proc.stdout)

    def test_03_controlled_test_campaign_send_submission_to_owner_number(self):
        """Simulate real message submission through connected WhatsApp session addressed to the owner's WhatsApp number."""
        # Clean assignment fixture for owner
        norm_number = app_module.normalize_phone_number(self.owner_mobile)
        cursor = self.conn.execute('''
            INSERT INTO task_assignments (user_id, task_id, campaign_id, campaign_type, assigned_numbers, status)
            VALUES (?, ?, ?, 'WhatsApp Tasks', ?, 'in_progress')
        ''', (self.worker_id, self.task_id, 101, json.dumps([norm_number])))
        assignment_id = cursor.lastrowid

        cursor.execute('''
            INSERT INTO recipient_allocations (campaign_id, phone_number, assignment_id, user_id, last_contacted_at, status)
            VALUES (?, ?, ?, ?, '2026-01-01 00:00:00', 'ALLOCATED')
        ''', (101, norm_number, assignment_id, self.worker_id))
        self.conn.commit()

        # Materialize the exact endpoint idempotency key and prove that this
        # test starts from a fresh, non-recovery dispatch state.
        plan_key = f'assignment-send-{assignment_id}'
        fresh_plan = app_module.create_assignment_send_plan(
            self.conn, assignment_id, self.worker_id, plan_key,
        )
        fresh_row = self.conn.execute(
            'SELECT state, batch_state, dispatch_state FROM whatsapp_campaign_plans WHERE id = ?',
            (fresh_plan['plan_id'],),
        ).fetchone()
        self.assertEqual(fresh_row['state'], 'ready')
        self.assertEqual(fresh_row['batch_state'], 'pending')
        self.assertEqual(fresh_row['dispatch_state'], 'not_started')

        # Mock adapter simulating connected Baileys socket acknowledging send
        captured_messages = []
        operation_order = []
        def mock_node_call(method, path, worker_id, body=None, extra_headers=None):
            operation_order.append((method, path))
            if path.endswith('/quota'):
                return {'success': True, 'worker_id': worker_id, 'quota_day': '2026-10-20', 'limit': 80, 'reserved': 0, 'remaining': 80, 'timezone': 'Asia/Kolkata'}, 200
            if path.endswith('/reserve'):
                data = json.loads(body) if isinstance(body, (str, bytes)) else (body or {})
                items_out = []
                for it in data['items']:
                    key = database.whatsapp_campaign_plan_recipient_idempotency_key(data['planId'], it['planRecipientId'])
                    items_out.append({
                        'position': it['position'],
                        'plan_id': data['planId'],
                        'plan_recipient_id': it['planRecipientId'],
                        'idempotency_key': key,
                        'message_id': key,
                        'state': 'reserved',
                        'recipient_jid': it['recipientJid'],
                        'content_type': it['contentType'],
                        'payload_reference': it['payloadReference'],
                    })
                return {
                    'success': True,
                    'created': True,
                    'duplicate': False,
                    'batch': {
                        'batch_id': data['batchId'],
                        'request_fingerprint': data['requestFingerprint'],
                        'sender_worker_id': data['senderWorkerId'],
                        'campaign_id': data['campaignId'],
                        'task_id': data['taskId'],
                        'plan_id': data['planId'],
                        'item_count': len(data['items']),
                        'state': 'reserved',
                        'items': items_out,
                    }
                }, 200
            if path.endswith('/send'):
                auth = extra_headers.get('X-TaskPe-Dispatch-Authorization')
                assert auth, 'Dispatch authorization must be present'
                captured_messages.append({'recipient': norm_number, 'worker_id': worker_id, 'auth': auth})
                batch_id = path.split('/')[-2]
                plan_row = self.conn.execute('SELECT * FROM whatsapp_campaign_plans WHERE batch_id = ?', (batch_id,)).fetchone()
                recipient_rows = self.conn.execute('SELECT * FROM whatsapp_campaign_plan_recipients WHERE plan_id = ? ORDER BY position', (plan_row['id'],)).fetchall()
                items_send = [{
                    'position': r['position'],
                    'message_id': r['ledger_message_id'],
                    'status': 'acknowledged',
                    'delivered': False,
                } for r in recipient_rows]
                return {'success': True, 'batch_id': batch_id, 'state': 'reserved', 'items': items_send}, 200
            if 'campaign-batches/' in path:
                batch_id = path.split('/')[-1]
                plan_row = self.conn.execute('SELECT * FROM whatsapp_campaign_plans WHERE batch_id = ?', (batch_id,)).fetchone()
                recipient_rows = self.conn.execute('SELECT * FROM whatsapp_campaign_plan_recipients WHERE plan_id = ? ORDER BY position', (plan_row['id'],)).fetchall()
                items_lookup = [{
                    'position': r['position'],
                    'message_id': r['ledger_message_id'],
                    'state': 'acknowledged',
                } for r in recipient_rows]
                return {
                    'success': True,
                    'found': True,
                    'batch': {
                        'batch_id': batch_id,
                        'request_fingerprint': plan_row['batch_request_fingerprint'],
                        'sender_worker_id': worker_id,
                        'campaign_id': plan_row['campaign_id'],
                        'task_id': plan_row['task_id'],
                        'plan_id': plan_row['id'],
                        'item_count': len(recipient_rows),
                    },
                    'items': items_lookup,
                }, 200
            return {'success': False}, 404

        with self.client.session_transaction() as sess:
            sess['user_id'] = self.worker_id
            sess['_csrf_token'] = 'csrf-owner-send'

        with patch.object(app_module, '_call_whatsapp_service', side_effect=mock_node_call):
            response = self.client.post(
                f'/api/tasks/{self.task_id}/assignments/{assignment_id}/whatsapp-send',
                json={},
                headers={'X-CSRF-Token': 'csrf-owner-send'},
            )
        self.assertEqual(response.status_code, 200, f"Send failed: {response.get_json()}")
        data = response.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['status'], 'acknowledged')
        # Crucial acceptance criterion: must NOT claim confirmed delivery
        self.assertFalse(data.get('delivered', True), 'An acknowledged send must never be reported as confirmed delivery!')
        self.assertEqual(len(captured_messages), 1)
        self.assertEqual(captured_messages[0]['recipient'], norm_number)
        send_operations = [path for _, path in operation_order if path.endswith('/send')]
        self.assertEqual(len(send_operations), 1, operation_order)
        self.assertNotIn('lookup', [method for method, _ in operation_order])
        final_plan = self.conn.execute(
            'SELECT dispatch_state, batch_state FROM whatsapp_campaign_plans WHERE id = ?',
            (fresh_plan['plan_id'],),
        ).fetchone()
        self.assertEqual(final_plan['dispatch_state'], 'complete')
        self.assertEqual(final_plan['batch_state'], 'reserved')

    def test_04_uncertain_outcome_cannot_resend_twice(self):
        """Confirm that an uncertain send outcome recovers via status lookup without invoking the send socket twice."""
        cursor = self.conn.execute('''
            INSERT INTO tasks (
                title, category, task_type, description, payout, template_text,
                destination_link, target_numbers, recipient_region, campaign_id, recipients_per_slot,
                time_limit_minutes, proof_instructions, total_slots, completed_slots, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            'Uncertain Test Task', 'WhatsApp Tasks', 'TEXT_LINK',
            'Test recovery without resending', 1.0,
            'Recovery test message', 'https://taskpe.in/shop',
            json.dumps(['+919876599999']), 'IN', 102, 1, 30, 'None', 1, 0, 'active',
        ))
        uncertain_task_id = cursor.lastrowid
        database.create_whatsapp_campaign_consent_declaration(
            self.conn, task_id=uncertain_task_id, campaign_id=102,
            business_user_id=self.worker_id, phone_numbers=['+919876599999'], region='IN',
        )
        self.conn.commit()

        content_ref = f'task:{uncertain_task_id}:text_link:v1'
        with app_module.app.test_request_context('/'):
            from flask import session
            session['user_id'] = self.worker_id
            plan = app_module.create_campaign_send_plan(
                self.conn, uncertain_task_id, self.worker_id, 'test-plan-uncertain-e2e',
                quantity=1, content_reference=content_ref, content_type='text'
            )
            plan_id = plan['plan_id']

            def mock_node_reserve(method, path, worker_id, body=None, extra_headers=None):
                if path.endswith('/quota'):
                    return {'success': True, 'worker_id': worker_id, 'quota_day': '2026-10-20', 'limit': 80, 'reserved': 0, 'remaining': 80, 'timezone': 'Asia/Kolkata'}, 200
                if path.endswith('/reserve'):
                    data = json.loads(body) if isinstance(body, (str, bytes)) else (body or {})
                    items_out = []
                    for it in data['items']:
                        key = database.whatsapp_campaign_plan_recipient_idempotency_key(data['planId'], it['planRecipientId'])
                        items_out.append({
                            'position': it['position'],
                            'plan_id': data['planId'],
                            'plan_recipient_id': it['planRecipientId'],
                            'idempotency_key': key,
                            'message_id': key,
                            'state': 'reserved',
                            'recipient_jid': it['recipientJid'],
                            'content_type': it['contentType'],
                            'payload_reference': it['payloadReference'],
                        })
                    return {
                        'success': True,
                        'created': True,
                        'duplicate': False,
                        'batch': {
                            'batch_id': data['batchId'],
                            'request_fingerprint': data['requestFingerprint'],
                            'sender_worker_id': data['senderWorkerId'],
                            'campaign_id': data['campaignId'],
                            'task_id': data['taskId'],
                            'plan_id': data['planId'],
                            'item_count': len(data['items']),
                            'state': 'reserved',
                            'items': items_out,
                        }
                    }, 200
                return {'success': False}, 404

            with patch.object(app_module, '_call_whatsapp_service', side_effect=mock_node_reserve):
                reserved = app_module.reserve_campaign_send_plan_with_node(self.conn, plan_id)
                self.assertEqual(reserved['status'], 'reserved')

        send_invocations = []
        lookup_invocations = []
        operation_order = []
        plan_row = dict(self.conn.execute('SELECT * FROM whatsapp_campaign_plans WHERE id = ?', (plan_id,)).fetchone())
        recipient_rows = self.conn.execute('SELECT * FROM whatsapp_campaign_plan_recipients WHERE plan_id = ? ORDER BY position', (plan_id,)).fetchall()

        def mock_node_send_call(method, path, worker_id, body=None, extra_headers=None):
            operation_order.append((method, path))
            if path.endswith('/send'):
                send_invocations.append(path)
                raise urllib.error.URLError('Socket timeout during dispatch')
            if 'campaign-batches/' in path and not path.endswith('/send'):
                lookup_invocations.append(path)
                return {
                    'success': True,
                    'found': True,
                    'batch': {
                        'batch_id': plan_row['batch_id'],
                        'request_fingerprint': plan_row['batch_request_fingerprint'],
                        'sender_worker_id': self.worker_id,
                        'campaign_id': 102,
                        'task_id': uncertain_task_id,
                        'plan_id': plan_id,
                        'item_count': 1,
                    },
                    'items': [{
                        'position': 1,
                        'message_id': recipient_rows[0]['ledger_message_id'],
                        'state': 'delivered_unknown',
                    }]
                }, 200
            return {'success': False}, 404

        with patch.object(app_module, '_call_whatsapp_service', side_effect=mock_node_send_call):
            result1 = app_module.send_reserved_whatsapp_campaign_batch(self.conn, plan_id, self.worker_id)
            self.assertEqual(result1['status'], 'recovery_required')
            self.assertTrue(result1['recovery_required'])
            self.assertFalse(result1['delivered'])
            self.assertEqual(len(send_invocations), 1)

            # Retry attempt on uncertain batch MUST NOT invoke /send again
            result2 = app_module.send_reserved_whatsapp_campaign_batch(self.conn, plan_id, self.worker_id)
            self.assertEqual(len(send_invocations), 1, "Send endpoint was invoked again on an uncertain batch!")
            self.assertEqual(len(lookup_invocations), 1, "Recovery must query status lookup without sending!")
            self.assertEqual(
                [path for _, path in operation_order if path.endswith('/send')],
                [send_invocations[0]],
            )
            self.assertEqual(result2['status'], 'recovery_required')
            self.assertFalse(result2['delivered'])


if __name__ == '__main__':
    unittest.main()
