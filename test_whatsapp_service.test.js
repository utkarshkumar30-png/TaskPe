import test from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { EventEmitter } from 'node:events';
import { Readable } from 'node:stream';
import * as serverModule from './server.js';
import { buildMessageContent, createTaskPeCampaignContentResolver, getSendConfig, isSupportedRecipientJid, sendReservedCampaignItem, sendReservedCampaignBatch, createService, parseConnectionPath, parseInternalPath, verifyInternalRequest, removeUserSession, assertPrivateDirectory, assertWorkerSessionDirectory, ReplayCache, deriveDispatchAuthorizationKey, verifyDispatchAuthorizationToken } from './server.js';
import { calculateCampaignBatchRequestFingerprint, calculateQuotaDay, closeLedger, createLedgerRepository, deriveCampaignPlanRecipientIdempotencyKey, getAttemptReconciliationConfig, getCampaignBatch, getCampaignBatchCancellationEvidence, getDailyQuotaStatusForTest, getOutgoingMessage, getQuotaConfig, initializeLedger, migrateLegacyOptOuts, reconcileLegacyFlaskOptOuts, reconcileStaleAttemptedMessages, reconcileStaleAttemptedMessagesForTest, registerRecipientOptOut, reserveAndEnqueueOutgoingMessage, reserveAndEnqueueOutgoingMessageForTest, reserveCampaignBatch, reserveCampaignBatchForTest, transitionOutgoingMessage, consumeDispatchAuthorization, getDispatchAuthorization } from './whatsapp_ledger.js';

const secret = 'local-only-test-secret-that-is-at-least-32-chars';
function ledgerInput(overrides = {}) {
  return {
    messageId: 'msg-001',
    idempotencyKey: 'idem-001',
    senderWorkerId: 42,
    recipientJid: '919876543210@s.whatsapp.net',
    contentType: 'text',
    ...overrides,
  };
}

function quotaInput(overrides = {}) {
  return {
    messageId: 'quota-msg-001',
    idempotencyKey: 'quota-idem-001',
    senderWorkerId: 42,
    recipientJid: '919876543210@s.whatsapp.net',
    contentType: 'text',
    ...overrides,
  };
}

function campaignBatchInput(overrides = {}) {
  const input = {
    batchId: 'batch-001',
    senderWorkerId: 42,
    campaignId: 7,
    taskId: 9,
    planId: 12,
    items: [
      { planRecipientId: 101, recipientJid: '919876543210@s.whatsapp.net', contentType: 'text', payloadReference: 'content-a' },
      { planRecipientId: 102, recipientJid: '919876543211@s.whatsapp.net', contentType: 'image', payloadReference: 'content-b' },
    ],
    ...overrides,
  };
  input.requestFingerprint = calculateCampaignBatchRequestFingerprint(input);
  return input;
}

async function withQuotaEnvironment(overrides, callback) {
  const previous = {
    limit: process.env.TASKPE_WA_DAILY_QUOTA_LIMIT,
    timezone: process.env.TASKPE_WA_QUOTA_TIMEZONE,
    stale: process.env.TASKPE_WA_ATTEMPT_STALE_SECONDS,
    sendTimeout: process.env.TASKPE_WA_SEND_TIMEOUT_MS,
  };
  if (overrides.limit === undefined) delete process.env.TASKPE_WA_DAILY_QUOTA_LIMIT;
  else process.env.TASKPE_WA_DAILY_QUOTA_LIMIT = String(overrides.limit);
  if (overrides.timezone === undefined) delete process.env.TASKPE_WA_QUOTA_TIMEZONE;
  else process.env.TASKPE_WA_QUOTA_TIMEZONE = overrides.timezone;
  if (overrides.stale === undefined) delete process.env.TASKPE_WA_ATTEMPT_STALE_SECONDS;
  else process.env.TASKPE_WA_ATTEMPT_STALE_SECONDS = String(overrides.stale);
  if (overrides.sendTimeout === undefined) delete process.env.TASKPE_WA_SEND_TIMEOUT_MS;
  else process.env.TASKPE_WA_SEND_TIMEOUT_MS = String(overrides.sendTimeout);
  try { return await callback(); }
  finally {
    if (previous.limit === undefined) delete process.env.TASKPE_WA_DAILY_QUOTA_LIMIT;
    else process.env.TASKPE_WA_DAILY_QUOTA_LIMIT = previous.limit;
    if (previous.timezone === undefined) delete process.env.TASKPE_WA_QUOTA_TIMEZONE;
    else process.env.TASKPE_WA_QUOTA_TIMEZONE = previous.timezone;
    if (previous.stale === undefined) delete process.env.TASKPE_WA_ATTEMPT_STALE_SECONDS;
    else process.env.TASKPE_WA_ATTEMPT_STALE_SECONDS = previous.stale;
    if (previous.sendTimeout === undefined) delete process.env.TASKPE_WA_SEND_TIMEOUT_MS;
    else process.env.TASKPE_WA_SEND_TIMEOUT_MS = previous.sendTimeout;
  }
}

async function withLedgerTest(callback) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-orchestrator-')); const dbPath = path.join(root, 'outgoing.sqlite3');
  const db = initializeLedger(dbPath);
  try { return await callback(createLedgerRepository(db), db); }
  finally { closeLedger(db); await fs.rm(root, { recursive: true, force: true }); }
}

function runLedgerChild(script, args, env = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, ['--input-type=module', '-e', script, ...args], {
      cwd: path.dirname(fileURLToPath(import.meta.url)),
      env: { ...process.env, ...env },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', chunk => { stdout += chunk; });
    child.stderr.on('data', chunk => { stderr += chunk; });
    child.on('error', error => resolve({ code: null, signal: null, stdout, stderr, spawnError: error.message }));
    child.on('exit', (code, signal) => resolve({ code, signal, stdout, stderr, spawnError: null }));
  });
}

/* Obsolete unfenced single-message orchestration tests were removed when the
 * production-capable helper was removed from server.js. */
/*
test('orchestrator persists attempted then acknowledged without claiming delivery', async () => {
  await withLedgerTest(async (ledger, db) => {
    const calls = [];
    const socket = { sendMessage: async (...args) => { calls.push(args); return { key: { id: 'transport-id-not-returned' } }; } };
    const result = await orchestrateOutgoingMessage({ ledger, socket, ...orchestratorInput() });
    assert.equal(result.accepted, true); assert.equal(result.status, 'acknowledged'); assert.equal(result.delivered, false);
    assert.equal(calls.length, 1); assert.deepEqual(calls[0], ['919876543210@s.whatsapp.net', { text: 'private message content' }]);
    const record = ledger.getByIdempotencyKey('orchestrator-idem-001');
    assert.equal(record.state, 'acknowledged'); assert.equal(record.attempt_count, 1); assert.equal(record.delivered_unknown_at, null);
    assert.match(record.attempt_operation_id, /^[A-Za-z0-9_-]{32,}$/); assert.match(record.attempt_lease_until, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/); assert.ok(new Date(record.attempt_lease_until).getTime() > Date.now());
    assert.equal(db.prepare('SELECT reserved_count FROM daily_message_quota WHERE sender_worker_id = 42').get().reserved_count, 1);
    assert.doesNotMatch(JSON.stringify(result), /private message content/);
  });
});

test('orchestrator does not resend an existing idempotency record', async () => {
  await withLedgerTest(async (ledger, db) => {
    let calls = 0;
    const socket = { sendMessage: async () => { calls += 1; } };
    const first = await orchestrateOutgoingMessage({ ledger, socket, ...orchestratorInput() });
    const duplicate = await orchestrateOutgoingMessage({ ledger, socket, ...orchestratorInput({ messageInput: { type: 'text', text: 'different content' } }) });
    assert.equal(first.accepted, true); assert.equal(duplicate.status, 'duplicate'); assert.equal(duplicate.state, 'acknowledged');
    assert.equal(calls, 1); assert.equal(duplicate.delivered, false); assert.doesNotMatch(JSON.stringify(duplicate), /content/);
    assert.equal(db.prepare('SELECT reserved_count FROM daily_message_quota WHERE sender_worker_id = 42').get().reserved_count, 1);
  });
});

test('orchestrator does not invoke the socket when ledger creation fails', async () => {
  let calls = 0;
  const socket = { sendMessage: async () => { calls += 1; } };
  const ledger = {
    reserveAndEnqueue: () => { throw new Error('private database details'); },
    transition: () => { throw new Error('must not transition'); },
  };
  const result = await orchestrateOutgoingMessage({ ledger, socket, ...orchestratorInput() });
  assert.equal(result.status, 'ledger_error'); assert.equal(calls, 0); assert.doesNotMatch(JSON.stringify(result), /private/);
});

test('orchestrator does not invoke the socket when attempted state cannot be persisted', async () => {
  let calls = 0;
  const socket = { sendMessage: async () => { calls += 1; } };
  const ledger = {
    reserveAndEnqueue: () => ({ created: true, duplicate: false, record: { message_id: 'msg-1' } }),
    transition: () => { throw new Error('private database details'); },
  };
  const result = await orchestrateOutgoingMessage({ ledger, socket, ...orchestratorInput() });
  assert.equal(result.status, 'ledger_error'); assert.equal(calls, 0);
});

test('orchestrator rejects an exhausted quota before invoking the socket', async () => {
  let calls = 0;
  const socket = { sendMessage: async () => { calls += 1; } };
  const ledger = {
    reserveAndEnqueue: () => { const error = new Error('quota'); error.code = 'QUOTA_EXCEEDED'; throw error; },
    transition: () => { throw new Error('must not transition'); },
  };
  const result = await orchestrateOutgoingMessage({ ledger, socket, ...orchestratorInput() });
  assert.deepEqual(result, { accepted: false, status: 'quota_exceeded', message: 'Daily message quota exceeded.', delivered: false });
  assert.equal(calls, 0);
});

test('orchestrator keeps quota reserved after a socket failure', async () => {
  await withQuotaEnvironment({ limit: 2 }, async () => {
    await withLedgerTest(async ledger => {
      let calls = 0;
      const socket = { sendMessage: async () => { calls += 1; throw new Error('private transport details'); } };
      const result = await orchestrateOutgoingMessage({ ledger, socket, ...orchestratorInput() });
      assert.equal(result.status, 'failed'); assert.equal(calls, 1);
      assert.equal(ledger.getByIdempotencyKey('orchestrator-idem-001').quota_reserved, 1);
    });
  });
});
*/

test('ledger requires matching operation ownership for completion transitions', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-operation-owner-')); const dbPath = path.join(root, 'outgoing.sqlite3');
  try {
    const db = initializeLedger(dbPath);
    reserveAndEnqueueOutgoingMessageForTest(db, ledgerInput(), '2026-01-01T00:00:00.000Z');
    assert.throws(() => transitionOutgoingMessage(db, 'msg-001', 'attempted'), /Attempt operation ownership is required/);
    const operationId = 'operation-owner-123456';
    transitionOutgoingMessage(db, 'msg-001', 'attempted', { now: '2026-01-01T00:00:00.000Z', attemptOperationId: operationId, attemptLeaseUntil: '2026-01-01T00:01:00.000Z' });
    assert.throws(() => transitionOutgoingMessage(db, 'msg-001', 'acknowledged', { attemptOperationId: 'operation-other-123456' }), /Attempt operation ownership mismatch/);
    assert.throws(() => transitionOutgoingMessage(db, 'msg-001', 'failed', { attemptOperationId: 'operation-other-123456', failureCode: 'TRANSPORT_ERROR' }), /Attempt operation ownership mismatch/);
    const acknowledged = transitionOutgoingMessage(db, 'msg-001', 'acknowledged', { attemptOperationId: operationId });
    assert.equal(acknowledged.state, 'acknowledged'); assert.equal(acknowledged.attempt_count, 1);
    closeLedger(db);
  } finally { await fs.rm(root, { recursive: true, force: true }); }
});

/* Obsolete tests for the removed unfenced single-message helper. */
/* test('bounded send timeout records delivered_unknown without retrying', async () => {
  await withQuotaEnvironment({ limit: 2, sendTimeout: 1000 }, async () => {
    await withLedgerTest(async ledger => {
      let calls = 0; let resolveSocket;
      const socket = { sendMessage: async () => { calls += 1; return new Promise(resolve => { resolveSocket = resolve; }); } };
      const result = await orchestrateOutgoingMessage({ ledger, socket, ...orchestratorInput() });
      assert.deepEqual({ status: result.status, delivered: result.delivered }, { status: 'uncertain', delivered: false });
      assert.equal(calls, 1);
      const record = ledger.getByIdempotencyKey('orchestrator-idem-001');
      assert.equal(record.state, 'delivered_unknown'); assert.equal(record.quota_reserved, 1); assert.equal(record.attempt_count, 1);
      resolveSocket();
      await new Promise(resolve => setImmediate(resolve));
      assert.equal(ledger.getByIdempotencyKey('orchestrator-idem-001').state, 'delivered_unknown');
      const duplicate = await orchestrateOutgoingMessage({ ledger, socket, ...orchestratorInput() });
      assert.equal(duplicate.status, 'duplicate'); assert.equal(calls, 1);
    });
  });
});

test('late socket rejection after timeout cannot alter the ledger or trigger a retry', async () => {
  await withQuotaEnvironment({ limit: 2, sendTimeout: 1000 }, async () => {
    await withLedgerTest(async ledger => {
      let calls = 0;
      const socket = { sendMessage: async () => { calls += 1; return new Promise((_, reject) => setTimeout(() => reject(new Error('late transport')), 1200)); } };
      const result = await orchestrateOutgoingMessage({ ledger, socket, ...orchestratorInput() });
      assert.equal(result.status, 'uncertain'); assert.equal(calls, 1);
      await new Promise(resolve => setTimeout(resolve, 300));
      assert.equal(ledger.getByIdempotencyKey('orchestrator-idem-001').state, 'delivered_unknown');
      const duplicate = await orchestrateOutgoingMessage({ ledger, socket, ...orchestratorInput() });
      assert.equal(duplicate.status, 'duplicate'); assert.equal(calls, 1);
    });
  });
});

test('unresolved socket work remains outside ledger reconciliation and is never invoked by it', async () => {
  await withQuotaEnvironment({ limit: 2, sendTimeout: 1000, stale: 60 }, async () => {
    await withLedgerTest(async (ledger, db) => {
      let socketCalls = 0;
      let socketSettled = false;
      const socket = { sendMessage: async () => { socketCalls += 1; return new Promise(resolve => { void resolve; }); } };
      const send = sendMessageWithSocket(socket, '919876543210@s.whatsapp.net', { type: 'text', text: 'unresolved' }, { testOnlyUnfenced: true });
      const result = await send;
      assert.equal(result.status, 'uncertain'); assert.equal(socketCalls, 1);
      const input = quotaInput({ messageId: 'reconcile-message', idempotencyKey: 'reconcile-idem' });
      reserveAndEnqueueOutgoingMessageForTest(db, input, '2026-01-01T10:00:00.000Z');
      transitionOutgoingMessage(db, input.messageId, 'attempted', { now: '2026-01-01T10:00:00.000Z', attemptOperationId: 'operation-reconcile-123456', attemptLeaseUntil: '2026-01-01T10:01:00.000Z' });
      assert.deepEqual(reconcileStaleAttemptedMessagesForTest(db, '2026-01-01T12:00:00.000Z'), { reconciled_count: 1 });
      assert.equal(socketSettled, false); assert.equal(socketCalls, 1);
    });
  });
});

test('timeout transition failure is sanitized and does not invoke the socket twice', async () => {
  await withQuotaEnvironment({ sendTimeout: 1000 }, async () => {
    let calls = 0; let transitions = 0;
    const ledger = {
      reserveAndEnqueue: () => ({ created: true, duplicate: false, record: { message_id: 'timeout-message' } }),
      transition: (messageId, state) => {
        transitions += 1;
        if (state === 'delivered_unknown') throw new Error('database unavailable');
        return { message_id: messageId, state };
      },
    };
    const socket = { sendMessage: async () => { calls += 1; return new Promise(() => {}); } };
    const result = await orchestrateOutgoingMessage({ ledger, socket, ...orchestratorInput() });
    assert.equal(result.status, 'ledger_error'); assert.equal(calls, 1); assert.equal(transitions, 2);
  });
});

test('one send attempt uses one configuration snapshot even if environment changes mid-attempt', async () => {
  await withQuotaEnvironment({ sendTimeout: 1000 }, async () => {
    await withLedgerTest(async ledger => {
      let calls = 0;
      const originalReserve = ledger.reserveAndEnqueue;
      ledger.reserveAndEnqueue = input => {
        const result = originalReserve(input);
        process.env.TASKPE_WA_SEND_TIMEOUT_MS = '120000';
        return result;
      };
      const socket = { sendMessage: async () => { calls += 1; return new Promise(() => {}); } };
      const started = Date.now();
      const result = await orchestrateOutgoingMessage({ ledger, socket, ...orchestratorInput() });
      assert.equal(result.status, 'uncertain'); assert.equal(calls, 1); assert.ok(Date.now() - started < 5000);
    });
  });
});

test('orchestrator records sanitized socket failure and does not retry', async () => {
  await withLedgerTest(async ledger => {
    let calls = 0;
    const socket = { sendMessage: async () => { calls += 1; throw new Error('private transport details'); } };
    const result = await orchestrateOutgoingMessage({ ledger, socket, ...orchestratorInput() });
    assert.equal(result.status, 'failed'); assert.equal(result.delivered, false); assert.equal(calls, 1);
    const record = ledger.getByIdempotencyKey('orchestrator-idem-001');
    assert.equal(record.state, 'failed'); assert.equal(record.failure_code, 'TRANSPORT_ERROR');
    assert.doesNotMatch(JSON.stringify(result), /private|message content/);
  });
});
*/

test('outgoing ledger initializes, persists, and reopens', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-ledger-')); const dbPath = path.join(root, 'outgoing.sqlite3');
  try {
    const db = initializeLedger(dbPath);
    const created = reserveAndEnqueueOutgoingMessageForTest(db, ledgerInput(), '2026-01-01T00:00:00.000Z').record;
    assert.equal(created.state, 'queued'); assert.equal(created.attempt_count, 0);
    assert.equal(created.batch_id, null);
    closeLedger(db);
    const reopened = initializeLedger(dbPath);
    assert.equal(getOutgoingMessage(reopened, 'msg-001').idempotency_key, 'idem-001');
    closeLedger(reopened);
  } finally { await fs.rm(root, { recursive: true, force: true }); }
});

test('outgoing ledger enforces idempotency across concurrent database handles', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-ledger-')); const dbPath = path.join(root, 'outgoing.sqlite3');
  try {
    const first = initializeLedger(dbPath); const second = initializeLedger(dbPath);
    reserveAndEnqueueOutgoingMessageForTest(first, ledgerInput(), '2026-01-01T00:00:00.000Z');
    assert.equal(reserveAndEnqueueOutgoingMessageForTest(second, ledgerInput({ messageId: 'msg-002' }), '2026-01-01T00:00:00.000Z').duplicate, true);
    assert.equal(getOutgoingMessage(second, 'msg-001').message_id, 'msg-001');
    closeLedger(first); closeLedger(second);
  } finally { await fs.rm(root, { recursive: true, force: true }); }
});

test('outgoing ledger supports explicit state transitions without equating acknowledgement with delivery', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-ledger-')); const dbPath = path.join(root, 'outgoing.sqlite3');
  try {
    const db = initializeLedger(dbPath); reserveAndEnqueueOutgoingMessageForTest(db, ledgerInput(), '2026-01-01T00:00:00.000Z');
    const operationId = 'operation-123456789012';
    transitionOutgoingMessage(db, 'msg-001', 'attempted', { now: '2026-01-01T00:01:00.000Z', attemptOperationId: operationId, attemptLeaseUntil: '2026-01-01T00:02:00.000Z' });
    const acknowledged = transitionOutgoingMessage(db, 'msg-001', 'acknowledged', { transportMessageId: 'wa-msg-1', now: '2026-01-01T00:02:00.000Z', attemptOperationId: operationId });
    assert.equal(acknowledged.state, 'acknowledged'); assert.equal(acknowledged.delivered_unknown_at, null);
    assert.throws(
      () => transitionOutgoingMessage(db, 'msg-001', 'delivered_unknown', { now: '2026-01-01T00:03:00.000Z', attemptOperationId: operationId }),
      /Invalid message state transition/
    );
    assert.equal(getOutgoingMessage(db, 'msg-001').state, 'acknowledged');
    assert.throws(() => transitionOutgoingMessage(db, 'msg-001', 'queued'), /Invalid message state transition/);
    closeLedger(db);
  } finally { await fs.rm(root, { recursive: true, force: true }); }
});

test('outgoing ledger rolls back failed transactions and sanitizes failure state', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-ledger-')); const dbPath = path.join(root, 'outgoing.sqlite3');
  try {
    const db = initializeLedger(dbPath); reserveAndEnqueueOutgoingMessageForTest(db, ledgerInput(), '2026-01-01T00:00:00.000Z');
    const attempted = transitionOutgoingMessage(db, 'msg-001', 'attempted', { attemptOperationId: 'operation-123456789012', attemptLeaseUntil: new Date(Date.now() + 60000).toISOString() });
    assert.equal(attempted.attempt_count, 1);
    assert.throws(() => transitionOutgoingMessage(db, 'msg-001', 'attempted'), /Invalid message state transition/);
    assert.equal(getOutgoingMessage(db, 'msg-002'), null);
    const final = transitionOutgoingMessage(db, 'msg-001', 'failed', { failureCode: 'TRANSPORT_ERROR', attemptOperationId: 'operation-123456789012' });
    assert.equal(final.state, 'failed'); assert.equal(final.failure_code, 'TRANSPORT_ERROR');
    closeLedger(db);
  } finally { await fs.rm(root, { recursive: true, force: true }); }
});

test('daily quota defaults to 80 and rejects the 81st distinct reservation', async () => {
  await withQuotaEnvironment({}, async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-quota-')); const dbPath = path.join(root, 'outgoing.sqlite3');
    try {
      assert.deepEqual(getQuotaConfig(), { limit: 80, timezone: 'Asia/Kolkata' });
      const db = initializeLedger(dbPath);
      for (let index = 1; index <= 80; index += 1) {
        const result = reserveAndEnqueueOutgoingMessageForTest(db, quotaInput({ messageId: `quota-msg-${index}`, idempotencyKey: `quota-idem-${index}` }), '2026-01-01T00:00:00.000Z');
        assert.equal(result.created, true);
      }
      assert.throws(() => reserveAndEnqueueOutgoingMessageForTest(db, quotaInput({ messageId: 'quota-msg-81', idempotencyKey: 'quota-idem-81' }), '2026-01-01T00:00:00.000Z'), error => error.code === 'QUOTA_EXCEEDED');
      assert.equal(db.prepare('SELECT reserved_count FROM daily_message_quota WHERE sender_worker_id = 42').get().reserved_count, 80);
      closeLedger(db);
    } finally { await fs.rm(root, { recursive: true, force: true }); }
  });
});

test('duplicate idempotency keys reserve quota only once', async () => {
  await withQuotaEnvironment({ limit: 2 }, async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-quota-')); const dbPath = path.join(root, 'outgoing.sqlite3');
    try {
      const db = initializeLedger(dbPath);
      const first = reserveAndEnqueueOutgoingMessageForTest(db, quotaInput(), '2026-01-01T00:00:00.000Z');
      const duplicate = reserveAndEnqueueOutgoingMessageForTest(db, quotaInput({ messageId: 'quota-msg-002' }), '2026-01-01T00:00:00.000Z');
      assert.equal(first.created, true); assert.equal(duplicate.duplicate, true); assert.equal(duplicate.record.message_id, first.record.message_id);
      assert.equal(db.prepare('SELECT COUNT(*) AS count FROM outgoing_messages').get().count, 1);
      assert.equal(db.prepare('SELECT reserved_count FROM daily_message_quota').get().reserved_count, 1);
      closeLedger(db);
    } finally { await fs.rm(root, { recursive: true, force: true }); }
  });
});

test('campaign batch reservation commits all items and reopens durably', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-batch-')); const dbPath = path.join(root, 'outgoing.sqlite3');
  let db = initializeLedger(dbPath);
  try {
    const input = campaignBatchInput();
    const result = reserveCampaignBatchForTest(db, input, '2026-01-01T00:00:00.000Z');
    assert.equal(result.created, true); assert.equal(result.batch.item_count, 2); assert.equal(result.items.length, 2);
    assert.equal(result.items[0].message_id, deriveCampaignPlanRecipientIdempotencyKey(12, 101));
    assert.equal(result.items[0].payload_reference, 'content-a');
    assert.equal(result.items[1].payload_reference, 'content-b');
    assert.deepEqual(
      db.prepare('SELECT batch_id FROM outgoing_messages ORDER BY message_id').all().map(row => row.batch_id),
      [input.batchId, input.batchId],
    );
    assert.equal(db.prepare('SELECT reserved_count FROM daily_message_quota').get().reserved_count, 2);
    closeLedger(db); db = null;
    const reopened = initializeLedger(dbPath);
    try {
      const reopenedBatch = getCampaignBatch(reopened, input.batchId, 42);
      assert.equal(reopenedBatch.items.length, 2);
      assert.deepEqual(reopenedBatch.items.map(item => item.payload_reference), [
        'content-a', 'content-b',
      ]);
    }
    finally { closeLedger(reopened); }
  } finally { if (db) closeLedger(db); await fs.rm(root, { recursive: true, force: true }); }
});

test('separate Node processes reserve competing batches without exceeding quota', async () => {
  await withQuotaEnvironment({ limit: 2 }, async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-batch-process-')); const dbPath = path.join(root, 'ledger.sqlite3');
    const moduleUrl = new URL('./whatsapp_ledger.js', import.meta.url).href;
    const script = `const { initializeLedger, reserveCampaignBatch } = await import(${JSON.stringify(moduleUrl)}); try { const db = initializeLedger(process.argv[1]); reserveCampaignBatch(db, JSON.parse(process.argv[2])); db.close(); process.exit(0); } catch (error) { process.exit(error.code === 'QUOTA_EXCEEDED' ? 2 : 1); }`;
    const first = campaignBatchInput({ batchId: 'batch-process-a', planId: 21 });
    const second = campaignBatchInput({ batchId: 'batch-process-b', planId: 22 });
    try {
      const parent = initializeLedger(dbPath); closeLedger(parent);
      const results = await Promise.all([
        runLedgerChild(script, [dbPath, JSON.stringify(first)], { TASKPE_WA_DAILY_QUOTA_LIMIT: '2' }),
        runLedgerChild(script, [dbPath, JSON.stringify(second)], { TASKPE_WA_DAILY_QUOTA_LIMIT: '2' }),
      ]);
      assert.deepEqual(results.map(result => result.code).sort(), [0, 2], JSON.stringify(results));
      const db = initializeLedger(dbPath);
      assert.equal(db.prepare('SELECT reserved_count FROM daily_message_quota').get().reserved_count, 2);
      const winningBatch = db.prepare('SELECT batch_id FROM whatsapp_campaign_batches').get().batch_id;
      assert.equal(db.prepare('SELECT COUNT(*) AS count FROM whatsapp_campaign_batches').get().count, 1);
      assert.equal(db.prepare('SELECT COUNT(*) AS count FROM outgoing_messages WHERE batch_id = ?').get(winningBatch).count, 2);
      closeLedger(db);
    } finally { await fs.rm(root, { recursive: true, force: true }); }
  });
});

test('separate Node processes initialize a new ledger with the batch migration safely', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-batch-init-')); const dbPath = path.join(root, 'ledger.sqlite3');
  const moduleUrl = new URL('./whatsapp_ledger.js', import.meta.url).href;
  const script = `const { initializeLedger } = await import(${JSON.stringify(moduleUrl)}); try { const db = initializeLedger(process.argv[1]); db.close(); process.exit(0); } catch { process.exit(1); }`;
  try {
    const results = await Promise.all([
      runLedgerChild(script, [dbPath], { TASKPE_WA_DAILY_QUOTA_LIMIT: '80', TASKPE_WA_QUOTA_TIMEZONE: 'Asia/Kolkata', TASKPE_WA_ATTEMPT_STALE_SECONDS: '900' }),
      runLedgerChild(script, [dbPath], { TASKPE_WA_DAILY_QUOTA_LIMIT: '80', TASKPE_WA_QUOTA_TIMEZONE: 'Asia/Kolkata', TASKPE_WA_ATTEMPT_STALE_SECONDS: '900' }),
    ]);
    assert.deepEqual(results.map(result => result.code).sort(), [0, 0], JSON.stringify(results));
    const db = initializeLedger(dbPath);
    try {
      assert.equal(db.prepare("SELECT COUNT(*) AS count FROM pragma_table_info('outgoing_messages') WHERE name = 'quota_day'").get().count, 1);
      assert.equal(db.prepare("SELECT COUNT(*) AS count FROM pragma_table_info('outgoing_messages') WHERE name = 'quota_reserved'").get().count, 1);
      assert.equal(db.prepare("SELECT COUNT(*) AS count FROM pragma_table_info('outgoing_messages') WHERE name = 'batch_id'").get().count, 1);
      assert.equal(db.prepare("SELECT COUNT(*) AS count FROM pragma_table_info('outgoing_messages') WHERE name = 'attempt_operation_id'").get().count, 1);
      assert.ok(db.prepare("SELECT name FROM pragma_table_info('outgoing_messages') WHERE name = 'batch_id'").get());
      assert.ok(db.prepare("SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'outgoing_messages_batch_lookup'").get());
    } finally { closeLedger(db); }
  } finally { await fs.rm(root, { recursive: true, force: true }); }
});

test('separate Node processes initialize an existing legacy ledger safely', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-legacy-init-')); const dbPath = path.join(root, 'ledger.sqlite3');
  const legacy = new DatabaseSync(dbPath);
  legacy.exec(`CREATE TABLE outgoing_messages (
    message_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
    sender_worker_id INTEGER NOT NULL, recipient_jid TEXT NOT NULL,
    campaign_id INTEGER, task_id INTEGER, content_type TEXT NOT NULL,
    payload_reference TEXT, state TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
    failure_code TEXT, transport_message_id TEXT, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, attempted_at TEXT, acknowledged_at TEXT,
    delivered_unknown_at TEXT, failed_at TEXT
  )`);
  legacy.prepare(`INSERT INTO outgoing_messages
    (message_id, idempotency_key, sender_worker_id, recipient_jid, content_type, state, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)`)
    .run('legacy-init-message', 'legacy-init-key', 42, '919876543210@s.whatsapp.net', 'text', '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z');
  legacy.close();
  const moduleUrl = new URL('./whatsapp_ledger.js', import.meta.url).href;
  const script = `const { initializeLedger } = await import(${JSON.stringify(moduleUrl)}); try { const db = initializeLedger(process.argv[1]); db.close(); process.exit(0); } catch (error) { console.error(error.message); process.exit(1); }`;
  try {
    const results = await Promise.all([runLedgerChild(script, [dbPath]), runLedgerChild(script, [dbPath])]);
    assert.deepEqual(results.map(result => result.code).sort(), [0, 0], JSON.stringify(results));
    const db = initializeLedger(dbPath);
    try {
      for (const column of ['quota_day', 'quota_reserved', 'batch_id', 'attempt_operation_id', 'attempt_lease_until']) {
        assert.equal(db.prepare("SELECT COUNT(*) AS count FROM pragma_table_info('outgoing_messages') WHERE name = ?").get(column).count, 1);
      }
      assert.equal(db.prepare("SELECT COUNT(*) AS count FROM sqlite_master WHERE type = 'index' AND name = 'outgoing_messages_batch_lookup'").get().count, 1);
      assert.equal(db.prepare('SELECT message_id, idempotency_key FROM outgoing_messages').get().message_id, 'legacy-init-message');
    } finally { closeLedger(db); }
  } finally { await fs.rm(root, { recursive: true, force: true }); }
});

test('identical campaign batch retry is idempotent and conflicting fingerprint fails closed', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-batch-idem-')); const db = initializeLedger(path.join(root, 'ledger.sqlite3'));
  try {
    const input = campaignBatchInput(); const first = reserveCampaignBatchForTest(db, input, '2026-01-01T00:00:00.000Z');
    const duplicate = reserveCampaignBatchForTest(db, input, '2026-01-01T00:00:00.000Z');
    assert.equal(duplicate.duplicate, true); assert.equal(duplicate.batch.batch_id, first.batch.batch_id);
    assert.deepEqual(db.prepare('SELECT batch_id FROM outgoing_messages ORDER BY message_id').all().map(row => row.batch_id), [input.batchId, input.batchId]);
    const conflicting = { ...input, items: [{ ...input.items[0], payloadReference: 'different-content' }] };
    conflicting.requestFingerprint = calculateCampaignBatchRequestFingerprint(conflicting);
    assert.throws(() => reserveCampaignBatchForTest(db, conflicting, '2026-01-01T00:00:00.000Z'), error => error.code === 'BATCH_CONFLICT');
    assert.equal(db.prepare('SELECT reserved_count FROM daily_message_quota').get().reserved_count, 2);
  } finally { closeLedger(db); await fs.rm(root, { recursive: true, force: true }); }
});

test('campaign batch quota exhaustion rolls back the entire batch', async () => {
  await withQuotaEnvironment({ limit: 1 }, async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-batch-quota-')); const db = initializeLedger(path.join(root, 'ledger.sqlite3'));
    try {
      assert.throws(() => reserveCampaignBatchForTest(db, campaignBatchInput(), '2026-01-01T00:00:00.000Z'), error => error.code === 'QUOTA_EXCEEDED');
      assert.equal(db.prepare('SELECT COUNT(*) AS count FROM whatsapp_campaign_batches').get().count, 0);
      assert.equal(db.prepare('SELECT COUNT(*) AS count FROM whatsapp_campaign_batch_items').get().count, 0);
      assert.equal(db.prepare('SELECT COUNT(*) AS count FROM outgoing_messages').get().count, 0);
      assert.equal(db.prepare('SELECT COUNT(*) AS count FROM daily_message_quota').get().count, 0);
    } finally { closeLedger(db); await fs.rm(root, { recursive: true, force: true }); }
  });
});

test('campaign batch insert failure rolls back quota and all batch records', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-batch-rollback-')); const db = initializeLedger(path.join(root, 'ledger.sqlite3'));
  try {
    db.exec("CREATE TRIGGER fail_batch_item BEFORE INSERT ON whatsapp_campaign_batch_items WHEN NEW.position = 2 BEGIN SELECT RAISE(ABORT, 'forced batch failure'); END");
    assert.throws(() => reserveCampaignBatchForTest(db, campaignBatchInput(), '2026-01-01T00:00:00.000Z'), error => error.code === 'BATCH_RESERVATION_FAILED');
    assert.equal(db.prepare('SELECT COUNT(*) AS count FROM daily_message_quota').get().count, 0);
    assert.equal(db.prepare('SELECT COUNT(*) AS count FROM whatsapp_campaign_batches').get().count, 0);
    assert.equal(db.prepare('SELECT COUNT(*) AS count FROM outgoing_messages').get().count, 0);
    assert.equal(db.prepare('SELECT COUNT(*) AS count FROM outgoing_messages WHERE batch_id IS NOT NULL').get().count, 0);
  } finally { closeLedger(db); await fs.rm(root, { recursive: true, force: true }); }
});

test('campaign batch rejects duplicate item identities and never invokes a socket', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-batch-invalid-')); const db = initializeLedger(path.join(root, 'ledger.sqlite3'));
  try {
    const input = campaignBatchInput({ items: [
      { planRecipientId: 101, recipientJid: '919876543210@s.whatsapp.net', contentType: 'text' },
      { planRecipientId: 101, recipientJid: '919876543211@s.whatsapp.net', contentType: 'text' },
    ] });
    assert.throws(() => reserveCampaignBatchForTest(db, input, '2026-01-01T00:00:00.000Z'), /Duplicate plan recipient ID/);
    assert.equal(db.prepare('SELECT COUNT(*) AS count FROM outgoing_messages').get().count, 0);
  } finally { closeLedger(db); await fs.rm(root, { recursive: true, force: true }); }
});

test('concurrent distinct reservations never exceed the configured quota', async () => {
  await withQuotaEnvironment({ limit: 80 }, async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-quota-concurrent-')); const dbPath = path.join(root, 'outgoing.sqlite3');
    try {
      const first = initializeLedger(dbPath); const second = initializeLedger(dbPath);
      const results = await Promise.all(Array.from({ length: 81 }, (_, index) => Promise.resolve().then(() => {
        const db = index % 2 ? first : second;
        try { return reserveAndEnqueueOutgoingMessageForTest(db, quotaInput({ messageId: `concurrent-msg-${index}`, idempotencyKey: `concurrent-idem-${index}` }), '2026-01-01T00:00:00.000Z'); }
        catch (error) { return error.code || 'ERROR'; }
      })));
      assert.equal(results.filter(result => result?.created === true).length, 80);
      assert.equal(results.filter(result => result === 'QUOTA_EXCEEDED').length, 1);
      assert.equal(first.prepare('SELECT reserved_count FROM daily_message_quota').get().reserved_count, 80);
      closeLedger(first); closeLedger(second);
    } finally { await fs.rm(root, { recursive: true, force: true }); }
  });
});

test('concurrent duplicate reservations create one message and one reservation', async () => {
  await withQuotaEnvironment({ limit: 2 }, async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-quota-duplicate-')); const dbPath = path.join(root, 'outgoing.sqlite3');
    try {
      const first = initializeLedger(dbPath); const second = initializeLedger(dbPath);
      const results = await Promise.all([
        Promise.resolve().then(() => reserveAndEnqueueOutgoingMessageForTest(first, quotaInput(), '2026-01-01T00:00:00.000Z')),
        Promise.resolve().then(() => reserveAndEnqueueOutgoingMessageForTest(second, quotaInput({ messageId: 'quota-msg-002' }), '2026-01-01T00:00:00.000Z')),
      ]);
      assert.equal(results.filter(result => result.created === true).length, 1);
      assert.equal(results.filter(result => result.duplicate === true).length, 1);
      assert.equal(first.prepare('SELECT COUNT(*) AS count FROM outgoing_messages').get().count, 1);
      assert.equal(first.prepare('SELECT reserved_count FROM daily_message_quota').get().reserved_count, 1);
      closeLedger(first); closeLedger(second);
    } finally { await fs.rm(root, { recursive: true, force: true }); }
  });
});

test('separate Node processes contend safely for one SQLite quota slot', async () => {
  await withQuotaEnvironment({ limit: 1 }, async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-process-quota-')); const dbPath = path.join(root, 'outgoing.sqlite3');
    const moduleUrl = new URL('./whatsapp_ledger.js', import.meta.url).href;
    const script = `const { initializeLedger, reserveAndEnqueueOutgoingMessage } = await import(${JSON.stringify(moduleUrl)}); try { const db = initializeLedger(process.argv[1]); reserveAndEnqueueOutgoingMessage(db, { messageId: process.argv[2], idempotencyKey: process.argv[3], senderWorkerId: 42, recipientJid: '919876543210@s.whatsapp.net', contentType: 'text' }); db.close(); process.exit(0); } catch (error) { process.exit(error.code === 'QUOTA_EXCEEDED' ? 2 : 1); }`;
    try {
      const parentDb = initializeLedger(dbPath);
      closeLedger(parentDb);
      const results = await Promise.all([
        runLedgerChild(script, [dbPath, 'process-message-1', 'process-idem-1']),
        runLedgerChild(script, [dbPath, 'process-message-2', 'process-idem-2']),
      ]);
      assert.deepEqual(
        results.map(result => result.code).sort(),
        [0, 2],
        `Unexpected ledger child results: ${JSON.stringify(results)}`,
      );
      const db = initializeLedger(dbPath);
      assert.equal(db.prepare('SELECT reserved_count FROM daily_message_quota').get().reserved_count, 1);
      assert.equal(db.prepare('SELECT COUNT(*) AS count FROM outgoing_messages').get().count, 1);
      closeLedger(db);
    } finally { await fs.rm(root, { recursive: true, force: true }); }
  });
});

test('separate Node processes reconcile one stale row atomically', async () => {
  await withQuotaEnvironment({ limit: 2, stale: 60 }, async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-process-reconcile-')); const dbPath = path.join(root, 'outgoing.sqlite3');
    const moduleUrl = new URL('./whatsapp_ledger.js', import.meta.url).href;
    const script = `const { initializeLedger, reconcileStaleAttemptedMessages } = await import(${JSON.stringify(moduleUrl)}); try { const db = initializeLedger(process.argv[1]); const result = reconcileStaleAttemptedMessages(db); db.close(); process.exit(result.reconciled_count === 1 ? 0 : 2); } catch { process.exit(1); }`;
    try {
      const db = initializeLedger(dbPath);
      reserveAndEnqueueOutgoingMessageForTest(db, quotaInput(), '2026-01-01T10:00:00.000Z');
      transitionOutgoingMessage(db, 'quota-msg-001', 'attempted', { now: '2026-01-01T10:00:00.000Z', attemptOperationId: 'operation-process-123456', attemptLeaseUntil: '2026-01-01T10:01:00.000Z' });
      closeLedger(db);
      const results = await Promise.all([runLedgerChild(script, [dbPath]), runLedgerChild(script, [dbPath])]);
      assert.deepEqual(results.map(result => result.code).sort(), [0, 2]);
      const reopened = initializeLedger(dbPath);
      assert.equal(reopened.prepare('SELECT state FROM outgoing_messages WHERE message_id = ?').get('quota-msg-001').state, 'delivered_unknown');
      assert.equal(reopened.prepare('SELECT reserved_count FROM daily_message_quota').get().reserved_count, 1);
      closeLedger(reopened);
    } finally { await fs.rm(root, { recursive: true, force: true }); }
  });
});

test('quota reservation rolls back when queued-message insertion fails', async () => {
  await withQuotaEnvironment({ limit: 2 }, async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-quota-rollback-')); const dbPath = path.join(root, 'outgoing.sqlite3');
    try {
      const db = initializeLedger(dbPath);
      reserveAndEnqueueOutgoingMessageForTest(db, quotaInput({ messageId: 'existing-message', idempotencyKey: 'existing-idem' }), '2026-01-01T00:00:00.000Z');
      assert.throws(() => reserveAndEnqueueOutgoingMessageForTest(db, quotaInput({ messageId: 'existing-message', idempotencyKey: 'new-idem' }), '2026-01-01T00:00:00.000Z'), /Unable to reserve daily message quota/);
      assert.equal(db.prepare('SELECT reserved_count FROM daily_message_quota').get().reserved_count, 1);
      assert.equal(getOutgoingMessage(db, 'existing-message').idempotency_key, 'existing-idem');
      closeLedger(db);
    } finally { await fs.rm(root, { recursive: true, force: true }); }
  });
});

test('quota reservation persists across ledger reopen and calculates Asia/Kolkata boundaries', async () => {
  await withQuotaEnvironment({ limit: 3 }, async () => {
    assert.equal(calculateQuotaDay('2026-01-01T18:29:59.999Z', 'Asia/Kolkata'), '2026-01-01');
    assert.equal(calculateQuotaDay('2026-01-01T18:30:00.000Z', 'Asia/Kolkata'), '2026-01-02');
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-quota-persist-')); const dbPath = path.join(root, 'outgoing.sqlite3');
    try {
      const db = initializeLedger(dbPath);
      const created = reserveAndEnqueueOutgoingMessageForTest(db, quotaInput(), '2026-01-01T18:30:00.000Z');
      closeLedger(db);
      const reopened = initializeLedger(dbPath);
      const row = getOutgoingMessage(reopened, created.record.message_id);
      assert.equal(row.quota_day, '2026-01-02'); assert.equal(row.quota_reserved, 1);
      assert.equal(reopened.prepare('SELECT reserved_count FROM daily_message_quota').get().reserved_count, 1);
      closeLedger(reopened);
    } finally { await fs.rm(root, { recursive: true, force: true }); }
  });
});

test('existing ledger rows survive quota schema migration', async () => {
  await withQuotaEnvironment({}, async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-quota-migration-')); const dbPath = path.join(root, 'outgoing.sqlite3');
    try {
      const legacy = new DatabaseSync(dbPath);
      legacy.exec(`CREATE TABLE outgoing_messages (
        message_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
        sender_worker_id INTEGER NOT NULL, recipient_jid TEXT NOT NULL,
        campaign_id INTEGER, task_id INTEGER, content_type TEXT NOT NULL,
        payload_reference TEXT, state TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
        failure_code TEXT, transport_message_id TEXT, created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL, attempted_at TEXT, acknowledged_at TEXT,
        delivered_unknown_at TEXT, failed_at TEXT
      )`);
      legacy.prepare(`INSERT INTO outgoing_messages
        (message_id, idempotency_key, sender_worker_id, recipient_jid, content_type,
         state, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)`)
        .run('msg-001', 'idem-001', 42, '919876543210@s.whatsapp.net', 'text', '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z');
      legacy.close();
      const reopened = initializeLedger(dbPath);
      const migrated = getOutgoingMessage(reopened, 'msg-001');
      assert.equal(migrated.idempotency_key, 'idem-001'); assert.equal(migrated.state, 'queued');
      assert.equal(migrated.quota_reserved, 0); assert.equal(migrated.quota_day, null);
      assert.equal(migrated.attempt_operation_id, null); assert.equal(migrated.attempt_lease_until, null);
      assert.equal(migrated.batch_id, null);
      assert.ok(reopened.prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'daily_message_quota'").get());
      assert.ok(reopened.prepare("SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'outgoing_messages_reconciliation_lookup'").get());
      assert.ok(reopened.prepare("SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'outgoing_messages_batch_lookup'").get());
      closeLedger(reopened);
      const rerun = initializeLedger(dbPath);
      try {
        assert.equal(getOutgoingMessage(rerun, 'msg-001').batch_id, null);
        assert.equal(rerun.prepare("SELECT COUNT(*) AS count FROM sqlite_master WHERE type = 'index' AND name = 'outgoing_messages_batch_lookup'").get().count, 1);
      } finally { closeLedger(rerun); }
    } finally { await fs.rm(root, { recursive: true, force: true }); }
  });
});

test('invalid quota configuration fails safely', async () => {
  await withQuotaEnvironment({ limit: 0 }, async () => assert.throws(() => getQuotaConfig(), /Invalid daily quota configuration/));
  await withQuotaEnvironment({ limit: 80, timezone: 'Not/A-Timezone' }, async () => assert.throws(() => getQuotaConfig(), /Invalid quota timezone configuration/));
  await withQuotaEnvironment({ limit: 80, stale: 59 }, async () => assert.throws(() => getAttemptReconciliationConfig(), /Invalid attempted-message reconciliation configuration/));
  await withQuotaEnvironment({ limit: 80, stale: 86401 }, async () => assert.throws(() => getAttemptReconciliationConfig(), /Invalid attempted-message reconciliation configuration/));
});

test('batch association migration fails closed when index creation is blocked', async () => {
  await withQuotaEnvironment({}, async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-batch-migration-failure-')); const dbPath = path.join(root, 'outgoing.sqlite3');
    try {
      const legacy = new DatabaseSync(dbPath);
      legacy.exec(`
        CREATE TABLE outgoing_messages (
          message_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
          sender_worker_id INTEGER NOT NULL, recipient_jid TEXT NOT NULL,
          campaign_id INTEGER, task_id INTEGER, content_type TEXT NOT NULL,
          payload_reference TEXT, state TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
          failure_code TEXT, transport_message_id TEXT, created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL, attempted_at TEXT, acknowledged_at TEXT,
          delivered_unknown_at TEXT, failed_at TEXT
        );
        CREATE TABLE outgoing_messages_batch_lookup (sentinel TEXT);
      `);
      legacy.prepare(`
        INSERT INTO outgoing_messages
          (message_id, idempotency_key, sender_worker_id, recipient_jid, content_type,
           state, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)
      `).run('migration-preserve-message', 'migration-preserve-key', 42,
        '919876543210@s.whatsapp.net', 'text', '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z');
      legacy.close();

      const moduleUrl = new URL('./whatsapp_ledger.js', import.meta.url).href;
      const result = await runLedgerChild(`
        const { initializeLedger } = await import(${JSON.stringify(moduleUrl)});
        try { initializeLedger(process.argv[1]); process.exit(0); }
        catch { process.exit(1); }
      `, [dbPath]);
      assert.equal(result.code, 1, JSON.stringify(result));

      const unchanged = new DatabaseSync(dbPath);
      try {
        const row = unchanged.prepare('SELECT message_id, idempotency_key FROM outgoing_messages').get();
        assert.equal(row.message_id, 'migration-preserve-message');
        assert.equal(row.idempotency_key, 'migration-preserve-key');
        assert.equal(unchanged.prepare("SELECT COUNT(*) AS count FROM pragma_table_info('outgoing_messages') WHERE name = 'batch_id'").get().count, 0);
        assert.equal(unchanged.prepare("SELECT type FROM sqlite_master WHERE name = 'outgoing_messages_batch_lookup'").get().type, 'table');
      } finally { unchanged.close(); }
    } finally { await fs.rm(root, { recursive: true, force: true }); }
  });
});

test('send timeout configuration is bounded and lease exceeds timeout', async () => {
  await withQuotaEnvironment({ sendTimeout: 1000 }, async () => {
    assert.deepEqual(getSendConfig(), { sendTimeoutMs: 1000, leaseDurationMs: 31000 });
  });
  await withQuotaEnvironment({ sendTimeout: 999 }, async () => assert.throws(() => getSendConfig(), /Invalid WhatsApp send timeout configuration/));
  await withQuotaEnvironment({ sendTimeout: 120001 }, async () => assert.throws(() => getSendConfig(), /Invalid WhatsApp send timeout configuration/));
});

test('reconciliation transitions only stale attempted rows with expired explicit leases', async () => {
  await withQuotaEnvironment({ limit: 5, stale: 900 }, async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-reconcile-')); const dbPath = path.join(root, 'outgoing.sqlite3');
    try {
      const db = initializeLedger(dbPath);
      reserveAndEnqueueOutgoingMessageForTest(db, quotaInput(), '2026-01-01T11:00:00.000Z');
      transitionOutgoingMessage(db, 'quota-msg-001', 'attempted', { now: '2026-01-01T11:00:00.000Z', attemptOperationId: 'operation-123456789012', attemptLeaseUntil: '2026-01-01T11:01:00.000Z' });
      db.prepare('UPDATE outgoing_messages SET attempt_operation_id = ?, attempt_lease_until = ?, transport_message_id = ? WHERE message_id = ?')
        .run('operation-1', '2026-01-01T11:01:00.000Z', 'transport-1', 'quota-msg-001');

      const result = reconcileStaleAttemptedMessagesForTest(db, '2026-01-01T12:00:00.000Z');
      assert.deepEqual(result, { reconciled_count: 1 });
      const row = getOutgoingMessage(db, 'quota-msg-001');
      assert.equal(row.state, 'delivered_unknown'); assert.equal(row.attempt_count, 1);
      assert.equal(row.quota_reserved, 1); assert.equal(row.quota_day, '2026-01-01');
      assert.equal(row.attempted_at, '2026-01-01T11:00:00.000Z');
      assert.equal(row.delivered_unknown_at, '2026-01-01T12:00:00.000Z');
      assert.equal(row.transport_message_id, 'transport-1'); assert.equal(row.attempt_operation_id, 'operation-1');
      assert.equal(db.prepare('SELECT reserved_count FROM daily_message_quota').get().reserved_count, 1);
      closeLedger(db);
    } finally { await fs.rm(root, { recursive: true, force: true }); }
  });
});

test('reconciliation ignores recent, active-lease, and legacy attempted rows', async () => {
  await withQuotaEnvironment({ limit: 5, stale: 900 }, async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-reconcile-')); const dbPath = path.join(root, 'outgoing.sqlite3');
    try {
      const db = initializeLedger(dbPath);
      const rows = [
        ['recent', '2026-01-01T11:50:00.000Z', '2026-01-01T12:30:00.000Z'],
        ['active', '2026-01-01T10:00:00.000Z', '2026-01-01T12:30:00.000Z'],
        ['legacy', '2026-01-01T10:00:00.000Z', null],
      ];
      for (const [suffix, attemptedAt, leaseUntil] of rows) {
        const input = quotaInput({ messageId: `message-${suffix}`, idempotencyKey: `idempotency-${suffix}` });
        reserveAndEnqueueOutgoingMessageForTest(db, input, attemptedAt);
        transitionOutgoingMessage(db, input.messageId, 'attempted', { now: attemptedAt, attemptOperationId: `operation-${suffix}-123456`, attemptLeaseUntil: leaseUntil || '2026-01-01T10:01:00.000Z' });
        db.prepare('UPDATE outgoing_messages SET attempt_operation_id = ?, attempt_lease_until = ? WHERE message_id = ?').run(`operation-${suffix}`, leaseUntil, input.messageId);
      }
      const result = reconcileStaleAttemptedMessagesForTest(db, '2026-01-01T12:00:00.000Z');
      assert.deepEqual(result, { reconciled_count: 0 });
      for (const [suffix] of rows) assert.equal(getOutgoingMessage(db, `message-${suffix}`).state, 'attempted');
      closeLedger(db);
    } finally { await fs.rm(root, { recursive: true, force: true }); }
  });
});

test('reconciliation is idempotent and late completion cannot overwrite delivered_unknown', async () => {
  await withQuotaEnvironment({ limit: 5, stale: 900 }, async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-reconcile-repeat-')); const dbPath = path.join(root, 'outgoing.sqlite3');
    try {
      const first = initializeLedger(dbPath); const second = initializeLedger(dbPath);
      reserveAndEnqueueOutgoingMessageForTest(first, quotaInput(), '2026-01-01T10:00:00.000Z');
      transitionOutgoingMessage(first, 'quota-msg-001', 'attempted', { now: '2026-01-01T10:00:00.000Z', attemptOperationId: 'operation-123456789012', attemptLeaseUntil: '2026-01-01T10:01:00.000Z' });
      first.prepare('UPDATE outgoing_messages SET attempt_operation_id = ?, attempt_lease_until = ? WHERE message_id = ?').run('operation-1', '2026-01-01T10:01:00.000Z', 'quota-msg-001');
      const results = await Promise.all([
        Promise.resolve().then(() => reconcileStaleAttemptedMessagesForTest(first, '2026-01-01T12:00:00.000Z')),
        Promise.resolve().then(() => reconcileStaleAttemptedMessagesForTest(second, '2026-01-01T12:00:00.000Z')),
      ]);
      assert.equal(results[0].reconciled_count + results[1].reconciled_count, 1);
      assert.deepEqual(reconcileStaleAttemptedMessages(second), { reconciled_count: 0 });
      assert.throws(() => transitionOutgoingMessage(second, 'quota-msg-001', 'acknowledged'), /Invalid message state transition/);
      assert.equal(second.prepare('SELECT reserved_count FROM daily_message_quota').get().reserved_count, 1);
      closeLedger(first); closeLedger(second);
    } finally { await fs.rm(root, { recursive: true, force: true }); }
  });
});

test('production reservation ignores caller-supplied time options', async () => {
  await withQuotaEnvironment({ limit: 2 }, async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-quota-clock-')); const dbPath = path.join(root, 'outgoing.sqlite3');
    try {
      const db = initializeLedger(dbPath);
      const result = reserveAndEnqueueOutgoingMessage(db, quotaInput(), { now: '2000-01-01T00:00:00.000Z' });
      assert.equal(result.record.quota_day, calculateQuotaDay(new Date(), 'Asia/Kolkata'));
      closeLedger(db);
    } finally { await fs.rm(root, { recursive: true, force: true }); }
  });
});
test('message content builder creates text and URL text without I/O', () => {
  let networkCalls = 0;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => { networkCalls += 1; throw new Error('network must not be called'); };
  try {
    assert.deepEqual(buildMessageContent('text', { text: 'Hello from TaskPe' }), { text: 'Hello from TaskPe' });
    assert.deepEqual(buildMessageContent('text', { text: 'Read https://example.test/offer' }), { text: 'Read https://example.test/offer' });
    assert.equal(networkCalls, 0);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('message content builder creates a native cta_url button without network access', () => {
  const payload = buildMessageContent('text', { text: 'Visit our website', ctaUrl: 'https://example.test/offer' });
  assert.equal(payload.interactiveMessage.nativeFlowMessage.buttons[0].name, 'cta_url');
  assert.deepEqual(JSON.parse(payload.interactiveMessage.nativeFlowMessage.buttons[0].buttonParamsJson), {
    display_text: 'Open Website',
    url: 'https://example.test/offer',
    merchant_url: 'https://example.test/offer',
  });
  assert.throws(() => buildMessageContent('text', { text: 'bad', ctaUrl: 'http://example.test' }), /valid HTTPS destination URL/);
  assert.throws(() => buildMessageContent('text', { text: 'bad', ctaUrl: 'https://example.test', buttonLabel: '' }), /CTA button label/);
});

test('image cta_url content requires a trusted prepared image message', () => {
  const preparedImageMessage = { url: 'https://media.example.test/image.enc', mediaKey: Buffer.from('key') };
  const payload = buildMessageContent('image', {
    media: Buffer.from('source-image'), caption: 'Image caption', ctaUrl: 'https://example.test', preparedImageMessage,
  });
  assert.deepEqual(payload.interactiveMessage.header.imageMessage, preparedImageMessage);
  assert.equal(payload.interactiveMessage.nativeFlowMessage.buttons[0].name, 'cta_url');
  assert.throws(() => buildMessageContent('image', { media: Buffer.from('source-image'), caption: 'caption', ctaUrl: 'https://example.test' }), /prepared image message/);
});

test('TaskPe campaign resolver reads trusted task content and approved assets only', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-content-resolver-'));
  const databasePath = path.join(root, 'taskpe.sqlite3'); const assetRoot = path.join(root, 'assets');
  const validPng = Buffer.from('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489', 'hex');
  await fs.mkdir(assetRoot); await fs.writeFile(path.join(assetRoot, 'campaign.png'), validPng);
  const db = new DatabaseSync(databasePath);
  db.exec('CREATE TABLE tasks (id INTEGER PRIMARY KEY, campaign_id INTEGER, task_type TEXT, template_text TEXT, image_path TEXT, destination_link TEXT)');
  db.prepare('INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)').run(9, 7, 'TEXT_LINK', 'Trusted task text', null, 'https://business.example.test/offer');
  db.prepare('INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)').run(10, 7, 'IMAGE_TEXT_LINK', 'Trusted image text', 'campaign.png', 'https://business.example.test/offer');
  db.close();
  try {
    const resolver = createTaskPeCampaignContentResolver({ databasePath, assetRoot });
    const batch = { task_id: 9, campaign_id: 7 };
    const text = await resolver({ batch, item: { content_type: 'text', payload_reference: 'template:v1' } });
    assert.equal(text.text, 'Trusted task text'); assert.equal(text.ctaUrl, 'https://business.example.test/offer');
    const image = await resolver({ batch: { task_id: 10, campaign_id: 7 }, item: { content_type: 'image', payload_reference: 'template:v1' } });
    assert.deepEqual(image.media, validPng); assert.equal(image.payloadReference, 'template:v1');
    await assert.rejects(() => resolver({ batch, item: { content_type: 'image', payload_reference: 'template:v1' } }), /Approved image campaign content is unavailable/);
    await assert.rejects(() => resolver({ batch, item: { content_type: 'text', payload_reference: 'arbitrary:browser-content' } }), /Unsupported campaign content reference/);

    const changed = new DatabaseSync(databasePath);
    changed.prepare('UPDATE tasks SET destination_link = ?, image_path = ? WHERE id = 10').run('http://unsafe.example.test', 'missing.png');
    changed.close();
    await assert.rejects(() => resolver({ batch: { task_id: 10, campaign_id: 7 }, item: { content_type: 'image', payload_reference: 'template:v1' } }), /valid HTTPS destination URL/);
    const restored = new DatabaseSync(databasePath);
    restored.prepare('UPDATE tasks SET destination_link = ?, image_path = ? WHERE id = 10').run('https://business.example.test/offer', 'missing.png');
    restored.close();
    await assert.rejects(() => resolver({ batch: { task_id: 10, campaign_id: 7 }, item: { content_type: 'image', payload_reference: 'template:v1' } }), /Approved task asset is invalid/);

    const ledgerRoot = path.join(root, 'ledger'); await fs.mkdir(ledgerRoot);
    const ledgerDb = initializeLedger(path.join(ledgerRoot, 'ledger.sqlite3')); const repo = createLedgerRepository(ledgerDb);
    const service = createService({ enabled: true, secret, sessionRoot: ledgerRoot, ledgerRepository: repo, taskDatabasePath: databasePath, taskAssetRoot: assetRoot, loadBaileys: false, allowUnsupportedFilesystem: true });
      const calls = []; service.connections.set('42', { socket: { relayMessage: async (...args) => { calls.push(args); return {}; } } });
    try {
      const input = campaignBatchInput({ batchId: 'batch-resolver-wired-001', taskId: 9, campaignId: 7, items: [{ planRecipientId: 301, recipientJid: '919876543210@s.whatsapp.net', contentType: 'text', payloadReference: 'template:v1' }] });
      reserveCampaignBatchForTest(ledgerDb, input, '2026-01-01T00:00:00.000Z');
      const sendPath = '/v1/users/42/campaign-batches/batch-resolver-wired-001/send';
      const response = await invoke(service, signedPathRequest('POST', sendPath, 42));
      assert.equal(response.status, 503, JSON.stringify(response));
      assert.deepEqual(response.body, { success: false, status: 'sending_disabled', message: 'Campaign sending is unavailable.' });
      assert.equal(calls.length, 0);
    } finally { await service.closeAll(); closeLedger(ledgerDb); }
  } finally { await fs.rm(root, { recursive: true, force: true }); }
});

test('TaskPe asset resolver accepts only supported image signatures and complete containers', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-asset-signatures-'));
  const databasePath = path.join(root, 'taskpe.sqlite3'); const assetRoot = path.join(root, 'assets');
  const png = Buffer.from('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489', 'hex');
  const jpeg = Buffer.from('ffd8ffd9', 'hex');
  const webp = Buffer.from('524946460400000057454250', 'hex');
  await fs.mkdir(assetRoot);
  await fs.writeFile(path.join(assetRoot, 'valid.png'), png);
  await fs.writeFile(path.join(assetRoot, 'valid.jpg'), jpeg);
  await fs.writeFile(path.join(assetRoot, 'valid.webp'), webp);
  const db = new DatabaseSync(databasePath);
  db.exec('CREATE TABLE tasks (id INTEGER PRIMARY KEY, campaign_id INTEGER, task_type TEXT, template_text TEXT, image_path TEXT, destination_link TEXT)');
  for (const [id, filename] of [[1, 'valid.png'], [2, 'valid.jpg'], [3, 'valid.webp']]) {
    db.prepare('INSERT INTO tasks VALUES (?, 1, ?, ?, ?, ?)').run(id, 'IMAGE_TEXT_LINK', `caption-${id}`, filename, 'https://business.example.test/offer');
  }
  db.close();
  try {
    const resolver = createTaskPeCampaignContentResolver({ databasePath, assetRoot });
    for (const id of [1, 2, 3]) {
      const result = await resolver({ batch: { task_id: id, campaign_id: 1 }, item: { content_type: 'image', payload_reference: 'template:v1' } });
      assert.equal(result.type, 'image');
    }
    await fs.writeFile(path.join(assetRoot, 'renamed.png'), Buffer.from('plain text, not an image'));
    await fs.writeFile(path.join(assetRoot, 'truncated.png'), png.subarray(0, 8));
    await fs.writeFile(path.join(assetRoot, 'empty.png'), Buffer.alloc(0));
    const changed = new DatabaseSync(databasePath);
    changed.prepare('INSERT INTO tasks VALUES (?, 1, ?, ?, ?, ?)').run(4, 'IMAGE_TEXT_LINK', 'bad', 'renamed.png', 'https://business.example.test/offer');
    changed.prepare('INSERT INTO tasks VALUES (?, 1, ?, ?, ?, ?)').run(5, 'IMAGE_TEXT_LINK', 'bad', 'truncated.png', 'https://business.example.test/offer');
    changed.prepare('INSERT INTO tasks VALUES (?, 1, ?, ?, ?, ?)').run(6, 'IMAGE_TEXT_LINK', 'bad', 'empty.png', 'https://business.example.test/offer');
    changed.prepare('INSERT INTO tasks VALUES (?, 1, ?, ?, ?, ?)').run(7, 'IMAGE_TEXT_LINK', 'bad', 'valid.png', 'https://business.example.test/offer');
    changed.close();
    for (const id of [4, 5, 6]) await assert.rejects(() => resolver({ batch: { task_id: id, campaign_id: 1 }, item: { content_type: 'image', payload_reference: 'template:v1' } }), /Approved task asset is invalid/);
    await fs.writeFile(path.join(assetRoot, 'valid.png'), Buffer.alloc(10 * 1024 * 1024 + 1, 0x01));
    await assert.rejects(() => resolver({ batch: { task_id: 7, campaign_id: 1 }, item: { content_type: 'image', payload_reference: 'template:v1' } }), /Approved task asset is invalid/);
  } finally { await fs.rm(root, { recursive: true, force: true }); }
});

test('message content builder creates image and video captions', () => {
  const image = Buffer.from('image-bytes');
  const video = Buffer.from('video-bytes');
  assert.deepEqual(buildMessageContent('image', { media: image, caption: 'Image caption' }), { image, caption: 'Image caption' });
  assert.deepEqual(buildMessageContent('video', { media: video, caption: 'Video caption' }), { video, caption: 'Video caption', ptv: false });
});

test('message content builder rejects missing or unsupported content', () => {
  assert.throws(() => buildMessageContent('text'), /Message text is required/);
  assert.throws(() => buildMessageContent('text', { text: '   ' }), /Message text is required/);
  assert.throws(() => buildMessageContent('image', { media: Buffer.from('x') }), /Media caption is required/);
  assert.throws(() => buildMessageContent('video', { caption: 'caption' }), /Media buffer is required/);
  assert.throws(() => buildMessageContent('image', { caption: 'caption', media: Buffer.from([]) }), /Media buffer is required/);
  assert.throws(() => buildMessageContent('image', { caption: 'caption', media: 'not-a-buffer' }), /Media buffer is required/);
  assert.throws(() => buildMessageContent('audio', { caption: 'caption', media: Buffer.from('x') }), /Unsupported message type/);
});

/* Retired direct/unfenced helper tests; campaign coverage uses the coordinated
 * reserved-batch path below. */
/* test('injected send adapter forwards each supported payload and returns acknowledgement', async () => {
  const calls = [];
  const socket = { sendMessage: async (...args) => { calls.push(args); return { key: { id: 'not-returned' } }; } };
  const jid = '919876543210@s.whatsapp.net';
  const image = Buffer.from('image-bytes');
  const video = Buffer.from('video-bytes');

  assert.deepEqual(await sendMessageWithSocket(socket, jid, { type: 'text', text: 'hello' }, { testOnlyUnfenced: true }), { accepted: true, status: 'acknowledged', delivered: false });
  assert.deepEqual(await sendMessageWithSocket(socket, jid, { type: 'text', text: 'See https://example.test/offer' }, { testOnlyUnfenced: true }), { accepted: true, status: 'acknowledged', delivered: false });
  assert.deepEqual(await sendMessageWithSocket(socket, jid, { type: 'image', media: image, caption: 'image caption' }, { testOnlyUnfenced: true }), { accepted: true, status: 'acknowledged', delivered: false });
  assert.deepEqual(await sendMessageWithSocket(socket, jid, { type: 'video', media: video, caption: 'video caption' }, { testOnlyUnfenced: true }), { accepted: true, status: 'acknowledged', delivered: false });
  assert.equal(calls.length, 4);
  assert.deepEqual(calls[0], [jid, { text: 'hello' }]);
  assert.deepEqual(calls[1], [jid, { text: 'See https://example.test/offer' }]);
  assert.deepEqual(calls[2], [jid, { image, caption: 'image caption' }]);
  assert.deepEqual(calls[3], [jid, { video, caption: 'video caption', ptv: false }]);
});

test('reserved campaign item sends text cta_url once and does not reserve quota again', async () => {
  await withQuotaEnvironment({ limit: 2 }, async () => {
    await withLedgerTest(async (ledger, db) => {
      const input = campaignBatchInput({
        batchId: 'batch-send-001',
        items: [{ planRecipientId: 201, recipientJid: '919876543210@s.whatsapp.net', contentType: 'text', payloadReference: 'trusted-text-201' }],
      });
      reserveCampaignBatchForTest(db, input, '2026-01-01T00:00:00.000Z');
      const batch = ledger.getCampaignBatch(input.batchId, input.senderWorkerId);
      const calls = [];
      const socket = { relayMessage: async (...args) => { calls.push(args); return { key: { id: 'transport-not-returned' } }; } };
      const resolver = async ({ item }) => ({ type: 'text', text: 'Trusted campaign text', payloadReference: item.payload_reference, ctaUrl: 'https://example.test/open' });

      const first = await sendReservedCampaignItem({ ledger, socket, batch: batch.batch, item: batch.items[0], resolveContent: resolver, testOnlyUnfenced: true });
      assert.deepEqual(first, { accepted: true, status: 'acknowledged', message_id: batch.items[0].message_id, delivered: false });
      assert.equal(calls.length, 1);
      assert.equal(calls[0][0], batch.items[0].recipient_jid);
      assert.equal(calls[0][1].interactiveMessage.nativeFlowMessage.buttons[0].name, 'cta_url');
      assert.equal(db.prepare('SELECT reserved_count FROM daily_message_quota').get().reserved_count, 1);
      assert.equal(getOutgoingMessage(db, batch.items[0].message_id).state, 'acknowledged');

      const duplicate = await sendReservedCampaignItem({ ledger, socket, batch: batch.batch, item: batch.items[0], resolveContent: resolver, testOnlyUnfenced: true });
      assert.deepEqual(duplicate, { accepted: false, status: 'not_sendable', message: 'Campaign item is no longer queued.', message_id: batch.items[0].message_id, delivered: false });
      assert.equal(calls.length, 1);
    });
  });
});

test('reserved image campaign item uses a prepared image and sanitized uncertain result', async () => {
  await withQuotaEnvironment({ limit: 2, sendTimeout: 1000 }, async () => {
    await withLedgerTest(async (ledger, db) => {
      const input = campaignBatchInput({
        batchId: 'batch-send-002',
        items: [{ planRecipientId: 202, recipientJid: '919876543211@s.whatsapp.net', contentType: 'image', payloadReference: 'trusted-image-202' }],
      });
      reserveCampaignBatchForTest(db, input, '2026-01-01T00:00:00.000Z');
      const batch = ledger.getCampaignBatch(input.batchId, input.senderWorkerId);
      const socket = { relayMessage: async () => new Promise(() => {}) };
      const resolver = async ({ item }) => ({
        type: 'image', media: Buffer.from('image'), caption: 'Trusted image', payloadReference: item.payload_reference,
        ctaUrl: 'https://example.test/image', preparedImageMessage: { url: 'https://media.example.test/image.enc' },
      });
      const result = await sendReservedCampaignItem({ ledger, socket, batch: batch.batch, item: batch.items[0], resolveContent: resolver, testOnlyUnfenced: true });
      assert.deepEqual(result, { accepted: false, status: 'uncertain', message: 'Message send outcome is unknown.', message_id: batch.items[0].message_id, delivered: false });
      assert.equal(getOutgoingMessage(db, batch.items[0].message_id).state, 'delivered_unknown');
      assert.equal(db.prepare('SELECT reserved_count FROM daily_message_quota').get().reserved_count, 1);
    });
  });
});
*/

test('image campaign HTTP dispatch remains blocked before Baileys preparation', async () => {
  await withQuotaEnvironment({ limit: 2, sendTimeout: 1000 }, async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-image-prep-'));
    const db = initializeLedger(path.join(root, 'ledger.sqlite3')); const ledger = createLedgerRepository(db);
    try {
      const input = campaignBatchInput({
        batchId: 'batch-image-native-flow-001',
        items: [{ planRecipientId: 203, recipientJid: '919876543212@s.whatsapp.net', contentType: 'image', payloadReference: 'template:image-v1' }],
      });
      reserveCampaignBatchForTest(db, input, '2026-01-01T00:00:00.000Z');
      const batch = ledger.getCampaignBatch(input.batchId, input.senderWorkerId);
      const uploads = []; const relays = [];
      const service = createService({
        enabled: true, secret, sessionRoot: root, ledgerRepository: ledger, allowUnsupportedFilesystem: true,
        loadBaileys: true,
        baileysLoader: async () => ({
          prepareWAMessageMedia: async (message, options) => {
            assert.ok(Buffer.isBuffer(message.image));
            const uploaded = await options.upload(Buffer.from('upload-source'));
            return { imageMessage: { url: uploaded.url } };
          },
        }),
        campaignContentResolver: async ({ item }) => ({ type: 'image', media: Buffer.from('trusted-image'), caption: 'Trusted image', payloadReference: item.payload_reference, ctaUrl: 'https://business.example.test/offer' }),
      });
      const socket = {
        waUploadToServer: async data => { uploads.push(data); return { url: 'https://media.example.test/uploaded' }; },
        relayMessage: async (...args) => { relays.push(args); return {}; },
      };
      service.connections.set('42', { socket });
      try {
        const result = await invoke(service, signedPathRequest('POST', '/v1/users/42/campaign-batches/batch-image-native-flow-001/send', 42));
        assert.equal(result.status, 503, JSON.stringify(result));
        assert.deepEqual(result.body, { success: false, status: 'sending_disabled', message: 'Campaign sending is unavailable.' });
        assert.equal(relays.length, 0);
        assert.equal(uploads.length, 0);
      } finally { await service.closeAll(); }
    } finally { closeLedger(db); await fs.rm(root, { recursive: true, force: true }); }
  });
});

/* Direct socket-helper tests were removed with the production export. */
/* test('injected send adapter rejects invalid recipients before invoking the socket', async () => {
  let calls = 0;
  const socket = { sendMessage: async () => { calls += 1; } };
  for (const jid of ['', 'not-a-jid', '12345@c.us', '12345-678@g.us', '0@s.whatsapp.net']) {
    const result = await sendMessageWithSocket(socket, jid, { type: 'text', text: 'not sent' }, { testOnlyUnfenced: true });
    assert.deepEqual(result, { accepted: false, status: 'validation_error', message: 'Invalid recipient.' });
  }
  assert.equal(calls, 0);
});

test('injected send adapter sanitizes socket failures', async () => {
  const socket = { sendMessage: async () => { throw new Error('private transport details'); } };
  const result = await sendMessageWithSocket(socket, '919876543210@s.whatsapp.net', { type: 'text', text: 'not logged' }, { testOnlyUnfenced: true });
  assert.deepEqual(result, { accepted: false, status: 'transport_error', message: 'Message send failed.' });
});
*/

test('valid internal signature is accepted and invalid secret is rejected', () => {
  const pathname = '/v1/users/42/status'; const timestamp = String(Math.floor(Date.now() / 1000)); const body = ''; const requestId = 'request-id-with-at-least-32-characters-1234';
  const signature = crypto.createHmac('sha256', secret).update(`${timestamp}.${requestId}.GET.${pathname}.${body}`).digest('hex');
  const request = { method: 'GET', pathname, body, headers: { 'x-taskpe-timestamp': timestamp, 'x-taskpe-request-id': requestId, 'x-taskpe-signature': signature, 'x-taskpe-user-id': '42' } };
  assert.equal(verifyInternalRequest({ ...request, secret }), true);
  assert.equal(verifyInternalRequest({ ...request, secret: 'wrong-secret' }), false);
  assert.equal(verifyInternalRequest({ ...request, secret, body: 'changed' }), false);
  assert.equal(verifyInternalRequest({ ...request, secret, pathname: '/v1/users/43/status' }), false);
  assert.equal(verifyInternalRequest({ ...request, secret, now: Date.now() + 61001 }), false);
});

test('replay cache atomically rejects repeated and over-capacity request IDs', () => {
  let now = Date.now();
  const cache = new ReplayCache({ maxEntries: 2, now: () => now });
  assert.equal(cache.accept('first-request-id-12345678901234567890', Math.floor(now / 1000)), true);
  assert.equal(cache.accept('first-request-id-12345678901234567890', Math.floor(now / 1000)), false);
  assert.equal(cache.accept('second-request-id-12345678901234567890', Math.floor(now / 1000)), true);
  assert.equal(cache.accept('third-request-id-12345678901234567890', Math.floor(now / 1000)), false);
  now += 62000;
  assert.equal(cache.accept('third-request-id-12345678901234567890', Math.floor(now / 1000)), true);
});

test('authenticated batch reservation and lookup use HMAC/replay protection without sockets', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-batch-api-')); const db = initializeLedger(path.join(root, 'ledger.sqlite3'));
  const repo = createLedgerRepository(db);
  const service = createService({ enabled: true, secret, sessionRoot: root, ledgerRepository: repo, loadBaileys: false, allowUnsupportedFilesystem: true });
  try {
    const input = campaignBatchInput({ batchId: 'batch-api-001' }); const body = JSON.stringify(input);
    assert.deepEqual(parseInternalPath('/v1/users/42/campaign-batches/reserve'), { userId: '42', action: 'batch_reserve' });
    assert.deepEqual(parseInternalPath('/v1/users/42/campaign-batches/batch-api-001'), { userId: '42', action: 'batch_lookup', batchId: 'batch-api-001' });
    const reserved = await invoke(service, signedPathRequest('POST', '/v1/users/42/campaign-batches/reserve', 42, body));
    assert.equal(reserved.status, 200, JSON.stringify(reserved)); assert.equal(reserved.body.batch.item_count, 2);
    const lookup = await invoke(service, signedPathRequest('GET', '/v1/users/42/campaign-batches/batch-api-001', 42));
    assert.equal(lookup.status, 200); assert.equal(lookup.body.found, true); assert.equal(lookup.body.batch.items.length, 2);
    const wrongWorker = await invoke(service, signedPathRequest('POST', '/v1/users/42/campaign-batches/reserve', 42, JSON.stringify({ ...input, senderWorkerId: 43, requestFingerprint: calculateCampaignBatchRequestFingerprint({ ...input, senderWorkerId: 43 }) })));
    assert.equal(wrongWorker.status, 403);
    const repeatedId = 'batch-api-repeated-request-id-1234567890'; const repeated = signedPathRequest('GET', '/v1/users/42/campaign-batches/batch-api-001', 42, '', repeatedId);
    assert.equal((await invoke(service, repeated)).status, 200); assert.equal((await invoke(service, repeated)).status, 401);
  } finally { await service.closeAll(); closeLedger(db); await fs.rm(root, { recursive: true, force: true }); }
});

test('authenticated batch send route remains fail-closed before socket dispatch', async () => {
  await withQuotaEnvironment({ limit: 3 }, async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-send-api-')); const db = initializeLedger(path.join(root, 'ledger.sqlite3'));
    const repo = createLedgerRepository(db); const calls = [];
    const service = createService({
      enabled: true, secret, sessionRoot: root, ledgerRepository: repo, loadBaileys: false, allowUnsupportedFilesystem: true,
      campaignContentResolver: async ({ item }) => item.content_type === 'text'
        ? { type: 'text', text: 'Trusted text', payloadReference: item.payload_reference, ctaUrl: 'https://example.test/open' }
        : { type: 'image', media: Buffer.from('image'), caption: 'Trusted image', payloadReference: item.payload_reference, ctaUrl: 'https://example.test/open', preparedImageMessage: { url: 'https://media.example.test/image.enc' } },
    });
    service.connections.set('42', { socket: { relayMessage: async (...args) => { calls.push(args); return {}; } } });
    try {
      const input = campaignBatchInput({ batchId: 'batch-send-api-001' });
      reserveCampaignBatchForTest(db, input, '2026-01-01T00:00:00.000Z');
      const pathName = '/v1/users/42/campaign-batches/batch-send-api-001/send';
      assert.deepEqual(parseInternalPath(pathName), { userId: '42', action: 'batch_send', batchId: 'batch-send-api-001' });
      const response = await invoke(service, signedPathRequest('POST', pathName, 42));
      assert.equal(response.status, 503, JSON.stringify(response));
      assert.deepEqual(response.body, { success: false, status: 'sending_disabled', message: 'Campaign sending is unavailable.' });
      assert.equal(calls.length, 0);
      assert.equal(db.prepare('SELECT COUNT(*) AS count FROM outgoing_messages WHERE state = \'queued\'').get().count, 2);
      const replay = signedPathRequest('POST', pathName, 42, '', 'batch-send-route-replay-request-1234567890');
      assert.equal((await invoke(service, replay)).status, 503);
      assert.equal((await invoke(service, replay)).status, 401);
      assert.equal(calls.length, 0);
    } finally { await service.closeAll(); closeLedger(db); await fs.rm(root, { recursive: true, force: true }); }
  });
});

test('status, quota, and reservation routes remain non-sending', async () => {
  await withQuotaEnvironment({ limit: 3 }, async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-non-send-routes-')); const db = initializeLedger(path.join(root, 'ledger.sqlite3'));
    const repo = createLedgerRepository(db); let socketCalls = 0;
    const service = createService({
      enabled: true, secret, sessionRoot: root, ledgerRepository: repo, loadBaileys: false, allowUnsupportedFilesystem: true,
    });
    service.connections.set('42', { socket: { sendMessage: async () => { socketCalls += 1; } } });
    try {
      const input = campaignBatchInput({ batchId: 'batch-non-send-routes-001' });
      const reserve = await invoke(service, signedPathRequest('POST', '/v1/users/42/campaign-batches/reserve', 42, JSON.stringify(input)));
      assert.equal(reserve.status, 200, JSON.stringify(reserve));
      const quota = await invoke(service, signedPathRequest('GET', '/v1/users/42/quota', 42));
      assert.equal(quota.status, 200, JSON.stringify(quota));
      const status = await invoke(service, signedPathRequest('GET', '/v1/users/42/status', 42));
      assert.equal(status.status, 200, JSON.stringify(status));
      assert.equal(socketCalls, 0);
    } finally { await service.closeAll(); closeLedger(db); await fs.rm(root, { recursive: true, force: true }); }
  });
});

test('cancellation evidence reports absent without mutating the ledger', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-cancel-absent-')); const db = initializeLedger(path.join(root, 'ledger.sqlite3'));
  const repo = createLedgerRepository(db); const service = createService({ enabled: true, secret, sessionRoot: root, ledgerRepository: repo, loadBaileys: false, allowUnsupportedFilesystem: true });
  try {
    reserveAndEnqueueOutgoingMessageForTest(db, ledgerInput({ messageId: 'legacy-unassociated', idempotencyKey: 'legacy-unassociated-key' }), '2026-01-01T00:00:00.000Z');
    const before = db.prepare('SELECT COUNT(*) AS count FROM outgoing_messages').get().count;
    const result = await invoke(service, signedPathRequest('GET', '/v1/users/42/campaign-batches/missing-batch/cancellation-status', 42));
    assert.deepEqual(result.body, { success: true, status: 'absent', batch_id: 'missing-batch' });
    assert.equal(db.prepare('SELECT COUNT(*) AS count FROM outgoing_messages').get().count, before);
    assert.equal(getCampaignBatchCancellationEvidence(db, 'missing-batch', 42).status, 'absent');
  } finally { await service.closeAll(); closeLedger(db); await fs.rm(root, { recursive: true, force: true }); }
});

test('cancellation evidence validates every batch item and outgoing identity', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-cancel-present-')); const db = initializeLedger(path.join(root, 'ledger.sqlite3'));
  const repo = createLedgerRepository(db); const service = createService({ enabled: true, secret, sessionRoot: root, ledgerRepository: repo, loadBaileys: false, allowUnsupportedFilesystem: true });
  try {
    const input = campaignBatchInput({ batchId: 'cancel-evidence-001' });
    reserveCampaignBatchForTest(db, input, '2026-01-01T00:00:00.000Z');
    const before = db.prepare('SELECT reserved_count FROM daily_message_quota').get().reserved_count;
    const result = await invoke(service, signedPathRequest('GET', '/v1/users/42/campaign-batches/cancel-evidence-001/cancellation-status', 42));
    assert.deepEqual(result.body, {
      success: true,
      status: 'present',
      batch_id: 'cancel-evidence-001',
      request_fingerprint: input.requestFingerprint,
      batch_record_present: true,
      item_count: 2,
      outgoing_record_count: 2,
      outgoing_state_counts: { queued: 2 },
    });
    assert.equal(db.prepare('SELECT reserved_count FROM daily_message_quota').get().reserved_count, before);
  } finally { await service.closeAll(); closeLedger(db); await fs.rm(root, { recursive: true, force: true }); }
});

test('cancellation evidence detects orphaned batch mappings and identity mismatches', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-cancel-invalid-')); const db = initializeLedger(path.join(root, 'ledger.sqlite3'));
  try {
    const input = campaignBatchInput({ batchId: 'cancel-evidence-002' });
    reserveCampaignBatchForTest(db, input, '2026-01-01T00:00:00.000Z');
    db.exec('PRAGMA foreign_keys = OFF');
    db.prepare('DELETE FROM whatsapp_campaign_batches WHERE batch_id = ?').run(input.batchId);
    assert.deepEqual(getCampaignBatchCancellationEvidence(db, input.batchId, 42), {
      status: 'present', batch_id: input.batchId, batch_record_present: false, outgoing_record_count: 2,
    });
    db.prepare('DELETE FROM whatsapp_campaign_batch_items WHERE batch_id = ?').run(input.batchId);
    assert.deepEqual(getCampaignBatchCancellationEvidence(db, input.batchId, 42), {
      status: 'present', batch_id: input.batchId, batch_record_present: false, outgoing_record_count: 2,
    });
    assert.deepEqual(getCampaignBatchCancellationEvidence(db, input.batchId, 43), {
      status: 'absent', batch_id: input.batchId,
    });

    const secondInput = campaignBatchInput({ batchId: 'cancel-evidence-003', planId: 13, items: [
      { planRecipientId: 201, recipientJid: '919876543220@s.whatsapp.net', contentType: 'text', payloadReference: 'content-c' },
      { planRecipientId: 202, recipientJid: '919876543221@s.whatsapp.net', contentType: 'video', payloadReference: 'content-d' },
    ] });
    reserveCampaignBatchForTest(db, secondInput, '2026-01-01T00:00:00.000Z');
    db.prepare('UPDATE outgoing_messages SET recipient_jid = ? WHERE idempotency_key = ?')
      .run('919999999999@s.whatsapp.net', deriveCampaignPlanRecipientIdempotencyKey(secondInput.planId, secondInput.items[0].planRecipientId));
    assert.deepEqual(getCampaignBatchCancellationEvidence(db, secondInput.batchId, 42), {
      status: 'invalid', batch_id: 'cancel-evidence-003',
    });
    const secondKey = deriveCampaignPlanRecipientIdempotencyKey(secondInput.planId, secondInput.items[0].planRecipientId);
    db.prepare('UPDATE outgoing_messages SET recipient_jid = ?, batch_id = ? WHERE idempotency_key = ?')
      .run(secondInput.items[0].recipientJid, 'other-batch', secondKey);
    assert.deepEqual(getCampaignBatchCancellationEvidence(db, secondInput.batchId, 42), {
      status: 'invalid', batch_id: 'cancel-evidence-003',
    });
  } finally { closeLedger(db); await fs.rm(root, { recursive: true, force: true }); }
});

test('cancellation evidence rejects malformed associated orphan records', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-cancel-orphan-invalid-')); const db = initializeLedger(path.join(root, 'ledger.sqlite3'));
  try {
    const input = campaignBatchInput({ batchId: 'cancel-evidence-orphan-invalid' });
    reserveCampaignBatchForTest(db, input, '2026-01-01T00:00:00.000Z');
    db.exec('PRAGMA foreign_keys = OFF');
    db.prepare('DELETE FROM whatsapp_campaign_batches WHERE batch_id = ?').run(input.batchId);
    db.prepare('DELETE FROM whatsapp_campaign_batch_items WHERE batch_id = ?').run(input.batchId);
    db.prepare('UPDATE outgoing_messages SET recipient_jid = ? WHERE batch_id = ?').run('malformed-recipient', input.batchId);
    assert.deepEqual(getCampaignBatchCancellationEvidence(db, input.batchId, 42), {
      status: 'invalid', batch_id: input.batchId,
    });
  } finally { closeLedger(db); await fs.rm(root, { recursive: true, force: true }); }
});

test('cancellation evidence rejects a retained mapping with an incorrect deterministic key', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-cancel-key-invalid-')); const db = initializeLedger(path.join(root, 'ledger.sqlite3'));
  try {
    const input = campaignBatchInput({ batchId: 'cancel-evidence-key-invalid' });
    reserveCampaignBatchForTest(db, input, '2026-01-01T00:00:00.000Z');
    db.exec('PRAGMA foreign_keys = OFF');
    db.prepare('DELETE FROM whatsapp_campaign_batches WHERE batch_id = ?').run(input.batchId);
    db.prepare('UPDATE whatsapp_campaign_batch_items SET idempotency_key = ? WHERE batch_id = ? AND position = 1')
      .run('wrong-key', input.batchId);
    assert.deepEqual(getCampaignBatchCancellationEvidence(db, input.batchId, 42), {
      status: 'invalid', batch_id: input.batchId,
    });
  } finally { closeLedger(db); await fs.rm(root, { recursive: true, force: true }); }
});

test('cancellation evidence reports actual SQLite lookup failure as unavailable to callers', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-cancel-sqlite-failure-')); const db = initializeLedger(path.join(root, 'ledger.sqlite3'));
  try {
    closeLedger(db);
    assert.throws(() => getCampaignBatchCancellationEvidence(db, 'cancel-evidence-sqlite-failure', 42));
  } finally { await fs.rm(root, { recursive: true, force: true }); }
});

test('cancellation evidence rejects missing outgoing rows and malformed persisted items', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-cancel-malformed-')); const db = initializeLedger(path.join(root, 'ledger.sqlite3'));
  try {
    const input = campaignBatchInput({ batchId: 'cancel-evidence-004' });
    reserveCampaignBatchForTest(db, input, '2026-01-01T00:00:00.000Z');
    db.prepare('DELETE FROM outgoing_messages WHERE idempotency_key = ?')
      .run(deriveCampaignPlanRecipientIdempotencyKey(input.planId, input.items[1].planRecipientId));
    assert.deepEqual(getCampaignBatchCancellationEvidence(db, input.batchId, 42), {
      status: 'invalid', batch_id: input.batchId,
    });
    db.prepare('UPDATE whatsapp_campaign_batch_items SET position = ? WHERE batch_id = ? AND position = 1')
      .run(3, input.batchId);
    assert.deepEqual(getCampaignBatchCancellationEvidence(db, input.batchId, 42), {
      status: 'invalid', batch_id: input.batchId,
    });
  } finally { closeLedger(db); await fs.rm(root, { recursive: true, force: true }); }
});

test('cancellation evidence lookup failures are sanitized and protected by HMAC and replay checks', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-cancel-auth-'));
  const service = createService({ enabled: true, secret, sessionRoot: root, ledgerRepository: {
    getCampaignBatchCancellationEvidence: () => { throw new Error('private ledger failure'); },
  }, loadBaileys: false, allowUnsupportedFilesystem: true });
  try {
    const valid = signedPathRequest('GET', '/v1/users/42/campaign-batches/cancel-evidence-005/cancellation-status', 42);
    const invalid = { ...valid, headers: { ...valid.headers, 'x-taskpe-signature': '0'.repeat(64) } };
    assert.equal((await invoke(service, invalid)).status, 401);
    const mismatch = signedPathRequest('GET', '/v1/users/42/campaign-batches/cancel-evidence-005/cancellation-status', 43);
    assert.equal((await invoke(service, mismatch)).status, 401);
    const repeated = signedPathRequest('GET', '/v1/users/42/campaign-batches/cancel-evidence-005/cancellation-status', 42, '', 'cancel-evidence-replay-request-1234567890');
    assert.equal((await invoke(service, repeated)).status, 503);
    assert.equal((await invoke(service, repeated)).status, 401);
    assert.deepEqual((await invoke(service, signedPathRequest('GET', '/v1/users/42/campaign-batches/cancel-evidence-005/cancellation-status', 42))).body, {
      success: false, status: 'unavailable', message: 'Batch cancellation evidence is unavailable.',
    });
  } finally { await service.closeAll(); await fs.rm(root, { recursive: true, force: true }); }
});

test('authenticated quota read returns existing usage without mutation', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-quota-api-'));
  const db = initializeLedger(path.join(root, 'ledger.sqlite3'));
  const repo = createLedgerRepository(db);
  reserveAndEnqueueOutgoingMessageForTest(db, quotaInput({ messageId: 'quota-api-message', idempotencyKey: 'quota-api-key' }), '2026-01-01T00:00:00.000Z');
  const service = createService({ enabled: true, secret, sessionRoot: root, ledgerRepository: repo, loadBaileys: false, allowUnsupportedFilesystem: true });
  try {
    const pathName = '/v1/users/42/quota';
    const result = await invoke(service, signedPathRequest('GET', pathName, 42));
    assert.equal(result.status, 200);
    assert.deepEqual(result.body, {
      success: true,
      worker_id: 42,
      quota_day: calculateQuotaDay(new Date(), 'Asia/Kolkata'),
      limit: 80,
      reserved: 0,
      remaining: 80,
      timezone: 'Asia/Kolkata',
    });
  } finally { await service.closeAll(); closeLedger(db); await fs.rm(root, { recursive: true, force: true }); }
});

test('quota read reports an existing current-day row and never inserts or increments it', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-quota-row-'));
  const db = initializeLedger(path.join(root, 'ledger.sqlite3'));
  const repo = createLedgerRepository(db);
  const quotaDay = calculateQuotaDay(new Date(), 'Asia/Kolkata');
  db.prepare(`INSERT INTO daily_message_quota
    (sender_worker_id, quota_day, limit_count, reserved_count, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?)`)
    .run(42, quotaDay, 80, 17, new Date().toISOString(), new Date().toISOString());
  const service = createService({ enabled: true, secret, sessionRoot: root, ledgerRepository: repo, loadBaileys: false, allowUnsupportedFilesystem: true });
  try {
    const before = db.prepare('SELECT reserved_count FROM daily_message_quota WHERE sender_worker_id = 42 AND quota_day = ?').get(quotaDay);
    const result = await invoke(service, signedPathRequest('GET', '/v1/users/42/quota', 42));
    const after = db.prepare('SELECT reserved_count FROM daily_message_quota WHERE sender_worker_id = 42 AND quota_day = ?').get(quotaDay);
    assert.equal(result.status, 200);
    assert.equal(result.body.reserved, 17);
    assert.equal(result.body.remaining, 63);
    assert.deepEqual(after, before);
    assert.equal(db.prepare('SELECT COUNT(*) AS count FROM daily_message_quota WHERE sender_worker_id = 42 AND quota_day = ?').get(quotaDay).count, 1);
  } finally { await service.closeAll(); closeLedger(db); await fs.rm(root, { recursive: true, force: true }); }
});

test('quota read rejects invalid authentication, worker mismatch, and replay', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-quota-auth-'));
  const db = initializeLedger(path.join(root, 'ledger.sqlite3'));
  const repo = createLedgerRepository(db);
  const service = createService({ enabled: true, secret, sessionRoot: root, ledgerRepository: repo, loadBaileys: false, allowUnsupportedFilesystem: true });
  try {
    const valid = signedPathRequest('GET', '/v1/users/42/quota', 42);
    const invalid = { ...valid, headers: { ...valid.headers, 'x-taskpe-signature': '0'.repeat(64) } };
    assert.equal((await invoke(service, invalid)).status, 401);
    const mismatch = signedPathRequest('GET', '/v1/users/42/quota', 43);
    assert.equal((await invoke(service, mismatch)).status, 401);
    const repeatedId = 'quota-api-repeated-request-id-1234567890';
    const repeated = signedPathRequest('GET', '/v1/users/42/quota', 42, '', repeatedId);
    assert.equal((await invoke(service, repeated)).status, 200);
    assert.equal((await invoke(service, repeated)).status, 401);
  } finally { await service.closeAll(); closeLedger(db); await fs.rm(root, { recursive: true, force: true }); }
});

test('quota read uses the Asia/Kolkata boundary without creating a row', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-quota-boundary-'));
  const db = initializeLedger(path.join(root, 'ledger.sqlite3'));
  try {
    const before = getDailyQuotaStatusForTest(db, 42, '2026-01-01T18:29:59.999Z');
    const after = getDailyQuotaStatusForTest(db, 42, '2026-01-01T18:30:00.000Z');
    assert.equal(before.quota_day, '2026-01-01');
    assert.equal(after.quota_day, '2026-01-02');
    assert.equal(db.prepare('SELECT COUNT(*) AS count FROM daily_message_quota').get().count, 0);
  } finally { closeLedger(db); await fs.rm(root, { recursive: true, force: true }); }
});

test('quota read failures are sanitized and do not invoke sockets', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-quota-failure-'));
  const calls = [];
  const service = createService({ enabled: true, secret, sessionRoot: root, ledgerRepository: {
    getDailyQuotaStatus: () => { calls.push('quota'); throw new Error('private ledger path'); },
  }, loadBaileys: false, allowUnsupportedFilesystem: true });
  try {
    const result = await invoke(service, signedPathRequest('GET', '/v1/users/42/quota', 42));
    assert.equal(result.status, 503);
    assert.deepEqual(result.body, { success: false, message: 'Quota information is unavailable.' });
    assert.deepEqual(calls, ['quota']);
  } finally { await service.closeAll(); await fs.rm(root, { recursive: true, force: true }); }
});

test('connection paths reject traversal and malformed user IDs', () => {
  assert.deepEqual(parseConnectionPath('/v1/users/42/qr'), { userId: '42', action: 'qr' });
  assert.equal(parseConnectionPath('/v1/users/../42/status'), null);
  assert.equal(parseConnectionPath('/v1/users/42/status/extra'), null);
  assert.equal(parseConnectionPath('/v1/users/0/status'), null);
  assert.rejects(removeUserSession('C:\\private\\taskpe-wa', '../outside'));
});

test('session roots and worker directories reject symlinks where supported', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-links-'));
  const target = path.join(root, 'target'); const rootLink = path.join(root, 'root-link'); const userLink = path.join(root, 'user-42');
  await fs.mkdir(target);
  try {
    await fs.symlink(target, rootLink, 'junction');
    await fs.symlink(target, userLink, 'junction');
  } catch (error) {
    await fs.rm(root, { recursive: true, force: true });
    if (['EPERM', 'EACCES', 'UNKNOWN'].includes(error.code)) return t.skip('symlink creation is unavailable on this operating system');
    throw error;
  }
  await assert.rejects(assertPrivateDirectory(rootLink));
  await assert.rejects(removeUserSession(root, '42'));
  await fs.rm(root, { recursive: true, force: true });
});

test('POSIX worker-directory permission failures are rejected', async (t) => {
  if (process.platform === 'win32') return t.skip('POSIX permission bits are not enforced on Windows');
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-permissions-'));
  const worker = path.join(root, 'user-42');
  await fs.mkdir(worker, { mode: 0o755 });
  try {
    await assert.rejects(assertWorkerSessionDirectory(root, worker), /permissions are too broad/);
  } finally {
    await fs.chmod(worker, 0o700);
    await fs.rm(root, { recursive: true, force: true });
  }
});

test('disconnect cleanup removes only the validated user session directory', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-'));
  const userDir = path.join(root, 'user-42');
  await fs.mkdir(userDir); await fs.writeFile(path.join(userDir, 'creds.json'), 'private');
  await removeUserSession(root, '42');
  await assert.rejects(fs.access(userDir));
  await fs.rm(root, { recursive: true, force: true });
});

function signedRequest(method, action, userId, body = '', requestId = crypto.randomBytes(24).toString('base64url')) {
  const pathname = `/v1/users/${userId}/${action}`;
  const timestamp = String(Math.floor(Date.now() / 1000));
  const signature = crypto.createHmac('sha256', secret).update(`${timestamp}.${requestId}.${method}.${pathname}.${body}`).digest('hex');
  return { method, url: pathname, headers: { 'x-taskpe-timestamp': timestamp, 'x-taskpe-request-id': requestId, 'x-taskpe-signature': signature, 'x-taskpe-user-id': String(userId) }, body };
}

function signedPathRequest(method, pathname, userId, body = '', requestId = crypto.randomBytes(24).toString('base64url')) {
  const timestamp = String(Math.floor(Date.now() / 1000));
  const signature = crypto.createHmac('sha256', secret).update(`${timestamp}.${requestId}.${method}.${pathname}.${body}`).digest('hex');
  return { method, url: pathname, headers: { 'x-taskpe-timestamp': timestamp, 'x-taskpe-request-id': requestId, 'x-taskpe-signature': signature, 'x-taskpe-user-id': String(userId) }, body };
}

async function invoke(service, request) {
  const req = Readable.from(request.body ? [Buffer.from(request.body)] : []);
  Object.assign(req, { method: request.method, url: request.url, headers: request.headers });
  let result;
  const res = { writeHead(status) { this.status = status; }, end(body) { result = { status: this.status, body: JSON.parse(body) }; } };
  await service.handle(req, res);
  return result;
}

async function fakeBaileys(counter, { saveCreds = async () => {} } = {}) {
  return {
    useMultiFileAuthState: async () => ({ state: {}, saveCreds }),
    default: () => { counter.count += 1; return { ev: new EventEmitter(), ws: { close() {} }, logout: async () => {} }; },
  };
}

test('concurrent QR requests create one connection per worker', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-concurrent-')); const counter = { count: 0 };
  const service = createService({ enabled: true, secret, sessionRoot: root, baileysLoader: () => fakeBaileys(counter), pinoLoader: () => ({}), allowUnsupportedFilesystem: true });
  const results = await Promise.all([invoke(service, signedRequest('POST', 'qr', 42)), invoke(service, signedRequest('POST', 'qr', 42))]);
  assert.equal(counter.count, 1); assert.equal(results[0].status, 200); assert.equal(results[1].status, 200);
  await service.closeAll(); await fs.rm(root, { recursive: true, force: true });
});

test('the actual handler rejects a repeated signed request ID', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-replay-handler-')); const counter = { count: 0 };
  const service = createService({ enabled: true, secret, sessionRoot: root, baileysLoader: () => fakeBaileys(counter), pinoLoader: () => ({}), allowUnsupportedFilesystem: true });
  const request = signedRequest('POST', 'qr', 42);
  const first = await invoke(service, request); const second = await invoke(service, request);
  assert.equal(first.status, 200); assert.equal(second.status, 401); assert.equal(counter.count, 1);
  await service.closeAll(); await fs.rm(root, { recursive: true, force: true });
});

test('connect and disconnect are serialized and old close cannot reconnect', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-lifecycle-')); const counter = { count: 0 };
  const service = createService({ enabled: true, secret, sessionRoot: root, baileysLoader: () => fakeBaileys(counter), pinoLoader: () => ({}), allowUnsupportedFilesystem: true });
  const connecting = invoke(service, signedRequest('POST', 'qr', 42));
  const disconnecting = invoke(service, signedRequest('POST', 'disconnect', 42));
  await Promise.all([connecting, disconnecting]);
  assert.equal(service.connections.size, 0); assert.equal(counter.count, 1);
  await service.closeAll(); await fs.rm(root, { recursive: true, force: true });
});

test('manual disconnect followed by an old close event does not reconnect', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-manual-disconnect-')); const counter = { count: 0 };
  const service = createService({ enabled: true, secret, sessionRoot: root, baileysLoader: () => fakeBaileys(counter), pinoLoader: () => ({}), allowUnsupportedFilesystem: true });
  await invoke(service, signedRequest('POST', 'qr', 42));
  const state = service.connections.get('42'); const oldSocket = state.socket;
  await invoke(service, signedRequest('POST', 'disconnect', 42));
  oldSocket.ev.emit('connection.update', { connection: 'close', lastDisconnect: { error: { output: { statusCode: 500 } } } });
  assert.equal(service.connections.size, 0); assert.equal(counter.count, 1);
  await service.closeAll(); await fs.rm(root, { recursive: true, force: true });
});

test('credential persistence failure disables only the affected connection', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-creds-')); const counter = { count: 0 }; const logs = [];
  const service = createService({
    enabled: true, secret, sessionRoot: root,
    baileysLoader: () => fakeBaileys(counter, { saveCreds: async () => { throw new Error('private credential details'); } }),
    pinoLoader: () => ({ error: record => logs.push(record), }), allowUnsupportedFilesystem: true,
  });
  await invoke(service, signedRequest('POST', 'qr', 42));
  const state = service.connections.get('42');
  state.socket.ev.emit('creds.update', { privateKey: 'must-not-be-logged' });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(state.status, 'error'); assert.equal(state.cancelled, true); assert.equal(state.persistenceFailed, true);
  assert.equal(counter.count, 1); assert.equal(logs.length, 1); assert.equal(logs[0].error, 'Error');
  await service.closeAll(); await fs.rm(root, { recursive: true, force: true });
});

test('shutdown cancels active connection lifecycle', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-shutdown-')); const counter = { count: 0 };
  const service = createService({ enabled: true, secret, sessionRoot: root, baileysLoader: () => fakeBaileys(counter), pinoLoader: () => ({}), allowUnsupportedFilesystem: true });
  await invoke(service, signedRequest('POST', 'qr', 42));
  const state = service.connections.get('42');
  await service.closeAll();
  state.socket.ev.emit('connection.update', { connection: 'close', lastDisconnect: { error: { output: { statusCode: 500 } } } });
  assert.equal(service.connections.size, 0); assert.equal(counter.count, 1);
  await fs.rm(root, { recursive: true, force: true });
});

function generateTestDispatchToken({
  tokenSecret = secret,
  authId = 'auth-' + crypto.randomUUID(),
  workerId = 42,
  planId = 12,
  batchId = 'batch-001',
  operationId = 'op-' + crypto.randomUUID(),
  requestFingerprint = '0'.repeat(64),
  issuedAt = Math.floor(Date.now() / 1000),
  expiresAt = Math.floor(Date.now() / 1000) + 60,
  version = 1,
  overrideSignature = null,
} = {}) {
  const payload = {
    v: version,
    auth_id: authId,
    worker_id: workerId,
    plan_id: planId,
    batch_id: batchId,
    operation_id: operationId,
    request_fingerprint: requestFingerprint,
    issued_at: issuedAt,
    expires_at: expiresAt,
  };
  const b64 = Buffer.from(JSON.stringify(payload)).toString('base64url');
  const canonical = `v${version}.${b64}`;
  const derivedKey = deriveDispatchAuthorizationKey(tokenSecret);
  const signature = overrideSignature || crypto.createHmac('sha256', derivedKey).update(Buffer.from(canonical, 'utf8')).digest('hex');
  return `v${version}.${b64}.${signature}`;
}

test('dispatch authorization token verification succeeds for valid token', () => {
  const token = generateTestDispatchToken();
  const result = verifyDispatchAuthorizationToken(token, secret);
  assert.equal(result.valid, true);
  assert.equal(result.payload.v, 1);
  assert.equal(result.payload.worker_id, 42);
  assert.equal(result.payload.plan_id, 12);
  assert.equal(result.payload.batch_id, 'batch-001');
});

test('dispatch authorization token verification rejects invalid signature and secret', () => {
  const token = generateTestDispatchToken({ tokenSecret: 'wrong-secret-that-is-at-least-32-chars-long' });
  const result = verifyDispatchAuthorizationToken(token, secret);
  assert.equal(result.valid, false);
  assert.equal(result.statusCode, 401);
  assert.match(result.message, /signature/i);

  const tamperedToken = generateTestDispatchToken({ overrideSignature: '0'.repeat(64) });
  const tamperedResult = verifyDispatchAuthorizationToken(tamperedToken, secret);
  assert.equal(tamperedResult.valid, false);
  assert.equal(tamperedResult.statusCode, 401);
});

test('dispatch authorization token verification rejects expired and future tokens', () => {
  const nowSec = Math.floor(Date.now() / 1000);
  const expiredToken = generateTestDispatchToken({ issuedAt: nowSec - 120, expiresAt: nowSec - 30 });
  const expiredResult = verifyDispatchAuthorizationToken(expiredToken, secret);
  assert.equal(expiredResult.valid, false);
  assert.equal(expiredResult.statusCode, 401);
  assert.match(expiredResult.message, /expired/i);

  const futureToken = generateTestDispatchToken({ issuedAt: nowSec + 60, expiresAt: nowSec + 120 });
  const futureResult = verifyDispatchAuthorizationToken(futureToken, secret);
  assert.equal(futureResult.valid, false);
  assert.equal(futureResult.statusCode, 401);
  assert.match(futureResult.message, /not yet valid/i);
});

test('dispatch authorization token verification rejects malformed tokens and claims', () => {
  assert.equal(verifyDispatchAuthorizationToken('invalid', secret).valid, false);
  assert.equal(verifyDispatchAuthorizationToken('v2.bad.signature', secret).valid, false);
  assert.equal(verifyDispatchAuthorizationToken('v1.notbase64!.' + '0'.repeat(64), secret).valid, false);

  const badJsonB64 = Buffer.from('not json').toString('base64url');
  const derivedKey = deriveDispatchAuthorizationKey(secret);
  const badSig = crypto.createHmac('sha256', derivedKey).update(Buffer.from(`v1.${badJsonB64}`, 'utf8')).digest('hex');
  const malformedResult = verifyDispatchAuthorizationToken(`v1.${badJsonB64}.${badSig}`, secret);
  assert.equal(malformedResult.valid, false);
  assert.equal(malformedResult.statusCode, 400);

  const invalidClaimsToken = generateTestDispatchToken({ workerId: -5 });
  const invalidClaimsResult = verifyDispatchAuthorizationToken(invalidClaimsToken, secret);
  assert.equal(invalidClaimsResult.valid, false);
  assert.equal(invalidClaimsResult.statusCode, 400);
});

test('dispatch authorization rejects missing or overlong lifetimes in the ledger primitive', async () => {
  await withLedgerTest(async (ledger) => {
    const input = campaignBatchInput({ batchId: 'batch-auth-lifetime', senderWorkerId: 42, planId: 10 });
    const reserved = ledger.reserveCampaignBatch(input);
    const issuedAt = new Date(Date.now() - 1000).toISOString();
    assert.throws(() => ledger.consumeDispatchAuthorization({
      authorizationId: 'auth-lifetime-missing', batchId: input.batchId,
      operationId: 'op-lifetime-missing', senderWorkerId: 42, planId: 10,
      requestFingerprint: reserved.batch.request_fingerprint,
      expiresAt: new Date(Date.now() + 60000).toISOString(),
    }), /authorization issue time/i);
    assert.throws(() => ledger.consumeDispatchAuthorization({
      authorizationId: 'auth-lifetime-long', batchId: input.batchId,
      operationId: 'op-lifetime-long', senderWorkerId: 42, planId: 10,
      requestFingerprint: reserved.batch.request_fingerprint,
      issuedAt, expiresAt: new Date(Date.now() + 121000).toISOString(),
    }), error => error.code === 'AUTHORIZATION_LIFETIME_INVALID');
  });
});

test('ledger repository consumes valid dispatch authorization for reserved batch', async () => {
  await withLedgerTest(async (ledger, db) => {
    const input = campaignBatchInput({ batchId: 'batch-auth-001', senderWorkerId: 42, planId: 10 });
    const reserved = ledger.reserveCampaignBatch(input);
    assert.equal(reserved.created, true);

    const authId = 'auth-test-001';
    const opId = 'op-test-operation-001';
    const consumed = ledger.consumeDispatchAuthorization({
      authorizationId: authId,
      batchId: 'batch-auth-001',
      operationId: opId,
      senderWorkerId: 42,
      planId: 10,
      requestFingerprint: reserved.batch.request_fingerprint,
      issuedAt: new Date(Date.now() - 1000).toISOString(),
      expiresAt: new Date(Date.now() + 60000).toISOString(),
    });
    assert.equal(consumed.consumed, true);
    assert.equal(consumed.authorizationId, authId);
    assert.equal(consumed.batchId, 'batch-auth-001');

    const persisted = ledger.getDispatchAuthorization('batch-auth-001', 42);
    assert.ok(persisted);
    assert.equal(persisted.authorization_id, authId);
    assert.equal(persisted.batch_id, 'batch-auth-001');
    assert.equal(persisted.operation_id, opId);
    assert.equal(persisted.status, 'consumed');
  });
});

test('ledger repository rejects duplicate authorization and duplicate batch dispatch', async () => {
  await withLedgerTest(async (ledger, db) => {
    const input = campaignBatchInput({ batchId: 'batch-auth-002', senderWorkerId: 42, planId: 10 });
    const reserved = ledger.reserveCampaignBatch(input);
    assert.equal(reserved.created, true);

    const first = ledger.consumeDispatchAuthorization({
      authorizationId: 'auth-test-first',
      batchId: 'batch-auth-002',
      operationId: 'op-test-operation-first',
      senderWorkerId: 42,
      planId: 10,
      requestFingerprint: reserved.batch.request_fingerprint,
      issuedAt: new Date(Date.now() - 1000).toISOString(),
      expiresAt: new Date(Date.now() + 60000).toISOString(),
    });
    assert.equal(first.consumed, true);

    // Replay same authorization ID
    assert.throws(() => {
      ledger.consumeDispatchAuthorization({
        authorizationId: 'auth-test-first',
        batchId: 'batch-auth-002',
        operationId: 'op-test-operation-first',
        senderWorkerId: 42,
        planId: 10,
        requestFingerprint: reserved.batch.request_fingerprint,
        issuedAt: new Date(Date.now() - 1000).toISOString(),
        expiresAt: new Date(Date.now() + 60000).toISOString(),
      });
    }, error => error.code === 'AUTHORIZATION_ALREADY_CONSUMED');

    // New authorization on already-authorized batch
    assert.throws(() => {
      ledger.consumeDispatchAuthorization({
        authorizationId: 'auth-test-second',
        batchId: 'batch-auth-002',
        operationId: 'op-test-operation-second',
        senderWorkerId: 42,
        planId: 10,
        requestFingerprint: reserved.batch.request_fingerprint,
        issuedAt: new Date(Date.now() - 1000).toISOString(),
        expiresAt: new Date(Date.now() + 60000).toISOString(),
      });
    }, error => error.code === 'BATCH_ALREADY_AUTHORIZED');
  });
});

test('ledger repository rejects authorization with mismatched identity, plan, or fingerprint', async () => {
  await withLedgerTest(async (ledger, db) => {
    const input = campaignBatchInput({ batchId: 'batch-auth-003', senderWorkerId: 42, planId: 10 });
    const reserved = ledger.reserveCampaignBatch(input);
    assert.equal(reserved.created, true);

    // Nonexistent batch
    assert.throws(() => {
      ledger.consumeDispatchAuthorization({
        authorizationId: 'auth-mismatch-1',
        batchId: 'batch-nonexistent',
        operationId: 'op-test-operation-mismatch-1',
        senderWorkerId: 42,
        planId: 10,
        requestFingerprint: reserved.batch.request_fingerprint,
        issuedAt: new Date(Date.now() - 1000).toISOString(),
        expiresAt: new Date(Date.now() + 60000).toISOString(),
      });
    }, error => error.code === 'BATCH_NOT_FOUND');

    // Worker mismatch
    assert.throws(() => {
      ledger.consumeDispatchAuthorization({
        authorizationId: 'auth-mismatch-2',
        batchId: 'batch-auth-003',
        operationId: 'op-test-operation-mismatch-2',
        senderWorkerId: 99,
        planId: 10,
        requestFingerprint: reserved.batch.request_fingerprint,
        issuedAt: new Date(Date.now() - 1000).toISOString(),
        expiresAt: new Date(Date.now() + 60000).toISOString(),
      });
    }, error => error.code === 'WORKER_MISMATCH');

    // Plan mismatch
    assert.throws(() => {
      ledger.consumeDispatchAuthorization({
        authorizationId: 'auth-mismatch-3',
        batchId: 'batch-auth-003',
        operationId: 'op-test-operation-mismatch-3',
        senderWorkerId: 42,
        planId: 999,
        requestFingerprint: reserved.batch.request_fingerprint,
        issuedAt: new Date(Date.now() - 1000).toISOString(),
        expiresAt: new Date(Date.now() + 60000).toISOString(),
      });
    }, error => error.code === 'PLAN_MISMATCH');

    // Fingerprint mismatch
    assert.throws(() => {
      ledger.consumeDispatchAuthorization({
        authorizationId: 'auth-mismatch-4',
        batchId: 'batch-auth-003',
        operationId: 'op-test-operation-mismatch-4',
        senderWorkerId: 42,
        planId: 10,
        requestFingerprint: 'f'.repeat(64),
        issuedAt: new Date(Date.now() - 1000).toISOString(),
        expiresAt: new Date(Date.now() + 60000).toISOString(),
      });
    }, error => error.code === 'FINGERPRINT_MISMATCH');

    // Items no longer queued
    db.prepare("UPDATE outgoing_messages SET state = 'attempted' WHERE rowid = (SELECT rowid FROM outgoing_messages WHERE batch_id = 'batch-auth-003' LIMIT 1)").run();
    assert.throws(() => {
      ledger.consumeDispatchAuthorization({
        authorizationId: 'auth-mismatch-5',
        batchId: 'batch-auth-003',
        operationId: 'op-test-operation-mismatch-5',
        senderWorkerId: 42,
        planId: 10,
        requestFingerprint: reserved.batch.request_fingerprint,
        issuedAt: new Date(Date.now() - 1000).toISOString(),
        expiresAt: new Date(Date.now() + 60000).toISOString(),
      });
    }, error => error.code === 'BATCH_ITEMS_NOT_QUEUED');
  });
});

test('HTTP batch_send requires and validates dispatch authorization header', async () => {
  await withLedgerTest(async (ledger, db) => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-http-auth-'));
    const service = createService({
      enabled: true,
      secret,
      sessionRoot: root,
      ledgerRepository: ledger,
      loadBaileys: false,
      allowUnsupportedFilesystem: true,
    });
    try {
      const input = campaignBatchInput({ batchId: 'batch-http-001', senderWorkerId: 42, planId: 15 });
      const reserved = ledger.reserveCampaignBatch(input);
      assert.equal(reserved.created, true);

      // 1. Missing header under hard-disabled send flag returns 503 sending_disabled
      const reqMissing = signedPathRequest('POST', '/v1/users/42/campaign-batches/batch-http-001/send', 42);
      const resMissing = await invoke(service, reqMissing);
      assert.equal(resMissing.status, 503);
      assert.equal(resMissing.body.status, 'sending_disabled');

      // 2. Invalid signature -> 401
      const reqInvalid = signedPathRequest('POST', '/v1/users/42/campaign-batches/batch-http-001/send', 42);
      reqInvalid.headers['x-taskpe-dispatch-authorization'] = generateTestDispatchToken({
        batchId: 'batch-http-001',
        overrideSignature: '0'.repeat(64),
      });
      const resInvalid = await invoke(service, reqInvalid);
      assert.equal(resInvalid.status, 401);
      assert.equal(resInvalid.body.status, 'invalid_authorization');

      // 3. Worker mismatch -> 403
      const reqWorkerMismatch = signedPathRequest('POST', '/v1/users/42/campaign-batches/batch-http-001/send', 42);
      reqWorkerMismatch.headers['x-taskpe-dispatch-authorization'] = generateTestDispatchToken({
        workerId: 99,
        batchId: 'batch-http-001',
      });
      const resWorkerMismatch = await invoke(service, reqWorkerMismatch);
      assert.equal(resWorkerMismatch.status, 403);
      assert.equal(resWorkerMismatch.body.status, 'identity_mismatch');

      // 4. Batch ID mismatch -> 403
      const reqBatchMismatch = signedPathRequest('POST', '/v1/users/42/campaign-batches/batch-http-001/send', 42);
      reqBatchMismatch.headers['x-taskpe-dispatch-authorization'] = generateTestDispatchToken({
        workerId: 42,
        batchId: 'other-batch',
      });
      const resBatchMismatch = await invoke(service, reqBatchMismatch);
      assert.equal(resBatchMismatch.status, 403);
      assert.equal(resBatchMismatch.body.status, 'identity_mismatch');

      // 5. Valid authorization -> validates successfully, but returns 503 sending_disabled
      // BEFORE consuming authorization or mutating ledger state.
      const reqValid = signedPathRequest('POST', '/v1/users/42/campaign-batches/batch-http-001/send', 42);
      reqValid.headers['x-taskpe-dispatch-authorization'] = generateTestDispatchToken({
        authId: 'auth-http-valid',
        workerId: 42,
        planId: 15,
        batchId: 'batch-http-001',
        operationId: 'op-http-valid-operation-001',
        requestFingerprint: reserved.batch.request_fingerprint,
      });
      const resValid = await invoke(service, reqValid);
      assert.equal(resValid.status, 503);
      assert.equal(resValid.body.status, 'sending_disabled');

      // Verify authorization was NOT consumed in ledger while sending is disabled
      const consumedAuth = ledger.getDispatchAuthorization('batch-http-001', 42);
      assert.equal(consumedAuth, null);

      // Verify outgoing messages remain intact in queued state
      const batchCheck = ledger.getCampaignBatch('batch-http-001', 42);
      assert.ok(batchCheck);
      assert.equal(batchCheck.batch.state, 'reserved');
      for (const item of batchCheck.items) {
        const msg = ledger.getOutgoingMessage(item.message_id);
        assert.equal(msg.state, 'queued');
      }

      // 6. Repeated request remains non-sending and still does not consume authorization
      const reqRepeated = signedPathRequest('POST', '/v1/users/42/campaign-batches/batch-http-001/send', 42);
      reqRepeated.headers['x-taskpe-dispatch-authorization'] = reqValid.headers['x-taskpe-dispatch-authorization'];
      const resRepeated = await invoke(service, reqRepeated);
      assert.equal(resRepeated.status, 503);
      assert.equal(resRepeated.body.status, 'sending_disabled');
      assert.equal(ledger.getDispatchAuthorization('batch-http-001', 42), null);

      // 7. Lookup-only recovery endpoint returns batch evidence without dispatching
      const reqLookup = signedPathRequest('GET', '/v1/users/42/campaign-batches/batch-http-001', 42);
      const resLookup = await invoke(service, reqLookup);
      assert.equal(resLookup.status, 200);
      assert.equal(resLookup.body.found, true);
      assert.equal(resLookup.body.batch.batch_id, 'batch-http-001');
    } finally {
      await service.closeAll();
      await fs.rm(root, { recursive: true, force: true });
    }
  });
});

test('Node opt-out coordination is durable, idempotent, and rejects conflicting logical retries', async () => {
  await withLedgerTest(async (ledger) => {
    const first = ledger.registerRecipientOptOut({
      requestId: 'optout-logical-001', recipientE164: '+14155550123',
      actorPrincipalId: 42, scope: 'platform', reason: 'user_opt_out',
    }, '2026-01-01T00:00:00.000Z');
    assert.equal(first.status, 'applied');
    const duplicate = ledger.registerRecipientOptOut({
      requestId: 'optout-logical-001', recipientE164: '+14155550123',
      actorPrincipalId: 42, scope: 'platform', reason: 'user_opt_out',
    }, '2026-01-01T00:00:01.000Z');
    assert.equal(duplicate.status, 'duplicate');
    assert.throws(() => ledger.registerRecipientOptOut({
      requestId: 'optout-logical-001', recipientE164: '+442071838750',
      actorPrincipalId: 42, scope: 'platform', reason: 'user_opt_out',
    }), error => error.code === 'OPTOUT_REQUEST_CONFLICT');
  });
});

test('authenticated opt-out route remains available while sending is disabled and never loads a socket', async () => {
  await withLedgerTest(async (ledger) => {
    const service = createService({ enabled: false, secret, ledgerRepository: ledger, loadBaileys: false });
    const payload = JSON.stringify({ requestId: 'optout-http-001', recipientE164: '+442071838750', actorPrincipalId: 42, scope: 'platform', reason: 'user_opt_out' });
    try {
      const response = await invoke(service, signedPathRequest('POST', '/v1/users/42/coordination/opt-outs', 42, payload));
      assert.equal(response.status, 200);
      assert.equal(response.body.status, 'applied');
      const replay = await invoke(service, signedPathRequest('POST', '/v1/users/42/coordination/opt-outs', 42, payload, 'optout-http-replay-00000000000000000000000000000000'));
      assert.equal(replay.status, 200);
      assert.equal(replay.body.status, 'duplicate');
      const mismatch = await invoke(service, signedPathRequest('POST', '/v1/users/42/coordination/opt-outs', 42, JSON.stringify({ ...JSON.parse(payload), actorPrincipalId: 99 }), 'optout-http-mismatch-00000000000000000000000000000000'));
      assert.equal(mismatch.status, 403);
    } finally {
      await service.closeAll();
    }
  });
});

test('opt-out commits before dispatch fencing and prevents any socket call', async () => {
  await withLedgerTest(async (ledger) => {
    const input = campaignBatchInput({ batchId: 'batch-optout-first-001', planId: 31, items: [
      { planRecipientId: 301, recipientJid: '14155550123@s.whatsapp.net', contentType: 'text', payloadReference: 'content-optout' },
    ]});
    const reserved = ledger.reserveCampaignBatch(input);
    const issued = new Date(Date.now() - 1000).toISOString();
    const expires = new Date(Date.now() + 60000).toISOString();
    const auth = ledger.consumeDispatchAuthorization({
      authorizationId: 'auth-optout-first-001', batchId: input.batchId, operationId: 'op-optout-first-0001',
      senderWorkerId: 42, planId: input.planId, requestFingerprint: reserved.batch.request_fingerprint,
      issuedAt: issued, expiresAt: expires,
    }, new Date().toISOString());
    ledger.registerRecipientOptOut({ requestId: 'optout-logical-002', recipientE164: '+14155550123', actorPrincipalId: 42, scope: 'platform', reason: 'user_opt_out' }, '2026-01-01T00:00:02.000Z');
    let socketCalls = 0;
    const result = await sendReservedCampaignBatch({
      ledger, socket: { sendMessage: async () => { socketCalls += 1; } }, senderWorkerId: 42, batchId: input.batchId,
      dispatchOperationId: auth.operationId, requireCoordination: true,
      resolveContent: async () => ({ type: 'text', payloadReference: 'content-optout', text: 'safe test' }),
    });
    assert.equal(socketCalls, 0);
    assert.equal(result.items[0].status, 'opted_out');
  });
});

test('per-recipient fence commits before a later opt-out and blocks future submission', async () => {
  await withLedgerTest(async (ledger) => {
    const input = campaignBatchInput({ batchId: 'batch-fence-first-001', planId: 32, items: [
      { planRecipientId: 302, recipientJid: '442071838750@s.whatsapp.net', contentType: 'text', payloadReference: 'content-fence' },
    ]});
    const reserved = ledger.reserveCampaignBatch(input);
    const auth = ledger.consumeDispatchAuthorization({
      authorizationId: 'auth-fence-first-001', batchId: input.batchId, operationId: 'op-fence-first-0001',
      senderWorkerId: 42, planId: input.planId, requestFingerprint: reserved.batch.request_fingerprint,
      issuedAt: '2026-01-01T00:00:00.000Z', expiresAt: '2026-01-01T00:01:00.000Z',
    }, '2026-01-01T00:00:01.000Z');
    const messageId = deriveCampaignPlanRecipientIdempotencyKey(input.planId, input.items[0].planRecipientId);
    const fenced = ledger.beginRecipientSubmission({ operationId: auth.operationId, messageId, senderWorkerId: 42, attemptOperationId: 'attempt-fence-first-0001', attemptLeaseUntil: '2026-01-01T00:00:30.000Z' }, '2026-01-01T00:00:02.000Z');
    assert.equal(fenced.state, 'submitting');
    const optout = ledger.registerRecipientOptOut({ requestId: 'optout-logical-003', recipientE164: '+442071838750', actorPrincipalId: 42, scope: 'platform', reason: 'user_opt_out' }, '2026-01-01T00:00:03.000Z');
    assert.equal(optout.status, 'applied');
    assert.throws(() => ledger.beginRecipientSubmission({ operationId: auth.operationId, messageId, senderWorkerId: 42, attemptOperationId: 'attempt-fence-first-0002', attemptLeaseUntil: '2026-01-01T00:00:40.000Z' }, '2026-01-01T00:00:04.000Z'), error => error.code === 'SUBMISSION_NOT_AUTHORIZED');
  });
});

test('separate Node processes compete for one recipient fence without duplicate acquisition', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-fence-process-'));
  const dbPath = path.join(root, 'ledger.sqlite3');
  const childScript = `
    import { initializeLedger, createLedgerRepository, closeLedger } from './whatsapp_ledger.js';
    const [dbPath, operationId, messageId, attemptOperationId] = process.argv.slice(1);
    const db = initializeLedger(dbPath);
    try {
      const result = createLedgerRepository(db).beginRecipientSubmission({
        operationId, messageId, senderWorkerId: 42, attemptOperationId,
        attemptLeaseUntil: '2030-01-01T00:01:00.000Z'
      }, '2030-01-01T00:00:01.000Z');
      process.stdout.write(JSON.stringify({ ok: true, state: result.state }));
    } catch (error) {
      process.stdout.write(JSON.stringify({ ok: false, code: error?.code || 'ERROR' }));
    } finally { closeLedger(db); }
  `;
  try {
    const db = initializeLedger(dbPath);
    const input = campaignBatchInput({ batchId: 'batch-process-fence-001', planId: 36, items: [
      { planRecipientId: 306, recipientJid: '14155550124@s.whatsapp.net', contentType: 'text', payloadReference: 'process-fence' },
    ] });
    const reserved = reserveCampaignBatchForTest(db, input, '2030-01-01T00:00:00.000Z');
    consumeDispatchAuthorization(db, {
      authorizationId: 'auth-process-fence-001', batchId: input.batchId, operationId: 'op-process-fence-001',
      senderWorkerId: 42, planId: input.planId, requestFingerprint: reserved.batch.request_fingerprint,
      issuedAt: '2029-12-31T23:59:00.000Z', expiresAt: '2030-01-01T00:01:00.000Z',
    }, '2030-01-01T00:00:00.000Z');
    closeLedger(db);
    const results = await Promise.all([
      runLedgerChild(childScript, [dbPath, 'op-process-fence-001', deriveCampaignPlanRecipientIdempotencyKey(input.planId, 306), 'attempt-process-a-001']),
      runLedgerChild(childScript, [dbPath, 'op-process-fence-001', deriveCampaignPlanRecipientIdempotencyKey(input.planId, 306), 'attempt-process-b-001']),
    ]);
    assert.equal(results.filter(result => JSON.parse(result.stdout).ok).length, 1);
    assert.equal(results.filter(result => !JSON.parse(result.stdout).ok && JSON.parse(result.stdout).code === 'SUBMISSION_NOT_AUTHORIZED').length, 1);
    const reopened = initializeLedger(dbPath);
    assert.equal(reopened.prepare("SELECT COUNT(*) AS count FROM whatsapp_dispatch_recipient_leases WHERE state = 'submitting'").get().count, 1);
    closeLedger(reopened);
  } finally { await fs.rm(root, { recursive: true, force: true }); }
});

test('a coordinated two-recipient batch processes both eligible recipients and suppresses opted-out items', async () => {
  await withLedgerTest(async (ledger) => {
    const input = campaignBatchInput({ batchId: 'batch-mixed-coordination-001', planId: 33, items: [
      { planRecipientId: 303, recipientJid: '14155550123@s.whatsapp.net', contentType: 'text', payloadReference: 'a' },
      { planRecipientId: 304, recipientJid: '442071838750@s.whatsapp.net', contentType: 'text', payloadReference: 'b' },
    ]});
    ledger.registerRecipientOptOut({ requestId: 'optout-mixed-001', recipientE164: '+14155550123', actorPrincipalId: 42, scope: 'platform', reason: 'user_opt_out' });
    const reserved = ledger.reserveCampaignBatch(input);
    const now = new Date();
    const auth = ledger.consumeDispatchAuthorization({
      authorizationId: 'auth-mixed-001', batchId: input.batchId, operationId: 'op-mixed-operation-001', senderWorkerId: 42,
      planId: input.planId, requestFingerprint: reserved.batch.request_fingerprint,
      issuedAt: new Date(now.getTime() - 1000).toISOString(), expiresAt: new Date(now.getTime() + 60000).toISOString(),
    }, now.toISOString());
    let socketCalls = 0;
    const result = await sendReservedCampaignBatch({
      ledger, socket: { sendMessage: async () => { socketCalls += 1; return { key: { id: `transport-${socketCalls}` } }; } },
      senderWorkerId: 42, batchId: input.batchId, dispatchOperationId: auth.operationId,
      resolveContent: async ({ item }) => ({ type: 'text', payloadReference: item.payload_reference, text: 'safe test' }),
    });
    assert.equal(socketCalls, 1);
    assert.deepEqual(result.items.map(item => item.status), ['opted_out', 'acknowledged']);
  });
});

test('production-capable reserved batch helper requires coordination by default', async () => {
  await withLedgerTest(async (ledger) => {
    const input = campaignBatchInput({ batchId: 'batch-coordination-required-001', planId: 35, items: [
      { planRecipientId: 306, recipientJid: '14155550125@s.whatsapp.net', contentType: 'text', payloadReference: 'coord-required' },
    ]});
    ledger.reserveCampaignBatch(input);
    let socketCalls = 0;
    const result = await sendReservedCampaignBatch({
      ledger, socket: { sendMessage: async () => { socketCalls += 1; } }, senderWorkerId: 42, batchId: input.batchId,
      resolveContent: async () => ({ type: 'text', payloadReference: 'coord-required', text: 'safe test' }),
    });
    assert.equal(socketCalls, 0);
    assert.deepEqual(result.items.map(item => item.status), ['coordination_unavailable']);
  });
});

test('unfenced single-message helpers are not production exports', () => {
  assert.equal(Object.prototype.hasOwnProperty.call(serverModule, 'sendMessageWithSocket'), false);
  assert.equal(Object.prototype.hasOwnProperty.call(serverModule, 'orchestrateOutgoingMessage'), false);
});

test('legacy opt-out migration imports only canonical E.164 values and quarantines ambiguous values idempotently', async () => {
  await withLedgerTest(async (ledger, db) => {
    const first = migrateLegacyOptOuts(db, [
      { sourceKey: 'legacy-1', recipientE164: '+919876543210', rawValue: '9876543210' },
      { sourceKey: 'legacy-2', recipientE164: '9876543210', rawValue: '9876543210' },
    ], '2026-01-01T00:00:00.000Z');
    assert.equal(first.imported, 1);
    const second = migrateLegacyOptOuts(db, [
      { sourceKey: 'legacy-1', recipientE164: '+919876543210', rawValue: '9876543210' },
      { sourceKey: 'legacy-2', recipientE164: '9876543210', rawValue: '9876543210' },
    ], '2026-01-01T00:00:01.000Z');
    assert.equal(second.imported, 1);
    assert.equal(db.prepare('SELECT COUNT(*) AS count FROM whatsapp_opt_out_requests').get().count, 1);
    assert.equal(db.prepare('SELECT COUNT(*) AS count FROM whatsapp_legacy_opt_out_quarantine').get().count, 1);
  });
});

test('controlled Flask legacy reconciliation reads source data, reports counts, and reruns idempotently', async () => {
  await withLedgerTest(async (ledger, db) => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'taskpe-wa-legacy-flask-'));
    const flaskPath = path.join(root, 'flask.sqlite3');
    const source = new DatabaseSync(flaskPath);
    source.exec('CREATE TABLE whatsapp_opt_outs (id INTEGER PRIMARY KEY, phone_number TEXT NOT NULL, reason TEXT, opted_out_at TEXT)');
    source.prepare('INSERT INTO whatsapp_opt_outs (id, phone_number, reason, opted_out_at) VALUES (?, ?, ?, ?)').run(1, '+14155550123', 'legacy', '2026-01-01T00:00:00Z');
    source.prepare('INSERT INTO whatsapp_opt_outs (id, phone_number, reason, opted_out_at) VALUES (?, ?, ?, ?)').run(2, '9876543210', 'legacy', '2026-01-01T00:00:00Z');
    source.close();
    try {
      const first = reconcileLegacyFlaskOptOuts(db, flaskPath, { trustedNow: '2026-01-01T00:00:00.000Z' });
      assert.deepEqual(first, { source_count: 2, imported_count: 1, quarantined_count: 1, existing_count: 0, unresolved_count: 1, conflicting_count: 0, failed_count: 0 });
      const second = reconcileLegacyFlaskOptOuts(db, flaskPath, { trustedNow: '2026-01-01T00:00:01.000Z' });
      assert.deepEqual(second, { source_count: 2, imported_count: 0, quarantined_count: 0, existing_count: 2, unresolved_count: 1, conflicting_count: 0, failed_count: 0 });
      assert.equal(db.prepare('SELECT COUNT(*) AS count FROM whatsapp_legacy_opt_out_reconciliation').get().count, 2);
      const input = campaignBatchInput({ batchId: 'batch-legacy-review-001', planId: 34, items: [
        { planRecipientId: 305, recipientJid: '14155550124@s.whatsapp.net', contentType: 'text', payloadReference: 'legacy-review' },
      ]});
      const reserved = ledger.reserveCampaignBatch(input);
      assert.throws(() => ledger.consumeDispatchAuthorization({
        authorizationId: 'auth-legacy-review-001', batchId: input.batchId, operationId: 'op-legacy-review-0001', senderWorkerId: 42,
        planId: input.planId, requestFingerprint: reserved.batch.request_fingerprint,
        issuedAt: '2026-01-01T00:00:00.000Z', expiresAt: '2026-01-01T00:01:00.000Z',
      }, '2026-01-01T00:00:01.000Z'), error => error.code === 'LEGACY_OPTOUT_REVIEW_REQUIRED');
    } finally { await fs.rm(root, { recursive: true, force: true }); }
  });
});

test('legacy quarantine resolution is audited, idempotent, and retains suppression', async () => {
  await withLedgerTest(async (ledger, db) => {
    const quarantine = db.prepare(`INSERT INTO whatsapp_legacy_opt_out_quarantine
      (source_key, value_digest, reason, source_value, source_provenance, created_at)
      VALUES (?, ?, ?, ?, ?, ?)`).run('flask:77', 'digest-77', 'canonical verification required', '020 7946 0018', 'flask:whatsapp_opt_outs', '2026-01-01T00:00:00.000Z');
    const first = ledger.resolveLegacyOptOutQuarantine({ quarantineId: Number(quarantine.lastInsertRowid), resolutionId: 'resolution-77', reviewerPrincipalId: 9, decision: 'suppress', recipientE164: '+442079460018', reason: 'verified legacy suppression', approvedMapping: '+442079460018' }, '2026-01-01T00:01:00.000Z');
    assert.equal(first.status, 'resolved');
    const duplicate = ledger.resolveLegacyOptOutQuarantine({ quarantineId: Number(quarantine.lastInsertRowid), resolutionId: 'resolution-77', reviewerPrincipalId: 9, decision: 'suppress', recipientE164: '+442079460018', reason: 'verified legacy suppression', approvedMapping: '+442079460018' }, '2026-01-01T00:02:00.000Z');
    assert.equal(duplicate.status, 'duplicate');
    assert.equal(db.prepare("SELECT status, source_value, resolved_by, resolved_recipient_e164 FROM whatsapp_legacy_opt_out_quarantine WHERE quarantine_id = ?").get(quarantine.lastInsertRowid).status, 'resolved');
    assert.equal(db.prepare("SELECT state FROM whatsapp_recipient_coordination WHERE recipient_e164 = ?").get('+442079460018').state, 'opted_out');
    assert.equal(db.prepare("SELECT COUNT(*) AS count FROM whatsapp_legacy_opt_out_quarantine WHERE status = 'unresolved'").get().count, 0);
  });
});
