import sqlite3
import json
import os
import re
import hashlib
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from werkzeug.security import generate_password_hash, check_password_hash
from money import inr_to_paise
import phonenumbers
from phonenumbers.phonenumberutil import NumberParseException

_IS_DEVELOPMENT = os.environ.get('ENVIRONMENT', 'production').strip().lower() == 'development'
_configured_db_path = os.environ.get('TASKPE_DB_PATH', '').strip()
if not _IS_DEVELOPMENT and not _configured_db_path:
    raise RuntimeError('TASKPE_DB_PATH must be set to a persistent database path when ENVIRONMENT=production.')
if _configured_db_path and not os.path.isabs(_configured_db_path):
    raise RuntimeError('TASKPE_DB_PATH must be an absolute path.')
DB_PATH = _configured_db_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'taskpe.db')

REQUIRED_SCHEMA = {
    'exact_money_settings': {'key', 'value_scaled', 'scale', 'kind', 'migrated_at'},
    'users': {'id', 'name', 'mobile', 'email', 'password_hash', 'role', 'wallet_balance', 'total_earned', 'referral_code', 'referred_by', 'referred_user_task_count', 'signup_method', 'created_at', 'wallet_balance_paise', 'total_earned_paise'},
    'tasks': {'id', 'title', 'category', 'task_type', 'description', 'payout', 'payout_paise', 'reward_per_number_paise', 'reward_source', 'template_text', 'image_path', 'destination_link', 'target_numbers', 'recipient_region', 'campaign_id', 'owner_user_id', 'campaign_review_status', 'campaign_approved_by', 'campaign_approved_at', 'campaign_approval_fingerprint', 'recipients_per_slot', 'time_limit_minutes', 'proof_instructions', 'total_slots', 'completed_slots', 'status', 'created_at'},
    'task_assignments': {'id', 'user_id', 'task_id', 'campaign_id', 'campaign_type', 'assigned_numbers', 'status', 'assigned_at', 'started_at', 'expires_at', 'slot_released', 'completed_at'},
    'target_number_records': {'id', 'assignment_id', 'task_id', 'campaign_id', 'user_id', 'target_number', 'normalized_target_number', 'status', 'reward_per_number', 'reward_per_number_paise', 'verified_at', 'paid_at', 'paid_amount', 'paid_amount_paise', 'created_at', 'updated_at'},
    'target_payments': {'id', 'target_number_record_id', 'assignment_id', 'task_id', 'campaign_id', 'user_id', 'submission_id', 'target_number', 'normalized_target_number', 'amount', 'amount_paise', 'rate', 'rate_paise', 'payment_reference', 'paid_at'},
    'message_audit_records': {'id', 'task_id', 'campaign_id', 'assignment_id', 'user_id', 'target_number_record_id', 'recipient_allocation_id', 'recipient_number', 'normalized_recipient_number', 'sender_number', 'message_text', 'status', 'first_opened_at', 'last_opened_at', 'open_count', 'submission_id', 'created_at', 'updated_at'},
    'whatsapp_campaign_plans': {'id', 'plan_key', 'request_fingerprint', 'recipient_fingerprint', 'task_id', 'campaign_id', 'sender_worker_id', 'content_reference', 'recipient_count', 'requested_quantity', 'state', 'batch_id', 'batch_request_fingerprint', 'batch_content_type', 'batch_state', 'batch_updated_at', 'dispatch_state', 'dispatch_operation_id', 'dispatch_started_at', 'cancellation_state', 'cancellation_evidence_status', 'cancellation_requested_at', 'cancellation_checked_at', 'created_at'},
    'whatsapp_campaign_plan_recipients': {'id', 'plan_id', 'recipient_number', 'position', 'idempotency_key', 'state', 'reservation_claim_id', 'reservation_lease_until', 'ledger_message_id', 'failure_code', 'created_at', 'updated_at'},
    'whatsapp_campaign_recipient_claims': {'id', 'campaign_id', 'task_id', 'recipient_number', 'plan_id', 'plan_recipient_id', 'state', 'created_at', 'updated_at'},
    'shop_consent_declarations': {'id', 'shop_name', 'uploader_user_id', 'source', 'recipient_count', 'declaration_text', 'created_at'},
    'recipient_consent_records': {'id', 'phone_number', 'declaration_id', 'status', 'created_at'},
    'whatsapp_opt_outs': {'id', 'phone_number', 'reason', 'opted_out_at'},
    'whatsapp_opt_out_operations': {'request_id', 'actor_user_id', 'phone_number', 'reason', 'state', 'created_at', 'updated_at'},
    'whatsapp_campaign_consent_declarations': {'id', 'task_id', 'campaign_id', 'business_user_id', 'declaration_text', 'normalized_recipient_list', 'recipient_count', 'recipient_fingerprint', 'created_at'},
    'campaign_inquiries': {'id', 'company_name', 'contact_person', 'email', 'phone', 'task_type', 'target_location', 'participant_count', 'campaign_description', 'estimated_budget', 'created_at', 'status'},
    'admin_assignment_release_audit': {'id', 'assignment_id', 'admin_user_id', 'worker_user_id', 'task_id', 'old_status', 'new_status', 'reason', 'created_at'},
    'recipient_allocations': {'id', 'campaign_id', 'phone_number', 'assignment_id', 'user_id', 'last_contacted_at', 'status'},
    'submissions': {'id', 'user_id', 'task_id', 'assignment_id', 'proof_image', 'notes', 'status', 'admin_feedback', 'payout_amount', 'audit_badge', 'audit_score', 'numbers_detected', 'numbers_required', 'invalid_count', 'approved_valid_count', 'auto_approved', 'submitted_at', 'reviewed_at', 'required_count', 'verified_count', 'approval_threshold', 'per_number_rate', 'payout_paise', 'per_number_rate_paise'},
    'task_proofs': {'id', 'submission_id', 'file_name', 'sort_order', 'created_at'},
    'settings': {'key', 'value'},
    'finance_settings_history': {'id', 'setting_name', 'old_value', 'new_value', 'changed_by', 'changed_at', 'unit', 'reason'},
    'wallet_transactions': {'id', 'user_id', 'type', 'amount', 'balance_after', 'reference_id', 'description', 'created_at', 'submission_id', 'amount_paise', 'balance_after_paise'},
    'withdrawals': {'id', 'user_id', 'amount', 'payout_method', 'upi_id', 'bank_account', 'bank_ifsc', 'bank_holder_name', 'status', 'utr_number', 'provider_reference', 'currency', 'usdt_amount', 'usdt_inr_rate', 'usdt_network', 'usdt_wallet_address', 'transaction_hash', 'failure_reason', 'requested_at', 'processed_at', 'amount_paise', 'usdt_amount_micro', 'usdt_inr_rate_scaled'},
    'otps': {'id', 'mobile', 'otp_code', 'expires_at', 'created_at', 'otp_hash', 'attempt_count', 'last_attempt_at'},
    'otp_request_log': {'id', 'mobile', 'created_at'},
    'mobile_verifications': {'mobile', 'verified_at', 'expires_at', 'consumed_at'},
    'password_reset_tokens': {'id', 'user_id', 'mobile', 'token_hash', 'expires_at', 'used_at', 'verified_at', 'created_at'},
    'device_registrations': {'id', 'device_id_hash', 'signup_method', 'user_id', 'created_at'},
    'email_verifications': {'id', 'email', 'token_hash', 'expires_at', 'used_at', 'created_at'},
}

REQUIRED_INDEXES = {
    'one_active_campaign_per_user',
    'target_number_records_assignment_status',
    'target_number_records_normalized_lookup',
    'target_payments_submission_lookup',
    'message_audit_assignment_recipient_unique',
    'message_audit_report_lookup',
    'whatsapp_campaign_plans_batch_id_unique',
    'whatsapp_campaign_plan_recipients_lookup',
    'whatsapp_campaign_plan_recipients_reservation_lookup',
    'whatsapp_campaign_plan_recipient_ledger_id_unique',
    'whatsapp_campaign_recipient_claims_lookup',
    'whatsapp_campaign_recipient_claims_active_unique',
    'shop_consent_declarations_uploader',
    'recipient_consent_phone_status',
    'whatsapp_opt_outs_phone_unique',
    'whatsapp_opt_out_operations_actor',
    'whatsapp_campaign_consent_lookup',
    'one_target_payment_per_campaign_number',
    'recipient_allocations_one_active',
    'recipient_allocations_campaign_state',
    'one_task_reward_per_submission',
    'one_referral_commission_per_submission',
    'one_withdrawal_refund_per_withdrawal',
    'one_referral_reversal_per_submission',
    'withdrawal_provider_reference_unique',
    'withdrawal_transaction_hash_unique',
    'finance_settings_history_setting_name',
    'finance_settings_history_changed_at',
    'finance_settings_history_changed_by',
}

REQUIRED_UNIQUE_INDEXES = {
    'whatsapp_opt_outs_phone_unique',
}

REQUIRED_CONSTRAINT_PATTERNS = {
    'recipient_consent_records': (
        r"check\s*\(\s*status\s+in\s*\(\s*'shop_attested'\s*,\s*'revoked'\s*\)\s*\)",
    ),
    'whatsapp_opt_out_operations': (
        r"check\s*\(\s*state\s+in\s*\(\s*'pending'\s*,\s*'unknown'\s*,\s*'node_confirmed'\s*,\s*'applied'\s*,\s*'failed'\s*\)\s*\)",
    ),
    'whatsapp_campaign_consent_declarations': (
        r'check\s*\(\s*recipient_count\s*>\s*0\s*\)',
        r'check\s*\(\s*length\s*\(\s*recipient_fingerprint\s*\)\s*=\s*64\s*\)',
    ),
}

WHATSAPP_PLAN_RECIPIENT_KEY_PREFIX = 'taskpe-wa-plan-recipient-v1:'
WHATSAPP_LEDGER_TOKEN_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$')


def whatsapp_campaign_plan_recipient_idempotency_key(plan_id, plan_recipient_id):
    """Return the canonical fixed-length key shared with the Node ledger."""
    if (isinstance(plan_id, bool) or not isinstance(plan_id, int) or plan_id < 1 or
            isinstance(plan_recipient_id, bool) or not isinstance(plan_recipient_id, int) or
            plan_recipient_id < 1):
        raise ValueError('Invalid campaign plan recipient identity.')
    value = f'{WHATSAPP_PLAN_RECIPIENT_KEY_PREFIX}{plan_id}:{plan_recipient_id}'.encode('utf-8')
    return hashlib.sha256(value).hexdigest()


def is_valid_whatsapp_ledger_token(value):
    return isinstance(value, str) and bool(WHATSAPP_LEDGER_TOKEN_PATTERN.fullmatch(value))


def _whatsapp_ledger_path():
    configured = os.environ.get('TASKPE_WA_LEDGER_DB_PATH', '').strip()
    if configured and not os.path.isabs(configured):
        raise RuntimeError('TASKPE_WA_LEDGER_DB_PATH must be an absolute path.')
    return configured or os.path.join(tempfile.gettempdir(), 'taskpe-wa-outgoing.sqlite3')


def _legacy_ledger_references(old_keys):
    """Fail closed if a Node ledger still references a key being replaced."""
    if not old_keys:
        return
    path = _whatsapp_ledger_path()
    if not os.path.isfile(path):
        raise RuntimeError(
            'Unable to inspect the WhatsApp ledger before legacy key migration: database file is unavailable.'
        )
    uri = 'file:' + path.replace('\\', '/') + '?mode=ro'
    ledger = None
    try:
        ledger = sqlite3.connect(uri, uri=True)
        table = ledger.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='outgoing_messages'"
        ).fetchone()
        if not table:
            raise RuntimeError(
                'Unable to inspect the WhatsApp ledger before legacy key migration: outgoing_messages table is missing.'
            )
        placeholders = ','.join('?' for _ in old_keys)
        query = f'''
            SELECT 1 FROM outgoing_messages
            WHERE idempotency_key IN ({placeholders})
            LIMIT 1
        '''
        if ledger.execute(query, tuple(old_keys)).fetchone():
            raise RuntimeError(
                'Unable to migrate legacy campaign keys: Node ledger outgoing_messages contains active references to old keys.'
            )
    finally:
        if ledger:
            ledger.close()


def _duplicate_campaign_ledger_message_ids(conn):
    return conn.execute('''
        SELECT ledger_message_id, COUNT(*) AS count
        FROM whatsapp_campaign_plan_recipients
        WHERE ledger_message_id IS NOT NULL
        GROUP BY ledger_message_id
        HAVING COUNT(*) > 1
    ''').fetchall()


def migrate_whatsapp_campaign_plan_identity(conn):
    """Backfill canonical keys and install ledger-ID integrity guards.

    This runs inside the caller's initialization transaction. It never deletes
    or rewrites plan identities, positions, recipients, or ledger rows.
    """
    duplicate_ids = _duplicate_campaign_ledger_message_ids(conn)
    if duplicate_ids:
        raise RuntimeError(
            'Duplicate non-null WhatsApp ledger message IDs exist; resolve them before initialization.'
        )
    rows = conn.execute('''
        SELECT id, plan_id, idempotency_key
        FROM whatsapp_campaign_plan_recipients
        ORDER BY id
    ''').fetchall()
    replacements = []
    old_keys = []
    for row in rows:
        expected = whatsapp_campaign_plan_recipient_idempotency_key(row['plan_id'], row['id'])
        if row['idempotency_key'] != expected:
            replacements.append((expected, row['id']))
            old_keys.append(row['idempotency_key'])
    _legacy_ledger_references(old_keys)
    for expected, row_id in replacements:
        conn.execute('''
            UPDATE whatsapp_campaign_plan_recipients
            SET idempotency_key = ?
            WHERE id = ?
        ''', (expected, row_id))
    conn.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS whatsapp_campaign_plan_recipient_ledger_id_unique
        ON whatsapp_campaign_plan_recipients(ledger_message_id)
        WHERE ledger_message_id IS NOT NULL
    ''')


def migrate_legacy_business_campaign_review_state(conn):
    """Quarantine legacy business campaigns until a real review succeeds.

    Ownership comes from the persisted owner relationship and business role.
    This additive, guarded update creates no consent, fingerprint, approver,
    or timestamp and is safe to repeat.
    """
    cursor = conn.execute('''
        UPDATE tasks
        SET status = 'pending_review', campaign_review_status = 'pending_review'
        WHERE status = 'active'
          AND campaign_review_status = 'not_required'
          AND owner_user_id IS NOT NULL
          AND owner_user_id IN (SELECT id FROM users WHERE role = 'business')
    ''')
    return cursor.rowcount

REQUIRED_TRIGGERS = {
    'paid_target_record_immutable_update',
    'paid_target_record_immutable_delete',
    'target_payments_immutable_update',
    'target_payments_immutable_delete',
    'finance_settings_history_no_update',
    'finance_settings_history_no_delete',
}

def normalize_legacy_target_number(value):
    """Return the historical digit-only storage key for legacy target rows."""
    return ''.join(character for character in str(value or '') if character.isdigit())


def normalize_target_number(value, region=None):
    """Return the canonical, validated E.164 representation of a number."""
    return normalize_recipient_number(value, region=region)


def validate_phone_region(region):
    """Return an explicit libphonenumber region or reject missing/unknown input."""
    if region is None or not isinstance(region, str) or not region.strip():
        return None
    normalized = region.strip().upper()
    if normalized not in phonenumbers.SUPPORTED_REGIONS:
        raise ValueError('Unsupported phone-number region.')
    return normalized


def normalize_recipient_number(value, region=None):
    """Normalize a recipient to E.164 without guessing a national region.

    International ``+`` and ``00`` forms carry their own country code. National
    forms require an explicit ISO-3166-1 alpha-2 region. Existing stored values
    are not rewritten by this API; callers should fail closed when they cannot
    be interpreted unambiguously.
    """
    raw = str(value or '').strip()
    if not raw:
        raise ValueError('A recipient phone number is required.')
    explicit_region = validate_phone_region(region)
    try:
        if raw.startswith('00'):
            parsed = phonenumbers.parse('+' + raw[2:], None)
        elif raw.startswith('+'):
            parsed = phonenumbers.parse(raw, None)
        else:
            if not explicit_region:
                raise ValueError('An explicit phone-number region is required for national format.')
            parsed = phonenumbers.parse(raw, explicit_region)
        if not phonenumbers.is_possible_number(parsed) or not phonenumbers.is_valid_number(parsed):
            raise ValueError('Invalid recipient phone number.')
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except NumberParseException as exc:
        raise ValueError('Invalid recipient phone number.') from exc

def is_development_environment():
    """Only an explicit development setting may create sample credentials/data."""
    return os.environ.get('ENVIRONMENT', 'production').strip().lower() == 'development'

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def schema_problems(conn):
    """Return precise schema gaps without creating or changing anything."""
    actual_tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    missing_tables = sorted(set(REQUIRED_SCHEMA) - actual_tables)
    missing_columns = []
    for table, required_columns in REQUIRED_SCHEMA.items():
        if table not in actual_tables:
            continue
        actual_columns = {
            row[1] for row in conn.execute(f'PRAGMA table_info({table})')
        }
        for column in sorted(required_columns - actual_columns):
            missing_columns.append(f'{table}.{column}')

    actual_indexes = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name IS NOT NULL"
        )
    }
    missing_indexes = sorted(REQUIRED_INDEXES - actual_indexes)
    non_unique_indexes = []
    for index_name in sorted(REQUIRED_UNIQUE_INDEXES & actual_indexes):
        # Resolve the owning table directly; PRAGMA index_list does not expose it.
        owner = conn.execute(
            "SELECT tbl_name FROM sqlite_master WHERE type = 'index' AND name = ?",
            (index_name,),
        ).fetchone()
        if owner:
            for row in conn.execute('PRAGMA index_list(' + repr(owner[0]) + ')').fetchall():
                if row[1] == index_name and not row[2]:
                    non_unique_indexes.append(index_name)
    actual_triggers = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        )
    }
    missing_triggers = sorted(REQUIRED_TRIGGERS - actual_triggers)
    missing_constraints = []
    for table, patterns in REQUIRED_CONSTRAINT_PATTERNS.items():
        if table not in actual_tables:
            continue
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        sql = re.sub(r'\s+', ' ', (row[0] if row else '').lower())
        for pattern in patterns:
            if not re.search(pattern, sql):
                missing_constraints.append(f'{table}: {pattern}')
    return {
        'tables': missing_tables,
        'columns': missing_columns,
        'indexes': missing_indexes,
        'non_unique_indexes': sorted(non_unique_indexes),
        'triggers': missing_triggers,
        'constraints': missing_constraints,
    }


def verify_required_schema(conn=None):
    """Verify the deployed schema without creating or changing anything."""
    if conn is None and not os.path.isfile(DB_PATH):
        raise RuntimeError(
            f'Database does not exist at {DB_PATH}; initialize or migrate the '
            'configured database before starting the application.'
        )
    owns_connection = conn is None
    connection = conn or get_db_connection()
    try:
        problems = []
        for kind, values in schema_problems(connection).items():
            if values:
                problems.append(f'{kind}: ' + ', '.join(values))
        if problems:
            raise RuntimeError(
                'Database schema is incomplete; run the documented explicit '
                'initialization/migrations before starting the application. Missing '
                + '; '.join(problems)
            )
        return True
    finally:
        if owns_connection:
            connection.close()


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
    conn.execute('''CREATE INDEX IF NOT EXISTS finance_settings_history_setting_name ON finance_settings_history(setting_name)''')
    conn.execute('''CREATE INDEX IF NOT EXISTS finance_settings_history_changed_at ON finance_settings_history(changed_at)''')
    conn.execute('''CREATE INDEX IF NOT EXISTS finance_settings_history_changed_by ON finance_settings_history(changed_by)''')
    conn.execute('''
        CREATE TRIGGER IF NOT EXISTS finance_settings_history_no_update
        BEFORE UPDATE ON finance_settings_history BEGIN
            SELECT RAISE(ABORT, 'finance settings history is immutable');
        END
    ''')
    conn.execute('''
        CREATE TRIGGER IF NOT EXISTS finance_settings_history_no_delete
        BEFORE DELETE ON finance_settings_history BEGIN
            SELECT RAISE(ABORT, 'finance settings history is immutable');
        END
    ''')


def ensure_task_type_and_reward_fields(conn):
    """Add task content/reward snapshots without rewriting financial history."""
    columns = {row[1] for row in conn.execute('PRAGMA table_info(tasks)')}
    additions = {
        'task_type': 'TEXT',
        'image_path': 'TEXT',
        'destination_link': 'TEXT',
        'recipient_region': 'TEXT',
        'reward_per_number_paise': 'INTEGER',
        'reward_source': "TEXT DEFAULT 'legacy_snapshot'",
    }
    for column, definition in additions.items():
        if column not in columns:
            conn.execute(f'ALTER TABLE tasks ADD COLUMN {column} {definition}')

    rate_row = conn.execute(
        "SELECT value FROM settings WHERE key = 'per_message_rate'"
    ).fetchone()
    try:
        default_rate_paise = inr_to_paise(rate_row[0]) if rate_row else 300
    except (TypeError, ValueError, ArithmeticError):
        default_rate_paise = 300

    rows = conn.execute('''
        SELECT id, category, payout, payout_paise, recipients_per_slot,
               task_type, reward_per_number_paise, reward_source
        FROM tasks
    ''').fetchall()
    for row in rows:
        task_type = row['task_type'] or (
            'IMAGE_TEXT_LINK' if row['category'] == 'IMAGE_TEXT_LINK' else 'TEXT_LINK'
        )
        reward_paise = row['reward_per_number_paise']
        if reward_paise is None:
            count = int(row['recipients_per_slot'] or 0)
            if row['payout_paise'] is not None and count > 0:
                reward_paise = int(row['payout_paise']) // count
            elif row['payout'] is not None and count > 0:
                reward_paise = int(
                    (Decimal(str(row['payout'])) * 100 / count)
                    .quantize(Decimal('1'), rounding=ROUND_HALF_UP)
                )
            else:
                reward_paise = default_rate_paise
        conn.execute('''
            UPDATE tasks
            SET task_type = COALESCE(task_type, ?),
                reward_per_number_paise = COALESCE(reward_per_number_paise, ?),
                reward_source = COALESCE(reward_source, 'legacy_snapshot')
            WHERE id = ?
        ''', (task_type, reward_paise, row['id']))

def ensure_recipient_allocation_history(conn):
    """Make recipient allocations append-only events with reusable releases."""
    table = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='recipient_allocations'").fetchone()
    if table:
        columns = {row[1] for row in conn.execute('PRAGMA table_info(recipient_allocations)')}
        has_legacy_unique = 'UNIQUE(CAMPAIGN_ID,PHONE_NUMBER)' in (table[0] or '').replace(' ', '').upper()
        if 'status' not in columns or has_legacy_unique:
            conn.execute('ALTER TABLE recipient_allocations RENAME TO recipient_allocations_legacy')
            conn.execute('''
                CREATE TABLE recipient_allocations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    campaign_id INTEGER NOT NULL,
                    phone_number TEXT NOT NULL,
                    assignment_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    last_contacted_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    status TEXT NOT NULL DEFAULT 'ALLOCATED'
                        CHECK (status IN ('ALLOCATED', 'COMPLETED', 'RELEASED')),
                    FOREIGN KEY (assignment_id) REFERENCES task_assignments(id),
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
            ''')
            conn.execute('''
                INSERT INTO recipient_allocations
                    (id, campaign_id, phone_number, assignment_id, user_id, last_contacted_at, status)
                SELECT id, campaign_id, phone_number, assignment_id, user_id, last_contacted_at, 'ALLOCATED'
                FROM recipient_allocations_legacy
            ''')
            conn.execute('DROP TABLE recipient_allocations_legacy')
    conn.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS recipient_allocations_one_active
        ON recipient_allocations(campaign_id, phone_number)
        WHERE status = 'ALLOCATED'
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS recipient_allocations_campaign_state
        ON recipient_allocations(campaign_id, phone_number, status)
    ''')
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='submissions'").fetchone():
        conn.execute('''
            UPDATE recipient_allocations
            SET status = 'RELEASED'
            WHERE status = 'ALLOCATED'
              AND assignment_id IN (
                  SELECT ta.id FROM task_assignments ta
                  WHERE ta.status IN ('expired', 'cancelled')
                    AND NOT EXISTS (SELECT 1 FROM submissions s WHERE s.assignment_id = ta.id)
              )
        ''')
def _initialize_database_with_connection(conn):
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
    if 'signup_method' not in user_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN signup_method TEXT")

    # Tasks Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'Direct Message',
            task_type TEXT NOT NULL DEFAULT 'TEXT_LINK',
            description TEXT NOT NULL,
            payout REAL NOT NULL,
            reward_per_number_paise INTEGER,
            reward_source TEXT NOT NULL DEFAULT 'legacy_snapshot',
            template_text TEXT NOT NULL,
            image_path TEXT,
            destination_link TEXT,
            target_numbers TEXT NOT NULL,
            recipient_region TEXT,
            campaign_id INTEGER,
            owner_user_id INTEGER,
            campaign_review_status TEXT NOT NULL DEFAULT 'not_required',
            campaign_approved_by INTEGER,
            campaign_approved_at DATETIME,
            campaign_approval_fingerprint TEXT,
            recipients_per_slot INTEGER NOT NULL DEFAULT 0,
            time_limit_minutes INTEGER NOT NULL DEFAULT 15,
            proof_instructions TEXT NOT NULL,
            total_slots INTEGER NOT NULL DEFAULT 100,
            completed_slots INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute("PRAGMA table_info(tasks)")
    task_columns = {row[1] for row in cursor.fetchall()}
    if 'owner_user_id' not in task_columns:
        cursor.execute('ALTER TABLE tasks ADD COLUMN owner_user_id INTEGER')
    for column, definition in {
        'campaign_review_status': "TEXT NOT NULL DEFAULT 'not_required'",
        'campaign_approved_by': 'INTEGER',
        'campaign_approved_at': 'DATETIME',
        'campaign_approval_fingerprint': 'TEXT',
    }.items():
        if column not in task_columns:
            cursor.execute(f'ALTER TABLE tasks ADD COLUMN {column} {definition}')
    cursor.execute('CREATE INDEX IF NOT EXISTS tasks_owner_status ON tasks(owner_user_id, status, created_at)')

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
            status TEXT NOT NULL DEFAULT 'ALLOCATED'
                CHECK (status IN ('ALLOCATED', 'COMPLETED', 'RELEASED')),
            FOREIGN KEY (assignment_id) REFERENCES task_assignments(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS recipient_cooldown_lookup
        ON recipient_allocations(phone_number, last_contacted_at)
    ''')
    ensure_recipient_allocation_history(conn)

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

    # Non-invasive per-recipient audit trail for the existing manual WhatsApp
    # workflow. Repeated opens of the same recipient within one assignment are
    # idempotent; reuse in a later assignment creates a separate history row.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS message_audit_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            campaign_id INTEGER,
            assignment_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            target_number_record_id INTEGER,
            recipient_allocation_id INTEGER,
            recipient_number TEXT NOT NULL,
            normalized_recipient_number TEXT NOT NULL,
            sender_number TEXT,
            message_text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'OPENED'
                CHECK (status IN ('OPENED', 'ATTEMPTED')),
            first_opened_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_opened_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            open_count INTEGER NOT NULL DEFAULT 1,
            submission_id INTEGER,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (assignment_id, normalized_recipient_number),
            FOREIGN KEY (task_id) REFERENCES tasks(id),
            FOREIGN KEY (assignment_id) REFERENCES task_assignments(id),
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (target_number_record_id) REFERENCES target_number_records(id),
            FOREIGN KEY (recipient_allocation_id) REFERENCES recipient_allocations(id),
            FOREIGN KEY (submission_id) REFERENCES submissions(id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS campaign_inquiries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_name TEXT NOT NULL,
            contact_person TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT,
            task_type TEXT NOT NULL,
            target_location TEXT,
            participant_count INTEGER,
            campaign_description TEXT NOT NULL,
            estimated_budget TEXT,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            status TEXT NOT NULL DEFAULT 'new'
                CHECK (status IN ('new', 'contacted', 'closed'))
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS campaign_inquiries_status_created ON campaign_inquiries(status, created_at)')
    cursor.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS message_audit_assignment_recipient_unique
        ON message_audit_records(assignment_id, normalized_recipient_number)
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS message_audit_report_lookup
        ON message_audit_records(campaign_id, task_id, user_id, first_opened_at)
    ''')

    # Phase 2: durable, pre-send audience snapshots.  This is deliberately
    # separate from the Node WhatsApp ledger and stores no message content,
    # credentials, QR data, or media.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS whatsapp_campaign_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_key TEXT NOT NULL UNIQUE,
            request_fingerprint TEXT NOT NULL,
            recipient_fingerprint TEXT,
            task_id INTEGER NOT NULL,
            campaign_id INTEGER NOT NULL,
            sender_worker_id INTEGER NOT NULL,
            content_reference TEXT NOT NULL,
            recipient_count INTEGER NOT NULL CHECK (recipient_count > 0),
            requested_quantity TEXT,
            state TEXT NOT NULL DEFAULT 'creating'
                CHECK (state IN ('creating', 'ready', 'cancelled')),
            batch_id TEXT UNIQUE,
            batch_request_fingerprint TEXT,
            batch_content_type TEXT NOT NULL DEFAULT 'text',
            batch_state TEXT NOT NULL DEFAULT 'pending'
                CHECK (batch_state IN ('pending', 'reserving', 'reserved', 'unknown', 'failed')),
            batch_updated_at DATETIME,
            dispatch_state TEXT NOT NULL DEFAULT 'not_started'
                CHECK (dispatch_state IN ('not_started', 'in_progress', 'unknown', 'complete')),
            dispatch_operation_id TEXT,
            dispatch_started_at DATETIME,
            cancellation_state TEXT NOT NULL DEFAULT 'not_requested',
            cancellation_evidence_status TEXT,
            cancellation_requested_at DATETIME,
            cancellation_checked_at DATETIME,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (task_id) REFERENCES tasks(id),
            FOREIGN KEY (sender_worker_id) REFERENCES users(id)
        )
    ''')
    plan_columns = {
        row[1] for row in cursor.execute('PRAGMA table_info(whatsapp_campaign_plans)')
    }
    plan_additions = {
        'batch_id': 'TEXT',
        'recipient_fingerprint': 'TEXT',
        'batch_request_fingerprint': 'TEXT',
        'batch_content_type': "TEXT NOT NULL DEFAULT 'text'",
        'requested_quantity': 'TEXT',
        'batch_state': "TEXT NOT NULL DEFAULT 'pending'",
        'batch_updated_at': 'DATETIME',
        'dispatch_state': "TEXT NOT NULL DEFAULT 'not_started'",
        'dispatch_operation_id': 'TEXT',
        'dispatch_started_at': 'DATETIME',
        'cancellation_state': "TEXT NOT NULL DEFAULT 'not_requested'",
        'cancellation_evidence_status': 'TEXT',
        'cancellation_requested_at': 'DATETIME',
        'cancellation_checked_at': 'DATETIME',
    }
    for column, definition in plan_additions.items():
        if column not in plan_columns:
            cursor.execute(f'ALTER TABLE whatsapp_campaign_plans ADD COLUMN {column} {definition}')
    cursor.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS whatsapp_campaign_plans_batch_id_unique
        ON whatsapp_campaign_plans(batch_id) WHERE batch_id IS NOT NULL
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS whatsapp_campaign_plan_recipients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id INTEGER NOT NULL,
            recipient_number TEXT NOT NULL,
            position INTEGER NOT NULL CHECK (position > 0),
            idempotency_key TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL DEFAULT 'planned'
                CHECK (state IN ('planned', 'reserving', 'reserved', 'attempted', 'acknowledged', 'failed', 'delivered_unknown')),
            reservation_claim_id TEXT,
            reservation_lease_until DATETIME,
            ledger_message_id TEXT,
            failure_code TEXT,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (plan_id, position),
            UNIQUE (plan_id, recipient_number),
            FOREIGN KEY (plan_id) REFERENCES whatsapp_campaign_plans(id)
        )
    ''')
    plan_recipient_columns = {
        row[1] for row in cursor.execute('PRAGMA table_info(whatsapp_campaign_plan_recipients)')
    }
    additions = {
        'state': "TEXT NOT NULL DEFAULT 'planned'",
        'reservation_claim_id': 'TEXT',
        'reservation_lease_until': 'DATETIME',
        'ledger_message_id': 'TEXT',
        'failure_code': 'TEXT',
        'updated_at': 'DATETIME',
    }
    for column, definition in additions.items():
        if column not in plan_recipient_columns:
            cursor.execute(f'ALTER TABLE whatsapp_campaign_plan_recipients ADD COLUMN {column} {definition}')
    cursor.execute('''
        UPDATE whatsapp_campaign_plan_recipients
        SET updated_at = COALESCE(updated_at, created_at, CURRENT_TIMESTAMP)
        WHERE updated_at IS NULL
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS whatsapp_campaign_plan_recipients_lookup
        ON whatsapp_campaign_plan_recipients(plan_id, position)
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS whatsapp_campaign_plan_recipients_reservation_lookup
        ON whatsapp_campaign_plan_recipients(state, reservation_lease_until)
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS whatsapp_campaign_recipient_claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            task_id INTEGER NOT NULL,
            recipient_number TEXT NOT NULL,
            plan_id INTEGER NOT NULL,
            plan_recipient_id INTEGER NOT NULL,
            state TEXT NOT NULL DEFAULT 'claimed'
                CHECK (state IN ('claimed', 'released')),
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (plan_id, plan_recipient_id),
            FOREIGN KEY (plan_id) REFERENCES whatsapp_campaign_plans(id),
            FOREIGN KEY (plan_recipient_id) REFERENCES whatsapp_campaign_plan_recipients(id)
        )
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS whatsapp_campaign_recipient_claims_lookup
        ON whatsapp_campaign_recipient_claims(campaign_id, recipient_number, state)
    ''')
    cursor.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS whatsapp_campaign_recipient_claims_active_unique
        ON whatsapp_campaign_recipient_claims(campaign_id, recipient_number)
        WHERE state = 'claimed'
    ''')
    migrate_whatsapp_campaign_plan_identity(conn)

    # Phase 2A: Shop Consent Declarations & Opt-Outs
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS shop_consent_declarations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shop_name TEXT NOT NULL,
            uploader_user_id INTEGER NOT NULL,
            source TEXT NOT NULL DEFAULT 'billing_counter',
            recipient_count INTEGER NOT NULL CHECK (recipient_count > 0),
            declaration_text TEXT NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (uploader_user_id) REFERENCES users(id)
        )
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS shop_consent_declarations_uploader
        ON shop_consent_declarations(uploader_user_id, created_at)
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS recipient_consent_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone_number TEXT NOT NULL,
            declaration_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'shop_attested'
                CHECK (status IN ('shop_attested', 'revoked')),
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (declaration_id) REFERENCES shop_consent_declarations(id)
        )
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS recipient_consent_phone_status
        ON recipient_consent_records(phone_number, status)
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS whatsapp_opt_outs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone_number TEXT NOT NULL UNIQUE,
            reason TEXT NOT NULL DEFAULT 'user_opt_out',
            opted_out_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS whatsapp_opt_outs_phone_unique
        ON whatsapp_opt_outs(phone_number)
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS whatsapp_opt_out_operations (
            request_id TEXT PRIMARY KEY,
            actor_user_id INTEGER NOT NULL,
            phone_number TEXT NOT NULL,
            reason TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'unknown', 'node_confirmed', 'applied', 'failed')),
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (actor_user_id) REFERENCES users(id)
        )
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS whatsapp_opt_out_operations_actor
        ON whatsapp_opt_out_operations(actor_user_id, created_at)
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS whatsapp_campaign_consent_declarations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            campaign_id INTEGER NOT NULL,
            business_user_id INTEGER NOT NULL,
            declaration_text TEXT NOT NULL,
            normalized_recipient_list TEXT NOT NULL,
            recipient_count INTEGER NOT NULL CHECK (recipient_count > 0),
            recipient_fingerprint TEXT NOT NULL CHECK (length(recipient_fingerprint) = 64),
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (task_id) REFERENCES tasks(id),
            FOREIGN KEY (business_user_id) REFERENCES users(id)
        )
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS whatsapp_campaign_consent_lookup
        ON whatsapp_campaign_consent_declarations(business_user_id, task_id, campaign_id)
    ''')
    migrate_legacy_business_campaign_review_state(conn)

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
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            mobile TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            expires_at DATETIME NOT NULL,
            used_at DATETIME,
            verified_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS password_reset_tokens_user_idx ON password_reset_tokens(user_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS password_reset_tokens_mobile_idx ON password_reset_tokens(mobile)')
    cursor.execute("PRAGMA table_info(password_reset_tokens)")
    if 'verified_at' not in [column[1] for column in cursor.fetchall()]:
        cursor.execute('ALTER TABLE password_reset_tokens ADD COLUMN verified_at DATETIME')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS device_registrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id_hash TEXT NOT NULL,
            signup_method TEXT NOT NULL CHECK (signup_method IN ('MOBILE', 'EMAIL')),
            user_id INTEGER NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS device_registrations_device_idx ON device_registrations(device_id_hash)')
    cursor.execute('CREATE INDEX IF NOT EXISTS device_registrations_method_idx ON device_registrations(signup_method)')
    cursor.execute('CREATE INDEX IF NOT EXISTS device_registrations_created_idx ON device_registrations(created_at)')
    cursor.execute('CREATE INDEX IF NOT EXISTS device_registrations_user_idx ON device_registrations(user_id)')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS email_verifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            expires_at DATETIME NOT NULL,
            used_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS email_verifications_email_idx ON email_verifications(email)')
    cursor.execute('CREATE INDEX IF NOT EXISTS email_verifications_expires_idx ON email_verifications(expires_at)')

    if is_development_environment():
        seed_data(conn)
    else:
        provision_initial_admin(conn)
    ensure_task_type_and_reward_fields(conn)

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

SHOP_CONSENT_DECLARATION_DEFAULT_TEXT = (
    "These customers gave us their number at our counter and agreed to receive offers from us on WhatsApp."
)

def _canonical_whatsapp_recipient_snapshot(phone_numbers, region=None):
    if not phone_numbers:
        raise ValueError('At least one recipient phone number is required.')
    seen = set()
    for number in phone_numbers:
        cleaned = normalize_recipient_number(number, region=region)
        seen.add(cleaned)
    normalized_numbers = sorted(seen)
    if not normalized_numbers:
        raise ValueError('No valid recipient phone numbers provided.')
    encoded = json.dumps(normalized_numbers, ensure_ascii=False, separators=(',', ':'))
    return normalized_numbers, encoded, hashlib.sha256(encoded.encode('utf-8')).hexdigest()

def create_whatsapp_campaign_consent_declaration(
    conn, task_id, campaign_id, business_user_id, phone_numbers,
    declaration_text=SHOP_CONSENT_DECLARATION_DEFAULT_TEXT, region=None,
):
    """Insert a campaign-level declaration without committing the caller's transaction."""
    if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id < 1:
        raise ValueError('A valid task ID is required.')
    if isinstance(campaign_id, bool) or not isinstance(campaign_id, int) or campaign_id < 1:
        raise ValueError('A valid campaign ID is required.')
    if isinstance(business_user_id, bool) or not isinstance(business_user_id, int) or business_user_id < 1:
        raise ValueError('A valid business user ID is required.')
    if declaration_text != SHOP_CONSENT_DECLARATION_DEFAULT_TEXT:
        raise ValueError('The approved business declaration text is required.')
    normalized, encoded, fingerprint = _canonical_whatsapp_recipient_snapshot(phone_numbers, region=region)
    task = conn.execute(
        'SELECT campaign_id, owner_user_id FROM tasks WHERE id = ?', (task_id,)
    ).fetchone()
    if task:
        expected_campaign_id = task['campaign_id'] or task_id
        if task['campaign_id'] is not None and campaign_id != expected_campaign_id:
            raise ValueError('The declaration campaign does not match the task.')
        if task['owner_user_id'] is not None and task['owner_user_id'] != business_user_id:
            raise ValueError('The declaration owner does not match the campaign owner.')
    cursor = conn.execute('''
        INSERT INTO whatsapp_campaign_consent_declarations
            (task_id, campaign_id, business_user_id, declaration_text,
             normalized_recipient_list, recipient_count, recipient_fingerprint)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (task_id, campaign_id, business_user_id, declaration_text.strip(),
          encoded, len(normalized), fingerprint))
    if task and task['owner_user_id'] is not None:
        conn.execute('''
            UPDATE tasks
            SET status = CASE WHEN campaign_approval_fingerprint IS NOT NULL
                              AND campaign_approval_fingerprint != ?
                              THEN 'pending_review' ELSE status END,
                campaign_review_status = CASE WHEN campaign_approval_fingerprint IS NOT NULL
                                              AND campaign_approval_fingerprint != ?
                                              THEN 'pending_review' ELSE campaign_review_status END,
                campaign_approved_by = CASE WHEN campaign_approval_fingerprint IS NOT NULL
                                             AND campaign_approval_fingerprint != ?
                                             THEN NULL ELSE campaign_approved_by END,
                campaign_approved_at = CASE WHEN campaign_approval_fingerprint IS NOT NULL
                                             AND campaign_approval_fingerprint != ?
                                             THEN NULL ELSE campaign_approved_at END,
                campaign_approval_fingerprint = CASE WHEN campaign_approval_fingerprint IS NOT NULL
                                                      AND campaign_approval_fingerprint != ?
                                                      THEN NULL ELSE campaign_approval_fingerprint END
            WHERE id = ? AND owner_user_id = ?
        ''', (fingerprint, fingerprint, fingerprint, fingerprint, fingerprint,
              task_id, business_user_id))
    return {
        'declaration_id': cursor.lastrowid,
        'task_id': task_id,
        'campaign_id': campaign_id,
        'business_user_id': business_user_id,
        'recipient_count': len(normalized),
        'recipient_fingerprint': fingerprint,
    }

def get_whatsapp_campaign_consent_declaration(conn, task_id, campaign_id=None):
    """Read the immutable campaign declaration; never treats absence as consent."""
    if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id < 1:
        raise ValueError('A valid task ID is required.')
    if campaign_id is None:
        return conn.execute(
            'SELECT * FROM whatsapp_campaign_consent_declarations WHERE task_id = ? ORDER BY id DESC LIMIT 1',
            (task_id,),
        ).fetchone()
    return conn.execute(
        'SELECT * FROM whatsapp_campaign_consent_declarations WHERE task_id = ? AND campaign_id = ? ORDER BY id DESC LIMIT 1',
        (task_id, campaign_id),
    ).fetchone()

def record_shop_consent_declaration(conn, shop_name, uploader_user_id, phone_numbers,
                                    source='billing_counter',
                                    declaration_text=SHOP_CONSENT_DECLARATION_DEFAULT_TEXT,
                                    region=None):
    """Record an auditable shop consent declaration and register shop_attested recipient consent."""
    if not shop_name or not isinstance(shop_name, str) or not shop_name.strip():
        raise ValueError('A valid shop name is required.')
    if isinstance(uploader_user_id, bool) or not isinstance(uploader_user_id, int) or uploader_user_id < 1:
        raise ValueError('A valid uploader user ID is required.')
    if not phone_numbers:
        raise ValueError('At least one recipient phone number is required.')

    if declaration_text != SHOP_CONSENT_DECLARATION_DEFAULT_TEXT:
        raise ValueError('The approved business declaration text is required.')
    normalized_numbers, _, _ = _canonical_whatsapp_recipient_snapshot(phone_numbers, region=region)

    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO shop_consent_declarations
            (shop_name, uploader_user_id, source, recipient_count, declaration_text)
        VALUES (?, ?, ?, ?, ?)
    ''', (shop_name.strip(), uploader_user_id, source, len(normalized_numbers), declaration_text.strip()))
    declaration_id = cursor.lastrowid

    cursor.executemany('''
        INSERT INTO recipient_consent_records (phone_number, declaration_id, status)
        VALUES (?, ?, 'shop_attested')
    ''', [(num, declaration_id) for num in normalized_numbers])
    return {
        'declaration_id': declaration_id,
        'shop_name': shop_name.strip(),
        'uploader_user_id': uploader_user_id,
        'source': source,
        'recipient_count': len(normalized_numbers),
        'phone_numbers': normalized_numbers,
    }

def record_whatsapp_opt_out(conn, phone_number, reason='user_opt_out', region=None):
    """Record an opt-out for a recipient. Opted-out recipients are never messaged."""
    cleaned = normalize_recipient_number(phone_number, region=region)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO whatsapp_opt_outs (phone_number, reason, opted_out_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
    ''', (cleaned, reason))
    cursor.execute('''
        UPDATE recipient_consent_records
        SET status = 'revoked'
        WHERE phone_number = ?
    ''', (cleaned,))

def is_recipient_opted_out(conn, phone_number, region=None):
    cleaned = normalize_recipient_number(phone_number, region=region)
    row = conn.execute(
        'SELECT 1 FROM whatsapp_opt_outs WHERE phone_number IN (?, ?) LIMIT 1', (cleaned, cleaned[1:])
    ).fetchone()
    return row is not None

def check_recipients_consent_and_opt_out(conn, phone_numbers, region=None):
    """Check consent and opt-out status for a list of numbers.

    Returns dict with:
      - valid_attested: list of numbers that have active shop_attested consent and not opted out
      - opted_out: list of numbers that have opted out (blocked)
      - unattested: list of numbers without valid consent record (blocked)
      - all_valid: boolean indicating if all numbers are validly attested and not opted out
    """
    cleaned_numbers = []
    invalid_numbers = []
    for num in phone_numbers:
        try:
            cleaned_numbers.append(normalize_recipient_number(num, region=region))
        except ValueError:
            invalid_numbers.append(str(num))

    opted_out = []
    unattested = []
    valid_attested = []

    opt_out_rows = set()
    table_check = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='whatsapp_opt_outs'"
    ).fetchone()
    consent_table_check = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recipient_consent_records'"
    ).fetchone()
    if not table_check or not consent_table_check:
        raise RuntimeError('Authoritative WhatsApp consent and opt-out data is unavailable.')
    if cleaned_numbers:
        placeholders = ','.join('?' for _ in cleaned_numbers)
        opt_out_rows = {
            r[0] for r in conn.execute(
                f'SELECT phone_number FROM whatsapp_opt_outs WHERE phone_number IN ({placeholders})',
                cleaned_numbers,
            ).fetchall()
        }
        opt_out_rows.update(r[0] for r in conn.execute(
            f'SELECT phone_number FROM whatsapp_opt_outs WHERE phone_number IN ({placeholders})',
            [number[1:] for number in cleaned_numbers],
        ).fetchall())

    attested_rows = set()
    if cleaned_numbers:
        placeholders = ','.join('?' for _ in cleaned_numbers)
        attested_rows = {
            r[0] for r in conn.execute(
                f"SELECT phone_number FROM recipient_consent_records WHERE status = 'shop_attested' AND phone_number IN ({placeholders})",
                cleaned_numbers,
            ).fetchall()
        }

    for num in cleaned_numbers:
        if num in opt_out_rows or num[1:] in opt_out_rows:
            opted_out.append(num)
        elif num in attested_rows:
            valid_attested.append(num)
        else:
            unattested.append(num)

    return {
        'valid_attested': valid_attested,
        'opted_out': opted_out,
        'unattested': unattested + invalid_numbers,
        'all_valid': len(opted_out) == 0 and len(unattested) == 0 and not invalid_numbers and len(cleaned_numbers) > 0,
    }


def initialize_database():
    """Initialize the database with explicit rollback and connection cleanup."""
    conn = get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        result = _initialize_database_with_connection(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return result

def init_db():
    """Backward-compatible alias for explicit initialization callers."""
    return initialize_database()


if __name__ == '__main__':
    initialize_database()
    print("Database initialized, migrated, and seeded successfully.")
