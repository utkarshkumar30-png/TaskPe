# TaskPe WhatsApp safety release candidate

This is a provisional, untrusted release-candidate workspace assembled from an explicit allowlist. It is not a signed release and has not been deployed.

## Original checkout snapshot

- Source checkout: `C:\Users\utkar\.gemini\antigravity\scratch\taskpe`
- Branch: `master`
- HEAD: `e15a632344d8c3d3ac3d47a84d84adab6c416b19`
- Remote baseline previously observed: `origin/master` at the same commit
- The original checkout was not staged, committed, reset, cleaned, or pushed.

## Inclusion rationale

The candidate includes the complete shared Flask/database files because the consent, E.164, campaign approval, opt-out, dispatch, and QR/pairing paths are interleaved with the existing application. It includes the current templates and JavaScript needed by those routes, the Node service/ledger, deployment, rollback, and database-restore controls, the explicit declaration-history migration, runtime dependencies, and focused tests.

The candidate deliberately excludes databases, uploads, WhatsApp sessions, secrets, caches, archives, temporary test roots, investigative scripts, and unrelated finance/OCR/spreadsheet/authentication tests and assets.

## Migration review

`migrate_whatsapp_consent_declaration_history.py` is explicit and requires `--db`. It detects the legacy `UNIQUE(task_id)` declaration table, renames the old table to an archive, creates the append-only table, copies all rows, and recreates the lookup index inside a transaction. It is not imported or called by application startup or `deploy.sh`. It has only been rehearsed against a disposable local database in the original checkout; no real or production database was used.

## Deployment review

`deploy.sh` validates the intended release checksum set, opens the intended live database read-only, and calls `database.verify_required_schema` before active application replacement. It retains backup, rollback, post-copy verification, and disabled WhatsApp-send-flag checks. The script was not executed.

## Known limitations and blockers

- This directory has no Git history or signed provenance; the manifest is provisional.
- The eight hashes hard-coded in `deploy.sh` do not match the candidate files. They correspond to an older tested artifact and must not be silently replaced here.
- The shared `app.py`, `database.py`, templates, and JavaScript files contain unrelated existing application changes in addition to the WhatsApp safety work. They require line-level review before approval.
- The candidate intentionally omits `node_modules`; Node tests require dependencies to be installed in a separate staging environment from `package-lock.json`.
- Full Bash deployment and real Flask/Node HTTP integration tests require the supported Linux/staging environment; Windows previously failed before the deployment script/socket harness could execute.
- Existing databases with the legacy declaration uniqueness constraint require the explicit migration after backup and staging rehearsal.
- Sending flags remain disabled by policy; no environment file is included or changed.

## Tests

Candidate-local validation performed in this workspace:

- Python syntax compilation: passed.
- Node syntax checks for `server.js` and `whatsapp_ledger.js`: passed.
- Focused Python suite: `144 passed, 1 failed, 33 subtests passed`; the failure is the existing controlled-send simulation asserting that its mock send callback was invoked, while the endpoint returned acknowledged. It remains unresolved.
- Deployment static checks: `5 passed, 9 deselected`.
- Node service tests: `83 passed, 2 skipped`; skips are Windows symlink/POSIX-permission limitations.
- `git diff --check` is not applicable because this candidate directory is not a Git checkout.

No deployment, production access, real WhatsApp transport, or sending flag enablement is permitted.
