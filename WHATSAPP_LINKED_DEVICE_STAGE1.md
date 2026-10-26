# TaskPe WhatsApp linked-device prototype (Stage 1)

This is a local-development-only QR pairing prototype. It is disabled by default and does not send messages, run campaigns, or change TaskPe task, proof, approval, payout, wallet, referral, or withdrawal behavior. The existing manual `wa.me` workflow remains unchanged.

## Local setup

1. Copy the WhatsApp variables from `.env.example` into the local Flask environment.
2. Generate a random shared secret of at least 32 characters and use the same value for Flask and the Node service. Do not commit it.
3. Prefer an absolute session directory outside the checkout with permissions restricted to the service account.
4. In a supported local Node environment (Node 20 or newer on POSIX with a private local filesystem), run `npm install` and then `npm run start` from this directory. The service listens only on `127.0.0.1`.
5. Set `TASKPE_WHATSAPP_LINKED_DEVICE_ENABLED=1` only for a deliberate local test. With the default `0`, Flask returns not found for the prototype routes.

The Flask routes use the authenticated TaskPe session as the only identity source:

- `POST /api/whatsapp/connection/qr`
- `GET /api/whatsapp/connection/status`
- `POST /api/whatsapp/connection/disconnect`

Browser state-changing requests still require the existing Flask CSRF protection. Flask authenticates each localhost service request with a timestamped HMAC; the Node service rejects stale, malformed, cross-user, and invalidly signed requests.

Each internal request also carries a random request ID. The ID is included in the HMAC and is accepted only once within the timestamp window. The replay cache is bounded and expires old entries.

## Scope and limitations

The Node service stores each user's Baileys multi-file credentials in a private `user-<id>` directory, rejects symlinked roots/directories, keeps QR values in memory with a short expiry, and removes that user's session directory on disconnect. Per-user connect/disconnect operations are serialized and stale socket callbacks cannot resurrect a deliberately disconnected session. Credential-save failures are caught, reported without credential contents, and disable the affected connection. It has bounded request bodies, timeouts, reconnect handling, and graceful shutdown. It does not expose session files through Flask or the static directory and does not provide message-sending endpoints.

### Supported filesystem/security configuration

Real enabled QR pairing is currently supported only on POSIX filesystems where the service can verify the session-root owner and restrictive mode bits. The service fails closed when enabled on Windows because portable Node APIs do not provide equivalent ACL/ownership verification here. Windows may run the disabled service and the injected unit tests, but it must not be used for QR pairing until explicit Windows ACL enforcement is implemented.

Path checks use `lstat`, `realpath`, root-boundary checks, restrictive permissions, and serialized operations. Portable filesystem APIs cannot eliminate every local-process race between validation and a later filesystem operation; the session root must therefore remain private to the service account.

This prototype does not establish production readiness for WhatsApp policy compliance, consent/opt-out operations, delivery confirmation, high availability, or persistent rate limits. A production design must address those separately and must use legitimate opted-in messaging workflows; random delays or rate limits are not enforcement workarounds.

### Local send timeout and uncertain outcomes

The local send adapter uses a bounded server-side timeout. A timeout stops TaskPe waiting for the Baileys promise; it does not cancel the underlying Baileys operation. The external send outcome therefore remains uncertain. Such records are marked `delivered_unknown`, retain their quota reservation, and are never automatically retried or resent. Lease expiry is a conservative reconciliation marker, not proof that the underlying socket operation has stopped. Reconciliation is ledger-only and must be explicitly invoked; it is not scheduled or run at startup.

## Reproducible Node installation

This checkout contains `package-lock.json` with lockfile version 3. The declared direct dependencies remain Baileys `^6.7.18` and Pino `^9.4.0`; the lockfile resolves Baileys `6.7.24` and Pino `9.14.0` under Node 20+.

Use the committed lockfile for reproducible installation and review it whenever `package.json` changes:

```text
npm ci
node --test test_whatsapp_service.test.js
```

On Windows PowerShell systems where `npm.ps1` is blocked by execution policy, use `npm.cmd ci` instead. Do not regenerate or edit the lockfile by hand; if dependencies change, run `npm.cmd install --package-lock-only`, review the resulting lockfile, then run `npm.cmd ci` and the tests.

## Rollback

Stop the local Node service and set `TASKPE_WHATSAPP_LINKED_DEVICE_ENABLED=0`. No existing TaskPe records are changed. For a code rollback, remove only the Stage 1 files/routes listed in the change report; do not alter the existing task or WhatsApp workflow. A local session directory may be removed after the linked device is disconnected if its contents are no longer needed.
