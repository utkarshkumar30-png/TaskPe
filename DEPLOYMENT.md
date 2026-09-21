# TaskPe Deployment Runbook

This document describes the existing TaskPe deployment architecture. It uses
Waitress behind a private reverse proxy and SQLite on persistent storage.

## Requirements

- Windows host for the documented batch launcher, or an equivalent host that
  can run Python 3.12.
- Python 3.12.x.
- A persistent location for the SQLite database, proof uploads, and campaign
  assets.
- A private reverse proxy for public HTTPS.

The application server is Waitress. Flask's development server is not a
production server.

## Installation

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Environment variables

Set variables in the process/service environment or a host-local secret
manager. The application does not automatically load the repository `.env`
file, and the production batch launcher does not load it either. A `.env`
file beside the checkout is therefore not sufficient; explicitly inject the
variables into the Windows service or process environment.

For a Windows process or service, configure the environment before starting
`wsgi:app` (for example, through the service wrapper's environment settings
or a host-local launch configuration). Do not commit a `.env` file or real
credentials.

Required for production:

```text
ENVIRONMENT=production
SECRET_KEY=<strong-random-production-secret>
TASKPE_DB_PATH=<absolute-persistent-sqlite-database-path>
TASKPE_UPLOAD_FOLDER=<absolute-persistent-proof-upload-directory>
TASKPE_TASK_ASSET_FOLDER=<absolute-persistent-campaign-asset-directory>
PAYOUT_MODE=MANUAL
SMS_PROVIDER=disabled
EMAIL_PROVIDER=disabled
```

These variables are mandatory for production. The three storage paths must
be absolute, persistent, outside the application repository, and readable or
writable only by the application/service account as appropriate. Back up the
database, proof uploads, and task assets separately.

To enable Fast2SMS OTP delivery:

```text
SMS_PROVIDER=fast2sms
FAST2SMS_ENABLED=1
FAST2SMS_API_KEY=<fast2sms-api-key>
FAST2SMS_ROUTE=q
```

Fast2SMS is the only SMS provider implemented by the application. The server
generates, hashes, stores, and validates OTPs; Fast2SMS Quick SMS is used for
delivery without the OTP Message API. Requests use `route=q`, the `message`
field, and `numbers`. The application rejects any route other than `q`.

The production application fails at startup if these variables are missing,
relative, or unavailable. Development may use repository-local defaults.

Additional supported variables:

```text
TASKPE_ADMIN_EMAIL=<initial-admin-email>
TASKPE_ADMIN_PASSWORD=<initial-admin-password>
TASKPE_ADMIN_MOBILE=<initial-admin-mobile>
TRUSTED_PROXY_HOPS=0
ENABLE_HSTS=0
```

`TASKPE_ADMIN_*` values are used only to provision an administrator on an
empty production database. They must be supplied through the deployment
environment and never committed.

`TRUSTED_PROXY_HOPS=1` is appropriate only when exactly one trusted local
HTTPS reverse proxy fronts Waitress. Enable `ENABLE_HSTS=1` only after HTTPS
is enforced and externally verified.

`EMAIL_PROVIDER=disabled` disables email delivery. This is the safe default
and does not send email. When email signup is intentionally enabled, the
implemented provider is Brevo's HTTPS transactional email API and requires:

```text
EMAIL_PROVIDER=brevo
BREVO_API_KEY=<secret>
EMAIL_FROM=<approved-sender>
EMAIL_FROM_NAME=TaskPe
```

In production, the application fails safely at startup when Brevo
configuration is incomplete. Credentials are read only from the explicitly
injected deployment environment and are never stored in this repository.
Email delivery uses HTTPS and does not use Gmail SMTP, Brevo SMTP, or SMTP
sockets.

Signup supports mobile OTP and email verification. Mobile signup uses the
existing Fast2SMS controls. Email signup uses hashed, expiring, one-time
tokens. The signed first-party device cookie is stored separately from
personal data; the server permits at most two email accounts and four mobile
accounts per device. Existing users without signup metadata remain valid.

## Persistent storage

Development-only local defaults are relative to the project directory:

```text
taskpe.db
uploads/proofs/
uploads/task-assets/
```

Production must configure absolute, persistent paths outside the application
checkout, for example:

```text
TASKPE_DB_PATH=<persistent-data-directory>\taskpe.db
TASKPE_UPLOAD_FOLDER=<persistent-data-directory>\uploads\proofs
TASKPE_TASK_ASSET_FOLDER=<persistent-data-directory>\uploads\task-assets
```

The database and both upload directories must be readable/writable only by
the application/service account as appropriate. Keep database files, uploads,
backups, and secrets outside source control, and back up the database and
each upload directory separately.

## Backup procedure

Stop writes to the application before a backup, or use SQLite's online backup
API. A safe SQLite backup must be made from a database connection rather than
blindly copying a live file. Keep the backup on separate storage and verify it
opens successfully.

Before any migration, record the database SHA-256 and retain a byte-for-byte
backup. Never overwrite the only backup.

Back up these application state directories separately:

```text
<persistent-data-directory>\uploads\proofs\
<persistent-data-directory>\uploads\task-assets\
```

## Migrations and integrity checks

Production application startup is verification-only. Importing `wsgi:app` does
not run schema migrations, financial backfills, withdrawal repairs, or seed
data. If the required schema is incomplete, startup fails with a migration
instruction and the database is not changed.

For a deliberate fresh database, initialize it explicitly after choosing and
backing up the target path:

```powershell
.\.venv\Scripts\python.exe initialize_database.py --db <fresh-database>
```

This explicit initializer performs the existing schema creation and
compatibility work. Development seeding is available only when
`ENVIRONMENT=development`; production never seeds demo data. Existing admin
accounts are not overwritten. Run explicit migrations separately, validate
them, and only then start the application.

Never run a migration directly against the only production copy. First create
and verify a disposable copy, run the migration using its explicit `--db`
argument, and inspect the result.

For the exact-money migration:

```powershell
.\.venv\Scripts\python.exe migrate_exact_money.py --db <copied-database> --report <temporary-report>.json
```

For the additive signup/device schema, use the same copy-first process:

```powershell
.\.venv\Scripts\python.exe migrate_signup_device_limits.py --db taskpe_staging_copy.db --report taskpe_staging_copy.signup-device-report.json
```

This adds `users.signup_method`, `device_registrations`, and
`email_verifications`; it does not fabricate historical device registrations
or alter financial rows.

Verify the migrated copy with SQLite:

```sql
PRAGMA integrity_check;
PRAGMA foreign_key_check;
```

The expected integrity result is `ok` and the foreign-key result is empty.

## Waitress startup

The existing Windows launcher runs:

```powershell
.\.venv\Scripts\waitress-serve.exe --listen=127.0.0.1:5000 --threads=4 wsgi:app
```

Waitress must remain bound to localhost/private networking. It must not be the
public Internet-facing endpoint.

## Reverse proxy and HTTPS

The required topology is:

```text
Internet -> HTTPS reverse proxy:443 -> Waitress:127.0.0.1:5000 -> TaskPe -> SQLite
```

The repository contains a Nginx example at
`deploy/nginx/taskpe.conf.example`. Create a host-local copy and configure:

- DNS and the public hostname
- HTTP-to-HTTPS redirect
- TLS certificate and private key paths outside the repository
- `X-Forwarded-Proto`, host, port, and client-IP headers
- request-size limits suitable for proof/image uploads
- proxy read/send timeouts
- firewall access to ports 80/443 only

HTTPS, DNS, certificates, and firewall rules are deployment configuration
requirements; they are not verified by this repository runbook.

## Fast2SMS

When `SMS_PROVIDER=fast2sms`, configure `FAST2SMS_API_KEY` and
`FAST2SMS_ROUTE=q` only in the secure deployment environment. Never print or
log the key. Fast2SMS Quick SMS delivery does not require the OTP Message API
or an OTP template ID; provider account approval and wallet requirements still
apply.

When SMS is disabled, OTP delivery is unavailable and production signup/mobile
verification must not be treated as verified.

`EMAIL_PROVIDER=disabled` means no email is sent. `EMAIL_PROVIDER=brevo`
uses Brevo's HTTPS transactional email API and requires the complete Brevo
configuration documented above. Keep email disabled until the API key and
approved sender are configured and a controlled delivery test is complete.
Development verification tokens are not production email delivery.

## Manual payout mode

`PAYOUT_MODE=MANUAL` is mandatory. TaskPe does not automatically transfer
cryptocurrency or mark payouts paid without the required manual confirmation
data. Provider references and transaction hashes must be recorded according to
the existing admin workflow.

## Health check and smoke test

After starting the staging copy or deployed service, verify:

```text
GET /health
```

The expected response is a non-sensitive JSON status response. Run the
application test suite against a disposable database before production use:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node --check static/js/app.js
node --check static/js/admin.js
```

Also verify login, admin authorization, task detail, wallet, withdrawal, and
protected upload routes without creating production financial data.

## Logging

Logs must never contain passwords, OTP values, API keys, `SECRET_KEY`, private
keys, or unnecessary wallet details. Keep operational logs on host-local
storage with appropriate retention and access controls.

## Restore and rollback

1. Stop Waitress and the reverse proxy route to the application.
2. Preserve the current database, uploads, and logs.
3. Restore the verified SQLite backup to a new path; do not overwrite the only
   copy.
4. Restore the matching proof and campaign-asset backups.
5. Run `PRAGMA integrity_check` and `PRAGMA foreign_key_check`.
6. Point `TASKPE_DB_PATH` and upload paths at the restored state.
7. Start the previously verified application build.
8. Verify `/health`, authentication, finance views, and manual payout mode.

Rollback means returning to the last verified application/database/upload set;
it does not mean deleting or rewriting the original production database.

## Production checklist

- [ ] Production environment variables supplied securely.
- [ ] Strong production `SECRET_KEY` configured.
- [ ] `PAYOUT_MODE=MANUAL` confirmed.
- [ ] Waitress bound privately on `127.0.0.1:5000`.
- [ ] Reverse proxy configured with HTTPS and HTTP redirect.
- [ ] DNS, certificate, firewall, and proxy headers externally verified.
- [ ] Database and both upload directories backed up and restorable.
- [ ] Disposable-copy migration validation completed where required.
- [ ] SQLite integrity and foreign-key checks pass.
- [ ] Full automated suite and JavaScript syntax checks pass.
- [ ] Fast2SMS configuration and any approved delivery test completed, if SMS
      is required.
- [ ] No runtime artifacts or secrets would be committed to Git.
