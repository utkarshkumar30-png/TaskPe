import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';

export const LEDGER_STATES = Object.freeze([
  'queued',
  'attempted',
  'acknowledged',
  'delivered_unknown',
  'failed',
]);

const CONTENT_TYPES = new Set(['text', 'image', 'video']);
const STATE_TRANSITIONS = new Map([
  ['queued', new Set(['attempted', 'failed'])],
  ['attempted', new Set(['acknowledged', 'delivered_unknown', 'failed'])],
  ['acknowledged', new Set(['acknowledged'])],
  ['delivered_unknown', new Set(['delivered_unknown'])],
  ['failed', new Set(['failed'])],
]);
const TOKEN_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/;
const JID_PATTERN = /^[1-9][0-9]{4,14}@s\.whatsapp\.net$/;
const FAILURE_CODE_PATTERN = /^[A-Z0-9][A-Z0-9_.:-]{0,63}$/;
const OPERATION_ID_PATTERN = /^[A-Za-z0-9_-]{16,200}$/;
const DEFAULT_QUOTA_LIMIT = 80;
const DEFAULT_QUOTA_TIMEZONE = 'Asia/Kolkata';
const DEFAULT_ATTEMPT_STALE_SECONDS = 900;
const MIN_ATTEMPT_STALE_SECONDS = 60;
const MAX_ATTEMPT_STALE_SECONDS = 86400;
const MAX_CAMPAIGN_BATCH_ITEMS = 80;
const MAX_DISPATCH_AUTHORIZATION_LIFETIME_SECONDS = 120;
const BATCH_STATE = 'reserved';
const BATCH_FINGERPRINT_PATTERN = /^[a-f0-9]{64}$/;

function defaultLedgerPath() {
  return process.env.TASKPE_WA_LEDGER_DB_PATH || path.join(os.tmpdir(), 'taskpe-wa-outgoing.sqlite3');
}

function isInside(parent, candidate) {
  const relative = path.relative(parent, candidate);
  return relative === '' || (relative !== '..' && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative));
}

function validateLedgerPath(dbPath) {
  if (typeof dbPath !== 'string' || !path.isAbsolute(dbPath)) throw new Error('Ledger path must be absolute.');
  const absolute = path.resolve(dbPath);
  const forbidden = [path.resolve(process.cwd(), 'static'), path.resolve(process.cwd(), 'public')];
  if (forbidden.some(directory => isInside(directory, absolute))) throw new Error('Ledger path must be outside public directories.');
  return absolute;
}

function ensureLedgerFileSafety(absolute) {
  if (!fs.existsSync(absolute)) return;
  const stat = fs.lstatSync(absolute);
  if (stat.isSymbolicLink() || !stat.isFile()) throw new Error('Ledger path must be a regular file.');
}

function validateToken(value, label) {
  if (typeof value !== 'string' || !TOKEN_PATTERN.test(value)) throw new Error(`Invalid ${label}.`);
  return value;
}

function validateOperationId(value) {
  if (typeof value !== 'string' || !OPERATION_ID_PATTERN.test(value)) throw new Error('Invalid attempt operation ID.');
  return value;
}

function validateLeaseTimestamp(value, label = 'attempt lease') {
  if (typeof value !== 'string' || Number.isNaN(new Date(value).getTime())) throw new Error(`Invalid ${label}.`);
  return new Date(value).toISOString();
}

function validateOptionalId(value, label) {
  if (value === null || value === undefined) return null;
  if (!Number.isSafeInteger(value) || value < 1) throw new Error(`Invalid ${label}.`);
  return value;
}

function validateBatchIdentity(value, label) {
  return validateToken(value, label);
}

function validateBatchFingerprint(value) {
  if (typeof value !== 'string' || !BATCH_FINGERPRINT_PATTERN.test(value)) {
    throw new Error('Invalid batch request fingerprint.');
  }
  return value;
}

function validateBatchInput(input) {
  const batchId = validateBatchIdentity(input?.batchId, 'batch ID');
  const requestFingerprint = validateBatchFingerprint(input?.requestFingerprint);
  const senderWorkerId = validateOptionalId(input?.senderWorkerId, 'sender worker ID');
  if (!senderWorkerId) throw new Error('Invalid sender worker ID.');
  const planId = validateOptionalId(input?.planId, 'campaign plan ID');
  if (!planId) throw new Error('Invalid campaign plan ID.');
  if (!Array.isArray(input?.items) || input.items.length < 1 || input.items.length > MAX_CAMPAIGN_BATCH_ITEMS) {
    throw new Error(`Batch must contain 1-${MAX_CAMPAIGN_BATCH_ITEMS} items.`);
  }
  const seenPlanRecipients = new Set();
  const seenKeys = new Set();
  const items = input.items.map((item, index) => {
    const planRecipientId = validateOptionalId(item?.planRecipientId, 'plan recipient ID');
    if (!planRecipientId || seenPlanRecipients.has(planRecipientId)) throw new Error('Duplicate plan recipient ID in batch.');
    seenPlanRecipients.add(planRecipientId);
    const idempotencyKey = deriveCampaignPlanRecipientIdempotencyKey(planId, planRecipientId);
    if (seenKeys.has(idempotencyKey)) throw new Error('Duplicate item identity in batch.');
    seenKeys.add(idempotencyKey);
    const recipientJid = item?.recipientJid;
    const contentType = item?.contentType;
    if (!JID_PATTERN.test(recipientJid) || !CONTENT_TYPES.has(contentType)) throw new Error('Invalid batch item.');
    const payloadReference = item?.payloadReference ?? null;
    if (payloadReference !== null && (typeof payloadReference !== 'string' || payloadReference.length > 500 || /[\u0000-\u001f\u007f]/.test(payloadReference))) {
      throw new Error('Invalid payload reference.');
    }
    return { position: index + 1, planRecipientId, idempotencyKey, messageId: idempotencyKey, recipientJid, contentType, payloadReference };
  });
  const campaignId = validateOptionalId(input?.campaignId, 'campaign ID');
  const taskId = validateOptionalId(input?.taskId, 'task ID');
  const calculatedFingerprint = calculateCampaignBatchRequestFingerprint({ batchId, senderWorkerId, campaignId, taskId, planId, items });
  if (calculatedFingerprint !== requestFingerprint) throw new Error('Batch request fingerprint does not match its contents.');
  return { batchId, requestFingerprint, senderWorkerId, campaignId, taskId, planId, items };
}

function validateMessageInput(input) {
  if (!input || !CONTENT_TYPES.has(input.contentType)) throw new Error('Invalid message content type.');
  if (!JID_PATTERN.test(input.recipientJid)) throw new Error('Invalid recipient.');
  if (input.payloadReference !== null && input.payloadReference !== undefined) {
    if (typeof input.payloadReference !== 'string' || input.payloadReference.length > 500 || /[\u0000-\u001f\u007f]/.test(input.payloadReference)) {
      throw new Error('Invalid payload reference.');
    }
  }
}

function rollback(db) {
  try { db.exec('ROLLBACK'); } catch {}
}

export function getQuotaConfig() {
  const rawLimit = process.env.TASKPE_WA_DAILY_QUOTA_LIMIT;
  const limit = rawLimit === undefined || rawLimit === '' ? DEFAULT_QUOTA_LIMIT : Number(rawLimit);
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > 1000000) throw new Error('Invalid daily quota configuration.');
  const timezone = process.env.TASKPE_WA_QUOTA_TIMEZONE || DEFAULT_QUOTA_TIMEZONE;
  if (typeof timezone !== 'string' || timezone.length > 100) throw new Error('Invalid quota timezone configuration.');
  try { new Intl.DateTimeFormat('en-CA', { timeZone: timezone }).format(new Date()); } catch { throw new Error('Invalid quota timezone configuration.'); }
  return Object.freeze({ limit, timezone });
}

export function getAttemptReconciliationConfig() {
  const raw = process.env.TASKPE_WA_ATTEMPT_STALE_SECONDS;
  const staleSeconds = raw === undefined || raw === '' ? DEFAULT_ATTEMPT_STALE_SECONDS : Number(raw);
  if (!Number.isSafeInteger(staleSeconds) || staleSeconds < MIN_ATTEMPT_STALE_SECONDS || staleSeconds > MAX_ATTEMPT_STALE_SECONDS) {
    throw new Error('Invalid attempted-message reconciliation configuration.');
  }
  return Object.freeze({ staleSeconds });
}

export function calculateQuotaDay(timestamp, timezone = DEFAULT_QUOTA_TIMEZONE) {
  const date = timestamp instanceof Date ? timestamp : new Date(timestamp);
  if (Number.isNaN(date.getTime())) throw new Error('Invalid quota timestamp.');
  try {
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit',
    }).formatToParts(date);
    const values = Object.fromEntries(parts.filter(part => part.type !== 'literal').map(part => [part.type, part.value]));
    if (!values.year || !values.month || !values.day) throw new Error('Invalid quota date.');
    return `${values.year}-${values.month}-${values.day}`;
  } catch { throw new Error('Invalid quota timezone configuration.'); }
}

function getDailyQuotaStatusAt(db, senderWorkerId, trustedNow) {
  const config = getQuotaConfig();
  const workerId = validateOptionalId(senderWorkerId, 'sender worker ID');
  if (!workerId) throw new Error('Invalid sender worker ID.');
  if (typeof trustedNow !== 'string' || Number.isNaN(new Date(trustedNow).getTime())) {
    throw new Error('Invalid quota timestamp.');
  }
  const now = new Date(trustedNow).toISOString();
  const quotaDay = calculateQuotaDay(now, config.timezone);
  const row = db.prepare(`
    SELECT limit_count, reserved_count
    FROM daily_message_quota
    WHERE sender_worker_id = ? AND quota_day = ?
  `).get(workerId, quotaDay);
  const limit = row ? Number(row.limit_count) : config.limit;
  const reserved = row ? Number(row.reserved_count) : 0;
  return {
    worker_id: workerId,
    quota_day: quotaDay,
    limit,
    reserved,
    remaining: Math.max(limit - reserved, 0),
    timezone: config.timezone,
  };
}

export function getDailyQuotaStatus(db, senderWorkerId) {
  return getDailyQuotaStatusAt(db, senderWorkerId, new Date().toISOString());
}

// Test-only deterministic clock seam; production callers use the trusted server clock above.
export function getDailyQuotaStatusForTest(db, senderWorkerId, trustedNow) {
  return getDailyQuotaStatusAt(db, senderWorkerId, trustedNow);
}

function ensureQuotaColumns(db) {
  const columns = new Set(db.prepare('PRAGMA table_info(outgoing_messages)').all().map(row => row.name));
  if (!columns.has('quota_day')) db.exec('ALTER TABLE outgoing_messages ADD COLUMN quota_day TEXT');
  if (!columns.has('quota_reserved')) db.exec('ALTER TABLE outgoing_messages ADD COLUMN quota_reserved INTEGER NOT NULL DEFAULT 0 CHECK (quota_reserved IN (0, 1))');
}

function ensureBatchAssociation(db) {
  // Called inside initializeLedger's single schema-bootstrap transaction.
  const columns = new Set(db.prepare('PRAGMA table_info(outgoing_messages)').all().map(row => row.name));
  if (!columns.has('batch_id')) db.exec('ALTER TABLE outgoing_messages ADD COLUMN batch_id TEXT');
  db.exec(`
    CREATE INDEX IF NOT EXISTS outgoing_messages_batch_lookup
      ON outgoing_messages(batch_id, sender_worker_id, state);
  `);
}

function ensureDispatchAuthorizationTable(db) {
  db.exec(`
    CREATE TABLE IF NOT EXISTS whatsapp_campaign_dispatch_authorizations (
      authorization_id TEXT PRIMARY KEY,
      batch_id TEXT NOT NULL UNIQUE,
      operation_id TEXT NOT NULL UNIQUE,
      sender_worker_id INTEGER NOT NULL CHECK (sender_worker_id > 0),
      plan_id INTEGER NOT NULL CHECK (plan_id > 0),
      request_fingerprint TEXT NOT NULL,
      consumed_at TEXT NOT NULL,
      expires_at TEXT NOT NULL,
      status TEXT NOT NULL CHECK (status IN ('consumed', 'completed', 'failed')),
      FOREIGN KEY (batch_id) REFERENCES whatsapp_campaign_batches(batch_id)
    );
    CREATE INDEX IF NOT EXISTS dispatch_auth_lookup
      ON whatsapp_campaign_dispatch_authorizations(sender_worker_id, batch_id);
  `);
  const columns = new Set(db.prepare('PRAGMA table_info(whatsapp_campaign_dispatch_authorizations)').all().map(row => row.name));
  if (!columns.has('issued_at')) db.exec('ALTER TABLE whatsapp_campaign_dispatch_authorizations ADD COLUMN issued_at TEXT');
}

function ensureRecipientCoordinationTables(db) {
  db.exec(`
    CREATE TABLE IF NOT EXISTS whatsapp_recipient_coordination (
      recipient_e164 TEXT PRIMARY KEY,
      state TEXT NOT NULL CHECK (state IN ('clear', 'opted_out')),
      opt_out_version INTEGER NOT NULL DEFAULT 0 CHECK (opt_out_version >= 0),
      last_request_id TEXT,
      opted_out_at TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS whatsapp_opt_out_requests (
      request_id TEXT PRIMARY KEY,
      recipient_e164 TEXT NOT NULL,
      actor_principal_id INTEGER NOT NULL CHECK (actor_principal_id > 0),
      scope TEXT NOT NULL CHECK (scope = 'platform'),
      source TEXT NOT NULL DEFAULT 'flask' CHECK (source = 'flask'),
      reason TEXT NOT NULL,
      status TEXT NOT NULL CHECK (status IN ('applied', 'duplicate')),
      created_at TEXT NOT NULL,
      applied_at TEXT NOT NULL,
      FOREIGN KEY (recipient_e164) REFERENCES whatsapp_recipient_coordination(recipient_e164)
    );
    CREATE INDEX IF NOT EXISTS whatsapp_opt_out_recipient_lookup
      ON whatsapp_opt_out_requests(recipient_e164, applied_at);
    CREATE TABLE IF NOT EXISTS whatsapp_legacy_opt_out_quarantine (
      quarantine_id INTEGER PRIMARY KEY AUTOINCREMENT,
      source_key TEXT NOT NULL UNIQUE,
      value_digest TEXT NOT NULL,
      reason TEXT NOT NULL,
      source_value TEXT,
      source_provenance TEXT,
      status TEXT NOT NULL DEFAULT 'unresolved' CHECK (status IN ('unresolved', 'resolved')),
      resolution_id TEXT,
      resolved_at TEXT,
      resolved_by INTEGER,
      resolved_recipient_e164 TEXT,
      resolution_reason TEXT,
      approved_mapping TEXT,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS whatsapp_legacy_opt_out_reconciliation (
      source_key TEXT PRIMARY KEY,
      source_digest TEXT NOT NULL,
      status TEXT NOT NULL CHECK (status IN ('imported', 'quarantined')),
      recipient_e164 TEXT,
      reconciled_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS whatsapp_dispatch_operations (
      operation_id TEXT PRIMARY KEY,
      authorization_id TEXT NOT NULL UNIQUE,
      batch_id TEXT NOT NULL UNIQUE,
      plan_id INTEGER NOT NULL CHECK (plan_id > 0),
      sender_worker_id INTEGER NOT NULL CHECK (sender_worker_id > 0),
      request_fingerprint TEXT NOT NULL,
      state TEXT NOT NULL CHECK (state IN ('authorized', 'submitting', 'acknowledged', 'failed', 'uncertain', 'reconciled')),
      expires_at TEXT NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      FOREIGN KEY (batch_id) REFERENCES whatsapp_campaign_batches(batch_id)
    );
    CREATE TABLE IF NOT EXISTS whatsapp_dispatch_recipient_leases (
      operation_id TEXT NOT NULL,
      message_id TEXT NOT NULL UNIQUE,
      batch_id TEXT NOT NULL,
      plan_recipient_id INTEGER NOT NULL,
      recipient_e164 TEXT NOT NULL,
      state TEXT NOT NULL CHECK (state IN ('authorized', 'revoked', 'submitting', 'acknowledged', 'failed', 'uncertain', 'reconciled')),
      lease_until TEXT NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      PRIMARY KEY (operation_id, plan_recipient_id),
      UNIQUE (operation_id, recipient_e164),
      FOREIGN KEY (operation_id) REFERENCES whatsapp_dispatch_operations(operation_id)
    );
    CREATE INDEX IF NOT EXISTS whatsapp_dispatch_lease_recipient_active
      ON whatsapp_dispatch_recipient_leases(recipient_e164, state);
    CREATE UNIQUE INDEX IF NOT EXISTS whatsapp_dispatch_lease_one_active
      ON whatsapp_dispatch_recipient_leases(recipient_e164)
      WHERE state IN ('authorized', 'submitting', 'uncertain');
  `);
  const quarantineColumns = new Set(db.prepare('PRAGMA table_info(whatsapp_legacy_opt_out_quarantine)').all().map(row => row.name));
  const quarantineMigrations = [
    ['source_value', 'TEXT'], ['source_provenance', 'TEXT'],
    ['status', "TEXT NOT NULL DEFAULT 'unresolved'"], ['resolution_id', 'TEXT'],
    ['resolved_at', 'TEXT'], ['resolved_by', 'INTEGER'],
    ['resolved_recipient_e164', 'TEXT'], ['resolution_reason', 'TEXT'],
    ['approved_mapping', 'TEXT'],
  ];
  for (const [name, definition] of quarantineMigrations) {
    if (!quarantineColumns.has(name)) db.exec(`ALTER TABLE whatsapp_legacy_opt_out_quarantine ADD COLUMN ${name} ${definition}`);
  }
  db.exec('CREATE UNIQUE INDEX IF NOT EXISTS legacy_quarantine_resolution_lookup ON whatsapp_legacy_opt_out_quarantine(resolution_id) WHERE resolution_id IS NOT NULL');
  const optOutColumns = new Set(db.prepare('PRAGMA table_info(whatsapp_opt_out_requests)').all().map(row => row.name));
  if (!optOutColumns.has('source')) db.exec("ALTER TABLE whatsapp_opt_out_requests ADD COLUMN source TEXT NOT NULL DEFAULT 'flask' CHECK (source = 'flask')");
}

export function initializeLedger(dbPath = defaultLedgerPath()) {
  getQuotaConfig();
  getAttemptReconciliationConfig();
  const absolute = validateLedgerPath(dbPath);
  fs.mkdirSync(path.dirname(absolute), { recursive: true, mode: 0o700 });
  ensureLedgerFileSafety(absolute);
  let db;
  for (let attempt = 0; attempt < 10; attempt++) {
    try {
      db = new DatabaseSync(absolute);
      db.exec('PRAGMA busy_timeout = 5000; PRAGMA journal_mode = WAL;');
      db.exec('BEGIN IMMEDIATE');
      break;
    } catch (err) {
      if (db) { try { db.close(); } catch {} db = null; }
      if (attempt === 9) throw err;
      const end = Date.now() + 25;
      while (Date.now() < end) {}
    }
  }
  try {
    db.exec(`
    CREATE TABLE IF NOT EXISTS outgoing_messages (
      message_id TEXT PRIMARY KEY,
      idempotency_key TEXT NOT NULL UNIQUE,
      batch_id TEXT,
      sender_worker_id INTEGER NOT NULL CHECK (sender_worker_id > 0),
      recipient_jid TEXT NOT NULL,
      campaign_id INTEGER,
      task_id INTEGER,
      content_type TEXT NOT NULL CHECK (content_type IN ('text', 'image', 'video')),
      payload_reference TEXT,
      state TEXT NOT NULL CHECK (state IN ('queued', 'attempted', 'acknowledged', 'delivered_unknown', 'failed')),
      attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
      failure_code TEXT,
      transport_message_id TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      attempted_at TEXT,
      acknowledged_at TEXT,
      delivered_unknown_at TEXT,
      failed_at TEXT
    );
    CREATE TABLE IF NOT EXISTS daily_message_quota (
      sender_worker_id INTEGER NOT NULL CHECK (sender_worker_id > 0),
      quota_day TEXT NOT NULL,
      limit_count INTEGER NOT NULL CHECK (limit_count > 0),
      reserved_count INTEGER NOT NULL DEFAULT 0 CHECK (reserved_count >= 0),
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      PRIMARY KEY (sender_worker_id, quota_day)
    );
    CREATE INDEX IF NOT EXISTS outgoing_messages_sender_state
      ON outgoing_messages(sender_worker_id, state, created_at);
    CREATE INDEX IF NOT EXISTS outgoing_messages_campaign_task
      ON outgoing_messages(campaign_id, task_id, created_at);
    CREATE TABLE IF NOT EXISTS whatsapp_campaign_batches (
      batch_id TEXT PRIMARY KEY,
      request_fingerprint TEXT NOT NULL,
      sender_worker_id INTEGER NOT NULL CHECK (sender_worker_id > 0),
      campaign_id INTEGER,
      task_id INTEGER,
      plan_id INTEGER NOT NULL CHECK (plan_id > 0),
      item_count INTEGER NOT NULL CHECK (item_count > 0 AND item_count <= ${MAX_CAMPAIGN_BATCH_ITEMS}),
      quota_day TEXT NOT NULL,
      state TEXT NOT NULL CHECK (state IN ('reserved')),
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS whatsapp_campaign_batch_items (
      batch_id TEXT NOT NULL,
      position INTEGER NOT NULL CHECK (position > 0 AND position <= ${MAX_CAMPAIGN_BATCH_ITEMS}),
      plan_id INTEGER NOT NULL CHECK (plan_id > 0),
      plan_recipient_id INTEGER NOT NULL CHECK (plan_recipient_id > 0),
      idempotency_key TEXT NOT NULL UNIQUE,
      message_id TEXT NOT NULL UNIQUE,
      recipient_jid TEXT NOT NULL,
      content_type TEXT NOT NULL CHECK (content_type IN ('text', 'image', 'video')),
      payload_reference TEXT,
      state TEXT NOT NULL CHECK (state IN ('reserved')),
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      PRIMARY KEY (batch_id, position),
      UNIQUE (batch_id, plan_recipient_id),
      UNIQUE (plan_id, plan_recipient_id),
      FOREIGN KEY (batch_id) REFERENCES whatsapp_campaign_batches(batch_id)
    );
    CREATE INDEX IF NOT EXISTS whatsapp_campaign_batch_items_lookup
      ON whatsapp_campaign_batch_items(batch_id, position);
    `);
    ensureQuotaColumns(db);
    ensureBatchAssociation(db);
    ensureDispatchAuthorizationTable(db);
    ensureRecipientCoordinationTables(db);
    const columns = new Set(db.prepare('PRAGMA table_info(outgoing_messages)').all().map(row => row.name));
    if (!columns.has('attempt_operation_id')) db.exec('ALTER TABLE outgoing_messages ADD COLUMN attempt_operation_id TEXT');
    if (!columns.has('attempt_lease_until')) db.exec('ALTER TABLE outgoing_messages ADD COLUMN attempt_lease_until TEXT');
    db.exec(`
      CREATE INDEX IF NOT EXISTS outgoing_messages_reconciliation_lookup
        ON outgoing_messages(state, attempted_at, attempt_lease_until);
      CREATE UNIQUE INDEX IF NOT EXISTS outgoing_messages_attempt_operation_unique
        ON outgoing_messages(attempt_operation_id)
        WHERE attempt_operation_id IS NOT NULL;
    `);
    db.exec('COMMIT');
  } catch (error) {
    rollback(db);
    db.close();
    throw error;
  }
  if (process.platform !== 'win32') fs.chmodSync(absolute, 0o600);
  return db;
}

function reserveAndEnqueueAt(db, input, trustedNow) {
  const config = getQuotaConfig();
  const record = {
    messageId: validateToken(input?.messageId, 'message ID'),
    idempotencyKey: validateToken(input?.idempotencyKey, 'idempotency key'),
    senderWorkerId: validateOptionalId(input?.senderWorkerId, 'sender worker ID'),
    recipientJid: input?.recipientJid,
    campaignId: validateOptionalId(input?.campaignId, 'campaign ID'),
    taskId: validateOptionalId(input?.taskId, 'task ID'),
    contentType: input?.contentType,
    payloadReference: input?.payloadReference ?? null,
    now: trustedNow,
  };
  if (!record.senderWorkerId) throw new Error('Invalid sender worker ID.');
  validateMessageInput(record);
  if (typeof record.now !== 'string' || Number.isNaN(new Date(record.now).getTime())) throw new Error('Invalid timestamp.');
  const quotaDay = calculateQuotaDay(record.now, config.timezone);

  db.exec('BEGIN IMMEDIATE');
  try {
    const existing = db.prepare('SELECT * FROM outgoing_messages WHERE idempotency_key = ?').get(record.idempotencyKey);
    if (existing) {
      db.exec('COMMIT');
      return { created: false, duplicate: true, record: existing };
    }

    db.prepare(`
      INSERT OR IGNORE INTO daily_message_quota
        (sender_worker_id, quota_day, limit_count, reserved_count, created_at, updated_at)
      VALUES (?, ?, ?, 0, ?, ?)
    `).run(record.senderWorkerId, quotaDay, config.limit, record.now, record.now);

    db.prepare(`
      UPDATE daily_message_quota
      SET reserved_count = reserved_count + 1, updated_at = ?
      WHERE sender_worker_id = ? AND quota_day = ? AND reserved_count < limit_count
    `).run(record.now, record.senderWorkerId, quotaDay);
    const changed = db.prepare('SELECT changes() AS count').get().count;
    if (changed !== 1) {
      const error = new Error('Daily message quota exceeded.');
      error.code = 'QUOTA_EXCEEDED';
      throw error;
    }

    db.prepare(`
      INSERT INTO outgoing_messages
        (message_id, idempotency_key, sender_worker_id, recipient_jid,
         campaign_id, task_id, content_type, payload_reference, state,
         attempt_count, created_at, updated_at, quota_day, quota_reserved)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?, 1)
    `).run(
      record.messageId, record.idempotencyKey, record.senderWorkerId, record.recipientJid,
      record.campaignId, record.taskId, record.contentType, record.payloadReference,
      record.now, record.now, quotaDay,
    );
    db.exec('COMMIT');
  } catch (error) {
    rollback(db);
    if (error?.code === 'QUOTA_EXCEEDED') throw error;
    throw new Error('Unable to reserve daily message quota.');
  }
  return { created: true, duplicate: false, record: getOutgoingMessage(db, record.messageId) };
}

export function reserveAndEnqueueOutgoingMessage(db, input) {
  return reserveAndEnqueueAt(db, input, new Date().toISOString());
}

// Test-only deterministic clock seam. Production callers must use the function above.
export function reserveAndEnqueueOutgoingMessageForTest(db, input, trustedNow) {
  if (typeof trustedNow !== 'string') throw new Error('A trusted test timestamp is required.');
  return reserveAndEnqueueAt(db, input, trustedNow);
}

/**
 * Canonical identity shared with Flask campaign-plan recipients.
 * The SHA-256 hex digest is both a valid ledger token and a fixed 64-byte key.
 */
export function deriveCampaignPlanRecipientIdempotencyKey(planId, planRecipientId) {
  const validPlanId = validateOptionalId(planId, 'campaign plan ID');
  const validRecipientId = validateOptionalId(planRecipientId, 'plan recipient ID');
  if (!validPlanId || !validRecipientId) throw new Error('Invalid campaign plan recipient identity.');
  return crypto.createHash('sha256')
    .update(`taskpe-wa-plan-recipient-v1:${validPlanId}:${validRecipientId}`, 'utf8')
    .digest('hex');
}

export function calculateCampaignBatchRequestFingerprint({ batchId, senderWorkerId, campaignId = null, taskId = null, planId, items }) {
  const canonical = {
    batchId, senderWorkerId, campaignId, taskId, planId,
    items: (items || []).map(({ position, planRecipientId, idempotencyKey, messageId, recipientJid, contentType, payloadReference }, index) => ({
      position: position || index + 1, planRecipientId,
      idempotencyKey: idempotencyKey || deriveCampaignPlanRecipientIdempotencyKey(planId, planRecipientId),
      messageId: messageId || deriveCampaignPlanRecipientIdempotencyKey(planId, planRecipientId),
      recipientJid, contentType, payloadReference: payloadReference ?? null,
    })),
  };
  return crypto.createHash('sha256').update(JSON.stringify(canonical), 'utf8').digest('hex');
}

function batchResult(db, batchId) {
  const batch = db.prepare(`
    SELECT batch_id, request_fingerprint, sender_worker_id, campaign_id, task_id,
           plan_id, item_count, quota_day, state, created_at, updated_at
    FROM whatsapp_campaign_batches WHERE batch_id = ?
  `).get(batchId);
  if (!batch) return null;
  const items = db.prepare(`
    SELECT batch_id, position, plan_id, plan_recipient_id, idempotency_key, message_id,
           recipient_jid, content_type, payload_reference, state, created_at, updated_at
    FROM whatsapp_campaign_batch_items
    WHERE batch_id = ? ORDER BY position
  `).all(batchId);
  return { batch, items };
}

function batchError(message, code) {
  const error = new Error(message);
  error.code = code;
  return error;
}

function reserveCampaignBatchAt(db, input, trustedNow) {
  const config = getQuotaConfig();
  const normalized = validateBatchInput(input);
  if (typeof trustedNow !== 'string' || Number.isNaN(new Date(trustedNow).getTime())) throw new Error('Invalid timestamp.');
  const now = new Date(trustedNow).toISOString();
  const quotaDay = calculateQuotaDay(now, config.timezone);

  db.exec('BEGIN IMMEDIATE');
  try {
    const existing = db.prepare(`
      SELECT request_fingerprint FROM whatsapp_campaign_batches WHERE batch_id = ?
    `).get(normalized.batchId);
    if (existing) {
      if (existing.request_fingerprint !== normalized.requestFingerprint) throw batchError('Batch identity conflicts with an existing reservation.', 'BATCH_CONFLICT');
      const stored = batchResult(db, normalized.batchId);
      db.exec('COMMIT');
      return { created: false, duplicate: true, ...stored };
    }

    db.prepare(`
      INSERT OR IGNORE INTO daily_message_quota
        (sender_worker_id, quota_day, limit_count, reserved_count, created_at, updated_at)
      VALUES (?, ?, ?, 0, ?, ?)
    `).run(normalized.senderWorkerId, quotaDay, config.limit, now, now);
    db.prepare(`
      UPDATE daily_message_quota
      SET reserved_count = reserved_count + ?, updated_at = ?
      WHERE sender_worker_id = ? AND quota_day = ?
        AND reserved_count + ? <= limit_count
    `).run(normalized.items.length, now, normalized.senderWorkerId, quotaDay, normalized.items.length);
    const quotaChanged = db.prepare('SELECT changes() AS count').get().count;
    if (quotaChanged !== 1) throw batchError('Daily message quota exceeded.', 'QUOTA_EXCEEDED');

    db.prepare(`
      INSERT INTO whatsapp_campaign_batches
        (batch_id, request_fingerprint, sender_worker_id, campaign_id, task_id,
         plan_id, item_count, quota_day, state, created_at, updated_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `).run(
      normalized.batchId, normalized.requestFingerprint, normalized.senderWorkerId,
      normalized.campaignId, normalized.taskId, normalized.planId,
      normalized.items.length, quotaDay, BATCH_STATE, now, now,
    );

    const insertOutgoing = db.prepare(`
      INSERT INTO outgoing_messages
        (message_id, idempotency_key, batch_id, sender_worker_id, recipient_jid,
         campaign_id, task_id, content_type, payload_reference, state,
         attempt_count, created_at, updated_at, quota_day, quota_reserved)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?, 1)
    `);
    const insertItem = db.prepare(`
      INSERT INTO whatsapp_campaign_batch_items
        (batch_id, position, plan_id, plan_recipient_id, idempotency_key,
         message_id, recipient_jid, content_type, payload_reference, state,
         created_at, updated_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?)
    `);
    for (const item of normalized.items) {
      const prior = db.prepare('SELECT message_id FROM outgoing_messages WHERE idempotency_key = ?').get(item.idempotencyKey);
      if (prior) throw batchError('Batch item identity is already reserved.', 'BATCH_ITEM_CONFLICT');
      insertOutgoing.run(
        item.messageId, item.idempotencyKey, normalized.batchId, normalized.senderWorkerId, item.recipientJid,
        normalized.campaignId, normalized.taskId, item.contentType, item.payloadReference,
        now, now, quotaDay,
      );
      insertItem.run(
        normalized.batchId, item.position, normalized.planId, item.planRecipientId,
        item.idempotencyKey, item.messageId, item.recipientJid, item.contentType,
        item.payloadReference, now, now,
      );
    }
    db.exec('COMMIT');
  } catch (error) {
    rollback(db);
    if (error?.code === 'QUOTA_EXCEEDED' || error?.code === 'BATCH_CONFLICT' || error?.code === 'BATCH_ITEM_CONFLICT') throw error;
    throw batchError('Unable to reserve campaign batch.', 'BATCH_RESERVATION_FAILED');
  }
  return { created: true, duplicate: false, ...batchResult(db, normalized.batchId) };
}

export function reserveCampaignBatch(db, input) {
  return reserveCampaignBatchAt(db, input, new Date().toISOString());
}

// Test-only deterministic clock seam; production callers use the trusted server clock above.
export function reserveCampaignBatchForTest(db, input, trustedNow) {
  if (typeof trustedNow !== 'string') throw new Error('A trusted test timestamp is required.');
  return reserveCampaignBatchAt(db, input, trustedNow);
}

export function getCampaignBatch(db, batchId, senderWorkerId = null) {
  const id = validateBatchIdentity(batchId, 'batch ID');
  const workerId = senderWorkerId === null ? null : validateOptionalId(senderWorkerId, 'sender worker ID');
  const row = workerId
    ? db.prepare('SELECT batch_id FROM whatsapp_campaign_batches WHERE batch_id = ? AND sender_worker_id = ?').get(id, workerId)
    : db.prepare('SELECT batch_id FROM whatsapp_campaign_batches WHERE batch_id = ?').get(id);
  return row ? batchResult(db, id) : null;
}

function validPayloadReference(value) {
  return value === null || value === undefined ||
    (typeof value === 'string' && value.length <= 500 && !/[\u0000-\u001f\u007f]/.test(value));
}

function cancellationEvidenceItemIsValid(item, batchId, expectedPlanId, position) {
  try {
    const expectedKey = deriveCampaignPlanRecipientIdempotencyKey(expectedPlanId ?? item.plan_id, item.plan_recipient_id);
    return item.batch_id === batchId && item.position === position &&
      Number.isSafeInteger(item.plan_id) && item.plan_id > 0 &&
      Number.isSafeInteger(item.plan_recipient_id) && item.plan_recipient_id > 0 &&
      item.idempotency_key === expectedKey && item.message_id === expectedKey &&
      JID_PATTERN.test(item.recipient_jid) && CONTENT_TYPES.has(item.content_type) &&
      validPayloadReference(item.payload_reference) && item.state === 'reserved';
  } catch {
    return false;
  }
}

function cancellationEvidenceRowIsValid(row, expected) {
  if (!row || !expected) return false;
  try {
    validateToken(row.message_id, 'message ID');
    validateToken(row.idempotency_key, 'idempotency key');
  } catch {
    return false;
  }
  if (row.batch_id !== expected.batch_id ||
      row.message_id !== expected.message_id ||
      row.idempotency_key !== expected.idempotency_key ||
      (expected.sender_worker_id !== undefined && row.sender_worker_id !== expected.sender_worker_id) ||
      (expected.campaign_id !== undefined && row.campaign_id !== expected.campaign_id) ||
      (expected.task_id !== undefined && row.task_id !== expected.task_id) ||
      row.recipient_jid !== expected.recipient_jid ||
      row.content_type !== expected.content_type ||
      (row.payload_reference ?? null) !== (expected.payload_reference ?? null)) return false;
  return Number.isSafeInteger(row.sender_worker_id) && row.sender_worker_id > 0 &&
    (row.campaign_id === null || (Number.isSafeInteger(row.campaign_id) && row.campaign_id > 0)) &&
    (row.task_id === null || (Number.isSafeInteger(row.task_id) && row.task_id > 0)) &&
    JID_PATTERN.test(row.recipient_jid) && CONTENT_TYPES.has(row.content_type) &&
    validPayloadReference(row.payload_reference) &&
    ['queued', 'attempted', 'acknowledged', 'delivered_unknown', 'failed'].includes(row.state);
}

function cancellationEvidenceOrphanIsValid(row, batchId, senderWorkerId) {
  return cancellationEvidenceRowIsValid(row, {
    batch_id: batchId,
    message_id: row?.message_id,
    idempotency_key: row?.idempotency_key,
    sender_worker_id: senderWorkerId,
    recipient_jid: row?.recipient_jid,
    content_type: row?.content_type,
    payload_reference: row?.payload_reference ?? null,
  }) && row.message_id === row.idempotency_key;
}

/**
 * Return read-only evidence for a future safe campaign-plan cancellation.
 * This deliberately does not delete, transition, refund, release, retry, or send.
 */
export function getCampaignBatchCancellationEvidence(db, batchId, senderWorkerId) {
  const id = validateBatchIdentity(batchId, 'batch ID');
  const workerId = validateOptionalId(senderWorkerId, 'sender worker ID');
  if (!workerId) throw new Error('Invalid sender worker ID.');

  db.exec('BEGIN');
  try {
    const batchById = db.prepare(`
      SELECT batch_id, request_fingerprint, sender_worker_id, campaign_id, task_id,
             plan_id, item_count, quota_day, state, created_at, updated_at
      FROM whatsapp_campaign_batches WHERE batch_id = ?
    `).get(id);
    // Do not reveal another worker's batch through an evidence response.
    const batch = batchById && batchById.sender_worker_id === workerId ? batchById : null;
    const items = db.prepare(`
      SELECT batch_id, position, plan_id, plan_recipient_id, idempotency_key,
             message_id, recipient_jid, content_type, payload_reference, state
      FROM whatsapp_campaign_batch_items
      WHERE batch_id = ? ORDER BY position
    `).all(id);

    const associatedOutgoing = db.prepare(`
      SELECT message_id, idempotency_key, batch_id, sender_worker_id, campaign_id, task_id,
             recipient_jid, content_type, payload_reference, state
      FROM outgoing_messages WHERE batch_id = ?
    `).all(id);

    // Do not reveal another worker's batch through an evidence response.
    if (batchById && batchById.sender_worker_id !== workerId) {
      db.exec('COMMIT');
      return { status: 'absent', batch_id: id };
    }
    if (!batch && items.length === 0) {
      // An associated row is durable evidence even if its batch mappings were lost.
      if (associatedOutgoing.length > 0) {
        const allForeign = associatedOutgoing.every(row => row.sender_worker_id !== workerId);
        if (allForeign) {
          db.exec('COMMIT');
          return { status: 'absent', batch_id: id };
        }
        if (!associatedOutgoing.every(row => cancellationEvidenceOrphanIsValid(row, id, workerId))) {
          db.exec('COMMIT');
          return { status: 'invalid', batch_id: id };
        }
        db.exec('COMMIT');
        return { status: 'present', batch_id: id, batch_record_present: false, outgoing_record_count: associatedOutgoing.length };
      }
      db.exec('COMMIT');
      return { status: 'absent', batch_id: id };
    }

    if (!batch) {
      const outgoingByKey = db.prepare(`
        SELECT message_id, idempotency_key, batch_id, sender_worker_id, campaign_id, task_id,
               recipient_jid, content_type, payload_reference, state
        FROM outgoing_messages WHERE idempotency_key = ?
      `);
      const seenKeys = new Set();
      if (associatedOutgoing.length !== items.length) {
        db.exec('COMMIT');
        return { status: 'invalid', batch_id: id };
      }
      for (const [index, item] of items.entries()) {
        if (!cancellationEvidenceItemIsValid(item, id, null, index + 1) || seenKeys.has(item.idempotency_key)) {
          db.exec('COMMIT');
          return { status: 'invalid', batch_id: id };
        }
        seenKeys.add(item.idempotency_key);
        const outgoing = outgoingByKey.get(item.idempotency_key);
        if (!outgoing || !cancellationEvidenceRowIsValid(outgoing, {
          batch_id: id,
          message_id: item.message_id,
          idempotency_key: item.idempotency_key,
          sender_worker_id: workerId,
          recipient_jid: item.recipient_jid,
          content_type: item.content_type,
          payload_reference: item.payload_reference,
        })) {
          db.exec('COMMIT');
          return { status: 'invalid', batch_id: id };
        }
      }
      db.exec('COMMIT');
      return { status: 'present', batch_id: id, batch_record_present: false, outgoing_record_count: items.length };
    }

    if (batch.sender_worker_id !== workerId ||
        !BATCH_FINGERPRINT_PATTERN.test(batch.request_fingerprint) ||
        !Number.isSafeInteger(batch.plan_id) || batch.plan_id < 1 ||
        !Number.isSafeInteger(batch.item_count) || batch.item_count < 1 || batch.item_count > MAX_CAMPAIGN_BATCH_ITEMS ||
        batch.state !== BATCH_STATE || items.length !== batch.item_count) {
      db.exec('COMMIT');
      return { status: 'invalid', batch_id: id };
    }

    const outgoingByKey = db.prepare(`
      SELECT message_id, idempotency_key, batch_id, sender_worker_id, campaign_id, task_id,
             recipient_jid, content_type, payload_reference, state
      FROM outgoing_messages WHERE idempotency_key = ?
    `);
    if (associatedOutgoing.length !== items.length) {
      db.exec('COMMIT');
      return { status: 'invalid', batch_id: id };
    }
    const seenKeys = new Set();
    const associatedKeys = new Set(associatedOutgoing.map(row => row.idempotency_key));
    const stateCounts = {};
    for (const [index, item] of items.entries()) {
      const expectedKey = deriveCampaignPlanRecipientIdempotencyKey(batch.plan_id, item.plan_recipient_id);
      const expected = {
        message_id: expectedKey,
        idempotency_key: expectedKey,
        batch_id: id,
        sender_worker_id: batch.sender_worker_id,
        campaign_id: batch.campaign_id,
        task_id: batch.task_id,
        recipient_jid: item.recipient_jid,
        content_type: item.content_type,
        payload_reference: item.payload_reference,
      };
      if (!cancellationEvidenceItemIsValid(item, id, batch.plan_id, index + 1) ||
          seenKeys.has(expectedKey) || !associatedKeys.has(expectedKey)) {
        db.exec('COMMIT');
        return { status: 'invalid', batch_id: id };
      }
      seenKeys.add(expectedKey);
      const outgoing = outgoingByKey.get(expectedKey);
      if (!cancellationEvidenceRowIsValid(outgoing, expected)) {
        db.exec('COMMIT');
        return { status: 'invalid', batch_id: id };
      }
      stateCounts[outgoing.state] = (stateCounts[outgoing.state] || 0) + 1;
    }
    db.exec('COMMIT');
    return {
      status: 'present',
      batch_id: id,
      request_fingerprint: batch.request_fingerprint,
      batch_record_present: true,
      item_count: items.length,
      outgoing_record_count: items.length,
      outgoing_state_counts: stateCounts,
    };
  } catch (error) {
    rollback(db);
    throw error;
  }
}

function reconcileStaleAttemptedMessagesAt(db, trustedNow) {
  const config = getAttemptReconciliationConfig();
  const nowDate = new Date(trustedNow);
  if (Number.isNaN(nowDate.getTime())) throw new Error('Invalid reconciliation timestamp.');
  const now = nowDate.toISOString();
  const cutoff = new Date(nowDate.getTime() - config.staleSeconds * 1000).toISOString();

  db.exec('BEGIN IMMEDIATE');
  try {
    const result = db.prepare(`
      UPDATE outgoing_messages
      SET state = 'delivered_unknown', delivered_unknown_at = ?, updated_at = ?
      WHERE state = 'attempted'
        AND attempted_at IS NOT NULL
        AND julianday(attempted_at) <= julianday(?)
        AND attempt_lease_until IS NOT NULL
        AND julianday(attempt_lease_until) <= julianday(?)
    `).run(now, now, cutoff, now);
    db.exec('COMMIT');
    return { reconciled_count: Number(result.changes ?? 0) };
  } catch {
    rollback(db);
    throw new Error('Unable to reconcile attempted messages.');
  }
}

export function reconcileStaleAttemptedMessages(db) {
  return reconcileStaleAttemptedMessagesAt(db, new Date().toISOString());
}

// Test-only deterministic clock seam. Production callers must use the function above.
export function reconcileStaleAttemptedMessagesForTest(db, trustedNow) {
  if (typeof trustedNow !== 'string') throw new Error('A trusted test timestamp is required.');
  return reconcileStaleAttemptedMessagesAt(db, trustedNow);
}

export function closeLedger(db) {
  db.close();
}

export function transitionOutgoingMessage(db, messageId, nextState, options = {}) {
  const id = validateToken(messageId, 'message ID');
  if (!LEDGER_STATES.includes(nextState)) throw new Error('Invalid message state.');
  const now = options.now || new Date().toISOString();
  const failureCode = options.failureCode ?? null;
  const transportMessageId = options.transportMessageId ?? null;
  const attemptOperationId = options.attemptOperationId ?? null;
  const attemptLeaseUntil = options.attemptLeaseUntil ?? null;
  if (failureCode !== null && (typeof failureCode !== 'string' || !FAILURE_CODE_PATTERN.test(failureCode))) throw new Error('Invalid failure code.');
  if (transportMessageId !== null && (typeof transportMessageId !== 'string' || !TOKEN_PATTERN.test(transportMessageId))) throw new Error('Invalid transport message ID.');
  if (attemptOperationId !== null) validateOperationId(attemptOperationId);
  const normalizedAttemptLeaseUntil = attemptLeaseUntil === null ? null : validateLeaseTimestamp(attemptLeaseUntil);

  db.exec('BEGIN IMMEDIATE');
  try {
    const current = getOutgoingMessage(db, id);
    if (!current) throw new Error('Outgoing message not found.');
    if (!STATE_TRANSITIONS.get(current.state)?.has(nextState)) throw new Error('Invalid message state transition.');
    if (nextState === 'failed' && !failureCode) throw new Error('Failure code is required.');
    if (nextState === 'attempted') {
      if (current.state !== 'queued' || !attemptOperationId || !attemptLeaseUntil) throw new Error('Attempt operation ownership is required.');
      if (new Date(normalizedAttemptLeaseUntil).getTime() <= new Date(now).getTime()) throw new Error('Attempt lease must be in the future.');
    }
    if (['acknowledged', 'failed', 'delivered_unknown'].includes(nextState)) {
      if (!attemptOperationId || current.attempt_operation_id !== attemptOperationId) throw new Error('Attempt operation ownership mismatch.');
    }

    const attemptedAt = nextState === 'attempted' && !current.attempted_at ? now : current.attempted_at;
    const acknowledgedAt = nextState === 'acknowledged' && !current.acknowledged_at ? now : current.acknowledged_at;
    const deliveredUnknownAt = nextState === 'delivered_unknown' && !current.delivered_unknown_at ? now : current.delivered_unknown_at;
    const failedAt = nextState === 'failed' && !current.failed_at ? now : current.failed_at;
    const attemptCount = nextState === 'attempted' ? current.attempt_count + 1 : current.attempt_count;
    const nextFailureCode = nextState === 'failed' ? failureCode : current.failure_code;
    const nextTransportId = transportMessageId ?? current.transport_message_id;
    const result = db.prepare(`
      UPDATE outgoing_messages
      SET state = ?, attempt_count = ?, failure_code = ?, transport_message_id = ?,
          updated_at = ?, attempted_at = ?, acknowledged_at = ?, delivered_unknown_at = ?, failed_at = ?,
          attempt_operation_id = CASE WHEN ? = 'attempted' THEN ? ELSE attempt_operation_id END,
          attempt_lease_until = CASE WHEN ? = 'attempted' THEN ? ELSE attempt_lease_until END
      WHERE message_id = ? AND state = ?
        AND (? IS NULL OR attempt_operation_id = ?)
    `).run(
      nextState, attemptCount, nextFailureCode, nextTransportId,
      now, attemptedAt, acknowledgedAt, deliveredUnknownAt, failedAt,
      nextState, attemptOperationId, nextState, normalizedAttemptLeaseUntil,
      id, current.state,
      nextState === 'attempted' ? null : attemptOperationId,
      nextState === 'attempted' ? null : attemptOperationId,
    );
    if (result.changes !== 1) throw new Error('Attempt operation ownership mismatch.');
    db.exec('COMMIT');
  } catch (error) {
    rollback(db);
    throw error.message === 'Outgoing message not found.' || error.message === 'Invalid message state transition.' || error.message === 'Failure code is required.' || error.message === 'Attempt operation ownership is required.' || error.message === 'Attempt operation ownership mismatch.' || error.message === 'Attempt lease must be in the future.'
      ? error
      : new Error('Unable to update outgoing message record.');
  }
  return getOutgoingMessage(db, id);
}

export function getOutgoingMessage(db, messageId) {
  const id = validateToken(messageId, 'message ID');
  return db.prepare('SELECT * FROM outgoing_messages WHERE message_id = ?').get(id) || null;
}

export function getOutgoingMessageByIdempotencyKey(db, idempotencyKey) {
  const key = validateToken(idempotencyKey, 'idempotency key');
  return db.prepare('SELECT * FROM outgoing_messages WHERE idempotency_key = ?').get(key) || null;
}

function validateRecipientE164(value) {
  if (typeof value !== 'string' || !/^\+[1-9][0-9]{6,14}$/.test(value)) {
    throw new Error('Invalid canonical recipient.');
  }
  return value;
}

function recipientE164FromJid(jid) {
  if (typeof jid !== 'string' || !JID_PATTERN.test(jid)) throw new Error('Invalid recipient.');
  return validateRecipientE164(`+${jid.slice(0, jid.indexOf('@'))}`);
}

function validateOptOutReason(value) {
  if (typeof value !== 'string' || value.length < 1 || value.length > 200 || /[\u0000-\u001f\u007f]/.test(value)) {
    throw new Error('Invalid opt-out reason.');
  }
  return value;
}

export function registerRecipientOptOut(db, input, trustedNow = new Date().toISOString()) {
  const requestId = validateToken(input?.requestId, 'opt-out request ID');
  const recipientE164 = validateRecipientE164(input?.recipientE164);
  const actorPrincipalId = validateOptionalId(input?.actorPrincipalId, 'opt-out actor');
  if (!actorPrincipalId) throw new Error('Invalid opt-out actor.');
  if (input?.scope !== 'platform') throw new Error('Invalid opt-out scope.');
  if (input?.source !== undefined && input.source !== 'flask') throw new Error('Invalid opt-out source.');
  const reason = validateOptOutReason(input?.reason || 'user_opt_out');
  const now = validateLeaseTimestamp(trustedNow, 'opt-out timestamp');
  db.exec('BEGIN IMMEDIATE');
  try {
    const existing = db.prepare('SELECT * FROM whatsapp_opt_out_requests WHERE request_id = ?').get(requestId);
    if (existing) {
      if (existing.recipient_e164 !== recipientE164 || existing.actor_principal_id !== actorPrincipalId || existing.reason !== reason || existing.scope !== 'platform') {
        throw batchError('Opt-out request identity conflicts with an existing request.', 'OPTOUT_REQUEST_CONFLICT');
      }
      db.exec('COMMIT');
      return { requestId, status: 'duplicate', version: Number(db.prepare('SELECT opt_out_version FROM whatsapp_recipient_coordination WHERE recipient_e164 = ?').get(recipientE164)?.opt_out_version || 0) };
    }
    let coordination = db.prepare('SELECT * FROM whatsapp_recipient_coordination WHERE recipient_e164 = ?').get(recipientE164);
    if (!coordination) {
      db.prepare(`INSERT INTO whatsapp_recipient_coordination
        (recipient_e164, state, opt_out_version, created_at, updated_at) VALUES (?, 'clear', 0, ?, ?)`)
        .run(recipientE164, now, now);
      coordination = { opt_out_version: 0, state: 'clear' };
    }
    const nextVersion = Number(coordination.opt_out_version) + 1;
    db.prepare(`INSERT INTO whatsapp_opt_out_requests
      (request_id, recipient_e164, actor_principal_id, scope, source, reason, status, created_at, applied_at)
      VALUES (?, ?, ?, 'platform', 'flask', ?, 'applied', ?, ?)`)
      .run(requestId, recipientE164, actorPrincipalId, reason, now, now);
    db.prepare(`UPDATE whatsapp_recipient_coordination
      SET state = 'opted_out', opt_out_version = ?, last_request_id = ?, opted_out_at = ?, updated_at = ?
      WHERE recipient_e164 = ?`)
      .run(nextVersion, requestId, now, now, recipientE164);
    db.prepare(`UPDATE whatsapp_dispatch_recipient_leases
      SET state = 'revoked', updated_at = ?
      WHERE recipient_e164 = ? AND state = 'authorized'`).run(now, recipientE164);
    db.exec('COMMIT');
    return { requestId, status: 'applied', version: nextVersion };
  } catch (error) {
    rollback(db);
    throw error;
  }
}

export function migrateLegacyOptOuts(db, rows, trustedNow = new Date().toISOString()) {
  if (!Array.isArray(rows)) throw new Error('Legacy opt-out rows are invalid.');
  const now = validateLeaseTimestamp(trustedNow, 'migration timestamp');
  db.exec('BEGIN IMMEDIATE');
  try {
    for (const row of rows) {
      const sourceKey = validateToken(String(row?.sourceKey || ''), 'legacy opt-out source key');
      const raw = typeof row?.recipientE164 === 'string' ? row.recipientE164 : '';
      if (!/^\+[1-9][0-9]{6,14}$/.test(raw)) {
        const digest = crypto.createHash('sha256').update(String(row?.rawValue || raw)).digest('hex');
        db.prepare(`INSERT OR IGNORE INTO whatsapp_legacy_opt_out_quarantine
          (source_key, value_digest, reason, source_value, source_provenance, created_at)
          VALUES (?, ?, 'canonical verification required', ?, 'legacy-migration-input', ?)`)
          .run(sourceKey, digest, String(row?.rawValue || raw), now);
        continue;
      }
      const requestId = `legacy-${sourceKey}`;
      const existing = db.prepare('SELECT recipient_e164 FROM whatsapp_opt_out_requests WHERE request_id = ?').get(requestId);
      if (!existing) {
        let coordination = db.prepare('SELECT opt_out_version FROM whatsapp_recipient_coordination WHERE recipient_e164 = ?').get(raw);
        if (!coordination) {
          db.prepare(`INSERT INTO whatsapp_recipient_coordination (recipient_e164, state, opt_out_version, created_at, updated_at) VALUES (?, 'clear', 0, ?, ?)`)
            .run(raw, now, now);
          coordination = { opt_out_version: 0 };
        }
        const nextVersion = Number(coordination.opt_out_version) + 1;
        db.prepare(`INSERT INTO whatsapp_opt_out_requests (request_id, recipient_e164, actor_principal_id, scope, reason, status, created_at, applied_at) VALUES (?, ?, 1, 'platform', 'legacy_opt_out', 'applied', ?, ?)`)
          .run(requestId, raw, now, now);
        db.prepare(`UPDATE whatsapp_recipient_coordination SET state = 'opted_out', opt_out_version = ?, last_request_id = ?, opted_out_at = ?, updated_at = ? WHERE recipient_e164 = ?`)
          .run(nextVersion, requestId, now, now, raw);
      }
    }
    db.exec('COMMIT');
  } catch (error) {
    rollback(db);
    throw error;
  }
  return { imported: rows.filter(row => /^\+[1-9][0-9]{6,14}$/.test(row?.recipientE164 || '')).length };
}

/**
 * Controlled, read-only reconciliation from the Flask mirror. The source
 * database is never changed. Only canonical E.164 values or an explicit
 * caller-supplied source-key mapping are imported; all other records are
 * quarantined and remain unsafe for automated dispatch until reviewed.
 */
export function reconcileLegacyFlaskOptOuts(db, flaskDbPath, { approvedMappings = {}, trustedNow = new Date().toISOString() } = {}) {
  if (typeof flaskDbPath !== 'string' || !path.isAbsolute(flaskDbPath)) throw new Error('Flask database path must be absolute.');
  const source = new DatabaseSync(flaskDbPath, { readOnly: true });
  const now = validateLeaseTimestamp(trustedNow, 'reconciliation timestamp');
  let imported = 0;
  let quarantined = 0;
  let existing = 0;
  let conflicting = 0;
  let failed = 0;
  try {
    const table = source.prepare("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'whatsapp_opt_outs'").get();
    if (!table) throw new Error('Flask opt-out table is unavailable.');
    const sourceVersionBefore = Number(source.prepare('PRAGMA data_version').get()?.data_version || 0);
    const rows = source.prepare('SELECT id, phone_number, reason, opted_out_at FROM whatsapp_opt_outs ORDER BY id').all();
    const sourceVersionAfter = Number(source.prepare('PRAGMA data_version').get()?.data_version || 0);
    if (sourceVersionBefore !== sourceVersionAfter) {
      conflicting += 1;
      throw batchError('Legacy opt-out source changed during reconciliation.', 'LEGACY_SOURCE_CHANGED_DURING_RECONCILIATION');
    }
    db.exec('BEGIN IMMEDIATE');
    try {
      for (const row of rows) {
        const sourceKey = `flask:${row.id}`;
        const raw = String(row.phone_number ?? '');
        const digest = crypto.createHash('sha256').update(`${raw}\u0000${String(row.reason ?? '')}\u0000${String(row.opted_out_at ?? '')}`).digest('hex');
        const prior = db.prepare('SELECT source_digest, status FROM whatsapp_legacy_opt_out_reconciliation WHERE source_key = ?').get(sourceKey);
        if (prior) {
          if (prior.source_digest !== digest) {
            conflicting += 1;
            throw batchError('Legacy opt-out source changed after reconciliation.', 'LEGACY_SOURCE_CONFLICT');
          }
          existing += 1;
          continue;
        }
        const mapped = approvedMappings && Object.prototype.hasOwnProperty.call(approvedMappings, String(row.id))
          ? approvedMappings[String(row.id)] : raw;
        const canonical = /^\+[1-9][0-9]{6,14}$/.test(mapped) ? mapped : null;
        if (!canonical) {
          db.prepare(`INSERT INTO whatsapp_legacy_opt_out_quarantine
            (source_key, value_digest, reason, source_value, source_provenance, created_at)
            VALUES (?, ?, 'canonical verification or approved mapping required', ?, 'flask:whatsapp_opt_outs', ?)`)
            .run(sourceKey, digest, raw, now);
          db.prepare(`INSERT INTO whatsapp_legacy_opt_out_reconciliation (source_key, source_digest, status, recipient_e164, reconciled_at) VALUES (?, ?, 'quarantined', NULL, ?)`)
            .run(sourceKey, digest, now);
          quarantined += 1;
          continue;
        }
        let coordination = db.prepare('SELECT opt_out_version FROM whatsapp_recipient_coordination WHERE recipient_e164 = ?').get(canonical);
        if (!coordination) {
          db.prepare(`INSERT INTO whatsapp_recipient_coordination (recipient_e164, state, opt_out_version, created_at, updated_at) VALUES (?, 'clear', 0, ?, ?)`)
            .run(canonical, now, now);
          coordination = { opt_out_version: 0 };
        }
        if (db.prepare('SELECT 1 FROM whatsapp_recipient_coordination WHERE recipient_e164 = ? AND state = \'opted_out\'').get(canonical)) {
          existing += 1;
        } else {
          const requestId = `legacy-flask-${row.id}`;
          const nextVersion = Number(coordination.opt_out_version) + 1;
          db.prepare(`INSERT OR IGNORE INTO whatsapp_opt_out_requests (request_id, recipient_e164, actor_principal_id, scope, source, reason, status, created_at, applied_at) VALUES (?, ?, 1, 'platform', 'flask', ?, 'applied', ?, ?)`)
            .run(requestId, canonical, String(row.reason || 'legacy_opt_out').slice(0, 200), now, now);
          db.prepare(`UPDATE whatsapp_recipient_coordination SET state = 'opted_out', opt_out_version = ?, last_request_id = ?, opted_out_at = ?, updated_at = ? WHERE recipient_e164 = ?`)
            .run(nextVersion, requestId, now, now, canonical);
          imported += 1;
        }
        db.prepare(`INSERT INTO whatsapp_legacy_opt_out_reconciliation (source_key, source_digest, status, recipient_e164, reconciled_at) VALUES (?, ?, 'imported', ?, ?)`)
          .run(sourceKey, digest, canonical, now);
      }
      db.exec('COMMIT');
    } catch (error) { rollback(db); throw error; }
    const unresolved = Number(db.prepare("SELECT COUNT(*) AS count FROM whatsapp_legacy_opt_out_quarantine WHERE status = 'unresolved'").get()?.count || 0);
    return {
      source_count: rows.length, imported_count: imported, quarantined_count: quarantined,
      existing_count: existing, unresolved_count: unresolved, conflicting_count: conflicting, failed_count: failed,
    };
  } finally { source.close(); }
}

/**
 * Resolve a quarantined legacy record by retaining suppression for a reviewed
 * canonical recipient. Resolution is append-only from the audit perspective:
 * it never clears an opt-out and never deletes the source quarantine row.
 */
export function resolveLegacyOptOutQuarantine(db, input, trustedNow = new Date().toISOString()) {
  const quarantineId = validateOptionalId(input?.quarantineId, 'quarantine ID');
  const resolutionId = validateToken(input?.resolutionId, 'resolution ID');
  const reviewerId = validateOptionalId(input?.reviewerPrincipalId, 'reviewer principal');
  const recipientE164 = validateRecipientE164(input?.recipientE164);
  const reason = validateOptOutReason(input?.reason || 'legacy_opt_out_reviewed');
  const approvedMapping = input?.approvedMapping === undefined || input.approvedMapping === null
    ? null : validateRecipientE164(input.approvedMapping);
  const now = validateLeaseTimestamp(trustedNow, 'resolution timestamp');
  if (!quarantineId || !reviewerId || input?.decision !== 'suppress') throw new Error('Invalid quarantine resolution.');
  db.exec('BEGIN IMMEDIATE');
  try {
    const row = db.prepare('SELECT * FROM whatsapp_legacy_opt_out_quarantine WHERE quarantine_id = ?').get(quarantineId);
    if (!row) throw batchError('Quarantine record was not found.', 'QUARANTINE_NOT_FOUND');
    if (row.status === 'resolved') {
      if (row.resolution_id === resolutionId && row.resolved_recipient_e164 === recipientE164) {
        db.exec('COMMIT');
        return { status: 'duplicate', resolutionId, quarantineId };
      }
      throw batchError('Quarantine resolution conflicts with an existing resolution.', 'QUARANTINE_RESOLUTION_CONFLICT');
    }
    const existingOptOut = db.prepare('SELECT opt_out_version FROM whatsapp_recipient_coordination WHERE recipient_e164 = ?').get(recipientE164);
    if (!existingOptOut) {
      db.prepare(`INSERT INTO whatsapp_recipient_coordination
        (recipient_e164, state, opt_out_version, created_at, updated_at)
        VALUES (?, 'clear', 0, ?, ?)`)
        .run(recipientE164, now, now);
    }
    const coordination = db.prepare('SELECT opt_out_version, state FROM whatsapp_recipient_coordination WHERE recipient_e164 = ?').get(recipientE164);
    if (coordination.state !== 'opted_out') {
      const requestId = `legacy-quarantine-${quarantineId}-${resolutionId}`;
      const nextVersion = Number(coordination.opt_out_version) + 1;
      db.prepare(`INSERT INTO whatsapp_opt_out_requests
        (request_id, recipient_e164, actor_principal_id, scope, source, reason, status, created_at, applied_at)
        VALUES (?, ?, ?, 'platform', 'flask', ?, 'applied', ?, ?)`)
        .run(requestId, recipientE164, reviewerId, reason, now, now);
      db.prepare(`UPDATE whatsapp_recipient_coordination
        SET state = 'opted_out', opt_out_version = ?, last_request_id = ?, opted_out_at = ?, updated_at = ?
        WHERE recipient_e164 = ?`)
        .run(nextVersion, requestId, now, now, recipientE164);
    }
    db.prepare(`UPDATE whatsapp_legacy_opt_out_quarantine
      SET status = 'resolved', resolution_id = ?, resolved_at = ?, resolved_by = ?,
          resolved_recipient_e164 = ?, resolution_reason = ?, approved_mapping = ?
      WHERE quarantine_id = ? AND status = 'unresolved'`)
      .run(resolutionId, now, reviewerId, recipientE164, reason, approvedMapping, quarantineId);
    db.exec('COMMIT');
    return { status: 'resolved', resolutionId, quarantineId };
  } catch (error) {
    rollback(db);
    throw error;
  }
}

function createDispatchCoordinationForBatch(db, input, now, expiresAt) {
  const unresolvedLegacy = db.prepare("SELECT COUNT(*) AS count FROM whatsapp_legacy_opt_out_quarantine WHERE status = 'unresolved'").get();
  if (Number(unresolvedLegacy?.count || 0) > 0) {
    throw batchError('Unresolved legacy opt-outs require review before dispatch.', 'LEGACY_OPTOUT_REVIEW_REQUIRED');
  }
  const items = db.prepare(`SELECT message_id, plan_recipient_id, recipient_jid FROM whatsapp_campaign_batch_items WHERE batch_id = ? ORDER BY position`).all(input.batchId);
  if (items.length < 1) throw batchError('Campaign batch has no items.', 'BATCH_ITEMS_INVALID');
  db.prepare(`INSERT INTO whatsapp_dispatch_operations
    (operation_id, authorization_id, batch_id, plan_id, sender_worker_id, request_fingerprint, state, expires_at, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, 'authorized', ?, ?, ?)`)
    .run(input.operationId, input.authorizationId, input.batchId, input.planId, input.senderWorkerId, input.requestFingerprint, expiresAt, now, now);
  for (const item of items) {
    const recipient = recipientE164FromJid(item.recipient_jid);
    const coordination = db.prepare('SELECT state FROM whatsapp_recipient_coordination WHERE recipient_e164 = ?').get(recipient);
    const leaseState = coordination?.state === 'opted_out' ? 'revoked' : 'authorized';
    db.prepare(`INSERT INTO whatsapp_dispatch_recipient_leases
      (operation_id, message_id, batch_id, plan_recipient_id, recipient_e164, state, lease_until, created_at, updated_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`)
      .run(input.operationId, item.message_id, input.batchId, item.plan_recipient_id, recipient, leaseState, expiresAt, now, now);
  }
}

export function beginRecipientSubmission(db, input, trustedNow = new Date().toISOString()) {
  const operationId = validateOperationId(input?.operationId);
  const messageId = validateToken(input?.messageId, 'message ID');
  const workerId = validateOptionalId(input?.senderWorkerId, 'sender worker ID');
  const attemptOperationId = validateOperationId(input?.attemptOperationId);
  const leaseUntil = validateLeaseTimestamp(input?.attemptLeaseUntil, 'attempt lease');
  const now = validateLeaseTimestamp(trustedNow, 'trusted timestamp');
  if (!workerId || Date.parse(leaseUntil) <= Date.parse(now)) throw new Error('Invalid submission lease.');
  db.exec('BEGIN IMMEDIATE');
  try {
    const row = db.prepare(`SELECT o.state AS operation_state, o.sender_worker_id, o.expires_at,
      l.state AS lease_state, l.lease_until, l.recipient_e164,
      m.state AS message_state
      FROM whatsapp_dispatch_operations o
      JOIN whatsapp_dispatch_recipient_leases l ON l.operation_id = o.operation_id AND l.message_id = ?
      JOIN outgoing_messages m ON m.message_id = l.message_id
      WHERE o.operation_id = ?`).get(messageId, operationId);
    if (!row || row.sender_worker_id !== workerId || !['authorized', 'submitting'].includes(row.operation_state) || row.message_state !== 'queued' || Date.parse(row.expires_at) <= Date.parse(now) || Date.parse(row.lease_until) <= Date.parse(now)) {
      throw batchError('Recipient submission is no longer authorized.', 'SUBMISSION_NOT_AUTHORIZED');
    }
    const coordination = db.prepare('SELECT state FROM whatsapp_recipient_coordination WHERE recipient_e164 = ?').get(row.recipient_e164);
    if (coordination?.state === 'opted_out') throw batchError('Recipient is opted out.', 'RECIPIENT_OPTED_OUT');
    if (row.lease_state !== 'authorized') throw batchError('Recipient submission is no longer authorized.', 'SUBMISSION_NOT_AUTHORIZED');
    const leaseUpdate = db.prepare(`UPDATE whatsapp_dispatch_recipient_leases SET state = 'submitting', lease_until = ?, updated_at = ? WHERE operation_id = ? AND message_id = ? AND state = 'authorized'`).run(leaseUntil, now, operationId, messageId);
    const messageUpdate = db.prepare(`UPDATE outgoing_messages SET state = 'attempted', attempt_count = attempt_count + 1, attempt_operation_id = ?, attempt_lease_until = ?, attempted_at = ?, updated_at = ? WHERE message_id = ? AND state = 'queued'`).run(attemptOperationId, leaseUntil, now, now, messageId);
    if (leaseUpdate.changes !== 1 || messageUpdate.changes !== 1) throw batchError('Recipient submission could not be fenced.', 'SUBMISSION_NOT_AUTHORIZED');
    db.prepare(`UPDATE whatsapp_dispatch_operations SET state = 'submitting', updated_at = ? WHERE operation_id = ? AND state = 'authorized'`).run(now, operationId);
    db.exec('COMMIT');
    return { operationId, messageId, recipientE164: row.recipient_e164, state: 'submitting' };
  } catch (error) { rollback(db); throw error; }
}

export function finishRecipientSubmission(db, input, trustedNow = new Date().toISOString()) {
  const operationId = validateOperationId(input?.operationId);
  const messageId = validateToken(input?.messageId, 'message ID');
  const state = input?.state;
  if (!['acknowledged', 'failed', 'uncertain'].includes(state)) throw new Error('Invalid recipient outcome.');
  const now = validateLeaseTimestamp(trustedNow, 'trusted timestamp');
  db.exec('BEGIN IMMEDIATE');
  try {
    const result = db.prepare(`UPDATE whatsapp_dispatch_recipient_leases SET state = ?, updated_at = ? WHERE operation_id = ? AND message_id = ? AND state = 'submitting'`).run(state, now, operationId, messageId);
    if (result.changes !== 1) throw batchError('Recipient submission ownership mismatch.', 'SUBMISSION_OWNERSHIP_MISMATCH');
    const remaining = db.prepare(`SELECT COUNT(*) AS count FROM whatsapp_dispatch_recipient_leases WHERE operation_id = ? AND state IN ('authorized', 'submitting', 'uncertain')`).get(operationId);
    const nextOperationState = remaining.count === 0 ? (state === 'uncertain' ? 'uncertain' : state) : 'submitting';
    db.prepare('UPDATE whatsapp_dispatch_operations SET state = ?, updated_at = ? WHERE operation_id = ?').run(nextOperationState, now, operationId);
    db.exec('COMMIT');
  } catch (error) { rollback(db); throw error; }
  return { operationId, messageId, state };
}

export function consumeDispatchAuthorization(db, input, trustedNow) {
  const authorizationId = validateToken(input?.authorizationId, 'authorization ID');
  const batchId = validateBatchIdentity(input?.batchId, 'batch ID');
  const operationId = validateOperationId(input?.operationId);
  const workerId = validateOptionalId(input?.senderWorkerId, 'sender worker ID');
  if (!workerId) throw new Error('Invalid sender worker ID.');
  const planId = validateOptionalId(input?.planId, 'campaign plan ID');
  if (!planId) throw new Error('Invalid campaign plan ID.');
  const requestFingerprint = validateBatchFingerprint(input?.requestFingerprint);
  const issuedAt = validateLeaseTimestamp(input?.issuedAt, 'authorization issue time');
  const expiresAt = validateLeaseTimestamp(input?.expiresAt, 'authorization expiration');
  const now = trustedNow ? validateLeaseTimestamp(trustedNow, 'trusted timestamp') : new Date().toISOString();
  const issuedMs = Date.parse(issuedAt);
  const expiresMs = Date.parse(expiresAt);
  const nowMs = Date.parse(now);
  if (expiresMs <= issuedMs || expiresMs - issuedMs > MAX_DISPATCH_AUTHORIZATION_LIFETIME_SECONDS * 1000) {
    throw batchError('Dispatch authorization lifetime is invalid.', 'AUTHORIZATION_LIFETIME_INVALID');
  }
  if (issuedMs > nowMs) throw batchError('Dispatch authorization is not yet valid.', 'AUTHORIZATION_NOT_YET_VALID');
  if (nowMs > expiresMs) throw batchError('Dispatch authorization has expired.', 'AUTHORIZATION_EXPIRED');

  db.exec('BEGIN IMMEDIATE');
  try {
    const existing = db.prepare(`
      SELECT authorization_id, batch_id, operation_id
      FROM whatsapp_campaign_dispatch_authorizations
      WHERE authorization_id = ? OR batch_id = ? OR operation_id = ?
    `).get(authorizationId, batchId, operationId);

    if (existing) {
      if (existing.authorization_id === authorizationId) {
        throw batchError('Dispatch authorization has already been consumed.', 'AUTHORIZATION_ALREADY_CONSUMED');
      }
      if (existing.batch_id === batchId) {
        throw batchError('Campaign batch has already been authorized for dispatch.', 'BATCH_ALREADY_AUTHORIZED');
      }
      if (existing.operation_id === operationId) {
        throw batchError('Dispatch operation has already been consumed.', 'OPERATION_ALREADY_CONSUMED');
      }
    }

    const batch = db.prepare(`
      SELECT batch_id, request_fingerprint, sender_worker_id, plan_id, state
      FROM whatsapp_campaign_batches
      WHERE batch_id = ?
    `).get(batchId);

    if (!batch) {
      throw batchError('Campaign batch not found.', 'BATCH_NOT_FOUND');
    }
    if (batch.state !== 'reserved') {
      throw batchError('Campaign batch is not in reserved state.', 'BATCH_NOT_RESERVED');
    }
    if (batch.sender_worker_id !== workerId) {
      throw batchError('Worker identity mismatch.', 'WORKER_MISMATCH');
    }
    if (batch.plan_id !== planId) {
      throw batchError('Campaign plan mismatch.', 'PLAN_MISMATCH');
    }
    if (batch.request_fingerprint !== requestFingerprint) {
      throw batchError('Batch request fingerprint mismatch.', 'FINGERPRINT_MISMATCH');
    }

    const nonQueued = db.prepare(`
      SELECT COUNT(*) AS count
      FROM outgoing_messages
      WHERE batch_id = ? AND state != 'queued'
    `).get(batchId);

    if (nonQueued && nonQueued.count > 0) {
      throw batchError('Campaign batch items are no longer queued.', 'BATCH_ITEMS_NOT_QUEUED');
    }

    db.prepare(`
      INSERT INTO whatsapp_campaign_dispatch_authorizations
        (authorization_id, batch_id, operation_id, sender_worker_id, plan_id, request_fingerprint, consumed_at, issued_at, expires_at, status)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'consumed')
    `).run(authorizationId, batchId, operationId, workerId, planId, requestFingerprint, now, issuedAt, expiresAt);

    createDispatchCoordinationForBatch(db, {
      authorizationId, batchId, operationId, senderWorkerId: workerId, planId, requestFingerprint,
    }, now, expiresAt);

    db.exec('COMMIT');
    return { consumed: true, authorizationId, batchId, operationId, consumedAt: now };
  } catch (error) {
    rollback(db);
    throw error;
  }
}

export function getDispatchAuthorization(db, batchId, senderWorkerId = null) {
  const id = validateBatchIdentity(batchId, 'batch ID');
  const workerId = senderWorkerId === null ? null : validateOptionalId(senderWorkerId, 'sender worker ID');
  const row = workerId
    ? db.prepare('SELECT * FROM whatsapp_campaign_dispatch_authorizations WHERE batch_id = ? AND sender_worker_id = ?').get(id, workerId)
    : db.prepare('SELECT * FROM whatsapp_campaign_dispatch_authorizations WHERE batch_id = ?').get(id);
  return row || null;
}

export function createLedgerRepository(db) {
  return {
    reserveAndEnqueue: input => reserveAndEnqueueOutgoingMessage(db, input),
    reserveCampaignBatch: input => reserveCampaignBatch(db, input),
    getCampaignBatch: (batchId, senderWorkerId) => getCampaignBatch(db, batchId, senderWorkerId),
    getCampaignBatchCancellationEvidence: (batchId, senderWorkerId) => getCampaignBatchCancellationEvidence(db, batchId, senderWorkerId),
    getDailyQuotaStatus: senderWorkerId => getDailyQuotaStatus(db, senderWorkerId),
    getByIdempotencyKey: key => getOutgoingMessageByIdempotencyKey(db, key),
    getOutgoingMessage: messageId => getOutgoingMessage(db, messageId),
    transition: (messageId, nextState, options) => transitionOutgoingMessage(db, messageId, nextState, options),
    consumeDispatchAuthorization: (input, trustedNow) => consumeDispatchAuthorization(db, input, trustedNow),
    getDispatchAuthorization: (batchId, senderWorkerId) => getDispatchAuthorization(db, batchId, senderWorkerId),
    registerRecipientOptOut: (input, trustedNow) => registerRecipientOptOut(db, input, trustedNow),
    resolveLegacyOptOutQuarantine: (input, trustedNow) => resolveLegacyOptOutQuarantine(db, input, trustedNow),
    beginRecipientSubmission: (input, trustedNow) => beginRecipientSubmission(db, input, trustedNow),
    finishRecipientSubmission: (input, trustedNow) => finishRecipientSubmission(db, input, trustedNow),
  };
}
