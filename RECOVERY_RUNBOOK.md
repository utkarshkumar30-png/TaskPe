# TaskPe recovery and rollback runbook

## Known-good state

The currently verified known-good application state is the checked application
source with the 83-test suite passing. The database backup used for the Phase 3
restore rehearsal is:

`taskpe.db.pre_exact_money_backup`

The matching proof-upload source is `uploads/proofs`. Backups must be stored
outside the application directory with restricted access and recorded SHA-256
hashes.

## Backup

Stop the WSGI service and expiry job before copying SQLite. Then copy both the
database selected by `TASKPE_DB_PATH` (or `taskpe.db` when unset) and the whole
`uploads/proofs` directory. Record the database hash and file count/hash list.
Do not overwrite the source during backup.

## Restore rehearsal

Create a new temporary directory, copy the known-good database backup to
`taskpe.db`, and copy the proof backup to `proofs`. Set `TASKPE_DB_PATH` to the
temporary database and point `app.config['UPLOAD_FOLDER']` to the temporary
proof directory. Start Waitress on a non-production port, for example:

```powershell
$env:TASKPE_DB_PATH = 'C:\staging\taskpe.db'
$env:ENVIRONMENT = 'production'
$env:SECRET_KEY = '<staging-only-secret>'
$env:SMS_PROVIDER = 'disabled'
$env:PAYOUT_MODE = 'MANUAL'
.venv\Scripts\waitress-serve.exe --listen=127.0.0.1:5010 wsgi:app
```

Smoke-test `/`, `/api/tasks`, `/api/auth/me`, admin statistics/submissions,
wallet summary, and a protected proof URL using a staging session. Confirm
users, tasks, submissions, wallet transactions, withdrawals, and proof files
are accessible. Stop the staging process after verification.

## Rollback

1. Stop the application and expiry job.
2. Restore the previous application version.
3. Restore the verified database backup to the exact `TASKPE_DB_PATH`.
4. Restore the matching proof-upload backup directory.
5. Verify ownership/permissions and database SHA-256 hash.
6. Start the WSGI application.
7. Run smoke tests for login, task listing, wallet summary, admin pages, proof
   access, and a read-only finance check.

Never delete the failed database before the backup is preserved. Do not run
schema migrations or recalculation scripts as part of rollback.

## Risks

- SQLite requires all writers to be stopped for a consistent file copy.
- Database and proof backups must be paired from the same point in time.
- The current in-memory rate limiter is not shared across multiple processes.
- A staging restore does not validate external Fast2SMS delivery; use a staging
  provider configuration separately.
