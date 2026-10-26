"""Focused tests for the read-only database-driven recipient selector."""

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from flask import session

os.environ.setdefault('ENVIRONMENT', 'development')
os.environ.setdefault('SECRET_KEY', 'recipient-eligibility-test-secret')

from app import build_recipient_eligibility, select_eligible_recipients
from app import app as flask_app
from app import (
    campaign_plan_recipient_idempotency_key,
    create_campaign_send_plan,
    recover_campaign_plan_recipient,
    release_campaign_send_plan_claims,
    cancel_campaign_send_plan,
    reserve_campaign_plan_recipient,
    reserve_campaign_send_plan_with_node,
)
import database


class RecipientEligibilityTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript('''
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY,
                campaign_id INTEGER,
                target_numbers TEXT NOT NULL
            );
            CREATE TABLE recipient_allocations (
                phone_number TEXT NOT NULL,
                campaign_id INTEGER NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE users (id INTEGER PRIMARY KEY);
            CREATE TABLE whatsapp_campaign_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_key TEXT NOT NULL UNIQUE,
                request_fingerprint TEXT NOT NULL,
                recipient_fingerprint TEXT,
                task_id INTEGER NOT NULL,
                campaign_id INTEGER NOT NULL,
                sender_worker_id INTEGER NOT NULL,
                content_reference TEXT NOT NULL,
                recipient_count INTEGER NOT NULL,
                requested_quantity TEXT,
                state TEXT NOT NULL,
                batch_id TEXT UNIQUE,
                batch_request_fingerprint TEXT,
                batch_content_type TEXT NOT NULL DEFAULT 'text',
                batch_state TEXT NOT NULL DEFAULT 'pending',
                batch_updated_at TEXT,
                dispatch_state TEXT NOT NULL DEFAULT 'not_started',
                dispatch_operation_id TEXT,
                dispatch_started_at TEXT,
                cancellation_state TEXT NOT NULL DEFAULT 'not_requested',
                cancellation_evidence_status TEXT,
                cancellation_requested_at TEXT,
                cancellation_checked_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE whatsapp_campaign_plan_recipients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL,
                recipient_number TEXT NOT NULL,
                position INTEGER NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL DEFAULT 'planned',
                reservation_claim_id TEXT,
                reservation_lease_until TEXT,
                ledger_message_id TEXT,
                failure_code TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (plan_id, position),
                UNIQUE (plan_id, recipient_number)
            );
            CREATE TABLE whatsapp_campaign_recipient_claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER NOT NULL,
                task_id INTEGER NOT NULL,
                recipient_number TEXT NOT NULL,
                plan_id INTEGER NOT NULL,
                plan_recipient_id INTEGER NOT NULL,
                state TEXT NOT NULL DEFAULT 'claimed',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (plan_id, plan_recipient_id)
            );
            CREATE UNIQUE INDEX whatsapp_campaign_recipient_claims_active_unique
                ON whatsapp_campaign_recipient_claims(campaign_id, recipient_number)
                WHERE state = 'claimed';
            CREATE TABLE whatsapp_opt_outs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone_number TEXT NOT NULL UNIQUE,
                reason TEXT NOT NULL DEFAULT 'user_opt_out',
                opted_out_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        self.conn.executemany('INSERT INTO users (id) VALUES (?)', [(7,), (8,)])
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def task(self, numbers, campaign_id=17):
        numbers = [database.normalize_recipient_number(number, region='IN') for number in numbers]
        return {
            'id': 4,
            'campaign_id': campaign_id,
            'target_numbers': json.dumps(numbers),
            'recipient_region': 'IN',
        }

    def test_normalizes_and_deduplicates_in_source_order(self):
        snapshot = build_recipient_eligibility(
            self.conn, self.task(['+91 98765 43210', '919876543210', '919876543211'])
        )
        self.assertEqual(snapshot['source_recipient_count'], 2)
        self.assertEqual(snapshot['eligible_recipients'], ['+919876543210', '+919876543211'])

    def test_excludes_allocated_and_completed_with_reasons(self):
        self.conn.executemany(
            'INSERT INTO recipient_allocations VALUES (?, ?, ?)',
            [
                ('919876543210', 17, 'ALLOCATED'),
                ('919876543211', 17, 'COMPLETED'),
                ('919876543212', 99, 'ALLOCATED'),
            ],
        )
        snapshot = build_recipient_eligibility(
            self.conn, self.task(['919876543210', '919876543211', '919876543212', '919876543213'])
        )
        self.assertEqual(snapshot['eligible_recipients'], ['+919876543212', '+919876543213'])
        self.assertEqual(snapshot['eligible_recipient_count'], 2)
        self.assertEqual(snapshot['exclusion_counts'], {
            'allocated_same_campaign': 1,
            'completed_same_campaign': 1,
            'claimed_same_campaign': 0,
        })

    def test_selection_is_deterministic_for_quantity_and_all(self):
        snapshot = build_recipient_eligibility(
            self.conn, self.task(['919876543210', '919876543211', '919876543212'])
        )
        self.assertEqual(
            select_eligible_recipients(snapshot, 2),
            ['+919876543210', '+919876543211'],
        )
        self.assertEqual(select_eligible_recipients(snapshot), snapshot['eligible_recipients'])
        self.assertEqual(select_eligible_recipients(snapshot, 'all'), snapshot['eligible_recipients'])

    def test_empty_pool_and_invalid_quantities(self):
        snapshot = build_recipient_eligibility(self.conn, self.task([]))
        self.assertEqual(snapshot['source_recipient_count'], 0)
        self.assertEqual(select_eligible_recipients(snapshot), [])
        for quantity in (0, -1, True, 1, '2'):
            with self.subTest(quantity=quantity), self.assertRaises(ValueError):
                select_eligible_recipients(snapshot, quantity)

    def test_missing_task_is_rejected(self):
        with self.assertRaises(ValueError):
            build_recipient_eligibility(self.conn, 999)

    def test_opt_out_table_is_authoritative_and_fail_closed_when_missing(self):
        empty = build_recipient_eligibility(self.conn, self.task(['919876543210']))
        self.assertEqual(empty['eligible_recipients'], ['+919876543210'])
        self.conn.execute(
            "INSERT INTO whatsapp_opt_outs (phone_number) VALUES (?)",
            ('+919876543210',),
        )
        self.conn.commit()
        opted_out = build_recipient_eligibility(self.conn, self.task(['919876543210']))
        self.assertEqual(opted_out['eligible_recipients'], [])
        self.assertEqual(opted_out['exclusion_counts']['opted_out'], 1)
        self.conn.execute('DROP TABLE whatsapp_opt_outs')
        with self.assertRaisesRegex(ValueError, 'opt-out data is unavailable'):
            build_recipient_eligibility(self.conn, self.task(['919876543210']))

    def test_opted_out_declared_recipient_is_excluded_before_plan_claim(self):
        task = self.task(['919876543210', '919876543211'])
        self.conn.execute(
            "INSERT INTO whatsapp_opt_outs (phone_number) VALUES (?)",
            ('919876543211',),
        )
        self.conn.commit()
        snapshot = build_recipient_eligibility(self.conn, task)
        self.assertEqual(snapshot['eligible_recipients'], ['+919876543210'])
        plan = create_campaign_send_plan(
            self.conn, task, 7, 'plan-optout-filter-001', 'all', 'template:v1'
        )
        rows = self.conn.execute(
            'SELECT recipient_number FROM whatsapp_campaign_plan_recipients WHERE plan_id = ?',
            (plan['plan_id'],),
        ).fetchall()
        self.assertEqual([row['recipient_number'] for row in rows], ['+919876543210'])
        self.assertEqual(
            self.conn.execute(
                'SELECT recipient_number FROM whatsapp_campaign_recipient_claims WHERE plan_id = ?',
                (plan['plan_id'],),
            ).fetchone()['recipient_number'],
            '+919876543210',
        )

    def test_plan_is_deterministic_and_idempotent(self):
        task = self.task(['919876543210', '919876543211', '919876543212'])
        first = create_campaign_send_plan(
            self.conn, task, 7, 'plan-001', 2, 'template:welcome-v1'
        )
        second = create_campaign_send_plan(
            self.conn, task, 7, 'plan-001', 2, 'template:welcome-v1'
        )
        self.assertTrue(first['created'])
        self.assertTrue(second['idempotent'])
        self.assertEqual(first['plan_id'], second['plan_id'])
        rows = self.conn.execute('''
            SELECT id, recipient_number, position, idempotency_key
            FROM whatsapp_campaign_plan_recipients ORDER BY position
        ''').fetchall()
        self.assertEqual([row['recipient_number'] for row in rows], [
            '+919876543210', '+919876543211'
        ])
        for row in rows:
            self.assertEqual(
                row['idempotency_key'],
                campaign_plan_recipient_idempotency_key(first['plan_id'], row['id']),
            )

    def test_active_claims_block_overlapping_same_campaign_plans(self):
        task = self.task(['919876543210', '919876543211'])
        first = create_campaign_send_plan(self.conn, task, 7, 'plan-claims-001', 'all', 'template:v1')
        with self.assertRaises(ValueError):
            create_campaign_send_plan(self.conn, task, 8, 'plan-claims-002', 1, 'template:v1')
        self.assertEqual(
            self.conn.execute('SELECT COUNT(*) FROM whatsapp_campaign_plans').fetchone()[0], 1
        )
        self.assertEqual(
            self.conn.execute('''
                SELECT COUNT(*) FROM whatsapp_campaign_recipient_claims
                WHERE plan_id = ? AND state = 'claimed'
            ''', (first['plan_id'],)).fetchone()[0], 2
        )

    def test_cross_campaign_claim_reuse_remains_allowed(self):
        first = create_campaign_send_plan(
            self.conn, self.task(['919876543210'], campaign_id=17), 7,
            'plan-cross-001', 1, 'template:v1'
        )
        second = create_campaign_send_plan(
            self.conn, self.task(['919876543210'], campaign_id=18), 8,
            'plan-cross-002', 1, 'template:v1'
        )
        self.assertNotEqual(first['plan_id'], second['plan_id'])
        self.assertEqual(
            self.conn.execute('SELECT COUNT(*) FROM whatsapp_campaign_recipient_claims').fetchone()[0], 2
        )

    def test_supported_plan_quantities_and_backend_limit(self):
        numbers = [f'9198765{index:05d}' for index in range(80)]
        for quantity in (20, 50, 80):
            with self.subTest(quantity=quantity):
                result = create_campaign_send_plan(
                    self.conn, self.task(numbers, campaign_id=100 + quantity), 7,
                    f'plan-quantity-{quantity}', quantity, 'template:v1'
                )
                self.assertEqual(result['recipient_count'], quantity)
        snapshot = build_recipient_eligibility(self.conn, self.task(numbers, campaign_id=200))
        for quantity in (0, -1, True, 81, '81'):
            with self.subTest(quantity=quantity), self.assertRaises(ValueError):
                select_eligible_recipients(snapshot, quantity)

    def test_claim_insertion_failure_rolls_back_plan_and_snapshot(self):
        task = self.task(['919876543210', '919876543211'])
        self.conn.execute('''
            CREATE TRIGGER fail_campaign_claim_insert
            BEFORE INSERT ON whatsapp_campaign_recipient_claims
            BEGIN SELECT RAISE(ABORT, 'simulated claim failure'); END;
        ''')
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            create_campaign_send_plan(self.conn, task, 7, 'plan-claims-rollback', 'all', 'template:v1')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM whatsapp_campaign_plans').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM whatsapp_campaign_plan_recipients').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM whatsapp_campaign_recipient_claims').fetchone()[0], 0)

    def test_only_explicitly_cancelled_plans_release_owned_claims(self):
        task = self.task(['919876543210'])
        first = create_campaign_send_plan(self.conn, task, 7, 'plan-release-001', 1, 'template:v1')
        with self.assertRaises(ValueError):
            release_campaign_send_plan_claims(self.conn, first['plan_id'])
        self.conn.execute("UPDATE whatsapp_campaign_plans SET state = 'cancelled' WHERE id = ?", (first['plan_id'],))
        self.conn.commit()
        with self.assertRaises(ValueError):
            release_campaign_send_plan_claims(self.conn, first['plan_id'])
        self.conn.execute('''
            UPDATE whatsapp_campaign_plans
            SET cancellation_state = 'cancelled', cancellation_evidence_status = 'absent'
            WHERE id = ?
        ''', (first['plan_id'],))
        self.conn.commit()
        self.assertEqual(release_campaign_send_plan_claims(self.conn, first['plan_id'])['released_count'], 1)
        second = create_campaign_send_plan(self.conn, task, 8, 'plan-release-002', 1, 'template:v1')
        self.assertEqual(release_campaign_send_plan_claims(self.conn, first['plan_id'])['released_count'], 0)
        self.assertEqual(
            self.conn.execute('''
                SELECT state FROM whatsapp_campaign_recipient_claims
                WHERE plan_id = ?
            ''', (second['plan_id'],)).fetchone()['state'], 'claimed'
        )

    def test_cancellation_requires_owner_and_validated_absence(self):
        plan = self._create_batch_plan(plan_key='cancel-owner')
        calls = []

        def adapter(operation, payload):
            calls.append(operation)
            return {'success': True, 'status': 'absent', 'batch_id': plan['batch_id']}, 200

        with self.assertRaises(PermissionError):
            self._cancel_as_worker(self.conn, plan['plan_id'], node_call=adapter, worker_id=8)
        self.assertEqual(calls, [])
        result = self._cancel_as_worker(self.conn, plan['plan_id'], node_call=adapter, now='2026-01-01 00:00:00')
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['released_count'], 2)
        self.assertEqual(
            self.conn.execute('SELECT COUNT(*) FROM whatsapp_campaign_recipient_claims WHERE state = "claimed"').fetchone()[0], 0
        )

    def test_cancellation_failure_evidence_retains_claims(self):
        for index, (evidence_status, response, status) in enumerate((
            ('present', {'success': True, 'status': 'present', 'batch_id': 'batch-plan'}, 200),
            ('invalid', {'success': True, 'status': 'invalid', 'batch_id': 'batch-plan'}, 200),
            ('unavailable', {'success': False, 'status': 'unavailable'}, 503),
            ('invalid', {'success': True, 'status': 'absent', 'batch_id': 'wrong-batch'}, 200),
            ('invalid', {'success': True, 'status': 'absent'}, 200),
        )):
            with self.subTest(evidence_status=evidence_status, response=response):
                plan = self._create_batch_plan(plan_key=f'cancel-{index}', campaign_id=800 + index)
                def adapter(operation, payload, response=response, status=status):
                    return response, status
                result = self._cancel_as_worker(self.conn, plan['plan_id'], node_call=adapter)
                self.assertEqual(result['status'], 'cancellation_blocked')
                self.assertEqual(
                    self.conn.execute('SELECT COUNT(*) FROM whatsapp_campaign_recipient_claims WHERE plan_id = ? AND state = "claimed"', (plan['plan_id'],)).fetchone()[0],
                    plan['recipient_count'],
                )

    def test_present_cancellation_evidence_requires_matching_server_fingerprint(self):
        for index, (suffix, fingerprint, expected) in enumerate((
            ('missing', None, 'invalid'),
            ('mismatch', '0' * 64, 'invalid'),
        ), start=1):
            with self.subTest(suffix=suffix):
                plan = self._create_batch_plan(plan_key=f'cancel-fingerprint-{suffix}', campaign_id=850 + index)
                response = {'success': True, 'status': 'present', 'batch_id': plan['batch_id']}
                if fingerprint is not None:
                    response['request_fingerprint'] = fingerprint
                result = self._cancel_as_worker(
                    self.conn, plan['plan_id'],
                    node_call=lambda *_args, response=response: (response, 200),
                )
                self.assertEqual(result['status'], 'cancellation_blocked')
                self.assertEqual(result['evidence_status'], expected)

        plan = self._create_batch_plan(plan_key='cancel-fingerprint-match', campaign_id=853)
        response = {
            'success': True,
            'status': 'present',
            'batch_id': plan['batch_id'],
            'request_fingerprint': plan['batch_request_fingerprint'],
        }
        result = self._cancel_as_worker(
            self.conn, plan['plan_id'],
            node_call=lambda *_args: (response, 200),
        )
        self.assertEqual(result['status'], 'cancellation_blocked')
        self.assertEqual(result['evidence_status'], 'present')

    def test_repeated_cancellation_is_idempotent_and_reservation_is_rejected(self):
        plan = self._create_batch_plan(plan_key='cancel-repeat')
        adapter = lambda operation, payload: ({'success': True, 'status': 'absent', 'batch_id': plan['batch_id']}, 200)
        first = self._cancel_as_worker(self.conn, plan['plan_id'], node_call=adapter)
        second = self._cancel_as_worker(self.conn, plan['plan_id'], node_call=adapter)
        self.assertEqual(first['status'], 'cancelled')
        self.assertTrue(second['idempotent'])
        blocked = self._reserve_as_worker(self.conn, plan['plan_id'], node_call=adapter)
        self.assertEqual(blocked['status'], 'cancelled')

    def test_cancellation_race_with_reservation_and_projection_failure_retains_claims(self):
        plan = self._create_batch_plan(plan_key='cancel-race')
        reservation_result = []

        def adapter(operation, payload):
            reservation_result.append(self._reserve_as_worker(self.conn, plan['plan_id'], node_call=lambda *_: ({}, 503)))
            row = self.conn.execute('SELECT id FROM whatsapp_campaign_plan_recipients WHERE plan_id = ? LIMIT 1', (plan['plan_id'],)).fetchone()
            self.conn.execute('UPDATE whatsapp_campaign_plan_recipients SET state = "reserved", ledger_message_id = "late-ledger" WHERE id = ?', (row['id'],))
            self.conn.commit()
            return {'success': True, 'status': 'absent', 'batch_id': plan['batch_id']}, 200

        result = self._cancel_as_worker(self.conn, plan['plan_id'], node_call=adapter)
        self.assertEqual(result['status'], 'cancellation_blocked')
        self.assertEqual(reservation_result[0]['status'], 'cancellation_pending')
        self.assertEqual(
            self.conn.execute('SELECT COUNT(*) FROM whatsapp_campaign_recipient_claims WHERE plan_id = ? AND state = "claimed"', (plan['plan_id'],)).fetchone()[0],
            plan['recipient_count'],
        )

    def test_reservation_started_before_cancellation_blocks_release_until_outcome(self):
        plan = self._create_batch_plan(plan_key='cancel-inflight')
        with tempfile.TemporaryDirectory(prefix='taskpe-cancel-race-', dir=os.getcwd()) as directory:
            path = os.path.join(directory, 'race.sqlite3')
            target = sqlite3.connect(path)
            self.conn.backup(target)
            target.close()
            first = sqlite3.connect(path, check_same_thread=False)
            second = sqlite3.connect(path, check_same_thread=False)
            first.row_factory = sqlite3.Row
            second.row_factory = sqlite3.Row
            entered = threading.Event()
            release = threading.Event()
            results = []

            def adapter(operation, payload):
                entered.set()
                self.assertTrue(release.wait(timeout=10))
                return self._node_batch_response(payload), 200

            def reserve():
                with flask_app.test_request_context('/'):
                    session['user_id'] = 7
                    results.append(reserve_campaign_send_plan_with_node(
                        first, plan['plan_id'], node_call=adapter,
                        now='2026-01-01 00:00:00'
                    ))

            thread = threading.Thread(target=reserve)
            thread.start()
            self.assertTrue(entered.wait(timeout=10))
            cancellation = self._cancel_as_worker(
                second, plan['plan_id'],
                node_call=lambda operation, payload: ({'success': True, 'status': 'absent', 'batch_id': plan['batch_id']}, 200),
                now='2026-01-01 00:00:01',
            )
            self.assertEqual(cancellation['status'], 'cancellation_blocked')
            self.assertEqual(
                second.execute('SELECT COUNT(*) FROM whatsapp_campaign_recipient_claims WHERE plan_id = ? AND state = "claimed"', (plan['plan_id'],)).fetchone()[0],
                plan['recipient_count'],
            )
            release.set()
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(results[0]['status'], 'reserved')
            first.close()
            second.close()

    def test_cancellation_recovery_reuses_same_batch_identity_after_crash(self):
        plan = self._create_batch_plan(plan_key='cancel-recovery')
        self.conn.execute('''
            UPDATE whatsapp_campaign_plans
            SET cancellation_state = 'checking', cancellation_requested_at = '2026-01-01 00:00:00'
            WHERE id = ?
        ''', (plan['plan_id'],))
        self.conn.commit()
        def adapter(operation, payload):
            return {'success': True, 'status': 'absent', 'batch_id': plan['batch_id']}, 200

        recovered = self._cancel_as_worker(
            self.conn, plan['plan_id'], node_call=adapter,
            now='2026-01-01 00:01:00', recover=True,
        )
        self.assertEqual(recovered['status'], 'cancelled')
        persisted = self.conn.execute(
            'SELECT batch_id, batch_request_fingerprint FROM whatsapp_campaign_plans WHERE id = ?',
            (plan['plan_id'],),
        ).fetchone()
        self.assertEqual(persisted['batch_id'], plan['batch_id'])
        self.assertEqual(persisted['batch_request_fingerprint'], plan['batch_request_fingerprint'])

    def test_concurrent_cancellation_attempts_are_idempotent(self):
        plan = self._create_batch_plan(plan_key='cancel-concurrent')
        with tempfile.TemporaryDirectory(prefix='taskpe-cancel-concurrent-', dir=os.getcwd()) as directory:
            path = os.path.join(directory, 'concurrent.sqlite3')
            target = sqlite3.connect(path)
            self.conn.backup(target)
            target.close()
            first = sqlite3.connect(path, check_same_thread=False)
            second = sqlite3.connect(path, check_same_thread=False)
            first.row_factory = sqlite3.Row
            second.row_factory = sqlite3.Row
            entered = threading.Event()
            release = threading.Event()
            results = []

            def first_adapter(operation, payload):
                entered.set()
                self.assertTrue(release.wait(timeout=10))
                return {'success': True, 'status': 'absent', 'batch_id': plan['batch_id']}, 200

            def run_first():
                with flask_app.test_request_context('/'):
                    session['user_id'] = 7
                    results.append(cancel_campaign_send_plan(first, plan['plan_id'], node_call=first_adapter))

            thread = threading.Thread(target=run_first)
            thread.start()
            self.assertTrue(entered.wait(timeout=10))
            second_result = self._cancel_as_worker(
                second, plan['plan_id'],
                node_call=lambda *_: ({'success': True, 'status': 'absent', 'batch_id': plan['batch_id']}, 200),
            )
            self.assertEqual(second_result['status'], 'cancellation_pending')
            release.set()
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(results[0]['status'], 'cancelled')
            self.assertEqual(
                second.execute('SELECT COUNT(*) FROM whatsapp_campaign_recipient_claims WHERE plan_id = ? AND state = "released"', (plan['plan_id'],)).fetchone()[0],
                plan['recipient_count'],
            )
            first.close()
            second.close()

    def test_single_recipient_reservation_and_recovery_respect_cancellation(self):
        plan = self._create_batch_plan(plan_key='cancel-single')
        recipient_id = self._plan_recipient_id('cancel-single')
        self.conn.execute('UPDATE whatsapp_campaign_plans SET cancellation_state = "checking" WHERE id = ?', (plan['plan_id'],))
        self.conn.commit()
        calls = []
        reserve = reserve_campaign_plan_recipient(
            self.conn, recipient_id,
            lambda payload: calls.append(payload), now='2026-01-01 00:00:00'
        )
        recover = recover_campaign_plan_recipient(
            self.conn, recipient_id,
            lambda key: calls.append(key), now='2026-01-01 00:01:00'
        )
        self.assertEqual(reserve['status'], 'cancellation_pending')
        self.assertEqual(recover['status'], 'cancellation_pending')
        self.assertEqual(calls, [])

    def test_cancellation_claim_release_rolls_back_on_database_failure(self):
        plan = self._create_batch_plan(plan_key='cancel-rollback')
        self.conn.execute('''
            CREATE TRIGGER fail_cancel_claim_release
            BEFORE UPDATE OF state ON whatsapp_campaign_recipient_claims
            WHEN NEW.state = 'released'
            BEGIN SELECT RAISE(ABORT, 'simulated cancellation failure'); END;
        ''')
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self._cancel_as_worker(
                self.conn, plan['plan_id'],
                node_call=lambda operation, payload: ({'success': True, 'status': 'absent', 'batch_id': plan['batch_id']}, 200),
            )
        state_row = self.conn.execute(
            'SELECT state, cancellation_state FROM whatsapp_campaign_plans WHERE id = ?',
            (plan['plan_id'],),
        ).fetchone()
        self.assertEqual(state_row['state'], 'ready')
        self.assertEqual(state_row['cancellation_state'], 'checking')
        self.assertEqual(
            self.conn.execute('SELECT COUNT(*) FROM whatsapp_campaign_recipient_claims WHERE plan_id = ? AND state = "claimed"', (plan['plan_id'],)).fetchone()[0],
            plan['recipient_count'],
        )

    def test_adapter_receives_exact_stored_canonical_key(self):
        task = self.task(['919876543210'])
        plan = create_campaign_send_plan(self.conn, task, 7, 'plan-key-match', 1, 'template:v1')
        recipient_id = self._plan_recipient_id('plan-key-match')
        stored = self.conn.execute(
            'SELECT idempotency_key FROM whatsapp_campaign_plan_recipients WHERE id = ?',
            (recipient_id,),
        ).fetchone()['idempotency_key']
        received = []

        def adapter(input_data):
            received.append(input_data['idempotencyKey'])
            return {'record': {'message_id': input_data['messageId'], 'state': 'queued'}}

        result = reserve_campaign_plan_recipient(self.conn, recipient_id, adapter, now='2026-01-01 00:00:00')
        self.assertEqual(result['status'], 'reserved')
        self.assertEqual(received, [stored])
        self.assertEqual(len(stored), 64)
        self.assertIsNotNone(plan['plan_id'])

    def test_plan_isolation_and_key_mismatch_are_rejected(self):
        task = self.task(['919876543210', '919876543211'])
        create_campaign_send_plan(self.conn, task, 7, 'plan-002', 'all', 'template:v1')
        with self.assertRaises(ValueError):
            create_campaign_send_plan(self.conn, task, 7, 'plan-002', 1, 'template:v1')
        create_campaign_send_plan(self.conn, self.task(['919876543210', '919876543211'], campaign_id=18), 8, 'plan-003', 1, 'template:v1')
        self.assertEqual(
            [tuple(row) for row in self.conn.execute('''
                SELECT sender_worker_id, campaign_id
                FROM whatsapp_campaign_plans ORDER BY id
            ''').fetchall()],
            [(7, 17), (8, 18)],
        )
        self.assertEqual(
            self.conn.execute('SELECT COUNT(*) FROM whatsapp_campaign_plans').fetchone()[0], 2
        )

    def test_plan_rolls_back_if_recipient_insert_fails(self):
        task = self.task(['919876543210', '919876543211'])
        self.conn.execute('''
            CREATE TRIGGER fail_plan_recipient_insert
            BEFORE INSERT ON whatsapp_campaign_plan_recipients
            BEGIN SELECT RAISE(ABORT, 'simulated insert failure'); END;
        ''')
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            create_campaign_send_plan(self.conn, task, 7, 'plan-004', 'all', 'template:v1')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM whatsapp_campaign_plans').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM whatsapp_campaign_plan_recipients').fetchone()[0], 0)

    def test_invalid_plan_selection_and_content_are_rejected(self):
        task = self.task(['919876543210'])
        for quantity in (0, 2, -1):
            with self.subTest(quantity=quantity), self.assertRaises(ValueError):
                create_campaign_send_plan(self.conn, task, 7, f'plan-{quantity}', quantity, 'template:v1')
        for reference in ('', 'bad\x00reference', 'x' * 501):
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                create_campaign_send_plan(self.conn, task, 7, 'plan-005', 1, reference)

    def test_all_selection_uses_only_current_campaign_eligibility(self):
        task = self.task(['919876543210', '919876543211'], campaign_id=17)
        self.conn.execute(
            'INSERT INTO recipient_allocations VALUES (?, ?, ?)',
            ('919876543210', 99, 'COMPLETED'),
        )
        self.conn.commit()
        result = create_campaign_send_plan(self.conn, task, 7, 'plan-006', 'all', 'template:v1')
        self.assertEqual(result['recipient_count'], 2)

    def _plan_recipient_id(self, plan_key, position=1):
        return self.conn.execute('''
            SELECT pr.id FROM whatsapp_campaign_plan_recipients pr
            JOIN whatsapp_campaign_plans p ON p.id = pr.plan_id
            WHERE p.plan_key = ? AND pr.position = ?
        ''', (plan_key, position)).fetchone()[0]

    def test_idempotency_key_is_stable_and_fixed_length(self):
        first = campaign_plan_recipient_idempotency_key(12, 34)
        self.assertEqual(first, campaign_plan_recipient_idempotency_key(12, 34))
        self.assertEqual(len(first), 64)
        self.assertNotEqual(first, campaign_plan_recipient_idempotency_key(12, 35))

    def test_crash_before_node_reservation_recovers_to_planned(self):
        task = self.task(['919876543210'])
        create_campaign_send_plan(self.conn, task, 7, 'plan-007', 1, 'template:v1')
        recipient_id = self._plan_recipient_id('plan-007')

        def failed_reservation(_input):
            raise RuntimeError('simulated process failure before node reservation')

        result = reserve_campaign_plan_recipient(
            self.conn, recipient_id, failed_reservation,
            now='2026-01-01 00:00:00', lease_seconds=60,
        )
        self.assertEqual(result['status'], 'reserving')
        recovered = recover_campaign_plan_recipient(
            self.conn, recipient_id, lambda _key: None,
            now='2026-01-01 00:02:00',
        )
        self.assertEqual(recovered['status'], 'planned')
        self.assertEqual(
            self.conn.execute('''
                SELECT state FROM whatsapp_campaign_recipient_claims
                WHERE plan_recipient_id = ?
            ''', (recipient_id,)).fetchone()['state'], 'claimed'
        )

    def test_ledger_lookup_failure_is_not_treated_as_absence(self):
        task = self.task(['919876543210'])
        create_campaign_send_plan(self.conn, task, 7, 'plan-lookup-failure', 1, 'template:v1')
        recipient_id = self._plan_recipient_id('plan-lookup-failure')

        reserve_campaign_plan_recipient(
            self.conn, recipient_id, lambda _input: (_ for _ in ()).throw(RuntimeError('node unavailable')),
            now='2026-01-01 00:00:00', lease_seconds=60,
        )
        recovered = recover_campaign_plan_recipient(
            self.conn, recipient_id,
            lambda _key: (_ for _ in ()).throw(RuntimeError('ledger unavailable')),
            now='2026-01-01 00:02:00',
        )
        self.assertEqual(recovered['status'], 'reserving')
        self.assertEqual(recovered['error_code'], 'LEDGER_LOOKUP_UNAVAILABLE')
        row = self.conn.execute(
            'SELECT state, ledger_message_id FROM whatsapp_campaign_plan_recipients WHERE id = ?',
            (recipient_id,),
        ).fetchone()
        self.assertEqual(tuple(row), ('reserving', None))

    def test_crash_after_node_reservation_projects_existing_row_once(self):
        task = self.task(['919876543210'])
        create_campaign_send_plan(self.conn, task, 7, 'plan-008', 1, 'template:v1')
        recipient_id = self._plan_recipient_id('plan-008')
        with tempfile.TemporaryDirectory(prefix='taskpe-ledger-', dir=os.getcwd()) as directory:
            ledger_path = os.path.join(directory, 'ledger.sqlite3')
            ledger = sqlite3.connect(ledger_path)
            ledger.execute('CREATE TABLE messages (id TEXT PRIMARY KEY, key TEXT UNIQUE, state TEXT)')
            ledger.execute('CREATE TABLE quota (count INTEGER NOT NULL)')
            ledger.execute('INSERT INTO quota VALUES (0)')
            ledger.commit()

            def reserve_then_crash(input_data):
                existing = ledger.execute('SELECT id, state FROM messages WHERE key = ?', (input_data['idempotencyKey'],)).fetchone()
                if not existing:
                    message_id = input_data['messageId']
                    ledger.execute('INSERT INTO messages VALUES (?, ?, ?)', (message_id, input_data['idempotencyKey'], 'queued'))
                    ledger.execute('UPDATE quota SET count = count + 1')
                    ledger.commit()
                raise RuntimeError('simulated crash after node commit')

            result = reserve_campaign_plan_recipient(
                self.conn, recipient_id, reserve_then_crash,
                now='2026-01-01 00:00:00', lease_seconds=60,
            )
            self.assertEqual(result['status'], 'reserving')
            key = campaign_plan_recipient_idempotency_key(1, recipient_id)

            def lookup(lookup_key):
                row = ledger.execute('SELECT id, state FROM messages WHERE key = ?', (lookup_key,)).fetchone()
                return {'message_id': row[0], 'state': row[1]} if row else None

            recovered = recover_campaign_plan_recipient(
                self.conn, recipient_id, lookup, now='2026-01-01 00:02:00'
            )
            self.assertEqual(recovered['status'], 'reserved')
            self.assertEqual(ledger.execute('SELECT count FROM quota').fetchone()[0], 1)
            repeated = recover_campaign_plan_recipient(
                self.conn, recipient_id, lambda _key: (_ for _ in ()).throw(AssertionError('lookup repeated')),
                now='2026-01-01 00:03:00',
            )
            self.assertEqual(repeated['status'], 'reserved')
            ledger.close()

    def test_competing_claim_cannot_call_second_reservation(self):
        task = self.task(['919876543210'])
        create_campaign_send_plan(self.conn, task, 7, 'plan-009', 1, 'template:v1')
        recipient_id = self._plan_recipient_id('plan-009')
        with tempfile.TemporaryDirectory(prefix='taskpe-claims-', dir=os.getcwd()) as directory:
            path = os.path.join(directory, 'plans.sqlite3')
            copy = sqlite3.connect(path)
            self.conn.backup(copy)
            copy.close()
            first = sqlite3.connect(path)
            second = sqlite3.connect(path)
            first.row_factory = sqlite3.Row
            second.row_factory = sqlite3.Row
            calls = []

            def first_failure(_input):
                calls.append('first')
                raise RuntimeError('first process stopped')

            reserve_campaign_plan_recipient(first, recipient_id, first_failure, now='2026-01-01 00:00:00', lease_seconds=60)
            result = reserve_campaign_plan_recipient(
                second, recipient_id, lambda _input: calls.append('second'),
                now='2026-01-01 00:00:30', lease_seconds=60,
            )
            self.assertEqual(result['status'], 'reserving')
            self.assertEqual(calls, ['first'])
            first.close()
            second.close()

    def test_quota_exhaustion_is_persisted_without_socket_activity(self):
        task = self.task(['919876543210'])
        create_campaign_send_plan(self.conn, task, 7, 'plan-010', 1, 'template:v1')
        recipient_id = self._plan_recipient_id('plan-010')
        calls = []

        class QuotaExceeded(Exception):
            code = 'QUOTA_EXCEEDED'

        def quota_failure(_input):
            calls.append('reservation')
            raise QuotaExceeded()

        result = reserve_campaign_plan_recipient(self.conn, recipient_id, quota_failure, now='2026-01-01 00:00:00')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['failure_code'], 'QUOTA_EXCEEDED')
        self.assertEqual(calls, ['reservation'])
        row = self.conn.execute('SELECT state, failure_code FROM whatsapp_campaign_plan_recipients WHERE id = ?', (recipient_id,)).fetchone()
        self.assertEqual(tuple(row), ('failed', 'QUOTA_EXCEEDED'))

    def test_existing_ledger_states_recover_without_socket_calls(self):
        states = ('queued', 'attempted', 'acknowledged', 'failed', 'delivered_unknown')
        task = self.task([f'9198765432{index:02d}' for index in range(len(states))])
        create_campaign_send_plan(self.conn, task, 7, 'plan-011', 'all', 'template:v1')
        rows = self.conn.execute('SELECT id FROM whatsapp_campaign_plan_recipients ORDER BY position').fetchall()
        for row, state in zip(rows, states):
            self.conn.execute('''
                UPDATE whatsapp_campaign_plan_recipients
                SET state = 'reserving', reservation_claim_id = ?,
                    reservation_lease_until = ?, ledger_message_id = NULL
                WHERE id = ?
            ''', (f'claim-{state}', '2026-01-01 00:01:00', row[0]))
        self.conn.commit()
        lookup_calls = []

        def lookup(key):
            lookup_calls.append(key)
            index = len(lookup_calls) - 1
            return {'message_id': f'ledger-{index}', 'state': states[index]}

        for row in rows:
            result = recover_campaign_plan_recipient(self.conn, row[0], lookup, now='2026-01-01 00:02:00')
            self.assertIn(result['status'], {'reserved', 'attempted', 'acknowledged', 'failed', 'delivered_unknown'})
        self.assertEqual(len(lookup_calls), len(states))

    def test_mismatched_ledger_id_cannot_overwrite_stale_projection(self):
        task = self.task(['919876543210'])
        create_campaign_send_plan(self.conn, task, 7, 'plan-012', 1, 'template:v1')
        recipient_id = self._plan_recipient_id('plan-012')
        self.conn.execute('''
            UPDATE whatsapp_campaign_plan_recipients
            SET state = 'reserving', reservation_claim_id = 'old-claim',
                reservation_lease_until = '2026-01-01 00:01:00', ledger_message_id = 'ledger-old'
            WHERE id = ?
        ''', (recipient_id,))
        self.conn.commit()
        result = recover_campaign_plan_recipient(
            self.conn, recipient_id,
            lambda _key: {'message_id': 'ledger-new', 'state': 'queued'},
            now='2026-01-01 00:02:00',
        )
        self.assertEqual(result['status'], 'reserving')
        row = self.conn.execute('SELECT state, ledger_message_id FROM whatsapp_campaign_plan_recipients WHERE id = ?', (recipient_id,)).fetchone()
        self.assertEqual(tuple(row), ('reserving', 'ledger-old'))

    def test_invalid_ledger_records_fail_closed_without_projection(self):
        records = (
            ('empty-dict', {}, None),
            ('non-dict', 'invalid-record', 'INVALID_LEDGER_RECORD'),
            ('unsupported-state', {'message_id': 'ledger-unsupported', 'state': 'sending'}, None),
            ('malformed-id', {'message_id': 'not valid', 'state': 'queued'}, None),
        )
        for index, (suffix, record, expected_error_code) in enumerate(records):
            with self.subTest(record=suffix):
                task = self.task(['919876543210'], campaign_id=300 + index)
                plan_key = f'plan-invalid-ledger-{suffix}'
                create_campaign_send_plan(self.conn, task, 7, plan_key, 1, 'template:v1')
                recipient_id = self._plan_recipient_id(plan_key)
                self.conn.execute('''
                    UPDATE whatsapp_campaign_plan_recipients
                    SET state = 'reserving', reservation_claim_id = ?,
                        reservation_lease_until = ?
                    WHERE id = ?
                ''', (f'claim-{suffix}', '2026-01-01 00:01:00', recipient_id))
                self.conn.commit()

                result = recover_campaign_plan_recipient(
                    self.conn, recipient_id, lambda _key, value=record: value,
                    now='2026-01-01 00:02:00',
                )
                self.assertEqual(result['status'], 'reserving')
                self.assertTrue(result['recovery_required'])
                if expected_error_code:
                    self.assertEqual(result['error_code'], expected_error_code)
                row = self.conn.execute('''
                    SELECT state, ledger_message_id
                    FROM whatsapp_campaign_plan_recipients WHERE id = ?
                ''', (recipient_id,)).fetchone()
                self.assertEqual(tuple(row), ('reserving', None))

    def test_malformed_adapter_ledger_id_does_not_project(self):
        task = self.task(['919876543210'])
        create_campaign_send_plan(self.conn, task, 7, 'plan-bad-ledger-id', 1, 'template:v1')
        recipient_id = self._plan_recipient_id('plan-bad-ledger-id')
        result = reserve_campaign_plan_recipient(
            self.conn, recipient_id,
            lambda _input: {'record': {'message_id': 'not valid', 'state': 'queued'}},
            now='2026-01-01 00:00:00',
        )
        self.assertEqual(result['status'], 'reserving')
        row = self.conn.execute(
            'SELECT state, ledger_message_id FROM whatsapp_campaign_plan_recipients WHERE id = ?',
            (recipient_id,),
        ).fetchone()
        self.assertEqual(tuple(row), ('reserving', None))

    def test_backfill_is_idempotent_and_preserves_phase2_rows(self):
        self.conn.execute('''
            INSERT INTO whatsapp_campaign_plans
                (id, plan_key, request_fingerprint, task_id, campaign_id,
                 sender_worker_id, content_reference, recipient_count, state)
            VALUES (50, 'legacy-plan', 'fingerprint', 4, 17, 7, 'template:v1', 1, 'ready')
        ''')
        self.conn.execute('''
            INSERT INTO whatsapp_campaign_plan_recipients
                (plan_id, recipient_number, position, idempotency_key, state)
            VALUES (50, '919876543210', 1, 'legacy-plan:1', 'reserved')
        ''')
        row = self.conn.execute('''
            SELECT id, position, recipient_number, state, idempotency_key
            FROM whatsapp_campaign_plan_recipients WHERE plan_id = 50
        ''').fetchone()
        with tempfile.TemporaryDirectory(prefix='taskpe-empty-ledger-', dir=os.getcwd()) as directory:
            ledger_path = os.path.join(directory, 'ledger.sqlite3')
            ledger = sqlite3.connect(ledger_path)
            ledger.execute('CREATE TABLE outgoing_messages (message_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE)')
            ledger.commit()
            ledger.close()
            previous = os.environ.get('TASKPE_WA_LEDGER_DB_PATH')
            os.environ['TASKPE_WA_LEDGER_DB_PATH'] = ledger_path
            try:
                database.migrate_whatsapp_campaign_plan_identity(self.conn)
                database.migrate_whatsapp_campaign_plan_identity(self.conn)
            finally:
                if previous is None:
                    os.environ.pop('TASKPE_WA_LEDGER_DB_PATH', None)
                else:
                    os.environ['TASKPE_WA_LEDGER_DB_PATH'] = previous
        migrated = self.conn.execute('''
            SELECT id, position, recipient_number, state, idempotency_key
            FROM whatsapp_campaign_plan_recipients WHERE plan_id = 50
        ''').fetchone()
        expected = campaign_plan_recipient_idempotency_key(50, row['id'])
        self.assertEqual(tuple(migrated[:4]), tuple(row[:4]))
        self.assertEqual(migrated['idempotency_key'], expected)
        self.assertEqual(
            self.conn.execute('SELECT idempotency_key FROM whatsapp_campaign_plan_recipients WHERE plan_id = 50').fetchone()[0],
            expected,
        )

    def _insert_legacy_row(self, plan_id, recipient_id, key):
        self.conn.execute('''
            INSERT INTO whatsapp_campaign_plans
                (id, plan_key, request_fingerprint, task_id, campaign_id,
                 sender_worker_id, content_reference, recipient_count, state)
            VALUES (?, ?, 'fingerprint', 4, 17, 7, 'template:v1', 1, 'ready')
        ''', (plan_id, f'legacy-{plan_id}'))
        self.conn.execute('''
            INSERT INTO whatsapp_campaign_plan_recipients
                (id, plan_id, recipient_number, position, idempotency_key, state)
            VALUES (?, ?, '919876543210', 1, ?, 'planned')
        ''', (recipient_id, plan_id, key))
        self.conn.commit()

    def test_missing_node_ledger_fails_closed_without_rewriting_legacy_key(self):
        old_key = 'legacy-missing-ledger:1'
        self._insert_legacy_row(70, 170, old_key)
        with tempfile.TemporaryDirectory(prefix='taskpe-missing-ledger-', dir=os.getcwd()) as directory:
            previous = os.environ.get('TASKPE_WA_LEDGER_DB_PATH')
            os.environ['TASKPE_WA_LEDGER_DB_PATH'] = os.path.join(directory, 'missing.sqlite3')
            try:
                with self.assertRaisesRegex(RuntimeError, 'database file is unavailable'):
                    database.migrate_whatsapp_campaign_plan_identity(self.conn)
            finally:
                if previous is None:
                    os.environ.pop('TASKPE_WA_LEDGER_DB_PATH', None)
                else:
                    os.environ['TASKPE_WA_LEDGER_DB_PATH'] = previous
        self.assertEqual(
            self.conn.execute('SELECT idempotency_key FROM whatsapp_campaign_plan_recipients WHERE id = 170').fetchone()[0],
            old_key,
        )

    def test_missing_node_ledger_table_fails_closed_without_rewriting_legacy_key(self):
        old_key = 'legacy-missing-table:1'
        self._insert_legacy_row(71, 171, old_key)
        with tempfile.TemporaryDirectory(prefix='taskpe-missing-table-', dir=os.getcwd()) as directory:
            ledger_path = os.path.join(directory, 'ledger.sqlite3')
            sqlite3.connect(ledger_path).close()
            previous = os.environ.get('TASKPE_WA_LEDGER_DB_PATH')
            os.environ['TASKPE_WA_LEDGER_DB_PATH'] = ledger_path
            try:
                with self.assertRaisesRegex(RuntimeError, 'outgoing_messages table is missing'):
                    database.migrate_whatsapp_campaign_plan_identity(self.conn)
            finally:
                if previous is None:
                    os.environ.pop('TASKPE_WA_LEDGER_DB_PATH', None)
                else:
                    os.environ['TASKPE_WA_LEDGER_DB_PATH'] = previous
        self.assertEqual(
            self.conn.execute('SELECT idempotency_key FROM whatsapp_campaign_plan_recipients WHERE id = 171').fetchone()[0],
            old_key,
        )

    def _create_phase2_database(self, path, object_name=None):
        conn = sqlite3.connect(path)
        conn.executescript('''
            CREATE TABLE whatsapp_campaign_plans (
                id INTEGER PRIMARY KEY, plan_key TEXT NOT NULL UNIQUE,
                request_fingerprint TEXT NOT NULL, task_id INTEGER NOT NULL,
                campaign_id INTEGER NOT NULL, sender_worker_id INTEGER NOT NULL,
                content_reference TEXT NOT NULL, recipient_count INTEGER NOT NULL,
                state TEXT NOT NULL, created_at TEXT
            );
            CREATE TABLE whatsapp_campaign_plan_recipients (
                id INTEGER PRIMARY KEY, plan_id INTEGER NOT NULL,
                recipient_number TEXT NOT NULL, position INTEGER NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE, created_at TEXT
            );
            INSERT INTO whatsapp_campaign_plans VALUES
                (77, 'legacy-init', 'fingerprint', 4, 17, 7, 'template:v1', 1, 'ready', '2026-01-01');
            INSERT INTO whatsapp_campaign_plan_recipients VALUES
                (88, 77, '919876543210', 1, 'legacy-init:1', '2026-01-01');
        ''')
        if object_name:
            conn.execute(f'CREATE TABLE {object_name} (marker INTEGER)')
        conn.commit()
        conn.close()

    def _create_empty_node_ledger(self, directory):
        path = os.path.join(directory, 'ledger.sqlite3')
        conn = sqlite3.connect(path)
        conn.execute('CREATE TABLE outgoing_messages (message_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE)')
        conn.commit()
        conn.close()
        return path

    def test_index_creation_failure_rolls_back_key_backfill(self):
        with tempfile.TemporaryDirectory(prefix='taskpe-index-failure-', dir=os.getcwd()) as directory:
            db_path = os.path.join(directory, 'phase2.sqlite3')
            index_name = 'whatsapp_campaign_plan_recipient_ledger_id_unique'
            self._create_phase2_database(db_path, index_name)
            ledger_path = self._create_empty_node_ledger(directory)
            old_path = database.DB_PATH
            previous = os.environ.get('TASKPE_WA_LEDGER_DB_PATH')
            database.DB_PATH = db_path
            os.environ['TASKPE_WA_LEDGER_DB_PATH'] = ledger_path
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    database.initialize_database()
            finally:
                database.DB_PATH = old_path
                if previous is None:
                    os.environ.pop('TASKPE_WA_LEDGER_DB_PATH', None)
                else:
                    os.environ['TASKPE_WA_LEDGER_DB_PATH'] = previous
            conn = sqlite3.connect(db_path)
            self.assertEqual(
                conn.execute('SELECT idempotency_key FROM whatsapp_campaign_plan_recipients WHERE id = 88').fetchone()[0],
                'legacy-init:1',
            )
            self.assertNotIn('state', {row[1] for row in conn.execute('PRAGMA table_info(whatsapp_campaign_plan_recipients)')})
            conn.close()

    def test_initializer_rolls_back_and_closes_connection_on_failure(self):
        with tempfile.TemporaryDirectory(prefix='taskpe-init-failure-', dir=os.getcwd()) as directory:
            db_path = os.path.join(directory, 'failure.sqlite3')
            tracked = {}

            class TrackingConnection(sqlite3.Connection):
                def close(self):
                    tracked['closed'] = True
                    return super().close()

            def connection_factory():
                conn = sqlite3.connect(db_path, factory=TrackingConnection)
                conn.row_factory = sqlite3.Row
                tracked['connection'] = conn
                return conn

            with patch.object(database, 'get_db_connection', side_effect=connection_factory), \
                    patch.object(database, 'migrate_whatsapp_campaign_plan_identity', side_effect=RuntimeError('injected migration failure')):
                with self.assertRaisesRegex(RuntimeError, 'injected migration failure'):
                    database.initialize_database()
            self.assertTrue(tracked.get('closed'))
            conn = sqlite3.connect(db_path)
            users = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
            conn.close()
            self.assertIsNone(users)

    def test_duplicate_ledger_message_ids_are_rejected_without_rewriting(self):
        task = self.task(['919876543210', '919876543211'])
        create_campaign_send_plan(self.conn, task, 7, 'plan-duplicate-ledger', 'all', 'template:v1')
        self.conn.execute('''
            UPDATE whatsapp_campaign_plan_recipients
            SET ledger_message_id = 'ledger-duplicate' WHERE plan_id = (SELECT id FROM whatsapp_campaign_plans WHERE plan_key = ?)
        ''', ('plan-duplicate-ledger',))
        self.conn.commit()
        before = self.conn.execute('''
            SELECT id, ledger_message_id FROM whatsapp_campaign_plan_recipients ORDER BY id
        ''').fetchall()
        with self.assertRaises(RuntimeError):
            database.migrate_whatsapp_campaign_plan_identity(self.conn)
        after = self.conn.execute('''
            SELECT id, ledger_message_id FROM whatsapp_campaign_plan_recipients ORDER BY id
        ''').fetchall()
        self.assertEqual([tuple(row) for row in before], [tuple(row) for row in after])

    def test_failed_legacy_key_migration_preserves_data_when_ledger_references_old_key(self):
        self.conn.execute('''
            INSERT INTO whatsapp_campaign_plans
                (id, plan_key, request_fingerprint, task_id, campaign_id,
                 sender_worker_id, content_reference, recipient_count, state)
            VALUES (60, 'legacy-referenced', 'fingerprint', 4, 17, 7, 'template:v1', 1, 'ready')
        ''')
        self.conn.execute('''
            INSERT INTO whatsapp_campaign_plan_recipients
                (plan_id, recipient_number, position, idempotency_key, state)
            VALUES (60, '919876543210', 1, 'legacy-referenced:1', 'reserved')
        ''')
        self.conn.commit()
        old_key = 'legacy-referenced:1'
        with tempfile.TemporaryDirectory(prefix='taskpe-ledger-ref-', dir=os.getcwd()) as directory:
            ledger_path = os.path.join(directory, 'ledger.sqlite3')
            ledger = sqlite3.connect(ledger_path)
            ledger.execute('CREATE TABLE outgoing_messages (message_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE)')
            ledger.execute('INSERT INTO outgoing_messages VALUES (?, ?)', ('ledger-old', old_key))
            ledger.commit()
            ledger.close()
            previous = os.environ.get('TASKPE_WA_LEDGER_DB_PATH')
            os.environ['TASKPE_WA_LEDGER_DB_PATH'] = ledger_path
            try:
                with self.assertRaises(RuntimeError):
                    database.migrate_whatsapp_campaign_plan_identity(self.conn)
            finally:
                if previous is None:
                    os.environ.pop('TASKPE_WA_LEDGER_DB_PATH', None)
                else:
                    os.environ['TASKPE_WA_LEDGER_DB_PATH'] = previous
        self.assertEqual(
            self.conn.execute('SELECT idempotency_key FROM whatsapp_campaign_plan_recipients WHERE plan_id = 60').fetchone()[0],
            old_key,
        )

    def test_database_initialization_fresh_and_rerun_install_identity_guard(self):
        with tempfile.TemporaryDirectory(prefix='taskpe-init-', dir=os.getcwd()) as directory:
            old_path = database.DB_PATH
            database.DB_PATH = os.path.join(directory, 'fresh.sqlite3')
            try:
                database.initialize_database()
                database.initialize_database()
                conn = database.get_db_connection()
                self.assertIsNotNone(conn.execute('''
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'index' AND name = 'whatsapp_campaign_plan_recipient_ledger_id_unique'
                ''').fetchone())
                conn.close()
            finally:
                database.DB_PATH = old_path

    def _create_batch_plan(self, plan_key='batch-plan', campaign_id=700, numbers=None,
                           content_reference='template:v1', content_type='text'):
        numbers = numbers or ['919876543210', '919876543211']
        return create_campaign_send_plan(
            self.conn, self.task(numbers, campaign_id=campaign_id), 7,
            plan_key, len(numbers), content_reference, content_type
        )

    def _node_batch_response(self, payload, found=True):
        items = []
        for item in payload['items']:
            key = campaign_plan_recipient_idempotency_key(
                payload['planId'], item['planRecipientId']
            )
            items.append({
                'position': item['position'],
                'plan_id': payload['planId'],
                'plan_recipient_id': item['planRecipientId'],
                'idempotency_key': key,
                'message_id': key,
                'recipient_jid': item['recipientJid'],
                'content_type': item['contentType'],
                'payload_reference': item['payloadReference'],
                'state': 'reserved',
            })
        return {
            'success': True,
            'found': found,
            'batch': {
                'batch_id': payload['batchId'],
                'request_fingerprint': payload['requestFingerprint'],
                'sender_worker_id': payload['senderWorkerId'],
                'campaign_id': payload['campaignId'],
                'task_id': payload['taskId'],
                'plan_id': payload['planId'],
                'item_count': len(items),
                'state': 'reserved',
                'items': items,
            },
        }

    def _assert_malformed_batch_response(self, mutation, *, content_type='text',
                                         plan_key='batch-malformed-response'):
        plan = self._create_batch_plan(
            plan_key=plan_key, campaign_id=710,
            content_reference='template:v1', content_type=content_type,
        )
        original_batch_id = plan['batch_id']
        response_holder = {}

        def adapter(operation, payload):
            response = self._node_batch_response(payload)
            mutation(response, response['batch']['items'])
            response_holder['response'] = response
            return response, 200

        result = self._reserve_as_worker(
            self.conn, plan['plan_id'], node_call=adapter,
            now='2026-01-01 00:00:00', content_type=content_type,
        )
        self.assertEqual(result['status'], 'unknown')
        self.assertTrue(result['recovery_required'])
        batch_row = self.conn.execute(
            'SELECT batch_id, batch_state FROM whatsapp_campaign_plans WHERE id = ?',
            (plan['plan_id'],),
        ).fetchone()
        self.assertEqual(tuple(batch_row), (original_batch_id, 'unknown'))
        rows = self.conn.execute('''
            SELECT state, ledger_message_id
            FROM whatsapp_campaign_plan_recipients
            WHERE plan_id = ? ORDER BY position
        ''', (plan['plan_id'],)).fetchall()
        self.assertEqual([row['state'] for row in rows], ['reserving', 'reserving'])
        self.assertEqual([row['ledger_message_id'] for row in rows], [None, None])
        self.assertEqual(
            self.conn.execute('''
                SELECT COUNT(*) FROM whatsapp_campaign_recipient_claims
                WHERE plan_id = ? AND state = 'claimed'
            ''', (plan['plan_id'],)).fetchone()[0],
            2,
        )
        return response_holder['response']

    def test_phase4a3_rejects_missing_recipient_jid_without_partial_projection(self):
        def mutate(response, items):
            del items[0]['recipient_jid']

        self._assert_malformed_batch_response(
            mutate, plan_key='batch-missing-recipient-jid'
        )

    def test_phase4a3_rejects_mismatched_recipient_jid_without_partial_projection(self):
        def mutate(response, items):
            items[1]['recipient_jid'] = '919999999999@s.whatsapp.net'

        self._assert_malformed_batch_response(
            mutate, plan_key='batch-mismatched-recipient-jid'
        )

    def test_phase4a3_rejects_missing_content_type_without_partial_projection(self):
        def mutate(response, items):
            del items[0]['content_type']

        self._assert_malformed_batch_response(
            mutate, plan_key='batch-missing-content-type'
        )

    def test_phase4a3_rejects_mismatched_content_type_without_partial_projection(self):
        def mutate(response, items):
            items[1]['content_type'] = 'video'

        self._assert_malformed_batch_response(
            mutate, plan_key='batch-mismatched-content-type', content_type='image'
        )

    def test_phase4a3_rejects_missing_payload_reference_without_partial_projection(self):
        def mutate(response, items):
            del items[0]['payload_reference']

        self._assert_malformed_batch_response(
            mutate, plan_key='batch-missing-payload-reference'
        )

    def test_phase4a3_rejects_mismatched_payload_reference_without_partial_projection(self):
        def mutate(response, items):
            items[1]['payload_reference'] = 'template:other'

        self._assert_malformed_batch_response(
            mutate, plan_key='batch-mismatched-payload-reference'
        )

    def test_phase4a3_validates_text_image_and_video_batch_responses(self):
        for index, content_type in enumerate(('text', 'image', 'video'), start=1):
            with self.subTest(content_type=content_type):
                plan = self._create_batch_plan(
                    plan_key=f'batch-valid-{content_type}',
                    campaign_id=720 + index,
                    content_reference=f'{content_type}:v1',
                    content_type=content_type,
                )

                def adapter(operation, payload):
                    return self._node_batch_response(payload), 200

                result = self._reserve_as_worker(
                    self.conn, plan['plan_id'], node_call=adapter,
                    now='2026-01-01 00:00:00', content_type=content_type,
                )
                self.assertEqual(result['status'], 'reserved')

    def _reserve_as_worker(self, *args, worker_id=7, **kwargs):
        with flask_app.test_request_context('/'):
            session['user_id'] = worker_id
            return reserve_campaign_send_plan_with_node(*args, **kwargs)

    def _cancel_as_worker(self, *args, worker_id=7, **kwargs):
        with flask_app.test_request_context('/'):
            session['user_id'] = worker_id
            return cancel_campaign_send_plan(*args, **kwargs)

    def test_phase4a3_success_projects_complete_node_batch(self):
        self._create_batch_plan()
        calls = []

        def adapter(operation, payload):
            calls.append(operation)
            return self._node_batch_response(payload), 200

        result = self._reserve_as_worker(
            self.conn, 1, node_call=adapter, now='2026-01-01 00:00:00'
        )
        self.assertEqual(result['status'], 'reserved')
        self.assertEqual(calls, ['reserve'])
        self.assertEqual(
            self.conn.execute('SELECT COUNT(*) FROM whatsapp_campaign_plan_recipients WHERE state = "reserved"').fetchone()[0],
            2,
        )
        self.assertEqual(
            self.conn.execute('SELECT batch_state FROM whatsapp_campaign_plans WHERE id = 1').fetchone()[0],
            'reserved',
        )

    def test_phase4a3_timeout_then_lookup_recovers_without_resend(self):
        self._create_batch_plan(plan_key='batch-timeout')
        calls = []
        committed = {}

        def adapter(operation, payload):
            calls.append(operation)
            if operation == 'reserve':
                committed['response'] = self._node_batch_response(payload)
                raise TimeoutError('simulated timeout after commit')
            return committed['response'], 200

        first = self._reserve_as_worker(
            self.conn, 1, node_call=adapter, now='2026-01-01 00:00:00'
        )
        self.assertEqual(first['status'], 'unknown')
        self.assertEqual(
            self.conn.execute('SELECT batch_state FROM whatsapp_campaign_plans WHERE id = 1').fetchone()[0],
            'unknown',
        )
        self.assertEqual(
            self.conn.execute('SELECT COUNT(*) FROM whatsapp_campaign_recipient_claims WHERE state = "claimed"').fetchone()[0],
            2,
        )
        recovered = self._reserve_as_worker(
            self.conn, 1, node_call=adapter, now='2026-01-01 00:00:01'
        )
        self.assertEqual(recovered['status'], 'reserved')
        self.assertEqual(calls, ['reserve', 'lookup'])

    def test_phase4a3_lookup_absence_retries_same_batch_and_malformed_response_stays_unknown(self):
        self._create_batch_plan(plan_key='batch-absence')
        calls = []

        def absence_adapter(operation, payload):
            calls.append(operation)
            if operation == 'lookup':
                return {'success': True, 'found': False}, 200
            return self._node_batch_response(payload), 200

        result = self._reserve_as_worker(
            self.conn, 1, node_call=absence_adapter, now='2026-01-01 00:00:00'
        )
        self.assertEqual(result['status'], 'reserved')
        self.assertEqual(calls, ['reserve'])

        self._create_batch_plan(plan_key='batch-malformed', campaign_id=701)

        def malformed_adapter(operation, payload):
            return {'success': True, 'batch': {'batch_id': payload['batchId'], 'items': []}}, 200

        malformed = self._reserve_as_worker(
            self.conn, 2, node_call=malformed_adapter, now='2026-01-01 00:00:00'
        )
        self.assertEqual(malformed['status'], 'unknown')
        self.assertEqual(
            self.conn.execute('SELECT COUNT(*) FROM whatsapp_campaign_plan_recipients WHERE plan_id = 2 AND ledger_message_id IS NOT NULL').fetchone()[0],
            0,
        )

    def test_phase4a3_partial_projection_is_repeatable_after_reopen(self):
        self._create_batch_plan(plan_key='batch-partial', campaign_id=704)
        committed = {}

        def timeout_adapter(operation, payload):
            committed['response'] = self._node_batch_response(payload)
            raise TimeoutError('simulated post-commit timeout')

        first = self._reserve_as_worker(
            self.conn, 1, node_call=timeout_adapter, now='2026-01-01 00:00:00'
        )
        self.assertEqual(first['status'], 'unknown')
        rows = self.conn.execute('''
            SELECT id FROM whatsapp_campaign_plan_recipients
            WHERE plan_id = 1 ORDER BY position
        ''').fetchall()
        first_key = campaign_plan_recipient_idempotency_key(1, rows[0]['id'])
        self.conn.execute('''
            UPDATE whatsapp_campaign_plan_recipients
            SET state = 'reserved', ledger_message_id = ?,
                reservation_claim_id = NULL, reservation_lease_until = NULL
            WHERE id = ?
        ''', (first_key, rows[0]['id']))
        self.conn.execute('UPDATE whatsapp_campaign_plans SET batch_state = "unknown" WHERE id = 1')
        self.conn.commit()

        def lookup_adapter(operation, payload):
            self.assertEqual(operation, 'lookup')
            return committed['response'], 200

        recovered = self._reserve_as_worker(
            self.conn, 1, node_call=lookup_adapter, now='2026-01-01 00:00:01'
        )
        self.assertEqual(recovered['status'], 'reserved')
        self.assertEqual(
            self.conn.execute('SELECT COUNT(*) FROM whatsapp_campaign_plan_recipients WHERE plan_id = 1 AND state = "reserved"').fetchone()[0],
            2,
        )

    def test_phase4a3_node_unavailable_keeps_claims_and_identity_is_session_owned(self):
        self._create_batch_plan(plan_key='batch-unavailable')

        def unavailable(operation, payload):
            raise OSError('node unavailable')

        with self.assertRaises(PermissionError):
            self._reserve_as_worker(
                self.conn, 1, node_call=unavailable, now='2026-01-01 00:00:00', worker_id=8
            )
        result = self._reserve_as_worker(
            self.conn, 1, node_call=unavailable, now='2026-01-01 00:00:00'
        )
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(
            self.conn.execute('SELECT COUNT(*) FROM whatsapp_campaign_recipient_claims WHERE state = "claimed"').fetchone()[0],
            2,
        )

    def test_phase4a3_node_authentication_failure_is_unknown_and_not_projected(self):
        self._create_batch_plan(plan_key='batch-auth-failure', campaign_id=702)
        with patch('app._call_whatsapp_service', return_value=(
            {'success': False, 'message': 'Invalid service credentials.'}, 401
        )) as call:
            result = self._reserve_as_worker(
                self.conn, 1, now='2026-01-01 00:00:00'
            )
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(call.call_count, 1)
        self.assertEqual(
            self.conn.execute('SELECT COUNT(*) FROM whatsapp_campaign_plan_recipients WHERE plan_id = 1 AND ledger_message_id IS NOT NULL').fetchone()[0],
            0,
        )

    def test_phase4a3_concurrent_recovery_attempts_do_not_project_twice(self):
        self._create_batch_plan(plan_key='batch-concurrent-recovery', campaign_id=703)
        with tempfile.TemporaryDirectory(prefix='taskpe-batch-recovery-', dir=os.getcwd()) as directory:
            path = os.path.join(directory, 'claims.sqlite3')
            target = sqlite3.connect(path)
            self.conn.backup(target)
            target.close()
            first = sqlite3.connect(path, check_same_thread=False)
            second = sqlite3.connect(path, check_same_thread=False)
            first.row_factory = sqlite3.Row
            second.row_factory = sqlite3.Row
            calls = []
            lock = threading.Lock()

            def adapter(operation, payload):
                with lock:
                    calls.append(operation)
                return self._node_batch_response(payload), 200

            results = []

            def run(connection):
                with flask_app.test_request_context('/'):
                    session['user_id'] = 7
                    results.append(reserve_campaign_send_plan_with_node(
                        connection, 1, node_call=adapter,
                        now='2026-01-01 00:00:00'
                    ))

            threads = [threading.Thread(target=run, args=(connection,)) for connection in (first, second)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(len(results), 2)
            self.assertLessEqual(calls.count('reserve'), 1)
            self.assertEqual(
                first.execute('SELECT batch_state FROM whatsapp_campaign_plans WHERE id = 1').fetchone()[0],
                'reserved',
            )
            first.close()
            second.close()

    def test_database_initialization_backfills_phase2_rows_without_changing_identity(self):
        with tempfile.TemporaryDirectory(prefix='taskpe-phase2-', dir=os.getcwd()) as directory:
            path = os.path.join(directory, 'phase2.sqlite3')
            conn = sqlite3.connect(path)
            conn.executescript('''
                CREATE TABLE whatsapp_campaign_plans (
                    id INTEGER PRIMARY KEY, plan_key TEXT NOT NULL UNIQUE,
                    request_fingerprint TEXT NOT NULL, task_id INTEGER NOT NULL,
                    campaign_id INTEGER NOT NULL, sender_worker_id INTEGER NOT NULL,
                    content_reference TEXT NOT NULL, recipient_count INTEGER NOT NULL,
                    state TEXT NOT NULL, created_at TEXT
                );
                CREATE TABLE whatsapp_campaign_plan_recipients (
                    id INTEGER PRIMARY KEY, plan_id INTEGER NOT NULL,
                    recipient_number TEXT NOT NULL, position INTEGER NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE, created_at TEXT
                );
                INSERT INTO whatsapp_campaign_plans VALUES
                    (77, 'legacy-init', 'fingerprint', 4, 17, 7, 'template:v1', 1, 'ready', '2026-01-01');
                INSERT INTO whatsapp_campaign_plan_recipients VALUES
                    (88, 77, '919876543210', 1, 'legacy-init:1', '2026-01-01');
            ''')
            conn.commit()
            conn.close()
            old_path = database.DB_PATH
            with tempfile.TemporaryDirectory(prefix='taskpe-phase2-ledger-', dir=os.getcwd()) as ledger_directory:
                ledger_path = os.path.join(ledger_directory, 'ledger.sqlite3')
                ledger = sqlite3.connect(ledger_path)
                ledger.execute('CREATE TABLE outgoing_messages (message_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE)')
                ledger.commit()
                ledger.close()
                previous = os.environ.get('TASKPE_WA_LEDGER_DB_PATH')
                os.environ['TASKPE_WA_LEDGER_DB_PATH'] = ledger_path
                database.DB_PATH = path
                try:
                    database.initialize_database()
                    conn = database.get_db_connection()
                    row = conn.execute('''
                        SELECT id, plan_id, recipient_number, position, state, idempotency_key
                        FROM whatsapp_campaign_plan_recipients WHERE id = 88
                    ''').fetchone()
                    self.assertEqual(tuple(row[:5]), (88, 77, '919876543210', 1, 'planned'))
                    self.assertEqual(row['idempotency_key'], campaign_plan_recipient_idempotency_key(77, 88))
                    conn.close()
                finally:
                    database.DB_PATH = old_path
                    if previous is None:
                        os.environ.pop('TASKPE_WA_LEDGER_DB_PATH', None)
                    else:
                        os.environ['TASKPE_WA_LEDGER_DB_PATH'] = previous


if __name__ == '__main__':
    unittest.main()
