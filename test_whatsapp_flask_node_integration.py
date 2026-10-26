import atexit
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from unittest.mock import patch


# Keep this harness isolated from the repository database.  These are process-local
# test settings; they do not alter .env or any persistent environment configuration.
_TEST_ROOT = tempfile.mkdtemp(prefix='taskpe-wa-integration-', dir=os.getcwd())
_FLASK_DB = os.path.join(_TEST_ROOT, 'flask.sqlite3')
_NODE_DB = os.path.join(_TEST_ROOT, 'node.sqlite3')
_NODE_SESSIONS = os.path.join(_TEST_ROOT, 'sessions')
_SECRET = 'integration-test-secret-' + ('x' * 32)
_NODE_PROCESSES = []
os.environ['ENVIRONMENT'] = 'development'
os.environ['TASKPE_DB_PATH'] = _FLASK_DB
os.environ['SECRET_KEY'] = 'integration-flask-secret'
os.environ['SMS_PROVIDER'] = 'disabled'
os.environ['TASKPE_WHATSAPP_LINKED_DEVICE_ENABLED'] = '0'
os.environ['TASKPE_WA_LEDGER_DB_PATH'] = _NODE_DB
os.environ['TASKPE_WA_SERVICE_SHARED_SECRET'] = _SECRET
os.environ['TASKPE_WA_DAILY_QUOTA_LIMIT'] = '80'
# Keep this integration harness on the disabled-send contract even when the
# test runner imports another module that exercises the opt-in path.
os.environ['TASKPE_WA_CAMPAIGN_BATCH_HTTP_SEND_ENABLED'] = '0'

import database
database.DB_PATH = _FLASK_DB
import app as app_module
from flask import session


_NODE_BOOTSTRAP = r'''
import http from 'node:http';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
const root = process.env.TASKPE_REPO;
const { createService } = await import(pathToFileURL(path.join(root, 'server.js')).href);
const ledger = await import(pathToFileURL(path.join(root, 'whatsapp_ledger.js')).href);
const db = ledger.initializeLedger(process.env.TASKPE_WA_LEDGER_DB_PATH);
const repository = ledger.createLedgerRepository(db);
const service = createService({
  enabled: true,
  secret: process.env.TASKPE_WA_SERVICE_SHARED_SECRET,
  sessionRoot: process.env.TASKPE_WA_SESSION_ROOT,
  ledgerRepository: repository,
  loadBaileys: false,
  allowUnsupportedFilesystem: true,
});
const server = http.createServer((req, res) =>
  service.handle(req, res).catch(() => {
    res.statusCode = 500;
    res.end(JSON.stringify({ success: false, message: 'Internal service error.' }));
  })
);
server.requestTimeout = 10000;
server.headersTimeout = 5000;
server.listen(0, '127.0.0.1', () => {
  process.stdout.write(JSON.stringify({ ready: true, port: server.address().port }) + '\n');
});
let stopping = false;
async function stop() {
  if (stopping) return;
  stopping = true;
  await service.closeAll();
  ledger.closeLedger(db);
  await new Promise(resolve => server.close(resolve));
  process.exit(0);
}
process.on('SIGTERM', stop);
process.on('SIGINT', stop);
'''


def _remove_test_root():
    for process in list(_NODE_PROCESSES):
        _stop_node_process(process)
    shutil.rmtree(_TEST_ROOT, ignore_errors=True)


atexit.register(_remove_test_root)


def _stop_node_process(process):
    if process is None or process.poll() is not None:
        if process in _NODE_PROCESSES:
            _NODE_PROCESSES.remove(process)
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    if process in _NODE_PROCESSES:
        _NODE_PROCESSES.remove(process)


class FlaskNodeIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved_db_path = database.DB_PATH
        cls._saved_env = {
            key: os.environ.get(key)
            for key in ('TASKPE_DB_PATH', 'ENVIRONMENT')
        }
        database.DB_PATH = _FLASK_DB
        os.environ['TASKPE_WA_CAMPAIGN_BATCH_HTTP_SEND_ENABLED'] = '0'
        cls._old_service_url = app_module.WHATSAPP_SERVICE_URL
        cls._old_linked_device_enabled = app_module.WHATSAPP_LINKED_DEVICE_ENABLED
        cls._old_consent_gate_available = app_module.WHATSAPP_CONSENT_GATE_AVAILABLE
        cls._old_service_secret = app_module.WHATSAPP_SERVICE_SHARED_SECRET
        cls._old_service_timeout = app_module.WHATSAPP_SERVICE_TIMEOUT_SECONDS
        cls._old_service_port = os.environ.get('TASKPE_WA_SERVICE_PORT')
        app_module.WHATSAPP_CONSENT_GATE_AVAILABLE = False
        database.init_db()
        cls.conn = database.get_db_connection()
        worker = cls.conn.execute(
            "SELECT id FROM users WHERE role != 'admin' ORDER BY id LIMIT 1"
        ).fetchone()
        if not worker:
            raise RuntimeError('The isolated database did not create a test worker.')
        cls.worker_id = worker['id']
        cls.conn.execute('''
            INSERT INTO tasks
                (title, category, task_type, description, payout, template_text,
                 target_numbers, recipient_region, campaign_id, recipients_per_slot, time_limit_minutes,
                 proof_instructions, total_slots, completed_slots, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            'HTTP integration test campaign', 'Research', 'TEXT_LINK',
            'Isolated integration test task', 1.0, 'Test message',
            json.dumps([f'+9198765432{i + 10:02d}' for i in range(2)]), 'IN',
            99001, 25, 25, 'No live proof', 1, 0, 'active',
        ))
        cls.task_id = cls.conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        database.create_whatsapp_campaign_consent_declaration(
            cls.conn, task_id=cls.task_id, campaign_id=99001,
            business_user_id=cls.worker_id,
            phone_numbers=[f'+9198765432{i + 10:02d}' for i in range(2)],
            declaration_text=database.SHOP_CONSENT_DECLARATION_DEFAULT_TEXT,
            region='IN',
        )

        other_worker = cls.conn.execute(
            "SELECT id FROM users WHERE id != ? AND role != 'admin' LIMIT 1", (cls.worker_id,)
        ).fetchone()
        if not other_worker:
            cls.conn.execute(
                "INSERT INTO users (name, mobile, email, password_hash, role) VALUES ('Worker 2', '919876543299', 'w2@test.local', 'hash', 'worker')"
            )
            cls.other_worker_id = cls.conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        else:
            cls.other_worker_id = other_worker['id']

        cls.conn.execute('''
            INSERT INTO tasks
                (title, category, task_type, description, payout, template_text,
                 target_numbers, recipient_region, campaign_id, recipients_per_slot, time_limit_minutes,
                 proof_instructions, total_slots, completed_slots, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            'Video test task', 'Research', 'VIDEO_TEXT_LINK',
            'Video description', 1.0, 'Video template text',
            json.dumps(['+919876543220', '+919876543221']), 'IN',
            99003, 2, 25, 'No live proof', 1, 0, 'active',
        ))
        cls.video_task_id = cls.conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        database.create_whatsapp_campaign_consent_declaration(
            cls.conn, task_id=cls.video_task_id, campaign_id=99003,
            business_user_id=cls.worker_id,
            phone_numbers=['+919876543220', '+919876543221'],
            declaration_text=database.SHOP_CONSENT_DECLARATION_DEFAULT_TEXT,
            region='IN',
        )
        cls.conn.commit()

        cls.node = cls._start_node()
        app_module.WHATSAPP_LINKED_DEVICE_ENABLED = True
        app_module.WHATSAPP_SERVICE_SHARED_SECRET = _SECRET
        app_module.WHATSAPP_SERVICE_TIMEOUT_SECONDS = 5
        app_module.WHATSAPP_SERVICE_URL = 'http://127.0.0.1:{}'.format(cls.node['port'])
        os.environ['TASKPE_WA_SERVICE_PORT'] = str(cls.node['port'])
        cls.client = app_module.app.test_client()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.conn.close()
        finally:
            if getattr(cls, 'node_process', None):
                _stop_node_process(cls.node_process)
            app_module.WHATSAPP_SERVICE_URL = cls._old_service_url
            app_module.WHATSAPP_LINKED_DEVICE_ENABLED = cls._old_linked_device_enabled
            app_module.WHATSAPP_CONSENT_GATE_AVAILABLE = cls._old_consent_gate_available
            app_module.WHATSAPP_SERVICE_SHARED_SECRET = cls._old_service_secret
            app_module.WHATSAPP_SERVICE_TIMEOUT_SECONDS = cls._old_service_timeout
            database.DB_PATH = cls._saved_db_path
            for key, value in cls._saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            if cls._old_service_port is None:
                os.environ.pop('TASKPE_WA_SERVICE_PORT', None)
            else:
                os.environ['TASKPE_WA_SERVICE_PORT'] = cls._old_service_port
            _remove_test_root()

    @classmethod
    def _start_node(cls):
        env = os.environ.copy()
        env.update({
            'TASKPE_REPO': os.getcwd(),
            'TASKPE_WA_LEDGER_DB_PATH': _NODE_DB,
            'TASKPE_WA_SESSION_ROOT': _NODE_SESSIONS,
            'TASKPE_WA_SERVICE_SHARED_SECRET': _SECRET,
            'TASKPE_WHATSAPP_LINKED_DEVICE_ENABLED': '0',
            'TASKPE_WA_DAILY_QUOTA_LIMIT': '80',
        })
        cls.node_process = subprocess.Popen(
            ['node', '--input-type=module', '-e', _NODE_BOOTSTRAP],
            cwd=os.getcwd(), env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        _NODE_PROCESSES.append(cls.node_process)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            line = cls.node_process.stdout.readline()
            if line:
                try:
                    ready = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ready.get('ready'):
                    return ready
            if cls.node_process.poll() is not None:
                stderr = cls.node_process.stderr.read()
                raise RuntimeError(
                    f'Node harness exited with {cls.node_process.returncode}: {stderr}'
                )
        _stop_node_process(cls.node_process)
        raise RuntimeError('Timed out waiting for the isolated Node harness.')

    def setUp(self):
        self.client = app_module.app.test_client()
        self.conn.execute('DELETE FROM whatsapp_campaign_plan_recipients')
        self.conn.execute('DELETE FROM whatsapp_campaign_recipient_claims')
        self.conn.execute('DELETE FROM whatsapp_campaign_plans')
        self.conn.execute('DELETE FROM recipient_allocations')
        self.conn.execute('DELETE FROM task_assignments')
        self.conn.commit()

    def _plan(self, key=None, content_type='text', task_id=None):
        key = key or f'http-plan-{uuid.uuid4().hex}'
        task_id = task_id or self.task_id
        content_slug = 'text_link' if content_type == 'text' else content_type
        content_ref = f'task:{task_id}:{content_slug}:v1'
        with app_module.app.test_request_context('/'):
            session['user_id'] = self.worker_id
            return app_module.create_campaign_send_plan(
                self.conn, task_id, self.worker_id, key, 2,
                content_ref, content_type,
            )

    def _reserve(self, plan_id, content_type='text'):
        with app_module.app.test_request_context('/'):
            session['user_id'] = self.worker_id
            return app_module.reserve_campaign_send_plan_with_node(
                self.conn, plan_id, now='2026-10-14 10:00:00', content_type=content_type
            )

    def _batch_payload(self, plan_id):
        plan = self.conn.execute(
            'SELECT * FROM whatsapp_campaign_plans WHERE id = ?', (plan_id,)
        ).fetchone()
        rows = self.conn.execute('''
            SELECT id, recipient_number, position
            FROM whatsapp_campaign_plan_recipients WHERE plan_id = ? ORDER BY position
        ''', (plan_id,)).fetchall()
        return {
            'batchId': plan['batch_id'],
            'requestFingerprint': plan['batch_request_fingerprint'],
            'senderWorkerId': self.worker_id,
            'campaignId': plan['campaign_id'], 'taskId': plan['task_id'],
            'planId': plan_id,
            'items': [{
                'position': row['position'], 'planRecipientId': row['id'],
                'recipientJid': f"{row['recipient_number'].lstrip('+')}@s.whatsapp.net",
                'contentType': plan['batch_content_type'],
                'payloadReference': plan['content_reference'],
            } for row in rows],
        }

    def _node_call(self, method, path, body=None, extra_headers=None, user_id=None):
        payload = '' if body is None else json.dumps(body, separators=(',', ':'))
        timestamp = str(int(time.time()))
        request_id = uuid.uuid4().hex
        canonical = f'{timestamp}.{request_id}.{method}.{path}.{payload}'
        signature = hmac.new(_SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()
        uid = str(self.worker_id if user_id is None else user_id)
        headers = {
            'Content-Type': 'application/json',
            'X-TaskPe-Timestamp': timestamp,
            'X-TaskPe-Request-ID': request_id,
            'X-TaskPe-Signature': signature,
            'X-TaskPe-User-ID': uid,
            **(extra_headers or {}),
        }
        data = payload.encode('utf-8') if payload else (b'' if method == 'POST' else None)
        request = urllib.request.Request(
            app_module.WHATSAPP_SERVICE_URL + path,
            data=data,
            method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_success_and_idempotent_retry_cross_real_http_boundary(self):
        plan = self._plan()
        result = self._reserve(plan['plan_id'])
        self.assertEqual(result['status'], 'reserved')
        node = __import__('sqlite3').connect(_NODE_DB)
        before_rows = node.execute(
            'SELECT COUNT(*) FROM outgoing_messages'
        ).fetchone()[0]
        before_reserved = node.execute(
            'SELECT COALESCE(SUM(reserved_count), 0) FROM daily_message_quota'
        ).fetchone()[0]
        status, duplicate = self._node_call(
            'POST', '/v1/users/%s/campaign-batches/reserve' % self.worker_id,
            self._batch_payload(plan['plan_id']),
        )
        self.assertEqual(status, 200)
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(node.execute(
            'SELECT COUNT(*) FROM outgoing_messages'
        ).fetchone()[0], before_rows)
        self.assertEqual(node.execute(
            'SELECT COALESCE(SUM(reserved_count), 0) FROM daily_message_quota'
        ).fetchone()[0], before_reserved)
        self.assertEqual(node.execute(
            'SELECT COUNT(*) FROM outgoing_messages WHERE batch_id = ?',
            (plan['batch_id'],),
        ).fetchone()[0], 2)
        node.close()

    def test_node_commit_then_flask_recovery_projects_same_batch(self):
        plan = self._plan()
        status, response = self._node_call(
            'POST', '/v1/users/%s/campaign-batches/reserve' % self.worker_id,
            self._batch_payload(plan['plan_id']),
        )
        self.assertEqual(status, 200)
        self.assertTrue(response['success'])
        self.conn.execute('''
            UPDATE whatsapp_campaign_plans SET batch_state = 'reserving' WHERE id = ?
        ''', (plan['plan_id'],))
        self.conn.execute('''
            UPDATE whatsapp_campaign_plan_recipients
            SET state = 'reserving', reservation_claim_id = 'lost-response-claim'
            WHERE plan_id = ?
        ''', (plan['plan_id'],))
        self.conn.commit()
        recovered = self._reserve(plan['plan_id'])
        self.assertEqual(recovered['status'], 'reserved')
        self.assertEqual(recovered['batch_id'], plan['batch_id'])
        self.assertEqual(
            self.conn.execute('''
                SELECT COUNT(*) FROM whatsapp_campaign_plan_recipients
                WHERE plan_id = ? AND state = 'reserved' AND ledger_message_id IS NOT NULL
            ''', (plan['plan_id'],)).fetchone()[0], 2,
        )

    def test_node_unavailable_is_unknown_and_claims_remain_held(self):
        plan = self._plan()
        original_url = app_module.WHATSAPP_SERVICE_URL
        original_port = os.environ.get('TASKPE_WA_SERVICE_PORT')
        app_module.WHATSAPP_SERVICE_URL = 'http://127.0.0.1:1'
        os.environ['TASKPE_WA_SERVICE_PORT'] = '1'
        try:
            result = self._reserve(plan['plan_id'])
        finally:
            app_module.WHATSAPP_SERVICE_URL = original_url
            if original_port is not None:
                os.environ['TASKPE_WA_SERVICE_PORT'] = original_port
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(
            self.conn.execute('''
                SELECT COUNT(*) FROM whatsapp_campaign_recipient_claims
                WHERE plan_id = ? AND state = 'claimed'
            ''', (plan['plan_id'],)).fetchone()[0], 2,
        )

    def test_committed_batch_blocks_cancellation_and_preserves_claims(self):
        plan = self._plan()
        status, response = self._node_call(
            'POST', '/v1/users/%s/campaign-batches/reserve' % self.worker_id,
            self._batch_payload(plan['plan_id']),
        )
        self.assertEqual(status, 200)
        self.assertTrue(response['success'])
        with app_module.app.test_request_context('/'):
            session['user_id'] = self.worker_id
            result = app_module.cancel_campaign_send_plan(
                self.conn, plan['plan_id'], now='2026-10-14 10:00:00'
            )
        self.assertEqual(result['status'], 'cancellation_blocked')
        self.assertEqual(
            self.conn.execute('''
                SELECT COUNT(*) FROM whatsapp_campaign_recipient_claims
                WHERE plan_id = ? AND state = 'claimed'
            ''', (plan['plan_id'],)).fetchone()[0], 2,
        )

    def test_cancellation_during_delayed_response_after_node_commit_retains_claims(self):
        plan = self._plan()
        with tempfile.TemporaryDirectory(prefix='taskpe-wa-race-', dir=os.getcwd()) as directory:
            path = os.path.join(directory, 'flask-race.sqlite3')
            target = __import__('sqlite3').connect(path)
            self.conn.backup(target)
            target.close()
            reserving_conn = __import__('sqlite3').connect(path, check_same_thread=False)
            cancelling_conn = __import__('sqlite3').connect(path, check_same_thread=False)
            reserving_conn.row_factory = cancelling_conn.row_factory = __import__('sqlite3').Row
            entered = threading.Event()
            release = threading.Event()
            reservation_result = []

            def delayed_node_adapter(operation, payload):
                self.assertEqual(operation, 'reserve')
                status, response = self._node_call(
                    'POST', '/v1/users/%s/campaign-batches/reserve' % self.worker_id,
                    payload,
                )
                self.assertEqual(status, 200)
                entered.set()
                self.assertTrue(release.wait(timeout=10))
                return response, status

            def reserve():
                with app_module.app.test_request_context('/'):
                    session['user_id'] = self.worker_id
                    reservation_result.append(
                        app_module.reserve_campaign_send_plan_with_node(
                            reserving_conn, plan['plan_id'],
                            node_call=delayed_node_adapter,
                            now='2026-10-14 10:00:00',
                        )
                    )

            thread = threading.Thread(target=reserve)
            thread.start()
            try:
                self.assertTrue(entered.wait(timeout=10))
                with app_module.app.test_request_context('/'):
                    session['user_id'] = self.worker_id
                    cancellation = app_module.cancel_campaign_send_plan(
                        cancelling_conn, plan['plan_id'],
                        now='2026-10-14 10:00:01',
                    )
                self.assertEqual(cancellation['status'], 'cancellation_blocked')
                self.assertEqual(cancellation['evidence_status'], 'present')
                self.assertEqual(
                    cancelling_conn.execute('''
                        SELECT COUNT(*) FROM whatsapp_campaign_recipient_claims
                        WHERE plan_id = ? AND state = 'claimed'
                    ''', (plan['plan_id'],)).fetchone()[0], 2,
                )
                release.set()
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())
                self.assertEqual(reservation_result[0]['status'], 'reserved')
            finally:
                release.set()
                thread.join(timeout=10)
                reserving_conn.close()
                cancelling_conn.close()

    def test_internal_hmac_and_replay_protection_cross_real_http_boundary(self):
        path = f'/v1/users/{self.worker_id}/quota'
        timestamp = str(int(time.time()))
        request_id = uuid.uuid4().hex
        canonical = f'{timestamp}.{request_id}.GET.{path}.'
        signature = hmac.new(_SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()
        request = urllib.request.Request(
            app_module.WHATSAPP_SERVICE_URL + path,
            headers={
                'X-TaskPe-Timestamp': timestamp,
                'X-TaskPe-Request-ID': request_id,
                'X-TaskPe-Signature': '0' * len(signature),
                'X-TaskPe-User-ID': str(self.worker_id),
            },
        )
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(error.exception.code, 401)
        request.headers['X-taskpe-signature'] = signature
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
        with self.assertRaises(urllib.error.HTTPError) as replay:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(replay.exception.code, 401)

    def test_public_flask_send_endpoint_blocked_by_consent_gate(self):
        plan = self._plan()
        self._reserve(plan['plan_id'])
        with self.client.session_transaction() as sess:
            sess['user_id'] = self.worker_id
            sess['_csrf_token'] = 'test-csrf-token'
        response = self.client.post(
            '/api/whatsapp/campaigns/send',
            json={'plan_id': plan['plan_id']},
            headers={'X-CSRF-Token': 'test-csrf-token'},
        )
        self.assertEqual(response.status_code, 412)
        body = response.get_json()
        self.assertFalse(body['success'])
        self.assertEqual(body['status'], 'consent_required')

    def test_e2e_dispatch_authorization_flow_not_consumed_when_sending_disabled(self):
        plan = self._plan()
        reserved = self._reserve(plan['plan_id'])
        self.assertEqual(reserved['status'], 'reserved')
        batch_id = plan['batch_id']

        with patch.object(app_module, 'WHATSAPP_CONSENT_GATE_AVAILABLE', True):
            result = app_module.send_reserved_whatsapp_campaign_batch(
                self.conn, plan['plan_id'], self.worker_id
            )

        self.assertEqual(result['status'], 'recovery_required')
        self.assertTrue(result['recovery_required'])
        self.assertFalse(result['delivered'])

        node_db = __import__('sqlite3').connect(_NODE_DB)
        node_db.row_factory = __import__('sqlite3').Row
        auth_row = node_db.execute(
            'SELECT * FROM whatsapp_campaign_dispatch_authorizations WHERE batch_id = ?',
            (batch_id,),
        ).fetchone()
        self.assertIsNone(auth_row)

        batch_row = node_db.execute(
            'SELECT state FROM whatsapp_campaign_batches WHERE batch_id = ?', (batch_id,)
        ).fetchone()
        self.assertEqual(batch_row['state'], 'reserved')
        queued_count = node_db.execute(
            'SELECT COUNT(*) FROM outgoing_messages WHERE batch_id = ? AND state = "queued"',
            (batch_id,),
        ).fetchone()[0]
        self.assertEqual(queued_count, 2)
        node_db.close()

        flask_plan = self.conn.execute(
            'SELECT dispatch_state, dispatch_operation_id FROM whatsapp_campaign_plans WHERE id = ?',
            (plan['plan_id'],),
        ).fetchone()
        self.assertEqual(flask_plan['dispatch_state'], 'unknown')
        self.assertIsNotNone(flask_plan['dispatch_operation_id'])
        claims_held = self.conn.execute(
            'SELECT COUNT(*) FROM whatsapp_campaign_recipient_claims WHERE plan_id = ? AND state = "claimed"',
            (plan['plan_id'],),
        ).fetchone()[0]
        self.assertEqual(claims_held, 2)

        with patch.object(app_module, 'WHATSAPP_CONSENT_GATE_AVAILABLE', True):
            recovered = app_module.send_reserved_whatsapp_campaign_batch(
                self.conn, plan['plan_id'], self.worker_id
            )
        self.assertEqual(recovered['status'], 'recovery_required')

        plan_row = self.conn.execute(
            'SELECT * FROM whatsapp_campaign_plans WHERE id = ?', (plan['plan_id'],)
        ).fetchone()
        token = app_module.create_dispatch_authorization_token(
            plan_row, self.worker_id, _SECRET, operation_id='op-retry-attempt-001'
        )
        status, response = self._node_call(
            'POST', f'/v1/users/{self.worker_id}/campaign-batches/{batch_id}/send',
            extra_headers={'X-TaskPe-Dispatch-Authorization': token},
        )
        self.assertEqual(status, 503)
        self.assertEqual(response['status'], 'sending_disabled')

    def test_node_send_boundary_rejects_missing_tampered_and_mismatched_tokens_over_http(self):
        plan = self._plan()
        self._reserve(plan['plan_id'])
        batch_id = plan['batch_id']
        plan_row = self.conn.execute(
            'SELECT * FROM whatsapp_campaign_plans WHERE id = ?', (plan['plan_id'],)
        ).fetchone()

        status, res = self._node_call(
            'POST', f'/v1/users/{self.worker_id}/campaign-batches/{batch_id}/send'
        )
        self.assertEqual(status, 503)
        self.assertEqual(res['status'], 'sending_disabled')

        valid_token = app_module.create_dispatch_authorization_token(
            plan_row, self.worker_id, _SECRET, operation_id='op-tamper-test-0001'
        )
        tampered_token = valid_token[:-4] + ('0000' if valid_token[-4:] != '0000' else '1111')
        status, res = self._node_call(
            'POST', f'/v1/users/{self.worker_id}/campaign-batches/{batch_id}/send',
            extra_headers={'X-TaskPe-Dispatch-Authorization': tampered_token},
        )
        self.assertEqual(status, 401)
        self.assertEqual(res['status'], 'invalid_authorization')

        expired_token = app_module.create_dispatch_authorization_token(
            plan_row, self.worker_id, _SECRET,
            now=int(time.time()) - 120, ttl_seconds=30, operation_id='op-expired-test-0001'
        )
        status, res = self._node_call(
            'POST', f'/v1/users/{self.worker_id}/campaign-batches/{batch_id}/send',
            extra_headers={'X-TaskPe-Dispatch-Authorization': expired_token},
        )
        self.assertEqual(status, 401)
        self.assertEqual(res['status'], 'invalid_authorization')

        mismatched_worker_plan = dict(plan_row)
        mismatched_worker_plan['sender_worker_id'] = self.worker_id + 99
        mismatched_worker_token = app_module.create_dispatch_authorization_token(
            mismatched_worker_plan, self.worker_id + 99, _SECRET, operation_id='op-mismatch-worker-1'
        )
        status, res = self._node_call(
            'POST', f'/v1/users/{self.worker_id}/campaign-batches/{batch_id}/send',
            extra_headers={'X-TaskPe-Dispatch-Authorization': mismatched_worker_token},
        )
        self.assertEqual(status, 403)
        self.assertEqual(res['status'], 'identity_mismatch')

        plan_other = self._plan(task_id=self.video_task_id, content_type='video')
        self._reserve(plan_other['plan_id'], content_type='video')
        mismatched_batch_token = app_module.create_dispatch_authorization_token(
            plan_row, self.worker_id, _SECRET, operation_id='op-mismatch-batch-001'
        )
        status, res = self._node_call(
            'POST', f'/v1/users/{self.worker_id}/campaign-batches/{plan_other["batch_id"]}/send',
            extra_headers={'X-TaskPe-Dispatch-Authorization': mismatched_batch_token},
        )
        self.assertEqual(status, 403)
        self.assertEqual(res['status'], 'identity_mismatch')

    def test_batch_with_non_queued_items_fails_closed_in_node_ledger(self):
        plan = self._plan()
        self._reserve(plan['plan_id'])
        batch_id = plan['batch_id']
        plan_row = self.conn.execute(
            'SELECT * FROM whatsapp_campaign_plans WHERE id = ?', (plan['plan_id'],)
        ).fetchone()

        node_db = __import__('sqlite3').connect(_NODE_DB)
        node_db.execute(
            'UPDATE outgoing_messages SET state = "attempted" WHERE batch_id = ? AND rowid = (SELECT rowid FROM outgoing_messages WHERE batch_id = ? LIMIT 1)',
            (batch_id, batch_id),
        )
        node_db.commit()
        node_db.close()

        token = app_module.create_dispatch_authorization_token(
            plan_row, self.worker_id, _SECRET, operation_id='op-partial-batch-001'
        )
        status, res = self._node_call(
            'POST', f'/v1/users/{self.worker_id}/campaign-batches/{batch_id}/send',
            extra_headers={'X-TaskPe-Dispatch-Authorization': token},
        )
        self.assertEqual(status, 503)
        self.assertEqual(res['status'], 'sending_disabled')

        node_db = __import__('sqlite3').connect(_NODE_DB)
        count = node_db.execute(
            'SELECT COUNT(*) FROM whatsapp_campaign_dispatch_authorizations WHERE batch_id = ?',
            (batch_id,),
        ).fetchone()[0]
        self.assertEqual(count, 0)
        node_db.close()

    def test_interrupted_dispatch_recovers_across_simulated_restart(self):
        plan = self._plan()
        self._reserve(plan['plan_id'])
        batch_id = plan['batch_id']

        self.conn.execute('''
            UPDATE whatsapp_campaign_plans
            SET dispatch_state = 'in_progress', dispatch_operation_id = 'op-crash-interrupted'
            WHERE id = ?
        ''', (plan['plan_id'],))
        self.conn.commit()

        fresh_conn = database.get_db_connection()
        try:
            with patch.object(app_module, 'WHATSAPP_CONSENT_GATE_AVAILABLE', True):
                result = app_module.send_reserved_whatsapp_campaign_batch(
                    fresh_conn, plan['plan_id'], self.worker_id
                )
            self.assertEqual(result['status'], 'recovery_required')
            plan_state = fresh_conn.execute(
                'SELECT dispatch_state FROM whatsapp_campaign_plans WHERE id = ?', (plan['plan_id'],)
            ).fetchone()['dispatch_state']
            self.assertIn(plan_state, {'in_progress', 'unknown'})
        finally:
            fresh_conn.close()

        node_db = __import__('sqlite3').connect(_NODE_DB)
        count = node_db.execute(
            'SELECT COUNT(*) FROM whatsapp_campaign_dispatch_authorizations WHERE batch_id = ?',
            (batch_id,),
        ).fetchone()[0]
        self.assertEqual(count, 0)
        node_db.close()

    def test_dispatch_request_never_completes_fails_closed_and_claims_held(self):
        plan = self._plan()
        self._reserve(plan['plan_id'])
        batch_id = plan['batch_id']

        def dropped_adapter(operation, payload, extra_headers=None):
            if operation == 'send':
                raise urllib.error.URLError('Connection dropped before response')
            return self._node_call('GET', f"/v1/users/{self.worker_id}/campaign-batches/{batch_id}")

        with patch.object(app_module, 'WHATSAPP_CONSENT_GATE_AVAILABLE', True):
            result = app_module.send_reserved_whatsapp_campaign_batch(
                self.conn, plan['plan_id'], self.worker_id, node_call=dropped_adapter
            )

        self.assertEqual(result['status'], 'recovery_required')
        self.assertTrue(result['recovery_required'])
        self.assertFalse(result['delivered'])

        # Verify Flask plan transitioned to unknown and claims remain held
        flask_plan = self.conn.execute(
            'SELECT dispatch_state, dispatch_operation_id FROM whatsapp_campaign_plans WHERE id = ?',
            (plan['plan_id'],),
        ).fetchone()
        self.assertEqual(flask_plan['dispatch_state'], 'unknown')
        self.assertIsNotNone(flask_plan['dispatch_operation_id'])
        claims_held = self.conn.execute(
            'SELECT COUNT(*) FROM whatsapp_campaign_recipient_claims WHERE plan_id = ? AND state = "claimed"',
            (plan['plan_id'],),
        ).fetchone()[0]
        self.assertEqual(claims_held, 2)

        # Node has zero dispatch authorizations recorded because request never completed
        node_db = __import__('sqlite3').connect(_NODE_DB)
        count = node_db.execute(
            'SELECT COUNT(*) FROM whatsapp_campaign_dispatch_authorizations WHERE batch_id = ?',
            (batch_id,),
        ).fetchone()[0]
        self.assertEqual(count, 0)
        node_db.close()

    def test_concurrent_duplicate_http_send_dispatches_remain_blocked_and_non_consuming(self):
        plan = self._plan()
        self._reserve(plan['plan_id'])
        batch_id = plan['batch_id']
        plan_row = self.conn.execute(
            'SELECT * FROM whatsapp_campaign_plans WHERE id = ?', (plan['plan_id'],)
        ).fetchone()

        token1 = app_module.create_dispatch_authorization_token(
            plan_row, self.worker_id, _SECRET, operation_id='op-concurrent-test-001'
        )
        token2 = app_module.create_dispatch_authorization_token(
            plan_row, self.worker_id, _SECRET, operation_id='op-concurrent-test-002'
        )

        results = []
        barrier = threading.Barrier(2)

        def call_send(token):
            barrier.wait()
            status, res = self._node_call(
                'POST', f'/v1/users/{self.worker_id}/campaign-batches/{batch_id}/send',
                extra_headers={'X-TaskPe-Dispatch-Authorization': token},
            )
            results.append((status, res))

        t1 = threading.Thread(target=call_send, args=(token1,))
        t2 = threading.Thread(target=call_send, args=(token2,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        for status, res in results:
            self.assertEqual(status, 503)
            self.assertEqual(res['status'], 'sending_disabled')

        # Node ledger must record zero authorization consumptions while sending is disabled
        node_db = __import__('sqlite3').connect(_NODE_DB)
        count = node_db.execute(
            'SELECT COUNT(*) FROM whatsapp_campaign_dispatch_authorizations WHERE batch_id = ?',
            (batch_id,),
        ).fetchone()[0]
        self.assertEqual(count, 0)
        # All messages remain queued
        queued = node_db.execute(
            'SELECT COUNT(*) FROM outgoing_messages WHERE batch_id = ? AND state = "queued"',
            (batch_id,),
        ).fetchone()[0]
        self.assertEqual(queued, 2)
        node_db.close()

    def test_recovery_performs_lookup_only_and_never_issues_token_or_sends(self):
        plan = self._plan()
        self._reserve(plan['plan_id'])

        with patch.object(app_module, 'WHATSAPP_CONSENT_GATE_AVAILABLE', True):
            initial_result = app_module.send_reserved_whatsapp_campaign_batch(
                self.conn, plan['plan_id'], self.worker_id
            )
        self.assertEqual(initial_result['status'], 'recovery_required')

        with patch.object(app_module, 'create_dispatch_authorization_token') as mock_token_creator, \
             patch.object(app_module, '_campaign_batch_adapter_call', wraps=app_module._campaign_batch_adapter_call) as mock_adapter, \
             patch.object(app_module, 'WHATSAPP_CONSENT_GATE_AVAILABLE', True):
            recovery_result = app_module.send_reserved_whatsapp_campaign_batch(
                self.conn, plan['plan_id'], self.worker_id
            )
            mock_token_creator.assert_not_called()
            # Ensure adapter was called with operation 'lookup' only, never 'send'
            operations = [call.args[1] for call in mock_adapter.call_args_list]
            self.assertIn('lookup', operations)
            self.assertNotIn('send', operations)

        self.assertEqual(recovery_result['status'], 'recovery_required')

    def test_dispatch_state_persistence_failure_retains_in_progress_claim(self):
        plan = self._plan()
        self._reserve(plan['plan_id'])

        with patch.object(app_module, 'WHATSAPP_CONSENT_GATE_AVAILABLE', True), \
             patch.object(app_module, '_mark_campaign_dispatch_unknown', side_effect=RuntimeError('DB write lock failure')):
            result = app_module.send_reserved_whatsapp_campaign_batch(
                self.conn, plan['plan_id'], self.worker_id
            )

        self.assertEqual(result['status'], 'recovery_required')
        # Plan remains in_progress because marking unknown failed
        plan_state = self.conn.execute(
            'SELECT dispatch_state FROM whatsapp_campaign_plans WHERE id = ?', (plan['plan_id'],)
        ).fetchone()['dispatch_state']
        self.assertEqual(plan_state, 'in_progress')
        # Claims remain held
        claims = self.conn.execute(
            'SELECT COUNT(*) FROM whatsapp_campaign_recipient_claims WHERE plan_id = ? AND state = "claimed"',
            (plan['plan_id'],),
        ).fetchone()[0]
        self.assertEqual(claims, 2)



    _assignment_seed = 0

    def _create_assignment(self, worker_id=None, task_id=None, status='in_progress',
                           expired=False, numbers=None):
        FlaskNodeIntegrationTests._assignment_seed += 1
        seed = FlaskNodeIntegrationTests._assignment_seed
        worker_id = worker_id or self.worker_id
        task_id = task_id or self.task_id
        task = self.conn.execute('SELECT campaign_id, target_numbers FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if numbers is None and status == 'in_progress':
            numbers = json.loads(task['target_numbers'])
        numbers = numbers or [f'+9198765432{seed:02d}', f'+9198765432{seed + 20:02d}']
        campaign_id = task['campaign_id'] if task else task_id
        expires_at = "2020-01-01 00:00:00" if expired else "2030-01-01 00:00:00"

        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO task_assignments (user_id, task_id, campaign_id, campaign_type, assigned_numbers, status, started_at, expires_at, slot_released)
            VALUES (?, ?, ?, 'Research', ?, ?, '2026-01-01 00:00:00', ?, 0)
        ''', (worker_id, task_id, campaign_id, json.dumps(numbers), status, expires_at))
        assignment_id = cursor.lastrowid

        for num in numbers:
            cursor.execute('''
                INSERT INTO recipient_allocations (campaign_id, phone_number, assignment_id, user_id, last_contacted_at, status)
                VALUES (?, ?, ?, ?, '2026-01-01 00:00:00', 'ALLOCATED')
            ''', (campaign_id, num, assignment_id, worker_id))
        self.conn.commit()
        return assignment_id, task_id, numbers

    def test_unauthenticated_request_rejected(self):
        assignment_id, task_id, _ = self._create_assignment()
        response = self.client.post(
            f'/api/tasks/{task_id}/assignments/{assignment_id}/whatsapp-send',
            json={},
        )
        self.assertEqual(response.status_code, 401)
        body = response.get_json()
        self.assertFalse(body['success'])

    def test_missing_or_invalid_csrf_token_rejected(self):
        assignment_id, task_id, _ = self._create_assignment()
        with self.client.session_transaction() as sess:
            sess['user_id'] = self.worker_id
            sess['_csrf_token'] = 'valid-token'

        # Missing CSRF
        res_missing = self.client.post(
            f'/api/tasks/{task_id}/assignments/{assignment_id}/whatsapp-send',
            json={},
        )
        self.assertEqual(res_missing.status_code, 403)
        self.assertFalse(res_missing.get_json()['success'])

        # Invalid CSRF
        res_invalid = self.client.post(
            f'/api/tasks/{task_id}/assignments/{assignment_id}/whatsapp-send',
            json={},
            headers={'X-CSRF-Token': 'wrong-token'},
        )
        self.assertEqual(res_invalid.status_code, 403)
        self.assertFalse(res_invalid.get_json()['success'])

    def test_another_worker_assignment_rejected(self):
        assignment_id, task_id, _ = self._create_assignment(worker_id=self.other_worker_id)
        with self.client.session_transaction() as sess:
            sess['user_id'] = self.worker_id
            sess['_csrf_token'] = 'csrf-tok'

        with patch.object(app_module, 'WHATSAPP_CONSENT_GATE_AVAILABLE', True):
            response = self.client.post(
                f'/api/tasks/{task_id}/assignments/{assignment_id}/whatsapp-send',
                json={},
                headers={'X-CSRF-Token': 'csrf-tok'},
            )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.get_json()['success'])

    def test_wrong_task_assignment_pairing_rejected(self):
        assignment_id, task_id, _ = self._create_assignment()
        with self.client.session_transaction() as sess:
            sess['user_id'] = self.worker_id
            sess['_csrf_token'] = 'csrf-tok'

        wrong_task_id = task_id + 999
        with patch.object(app_module, 'WHATSAPP_CONSENT_GATE_AVAILABLE', True):
            response = self.client.post(
                f'/api/tasks/{wrong_task_id}/assignments/{assignment_id}/whatsapp-send',
                json={},
                headers={'X-CSRF-Token': 'csrf-tok'},
            )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()['success'])

    def test_cancelled_completed_or_expired_assignment_rejected(self):
        with self.client.session_transaction() as sess:
            sess['user_id'] = self.worker_id
            sess['_csrf_token'] = 'csrf-tok'

        with patch.object(app_module, 'WHATSAPP_CONSENT_GATE_AVAILABLE', True):
            # Cancelled
            a1, t1, _ = self._create_assignment(status='cancelled')
            r1 = self.client.post(
                f'/api/tasks/{t1}/assignments/{a1}/whatsapp-send',
                json={},
                headers={'X-CSRF-Token': 'csrf-tok'},
            )
            self.assertEqual(r1.status_code, 400)
            self.assertFalse(r1.get_json()['success'])

            # Completed / paid
            a2, t2, _ = self._create_assignment(status='paid')
            r2 = self.client.post(
                f'/api/tasks/{t2}/assignments/{a2}/whatsapp-send',
                json={},
                headers={'X-CSRF-Token': 'csrf-tok'},
            )
            self.assertEqual(r2.status_code, 400)
            self.assertFalse(r2.get_json()['success'])

            # Expired
            a3, t3, _ = self._create_assignment(expired=True)
            r3 = self.client.post(
                f'/api/tasks/{t3}/assignments/{a3}/whatsapp-send',
                json={},
                headers={'X-CSRF-Token': 'csrf-tok'},
            )
            self.assertEqual(r3.status_code, 400)
            self.assertFalse(r3.get_json()['success'])

    def test_recipient_outside_assignment_rejected(self):
        assignment_id, task_id, _ = self._create_assignment(numbers=['919876543210', '919876543211'])
        with self.client.session_transaction() as sess:
            sess['user_id'] = self.worker_id
            sess['_csrf_token'] = 'csrf-tok'

        with patch.object(app_module, 'WHATSAPP_CONSENT_GATE_AVAILABLE', True):
            response = self.client.post(
                f'/api/tasks/{task_id}/assignments/{assignment_id}/whatsapp-send',
                json={'recipients': ['919999999999']},
                headers={'X-CSRF-Token': 'csrf-tok'},
            )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()['success'])

    def test_missing_or_incorrect_allocated_row_rejected(self):
        assignment_id, task_id, numbers = self._create_assignment()
        # Alter allocation status from ALLOCATED to RELEASED
        self.conn.execute(
            "UPDATE recipient_allocations SET status = 'RELEASED' WHERE assignment_id = ? AND phone_number = ?",
            (assignment_id, numbers[0])
        )
        self.conn.commit()

        with self.client.session_transaction() as sess:
            sess['user_id'] = self.worker_id
            sess['_csrf_token'] = 'csrf-tok'

        with patch.object(app_module, 'WHATSAPP_CONSENT_GATE_AVAILABLE', True):
            response = self.client.post(
                f'/api/tasks/{task_id}/assignments/{assignment_id}/whatsapp-send',
                json={},
                headers={'X-CSRF-Token': 'csrf-tok'},
            )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()['success'])

    def test_client_supplied_arbitrary_content_cannot_override_task_content(self):
        assignment_id, task_id, _ = self._create_assignment()
        with self.client.session_transaction() as sess:
            sess['user_id'] = self.worker_id
            sess['_csrf_token'] = 'csrf-tok'

        # Client attempts to send arbitrary message and destination link
        with patch.object(app_module, 'WHATSAPP_CONSENT_GATE_AVAILABLE', True):
            response = self.client.post(
                f'/api/tasks/{task_id}/assignments/{assignment_id}/whatsapp-send',
                json={
                    'template_text': 'malicious override',
                    'destination_link': 'http://malicious.test',
                    'content_type': 'video',
                },
                headers={'X-CSRF-Token': 'csrf-tok'},
            )
        plan = self.conn.execute(
            'SELECT * FROM whatsapp_campaign_plans WHERE sender_worker_id = ? ORDER BY id DESC LIMIT 1',
            (self.worker_id,)
        ).fetchone()
        self.assertIsNotNone(plan)
        self.assertEqual(plan['content_reference'], f'task:{task_id}:text_link:v1')
        self.assertEqual(plan['batch_content_type'], 'text')

    def test_unsupported_video_content_rejected(self):
        assignment_id, video_task_id, _ = self._create_assignment(task_id=self.video_task_id)
        with self.client.session_transaction() as sess:
            sess['user_id'] = self.worker_id
            sess['_csrf_token'] = 'csrf-tok'

        with patch.object(app_module, 'WHATSAPP_CONSENT_GATE_AVAILABLE', True):
            response = self.client.post(
                f'/api/tasks/{video_task_id}/assignments/{assignment_id}/whatsapp-send',
                json={},
                headers={'X-CSRF-Token': 'csrf-tok'},
            )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()['success'])
        self.assertIn('video', response.get_json()['message'].lower())

    def test_reservation_failure_does_not_trigger_sending(self):
        assignment_id, task_id, _ = self._create_assignment()
        with self.client.session_transaction() as sess:
            sess['user_id'] = self.worker_id
            sess['_csrf_token'] = 'csrf-tok'

        # Mock quota status to return 0 remaining
        with patch.object(app_module, 'WHATSAPP_CONSENT_GATE_AVAILABLE', True), \
             patch.object(app_module, '_worker_whatsapp_quota_status', return_value={'status': 'available', 'remaining': 0}):
            response = self.client.post(
                f'/api/tasks/{task_id}/assignments/{assignment_id}/whatsapp-send',
                json={},
                headers={'X-CSRF-Token': 'csrf-tok'},
            )
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.get_json()['success'])
        self.assertEqual(response.get_json()['status'], 'quota_exceeded')

        # Verify no dispatch authorizations were created
        node_db = __import__('sqlite3').connect(_NODE_DB)
        has_table = node_db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='whatsapp_campaign_dispatch_authorizations'").fetchone()
        count = node_db.execute('SELECT COUNT(*) FROM whatsapp_campaign_dispatch_authorizations').fetchone()[0] if has_table else 0
        self.assertEqual(count, 0)
        node_db.close()

    def test_uncertain_reservation_recovered_by_batch_lookup(self):
        assignment_id, task_id, _ = self._create_assignment()
        plan_key = f'test-uncertain-plan-{uuid.uuid4().hex}'
        plan = app_module.create_assignment_send_plan(
            self.conn, assignment_id, self.worker_id, plan_key
        )

        # Pre-commit the batch in Node directly so it exists
        status, node_res = self._node_call(
            'POST', f'/v1/users/{self.worker_id}/campaign-batches/reserve',
            self._batch_payload(plan['plan_id']),
        )
        self.assertEqual(status, 200)

        # Mark Flask batch state and recipients as reserving
        self.conn.execute("UPDATE whatsapp_campaign_plans SET batch_state = 'reserving' WHERE id = ?", (plan['plan_id'],))
        self.conn.execute('''
            UPDATE whatsapp_campaign_plan_recipients
            SET state = 'reserving', reservation_claim_id = 'lost-response-claim'
            WHERE plan_id = ?
        ''', (plan['plan_id'],))
        self.conn.commit()

        # Execute assignment send batch - recovery lookup succeeds
        with patch.object(app_module, 'WHATSAPP_CONSENT_GATE_AVAILABLE', True):
            with app_module.app.test_request_context('/'):
                session['user_id'] = self.worker_id
                result = app_module.send_assignment_whatsapp_batch(
                    self.conn, assignment_id, self.worker_id, task_id=task_id, plan_key=plan_key
                )
        # Reservation was recovered to 'reserved', then send dispatch failed closed safely
        self.assertEqual(result['status'], 'recovery_required')
        refreshed_batch_state = self.conn.execute(
            'SELECT batch_state FROM whatsapp_campaign_plans WHERE id = ?', (plan['plan_id'],)
        ).fetchone()['batch_state']
        self.assertEqual(refreshed_batch_state, 'reserved')

    def test_repeated_requests_idempotent_no_duplicate_sends(self):
        assignment_id, task_id, _ = self._create_assignment()
        plan_key = f'test-idempotent-plan-{uuid.uuid4().hex}'

        plan1 = app_module.create_assignment_send_plan(
            self.conn, assignment_id, self.worker_id, plan_key
        )
        self.assertTrue(plan1['created'])

        plan2 = app_module.create_assignment_send_plan(
            self.conn, assignment_id, self.worker_id, plan_key
        )
        self.assertFalse(plan2['created'])
        self.assertTrue(plan2['idempotent'])
        self.assertEqual(plan1['plan_id'], plan2['plan_id'])

        # Count plans and recipients in database
        plan_count = self.conn.execute(
            'SELECT COUNT(*) FROM whatsapp_campaign_plans WHERE plan_key = ?', (plan_key,)
        ).fetchone()[0]
        self.assertEqual(plan_count, 1)

    def test_uncertain_send_recovered_by_status_lookup(self):
        assignment_id, task_id, _ = self._create_assignment()
        plan_key = f'test-uncertain-send-{uuid.uuid4().hex}'
        plan = app_module.create_assignment_send_plan(
            self.conn, assignment_id, self.worker_id, plan_key
        )
        # Reserve with node
        with app_module.app.test_request_context('/'):
            session['user_id'] = self.worker_id
            reserved = app_module.reserve_campaign_send_plan_with_node(self.conn, plan['plan_id'])
            self.assertEqual(reserved['status'], 'reserved')

        # Drop the send request
        def dropped_send_adapter(operation, payload, extra_headers=None):
            if operation == 'send':
                raise urllib.error.URLError('Network drop on send')
            return self._node_call('GET', f"/v1/users/{self.worker_id}/campaign-batches/{plan['batch_id']}")

        with patch.object(app_module, 'WHATSAPP_CONSENT_GATE_AVAILABLE', True):
            with app_module.app.test_request_context('/'):
                session['user_id'] = self.worker_id
                result = app_module.send_assignment_whatsapp_batch(
                    self.conn, assignment_id, self.worker_id, task_id=task_id,
                    plan_key=plan_key, node_call=dropped_send_adapter
                )

        self.assertEqual(result['status'], 'recovery_required')
        self.assertTrue(result['recovery_required'])

        # Verify claims remain held
        claims = self.conn.execute(
            'SELECT COUNT(*) FROM whatsapp_campaign_recipient_claims WHERE plan_id = ? AND state = "claimed"',
            (plan['plan_id'],)
        ).fetchone()[0]
        self.assertEqual(claims, 2)

    def test_valid_assignment_blocked_by_consent_gate_by_default(self):
        assignment_id, task_id, _ = self._create_assignment()
        with self.client.session_transaction() as sess:
            sess['user_id'] = self.worker_id
            sess['_csrf_token'] = 'csrf-tok'

        node_db = __import__('sqlite3').connect(_NODE_DB)
        has_batches = node_db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='campaign_batches'").fetchone()
        initial_batches = node_db.execute('SELECT COUNT(*) FROM campaign_batches').fetchone()[0] if has_batches else 0
        has_messages = node_db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outgoing_messages'").fetchone()
        initial_messages = node_db.execute('SELECT COUNT(*) FROM outgoing_messages').fetchone()[0] if has_messages else 0
        node_db.close()

        response = self.client.post(
            f'/api/tasks/{task_id}/assignments/{assignment_id}/whatsapp-send',
            json={},
            headers={'X-CSRF-Token': 'csrf-tok'},
        )
        self.assertEqual(response.status_code, 412)
        body = response.get_json()
        self.assertFalse(body['success'])
        self.assertEqual(body['status'], 'consent_required')

        # Verify no plan was created
        plan_count = self.conn.execute(
            'SELECT COUNT(*) FROM whatsapp_campaign_plans WHERE task_id = ?', (task_id,)
        ).fetchone()[0]
        self.assertEqual(plan_count, 0)

        # Verify no recipient claims were created
        claim_count = self.conn.execute(
            'SELECT COUNT(*) FROM whatsapp_campaign_recipient_claims'
        ).fetchone()[0]
        self.assertEqual(claim_count, 0)

        # Verify Node ledger has no new batches or outgoing messages
        node_db = __import__('sqlite3').connect(_NODE_DB)
        has_batches = node_db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='campaign_batches'").fetchone()
        batch_count = node_db.execute('SELECT COUNT(*) FROM campaign_batches').fetchone()[0] if has_batches else 0
        has_messages = node_db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outgoing_messages'").fetchone()
        msg_count = node_db.execute('SELECT COUNT(*) FROM outgoing_messages').fetchone()[0] if has_messages else 0
        node_db.close()
        self.assertEqual(batch_count, initial_batches)
        self.assertEqual(msg_count, initial_messages)

    def test_disabled_consent_gate_http_path_never_calls_node_or_mutates_state(self):
        assignment_id, task_id, _ = self._create_assignment()
        with self.client.session_transaction() as sess:
            sess['user_id'] = self.worker_id
            sess['_csrf_token'] = 'csrf-tok'

        with patch.object(app_module, '_call_whatsapp_service') as mock_node_call:
            response = self.client.post(
                f'/api/tasks/{task_id}/assignments/{assignment_id}/whatsapp-send',
                json={},
                headers={'X-CSRF-Token': 'csrf-tok'},
            )

        self.assertEqual(response.status_code, 412)
        body = response.get_json()
        self.assertFalse(body['success'])
        self.assertEqual(body['status'], 'consent_required')

        # Node service was never invoked
        mock_node_call.assert_not_called()

        # Database state remains completely unmutated
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM whatsapp_campaign_plans').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM whatsapp_campaign_recipient_claims').fetchone()[0], 0)
        assignment_row = self.conn.execute('SELECT status FROM task_assignments WHERE id = ?', (assignment_id,)).fetchone()
        self.assertEqual(assignment_row['status'], 'in_progress')
        allocations = self.conn.execute(
            'SELECT status FROM recipient_allocations WHERE assignment_id = ?', (assignment_id,)
        ).fetchall()
        self.assertTrue(all(a['status'] == 'ALLOCATED' for a in allocations))

    def test_send_assignment_whatsapp_batch_function_blocked_when_consent_gate_disabled(self):
        assignment_id, task_id, _ = self._create_assignment()
        with patch.object(app_module, '_call_whatsapp_service') as mock_node_call:
            result = app_module.send_assignment_whatsapp_batch(
                self.conn, assignment_id, self.worker_id, task_id=task_id
            )

        self.assertFalse(result.get('success'))
        self.assertEqual(result.get('status'), 'consent_required')
        mock_node_call.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM whatsapp_campaign_plans').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM whatsapp_campaign_recipient_claims').fetchone()[0], 0)

    def test_valid_assignment_reserve_and_send_when_consent_gate_enabled(self):
        assignment_id, task_id, _ = self._create_assignment()
        with self.client.session_transaction() as sess:
            sess['user_id'] = self.worker_id
            sess['_csrf_token'] = 'csrf-tok'

        with patch.object(app_module, 'WHATSAPP_CONSENT_GATE_AVAILABLE', True):
            response = self.client.post(
                f'/api/tasks/{task_id}/assignments/{assignment_id}/whatsapp-send',
                json={},
                headers={'X-CSRF-Token': 'csrf-tok'},
            )
        # Node returns 503 sending_disabled (because CAMPAIGN_BATCH_HTTP_SEND_ENABLED is false)
        # Flask catches this uncertainty, marks unknown, and returns 202 recovery_required safely!
        self.assertEqual(response.status_code, 202)
        body = response.get_json()
        self.assertFalse(body['success'])
        self.assertEqual(body['status'], 'recovery_required')
        self.assertTrue(body['recovery_required'])

        # Verify reservation succeeded and messages are queued in Node ledger
        node_db = __import__('sqlite3').connect(_NODE_DB)
        plan = self.conn.execute(
            'SELECT batch_id FROM whatsapp_campaign_plans WHERE sender_worker_id = ? ORDER BY id DESC LIMIT 1',
            (self.worker_id,)
        ).fetchone()
        queued = node_db.execute(
            'SELECT COUNT(*) FROM outgoing_messages WHERE batch_id = ? AND state = "queued"',
            (plan['batch_id'],)
        ).fetchone()[0]
        self.assertEqual(queued, 2)
        node_db.close()


if __name__ == '__main__':
    unittest.main()


