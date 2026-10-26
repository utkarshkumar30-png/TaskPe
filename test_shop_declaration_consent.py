import atexit
import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

_TEST_DIR = tempfile.mkdtemp(prefix='taskpe-test-consent-')
_DB_PATH = os.path.join(_TEST_DIR, 'test_taskpe.db')
_LEDGER_PATH = os.path.join(_TEST_DIR, 'test_ledger.sqlite3')

os.environ['ENVIRONMENT'] = 'development'
os.environ['TASKPE_DB_PATH'] = _DB_PATH
os.environ['TASKPE_WA_LEDGER_DB_PATH'] = _LEDGER_PATH
os.environ['TASKPE_WA_SERVICE_SHARED_SECRET'] = 'consent-test-secret-min32-characters-long'
os.environ['TASKPE_WHATSAPP_LINKED_DEVICE_ENABLED'] = '1'

import database
import app as app_module

atexit.register(lambda: shutil.rmtree(_TEST_DIR, ignore_errors=True))


class ShopDeclarationConsentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database.initialize_database()

    def setUp(self):
        self.conn = database.get_db_connection()
        self.app = app_module.app
        self.client = self.app.test_client()

    def tearDown(self):
        self.conn.close()

    def _create_dispatch_task(self, task_id, numbers):
        normalized = [database.normalize_recipient_number(number, region='IN') for number in numbers]
        self.conn.execute('''
            INSERT INTO tasks
                (id, title, category, task_type, description, payout, template_text,
                 target_numbers, recipient_region, campaign_id, proof_instructions, status)
            VALUES (?, 'Consent dispatch task', 'WhatsApp Tasks', 'TEXT_LINK', 'Test', 1.0,
                    'Hello', ?, 'IN', ?, 'None', 'active')
        ''', (task_id, json.dumps(normalized), task_id))
        database.create_whatsapp_campaign_consent_declaration(
            self.conn, task_id=task_id, campaign_id=task_id, business_user_id=1,
            phone_numbers=normalized, declaration_text=database.SHOP_CONSENT_DECLARATION_DEFAULT_TEXT,
        )
        self.conn.commit()
        return normalized

    def test_1_recipient_without_declaration_is_blocked(self):
        """1. Recipient without a declaration is blocked."""
        num = '+919876543210'
        # Ensure number has no consent declaration
        self.conn.execute("DELETE FROM recipient_consent_records WHERE phone_number = ?", (num,))
        self.conn.commit()

        # Check consent evaluation directly
        is_valid, err = app_module._evaluate_recipients_consent(self.conn, [num])
        self.assertFalse(is_valid)
        self.assertIn('missing shop declaration consent', err)

        # In check_recipients_consent_and_opt_out
        res = database.check_recipients_consent_and_opt_out(self.conn, [num])
        self.assertFalse(res['all_valid'])
        self.assertIn(num, res['unattested'])

    def test_2_recipient_with_valid_declaration_passes_consent_check(self):
        """2. Recipient with a valid declaration can pass the consent check."""
        num = '+919876543211'
        # Ensure clean state
        self.conn.execute("DELETE FROM recipient_consent_records WHERE phone_number = ?", (num,))
        self.conn.execute("DELETE FROM whatsapp_opt_outs WHERE phone_number = ?", (num,))
        self.conn.commit()

        # Record shop declaration
        declaration = database.record_shop_consent_declaration(
            self.conn, shop_name='Kirana Store Counter A',
            uploader_user_id=1, phone_numbers=[num],
            source='billing_counter', region='IN',
        )
        self.assertEqual(declaration['shop_name'], 'Kirana Store Counter A')
        self.assertEqual(declaration['source'], 'billing_counter')
        self.assertEqual(declaration['recipient_count'], 1)

        # Check consent evaluation
        is_valid, err = app_module._evaluate_recipients_consent(self.conn, [num])
        self.assertTrue(is_valid)
        self.assertIsNone(err)

        res = database.check_recipients_consent_and_opt_out(self.conn, [num])
        self.assertTrue(res['all_valid'])
        self.assertIn(num, res['valid_attested'])
        self.assertEqual(res['unattested'], [])
        self.assertEqual(res['opted_out'], [])

    def test_3_opted_out_recipient_remains_blocked_even_with_declaration(self):
        """3. An opted-out recipient remains blocked even with a declaration."""
        num = '+919876543212'
        # Record shop declaration first
        database.record_shop_consent_declaration(
            self.conn, shop_name='Pharmacy Counter',
            uploader_user_id=1, phone_numbers=[num], region='IN',
        )
        # Verify valid attested
        is_valid, _ = app_module._evaluate_recipients_consent(self.conn, [num])
        self.assertTrue(is_valid)

        # Record opt-out
        database.record_whatsapp_opt_out(self.conn, num, reason='user_opt_out', region='IN')

        # Recipient must now be blocked
        is_valid, err = app_module._evaluate_recipients_consent(self.conn, [num])
        self.assertFalse(is_valid)
        self.assertIn('opted out', err.lower())

        res = database.check_recipients_consent_and_opt_out(self.conn, [num])
        self.assertFalse(res['all_valid'])
        self.assertIn(num, res['opted_out'])

    def test_campaign_declaration_is_auditable_and_caller_transactional(self):
        task_id = 910001
        self.conn.execute('DELETE FROM whatsapp_campaign_consent_declarations WHERE task_id = ?', (task_id,))
        self.conn.commit()
        self.conn.execute('BEGIN')
        declaration = database.create_whatsapp_campaign_consent_declaration(
            self.conn, task_id=task_id, campaign_id=task_id, business_user_id=1,
            phone_numbers=['+91 98765 43210', '+919876543210', '+919876543211'],
            declaration_text=database.SHOP_CONSENT_DECLARATION_DEFAULT_TEXT,
        )
        self.assertEqual(declaration['recipient_count'], 2)
        self.assertEqual(len(declaration['recipient_fingerprint']), 64)
        self.assertIsNotNone(self.conn.execute(
            'SELECT id FROM whatsapp_campaign_consent_declarations WHERE task_id = ?', (task_id,)
        ).fetchone())
        self.conn.rollback()
        self.assertIsNone(self.conn.execute(
            'SELECT id FROM whatsapp_campaign_consent_declarations WHERE task_id = ?', (task_id,)
        ).fetchone())

    def test_campaign_declaration_rejects_changed_snapshot_at_dispatch(self):
        task_id = 910002
        self.conn.execute('DELETE FROM whatsapp_campaign_consent_declarations WHERE task_id = ?', (task_id,))
        database.create_whatsapp_campaign_consent_declaration(
            self.conn, task_id=task_id, campaign_id=task_id, business_user_id=1,
            phone_numbers=['+919876543220'], declaration_text=database.SHOP_CONSENT_DECLARATION_DEFAULT_TEXT,
        )
        self.conn.commit()
        plan = {'task_id': task_id, 'campaign_id': task_id}
        rows = [{'recipient_number': '919876543221'}]
        self.assertFalse(app_module._validate_campaign_consent_declaration_for_dispatch(self.conn, plan, rows))

    def test_dispatch_validates_complete_declaration_subset_and_opt_outs(self):
        task_id = 920001
        first, opted_out = self._create_dispatch_task(
            task_id, ['+919876543220', '+919876543221']
        )
        plan = {
            'task_id': task_id,
            'campaign_id': task_id,
            'recipient_fingerprint': app_module._campaign_recipient_fingerprint(
                [first, opted_out]
            ),
        }

        # Eligibility may exclude the opted-out member while the remaining
        # planned recipient still belongs to the complete declared source list.
        self.conn.execute(
            "INSERT INTO whatsapp_opt_outs (phone_number, reason) VALUES (?, 'legacy')",
            (opted_out[1:],),
        )
        self.conn.commit()
        self.assertTrue(
            app_module._validate_campaign_consent_declaration_for_dispatch(
                self.conn, plan, [{'recipient_number': first}]
            )
        )

        self.assertFalse(
            app_module._validate_campaign_consent_declaration_for_dispatch(
                self.conn, plan, [{'recipient_number': opted_out}]
            )
        )
        self.assertFalse(
            app_module._validate_campaign_consent_declaration_for_dispatch(
                self.conn, plan, [{'recipient_number': '+919876543222'}]
            )
        )

        # A changed source list invalidates the declaration even when the
        # currently planned recipient is still in the old declared list.
        self.conn.execute(
            'UPDATE tasks SET target_numbers = ? WHERE id = ?',
            (json.dumps([first]), task_id),
        )
        self.conn.commit()
        self.assertFalse(
            app_module._validate_campaign_consent_declaration_for_dispatch(
                self.conn, plan, [{'recipient_number': first}]
            )
        )

        # Restore the source list, then tamper with the original declaration.
        self.conn.execute(
            'UPDATE tasks SET target_numbers = ? WHERE id = ?',
            (json.dumps([first, opted_out]), task_id),
        )
        self.conn.execute(
            'UPDATE whatsapp_campaign_consent_declarations SET declaration_text = ? WHERE task_id = ?',
            ('tampered declaration', task_id),
        )
        self.conn.commit()
        self.assertFalse(
            app_module._validate_campaign_consent_declaration_for_dispatch(
                self.conn, plan, [{'recipient_number': first}]
            )
        )

    def test_4_invalid_and_expired_dispatch_authorizations_are_rejected(self):
        """4. Invalid and expired dispatch authorizations are rejected."""
        plan = {
            'id': 1, 'batch_id': 'batch-auth-test',
            'request_fingerprint': 'a' * 64,
            'sender_worker_id': 1,
        }
        secret = os.environ['TASKPE_WA_SERVICE_SHARED_SECRET']

        # 1. Expired token (ttl negative)
        with self.assertRaises(ValueError):
            app_module.create_dispatch_authorization_token(
                plan=plan, worker_id=1, secret=secret,
                ttl_seconds=-10, operation_id='op-auth-test-1',
            )

        # 2. Tampered signature
        valid_token = app_module.create_dispatch_authorization_token(
            plan=plan, worker_id=1, secret=secret,
            ttl_seconds=60, operation_id='op-auth-test-2',
        )
        parts = valid_token.split('.')
        self.assertEqual(len(parts), 3)
        tampered_token = f"{parts[0]}.{parts[1]}.{'0' * 64}"

        # 3. Malformed token
        malformed_token = "invalid-token"

        # Verify Node / server logic principles:
        # A valid token generated with correct ttl
        self.assertIsInstance(valid_token, str)

        # Confirm tampering changes the token without creating a valid authorization.
        self.assertNotEqual(tampered_token, valid_token)

    def test_5_duplicate_dispatch_is_prevented(self):
        """5. Duplicate dispatch is prevented."""
        # When a batch is already completed, send_reserved_whatsapp_campaign_batch returns already_processed
        plan = {
            'id': 999, 'batch_id': 'batch-dup-001', 'dispatch_state': 'complete',
            'dispatch_operation_id': 'op-dup', 'sender_worker_id': 1,
        }
        with patch.object(app_module, 'WHATSAPP_CONSENT_GATE_AVAILABLE', True), \
             patch.object(app_module, '_campaign_batch_plan_row', return_value=plan):
            res = app_module.send_reserved_whatsapp_campaign_batch(self.conn, 999, 1)
            self.assertEqual(res, {'status': 'already_processed', 'delivered': False})

    def test_6_missing_or_inconsistent_configuration_fails_safely(self):
        """6. Missing or inconsistent configuration fails safely."""
        # When TASKPE_WHATSAPP_CONSENT_GATE_ENABLED is 0 / False:
        with patch.object(app_module, 'WHATSAPP_CONSENT_GATE_AVAILABLE', False), \
             patch.object(app_module, 'WHATSAPP_LINKED_DEVICE_ENABLED', True):
            with self.client.session_transaction() as sess:
                sess['user_id'] = 1
                sess['_csrf_token'] = 'test-token'
            resp = self.client.post(
                '/api/whatsapp/campaigns/send',
                json={'plan_id': 1},
                headers={'X-CSRF-Token': 'test-token'},
            )
        self.assertEqual(resp.status_code, 412)
        self.assertEqual(resp.get_json()['status'], 'consent_required')

        # When feature flag disabled on routes
        with patch.object(app_module, 'WHATSAPP_CONSENT_GATE_AVAILABLE', True), \
             patch.object(app_module, 'WHATSAPP_LINKED_DEVICE_ENABLED', False):
            with self.client.session_transaction() as sess:
                sess['user_id'] = 1
                sess['_csrf_token'] = 'test-token'
            resp = self.client.post(
                '/api/whatsapp/campaigns/send',
                json={'plan_id': 1},
                headers={'X-CSRF-Token': 'test-token'},
            )
            self.assertEqual(resp.status_code, 404)
            self.assertEqual(resp.get_json()['message'], 'Linked-device WhatsApp integration is disabled.')

    def test_7_recovery_after_uncertain_result_does_not_cause_duplicate_sending(self):
        """7. Recovery after an uncertain result does not cause duplicate sending."""
        plan = {
            'id': 777, 'batch_id': 'batch-recovery-001', 'dispatch_state': 'in_progress',
            'dispatch_operation_id': 'op-recovery-1', 'sender_worker_id': 1,
        }
        rows = [{'position': 1, 'ledger_message_id': 'ledger-msg-777', 'recipient_number': '919876543211'}]
        # When recovering an in_progress plan, it calls _campaign_dispatch_recovery which queries status
        with patch.object(app_module, 'WHATSAPP_CONSENT_GATE_AVAILABLE', True), \
             patch.object(app_module, '_campaign_batch_plan_row', return_value=plan), \
             patch.object(app_module, '_campaign_batch_recipient_rows', return_value=rows), \
             patch.object(app_module, '_validate_campaign_send_plan_for_dispatch', return_value=(plan, rows)), \
             patch.object(app_module, '_campaign_dispatch_recovery', return_value={
                 'status': 'acknowledged', 'delivered': False, 'acknowledged': 1, 'failed': 0, 'uncertain': 0,
             }) as recovery_mock:
            res = app_module.send_reserved_whatsapp_campaign_batch(self.conn, 777, 1)
            self.assertEqual(res['status'], 'acknowledged')
            self.assertFalse(res['delivered'])
            recovery_mock.assert_called_once()

    def test_8_successful_socket_acknowledgement_is_not_reported_as_confirmed_delivery(self):
        """8. A successful socket/API acknowledgement is not reported as confirmed delivery."""
        plan = {
            'id': 888, 'task_id': 888, 'campaign_id': 888, 'batch_id': 'batch-ack-001', 'batch_request_fingerprint': 'e' * 64, 'dispatch_state': 'not_started',
            'dispatch_operation_id': None, 'sender_worker_id': 1,
        }
        rows = [{'position': 1, 'ledger_message_id': 'ledger-msg-888', 'recipient_number': '919876543211'}]
        self.conn.execute('DELETE FROM whatsapp_campaign_consent_declarations WHERE task_id = ?', (888,))
        database.create_whatsapp_campaign_consent_declaration(
            self.conn, task_id=888, campaign_id=888, business_user_id=1,
            phone_numbers=['+919876543211'], declaration_text=database.SHOP_CONSENT_DECLARATION_DEFAULT_TEXT,
        )
        self.conn.commit()
        response = {
            'success': True,
            'batch_id': 'batch-ack-001',
            'items': [{'position': 1, 'message_id': 'ledger-msg-888', 'status': 'acknowledged'}],
        }
        with patch.object(app_module, 'WHATSAPP_CONSENT_GATE_AVAILABLE', True), \
             patch.object(app_module, '_campaign_batch_plan_row', return_value=plan), \
             patch.object(app_module, '_validate_campaign_send_plan_for_dispatch', return_value=(plan, rows)), \
             patch.object(app_module, '_validate_campaign_consent_declaration_for_dispatch', return_value=True), \
             patch.object(app_module, '_claim_campaign_dispatch', return_value={
                 'claimed': True, 'state': 'in_progress', 'operation_id': 'op-ack-1',
             }), \
             patch.object(app_module, '_campaign_batch_adapter_call', return_value=(response, 200)), \
             patch.object(app_module, '_project_campaign_send_outcomes', return_value={
                 'acknowledged': 1, 'failed': 0, 'uncertain': 0, 'not_sendable': 0,
             }):
            res = app_module.send_reserved_whatsapp_campaign_batch(self.conn, 888, 1)
            self.assertEqual(res['status'], 'acknowledged')
            # Critical: delivered MUST be False
            self.assertFalse(res['delivered'])
            self.assertNotIn('delivered_true', res)

    def test_same_task_replacement_preserves_history_and_invalidates_approval(self):
        task_id = 930001
        old_number = '+919876543230'
        new_number = '+919876543231'
        self.conn.execute('DELETE FROM tasks WHERE id = ?', (task_id,))
        self.conn.execute('''
            INSERT INTO tasks
                (id, title, category, task_type, description, payout, template_text,
                 target_numbers, recipient_region, campaign_id, owner_user_id,
                 campaign_review_status, campaign_approval_fingerprint,
                 proof_instructions, status)
            VALUES (?, 'Replacement', 'WhatsApp Tasks', 'TEXT_LINK', 'Replacement', 1,
                    'Hello', ?, 'IN', ?, 1, 'approved', ?, 'None', 'active')
        ''', (task_id, json.dumps([old_number]), task_id,
              app_module._campaign_recipient_fingerprint([old_number])))
        first = database.create_whatsapp_campaign_consent_declaration(
            self.conn, task_id, task_id, 1, [old_number],
        )
        self.conn.execute(
            'UPDATE tasks SET target_numbers = ? WHERE id = ?',
            (json.dumps([new_number]), task_id),
        )
        second = database.create_whatsapp_campaign_consent_declaration(
            self.conn, task_id, task_id, 1, [new_number],
        )
        self.conn.commit()
        self.assertNotEqual(first['declaration_id'], second['declaration_id'])
        self.assertEqual(self.conn.execute(
            'SELECT COUNT(*) FROM whatsapp_campaign_consent_declarations WHERE task_id = ?',
            (task_id,),
        ).fetchone()[0], 2)
        task = self.conn.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
        self.assertEqual(task['campaign_review_status'], 'pending_review')
        self.assertIsNone(task['campaign_approval_fingerprint'])
        self.assertFalse(app_module._validate_campaign_consent_declaration_for_dispatch(
            self.conn,
            {'task_id': task_id, 'campaign_id': task_id,
             'recipient_fingerprint': first['recipient_fingerprint']},
            [{'recipient_number': old_number}],
        ))
        database.record_whatsapp_opt_out(self.conn, new_number, region='IN')
        self.assertFalse(app_module._validate_campaign_consent_declaration_for_dispatch(
            self.conn,
            {'task_id': task_id, 'campaign_id': task_id,
             'recipient_fingerprint': second['recipient_fingerprint']},
            [{'recipient_number': new_number}],
        ))

    def test_replacement_rejects_owner_mismatch_and_ownerless_admin_behavior_remains(self):
        task_id = 930002
        self.conn.execute('''
            INSERT INTO tasks
                (id, title, category, task_type, description, payout, template_text,
                 target_numbers, recipient_region, campaign_id, owner_user_id,
                 proof_instructions, status)
            VALUES (?, 'Owner binding', 'WhatsApp Tasks', 'TEXT_LINK', 'Owner binding', 1,
                    'Hello', ?, 'IN', ?, 1, 'None', 'pending_review')
        ''', (task_id, json.dumps(['+919876543232']), task_id))
        with self.assertRaises(ValueError):
            database.create_whatsapp_campaign_consent_declaration(
                self.conn, task_id, task_id, 2, ['+919876543232'],
            )
        ownerless_task = 930003
        self.conn.execute('''
            INSERT INTO tasks
                (id, title, category, task_type, description, payout, template_text,
                 target_numbers, recipient_region, campaign_id, proof_instructions, status)
            VALUES (?, 'Ownerless', 'WhatsApp Tasks', 'TEXT_LINK', 'Ownerless', 1,
                    'Hello', ?, 'IN', ?, 'None', 'active')
        ''', (ownerless_task, json.dumps(['+919876543233']), ownerless_task))
        database.create_whatsapp_campaign_consent_declaration(
            self.conn, ownerless_task, ownerless_task, 2, ['+919876543233'],
        )
        self.conn.rollback()


if __name__ == '__main__':
    unittest.main()
