import sqlite3
import json
import os
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash

DB_PATH = os.environ.get('TASKPE_DB_PATH') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'taskpe.db')

def normalize_target_number(value):
    """Use the project's canonical phone normalization for target records."""
    return ''.join(character for character in str(value or '') if character.isdigit())

def is_development_environment():
    """Only an explicit development setting may create sample credentials/data."""
    return os.environ.get('ENVIRONMENT', 'production').strip().lower() == 'development'

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def withdrawal_identifier_collisions(conn):
    """Return duplicate non-empty payout identifiers before unique indexes exist."""
    collisions = {}
    for column in ('provider_reference', 'transaction_hash'):
        rows = conn.execute(f'''
            SELECT {column} AS identifier, GROUP_CONCAT(id) AS withdrawal_ids,
                   COUNT(*) AS occurrences
            FROM withdrawals
            WHERE {column} IS NOT NULL AND TRIM({column}) != ''
            GROUP BY {column}
            HAVING COUNT(*) > 1
        ''').fetchall()
        if rows:
            collisions[column] = [
                {
                    'identifier': row['identifier'] if isinstance(row, sqlite3.Row) else row[0],
                    'withdrawal_ids': row['withdrawal_ids'] if isinstance(row, sqlite3.Row) else row[1],
                    'occurrences': row['occurrences'] if isinstance(row, sqlite3.Row) else row[2],
                }
                for row in rows
            ]
    return collisions


def ensure_withdrawal_identifier_indexes(conn):
    """Install database-level payout identifier guards after collision checks."""
    collisions = withdrawal_identifier_collisions(conn)
    if collisions:
        details = '; '.join(
            f'{column}: ' + ', '.join(
                f'{row["identifier"]!r} on withdrawals {row["withdrawal_ids"]}'
                for row in rows
            )
            for column, rows in collisions.items()
        )
        raise RuntimeError(
            'Cannot enforce withdrawal identifier uniqueness; resolve existing collisions first: '
            + details
        )

    conn.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS withdrawal_provider_reference_unique
        ON withdrawals(provider_reference)
        WHERE provider_reference IS NOT NULL AND TRIM(provider_reference) != ''
    ''')
    conn.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS withdrawal_transaction_hash_unique
        ON withdrawals(transaction_hash)
        WHERE transaction_hash IS NOT NULL AND TRIM(transaction_hash) != ''
    ''')


def ensure_finance_settings_history(conn):
    """Create the append-only finance settings audit structure."""
    conn.execute('''
        CREATE TABLE IF NOT EXISTS finance_settings_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            setting_name TEXT NOT NULL,
            old_value TEXT NOT NULL,
            new_value TEXT NOT NULL,
            changed_by INTEGER NOT NULL,
            changed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            unit TEXT,
            reason TEXT,
            FOREIGN KEY (changed_by) REFERENCES users(id)
        )
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS finance_settings_history_setting_name
        ON finance_settings_history(setting_name)
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS finance_settings_history_changed_at
        ON finance_settings_history(changed_at)
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS finance_settings_history_changed_by
        ON finance_settings_history(changed_by)
    ''')
    conn.execute('''
        CREATE TRIGGER IF NOT EXISTS finance_settings_history_no_update
        BEFORE UPDATE ON finance_settings_history
        BEGIN
            SELECT RAISE(ABORT, 'finance settings history is immutable');
        END
    ''')
    conn.execute('''
        CREATE TRIGGER IF NOT EXISTS finance_settings_history_no_delete
        BEFORE DELETE ON finance_settings_history
        BEGIN
            SELECT RAISE(ABORT, 'finance settings history is immutable');
        END
    ''')

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()

    # Exact-money settings are maintained alongside the legacy text settings.
    # Keeping this table in the normal idempotent initializer ensures the admin
    # settings endpoint works on both fresh and migrated databases.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS exact_money_settings (
            key TEXT PRIMARY KEY,
            value_scaled INTEGER NOT NULL,
            scale INTEGER NOT NULL,
            kind TEXT NOT NULL,
            migrated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Users Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            mobile TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            wallet_balance REAL NOT NULL DEFAULT 0.0,
            total_earned REAL NOT NULL DEFAULT 0.0,
            referral_code TEXT UNIQUE NOT NULL,
            referred_by INTEGER,
            referred_user_task_count INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (referred_by) REFERENCES users(id)
        )
    ''')

    cursor.execute("PRAGMA table_info(users)")
    user_cols = [c[1] for c in cursor.fetchall()]
    if 'referred_user_task_count' not in user_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN referred_user_task_count INTEGER NOT NULL DEFAULT 0")
    if 'wallet_balance_paise' not in user_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN wallet_balance_paise INTEGER")
    if 'total_earned_paise' not in user_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN total_earned_paise INTEGER")

    # Tasks Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'Direct Message',
            description TEXT NOT NULL,
            payout REAL NOT NULL,
            template_text TEXT NOT NULL,
            target_numbers TEXT NOT NULL,
            campaign_id INTEGER,
            recipients_per_slot INTEGER NOT NULL DEFAULT 0,
            time_limit_minutes INTEGER NOT NULL DEFAULT 15,
            proof_instructions TEXT NOT NULL,
            total_slots INTEGER NOT NULL DEFAULT 100,
            completed_slots INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Each start creates a private assignment.  The partial unique index is the
    # database-level guard that permits one active assignment per user/campaign
    # type, even if two requests arrive at the same time.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS task_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            task_id INTEGER NOT NULL,
            campaign_id INTEGER NOT NULL,
            campaign_type TEXT NOT NULL,
            assigned_numbers TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'in_progress',
            assigned_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            expires_at DATETIME,
            slot_released INTEGER NOT NULL DEFAULT 0,
            completed_at DATETIME,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        )
    ''')
    cursor.execute("PRAGMA table_info(tasks)")
    task_cols = [c[1] for c in cursor.fetchall()]
    if 'payout_paise' not in task_cols:
        cursor.execute("ALTER TABLE tasks ADD COLUMN payout_paise INTEGER")
    cursor.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_campaign_per_user
        ON task_assignments(user_id, campaign_type)
        WHERE status IN ('in_progress', 'pending_review')
    ''')
    cursor.execute("PRAGMA table_info(task_assignments)")
    assignment_cols = [c[1] for c in cursor.fetchall()]
    if 'started_at' not in assignment_cols:
        cursor.execute("ALTER TABLE task_assignments ADD COLUMN started_at DATETIME")
    if 'expires_at' not in assignment_cols:
        cursor.execute("ALTER TABLE task_assignments ADD COLUMN expires_at DATETIME")
    if 'slot_released' not in assignment_cols:
        cursor.execute("ALTER TABLE task_assignments ADD COLUMN slot_released INTEGER NOT NULL DEFAULT 0")

    # Phase 1 target-number foundation: one durable row per number assigned to
    # an assignment. Existing assigned_numbers JSON remains the source used by
    # the current task flow; this table is additive and migration-safe.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS target_number_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assignment_id INTEGER NOT NULL,
            task_id INTEGER NOT NULL,
            campaign_id INTEGER,
            user_id INTEGER NOT NULL,
            target_number TEXT NOT NULL,
            normalized_target_number TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'assigned'
                CHECK (status IN ('assigned', 'verified', 'paid', 'remaining', 'rejected')),
            reward_per_number NUMERIC,
            verified_at DATETIME,
            paid_at DATETIME,
            paid_amount NUMERIC,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (assignment_id, normalized_target_number),
            FOREIGN KEY (assignment_id) REFERENCES task_assignments(id),
            FOREIGN KEY (task_id) REFERENCES tasks(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS target_number_records_assignment_status
        ON target_number_records(assignment_id, status)
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS target_number_records_normalized_lookup
        ON target_number_records(normalized_target_number, status)
    ''')
    cursor.execute("PRAGMA table_info(target_number_records)")
    target_record_cols = [c[1] for c in cursor.fetchall()]
    if 'reward_per_number_paise' not in target_record_cols:
        cursor.execute("ALTER TABLE target_number_records ADD COLUMN reward_per_number_paise INTEGER")
    if 'paid_amount_paise' not in target_record_cols:
        cursor.execute("ALTER TABLE target_number_records ADD COLUMN paid_amount_paise INTEGER")
    # Once a target has been paid its assignment, ownership and lifecycle
    # history are immutable. Reuse creates a new record instead of mutating it.
    cursor.execute('''
        CREATE TRIGGER IF NOT EXISTS paid_target_record_immutable_update
        BEFORE UPDATE ON target_number_records
        WHEN OLD.status = 'paid'
        BEGIN
            SELECT RAISE(ABORT, 'paid target records are immutable');
        END
    ''')
    cursor.execute('''
        CREATE TRIGGER IF NOT EXISTS paid_target_record_immutable_delete
        BEFORE DELETE ON target_number_records
        WHEN OLD.status = 'paid'
        BEGIN
            SELECT RAISE(ABORT, 'paid target records are immutable');
        END
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS target_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_number_record_id INTEGER NOT NULL UNIQUE,
            assignment_id INTEGER NOT NULL,
            task_id INTEGER NOT NULL,
            campaign_id INTEGER,
            user_id INTEGER NOT NULL,
            submission_id INTEGER NOT NULL,
            target_number TEXT NOT NULL,
            normalized_target_number TEXT NOT NULL,
            amount NUMERIC NOT NULL,
            rate NUMERIC NOT NULL,
            payment_reference TEXT NOT NULL UNIQUE,
            paid_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (target_number_record_id) REFERENCES target_number_records(id),
            FOREIGN KEY (assignment_id) REFERENCES task_assignments(id),
            FOREIGN KEY (task_id) REFERENCES tasks(id),
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (submission_id) REFERENCES submissions(id)
        )
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS target_payments_submission_lookup
        ON target_payments(submission_id)
    ''')
    cursor.execute("PRAGMA table_info(target_payments)")
    target_payment_cols = [c[1] for c in cursor.fetchall()]
    if 'amount_paise' not in target_payment_cols:
        cursor.execute("ALTER TABLE target_payments ADD COLUMN amount_paise INTEGER")
    if 'rate_paise' not in target_payment_cols:
        cursor.execute("ALTER TABLE target_payments ADD COLUMN rate_paise INTEGER")
    cursor.execute("PRAGMA table_info(target_payments)")
    payment_columns = [c[1] for c in cursor.fetchall()]
    if 'target_number' not in payment_columns:
        cursor.execute("ALTER TABLE target_payments ADD COLUMN target_number TEXT")
    if 'normalized_target_number' not in payment_columns:
        cursor.execute("ALTER TABLE target_payments ADD COLUMN normalized_target_number TEXT")
    cursor.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS one_target_payment_per_campaign_number
        ON target_payments(campaign_id, normalized_target_number)
        WHERE campaign_id IS NOT NULL AND normalized_target_number IS NOT NULL
    ''')
    cursor.execute('''
        CREATE TRIGGER IF NOT EXISTS target_payments_immutable_update
        BEFORE UPDATE ON target_payments
        BEGIN
            SELECT RAISE(ABORT, 'target payment identities are immutable');
        END
    ''')
    cursor.execute('''
        CREATE TRIGGER IF NOT EXISTS target_payments_immutable_delete
        BEFORE DELETE ON target_payments
        BEGIN
            SELECT RAISE(ABORT, 'target payment identities are immutable');
        END
    ''')

    # Backfill legacy assignments and immediately repair the reported stuck-task
    # state. Slots are derived from active assignments, so changing an overdue
    # assignment to expired makes its slot available without mutating paid counts.
    cursor.execute("UPDATE task_assignments SET started_at = COALESCE(started_at, assigned_at, CURRENT_TIMESTAMP)")
    cursor.execute('''
        UPDATE task_assignments
        SET expires_at = COALESCE(
            expires_at,
            datetime(started_at, '+' || COALESCE(
                (SELECT time_limit_minutes FROM tasks WHERE tasks.id = task_assignments.task_id),
                25
            ) || ' minutes')
        )
    ''')
    cursor.execute('''
        UPDATE task_assignments
        SET status = 'expired', slot_released = 1
        WHERE status = 'in_progress' AND expires_at <= CURRENT_TIMESTAMP AND slot_released = 0
    ''')
    cursor.execute("UPDATE task_assignments SET slot_released = 1 WHERE status IN ('expired', 'cancelled') AND slot_released = 0")

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS admin_assignment_release_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assignment_id INTEGER NOT NULL UNIQUE,
            admin_user_id INTEGER NOT NULL,
            worker_user_id INTEGER NOT NULL,
            task_id INTEGER NOT NULL,
            old_status TEXT NOT NULL,
            new_status TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (assignment_id) REFERENCES task_assignments(id),
            FOREIGN KEY (admin_user_id) REFERENCES users(id),
            FOREIGN KEY (worker_user_id) REFERENCES users(id),
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        )
    ''')

    # A recipient can appear once in a campaign and is held for seven days
    # before any different campaign can use it again.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS recipient_allocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            phone_number TEXT NOT NULL,
            assignment_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            last_contacted_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(campaign_id, phone_number),
            FOREIGN KEY (assignment_id) REFERENCES task_assignments(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS recipient_cooldown_lookup
        ON recipient_allocations(phone_number, last_contacted_at)
    ''')

    cursor.execute("PRAGMA table_info(tasks)")
    task_cols = [c[1] for c in cursor.fetchall()]
    if 'campaign_id' not in task_cols:
        cursor.execute("ALTER TABLE tasks ADD COLUMN campaign_id INTEGER")
    if 'recipients_per_slot' not in task_cols:
        cursor.execute("ALTER TABLE tasks ADD COLUMN recipients_per_slot INTEGER NOT NULL DEFAULT 0")
    task_rows = cursor.execute("SELECT id, target_numbers, campaign_id, recipients_per_slot FROM tasks").fetchall()
    for task_row in task_rows:
        try:
            recipient_count = len(json.loads(task_row['target_numbers']))
        except Exception:
            recipient_count = 1
        cursor.execute('''
            UPDATE tasks SET campaign_id = COALESCE(campaign_id, ?),
                recipients_per_slot = CASE WHEN recipients_per_slot <= 0 THEN ? ELSE recipients_per_slot END
            WHERE id = ?
        ''', (task_row['id'], recipient_count, task_row['id']))

    # Legacy campaigns often reuse one short recipient list for many slots.
    # Stop those campaigns until their source pool is expanded; otherwise they
    # would be unable to issue an allocation without spamming contacts.
    for task_row in cursor.execute("SELECT id, target_numbers, total_slots, recipients_per_slot, status FROM tasks").fetchall():
        try:
            unique_pool_size = len(set(str(number) for number in json.loads(task_row['target_numbers'])))
        except Exception:
            unique_pool_size = 0
        if task_row['status'] == 'active' and unique_pool_size < task_row['total_slots'] * task_row['recipients_per_slot']:
            cursor.execute('''
                UPDATE tasks
                SET status = 'paused',
                    proof_instructions = 'Campaign paused: add enough unique recipients for every slot before activation.'
                WHERE id = ?
            ''', (task_row['id'],))

    # Submissions Table (with OCR audit fields)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            task_id INTEGER NOT NULL,
            assignment_id INTEGER,
            proof_image TEXT NOT NULL,
            notes TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            admin_feedback TEXT,
            payout_amount REAL NOT NULL,
            audit_badge TEXT,
            audit_score REAL,
            numbers_detected INTEGER,
            numbers_required INTEGER,
            invalid_count INTEGER NOT NULL DEFAULT 0,
            approved_valid_count INTEGER,
            auto_approved INTEGER DEFAULT 0,
            submitted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            reviewed_at DATETIME,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        )
    ''')

    # Migration check: Ensure audit columns exist if table was already created
    cursor.execute("PRAGMA table_info(submissions)")
    existing_cols = [c[1] for c in cursor.fetchall()]
    if 'audit_badge' not in existing_cols:
        cursor.execute("ALTER TABLE submissions ADD COLUMN audit_badge TEXT")
    if 'audit_score' not in existing_cols:
        cursor.execute("ALTER TABLE submissions ADD COLUMN audit_score REAL")
    if 'numbers_detected' not in existing_cols:
        cursor.execute("ALTER TABLE submissions ADD COLUMN numbers_detected INTEGER")
    if 'numbers_required' not in existing_cols:
        cursor.execute("ALTER TABLE submissions ADD COLUMN numbers_required INTEGER")
    if 'auto_approved' not in existing_cols:
        cursor.execute("ALTER TABLE submissions ADD COLUMN auto_approved INTEGER DEFAULT 0")
    if 'invalid_count' not in existing_cols:
        cursor.execute("ALTER TABLE submissions ADD COLUMN invalid_count INTEGER NOT NULL DEFAULT 0")
    if 'approved_valid_count' not in existing_cols:
        cursor.execute("ALTER TABLE submissions ADD COLUMN approved_valid_count INTEGER")
    if 'required_count' not in existing_cols:
        cursor.execute("ALTER TABLE submissions ADD COLUMN required_count INTEGER")
    if 'verified_count' not in existing_cols:
        cursor.execute("ALTER TABLE submissions ADD COLUMN verified_count INTEGER")
    if 'approval_threshold' not in existing_cols:
        cursor.execute("ALTER TABLE submissions ADD COLUMN approval_threshold INTEGER")
    if 'per_number_rate' not in existing_cols:
        cursor.execute("ALTER TABLE submissions ADD COLUMN per_number_rate NUMERIC")
    if 'assignment_id' not in existing_cols:
        cursor.execute("ALTER TABLE submissions ADD COLUMN assignment_id INTEGER")
    if 'payout_paise' not in existing_cols:
        cursor.execute("ALTER TABLE submissions ADD COLUMN payout_paise INTEGER")
    if 'per_number_rate_paise' not in existing_cols:
        cursor.execute("ALTER TABLE submissions ADD COLUMN per_number_rate_paise INTEGER")

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS task_proofs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            submission_id INTEGER NOT NULL,
            file_name TEXT NOT NULL,
            sort_order INTEGER NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(submission_id, sort_order),
            FOREIGN KEY (submission_id) REFERENCES submissions(id)
        )
    ''')

    # Settings Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    ''')
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('auto_approve_enabled', '0')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('per_message_rate', '3')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('referral_limit_tasks', '20')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('referral_commission_percent', '10')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('minimum_withdrawal_amount', '50')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('usdt_inr_rate', '90')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('usdt_network', 'TRC20')")
    ensure_finance_settings_history(conn)

    # Wallet Transactions Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS wallet_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            amount REAL NOT NULL,
            balance_after REAL NOT NULL,
            reference_id TEXT,
            description TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')
    # Bind task rewards to their source submission so the database itself
    # rejects a second reward for the same submission. Keep nullable for
    # historical/non-task ledger rows.
    cursor.execute("PRAGMA table_info(wallet_transactions)")
    wallet_columns = [c[1] for c in cursor.fetchall()]
    if 'submission_id' not in wallet_columns:
        cursor.execute("ALTER TABLE wallet_transactions ADD COLUMN submission_id INTEGER")
    if 'amount_paise' not in wallet_columns:
        cursor.execute("ALTER TABLE wallet_transactions ADD COLUMN amount_paise INTEGER")
    if 'balance_after_paise' not in wallet_columns:
        cursor.execute("ALTER TABLE wallet_transactions ADD COLUMN balance_after_paise INTEGER")
    cursor.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS one_task_reward_per_submission
        ON wallet_transactions(submission_id)
        WHERE type = 'task_reward' AND submission_id IS NOT NULL
    ''')
    # One referral commission corresponds to exactly one approved submission.
    cursor.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS one_referral_commission_per_submission
        ON wallet_transactions(reference_id)
        WHERE type = 'referral_task_commission'
    ''')
    cursor.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS one_withdrawal_refund_per_withdrawal
        ON wallet_transactions(reference_id)
        WHERE type = 'withdrawal_refund'
    ''')
    # A referral commission for an approved submission can be reversed once.
    cursor.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS one_referral_reversal_per_submission
        ON wallet_transactions(reference_id)
        WHERE type = 'referral_reversal'
    ''')

    # Withdrawals Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            payout_method TEXT NOT NULL,
            upi_id TEXT,
            bank_account TEXT,
            bank_ifsc TEXT,
            bank_holder_name TEXT,
            status TEXT NOT NULL DEFAULT 'requested',
            utr_number TEXT,
            provider_reference TEXT,
            currency TEXT NOT NULL DEFAULT 'INR',
            usdt_amount NUMERIC,
            usdt_inr_rate NUMERIC,
            usdt_network TEXT,
            usdt_wallet_address TEXT,
            transaction_hash TEXT,
            failure_reason TEXT,
            requested_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            processed_at DATETIME,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')
    cursor.execute("PRAGMA table_info(withdrawals)")
    withdrawal_cols = [c[1] for c in cursor.fetchall()]
    if 'provider_reference' not in withdrawal_cols:
        cursor.execute("ALTER TABLE withdrawals ADD COLUMN provider_reference TEXT")
    if 'failure_reason' not in withdrawal_cols:
        cursor.execute("ALTER TABLE withdrawals ADD COLUMN failure_reason TEXT")
    for column, definition in (
        ('currency', "TEXT NOT NULL DEFAULT 'INR'"), ('usdt_amount', 'NUMERIC'),
        ('usdt_inr_rate', 'NUMERIC'), ('usdt_network', 'TEXT'),
        ('usdt_wallet_address', 'TEXT'), ('transaction_hash', 'TEXT')
    ):
        if column not in withdrawal_cols:
            cursor.execute(f"ALTER TABLE withdrawals ADD COLUMN {column} {definition}")
    cursor.execute("PRAGMA table_info(withdrawals)")
    withdrawal_cols = [c[1] for c in cursor.fetchall()]
    if 'amount_paise' not in withdrawal_cols:
        cursor.execute("ALTER TABLE withdrawals ADD COLUMN amount_paise INTEGER")
    if 'usdt_amount_micro' not in withdrawal_cols:
        cursor.execute("ALTER TABLE withdrawals ADD COLUMN usdt_amount_micro INTEGER")
    if 'usdt_inr_rate_scaled' not in withdrawal_cols:
        cursor.execute("ALTER TABLE withdrawals ADD COLUMN usdt_inr_rate_scaled INTEGER")
    ensure_withdrawal_identifier_indexes(conn)
    # Legacy values are mapped once to the explicit manual-payout state model.
    cursor.execute("UPDATE withdrawals SET status = 'requested' WHERE status = 'pending'")
    cursor.execute("UPDATE withdrawals SET status = 'paid' WHERE status = 'approved' AND COALESCE(utr_number, '') != ''")

    # OTP Verification Table (for mobile verification)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS otps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mobile TEXT NOT NULL,
            otp_code TEXT NOT NULL,
            otp_hash TEXT,
            expires_at DATETIME NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_attempt_at DATETIME
        )
    ''')
    cursor.execute("PRAGMA table_info(otps)")
    otp_cols = [c[1] for c in cursor.fetchall()]
    if 'otp_hash' not in otp_cols:
        cursor.execute("ALTER TABLE otps ADD COLUMN otp_hash TEXT")
    if 'attempt_count' not in otp_cols:
        cursor.execute("ALTER TABLE otps ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0")
    if 'last_attempt_at' not in otp_cols:
        cursor.execute("ALTER TABLE otps ADD COLUMN last_attempt_at DATETIME")
    # Request history is separate from usable OTPs so provider failures are
    # still rate-limited without leaving a verifiable code behind.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS otp_request_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mobile TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS mobile_verifications (
            mobile TEXT PRIMARY KEY,
            verified_at DATETIME NOT NULL,
            expires_at DATETIME NOT NULL,
            consumed_at DATETIME
        )
    ''')

    conn.commit()
    if is_development_environment():
        seed_data(conn)
    else:
        provision_initial_admin(conn)
    conn.close()

def provision_initial_admin(conn):
    """Provision an admin only on an empty production install with supplied secrets."""
    cursor = conn.cursor()
    if cursor.execute("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1").fetchone():
        return
    email = os.environ.get('TASKPE_ADMIN_EMAIL', '').strip().lower()
    password = os.environ.get('TASKPE_ADMIN_PASSWORD', '')
    mobile = ''.join(c for c in os.environ.get('TASKPE_ADMIN_MOBILE', '') if c.isdigit())
    if not email or not password or not mobile:
        return
    if len(password) < 12 or len(mobile) < 10:
        return
    referral_code = 'ADMIN' + ''.join(c for c in mobile[-3:] if c.isdigit())
    cursor.execute('''
        INSERT INTO users (name, mobile, email, password_hash, role, wallet_balance, total_earned, referral_code)
        VALUES (?, ?, ?, ?, 'admin', 0.0, 0.0, ?)
    ''', ('Initial Administrator', mobile, email, generate_password_hash(password), referral_code))
    conn.commit()

def seed_data(conn):
    cursor = conn.cursor()

    # Check if admin already exists
    cursor.execute("SELECT id FROM users WHERE email = 'admin@taskpe.com'")
    if cursor.fetchone():
        # Update existing Task 4 to 25 numbers if needed
        twenty_five_nums = [f"9197112233{i:02d}" for i in range(1, 26)]
        cursor.execute('''
            UPDATE tasks 
            SET title = 'Promote Grand Opening of Slice & Spice Bistro to 25 Numbers',
                description = 'Spread the word for a new cafe opening in Connaught Place with Buy 1 Get 1 Free voucher to 25 verified contacts.',
                target_numbers = ?,
                payout = 150.0,
                time_limit_minutes = 25,
                proof_instructions = 'Upload screenshot of chat window displaying sent text and numbers. AI OCR will automatically count 25/25 numbers.',
                total_slots = 50,
                completed_slots = 25
            WHERE id = 4 OR title LIKE '%Slice & Spice Bistro%'
        ''', (json.dumps(twenty_five_nums),))
        conn.commit()
        return

    # 1. Admin User
    admin_pw = generate_password_hash('admin123')
    cursor.execute('''
        INSERT INTO users (name, mobile, email, password_hash, role, wallet_balance, total_earned, referral_code)
        VALUES ('TaskPe Admin', '9876500000', 'admin@taskpe.com', ?, 'admin', 0.0, 0.0, 'ADMIN99')
    ''', (admin_pw,))

    # 2. Demo Earner Users
    user_pw = generate_password_hash('user123')
    cursor.execute('''
        INSERT INTO users (name, mobile, email, password_hash, role, wallet_balance, total_earned, referral_code)
        VALUES ('Rohan Sharma', '9876512345', 'earner@taskpe.com', ?, 'user', 140.0, 350.0, 'ROHAN100')
    ''', (user_pw,))
    demo_user_id = cursor.lastrowid

    cursor.execute('''
        INSERT INTO users (name, mobile, email, password_hash, role, wallet_balance, total_earned, referral_code, referred_by)
        VALUES ('Priya Patel', '9876523456', 'priya@taskpe.com', ?, 'user', 210.0, 420.0, 'PRIYA200', ?)
    ''', (user_pw, demo_user_id))

    cursor.execute('''
        INSERT INTO users (name, mobile, email, password_hash, role, wallet_balance, total_earned, referral_code, referred_by)
        VALUES ('Amit Kumar', '9876534567', 'amit@taskpe.com', ?, 'user', 70.0, 280.0, 'AMIT300', ?)
    ''', (user_pw, demo_user_id))

    # 25 Numbers list for Task 4
    twenty_five_numbers = [f"9197112233{i:02d}" for i in range(1, 26)]

    # 3. Sample WhatsApp Tasks
    tasks = [
        (
            'Send Diwali Mega Sale Promo to 10 Numbers',
            'Direct Message',
            'Send our Diwali promotional message with discount voucher link to 10 verified business numbers provided below.',
            70.0,
            '🔥 *Mega Festive Deal!* Get flat 60% off on all electronics + Extra 10% instant UPI cashback. Check here: https://bit.ly/festive-deal-2026. Use Coupon: *FESTIVE60*',
            json.dumps(['919811122233', '919822233344', '919833344455', '919844455566', '919855566677', '919866677788', '919877788899', '919888899900', '919899900011', '919800011122']),
            15,
            'Take a screenshot of WhatsApp chat history showing the message sent with double ticks or delivered status to at least 8 numbers.',
            50,
            12,
            'active'
        ),
        (
            'Broadcast AI & Python Free Masterclass to 8 Contacts',
            'Broadcast',
            'Forward our weekend Tech webinar registration link to 8 interested student or professional contacts.',
            80.0,
            '🚀 *Join Free Python & AI Masterclass!* Hosted by Google Deepmind alumni this Sunday at 6 PM. Limited spots: https://bit.ly/free-ai-class-2026. Forward to college groups!',
            json.dumps(['919123456780', '919123456781', '919123456782', '919123456783', '919123456784', '919123456785', '919123456786', '919123456787']),
            10,
            'Upload a screenshot of the broadcast list report or individual sent messages with timestamps.',
            30,
            7,
            'active'
        ),
        (
            'Share FastPay UPI App Invite to 5 Numbers',
            'App Invite',
            'Share the FastPay cashback referral link with 5 contacts looking to earn cashback on UPI transfers.',
            50.0,
            '💰 *Instant ₹50 UPI Cashback!* Download FastPay app and make your first transaction of ₹1 to get instant ₹50 in bank. Download link: https://fastpay.in/dl/TASKPE50',
            json.dumps(['919988776655', '919977665544', '919966554433', '919955443322', '919944332211']),
            8,
            'Submit 1 screenshot showing messages sent to contacts.',
            100,
            24,
            'active'
        ),
        (
            'Promote Grand Opening of Slice & Spice Bistro to 25 Numbers',
            'Direct Message',
            'Spread the word for a new cafe opening in Connaught Place with Buy 1 Get 1 Free voucher to 25 verified contacts.',
            150.0,
            '🍕 *Grand Opening Offer!* Slice & Spice Bistro is now open at CP! Enjoy Buy 1 Get 1 Free on all woodfired pizzas this weekend only. Show this message at counter!',
            json.dumps(twenty_five_numbers),
            25,
            'Upload screenshot of chat window displaying sent text and numbers. AI OCR will automatically count 25/25 numbers.',
            50,
            25,
            'active'
        ),
        (
            'Post WhatsApp Status for Luxury Villa Launch',
            'Status Share',
            'Upload the provided villa brochure text and contact number to your WhatsApp Status for 24 hours.',
            60.0,
            '🏡 *Luxury 3BHK Smart Villas starting ₹55 Lakhs!* Gated community with clubhouse & pool. Call/WhatsApp 9876500000 for VIP preview & site visit pass.',
            json.dumps(['919876500000']),
            15,
            'Upload screenshot of your WhatsApp Status showing view count after at least 15 minutes of posting.',
            60,
            15,
            'active'
        )
    ]

    for t in tasks:
        cursor.execute('''
            INSERT INTO tasks (title, category, description, payout, template_text, target_numbers, time_limit_minutes, proof_instructions, total_slots, completed_slots, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', t)

    # 4. Sample Submissions (with OCR Audit data)
    cursor.execute('''
        INSERT INTO submissions (user_id, task_id, proof_image, notes, status, admin_feedback, payout_amount, audit_badge, audit_score, numbers_detected, numbers_required, auto_approved, submitted_at, reviewed_at)
        VALUES (?, 1, 'sample_proof.png', 'Sent to all 10 numbers successfully.', 'approved', 'Verified. Great job!', 70.0, 'Auto-Verified: 10/10 numbers found (Match: 96%)', 96.0, 10, 10, 1, ?, ?)
    ''', (demo_user_id, (datetime.now() - timedelta(hours=4)).strftime('%Y-%m-%d %H:%M:%S'), (datetime.now() - timedelta(hours=3)).strftime('%Y-%m-%d %H:%M:%S')))

    cursor.execute('''
        INSERT INTO submissions (user_id, task_id, proof_image, notes, status, admin_feedback, payout_amount, audit_badge, audit_score, numbers_detected, numbers_required, auto_approved, submitted_at)
        VALUES (?, 4, 'sample_proof.png', 'Forwarded to 25 contacts as requested. Double ticks verified.', 'pending', NULL, 150.0, 'Auto-Verified: 25/25 numbers found (Match: 95%)', 95.0, 25, 25, 0, ?)
    ''', (demo_user_id, (datetime.now() - timedelta(minutes=25)).strftime('%Y-%m-%d %H:%M:%S')))

    # 5. Wallet Transactions for Demo Earner
    cursor.execute('''
        INSERT INTO wallet_transactions (user_id, type, amount, balance_after, reference_id, description, created_at)
        VALUES (?, 'task_reward', 70.0, 70.0, 'TASK-1', 'Earnings for: Send Diwali Mega Sale Promo to 10 Numbers', ?)
    ''', (demo_user_id, (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S')))

    cursor.execute('''
        INSERT INTO wallet_transactions (user_id, type, amount, balance_after, reference_id, description, created_at)
        VALUES (?, 'task_reward', 80.0, 150.0, 'TASK-2', 'Earnings for: Broadcast AI & Python Free Masterclass', ?)
    ''', (demo_user_id, (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')))

    cursor.execute('''
        INSERT INTO wallet_transactions (user_id, type, amount, balance_after, reference_id, description, created_at)
        VALUES (?, 'referral_bonus', 40.0, 190.0, 'REF-BONUS', 'Referral bonus from Priya Patel and Amit Kumar', ?)
    ''', (demo_user_id, (datetime.now() - timedelta(hours=12)).strftime('%Y-%m-%d %H:%M:%S')))

    cursor.execute('''
        INSERT INTO wallet_transactions (user_id, type, amount, balance_after, reference_id, description, created_at)
        VALUES (?, 'task_reward', 70.0, 260.0, 'TASK-1', 'Earnings for Task #1 second batch', ?)
    ''', (demo_user_id, (datetime.now() - timedelta(hours=6)).strftime('%Y-%m-%d %H:%M:%S')))

    cursor.execute('''
        INSERT INTO wallet_transactions (user_id, type, amount, balance_after, reference_id, description, created_at)
        VALUES (?, 'withdrawal', -120.0, 140.0, 'WDR-101', 'Withdrawal via UPI: rohan@oksbi', ?)
    ''', (demo_user_id, (datetime.now() - timedelta(hours=5)).strftime('%Y-%m-%d %H:%M:%S')))

    # 6. Sample Withdrawal Request
    cursor.execute('''
        INSERT INTO withdrawals (user_id, amount, payout_method, upi_id, status, utr_number, requested_at, processed_at)
        VALUES (?, 120.0, 'upi', 'rohan@oksbi', 'approved', 'UPI/2026/09087654321', ?, ?)
    ''', (demo_user_id, (datetime.now() - timedelta(hours=6)).strftime('%Y-%m-%d %H:%M:%S'), (datetime.now() - timedelta(hours=5)).strftime('%Y-%m-%d %H:%M:%S')))

    # Pending withdrawal from Priya
    cursor.execute('''
        INSERT INTO withdrawals (user_id, amount, payout_method, upi_id, status, requested_at)
        VALUES (3, 150.0, 'upi', 'priya@paytm', 'pending', ?)
    ''', ((datetime.now() - timedelta(minutes=45)).strftime('%Y-%m-%d %H:%M:%S'),))

    conn.commit()

if __name__ == '__main__':
    init_db()
    print("Database initialized, migrated, and seeded successfully.")
