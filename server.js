import http from 'node:http';
import crypto from 'node:crypto';
import fsSync from 'node:fs';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { fileURLToPath } from 'node:url';
import { closeLedger, createLedgerRepository, initializeLedger } from './whatsapp_ledger.js';

const HOST = '127.0.0.1';
const DEFAULT_PORT = 8765;
const MAX_BODY_BYTES = 8 * 1024;
const MAX_BATCH_BODY_BYTES = 64 * 1024;
const MAX_CLOCK_SKEW_SECONDS = 60;
const QR_TTL_MS = 120000;
const MAX_REPLAY_CACHE_ENTRIES = 10000;
const REQUEST_ID_PATTERN = /^[A-Za-z0-9_-]{32,128}$/;
const DEFAULT_SEND_TIMEOUT_MS = 30000;
const MIN_SEND_TIMEOUT_MS = 1000;
const MAX_SEND_TIMEOUT_MS = 120000;
const SEND_LEASE_MARGIN_MS = 30000;
const DEFAULT_CTA_BUTTON_LABEL = 'Open Website';
const MAX_CTA_BUTTON_LABEL_LENGTH = 80;
const MAX_APPROVED_IMAGE_BYTES = 10 * 1024 * 1024;
const TASK_TEMPLATE_REFERENCE_PATTERN = /^(template|task):[A-Za-z0-9._:-]{1,100}$/;

function envFlag(name, fallback = false) {
  const value = process.env[name];
  return value === undefined ? fallback : ['1', 'true', 'yes'].includes(value.trim().toLowerCase());
}

function configFromEnv() {
  const port = Number.parseInt(process.env.TASKPE_WA_SERVICE_PORT || String(DEFAULT_PORT), 10);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('TASKPE_WA_SERVICE_PORT must be 1-65535.');
  const secret = (process.env.TASKPE_WA_SERVICE_SHARED_SECRET || '').trim();
  const enabled = envFlag('TASKPE_WHATSAPP_LINKED_DEVICE_ENABLED');
  if (enabled && secret.length < 32) throw new Error('TASKPE_WA_SERVICE_SHARED_SECRET must be at least 32 characters when enabled.');
  return {
    enabled,
    port,
    secret,
    sessionRoot: process.env.TASKPE_WA_SESSION_DIR || path.join(os.tmpdir(), 'taskpe-wa-sessions'),
    taskDatabasePath: process.env.TASKPE_DB_PATH || null,
    taskAssetRoot: process.env.TASKPE_TASK_ASSET_FOLDER || path.join(process.cwd(), 'uploads', 'task-assets'),
    ctaButtonLabel: process.env.TASKPE_WA_CTA_BUTTON_LABEL || 'Shop Now',
    batchHttpSendEnabled: envFlag('TASKPE_WA_CAMPAIGN_BATCH_HTTP_SEND_ENABLED', false),
    allowUnsupportedFilesystem: envFlag('TASKPE_WA_ALLOW_UNSUPPORTED_FILESYSTEM', false),
  };
}

export async function assertPrivateDirectory(directory, { create = false } = {}) {
  const absolute = path.resolve(directory);
  if (!path.isAbsolute(directory) || absolute !== directory) throw new Error('Session paths must be absolute.');
  let stat;
  try {
    stat = await fs.lstat(absolute);
  } catch (error) {
    if (!create || error.code !== 'ENOENT') throw error;
    await fs.mkdir(absolute, { recursive: false, mode: 0o700 });
    stat = await fs.lstat(absolute);
  }
  if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error('Session directory must not be a symlink.');
  if (process.platform !== 'win32' && (stat.mode & 0o077)) throw new Error('Session directory permissions are too broad.');
  if (typeof process.getuid === 'function' && stat.uid !== process.getuid()) throw new Error('Session directory owner is unsafe.');
  await fs.chmod(absolute, 0o700);
  const checked = await fs.lstat(absolute);
  if (!checked.isDirectory() || checked.isSymbolicLink()) throw new Error('Session directory changed unexpectedly.');
  if (process.platform !== 'win32' && (checked.mode & 0o077)) throw new Error('Session directory permissions are too broad.');
  if (typeof process.getuid === 'function' && checked.uid !== process.getuid()) throw new Error('Session directory owner is unsafe.');
  return absolute;
}

async function assertCleanupBoundary(root, directory) {
  const rootPath = await assertPrivateDirectory(root);
  const rootReal = await fs.realpath(rootPath);
  const directoryReal = await fs.realpath(directory);
  if (path.dirname(directoryReal) !== rootReal) throw new Error('Session cleanup escaped the configured root.');
  await assertPrivateDirectory(rootPath);
}

export async function assertWorkerSessionDirectory(root, directory) {
  const rootPath = await assertPrivateDirectory(root);
  const expectedRoot = path.resolve(rootPath);
  const workerPath = path.resolve(directory);
  if (path.dirname(workerPath) !== expectedRoot) throw new Error('Worker session path is outside the configured root.');
  const checkedWorker = await assertPrivateDirectory(workerPath);
  const rootReal = await fs.realpath(rootPath);
  const workerReal = await fs.realpath(checkedWorker);
  if (path.dirname(workerReal) !== rootReal) throw new Error('Worker session path escaped the configured root.');
  const finalStat = await fs.lstat(checkedWorker);
  if (!finalStat.isDirectory() || finalStat.isSymbolicLink()) throw new Error('Worker session directory changed unexpectedly.');
  if (process.platform !== 'win32' && (finalStat.mode & 0o077)) throw new Error('Worker session permissions are too broad.');
  if (typeof process.getuid === 'function' && finalStat.uid !== process.getuid()) throw new Error('Worker session owner is unsafe.');
  return checkedWorker;
}

export function parseConnectionPath(pathname) {
  const match = /^\/v1\/users\/([1-9][0-9]*)\/(qr|status|disconnect)$/.exec(pathname);
  return match ? { userId: match[1], action: match[2] } : null;
}

export function parseInternalPath(pathname) {
  const connection = parseConnectionPath(pathname);
  if (connection) return connection;
  let match = /^\/v1\/users\/([1-9][0-9]*)\/quota$/.exec(pathname);
  if (match) return { userId: match[1], action: 'quota' };
  match = /^\/v1\/users\/([1-9][0-9]*)\/campaign-batches\/reserve$/.exec(pathname);
  if (match) return { userId: match[1], action: 'batch_reserve' };
  match = /^\/v1\/users\/([1-9][0-9]*)\/coordination\/opt-outs$/.exec(pathname);
  if (match) return { userId: match[1], action: 'opt_out' };
  match = /^\/v1\/users\/([1-9][0-9]*)\/coordination\/legacy-opt-outs\/([1-9][0-9]*)\/resolve$/.exec(pathname);
  if (match) return { userId: match[1], action: 'legacy_opt_out_resolution', quarantineId: match[2] };
  match = /^\/v1\/users\/([1-9][0-9]*)\/campaign-batches\/([A-Za-z0-9][A-Za-z0-9._:-]{0,199})\/cancellation-status$/.exec(pathname);
  if (match) return { userId: match[1], action: 'batch_cancellation_evidence', batchId: match[2] };
  match = /^\/v1\/users\/([1-9][0-9]*)\/campaign-batches\/([A-Za-z0-9][A-Za-z0-9._:-]{0,199})\/send$/.exec(pathname);
  if (match) return { userId: match[1], action: 'batch_send', batchId: match[2] };
  match = /^\/v1\/users\/([1-9][0-9]*)\/campaign-batches\/([A-Za-z0-9][A-Za-z0-9._:-]{0,199})$/.exec(pathname);
  return match ? { userId: match[1], action: 'batch_lookup', batchId: match[2] } : null;
}

/**
 * Build validated Baileys message content without performing any I/O.
 * URLs remain plain text; link-preview generation is intentionally not done here.
 */
function validateHttpsUrl(value) {
  if (typeof value !== 'string' || value.length > 2048) throw new TypeError('A valid HTTPS destination URL is required.');
  let parsed;
  try { parsed = new URL(value); } catch { throw new TypeError('A valid HTTPS destination URL is required.'); }
  if (parsed.protocol !== 'https:' || parsed.username || parsed.password || parsed.hash) throw new TypeError('A valid HTTPS destination URL is required.');
  return parsed.toString();
}

function validateTrustedReadPath(value, label) {
  if (typeof value !== 'string' || !path.isAbsolute(value) || path.resolve(value) !== value) throw new Error(`${label} must be an absolute path.`);
  const stat = fsSync.lstatSync(value);
  if (!stat.isFile() || stat.isSymbolicLink()) throw new Error(`${label} is unsafe.`);
  return value;
}

function validateTrustedAssetRoot(value) {
  if (typeof value !== 'string' || !path.isAbsolute(value) || path.resolve(value) !== value) throw new Error('Task asset root must be an absolute path.');
  const stat = fsSync.lstatSync(value);
  if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error('Task asset root is unsafe.');
  return value;
}

function readApprovedTaskAsset(assetRoot, imagePath) {
  if (typeof imagePath !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$/.test(imagePath)) throw new Error('Approved task asset is invalid.');
  const root = path.resolve(assetRoot);
  const candidate = path.resolve(root, imagePath);
  if (path.dirname(candidate) !== root) throw new Error('Approved task asset is outside the configured root.');
  const realRoot = fsSync.realpathSync(root);
  let stat;
  try { stat = fsSync.lstatSync(candidate); } catch { throw new Error('Approved task asset is invalid.'); }
  if (stat.isSymbolicLink() || !stat.isFile() || stat.size < 1 || stat.size > MAX_APPROVED_IMAGE_BYTES) throw new Error('Approved task asset is invalid.');
  let realCandidate;
  try { realCandidate = fsSync.realpathSync(candidate); } catch { throw new Error('Approved task asset is invalid.'); }
  if (path.dirname(realCandidate) !== realRoot) throw new Error('Approved task asset escaped the configured root.');
  const contents = fsSync.readFileSync(realCandidate);
  if (!isSupportedImageContent(contents)) throw new Error('Approved task asset is invalid.');
  return contents;
}

function isSupportedImageContent(contents) {
  if (!Buffer.isBuffer(contents) || contents.length < 4) return false;

  // PNG: signature plus a complete IHDR chunk. This rejects truncated files
  // while keeping validation independent of an image-decoder dependency.
  const pngSignature = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  if (contents.length >= 33 && contents.subarray(0, 8).equals(pngSignature) && contents.readUInt32BE(8) === 13 && contents.subarray(12, 16).toString('ascii') === 'IHDR') return true;

  // JPEG: SOI and EOI markers. A complete JPEG decoder is not installed in
  // this service, so this is a conservative container/signature check.
  if (contents.length >= 4 && contents[0] === 0xff && contents[1] === 0xd8 && contents[contents.length - 2] === 0xff && contents[contents.length - 1] === 0xd9) return true;

  // WebP: RIFF/WEBP header with a declared size that fits in the file.
  if (contents.length >= 12 && contents.subarray(0, 4).toString('ascii') === 'RIFF' && contents.subarray(8, 12).toString('ascii') === 'WEBP') {
    return contents.readUInt32LE(4) + 8 <= contents.length;
  }
  return false;
}

/**
 * Read-only resolver for the persisted TaskPe task record. The resolver is
 * constructed from server configuration, never from request data. Only the
 * canonical plan reference is accepted; arbitrary SQL, paths, URLs, and
 * message bodies supplied by a browser are not accepted.
 */
export function createTaskPeCampaignContentResolver({ databasePath, assetRoot, buttonLabel = DEFAULT_CTA_BUTTON_LABEL } = {}) {
  const trustedDatabasePath = validateTrustedReadPath(databasePath, 'Task database path');
  const trustedAssetRoot = validateTrustedAssetRoot(assetRoot);
  if (typeof buttonLabel !== 'string' || buttonLabel.trim() === '' || buttonLabel.length > MAX_CTA_BUTTON_LABEL_LENGTH) throw new Error('Invalid CTA button label.');
  return async ({ batch, item }) => {
    if (!batch || !item || typeof item.payload_reference !== 'string' || !TASK_TEMPLATE_REFERENCE_PATTERN.test(item.payload_reference) || !Number.isSafeInteger(batch.task_id) || batch.task_id < 1 || !Number.isSafeInteger(batch.campaign_id) || batch.campaign_id < 1) throw new Error('Unsupported campaign content reference.');
    let db;
    try {
      db = new DatabaseSync(trustedDatabasePath, { readOnly: true });
      const task = db.prepare(`
        SELECT id, campaign_id, task_type, template_text, image_path, destination_link
        FROM tasks WHERE id = ? AND campaign_id = ?
      `).get(batch.task_id, batch.campaign_id);
      if (!task || typeof task.template_text !== 'string' || task.template_text.trim() === '') throw new Error('Task content is unavailable.');
      const ctaUrl = task.destination_link ? validateHttpsUrl(task.destination_link) : undefined;
      if (item.content_type === 'text') {
        if (task.task_type !== 'TEXT_LINK' || task.image_path) throw new Error('Approved text campaign content is unavailable.');
        return { type: 'text', text: task.template_text, payloadReference: item.payload_reference, ...(ctaUrl ? { ctaUrl, buttonLabel } : {}) };
      }
      if (item.content_type !== 'image' || task.task_type !== 'IMAGE_TEXT_LINK' || !task.image_path || !ctaUrl) throw new Error('Approved image campaign content is unavailable.');
      return { type: 'image', media: readApprovedTaskAsset(trustedAssetRoot, task.image_path), caption: task.template_text, ctaUrl, buttonLabel, payloadReference: item.payload_reference };
    } finally {
      if (db) db.close();
    }
  };
}

function ctaButton(url, buttonLabel = DEFAULT_CTA_BUTTON_LABEL) {
  if (typeof buttonLabel !== 'string' || buttonLabel.trim() === '' || buttonLabel.length > MAX_CTA_BUTTON_LABEL_LENGTH) {
    throw new TypeError('A valid CTA button label is required.');
  }
  const normalizedUrl = validateHttpsUrl(url);
  return {
    name: 'cta_url',
    buttonParamsJson: JSON.stringify({ display_text: buttonLabel, url: normalizedUrl, merchant_url: normalizedUrl }),
  };
}

export function buildMessageContent(type, { text, caption, media, ctaUrl, buttonLabel = DEFAULT_CTA_BUTTON_LABEL, preparedImageMessage } = {}) {
  if (type === 'text') {
    if (typeof text !== 'string' || text.trim() === '') throw new TypeError('Message text is required.');
    if (ctaUrl !== undefined && ctaUrl !== null) {
      return {
        interactiveMessage: {
          body: { text },
          nativeFlowMessage: { buttons: [ctaButton(ctaUrl, buttonLabel)] },
        },
      };
    }
    return { text };
  }

  if (type !== 'image' && type !== 'video') throw new TypeError('Unsupported message type.');
  if (typeof caption !== 'string' || caption.trim() === '') throw new TypeError('Media caption is required.');
  if (!Buffer.isBuffer(media) || media.length === 0) throw new TypeError('Media buffer is required.');

  if (ctaUrl !== undefined && ctaUrl !== null) {
    if (type !== 'image') throw new TypeError('CTA buttons are supported for text and image messages only.');
    if (!preparedImageMessage || typeof preparedImageMessage !== 'object' || Array.isArray(preparedImageMessage)) {
      throw new TypeError('A prepared image message is required for an image CTA.');
    }
    return {
      interactiveMessage: {
        header: { hasMediaAttachment: true, imageMessage: preparedImageMessage },
        body: { text: caption },
        nativeFlowMessage: { buttons: [ctaButton(ctaUrl, buttonLabel)] },
      },
    };
  }

  return type === 'image'
    ? { image: media, caption }
    : { video: media, caption, ptv: false };
}

export function isSupportedRecipientJid(jid) {
  return typeof jid === 'string' && /^[1-9][0-9]{4,14}@s\.whatsapp\.net$/.test(jid);
}

export function getSendConfig() {
  const raw = process.env.TASKPE_WA_SEND_TIMEOUT_MS;
  const sendTimeoutMs = raw === undefined || raw === '' ? DEFAULT_SEND_TIMEOUT_MS : Number(raw);
  if (!Number.isSafeInteger(sendTimeoutMs) || sendTimeoutMs < MIN_SEND_TIMEOUT_MS || sendTimeoutMs > MAX_SEND_TIMEOUT_MS) {
    throw new Error('Invalid WhatsApp send timeout configuration.');
  }
  return Object.freeze({ sendTimeoutMs, leaseDurationMs: sendTimeoutMs + SEND_LEASE_MARGIN_MS });
}

/**
 * Send one already-validated message through an injected socket.
 * A resolved sendMessage call is acknowledged by the socket only; it is not delivery confirmation.
 */
async function sendMessageWithSocketUsingConfig(socket, jid, messageInput, sendConfig) {
  if (!isSupportedRecipientJid(jid)) {
    return { accepted: false, status: 'validation_error', message: 'Invalid recipient.' };
  }

  let payload;
  try {
    payload = buildMessageContent(messageInput?.type, messageInput);
  } catch {
    return { accepted: false, status: 'validation_error', message: 'Invalid message content.' };
  }

  if (!socket || (typeof socket.sendMessage !== 'function' && typeof socket.relayMessage !== 'function')) {
    return { accepted: false, status: 'transport_error', message: 'Message transport unavailable.' };
  }

  const { sendTimeoutMs } = sendConfig;
  let timer;
  try {
    const sendOperation = payload.interactiveMessage && typeof socket.relayMessage === 'function'
      ? socket.relayMessage(jid, payload, { messageId: crypto.randomUUID() })
      : socket.sendMessage(jid, payload);
    await Promise.race([
      sendOperation,
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('SEND_TIMEOUT')), sendTimeoutMs); }),
    ]);
    return { accepted: true, status: 'acknowledged', delivered: false };
  } catch (error) {
    if (error?.message === 'SEND_TIMEOUT') return { accepted: false, status: 'uncertain', message: 'Message send outcome is unknown.' };
    return { accepted: false, status: 'transport_error', message: 'Message send failed.' };
  } finally {
    if (timer) clearTimeout(timer);
  }
}

function sanitizedBatchSendItem(item, result) {
  return {
    position: item.position,
    message_id: item.message_id,
    status: result.status,
    delivered: false,
  };
}

/**
 * Send one already-reserved campaign item. Content comes only from the trusted
 * resolver supplied by the server owner; the HTTP request cannot supply it.
 */
export async function sendReservedCampaignItem({ ledger, socket, batch, item, resolveContent, prepareImageMessage = null, dispatchOperationId = null }) {
  if (!ledger || typeof ledger.getOutgoingMessage !== 'function' || typeof ledger.transition !== 'function') {
    return { accepted: false, status: 'ledger_error', message: 'Campaign sending is unavailable.', delivered: false };
  }
  if (!batch || batch.state !== 'reserved' || !item || item.state !== 'reserved' || item.batch_id !== batch.batch_id) {
    return { accepted: false, status: 'invalid_reservation', message: 'Campaign reservation is invalid.', delivered: false };
  }
  const record = ledger.getOutgoingMessage(item.message_id);
  if (!record || record.batch_id !== batch.batch_id || record.message_id !== item.message_id || record.idempotency_key !== item.idempotency_key || record.sender_worker_id !== batch.sender_worker_id || record.recipient_jid !== item.recipient_jid || record.content_type !== item.content_type || record.payload_reference !== item.payload_reference) {
    return { accepted: false, status: 'invalid_reservation', message: 'Campaign reservation is invalid.', delivered: false };
  }
  if (record.state !== 'queued') return { accepted: false, status: 'not_sendable', message: 'Campaign item is no longer queued.', message_id: item.message_id, delivered: false };
  if (typeof resolveContent !== 'function') return { accepted: false, status: 'content_unavailable', message: 'Campaign content is unavailable.', message_id: item.message_id, delivered: false };

  let messageInput;
  try {
    messageInput = await resolveContent({ batch, item });
    if (!messageInput || messageInput.type !== item.content_type || messageInput.payloadReference !== item.payload_reference) throw new Error('Content identity mismatch.');
    if (messageInput.type === 'image' && messageInput.ctaUrl && !messageInput.preparedImageMessage) {
      if (typeof prepareImageMessage !== 'function') throw new Error('Prepared image content is unavailable.');
      messageInput = { ...messageInput, preparedImageMessage: await prepareImageMessage(messageInput.media, { batch, item, socket }) };
    }
    buildMessageContent(messageInput.type, messageInput);
  } catch {
    return { accepted: false, status: 'content_invalid', message: 'Campaign content is invalid.', message_id: item.message_id, delivered: false };
  }

  let sendConfig;
  try { sendConfig = getSendConfig(); } catch { return { accepted: false, status: 'configuration_error', message: 'Message sending is unavailable.', message_id: item.message_id, delivered: false }; }
  const attemptOperationId = crypto.randomBytes(24).toString('base64url');
  const attemptLeaseUntil = new Date(Date.now() + sendConfig.leaseDurationMs).toISOString();
  try {
    if (typeof ledger.beginRecipientSubmission !== 'function') return { accepted: false, status: 'coordination_unavailable', message: 'Campaign coordination is unavailable.', message_id: item.message_id, delivered: false };
    if (!dispatchOperationId) return { accepted: false, status: 'coordination_unavailable', message: 'Campaign coordination is unavailable.', message_id: item.message_id, delivered: false };
    ledger.beginRecipientSubmission({ operationId: dispatchOperationId, messageId: item.message_id, senderWorkerId: batch.sender_worker_id, attemptOperationId, attemptLeaseUntil });
  } catch (error) {
    if (error?.code === 'RECIPIENT_OPTED_OUT') return { accepted: false, status: 'opted_out', message: 'Recipient is not eligible for sending.', message_id: item.message_id, delivered: false };
    return { accepted: false, status: 'ledger_error', message: 'Unable to start campaign send.', message_id: item.message_id, delivered: false };
  }
  const adapterResult = await sendMessageWithSocketUsingConfig(socket, item.recipient_jid, messageInput, sendConfig);
  if (adapterResult.accepted === true && adapterResult.status === 'acknowledged') {
    try { ledger.transition(item.message_id, 'acknowledged', { attemptOperationId }); if (dispatchOperationId && typeof ledger.finishRecipientSubmission === 'function') ledger.finishRecipientSubmission({ operationId: dispatchOperationId, messageId: item.message_id, state: 'acknowledged' }); }
    catch { return { accepted: false, status: 'ledger_error', message: 'Unable to record campaign send.', message_id: item.message_id, delivered: false }; }
    return { accepted: true, status: 'acknowledged', message_id: item.message_id, delivered: false };
  }
  if (adapterResult.status === 'uncertain') {
    try { ledger.transition(item.message_id, 'delivered_unknown', { attemptOperationId }); if (dispatchOperationId && typeof ledger.finishRecipientSubmission === 'function') ledger.finishRecipientSubmission({ operationId: dispatchOperationId, messageId: item.message_id, state: 'uncertain' }); }
    catch { return { accepted: false, status: 'ledger_error', message: 'Unable to record campaign send outcome.', message_id: item.message_id, delivered: false }; }
    return { accepted: false, status: 'uncertain', message: 'Message send outcome is unknown.', message_id: item.message_id, delivered: false };
  }
  try { ledger.transition(item.message_id, 'failed', { attemptOperationId, failureCode: adapterResult.status === 'validation_error' ? 'VALIDATION_ERROR' : 'TRANSPORT_ERROR' }); if (dispatchOperationId && typeof ledger.finishRecipientSubmission === 'function') ledger.finishRecipientSubmission({ operationId: dispatchOperationId, messageId: item.message_id, state: 'failed' }); }
  catch { return { accepted: false, status: 'ledger_error', message: 'Unable to record campaign send failure.', message_id: item.message_id, delivered: false }; }
  return { accepted: false, status: 'failed', message: 'Message send failed.', message_id: item.message_id, delivered: false };
}

export async function sendReservedCampaignBatch({ ledger, socket, senderWorkerId, batchId, resolveContent, prepareImageMessage = null, dispatchOperationId = null }) {
  if (!ledger || typeof ledger.getCampaignBatch !== 'function') return { success: false, status: 'ledger_error', message: 'Campaign sending is unavailable.' };
  const batch = ledger.getCampaignBatch(batchId, senderWorkerId);
  if (!batch) return { success: false, status: 'not_found', message: 'Campaign reservation was not found.' };
  const items = [];
  for (const item of batch.items) items.push(sanitizedBatchSendItem(item, await sendReservedCampaignItem({ ledger, socket, batch: batch.batch, item, resolveContent, prepareImageMessage, dispatchOperationId })));
  return { success: true, batch_id: batch.batch.batch_id, state: batch.batch.state, items };
}

function sanitizedDuplicateResult(record) {
  return {
    accepted: false,
    status: 'duplicate',
    message_id: record.message_id,
    state: record.state,
    delivered: false,
  };
}

function ledgerFailureResult(messageId = null) {
  return {
    accepted: false,
    status: 'ledger_error',
    message: 'Unable to record outgoing message.',
    ...(messageId ? { message_id: messageId } : {}),
    delivered: false,
  };
}

function quotaExceededResult() {
  return {
    accepted: false,
    status: 'quota_exceeded',
    message: 'Daily message quota exceeded.',
    delivered: false,
  };
}

/**
 * Coordinate one non-retrying send attempt with the durable ledger.
 * The caller supplies a ledger repository and socket; this function is not an HTTP handler.
 */
function safeUserDirectory(root, userId) {
  if (!/^[1-9][0-9]*$/.test(String(userId))) throw new Error('Invalid user ID.');
  const directory = path.resolve(root, `user-${userId}`);
  const rootPath = path.resolve(root);
  if (path.dirname(directory) !== rootPath) throw new Error('Invalid session path.');
  return directory;
}

export function verifyInternalRequest({ method, pathname, body, headers, secret, now = Date.now() }) {
  if (!secret || typeof secret !== 'string') return false;
  const timestamp = String(headers['x-taskpe-timestamp'] || '');
  const requestId = String(headers['x-taskpe-request-id'] || '');
  const signature = String(headers['x-taskpe-signature'] || '');
  const userHeader = String(headers['x-taskpe-user-id'] || '');
  const parsed = parseInternalPath(pathname);
  if (!parsed || userHeader !== parsed.userId || !/^[0-9]{10,}$/.test(timestamp) || !REQUEST_ID_PATTERN.test(requestId)) return false;
  if (Math.abs(Math.floor(now / 1000) - Number(timestamp)) > MAX_CLOCK_SKEW_SECONDS) return false;
  if (!/^[a-f0-9]{64}$/.test(signature)) return false;
  const expected = crypto.createHmac('sha256', secret)
    .update(`${timestamp}.${requestId}.${method.toUpperCase()}.${pathname}.${body || ''}`, 'utf8').digest('hex');
  return crypto.timingSafeEqual(Buffer.from(signature, 'hex'), Buffer.from(expected, 'hex'));
}

export function deriveDispatchAuthorizationKey(secret) {
  if (typeof secret !== 'string' || !secret) throw new Error('A valid secret is required.');
  return crypto.createHmac('sha256', secret).update('taskpe-wa-dispatch-authorization-v1', 'utf8').digest();
}

export function verifyDispatchAuthorizationToken(token, secret, { now = Date.now(), maxSkewSeconds = 5 } = {}) {
  if (typeof token !== 'string') return { valid: false, statusCode: 401, message: 'Invalid dispatch authorization token format.' };
  const parts = token.split('.');
  if (parts.length !== 3 || parts[0] !== 'v1') {
    return { valid: false, statusCode: 401, message: 'Invalid dispatch authorization token format.' };
  }
  const [version, b64Payload, signature] = parts;
  if (!/^[A-Za-z0-9_-]+$/.test(b64Payload) || !/^[a-f0-9]{64}$/.test(signature)) {
    return { valid: false, statusCode: 401, message: 'Invalid dispatch authorization token format.' };
  }
  let derivedKey;
  try {
    derivedKey = deriveDispatchAuthorizationKey(secret);
  } catch {
    return { valid: false, statusCode: 503, message: 'Authorization verification unavailable.' };
  }
  const canonicalData = Buffer.from(`${version}.${b64Payload}`, 'utf8');
  const expectedSignature = crypto.createHmac('sha256', derivedKey).update(canonicalData).digest('hex');
  if (!crypto.timingSafeEqual(Buffer.from(signature, 'hex'), Buffer.from(expectedSignature, 'hex'))) {
    return { valid: false, statusCode: 401, message: 'Invalid dispatch authorization signature.' };
  }
  let payload;
  try {
    const jsonStr = Buffer.from(b64Payload, 'base64url').toString('utf8');
    payload = JSON.parse(jsonStr);
  } catch {
    return { valid: false, statusCode: 400, message: 'Malformed dispatch authorization payload.' };
  }
  if (!payload || typeof payload !== 'object' || payload.v !== 1) {
    return { valid: false, statusCode: 400, message: 'Invalid dispatch authorization payload.' };
  }
  const { auth_id, worker_id, plan_id, batch_id, operation_id, request_fingerprint, issued_at, expires_at } = payload;
  if (typeof auth_id !== 'string' || !auth_id ||
      !Number.isSafeInteger(worker_id) || worker_id < 1 ||
      !Number.isSafeInteger(plan_id) || plan_id < 1 ||
      typeof batch_id !== 'string' || !batch_id ||
      typeof operation_id !== 'string' || !operation_id ||
      typeof request_fingerprint !== 'string' || !/^[a-f0-9]{64}$/.test(request_fingerprint) ||
      !Number.isSafeInteger(issued_at) ||
      !Number.isSafeInteger(expires_at)) {
    return { valid: false, statusCode: 400, message: 'Invalid dispatch authorization payload claims.' };
  }
  const nowSeconds = Math.floor(now / 1000);
  if (issued_at > nowSeconds + maxSkewSeconds) {
    return { valid: false, statusCode: 401, message: 'Dispatch authorization token is not yet valid.' };
  }
  if (nowSeconds > expires_at + maxSkewSeconds) {
    return { valid: false, statusCode: 401, message: 'Dispatch authorization token has expired.' };
  }
  if (expires_at <= issued_at || expires_at - issued_at > 120) {
    return { valid: false, statusCode: 401, message: 'Dispatch authorization lifetime is invalid.' };
  }
  return { valid: true, payload };
}

export class ReplayCache {
  constructor({ maxEntries = MAX_REPLAY_CACHE_ENTRIES, now = () => Date.now() } = {}) {
    this.maxEntries = maxEntries;
    this.now = now;
    this.entries = new Map();
  }

  accept(requestId, timestampSeconds) {
    const now = this.now();
    for (const [key, expiresAt] of this.entries) if (expiresAt <= now) this.entries.delete(key);
    if (this.entries.has(requestId) || this.entries.size >= this.maxEntries) return false;
    this.entries.set(requestId, (Number(timestampSeconds) * 1000) + ((MAX_CLOCK_SKEW_SECONDS + 1) * 1000));
    return true;
  }
}

export async function removeUserSession(root, userId) {
  const directory = safeUserDirectory(root, userId);
  const stat = await fs.lstat(directory).catch(error => error.code === 'ENOENT' ? null : Promise.reject(error));
  if (stat) await assertCleanupBoundary(root, directory);
  if (stat && (stat.isSymbolicLink() || !stat.isDirectory())) throw new Error('Refusing to remove unsafe session path.');
  if (stat) {
    const finalStat = await fs.lstat(directory);
    if (finalStat.isSymbolicLink() || !finalStat.isDirectory()) throw new Error('Session path changed during cleanup.');
  }
  await fs.rm(directory, { recursive: true, force: true });
}

function jsonResponse(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Content-Length': Buffer.byteLength(body), 'Cache-Control': 'no-store' });
  res.end(body);
}

async function readBody(req, maxBytes = MAX_BODY_BYTES) {
  let size = 0;
  const chunks = [];
  for await (const chunk of req) {
    size += chunk.length;
    if (size > maxBytes) throw Object.assign(new Error('Request body too large.'), { statusCode: 413 });
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString('utf8');
}

function sanitizedBatchResult(result) {
  if (!result || !result.batch) return null;
  return {
    batch_id: result.batch.batch_id,
    request_fingerprint: result.batch.request_fingerprint,
    sender_worker_id: result.batch.sender_worker_id,
    campaign_id: result.batch.campaign_id,
    task_id: result.batch.task_id,
    plan_id: result.batch.plan_id,
    item_count: result.batch.item_count,
    quota_day: result.batch.quota_day,
    state: result.batch.state,
    created_at: result.batch.created_at,
    updated_at: result.batch.updated_at,
    items: (result.items || []).map(item => ({
      position: item.position,
      plan_id: item.plan_id,
      plan_recipient_id: item.plan_recipient_id,
      idempotency_key: item.idempotency_key,
      message_id: item.message_id,
      recipient_jid: item.recipient_jid,
      content_type: item.content_type,
      payload_reference: item.payload_reference,
      state: item.state,
      created_at: item.created_at,
      updated_at: item.updated_at,
    })),
  };
}

export function createService({ enabled, secret, sessionRoot, ledgerRepository = null, campaignContentResolver = null, taskDatabasePath = null, taskAssetRoot = null, ctaButtonLabel = null, loadBaileys = true, baileysLoader = null, pinoLoader = null, allowUnsupportedFilesystem = false, batchHttpSendEnabled = undefined } = {}) {
  const environmentConfig = configFromEnv();
  const config = {
    ...environmentConfig,
    ...(enabled === undefined ? {} : { enabled }),
    ...(secret === undefined ? {} : { secret }),
    ...(sessionRoot === undefined ? {} : { sessionRoot }),
    ...(batchHttpSendEnabled === undefined ? {} : { batchHttpSendEnabled }),
  };
  const effectiveAllowUnsupportedFilesystem = allowUnsupportedFilesystem || config.allowUnsupportedFilesystem;
  if (config.enabled && (!config.secret || config.secret.length < 32)) throw new Error('A strong service secret is required when enabled.');
  if (config.enabled && process.platform === 'win32' && !effectiveAllowUnsupportedFilesystem) throw new Error('Enabled QR pairing requires POSIX filesystem ownership and permission checks.');
  const connections = new Map();
  const operations = new Map();
  const replayCache = new ReplayCache();
  const effectiveButtonLabel = ctaButtonLabel || config.ctaButtonLabel || DEFAULT_CTA_BUTTON_LABEL;
  const trustedCampaignContentResolver = campaignContentResolver || (config.enabled && taskDatabasePath && taskAssetRoot
    ? createTaskPeCampaignContentResolver({ databasePath: taskDatabasePath, assetRoot: taskAssetRoot, buttonLabel: effectiveButtonLabel })
    : null);
  let baileys;
  let shuttingDown = false;

  async function withUserLock(userId, operation) {
    const previous = operations.get(userId) || Promise.resolve();
    let release;
    const barrier = new Promise(resolve => { release = resolve; });
    operations.set(userId, barrier);
    try {
      await previous;
      return await operation();
    } finally {
      release();
      if (operations.get(userId) === barrier) operations.delete(userId);
    }
  }

  async function getBaileys() {
    if (!loadBaileys) throw new Error('Baileys loading disabled for this test.');
    if (baileysLoader) return baileysLoader();
    baileys ||= await import('@whiskeysockets/baileys');
    return baileys;
  }

  async function getLogger() {
    if (pinoLoader) return pinoLoader();
    return (await import('pino')).default({ level: 'silent' });
  }

  async function prepareImageMessage(media, { socket }) {
    const lib = await getBaileys();
    if (typeof lib.prepareWAMessageMedia !== 'function' || typeof socket?.waUploadToServer !== 'function') throw new Error('Image preparation is unavailable.');
    const prepared = await lib.prepareWAMessageMedia({ image: media }, { upload: socket.waUploadToServer.bind(socket) });
    if (!prepared?.imageMessage || typeof prepared.imageMessage !== 'object') throw new Error('Image preparation failed.');
    return prepared.imageMessage;
  }

  async function ensureConnection(userId) {
    let state = connections.get(userId);
    if (state?.socket) return state;
    if (shuttingDown || state?.cancelled) return null;
    const lib = await getBaileys();
    if (shuttingDown || state?.cancelled || connections.get(userId) !== state) return null;
    await assertPrivateDirectory(config.sessionRoot);
    const directory = safeUserDirectory(config.sessionRoot, userId);
    let userStat;
    try {
      userStat = await fs.lstat(directory);
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
      await fs.mkdir(directory, { recursive: false, mode: 0o700 });
      userStat = await fs.lstat(directory);
    }
    if (userStat.isSymbolicLink() || !userStat.isDirectory()) throw new Error('Worker session directory is unsafe.');
    if (process.platform !== 'win32' && (userStat.mode & 0o077)) throw new Error('Worker session permissions are too broad.');
    if (typeof process.getuid === 'function' && userStat.uid !== process.getuid()) throw new Error('Worker session owner is unsafe.');
    await fs.chmod(directory, 0o700);
    await assertWorkerSessionDirectory(config.sessionRoot, directory);
    const auth = await lib.useMultiFileAuthState(directory);
    if (shuttingDown || state?.cancelled || connections.get(userId) !== state) return null;
    state = state || { generation: 0, cancelled: false };
    state.generation += 1;
    state.qr = null; state.qrExpiresAt = 0; state.status = 'connecting'; state.reconnectTimer = null; state.reconnects = state.reconnects || 0;
    connections.set(userId, state);
    const logger = await getLogger();
    const socket = lib.default({ auth: auth.state, printQRInTerminal: false, logger });
    state.socket = socket;
    socket.ev.on('creds.update', async creds => {
      if (connections.get(userId) !== state || state.cancelled) return;
      try {
        await auth.saveCreds(creds);
      } catch (error) {
        if (connections.get(userId) !== state || state.cancelled) return;
        state.cancelled = true;
        state.generation += 1;
        state.status = 'error';
        state.persistenceFailed = true;
        logger.error({ error: error?.name || 'CredentialPersistenceError' }, 'WhatsApp credential persistence failed; connection disabled.');
        try { socket.ws?.close(); } catch {}
      }
    });
    socket.ev.on('connection.update', ({ connection, lastDisconnect, qr }) => {
      if (connections.get(userId) !== state || state.cancelled) return;
      if (qr) { state.qr = qr; state.qrExpiresAt = Date.now() + QR_TTL_MS; state.status = 'qr'; }
      if (connection === 'open') { state.status = 'connected'; state.qr = null; state.qrExpiresAt = 0; state.reconnects = 0; }
      if (connection === 'close' && !shuttingDown) {
        state.socket = null; state.status = 'disconnected';
        const code = lastDisconnect?.error?.output?.statusCode;
        if (code !== 401 && state.reconnects < 5) {
          const delay = Math.min(5000, 1000 * 2 ** state.reconnects++);
          const generation = state.generation;
          state.reconnectTimer = setTimeout(() => {
            state.reconnectTimer = null;
            withUserLock(userId, async () => {
              if (shuttingDown || state.cancelled || state.generation !== generation || connections.get(userId) !== state) return;
              await ensureConnection(userId);
            }).catch(() => { if (!state.cancelled) state.status = 'error'; });
          }, delay);
        }
      }
    });
    return state;
  }

  async function handle(req, res) {
    const parsed = parseInternalPath(new URL(req.url, `http://${HOST}`).pathname);
    if (!parsed) return jsonResponse(res, 404, { success: false, message: 'Not found.' });
    if (!config.enabled && !['opt_out', 'legacy_opt_out_resolution'].includes(parsed.action)) return jsonResponse(res, 404, { success: false, message: 'Linked-device integration is disabled.' });
    const expectedMethod = ['status', 'quota', 'batch_lookup', 'batch_cancellation_evidence'].includes(parsed.action) ? 'GET' : 'POST';
    if (req.method !== expectedMethod) return jsonResponse(res, 405, { success: false, message: 'Method not allowed.' });
    let body;
    try { body = await readBody(req, parsed.action === 'batch_reserve' ? MAX_BATCH_BODY_BYTES : MAX_BODY_BYTES); } catch (error) { return jsonResponse(res, error.statusCode || 400, { success: false, message: error.message }); }
    const pathname = new URL(req.url, `http://${HOST}`).pathname;
    if (!verifyInternalRequest({ method: req.method, pathname, body, headers: req.headers, secret: config.secret })) {
      return jsonResponse(res, 401, { success: false, message: 'Invalid service credentials.' });
    }
    if (!replayCache.accept(String(req.headers['x-taskpe-request-id']), String(req.headers['x-taskpe-timestamp']))) {
      return jsonResponse(res, 401, { success: false, message: 'Request expired or already used.' });
    }
    try {
      if (parsed.action === 'opt_out') {
        if (!ledgerRepository || typeof ledgerRepository.registerRecipientOptOut !== 'function') {
          return jsonResponse(res, 503, { success: false, message: 'Opt-out coordination is unavailable.' });
        }
        let input;
        try { input = body ? JSON.parse(body) : null; } catch { return jsonResponse(res, 400, { success: false, message: 'Invalid opt-out request.' }); }
        if (!input || String(input.actorPrincipalId) !== parsed.userId || input.scope !== 'platform') {
          return jsonResponse(res, 403, { success: false, message: 'Opt-out actor mismatch.' });
        }
        try {
          const result = ledgerRepository.registerRecipientOptOut({
            requestId: input.requestId,
            recipientE164: input.recipientE164,
            actorPrincipalId: Number(parsed.userId),
            scope: 'platform',
            reason: input.reason,
          });
          return jsonResponse(res, 200, { success: true, status: result.status, request_id: result.requestId, version: result.version });
        } catch (error) {
          if (error?.code === 'OPTOUT_REQUEST_CONFLICT') return jsonResponse(res, 409, { success: false, status: 'conflict', message: 'Opt-out request conflicts with an existing request.' });
          return jsonResponse(res, 400, { success: false, message: 'Invalid opt-out request.' });
        }
      }
      if (parsed.action === 'legacy_opt_out_resolution') {
        if (!ledgerRepository || typeof ledgerRepository.resolveLegacyOptOutQuarantine !== 'function') {
          return jsonResponse(res, 503, { success: false, message: 'Opt-out reconciliation is unavailable.' });
        }
        let input;
        try { input = body ? JSON.parse(body) : null; } catch { return jsonResponse(res, 400, { success: false, message: 'Invalid quarantine resolution.' }); }
        if (!input || String(input.reviewerPrincipalId) !== parsed.userId || Number(input.quarantineId) !== Number(parsed.quarantineId) || input.decision !== 'suppress') {
          return jsonResponse(res, 403, { success: false, message: 'Quarantine resolution actor mismatch.' });
        }
        try {
          const result = ledgerRepository.resolveLegacyOptOutQuarantine({
            ...input,
            quarantineId: Number(parsed.quarantineId),
            reviewerPrincipalId: Number(parsed.userId),
          });
          return jsonResponse(res, 200, { success: true, status: result.status, resolution_id: result.resolutionId, quarantine_id: result.quarantineId });
        } catch (error) {
          if (error?.code === 'QUARANTINE_NOT_FOUND') return jsonResponse(res, 404, { success: false, message: 'Quarantine record was not found.' });
          if (error?.code === 'QUARANTINE_RESOLUTION_CONFLICT') return jsonResponse(res, 409, { success: false, status: 'conflict', message: 'Quarantine resolution conflicts with an existing resolution.' });
          return jsonResponse(res, 400, { success: false, message: 'Invalid quarantine resolution.' });
        }
      }
      if (parsed.action === 'quota') {
        if (!ledgerRepository || typeof ledgerRepository.getDailyQuotaStatus !== 'function') {
          return jsonResponse(res, 503, { success: false, message: 'Quota information is unavailable.' });
        }
        try {
          const quota = ledgerRepository.getDailyQuotaStatus(Number(parsed.userId));
          return jsonResponse(res, 200, { success: true, ...quota });
        } catch {
          return jsonResponse(res, 503, { success: false, message: 'Quota information is unavailable.' });
        }
      }
      if (parsed.action === 'batch_reserve') {
        if (!ledgerRepository || typeof ledgerRepository.reserveCampaignBatch !== 'function') {
          return jsonResponse(res, 503, { success: false, message: 'Batch reservation is unavailable.' });
        }
        let input;
        try { input = body ? JSON.parse(body) : null; } catch { return jsonResponse(res, 400, { success: false, message: 'Invalid batch request.' }); }
        if (!input || String(input.senderWorkerId) !== parsed.userId) return jsonResponse(res, 403, { success: false, message: 'Worker identity mismatch.' });
        try {
          const result = ledgerRepository.reserveCampaignBatch(input);
          return jsonResponse(res, 200, { success: true, created: result.created, duplicate: result.duplicate, batch: sanitizedBatchResult(result) });
        } catch (error) {
          if (error?.code === 'QUOTA_EXCEEDED') return jsonResponse(res, 409, { success: false, status: 'quota_exceeded', message: 'Daily message quota exceeded.' });
          if (error?.code === 'BATCH_CONFLICT' || error?.code === 'BATCH_ITEM_CONFLICT') return jsonResponse(res, 409, { success: false, status: 'conflict', message: 'Batch reservation conflicts with an existing reservation.' });
          if (error?.code === 'BATCH_RESERVATION_FAILED') return jsonResponse(res, 503, { success: false, message: 'Batch reservation is unavailable.' });
          return jsonResponse(res, 400, { success: false, message: 'Invalid batch request.' });
        }
      }
      if (parsed.action === 'batch_lookup') {
        if (!ledgerRepository || typeof ledgerRepository.getCampaignBatch !== 'function') {
          return jsonResponse(res, 503, { success: false, message: 'Batch lookup is unavailable.' });
        }
        try {
          const result = ledgerRepository.getCampaignBatch(parsed.batchId, Number(parsed.userId));
          return jsonResponse(res, 200, result
            ? { success: true, found: true, batch: sanitizedBatchResult(result) }
            : { success: true, found: false });
        } catch {
          return jsonResponse(res, 503, { success: false, message: 'Batch lookup is unavailable.' });
        }
      }
      if (parsed.action === 'batch_cancellation_evidence') {
        if (!ledgerRepository || typeof ledgerRepository.getCampaignBatchCancellationEvidence !== 'function') {
          return jsonResponse(res, 503, { success: false, message: 'Batch cancellation evidence is unavailable.' });
        }
        try {
          const result = ledgerRepository.getCampaignBatchCancellationEvidence(parsed.batchId, Number(parsed.userId));
          return jsonResponse(res, 200, { success: true, ...result });
        } catch {
          return jsonResponse(res, 503, { success: false, status: 'unavailable', message: 'Batch cancellation evidence is unavailable.' });
        }
      }
      if (parsed.action === 'batch_send') {
        const batchHttpSendEnabled = config.batchHttpSendEnabled ?? false;
        const authHeader = req.headers['x-taskpe-dispatch-authorization'];
        if (!authHeader) {
          if (!batchHttpSendEnabled) {
            return jsonResponse(res, 503, { success: false, status: 'sending_disabled', message: 'Campaign sending is unavailable.' });
          }
          return jsonResponse(res, 401, { success: false, status: 'authorization_required', message: 'Dispatch authorization token is required.' });
        }
        const verification = verifyDispatchAuthorizationToken(authHeader, config.secret);
        if (!verification.valid) {
          return jsonResponse(res, verification.statusCode || 401, { success: false, status: 'invalid_authorization', message: verification.message });
        }
        if (String(verification.payload.worker_id) !== parsed.userId || verification.payload.batch_id !== parsed.batchId) {
          return jsonResponse(res, 403, { success: false, status: 'identity_mismatch', message: 'Dispatch authorization identity mismatch.' });
        }
        if (body !== '') return jsonResponse(res, 400, { success: false, message: 'Campaign send requests must not contain message content.' });
        if (!batchHttpSendEnabled) {
          return jsonResponse(res, 503, { success: false, status: 'sending_disabled', message: 'Campaign sending is unavailable.' });
        }
        if (!ledgerRepository || typeof ledgerRepository.consumeDispatchAuthorization !== 'function') {
          return jsonResponse(res, 503, { success: false, message: 'Campaign sending is unavailable.' });
        }
        let consumedAuthorization;
        try {
          consumedAuthorization = ledgerRepository.consumeDispatchAuthorization({
            authorizationId: verification.payload.auth_id,
            batchId: verification.payload.batch_id,
            operationId: verification.payload.operation_id,
            senderWorkerId: verification.payload.worker_id,
            planId: verification.payload.plan_id,
            requestFingerprint: verification.payload.request_fingerprint,
            issuedAt: new Date(verification.payload.issued_at * 1000).toISOString(),
            expiresAt: new Date(verification.payload.expires_at * 1000).toISOString(),
          });
        } catch (error) {
          if (error?.code === 'AUTHORIZATION_ALREADY_CONSUMED' || error?.code === 'BATCH_ALREADY_AUTHORIZED' || error?.code === 'OPERATION_ALREADY_CONSUMED') {
            return jsonResponse(res, 409, { success: false, status: 'conflict', message: error.message });
          }
          if (error?.code === 'BATCH_NOT_FOUND') {
            return jsonResponse(res, 404, { success: false, status: 'not_found', message: 'Campaign reservation was not found.' });
          }
          if (error?.code === 'WORKER_MISMATCH' || error?.code === 'PLAN_MISMATCH' || error?.code === 'FINGERPRINT_MISMATCH' || error?.code === 'BATCH_ITEMS_NOT_QUEUED' || error?.code === 'BATCH_NOT_RESERVED' || error?.code === 'RECIPIENT_OPTED_OUT') {
            return jsonResponse(res, 409, { success: false, status: 'reservation_mismatch', message: error.message });
          }
          return jsonResponse(res, 503, { success: false, message: 'Dispatch authorization processing is unavailable.' });
        }
        if (typeof ledgerRepository.getCampaignBatch !== 'function' || typeof ledgerRepository.getOutgoingMessage !== 'function' || typeof ledgerRepository.transition !== 'function' || typeof trustedCampaignContentResolver !== 'function') {
          return jsonResponse(res, 503, { success: false, message: 'Campaign sending is unavailable.' });
        }
        const state = connections.get(parsed.userId);
        if (!state?.socket) return jsonResponse(res, 503, { success: false, message: 'WhatsApp connection is unavailable.' });
        try {
          const result = await sendReservedCampaignBatch({ ledger: ledgerRepository, socket: state.socket, senderWorkerId: Number(parsed.userId), batchId: parsed.batchId, dispatchOperationId: consumedAuthorization.operationId, resolveContent: trustedCampaignContentResolver, prepareImageMessage });
          return jsonResponse(res, result.success ? 200 : (result.status === 'not_found' ? 404 : 503), result);
        } catch {
          return jsonResponse(res, 503, { success: false, message: 'Campaign sending is unavailable.' });
        }
      }
      if (parsed.action === 'disconnect') {
        await withUserLock(parsed.userId, async () => {
          const state = connections.get(parsed.userId);
          if (state) {
            state.cancelled = true;
            state.generation += 1;
            if (state.reconnectTimer) clearTimeout(state.reconnectTimer);
            if (state.socket?.logout) { try { await Promise.race([state.socket.logout(), new Promise((_, reject) => setTimeout(() => reject(new Error('logout timeout')), 5000))]); } catch { /* cleanup remains best effort */ } }
            if (state.socket?.ws?.close) state.socket.ws.close();
            if (connections.get(parsed.userId) === state) connections.delete(parsed.userId);
          }
          await removeUserSession(config.sessionRoot, parsed.userId);
        });
        return jsonResponse(res, 200, { success: true, status: 'disconnected', cleanup: true });
      }
      const state = await withUserLock(parsed.userId, () => ensureConnection(parsed.userId));
      if (!state) return jsonResponse(res, 503, { success: false, message: 'WhatsApp service is shutting down.' });
      if (parsed.action === 'qr') {
        return jsonResponse(res, 200, { success: true, status: state.status, qr: state.qr && state.qrExpiresAt > Date.now() ? state.qr : null, expires_at: state.qrExpiresAt || null });
      }
      return jsonResponse(res, 200, { success: true, status: state.status, qr_available: Boolean(state.qr && state.qrExpiresAt > Date.now()) });
    } catch (error) {
      return jsonResponse(res, 503, { success: false, message: 'WhatsApp service unavailable.' });
    }
  }

  return { handle, connections, replayCache, closeAll: async () => {
    shuttingDown = true;
    for (const state of connections.values()) {
      state.cancelled = true;
      state.generation += 1;
      if (state.reconnectTimer) clearTimeout(state.reconnectTimer);
      try { state.socket?.ws?.close(); } catch {}
    }
    connections.clear();
    await Promise.allSettled([...operations.values()]);
  } };
}

export async function startServer(overrides = {}) {
  const config = { ...configFromEnv(), ...overrides };
  if (config.enabled && process.platform === 'win32' && !config.allowUnsupportedFilesystem) throw new Error('Enabled QR pairing requires POSIX filesystem ownership and permission checks.');
  await assertPrivateDirectory(config.sessionRoot, { create: true });
  let ledgerDb = null;
  let ledgerRepository = config.ledgerRepository || null;
  if (config.enabled && !ledgerRepository) {
    ledgerDb = initializeLedger();
    ledgerRepository = createLedgerRepository(ledgerDb);
  }
  const service = createService({ ...config, ledgerRepository });
  const server = http.createServer((req, res) => service.handle(req, res).catch(() => jsonResponse(res, 500, { success: false, message: 'Internal service error.' })));
  server.requestTimeout = 10000; server.headersTimeout = 5000; server.keepAliveTimeout = 5000;
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(config.port, HOST, resolve); });
  const shutdown = async () => { await service.closeAll(); if (ledgerDb) closeLedger(ledgerDb); await new Promise(resolve => server.close(resolve)); };
  process.once('SIGINT', shutdown); process.once('SIGTERM', shutdown);
  return { server, service, shutdown };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  startServer().then(() => console.log(`TaskPe WhatsApp service listening on ${HOST}`)).catch(error => { console.error(error.message); process.exitCode = 1; });
}
