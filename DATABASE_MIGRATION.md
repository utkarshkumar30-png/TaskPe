# TaskPe database migration procedure

This procedure is intentionally copy-first. Do not run the migration against
the live database until the staging report and integrity checks have been
reviewed.

## Identify the database

`TASKPE_DB_PATH` overrides the default. If it is unset, the production path is
`taskpe.db` beside `database.py`.

## Backup

Stop the application and expiry job, then copy the database and proof uploads
to a separate, access-controlled backup location. For SQLite, copying while
the process is stopped preserves a consistent file. Keep the original files
unchanged and record SHA-256 hashes.

```powershell
$stamp = Get-Date -Format yyyyMMdd_HHmmss
Copy-Item -LiteralPath $env:TASKPE_DB_PATH -Destination "taskpe.db.backup.$stamp"
Copy-Item -LiteralPath uploads\proofs -Destination "proofs.backup.$stamp" -Recurse
Get-FileHash "taskpe.db.backup.$stamp" -Algorithm SHA256
```

If `TASKPE_DB_PATH` is empty, use the project-local `taskpe.db` explicitly.

## Stage and migrate

Copy the backed-up database to a staging filename. Never point this command at
the production path:

```powershell
Copy-Item taskpe.db taskpe.db.staging -Force
.venv\Scripts\python.exe migrate_exact_money.py `
  --db taskpe.db.staging `
  --report taskpe.db.staging.report.json
```

The migration adds exact integer columns and retains every legacy column and
row. It converts INR to paise, USDT to micro-USDT, and percentages to basis
points. A failed conversion rolls back the transaction. Running it again is
safe and reports `already_migrated` rather than converting again.

## Staging verification

Run SQLite checks against the staging copy:

```powershell
.venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('taskpe.db.staging'); print(c.execute('pragma integrity_check').fetchone()[0]); print(c.execute('pragma foreign_key_check').fetchall()); c.close()"
```

Compare row counts and financial totals for users, tasks, submissions,
wallet transactions, withdrawals, referrals, target records, and target
payments against the pre-migration backup. Historical ledger rows must remain
present; do not recalculate rewards or balances.

## Production execution

Only after staging review: stop all writers, take a fresh backup, run the same
command against the explicitly verified production path, save the JSON report,
run integrity checks, and retain both the pre- and post-migration backups.
Restart the WSGI service only after the report and checks are clean. If any
row is rejected or any integrity check fails, restore the backup instead of
continuing.

## Withdrawal payment-identifier uniqueness

Before enabling provider payouts on an existing database, inspect and apply the
dedicated migration against an explicit staging copy:

```powershell
.venv\Scripts\python.exe migrate_withdrawal_identifier_uniqueness.py `
  --db taskpe_staging_copy.db `
  --report taskpe_staging_copy.withdrawal_uniqueness.json
```

The migration first reports duplicate non-empty `provider_reference` or
`transaction_hash` values and leaves the database unchanged when collisions are
found. After those records are reviewed and resolved through an approved
financial-history procedure, rerun it. It creates partial unique indexes so
NULL and empty identifiers remain allowed while real payout identifiers cannot
be reused.

## Finance settings history

Apply the audit-table migration only to an explicit staging copy first:

```powershell
.venv\Scripts\python.exe migrate_finance_settings_history.py `
  --db taskpe_staging_copy.db `
  --report taskpe_staging_copy.finance_settings_history.json
```

The migration creates the append-only `finance_settings_history` table, its
lookup indexes, and database triggers that reject updates and deletes. It does
not fabricate history for existing settings; the first new successful change
starts the audit trail.
