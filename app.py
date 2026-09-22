import os
import json
import random
import string
import hashlib
import hmac
import sqlite3
import re
import logging
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, request, jsonify, render_template, session, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import BadSignature, URLSafeSerializer
from money import (
    bps_of_paise,
    decimal_value,
    inr_to_paise,
    paise_to_inr,
    percentage_to_bps,
    rate_paise_per_unit,
    usdt_to_micro,
)

import database
import ocr_scanner

ENVIRONMENT = os.environ.get('ENVIRONMENT', 'production').strip().lower()
# Development conveniences are enabled only by this explicit value. Unknown
# environments follow the production-safe path.
IS_DEVELOPMENT = ENVIRONMENT == 'development'
IS_PRODUCTION = not IS_DEVELOPMENT
_configured_db_path = os.environ.get('TASKPE_DB_PATH', '').strip()
if IS_PRODUCTION and not _configured_db_path:
    raise RuntimeError('TASKPE_DB_PATH must be set to a persistent database path when ENVIRONMENT=production.')
if _configured_db_path and not os.path.isabs(_configured_db_path):
    raise RuntimeError('TASKPE_DB_PATH must be an absolute path.')
SMS_PROVIDER = os.environ.get('SMS_PROVIDER', 'disabled').strip().lower()
FAST2SMS_API_KEY = os.environ.get('FAST2SMS_API_KEY', '').strip()
_fast2sms_enabled_raw = os.environ.get('FAST2SMS_ENABLED')
FAST2SMS_ENABLED = (
    _fast2sms_enabled_raw.strip().lower() in {'1', 'true', 'yes'}
    if _fast2sms_enabled_raw is not None
    else SMS_PROVIDER == 'fast2sms'
)
FAST2SMS_ROUTE = os.environ.get('FAST2SMS_ROUTE', 'q').strip().lower() or 'q'
FAST2SMS_SUCCESS = 'SUCCESS'
FAST2SMS_PROVIDER_REJECTED = 'PROVIDER_REJECTED'
FAST2SMS_PROVIDER_ERROR = 'PROVIDER_ERROR'
FAST2SMS_NETWORK_ERROR = 'NETWORK_ERROR'
PAYOUT_MODE = os.environ.get('PAYOUT_MODE', 'MANUAL').strip().upper()
if PAYOUT_MODE != 'MANUAL':
    raise RuntimeError('Unsupported PAYOUT_MODE. Only MANUAL payout mode is allowed.')
OTP_RESEND_COOLDOWN_SECONDS = 60
OTP_EXPIRY_MINUTES = 10
OTP_MAX_VERIFICATION_ATTEMPTS = 5
OTP_MAX_REQUESTS_PER_HOUR = 5
RESET_TOKEN_EXPIRY_MINUTES = 10
EMAIL_VERIFICATION_EXPIRY_MINUTES = 30
DEVICE_COOKIE_NAME = 'taskpe_device'
EMAIL_DEVICE_ACCOUNT_LIMIT = 2
MOBILE_DEVICE_ACCOUNT_LIMIT = 4
EMAIL_PROVIDER = os.environ.get('EMAIL_PROVIDER', 'disabled').strip().lower()
BREVO_API_KEY = os.environ.get('BREVO_API_KEY', '').strip()
EMAIL_FROM = os.environ.get('EMAIL_FROM', '').strip()
EMAIL_FROM_NAME = os.environ.get('EMAIL_FROM_NAME', 'TaskPe').strip() or 'TaskPe'
BREVO_API_URL = 'https://api.brevo.com/v3/smtp/email'
SECRET_KEY = os.environ.get('SECRET_KEY', '').strip()
if IS_PRODUCTION and not SECRET_KEY:
    raise RuntimeError('SECRET_KEY must be set when ENVIRONMENT=production.')
if IS_DEVELOPMENT and not SECRET_KEY:
    SECRET_KEY = 'taskpe-development-only-secret'
if SMS_PROVIDER not in {'disabled', 'fast2sms'}:
    raise RuntimeError('Unsupported SMS_PROVIDER. Use fast2sms or disabled.')
if IS_PRODUCTION and SMS_PROVIDER == 'fast2sms' and not FAST2SMS_ENABLED:
    raise RuntimeError('FAST2SMS_ENABLED must be 1 when SMS_PROVIDER=fast2sms in production.')
if IS_PRODUCTION and SMS_PROVIDER == 'fast2sms' and not FAST2SMS_API_KEY:
    raise RuntimeError('FAST2SMS_API_KEY is required when SMS_PROVIDER=fast2sms in production.')
if EMAIL_PROVIDER not in {'disabled', 'brevo', 'brevo_api'}:
    raise RuntimeError('Unsupported EMAIL_PROVIDER. Use brevo or disabled.')
if IS_PRODUCTION and EMAIL_PROVIDER in {'brevo', 'brevo_api'}:
    missing_email_config = [
        name for name, value in {
            'BREVO_API_KEY': BREVO_API_KEY,
            'EMAIL_FROM': EMAIL_FROM,
        }.items() if not value
    ]
    if missing_email_config:
        raise RuntimeError('Brevo email provider configuration is incomplete.')
try:
    TRUSTED_PROXY_HOPS = int(os.environ.get('TRUSTED_PROXY_HOPS', '0'))
except ValueError:
    raise RuntimeError('TRUSTED_PROXY_HOPS must be 0 or 1.')
if TRUSTED_PROXY_HOPS not in {0, 1}:
    raise RuntimeError('TRUSTED_PROXY_HOPS must be 0 or 1.')
ENABLE_HSTS = os.environ.get('ENABLE_HSTS', '').strip().lower() in {'1', 'true', 'yes'}

logger = logging.getLogger(__name__)
_rate_limit_lock = threading.Lock()
_rate_limit_events = {}
RATE_LIMIT_RULES = {
    '/api/auth/login': (10, 300),
    '/api/auth/register': (10, 600),
    '/api/auth/send-otp': (5, 3600),
    '/api/auth/verify-otp': (15, 600),
    '/api/auth/forgot-password': (5, 3600),
    '/api/auth/forgot-password/verify': (15, 600),
    '/api/auth/reset-password': (10, 600),
    '/api/auth/send-email-verification': (5, 3600),
    '/api/auth/verify-email': (15, 600),
    '/api/auth/register-email': (10, 600),
    '/api/wallet/withdraw': (5, 600),
}

app = Flask(__name__, static_folder='static', template_folder='templates')
if TRUSTED_PROXY_HOPS:
    # Waitress is private-bound; only the explicitly configured local proxy hop
    # may assert the original client scheme/host through X-Forwarded headers.
    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=TRUSTED_PROXY_HOPS,
        x_proto=TRUSTED_PROXY_HOPS,
        x_host=TRUSTED_PROXY_HOPS,
        x_port=TRUSTED_PROXY_HOPS,
    )
app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=IS_PRODUCTION,
    SESSION_COOKIE_SAMESITE='Lax',
    DEBUG=IS_DEVELOPMENT and os.environ.get('FLASK_DEBUG', '').strip() == '1'
)
DEVICE_SIGNER = URLSafeSerializer(SECRET_KEY, salt='taskpe-device-v1')

@app.after_request
def add_security_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    response.headers.setdefault('Content-Security-Policy', "base-uri 'self'; object-src 'none'; frame-ancestors 'none'")
    if ENABLE_HSTS and request.is_secure:
        response.headers.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
    if session.get('user_id') or request.path.startswith('/api/admin') or request.path.startswith('/api/wallet'):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Pragma'] = 'no-cache'
    return response

@app.route('/health', methods=['GET'])
def health():
    """Non-sensitive liveness/readiness check for staging and operations."""
    conn = None
    try:
        conn = database.get_db_connection()
        conn.execute('SELECT 1').fetchone()
        return jsonify({'status': 'ok', 'payout_mode': PAYOUT_MODE})
    except Exception:
        return jsonify({'status': 'unhealthy'}), 503
    finally:
        if conn is not None:
            conn.close()

def csrf_token():
    token = session.get('_csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['_csrf_token'] = token
    return token

@app.context_processor
def inject_security_context():
    return {'csrf_token': csrf_token, 'payout_mode': PAYOUT_MODE}

def _request_rate_key(path):
    data = request.get_json(silent=True) or {}
    identity = (data.get('identifier') or data.get('mobile') or '').strip().lower()
    user_id = session.get('user_id')
    return (path, request.remote_addr or 'unknown', identity, user_id)

@app.before_request
def enforce_request_controls():
    if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}:
        rule = RATE_LIMIT_RULES.get(request.path)
        if request.path.startswith('/api/tasks/') and request.path.endswith('/submit'):
            rule = (10, 600)
        elif request.path.startswith('/api/admin/'):
            rule = (60, 60)
        if rule:
            limit, window = rule
            now = time.monotonic()
            key = _request_rate_key(request.path)
            with _rate_limit_lock:
                events = [stamp for stamp in _rate_limit_events.get(key, []) if now - stamp < window]
                if len(events) >= limit:
                    _rate_limit_events[key] = events
                    return jsonify({'success': False, 'message': 'Too many requests. Please try again later.'}), 429
                events.append(now)
                _rate_limit_events[key] = events

        supplied = request.headers.get('X-CSRF-Token')
        browser_request = bool(request.headers.get('Origin') or request.headers.get('Referer'))
        if supplied is not None or browser_request:
            expected = session.get('_csrf_token')
            if not expected or not supplied or not hmac.compare_digest(expected, supplied):
                return jsonify({'success': False, 'message': 'Invalid CSRF token.'}), 403

@app.before_request
def prepare_device_identity():
    """Load or create a signed, opaque first-party device identifier."""
    signed = request.cookies.get(DEVICE_COOKIE_NAME)
    try:
        device_id = DEVICE_SIGNER.loads(signed) if signed else None
        if not isinstance(device_id, str) or len(device_id) < 32:
            raise BadSignature('invalid device id')
        request.environ['taskpe_device_id'] = device_id
        request.environ['taskpe_device_cookie'] = None
    except BadSignature:
        device_id = secrets.token_urlsafe(32)
        request.environ['taskpe_device_id'] = device_id
        request.environ['taskpe_device_cookie'] = DEVICE_SIGNER.dumps(device_id)

@app.after_request
def set_device_cookie(response):
    signed = request.environ.get('taskpe_device_cookie')
    if signed:
        response.set_cookie(
            DEVICE_COOKIE_NAME,
            signed,
            max_age=60 * 60 * 24 * 365,
            httponly=True,
            secure=IS_PRODUCTION,
            samesite='Lax',
        )
    return response

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
def configured_storage_path(name, development_default):
    value = os.environ.get(name, '').strip()
    if IS_PRODUCTION and not value:
        raise RuntimeError(f'{name} must be set to a persistent path when ENVIRONMENT=production.')
    if value and not os.path.isabs(value):
        raise RuntimeError(f'{name} must be an absolute path.')
    return value or development_default

UPLOAD_FOLDER = configured_storage_path(
    'TASKPE_UPLOAD_FOLDER', os.path.join(BASE_DIR, 'uploads', 'proofs')
)
TASK_ASSET_FOLDER = configured_storage_path(
    'TASKPE_TASK_ASSET_FOLDER', os.path.join(BASE_DIR, 'uploads', 'task-assets')
)

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'gif'}

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['TASK_ASSET_FOLDER'] = TASK_ASSET_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB

try:
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(TASK_ASSET_FOLDER, exist_ok=True)
except OSError as exc:
    raise RuntimeError('Configured upload paths are missing or unavailable.') from exc

# Development keeps the existing explicit initializer and seed behavior. A
# production WSGI import must only verify that the deployment was migrated.
if IS_DEVELOPMENT:
    database.initialize_database()
else:
    database.verify_required_schema()

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def valid_image_content(file_storage):
    """Validate common gallery image signatures without trusting extensions."""
    try:
        position = file_storage.stream.tell()
        head = file_storage.stream.read(16)
        file_storage.stream.seek(position)
    except (AttributeError, OSError):
        return False
    if head.startswith(b'\x89PNG\r\n\x1a\n'):
        return True
    if head.startswith(b'\xff\xd8\xff'):
        return True
    if head.startswith(b'GIF87a') or head.startswith(b'GIF89a'):
        return True
    return head[:4] == b'RIFF' and head[8:12] == b'WEBP'

def generate_referral_code(name):
    clean = "".join([c for c in name if c.isalnum()]).upper()[:4]
    if len(clean) < 3:
        clean = "TASK"
    digits = ''.join(random.choices(string.digits, k=3))
    return f"{clean}{digits}"

def normalize_phone_number(value):
    """Normalize phone input for allocation and target-number uniqueness."""
    return database.normalize_target_number(value)

def normalize_auth_mobile(value):
    """Normalize Indian signup/OTP input to its ten-digit local form."""
    digits = database.normalize_target_number(value)
    if len(digits) == 12 and digits.startswith('91'):
        digits = digits[-10:]
    return digits

def current_device_id_hash():
    device_id = request.environ.get('taskpe_device_id')
    if not device_id:
        raise RuntimeError('Device identity was not prepared.')
    return hashlib.sha256(device_id.encode('utf-8')).hexdigest()

def device_account_limit_reached(conn, signup_method):
    limit = EMAIL_DEVICE_ACCOUNT_LIMIT if signup_method == 'EMAIL' else MOBILE_DEVICE_ACCOUNT_LIMIT
    row = conn.execute('''
        SELECT COUNT(*) AS count FROM device_registrations
        WHERE device_id_hash = ? AND signup_method = ?
    ''', (current_device_id_hash(), signup_method)).fetchone()
    return int(row['count']) >= limit

def device_limit_message(signup_method):
    if signup_method == 'EMAIL':
        return 'This device has reached the maximum number of email accounts allowed.'
    return 'This device has reached the maximum number of mobile accounts allowed.'

def email_token_digest(email, token):
    return hmac.new(SECRET_KEY.encode('utf-8'), f'{email}:{token}'.encode('utf-8'), hashlib.sha256).hexdigest()

class EmailProvider:
    def send_verification(self, recipient, token):
        raise NotImplementedError


class DisabledEmailProvider(EmailProvider):
    def send_verification(self, recipient, token):
        return IS_DEVELOPMENT


class BrevoEmailProvider(EmailProvider):
    def send_verification(self, recipient, token):
        payload = json.dumps({
            'sender': {
                'name': EMAIL_FROM_NAME,
                'email': EMAIL_FROM,
            },
            'to': [{'email': recipient}],
            'subject': 'TaskPe email verification',
            'textContent': (
                f'Your TaskPe email verification token is: {token}\n'
                f'This token expires in {EMAIL_VERIFICATION_EXPIRY_MINUTES} minutes.'
            ),
        }).encode('utf-8')
        provider_request = urllib.request.Request(
            BREVO_API_URL,
            data=payload,
            headers={
                'accept': 'application/json',
                'api-key': BREVO_API_KEY,
                'content-type': 'application/json',
            },
            method='POST',
        )
        try:
            with urllib.request.urlopen(provider_request, timeout=10) as response:
                status = int(getattr(response, 'status', response.getcode()))
                response_body = response.read().decode('utf-8')
            provider_result = json.loads(response_body) if response_body else {}
            if 200 <= status < 300 and isinstance(provider_result, dict) and provider_result.get('messageId'):
                return True
            logger.warning('Brevo email verification delivery was rejected.')
            return False
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, TimeoutError, ValueError):
            logger.warning('Brevo email verification delivery failed.')
            return False


email_provider = BrevoEmailProvider() if EMAIL_PROVIDER in {'brevo', 'brevo_api'} else DisabledEmailProvider()


def send_email_verification(email, token):
    """Send through the configured provider without logging email secrets."""
    return email_provider.send_verification(email, token)

def _sanitize_fast2sms_text(value):
    """Keep provider diagnostics useful without logging credentials or secrets."""
    if isinstance(value, (list, tuple)):
        value = '; '.join(str(item) for item in value)
    text = str(value or '')
    if FAST2SMS_API_KEY:
        text = text.replace(FAST2SMS_API_KEY, '[REDACTED]')
    text = re.sub(
        r'(?i)(authorization|api[_ -]?key|access[_ -]?token|token|secret|password)'
        r'\s*([:=])\s*("[^"]*"|\'[^\']*\'|[^,\s}]+)',
        r'\1\2[REDACTED]',
        text,
    )
    text = re.sub(r'\b\d{10,12}\b', '[REDACTED_MOBILE]', text)
    text = re.sub(r'\b\d{6}\b', '[REDACTED_OTP]', text)
    return re.sub(r'[\r\n]+', ' ', text)[:240]


def _fast2sms_response_details(raw):
    """Extract only safe provider fields for internal diagnostics."""
    try:
        result = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        result = {}
    if not isinstance(result, dict):
        return None, '', _sanitize_fast2sms_text(raw), ''
    request_id = re.sub(r'[^A-Za-z0-9._-]', '', str(result.get('request_id') or ''))[:120]
    message = _sanitize_fast2sms_text(result.get('message'))
    error_code = _sanitize_fast2sms_text(result.get('code') or result.get('error_code'))
    if not error_code:
        match = re.search(r'\b(?:995|996|997|998|999)\b', f'{message} {_sanitize_fast2sms_text(raw)}')
        error_code = match.group(0) if match else ''
    return result.get('return') if isinstance(result.get('return'), bool) else None, request_id, message, error_code


def send_fast2sms_otp(mobile, otp):
    """Deliver an OTP through Fast2SMS and return a safe internal result."""
    if SMS_PROVIDER == 'disabled' or (IS_DEVELOPMENT and not FAST2SMS_ENABLED):
        return FAST2SMS_SUCCESS
    if SMS_PROVIDER != 'fast2sms' or not FAST2SMS_ENABLED or not FAST2SMS_API_KEY:
        logger.error('Fast2SMS OTP configuration is incomplete.')
        return FAST2SMS_PROVIDER_ERROR
    route = FAST2SMS_ROUTE
    if route != 'q':
        logger.error('Fast2SMS route configuration is unsupported; only Quick SMS route=q is allowed.')
        return FAST2SMS_PROVIDER_ERROR
    payload = json.dumps({
        'message': f'TaskPe verification code: {otp}. This code expires in 10 minutes. Do not share this code with anyone.',
        'route': route,
        'numbers': mobile[-10:],
    }).encode('utf-8')
    request_obj = urllib.request.Request(
        'https://www.fast2sms.com/dev/bulkV2',
        data=payload,
        method='POST',
        headers={
            'Authorization': FAST2SMS_API_KEY,
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        },
    )
    try:
        with urllib.request.urlopen(request_obj, timeout=10) as response:
            status = int(getattr(response, 'status', response.getcode()))
            raw = response.read().decode('utf-8', errors='replace')
        returned, request_id, message, error_code = _fast2sms_response_details(raw)
        logger.info(
            'Fast2SMS OTP response: reached=True status=%s return=%s request_id=%s message=%s error_code=%s route=%s',
            status, returned, request_id or '[none]', message or '[none]', error_code or '[none]', route,
        )
        if not 200 <= status < 300:
            return FAST2SMS_PROVIDER_ERROR
        return FAST2SMS_SUCCESS if returned is True else FAST2SMS_PROVIDER_REJECTED
    except urllib.error.HTTPError as exc:
        try:
            response_body = exc.read().decode('utf-8', errors='replace')
        except (OSError, ValueError):
            response_body = ''
        returned, request_id, message, error_code = _fast2sms_response_details(response_body)
        logger.warning(
            'Fast2SMS OTP response: reached=True status=%s return=%s request_id=%s message=%s error_code=%s route=%s',
            exc.code, returned, request_id or '[none]', message or '[none]', error_code or '[none]', route,
        )
        return FAST2SMS_PROVIDER_ERROR
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        logger.warning(
            'Fast2SMS OTP response: reached=False status=None return=None request_id=[none] message=%s error_code=[none] route=%s',
            _sanitize_fast2sms_text(exc), route,
        )
        return FAST2SMS_NETWORK_ERROR

def otp_digest(mobile, otp):
    """Return a server-only digest for a mobile-bound OTP."""
    secret = SECRET_KEY.encode('utf-8')
    message = f'{mobile}:{otp}'.encode('utf-8')
    return hmac.new(secret, message, hashlib.sha256).hexdigest()

def reset_token_digest(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()

def task_recipient_pool(task):
    try:
        numbers = json.loads(task['target_numbers'])
    except Exception:
        numbers = [task['target_numbers']]
    # Preserve order while eliminating formatting duplicates in the source pool.
    unique_numbers = []
    seen = set()
    for number in numbers:
        clean = normalize_phone_number(number)
        if clean and clean not in seen:
            seen.add(clean)
            unique_numbers.append(clean)
    return unique_numbers

def get_assigned_numbers(assignment):
    try:
        return json.loads(assignment['assigned_numbers'])
    except Exception:
        return []

def get_setting_number(conn, key, default):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    try:
        return float(row['value']) if row else default
    except (TypeError, ValueError):
        return default

def get_setting_decimal(conn, key, default):
    """Read an exact setting, falling back to its legacy text value."""
    try:
        exact = conn.execute(
            "SELECT value_scaled, scale FROM exact_money_settings WHERE key = ?",
            (key,),
        ).fetchone()
        if exact and exact['value_scaled'] is not None and exact['scale']:
            return Decimal(exact['value_scaled']) / Decimal(exact['scale'])
    except sqlite3.OperationalError:
        pass
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    try:
        return Decimal(str(row['value'])) if row else Decimal(str(default))
    except Exception:
        return Decimal(str(default))

def ensure_exact_user_balance(conn, user_id):
    """Backfill the exact balance columns lazily for legacy databases."""
    columns = {row[1] for row in conn.execute('PRAGMA table_info(users)')}
    if 'wallet_balance_paise' not in columns or 'total_earned_paise' not in columns:
        return None
    row = conn.execute("SELECT wallet_balance, total_earned, wallet_balance_paise, total_earned_paise FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        return None
    balance = row['wallet_balance_paise'] if row['wallet_balance_paise'] is not None else inr_to_paise(row['wallet_balance'])
    earned = row['total_earned_paise'] if row['total_earned_paise'] is not None else inr_to_paise(row['total_earned'])
    conn.execute("UPDATE users SET wallet_balance_paise = ?, total_earned_paise = ? WHERE id = ?", (balance, earned, user_id))
    return balance, earned

def apply_wallet_delta(cursor, user_id, delta_paise, *, require_balance=False):
    """Apply an exact paise delta and mirror legacy columns for compatibility."""
    columns = {row[1] for row in cursor.connection.execute('PRAGMA table_info(users)')}
    if 'wallet_balance_paise' not in columns or 'total_earned_paise' not in columns:
        row = cursor.execute("SELECT wallet_balance, total_earned FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return None
        current = inr_to_paise(row['wallet_balance'])
        new_balance = current + int(delta_paise)
        if require_balance and new_balance < 0:
            return None
        new_earned = inr_to_paise(row['total_earned']) + int(delta_paise)
        cursor.execute("UPDATE users SET wallet_balance = ?, total_earned = ? WHERE id = ?", (str(paise_to_inr(new_balance)), str(paise_to_inr(new_earned)), user_id))
        return new_balance
    row = cursor.execute("SELECT wallet_balance, total_earned, wallet_balance_paise, total_earned_paise FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        return None
    current = row['wallet_balance_paise'] if row['wallet_balance_paise'] is not None else inr_to_paise(row['wallet_balance'])
    earned = row['total_earned_paise'] if row['total_earned_paise'] is not None else inr_to_paise(row['total_earned'])
    new_balance = current + int(delta_paise)
    if require_balance and new_balance < 0:
        return None
    new_earned = earned + int(delta_paise)
    cursor.execute("""
        UPDATE users SET wallet_balance_paise = ?, total_earned_paise = ?,
            wallet_balance = ?, total_earned = ? WHERE id = ?
    """, (new_balance, new_earned, str(paise_to_inr(new_balance)), str(paise_to_inr(new_earned)), user_id))
    return new_balance

def insert_wallet_transaction(cursor, user_id, kind, amount_paise, balance_paise, reference_id, description, submission_id=None):
    """Write both exact ledger values and legacy display columns."""
    columns = {row[1] for row in cursor.connection.execute('PRAGMA table_info(wallet_transactions)')}
    if 'amount_paise' not in columns or 'balance_after_paise' not in columns:
        if submission_id is None:
            cursor.execute("INSERT INTO wallet_transactions (user_id, type, amount, balance_after, reference_id, description) VALUES (?, ?, ?, ?, ?, ?)", (user_id, kind, str(paise_to_inr(amount_paise)), str(paise_to_inr(balance_paise)), reference_id, description))
        else:
            cursor.execute("INSERT INTO wallet_transactions (user_id, type, amount, balance_after, reference_id, description, submission_id) VALUES (?, ?, ?, ?, ?, ?, ?)", (user_id, kind, str(paise_to_inr(amount_paise)), str(paise_to_inr(balance_paise)), reference_id, description, submission_id))
        return
    cursor.execute("""
        INSERT INTO wallet_transactions
            (user_id, type, amount, balance_after, amount_paise, balance_after_paise,
             reference_id, description, submission_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (user_id, kind, str(paise_to_inr(amount_paise)), str(paise_to_inr(balance_paise)),
          int(amount_paise), int(balance_paise), reference_id, description, submission_id))

def get_setting_text(conn, key, default):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row['value'].strip() if row and row['value'] else default

def valid_usdt_address(network, address):
    """Basic network-specific syntax validation; this never proves ownership."""
    if network == 'TRC20':
        return bool(re.fullmatch(r'T[1-9A-HJ-NP-Za-km-z]{33}', address))
    if network == 'ERC20':
        return bool(re.fullmatch(r'0x[a-fA-F0-9]{40}', address))
    if network == 'BEP20':
        return bool(re.fullmatch(r'0x[a-fA-F0-9]{40}', address))
    return False

def calculate_task_payout(conn, task, recipient_count=None):
    """Return the task's immutable per-recipient reward times assigned recipients."""
    count = recipient_count if recipient_count is not None else (task['recipients_per_slot'] or len(task_recipient_pool(task)))
    return float(paise_to_inr(calculate_task_payout_paise(conn, task, count)))

def calculate_task_payout_paise(conn, task, recipient_count=None):
    count = recipient_count if recipient_count is not None else (task['recipients_per_slot'] or len(task_recipient_pool(task)))
    rate_paise = task.get('reward_per_number_paise') if isinstance(task, dict) else None
    if rate_paise is None and hasattr(task, 'keys') and 'reward_per_number_paise' in task.keys():
        rate_paise = task['reward_per_number_paise']
    if rate_paise is None:
        rate_paise = inr_to_paise(get_setting_decimal(conn, 'per_message_rate', '3'))
    rate_paise = int(rate_paise)
    return rate_paise * int(count)

def approval_threshold(required_count):
    """Return the smallest whole-number count meeting the 80% rule."""
    required = max(0, int(required_count or 0))
    return (required * 80 + 99) // 100

def submission_required_count(submission):
    return int(submission['required_count'] or submission['numbers_required'] or 0) if 'required_count' in submission.keys() else int(submission['numbers_required'] or 0)

def submission_verified_count(submission):
    return int(submission['verified_count'] or submission['numbers_detected'] or 0) if 'verified_count' in submission.keys() else int(submission['numbers_detected'] or 0)

def submission_per_number_rate(conn, submission):
    if 'per_number_rate_paise' in submission.keys() and submission['per_number_rate_paise'] is not None:
        return paise_to_inr(submission['per_number_rate_paise'])
    if 'per_number_rate' in submission.keys() and submission['per_number_rate'] is not None:
        return Decimal(str(submission['per_number_rate']))
    # Legacy/manual rows may contain a stale full-task payout and no rate
    # snapshot. Never reverse-engineer a rate from that amount; doing so can
    # overpay partial approvals. Use the configured rate as the safe fallback.
    return Decimal(str(get_setting_number(conn, 'per_message_rate', 3.0)))

def finalize_target_records_for_approval(conn, assignment_id, approved_count, paid_amount_per_target,
                                         submission_id=None, task_id=None, campaign_id=None, user_id=None):
    """Persist the exact paid and remaining target identities for an approval."""
    paid_amount_per_target_paise = inr_to_paise(paid_amount_per_target)
    records = conn.execute('''
        SELECT id, status, target_number, normalized_target_number FROM target_number_records
        WHERE assignment_id = ? ORDER BY id
    ''', (assignment_id,)).fetchall()
    # Legacy/manual submissions may predate target-record population. Keep
    # their existing approval behavior; new submissions populate this table.
    if not records:
        return
    verified = [record for record in records if record['status'] == 'verified']
    already_paid = [record for record in records if record['status'] == 'paid']
    if approved_count < 0 or approved_count > len(verified) + len(already_paid):
        raise ValueError('Approved target count exceeds verified target records.')
    assignment = conn.execute('''
        SELECT user_id, task_id, campaign_id FROM task_assignments WHERE id = ?
    ''', (assignment_id,)).fetchone()
    if assignment:
        user_id = assignment['user_id'] if user_id is None else user_id
        task_id = assignment['task_id'] if task_id is None else task_id
        campaign_id = assignment['campaign_id'] if campaign_id is None else campaign_id
    # Retries are idempotent: retain existing paid targets and only promote
    # the remaining verified targets needed to reach the approved count.
    paid_ids = {record['id'] for record in already_paid}
    paid_ids.update(record['id'] for record in verified[:max(0, approved_count - len(already_paid))])
    for record in records:
        if record['id'] in paid_ids:
            if submission_id is not None and record['status'] != 'paid':
                conn.execute('''
                    INSERT INTO target_payments
                        (target_number_record_id, assignment_id, task_id, campaign_id,
                         user_id, submission_id, target_number, normalized_target_number,
                         amount, rate, amount_paise, rate_paise, payment_reference)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    record['id'], assignment_id, task_id, campaign_id, user_id,
                    submission_id, record['target_number'], record['normalized_target_number'],
                    str(paise_to_inr(paid_amount_per_target_paise)), str(paise_to_inr(paid_amount_per_target_paise)),
                    paid_amount_per_target_paise, paid_amount_per_target_paise,
                    f'TARGET-PAY-{record["id"]}'
                ))
            conn.execute('''
                UPDATE target_number_records
                SET status = 'paid', paid_at = CURRENT_TIMESTAMP,
                    paid_amount = ?, paid_amount_paise = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'verified'
            ''', (str(paise_to_inr(paid_amount_per_target_paise)), paid_amount_per_target_paise, record['id']))
        elif record['status'] in ('assigned', 'verified'):
            conn.execute('''
                UPDATE target_number_records
                SET status = 'remaining', updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status IN ('assigned', 'verified')
            ''', (record['id'],))

def get_reusable_target_numbers(conn, campaign_id=None, candidate_numbers=None, user_id=None, limit=None):
    """Return deterministic remaining targets that can be assigned again.

    This function only selects numbers. The caller must be inside the existing
    BEGIN IMMEDIATE allocation transaction before inserting the new assignment
    and recipient_allocations rows.
    """
    candidate_set = None
    if candidate_numbers is not None:
        candidate_set = {normalize_phone_number(number) for number in candidate_numbers if normalize_phone_number(number)}
    rows = conn.execute('''
        SELECT id, normalized_target_number, target_number
        FROM target_number_records
        WHERE status = 'remaining'
        ORDER BY id
    ''').fetchall()
    reusable = []
    for row in rows:
        number = row['normalized_target_number']
        if not number or (candidate_set is not None and number not in candidate_set):
            continue
        if conn.execute('''
            SELECT 1 FROM target_number_records
            WHERE normalized_target_number = ?
              AND status IN ('assigned', 'verified')
        ''', (number,)).fetchone():
            continue
        # A genuinely remaining target was assigned but not completed and is
        # immediately reusable within the campaign.
        if campaign_id is not None and conn.execute('''
            SELECT 1 FROM recipient_allocations
            WHERE campaign_id = ? AND phone_number = ?
              AND status IN ('ALLOCATED', 'COMPLETED')
        ''', (campaign_id, number)).fetchone():
            continue
        reusable.append(number)
        if limit is not None and len(reusable) >= int(limit):
            break
    return reusable

def release_expired_assignments(conn):
    """Release overdue, unsubmitted assignments using database time as truth."""
    started_transaction = not conn.in_transaction
    if started_transaction:
        conn.execute('BEGIN IMMEDIATE')
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE recipient_allocations
        SET status = 'RELEASED'
        WHERE status = 'ALLOCATED'
          AND assignment_id IN (
              SELECT ta.id FROM task_assignments ta
              WHERE ta.status = 'in_progress'
                AND ta.expires_at IS NOT NULL
                AND ta.expires_at <= CURRENT_TIMESTAMP
                AND NOT EXISTS (
                    SELECT 1 FROM submissions s WHERE s.assignment_id = ta.id
                )
          )
    ''')
    cursor.execute('''
        UPDATE task_assignments
        SET status = 'expired', slot_released = 1
        WHERE status = 'in_progress'
          AND expires_at IS NOT NULL
          AND expires_at <= CURRENT_TIMESTAMP
          AND slot_released = 0
    ''')
    if started_transaction:
        conn.commit()
    return cursor.rowcount

def settle_recipient_allocations_for_approval(conn, assignment_id):
    """Keep OCR-verified recipients permanently used and release the rest."""
    conn.execute('''
        UPDATE recipient_allocations
        SET status = CASE WHEN EXISTS (
            SELECT 1 FROM target_number_records tnr
            WHERE tnr.assignment_id = recipient_allocations.assignment_id
              AND tnr.normalized_target_number = recipient_allocations.phone_number
              AND tnr.status IN ('verified', 'paid')
        ) THEN 'COMPLETED' ELSE 'RELEASED' END
        WHERE assignment_id = ? AND status = 'ALLOCATED'
    ''', (assignment_id,))

def credit_referrer_for_task(cursor, worker, payout, submission_id):
    """Backend-only referral cap; no UI signal is emitted once the cap is reached."""
    if not worker['referred_by']:
        return
    limit_row = cursor.execute("SELECT value FROM settings WHERE key = 'referral_limit_tasks'").fetchone()
    try:
        task_limit = int(limit_row['value']) if limit_row else 20
    except (TypeError, ValueError):
        task_limit = 20
    worker_now = cursor.execute("SELECT referred_user_task_count FROM users WHERE id = ?", (worker['id'],)).fetchone()
    completed_count = worker_now['referred_user_task_count'] or 0
    if completed_count > task_limit:
        return
    percent_row = cursor.execute("SELECT value FROM settings WHERE key = 'referral_commission_percent'").fetchone()
    try:
        commission_percent = get_setting_decimal(cursor.connection, 'referral_commission_percent', '10') if percent_row else Decimal('10')
        commission_bps = percentage_to_bps(commission_percent)
    except Exception:
        commission_bps = percentage_to_bps('10')
    payout_paise = inr_to_paise(payout)
    bonus_paise = bps_of_paise(payout_paise, commission_bps)
    ensure_exact_user_balance(cursor.connection, worker['referred_by'])
    reference_id = f"REF-TASK-{submission_id}"
    # Claim the unique ledger reference before modifying the balance. The partial
    # index is the database-level duplicate-credit guard.
    existing = cursor.execute("SELECT 1 FROM wallet_transactions WHERE type = 'referral_task_commission' AND reference_id = ?", (reference_id,)).fetchone()
    if existing:
        return
    balance_paise = apply_wallet_delta(cursor, worker['referred_by'], bonus_paise)
    insert_wallet_transaction(cursor, worker['referred_by'], 'referral_task_commission', bonus_paise, balance_paise, reference_id, f'{commission_percent}% referral commission')

def get_current_user():
    user_id = session.get('user_id')
    if not user_id:
        return None
    conn = database.get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return user

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('user_id'):
            return jsonify({'success': False, 'message': 'Authentication required. Please log in.'}), 401
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_current_user()
        if not user or user['role'] != 'admin':
            return jsonify({'success': False, 'message': 'Admin privileges required.'}), 403
        return f(*args, **kwargs)
    return decorated_function

# ----------------- WEB PAGES -----------------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/admin')
@app.route('/admin/')
@admin_required
def admin_page():
    """Separate, protected administration UI; API authorization remains enforced."""
    return render_template('admin.html')

@app.route('/uploads/proofs/<filename>')
def uploaded_proof(filename):
    # Legacy URL retained for the current UI, but it is no longer public.
    # Filenames are generated server-side, and authorization is checked against
    # the submission that owns the proof before serving the file.
    if not filename or secure_filename(filename) != filename:
        return jsonify({'success': False, 'message': 'Proof not found.'}), 404
    user = get_current_user()
    if not user:
        return jsonify({'success': False, 'message': 'Authentication required.'}), 401
    conn = database.get_db_connection()
    proof = conn.execute('''
        SELECT s.user_id
        FROM submissions s
        LEFT JOIN task_proofs tp ON tp.submission_id = s.id
        WHERE s.proof_image = ? OR tp.file_name = ?
        LIMIT 1
    ''', (filename, filename)).fetchone()
    conn.close()
    if not proof:
        return jsonify({'success': False, 'message': 'Proof not found.'}), 404
    if user['role'] != 'admin' and proof['user_id'] != user['id']:
        return jsonify({'success': False, 'message': 'Not authorized to view this proof.'}), 403
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

def normalize_task_destination(value):
    """Allow only ordinary HTTP(S) links for worker-facing campaign tasks."""
    raw = str(value or '').strip()
    if not raw or any(ord(char) < 32 or char.isspace() for char in raw):
        return None
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme.lower() not in {'http', 'https'} or not parsed.netloc:
        return None
    if parsed.username or parsed.password or not parsed.hostname:
        return None
    return urllib.parse.urlunparse((parsed.scheme.lower(), parsed.netloc, parsed.path or '/', '', parsed.query, parsed.fragment))

@app.route('/uploads/task-assets/<filename>')
@login_required
def uploaded_task_asset(filename):
    if not filename or secure_filename(filename) != filename or not filename.startswith('campaign_'):
        return jsonify({'success': False, 'message': 'Task image not found.'}), 404
    user = get_current_user()
    conn = database.get_db_connection()
    task = conn.execute(
        "SELECT id, status FROM tasks WHERE image_path = ?", (filename,)
    ).fetchone()
    conn.close()
    if not task or (user['role'] != 'admin' and task['status'] not in {'active', 'paused'}):
        return jsonify({'success': False, 'message': 'Task image not found.'}), 404
    return send_from_directory(app.config['TASK_ASSET_FOLDER'], filename)

# ----------------- AUTH APIS -----------------
@app.route('/api/auth/csrf', methods=['GET'])
def auth_csrf():
    return jsonify({'success': True, 'csrf_token': csrf_token()})

@app.route('/api/auth/me', methods=['GET'])
def auth_me():
    user = get_current_user()
    if not user:
        return jsonify({'logged_in': False})
    return jsonify({
        'logged_in': True,
        'user': {
            'id': user['id'],
            'name': user['name'],
            'email': user['email'],
            'mobile': user['mobile'],
            'role': user['role'],
            'wallet_balance': round(user['wallet_balance'], 2),
            'total_earned': round(user['total_earned'], 2),
            'referral_code': user['referral_code']
        }
    })

@app.route('/api/auth/register', methods=['POST'])
def register():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    mobile = normalize_auth_mobile(data.get('mobile') or '')
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''
    ref_code = (data.get('referral_code') or '').strip().upper()

    if not name or not mobile or not email or not password:
        return jsonify({'success': False, 'message': 'All fields are required.'}), 400

    if not re.fullmatch(r'[6-9]\d{9}', mobile):
        return jsonify({'success': False, 'message': 'Please enter a valid 10-digit mobile number.'}), 400

    conn = database.get_db_connection()
    conn.execute('BEGIN IMMEDIATE')
    verification = conn.execute('''
        SELECT mobile FROM mobile_verifications
        WHERE mobile = ? AND consumed_at IS NULL AND expires_at > CURRENT_TIMESTAMP
    ''', (mobile,)).fetchone()
    if not verification:
        conn.rollback()
        conn.close()
        return jsonify({'success': False, 'message': 'Verify the OTP for this mobile number before registering.'}), 400
    # Check existing user
    existing = conn.execute("SELECT id FROM users WHERE email = ? OR mobile = ?", (email, mobile)).fetchone()
    if existing:
        conn.rollback()
        conn.close()
        return jsonify({'success': False, 'message': 'User with this email or mobile already exists.'}), 400

    if device_account_limit_reached(conn, 'MOBILE'):
        conn.rollback()
        conn.close()
        return jsonify({'success': False, 'message': device_limit_message('MOBILE')}), 429

    # Check referrer
    referred_by = None
    if ref_code:
        ref_user = conn.execute("SELECT id FROM users WHERE referral_code = ?", (ref_code,)).fetchone()
        if ref_user:
            referred_by = ref_user['id']

    # Generate new referral code
    new_code = generate_referral_code(name)
    # Check collision
    while conn.execute("SELECT id FROM users WHERE referral_code = ?", (new_code,)).fetchone():
        new_code = generate_referral_code(name)

    pw_hash = generate_password_hash(password)
    # Give initial signup bonus ₹20
    initial_bonus_paise = inr_to_paise('20.00')
    initial_bonus = str(paise_to_inr(initial_bonus_paise))

    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO users (name, mobile, email, password_hash, role, wallet_balance, total_earned, referral_code, referred_by, signup_method)
        VALUES (?, ?, ?, ?, 'user', ?, ?, ?, ?, 'MOBILE')
    ''', (name, mobile, email, pw_hash, initial_bonus, initial_bonus, new_code, referred_by))
    new_user_id = cursor.lastrowid
    cursor.execute('''
        INSERT INTO device_registrations (device_id_hash, signup_method, user_id)
        VALUES (?, 'MOBILE', ?)
    ''', (current_device_id_hash(), new_user_id))
    cursor.execute("UPDATE users SET wallet_balance_paise = ?, total_earned_paise = ? WHERE id = ?", (initial_bonus_paise, initial_bonus_paise, new_user_id))

    # Record signup bonus transaction
    insert_wallet_transaction(
        cursor, new_user_id, 'referral_bonus', initial_bonus_paise,
        initial_bonus_paise, f'SIGNUP-BONUS-{new_user_id}',
        'Welcome Signup Bonus from TaskPe'
    )

    # This state is tied to the normalized mobile number and can be consumed
    # exactly once, so a verified OTP cannot be replayed for another signup.
    consumed = cursor.execute('''
        UPDATE mobile_verifications SET consumed_at = CURRENT_TIMESTAMP
        WHERE mobile = ? AND consumed_at IS NULL AND expires_at > CURRENT_TIMESTAMP
    ''', (mobile,))
    if consumed.rowcount != 1:
        conn.rollback()
        conn.close()
        return jsonify({'success': False, 'message': 'OTP verification has expired. Please verify again.'}), 400

    conn.commit()
    conn.close()

    session['user_id'] = new_user_id
    return jsonify({
        'success': True,
        'message': 'Account created successfully! Welcome bonus ₹20 added.',
        'user': {
            'id': new_user_id,
            'name': name,
            'email': email,
            'mobile': mobile,
            'role': 'user',
            'wallet_balance': initial_bonus,
            'total_earned': initial_bonus,
            'referral_code': new_code
        }
    })

@app.route('/api/auth/send-email-verification', methods=['POST'])
def send_email_verification_route():
    data = request.get_json() or {}
    email = (data.get('email') or '').strip().lower()
    generic = 'If this email can be registered, a verification link has been sent.'
    if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email) or len(email) > 254:
        return jsonify({'success': False, 'message': 'Please enter a valid email address.'}), 400

    conn = database.get_db_connection()
    existing = conn.execute('SELECT id FROM users WHERE email = ?', (email,)).fetchone()
    recent = conn.execute('''
        SELECT 1 FROM email_verifications
        WHERE email = ? AND created_at > datetime('now', '-60 seconds')
        LIMIT 1
    ''', (email,)).fetchone()
    if existing or recent:
        conn.close()
        return jsonify({'success': True, 'message': generic})
    conn.execute('BEGIN IMMEDIATE')
    token = secrets.token_urlsafe(32)
    conn.execute('DELETE FROM email_verifications WHERE email = ? AND used_at IS NULL', (email,))
    conn.execute('''
        INSERT INTO email_verifications (email, token_hash, expires_at)
        VALUES (?, ?, datetime('now', ?))
    ''', (email, email_token_digest(email, token), f'+{EMAIL_VERIFICATION_EXPIRY_MINUTES} minutes'))
    conn.commit()
    conn.close()

    if not send_email_verification(email, token):
        return jsonify({'success': False, 'message': 'Unable to send email verification right now.'}), 503
    response = {'success': True, 'message': generic}
    if IS_DEVELOPMENT:
        response['verification_token'] = token
    return jsonify(response)

@app.route('/api/auth/verify-email', methods=['POST'])
def verify_email():
    data = request.get_json() or {}
    email = (data.get('email') or '').strip().lower()
    token = (data.get('verification_token') or '').strip()
    if not email or not token:
        return jsonify({'success': False, 'message': 'Invalid or expired email verification.'}), 400
    conn = database.get_db_connection()
    row = conn.execute('''
        SELECT id FROM email_verifications
        WHERE email = ? AND token_hash = ? AND used_at IS NULL
          AND expires_at > CURRENT_TIMESTAMP
    ''', (email, email_token_digest(email, token))).fetchone()
    conn.close()
    if not row:
        return jsonify({'success': False, 'message': 'Invalid or expired email verification.'}), 400
    return jsonify({'success': True, 'message': 'Email verified successfully.'})

@app.route('/api/auth/register-email', methods=['POST'])
def register_email():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''
    ref_code = (data.get('referral_code') or '').strip().upper()
    token = (data.get('verification_token') or '').strip()
    if not name or not email or not password or not token:
        return jsonify({'success': False, 'message': 'All fields are required.'}), 400
    if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email) or len(email) > 254:
        return jsonify({'success': False, 'message': 'Please enter a valid email address.'}), 400
    if len(password) < 8:
        return jsonify({'success': False, 'message': 'Password must be at least 8 characters.'}), 400

    conn = database.get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute('SELECT id FROM users WHERE email = ?', (email,)).fetchone():
            conn.rollback()
            return jsonify({'success': False, 'message': 'User with this email already exists.'}), 400
        verification = conn.execute('''
            SELECT id FROM email_verifications
            WHERE email = ? AND token_hash = ? AND used_at IS NULL
              AND expires_at > CURRENT_TIMESTAMP
        ''', (email, email_token_digest(email, token))).fetchone()
        if not verification:
            conn.rollback()
            return jsonify({'success': False, 'message': 'Verify your email before registering.'}), 400
        if device_account_limit_reached(conn, 'EMAIL'):
            conn.rollback()
            return jsonify({'success': False, 'message': device_limit_message('EMAIL')}), 429

        referred_by = None
        if ref_code:
            ref_user = conn.execute('SELECT id FROM users WHERE referral_code = ?', (ref_code,)).fetchone()
            if ref_user:
                referred_by = ref_user['id']
        new_code = generate_referral_code(name)
        while conn.execute('SELECT id FROM users WHERE referral_code = ?', (new_code,)).fetchone():
            new_code = generate_referral_code(name)
        initial_bonus_paise = inr_to_paise('20.00')
        synthetic_mobile = 'email-' + secrets.token_hex(16)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO users (name, mobile, email, password_hash, role, wallet_balance,
                total_earned, referral_code, referred_by, signup_method)
            VALUES (?, ?, ?, ?, 'user', ?, ?, ?, ?, 'EMAIL')
        ''', (name, synthetic_mobile, email, generate_password_hash(password),
              str(paise_to_inr(initial_bonus_paise)), str(paise_to_inr(initial_bonus_paise)),
              new_code, referred_by))
        new_user_id = cursor.lastrowid
        cursor.execute('UPDATE users SET wallet_balance_paise = ?, total_earned_paise = ? WHERE id = ?',
                       (initial_bonus_paise, initial_bonus_paise, new_user_id))
        insert_wallet_transaction(cursor, new_user_id, 'referral_bonus', initial_bonus_paise,
                                  initial_bonus_paise, f'SIGNUP-BONUS-{new_user_id}',
                                  'Welcome Signup Bonus from TaskPe')
        consumed = cursor.execute('''
            UPDATE email_verifications SET used_at = CURRENT_TIMESTAMP
            WHERE id = ? AND used_at IS NULL
        ''', (verification['id'],))
        if consumed.rowcount != 1:
            conn.rollback()
            return jsonify({'success': False, 'message': 'Email verification has expired. Please verify again.'}), 400
        cursor.execute('''
            INSERT INTO device_registrations (device_id_hash, signup_method, user_id)
            VALUES (?, 'EMAIL', ?)
        ''', (current_device_id_hash(), new_user_id))
        conn.commit()
    finally:
        conn.close()
    session['user_id'] = new_user_id
    return jsonify({'success': True, 'message': 'Account created successfully! Welcome bonus ₹20 added.', 'user': {
        'id': new_user_id, 'name': name, 'email': email, 'mobile': synthetic_mobile,
        'role': 'user', 'wallet_balance': str(paise_to_inr(initial_bonus_paise)),
        'total_earned': str(paise_to_inr(initial_bonus_paise)), 'referral_code': new_code,
    }})

@app.route('/api/auth/login', methods=['POST'])
def login():
    data = request.get_json() or {}
    identifier = (data.get('identifier') or '').strip().lower()
    password = data.get('password') or ''

    if not identifier or not password:
        return jsonify({'success': False, 'message': 'Mobile/Email and Password are required.'}), 400

    conn = database.get_db_connection()
    normalized_mobile = normalize_auth_mobile(identifier)
    mobile_candidates = [identifier]
    if re.fullmatch(r'[6-9]\d{9}', normalized_mobile):
        mobile_candidates.extend((normalized_mobile, f'91{normalized_mobile}'))
    mobile_placeholders = ', '.join('?' for _ in mobile_candidates)
    user = conn.execute(
        f"SELECT * FROM users WHERE email = ? OR mobile IN ({mobile_placeholders})",
        (identifier, *mobile_candidates),
    ).fetchone()
    conn.close()

    if not user or not check_password_hash(user['password_hash'], password):
        return jsonify({'success': False, 'message': 'Invalid login credentials. Please check and retry.'}), 401

    session['user_id'] = user['id']
    return jsonify({
        'success': True,
        'message': f"Welcome back, {user['name']}!",
        'user': {
            'id': user['id'],
            'name': user['name'],
            'email': user['email'],
            'mobile': user['mobile'],
            'role': user['role'],
            'wallet_balance': round(user['wallet_balance'], 2),
            'total_earned': round(user['total_earned'], 2),
            'referral_code': user['referral_code']
        }
    })

@app.route('/api/auth/demo-login', methods=['POST'])
def demo_login():
    if not IS_DEVELOPMENT:
        return jsonify({'success': False, 'message': 'Demo authentication is disabled.'}), 404
    data = request.get_json() or {}
    role = data.get('role', 'earner')

    conn = database.get_db_connection()
    if role == 'admin':
        user = conn.execute("SELECT * FROM users WHERE role = 'admin' LIMIT 1").fetchone()
    else:
        user = conn.execute("SELECT * FROM users WHERE role = 'user' ORDER BY id ASC LIMIT 1").fetchone()
    conn.close()

    if not user:
        return jsonify({'success': False, 'message': 'Demo account not found.'}), 404

    session['user_id'] = user['id']
    return jsonify({
        'success': True,
        'message': f"Logged in as Demo {user['role'].capitalize()} ({user['name']})",
        'user': {
            'id': user['id'],
            'name': user['name'],
            'email': user['email'],
            'mobile': user['mobile'],
            'role': user['role'],
            'wallet_balance': round(user['wallet_balance'], 2),
            'total_earned': round(user['total_earned'], 2),
            'referral_code': user['referral_code']
        }
    })

@app.route('/api/auth/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'success': True, 'message': 'Logged out successfully.'})

def otp_response(payload, status=200):
    """Return a concrete Flask response for both route and internal callers."""
    response = jsonify(payload)
    response.status_code = status
    return response

@app.route('/api/auth/send-otp', methods=['POST'])
def send_otp():
    data = request.get_json() or {}
    mobile = normalize_auth_mobile(data.get('mobile') or '')
    if not mobile or len(mobile) != 10 or not re.fullmatch(r'[6-9]\d{9}', mobile):
        return otp_response({'success': False, 'message': 'Valid 10-digit mobile number required.'}, 400)

    conn = database.get_db_connection()
    try:
        # Hold the write transaction across the provider call. This serializes
        # competing OTP requests so two concurrent requests cannot both pass
        # the cooldown/hourly checks before a successful send is recorded.
        conn.execute('BEGIN IMMEDIATE')
        recent = conn.execute('''
            SELECT 1 FROM otp_request_log
            WHERE mobile = ? AND created_at > datetime('now', ?)
            LIMIT 1
        ''', (mobile, f'-{OTP_RESEND_COOLDOWN_SECONDS} seconds')).fetchone()
        if not recent:
            # Keep cooldown compatibility for OTP rows created before request
            # history was introduced.
            recent = conn.execute('''
                SELECT 1 FROM otps
                WHERE mobile = ? AND created_at > datetime('now', ?)
                LIMIT 1
            ''', (mobile, f'-{OTP_RESEND_COOLDOWN_SECONDS} seconds')).fetchone()
        hourly = conn.execute('''
            SELECT COUNT(*) AS count FROM otp_request_log
            WHERE mobile = ? AND created_at > datetime('now', '-1 hour')
        ''', (mobile,)).fetchone()
        if not hourly or hourly['count'] == 0:
            hourly = conn.execute('''
                SELECT COUNT(*) AS count FROM otps
                WHERE mobile = ? AND created_at > datetime('now', '-1 hour')
            ''', (mobile,)).fetchone()
        if recent:
            conn.rollback()
            return otp_response({'success': False, 'message': 'Please wait before requesting another OTP.'}, 429)
        if hourly and hourly['count'] >= OTP_MAX_REQUESTS_PER_HOUR:
            conn.rollback()
            return otp_response({'success': False, 'message': 'Too many OTP requests. Please try again later.'}, 429)

        otp = f"{secrets.randbelow(1_000_000):06d}"
        expires = (datetime.now() + timedelta(minutes=OTP_EXPIRY_MINUTES)).strftime('%Y-%m-%d %H:%M:%S')
        provider_failure_status = 502
        if IS_PRODUCTION and (SMS_PROVIDER != 'fast2sms' or not FAST2SMS_ENABLED):
            provider_result = FAST2SMS_PROVIDER_ERROR
            provider_failure_status = 503
        else:
            provider_result = send_fast2sms_otp(mobile, otp)
        if provider_result != FAST2SMS_SUCCESS:
            # Count failed attempts toward the hourly abuse limit, but place
            # them just outside the cooldown window so a provider outage does
            # not create a misleading 60-second resend lockout.
            conn.execute("INSERT INTO otp_request_log (mobile, created_at) VALUES (?, datetime('now', '-61 seconds'))", (mobile,))
            conn.commit()
            return otp_response({'success': False, 'message': 'Unable to send OTP right now. Please try again.'}, provider_failure_status)

        # Only a confirmed provider success creates the usable OTP record and
        # the current-time cooldown/rate-limit entry.
        conn.execute('INSERT INTO otp_request_log (mobile) VALUES (?)', (mobile,))
        # A new OTP invalidates any earlier registration verification for this phone.
        conn.execute("DELETE FROM mobile_verifications WHERE mobile = ?", (mobile,))
        conn.execute("""
            INSERT INTO otps (mobile, otp_code, otp_hash, expires_at, attempt_count)
            VALUES (?, ?, ?, ?, 0)
        """, (mobile, '__hashed__', otp_digest(mobile, otp), expires))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    response = {
        'success': True,
        'message': f"OTP sent to +91 {mobile}."
    }
    # Never return a real OTP to a production browser.  Development keeps this
    # optional aid behind the explicit ENVIRONMENT=development switch.
    if IS_DEVELOPMENT:
        response['otp_code'] = otp
    return otp_response(response)

@app.route('/api/auth/forgot-password', methods=['POST'])
def forgot_password():
    """Start reset without revealing whether the mobile belongs to an account."""
    data = request.get_json() or {}
    mobile = normalize_auth_mobile(data.get('mobile') or '')
    generic = 'If the account exists, a verification code has been sent.'
    if not mobile or not re.fullmatch(r'[6-9]\d{9}', mobile):
        return jsonify({'success': False, 'message': 'Please enter a valid 10-digit mobile number.'}), 400

    conn = database.get_db_connection()
    user = conn.execute('SELECT id FROM users WHERE mobile = ?', (mobile,)).fetchone()
    conn.close()
    if not user:
        logger.info('Password reset requested for an unrecognized account')
        # Return an indistinguishable decoy challenge. It is intentionally not
        # persisted and can never verify an OTP or reset a password.
        return jsonify({'success': True, 'message': generic,
                        'challenge_token': secrets.token_urlsafe(32)})

    # Keep OTP generation, storage, provider handling, cooldown, and request
    # limits in the existing implementation. Only normalize its success text.
    response = send_otp()
    if response.status_code == 200:
        logger.info('Password reset requested')
        payload = {'success': True, 'message': generic}
        # Preserve the existing development-only testing aid without ever
        # exposing an OTP in production.
        if IS_DEVELOPMENT and response.get_json().get('otp_code'):
            payload['otp_code'] = response.get_json()['otp_code']
        challenge_token = secrets.token_urlsafe(32)
        conn = database.get_db_connection()
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('DELETE FROM password_reset_tokens WHERE user_id = ? AND used_at IS NULL', (user['id'],))
        conn.execute('''
            INSERT INTO password_reset_tokens
                (user_id, mobile, token_hash, expires_at, verified_at)
            VALUES (?, ?, ?, datetime('now', ?), NULL)
        ''', (user['id'], mobile, reset_token_digest(challenge_token),
              f'+{RESET_TOKEN_EXPIRY_MINUTES} minutes'))
        conn.commit()
        conn.close()
        payload['challenge_token'] = challenge_token
        return jsonify(payload)
    return response

@app.route('/api/auth/verify-otp', methods=['POST'])
def verify_otp():
    data = request.get_json() or {}
    raw_mobile = database.normalize_target_number(data.get('mobile') or '')
    mobile = normalize_auth_mobile(raw_mobile)
    otp_code = (data.get('otp_code') or '').strip()

    if not mobile or len(mobile) != 10 or not re.fullmatch(r'[6-9]\d{9}', mobile) or not re.fullmatch(r'\d{6}', otp_code):
        return jsonify({'success': False, 'message': 'Invalid or expired OTP code.'}), 400
    # Accept legacy rows that stored the same Indian number with its 91
    # country prefix, while keeping the normalized mobile as the identity.
    mobile_keys = (mobile, raw_mobile) if raw_mobile != mobile else (mobile,)

    # Lookup and consumption are one write transaction.  The mobile is part of
    # the lookup, so a code issued for another number can never verify here.
    conn = database.get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        record = conn.execute('''
            SELECT * FROM otps
            WHERE mobile IN (?, ?) AND expires_at > CURRENT_TIMESTAMP
            ORDER BY id DESC LIMIT 1
        ''', mobile_keys if len(mobile_keys) == 2 else (mobile, mobile)).fetchone()
        expires_at = record['expires_at'] if record else None
        valid = False
        if record and int(record['attempt_count'] or 0) < OTP_MAX_VERIFICATION_ATTEMPTS:
            if record['otp_hash']:
                valid = any(hmac.compare_digest(record['otp_hash'], otp_digest(key, otp_code)) for key in mobile_keys)
            else:
                # Support legacy OTP rows created before hashed storage.
                valid = hmac.compare_digest(str(record['otp_code'] or ''), otp_code)
        if record and not valid:
            if int(record['attempt_count'] or 0) < OTP_MAX_VERIFICATION_ATTEMPTS:
                conn.execute('UPDATE otps SET attempt_count = attempt_count + 1, last_attempt_at = CURRENT_TIMESTAMP WHERE id = ?', (record['id'],))
            conn.commit()
            record = None
        elif record:
            consumed = conn.execute("DELETE FROM otps WHERE id = ? AND mobile IN (?, ?)", (record['id'], mobile_keys[0], mobile_keys[-1]))
            if consumed.rowcount != 1:
                record = None
            if record:
                conn.commit()
            else:
                conn.rollback()
        else:
            conn.rollback()
    finally:
        conn.close()

    if not record:
        return jsonify({'success': False, 'message': 'Invalid or expired OTP code.'}), 400

    conn = database.get_db_connection()
    conn.execute('''
        INSERT INTO mobile_verifications (mobile, verified_at, expires_at, consumed_at)
        VALUES (?, CURRENT_TIMESTAMP, ?, NULL)
        ON CONFLICT(mobile) DO UPDATE SET
            verified_at = excluded.verified_at,
            expires_at = excluded.expires_at,
            consumed_at = NULL
    ''', (mobile, expires_at))
    conn.commit()
    conn.close()

    return jsonify({'success': True, 'message': 'Mobile number verified successfully!'})

@app.route('/api/auth/forgot-password/verify', methods=['POST'])
def verify_forgot_password_otp():
    data = request.get_json() or {}
    mobile = normalize_auth_mobile(data.get('mobile') or '')
    challenge_token = (data.get('challenge_token') or '').strip()
    otp_code = (data.get('otp_code') or '').strip()
    if not mobile or not challenge_token or not re.fullmatch(r'[6-9]\d{9}', mobile) or not re.fullmatch(r'\d{6}', otp_code):
        return jsonify({'success': False, 'message': 'Invalid or expired verification code.'}), 400

    conn = database.get_db_connection()
    user = conn.execute('SELECT id FROM users WHERE mobile = ?', (mobile,)).fetchone()
    conn.close()
    if not user:
        return jsonify({'success': False, 'message': 'Invalid or expired verification code.'}), 400

    conn = database.get_db_connection()
    challenge = conn.execute('''
        SELECT id FROM password_reset_tokens
        WHERE token_hash = ? AND user_id = ? AND mobile = ?
          AND verified_at IS NULL AND used_at IS NULL
          AND expires_at > CURRENT_TIMESTAMP
    ''', (reset_token_digest(challenge_token), user['id'], mobile)).fetchone()
    conn.close()
    if not challenge:
        logger.info('Password reset OTP verification failed')
        return jsonify({'success': False, 'message': 'Invalid or expired verification code.'}), 400

    # Reuse the existing atomic, hashed, mobile-bound OTP verifier. Its
    # one-time consumption prevents replay before a reset token is issued.
    verification = verify_otp()
    if verification.status_code != 200:
        logger.info('Password reset OTP verification failed')
        return jsonify({'success': False, 'message': 'Invalid or expired verification code.'}), 400

    token = secrets.token_urlsafe(32)
    conn = database.get_db_connection()
    conn.execute('BEGIN IMMEDIATE')
    updated = conn.execute('''
        UPDATE password_reset_tokens
        SET token_hash = ?, verified_at = CURRENT_TIMESTAMP
        WHERE id = ? AND verified_at IS NULL AND used_at IS NULL
    ''', (reset_token_digest(token), challenge['id']))
    if updated.rowcount != 1:
        conn.rollback()
        conn.close()
        return jsonify({'success': False, 'message': 'Invalid or expired verification code.'}), 400
    conn.commit()
    conn.close()
    logger.info('Password reset OTP verified')
    return jsonify({'success': True, 'message': 'Verification successful.', 'reset_token': token})

@app.route('/api/auth/reset-password', methods=['POST'])
def reset_password():
    data = request.get_json() or {}
    token = (data.get('reset_token') or '').strip()
    new_password = data.get('new_password') or ''
    confirm_password = data.get('confirm_password') or ''
    if not token:
        return jsonify({'success': False, 'message': 'Invalid or expired reset request.'}), 400
    if len(new_password) < 8:
        return jsonify({'success': False, 'message': 'Password must be at least 8 characters.'}), 400
    if new_password != confirm_password:
        return jsonify({'success': False, 'message': 'Passwords do not match.'}), 400

    conn = database.get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        reset = conn.execute('''
            SELECT id, user_id FROM password_reset_tokens
            WHERE token_hash = ? AND used_at IS NULL AND verified_at IS NOT NULL
              AND expires_at > CURRENT_TIMESTAMP
        ''', (reset_token_digest(token),)).fetchone()
        if not reset:
            conn.rollback()
            return jsonify({'success': False, 'message': 'Invalid or expired reset request.'}), 400
        conn.execute('UPDATE users SET password_hash = ? WHERE id = ?',
                     (generate_password_hash(new_password), reset['user_id']))
        consumed = conn.execute('''
            UPDATE password_reset_tokens SET used_at = CURRENT_TIMESTAMP
            WHERE id = ? AND used_at IS NULL
        ''', (reset['id'],))
        if consumed.rowcount != 1:
            conn.rollback()
            return jsonify({'success': False, 'message': 'Invalid or expired reset request.'}), 400
        conn.commit()
    finally:
        conn.close()
    session.pop('user_id', None)
    logger.info('Password reset completed')
    return jsonify({'success': True, 'message': 'Password reset successfully. Please log in.'})

# ----------------- TASKS APIS -----------------
@app.route('/api/tasks', methods=['GET'])
def get_tasks():
    user_id = session.get('user_id')
    conn = database.get_db_connection()
    release_expired_assignments(conn)
    conn.commit()
    # Workers may only discover and take currently published tasks. Archived
    # and paused tasks remain available to administrators for history review.
    tasks = conn.execute("SELECT * FROM tasks WHERE status = 'active' ORDER BY id DESC").fetchall()
    slot_rows = conn.execute('''
        SELECT task_id, COUNT(*) AS occupied
        FROM task_assignments
        WHERE status IN ('in_progress', 'pending_review', 'paid')
        GROUP BY task_id
    ''').fetchall()
    occupied_by_task = {row['task_id']: row['occupied'] for row in slot_rows}

    user_submissions = {}
    current_assignments = {}
    active_campaigns = {}
    if user_id:
        subs = conn.execute("SELECT task_id, status FROM submissions WHERE user_id = ?", (user_id,)).fetchall()
        for s in subs:
            user_submissions[s['task_id']] = s['status']
        assignments = conn.execute('''
            SELECT id, task_id, campaign_type, status FROM task_assignments
            WHERE user_id = ? AND status IN ('in_progress', 'pending_review', 'paid')
            ORDER BY id DESC
        ''', (user_id,)).fetchall()
        for assignment in assignments:
            current_assignments.setdefault(assignment['task_id'], assignment)
            if assignment['status'] in ('in_progress', 'pending_review'):
                active_campaigns.setdefault(assignment['campaign_type'], {
                    'task_id': assignment['task_id'],
                    'status': assignment['status']
                })
    result = []
    for t in tasks:
        target_nums = []
        try:
            target_nums = json.loads(t['target_numbers'])
        except Exception:
            target_nums = [t['target_numbers']]

        user_status = user_submissions.get(t['id'], None)
        current_assignment = current_assignments.get(t['id'])

        result.append({
            'id': t['id'],
            'title': t['title'],
            'category': t['category'],
            'task_type': t['task_type'] or 'TEXT_LINK',
            'description': t['description'],
            'payout': calculate_task_payout(conn, t, t['recipients_per_slot'] or len(target_nums)),
            'per_message_rate': float(paise_to_inr(t['reward_per_number_paise'] or inr_to_paise(get_setting_decimal(conn, 'per_message_rate', '3')))),
            'reward_per_number': float(paise_to_inr(t['reward_per_number_paise'] or inr_to_paise(get_setting_decimal(conn, 'per_message_rate', '3')))),
            'reward_source': t['reward_source'] or 'legacy_snapshot',
            'template_text': t['template_text'],
            'image_url': f"/uploads/task-assets/{t['image_path']}" if t['image_path'] else None,
            'destination_link': t['destination_link'],
            # Recipient pools are never returned from the dashboard.  Only a
            # private assignment may expose its allocated numbers.
            'target_numbers': [],
            'target_count': t['recipients_per_slot'] or len(target_nums),
            'time_limit_minutes': t['time_limit_minutes'],
            'proof_instructions': t['proof_instructions'],
            'total_slots': t['total_slots'],
            'taken_slots': int(occupied_by_task.get(t['id'], 0)),
            'available_slots': max(int(t['total_slots']) - int(occupied_by_task.get(t['id'], 0)), 0),
            'completed_slots': t['completed_slots'],
            'slots_remaining': max(0, t['total_slots'] - occupied_by_task.get(t['id'], 0)),
            'status': t['status'],
            'user_submission_status': user_status,
            'current_assignment_id': current_assignment['id'] if current_assignment else None,
            'current_assignment_status': current_assignment['status'] if current_assignment else None,
            'campaign_active': t['category'] in active_campaigns,
            'campaign_active_status': active_campaigns.get(t['category'], {}).get('status'),
            'active_task_id': active_campaigns.get(t['category'], {}).get('task_id')
        })

    conn.close()
    return jsonify({'success': True, 'tasks': result})

@app.route('/api/tasks/<int:task_id>/take', methods=['POST'])
@login_required
def take_task(task_id):
    """Atomically allocate a private, never-repeated recipient set to a user."""
    user = get_current_user()
    conn = database.get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        release_expired_assignments(conn)
        task = conn.execute("SELECT * FROM tasks WHERE id = ? AND status = 'active'", (task_id,)).fetchone()
        if not task:
            return jsonify({'success': False, 'message': 'This campaign is not available.'}), 404
        campaign_id = task['campaign_id'] or task['id']
        required = task['recipients_per_slot'] or len(task_recipient_pool(task))
        occupied_slots = conn.execute('''
            SELECT COUNT(*) AS count FROM task_assignments
            WHERE task_id = ? AND status IN ('in_progress', 'pending_review', 'paid')
        ''', (task_id,)).fetchone()['count']
        if occupied_slots >= task['total_slots']:
            return jsonify({'success': False, 'message': 'All slots for this campaign have been assigned.'}), 400
        active = conn.execute('''
            SELECT id FROM task_assignments
            WHERE user_id = ? AND campaign_type = ? AND status IN ('in_progress', 'pending_review')
        ''', (user['id'], task['category'])).fetchone()
        if active:
            return jsonify({'success': False, 'message': f'You already have an active {task["category"]} task. Submit or finish it before taking another.'}), 409

        available = []
        for number in task_recipient_pool(task):
            already_used = conn.execute('''
                SELECT 1 FROM recipient_allocations
                WHERE campaign_id = ? AND phone_number = ?
                  AND status IN ('ALLOCATED', 'COMPLETED')
            ''', (campaign_id, number)).fetchone()
            if not already_used:
                available.append(number)
            if len(available) == required:
                break
        if len(available) < required:
            return jsonify({
                'success': False,
                'message': f'Not enough safe recipients remain. This campaign needs {required} unique numbers per slot; add more recipients before assigning another slot.'
            }), 409

        cursor = conn.cursor()
        expiry_modifier = f"+{max(1, int(task['time_limit_minutes']))} minutes"
        cursor.execute('''
            INSERT INTO task_assignments (user_id, task_id, campaign_id, campaign_type, assigned_numbers, status, started_at, expires_at, slot_released)
            VALUES (?, ?, ?, ?, ?, 'in_progress', CURRENT_TIMESTAMP, datetime('now', ?), 0)
        ''', (user['id'], task_id, campaign_id, task['category'], json.dumps(available), expiry_modifier))
        assignment_id = cursor.lastrowid
        cursor.executemany('''
            INSERT INTO recipient_allocations
                (campaign_id, phone_number, assignment_id, user_id, last_contacted_at, status)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, 'ALLOCATED')
        ''', [(campaign_id, number, assignment_id, user['id']) for number in available])
        conn.commit()
        return jsonify({'success': True, 'assignment_id': assignment_id, 'message': f'{len(available)} unique recipients assigned.'})
    except sqlite3.IntegrityError:
        conn.rollback()
        return jsonify({'success': False, 'message': 'An active task for this campaign already exists. Refresh and try again.'}), 409
    finally:
        conn.close()

@app.route('/api/tasks/<int:task_id>', methods=['GET'])
def get_task_detail(task_id):
    conn = database.get_db_connection()
    release_expired_assignments(conn)
    conn.commit()
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        conn.close()
        return jsonify({'success': False, 'message': 'Task not found.'}), 404

    user_id = session.get('user_id')
    submission = None
    assignment = None
    target_nums = []
    if user_id:
        assignment = conn.execute('''
            SELECT * FROM task_assignments
            WHERE task_id = ? AND user_id = ? AND status IN ('in_progress', 'pending_review', 'paid')
            ORDER BY id DESC LIMIT 1
        ''', (task_id, user_id)).fetchone()
        if assignment:
            target_nums = get_assigned_numbers(assignment)
        sub = None
        if assignment:
            sub = conn.execute(
                "SELECT * FROM submissions WHERE assignment_id = ? ORDER BY id DESC LIMIT 1",
                (assignment['id'],)
            ).fetchone()
        if sub:
            submission = {
                'id': sub['id'],
                'status': sub['status'],
                'proof_image': sub['proof_image'],
                'admin_feedback': sub['admin_feedback'],
                'submitted_at': sub['submitted_at']
            }

    payout = calculate_task_payout(conn, task, len(target_nums) or (task['recipients_per_slot'] or 0))
    reward_per_number = float(paise_to_inr(
        task['reward_per_number_paise'] or inr_to_paise(get_setting_decimal(conn, 'per_message_rate', '3'))
    ))
    occupied_slots = conn.execute('''
        SELECT COUNT(*) AS count FROM task_assignments
        WHERE task_id = ? AND status IN ('in_progress', 'pending_review', 'paid')
    ''', (task_id,)).fetchone()['count']
    total_slots = int(task['total_slots'])
    taken_slots = int(occupied_slots)
    available_slots = max(total_slots - taken_slots, 0)
    conn.close()

    return jsonify({
        'success': True,
        'task': {
            'id': task['id'],
            'title': task['title'],
            'category': task['category'],
            'task_type': task['task_type'] or 'TEXT_LINK',
            'description': task['description'],
            'payout': payout,
            'per_message_rate': reward_per_number,
            'reward_per_number': reward_per_number,
            'reward_source': task['reward_source'] or 'legacy_snapshot',
            'template_text': task['template_text'],
            'image_url': f"/uploads/task-assets/{task['image_path']}" if task['image_path'] else None,
            'destination_link': task['destination_link'],
            'target_numbers': target_nums,
            'time_limit_minutes': task['time_limit_minutes'],
            'proof_instructions': task['proof_instructions'],
            'total_slots': task['total_slots'],
            'completed_slots': task['completed_slots'],
            'taken_slots': taken_slots,
            'available_slots': available_slots,
            'slots_remaining': available_slots,
            'status': task['status'],
            'requires_assignment': not bool(target_nums),
            'assignment_status': assignment['status'] if user_id and assignment else None,
            'assignment_id': assignment['id'] if user_id and assignment else None,
            'expires_at': assignment['expires_at'] if user_id and assignment else None,
            'user_submission': submission
        }
    })

@app.route('/api/get_task_time/<int:task_id>', methods=['GET'])
@login_required
def get_task_time(task_id):
    """Return server-calculated time remaining; never extend an assignment here."""
    user = get_current_user()
    conn = database.get_db_connection()
    release_expired_assignments(conn)
    conn.commit()
    assignment = conn.execute('''
        SELECT id, status, expires_at, CURRENT_TIMESTAMP AS server_time,
               MAX(0, CAST(strftime('%s', expires_at) AS INTEGER) - CAST(strftime('%s', 'now') AS INTEGER)) AS remaining_seconds
        FROM task_assignments
        WHERE user_id = ? AND task_id = ?
        ORDER BY id DESC LIMIT 1
    ''', (user['id'], task_id)).fetchone()
    conn.close()
    if not assignment:
        return jsonify({'success': False, 'message': 'No task assignment found.'}), 404
    return jsonify({
        'success': True,
        'assignment_id': assignment['id'],
        'status': assignment['status'],
        'expires_at': assignment['expires_at'],
        'server_time': assignment['server_time'],
        'remaining_seconds': assignment['remaining_seconds'] if assignment['status'] == 'in_progress' else 0
    })

@app.route('/api/tasks/<int:task_id>/cancel', methods=['POST'])
@login_required
def cancel_task_assignment(task_id):
    """Cancel an unsubmitted active assignment and release its slot immediately."""
    user = get_current_user()
    conn = database.get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        release_expired_assignments(conn)
        assignment = conn.execute('''
            SELECT * FROM task_assignments
            WHERE user_id = ? AND task_id = ? AND status = 'in_progress'
            ORDER BY id DESC LIMIT 1
        ''', (user['id'], task_id)).fetchone()
        if not assignment:
            conn.rollback()
            return jsonify({'success': False, 'message': 'There is no active task to cancel.'}), 400
        submitted = conn.execute("SELECT 1 FROM submissions WHERE assignment_id = ?", (assignment['id'],)).fetchone()
        if submitted:
            conn.rollback()
            return jsonify({'success': False, 'message': 'Submitted tasks cannot be cancelled.'}), 400
        conn.execute("UPDATE recipient_allocations SET status = 'RELEASED' WHERE assignment_id = ? AND status = 'ALLOCATED'", (assignment['id'],))
        conn.execute("UPDATE task_assignments SET status = 'cancelled', slot_released = 1 WHERE id = ?", (assignment['id'],))
        conn.commit()
        return jsonify({'success': True, 'message': 'Task cancelled and slot released.'})
    finally:
        conn.close()

@app.route('/api/tasks/<int:task_id>/submit', methods=['POST'])
@login_required
def submit_task_proof(task_id):
    user = get_current_user()
    files = request.files.getlist('proof_images') or request.files.getlist('proof_image')
    if not files or any(not file.filename or not allowed_file(file.filename) or not valid_image_content(file) for file in files):
        return jsonify({'success': False, 'message': 'Upload between 1 and 8 PNG, JPG, JPEG, or WEBP screenshots.'}), 400
    if not 1 <= len(files) <= 8:
        return jsonify({'success': False, 'message': 'Upload a minimum of 1 and a maximum of 8 screenshots.'}), 400

    notes = request.form.get('notes', '').strip()

    conn = database.get_db_connection()
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        conn.close()
        return jsonify({'success': False, 'message': 'Task not found.'}), 404

    if task['status'] != 'active':
        conn.close()
        return jsonify({'success': False, 'message': 'This task is no longer active.'}), 400

    release_expired_assignments(conn)
    conn.commit()
    assignment = conn.execute('''
        SELECT * FROM task_assignments
        WHERE user_id = ? AND task_id = ? AND status = 'in_progress'
        ORDER BY id DESC LIMIT 1
    ''', (user['id'], task_id)).fetchone()
    if not assignment:
        conn.close()
        return jsonify({'success': False, 'message': 'Start this campaign first. Proof can only be submitted for your assigned recipients.'}), 400

    # Check if already submitted and pending/approved
    existing = conn.execute("SELECT * FROM submissions WHERE assignment_id = ? AND status IN ('pending', 'approved')", (assignment['id'],)).fetchone()
    if existing:
        conn.close()
        return jsonify({'success': False, 'message': f'You already have a {existing["status"]} submission for this task.'}), 400

    # Save every proof image; the first remains the legacy preview image.
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filenames = []
    file_paths = []
    for index, file in enumerate(files, start=1):
        ext = file.filename.rsplit('.', 1)[1].lower()
        filename = secure_filename(f"proof_u{user['id']}_t{task_id}_{timestamp}_{index}.{ext}")
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(file_path)
        filenames.append(filename)
        file_paths.append(file_path)

    cursor = conn.cursor()

    # Audit only the recipient set assigned to this user, never the campaign pool.
    target_nums = get_assigned_numbers(assignment)

    # Run Automated AI / OCR Screenshot Audit
    # Notes are untrusted user assertions.  They are saved for an administrator
    # but are intentionally excluded from automated proof verification.
    audit_res = ocr_scanner.audit_screenshot_proof(file_paths, target_nums, task['template_text'])
    audit_badge = audit_res['audit_badge']
    audit_score = audit_res['match_percentage']
    numbers_detected = audit_res['numbers_found']
    numbers_required = audit_res['total_targets']
    approval_minimum = approval_threshold(numbers_required)
    # Fix the rate at submission time. Invalid WhatsApp recipients are excluded
    # from the payable count, and later setting changes cannot affect this proof.
    payout_amount = calculate_task_payout(conn, task, numbers_detected)
    per_number_rate = calculate_task_payout(conn, task, 1)
    payout_amount_paise = calculate_task_payout_paise(conn, task, numbers_detected)
    per_number_rate_paise = calculate_task_payout_paise(conn, task, 1)
    audit_passed = audit_res['audit_passed']

    # Phase 2 audit state is recorded per assigned target for later phases.
    # This is verification metadata only: proof auditing never marks a target
    # paid and does not create any wallet/ledger mutation.
    target_reward = calculate_task_payout(conn, task, 1)
    matched_targets = {normalize_phone_number(number) for number in audit_res.get('matched_numbers', [])}
    invalid_targets = {normalize_phone_number(number) for number in audit_res.get('invalid_numbers', [])}
    target_rows = []
    for target in target_nums:
        normalized = normalize_phone_number(target)
        status = 'verified' if normalized in matched_targets else ('rejected' if normalized in invalid_targets else 'assigned')
        target_rows.append((assignment['id'], task_id, task['campaign_id'] or task_id, user['id'], str(target), normalized, status, str(paise_to_inr(per_number_rate_paise)), per_number_rate_paise))
    cursor.executemany('''
        INSERT OR IGNORE INTO target_number_records
            (assignment_id, task_id, campaign_id, user_id, target_number,
             normalized_target_number, status, reward_per_number, reward_per_number_paise,
             verified_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CASE WHEN ? = 'verified' THEN CURRENT_TIMESTAMP ELSE NULL END, CURRENT_TIMESTAMP)
    ''', [row + (row[6],) for row in target_rows])

    # Check if automatic approval is enabled in settings
    setting_row = conn.execute("SELECT value FROM settings WHERE key = 'auto_approve_enabled'").fetchone()
    auto_approve_active = (setting_row and setting_row['value'] == '1')

    is_auto_approved = 1 if (audit_passed and numbers_detected >= approval_minimum and auto_approve_active) else 0
    sub_status = 'approved' if is_auto_approved else 'pending'
    admin_feedback = 'Auto-approved by AI OCR Audit Engine' if is_auto_approved else None
    reviewed_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S') if is_auto_approved else None

    cursor.execute('''
        INSERT INTO submissions (user_id, task_id, assignment_id, proof_image, notes, status, admin_feedback, payout_amount, payout_paise, audit_badge, audit_score, numbers_detected, numbers_required, invalid_count, approved_valid_count, auto_approved, reviewed_at, required_count, verified_count, approval_threshold, per_number_rate, per_number_rate_paise)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (user['id'], task_id, assignment['id'], filenames[0], notes, sub_status, admin_feedback, str(paise_to_inr(payout_amount_paise)), payout_amount_paise, audit_badge, audit_score, numbers_detected, numbers_required, audit_res['invalid_count'], numbers_detected if is_auto_approved else None, is_auto_approved, reviewed_at, numbers_required, numbers_detected, approval_minimum, str(paise_to_inr(per_number_rate_paise)), per_number_rate_paise))
    sub_id = cursor.lastrowid
    if is_auto_approved:
        settle_recipient_allocations_for_approval(conn, assignment['id'])
        finalize_target_records_for_approval(
            conn, assignment['id'], numbers_detected, per_number_rate,
            submission_id=sub_id, task_id=task_id,
            campaign_id=task['campaign_id'] or task_id, user_id=user['id']
        )
    cursor.executemany('''
        INSERT INTO task_proofs (submission_id, file_name, sort_order)
        VALUES (?, ?, ?)
    ''', [(sub_id, filename, index) for index, filename in enumerate(filenames, start=1)])

    cursor.execute('''
        UPDATE task_assignments
        SET status = ?, completed_at = CASE WHEN ? = 'paid' THEN CURRENT_TIMESTAMP ELSE NULL END
        WHERE id = ?
    ''', ('paid' if is_auto_approved else 'pending_review', 'paid' if is_auto_approved else 'pending_review', assignment['id']))

    # If auto-approved, credit user wallet immediately
    if is_auto_approved:
        ensure_exact_user_balance(conn, user['id'])
        balance_paise = apply_wallet_delta(cursor, user['id'], payout_amount_paise)
        cursor.execute("UPDATE users SET referred_user_task_count = referred_user_task_count + 1 WHERE id = ?", (user['id'],))
        insert_wallet_transaction(cursor, user['id'], 'task_reward', payout_amount_paise, balance_paise, f"AUTO-SUB-{sub_id}", f"Auto-approved reward for: {task['title']}", sub_id)
        credit_referrer_for_task(cursor, user, payout_amount, sub_id)

    # Increment completed slots count
    cursor.execute("UPDATE tasks SET completed_slots = completed_slots + 1 WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()

    if is_auto_approved:
        msg = f"🎉 Auto-Verified! {audit_badge}. ₹{payout_amount:.2f} credited instantly to your wallet!"
    else:
        msg = f"Proof submitted & analyzed! {audit_badge}."

    return jsonify({
        'success': True,
        'submission_id': sub_id,
        'proof_count': len(filenames),
        'audit_badge': audit_badge,
        'audit_passed': audit_passed,
        'auto_approved': bool(is_auto_approved),
        'message': msg
    })

# ----------------- WALLET & WITHDRAWAL APIS -----------------
@app.route('/api/wallet/summary', methods=['GET'])
@login_required
def wallet_summary():
    user = get_current_user()
    conn = database.get_db_connection()

    # Calculate pending earnings
    pending_row = conn.execute('''
        SELECT SUM(payout_amount) as total_pending 
        FROM submissions 
        WHERE user_id = ? AND status = 'pending'
    ''', (user['id'],)).fetchone()
    total_pending = pending_row['total_pending'] or 0.0

    # A withdrawal is disbursed only after manual payout has a provider reference.
    withdrawn_row = conn.execute('''
        SELECT SUM(amount) as total_withdrawn 
        FROM withdrawals 
        WHERE user_id = ? AND status = 'paid'
    ''', (user['id'],)).fetchone()
    total_withdrawn = withdrawn_row['total_withdrawn'] or 0.0

    # Transactions
    txs = conn.execute('''
        SELECT * FROM wallet_transactions 
        WHERE user_id = ? 
        ORDER BY id DESC LIMIT 20
    ''', (user['id'],)).fetchall()

    tx_list = []
    for t in txs:
        tx_list.append({
            'id': t['id'],
            'type': t['type'],
            'amount': round(t['amount'], 2),
            'balance_after': round(t['balance_after'], 2),
            'reference_id': t['reference_id'],
            'description': t['description'],
            'created_at': t['created_at']
        })

    # Withdrawals list
    wds = conn.execute('''
        SELECT * FROM withdrawals 
        WHERE user_id = ? 
        ORDER BY id DESC LIMIT 15
    ''', (user['id'],)).fetchall()

    wd_list = []
    for w in wds:
        wd_list.append({
            'id': w['id'],
            'amount': round(w['amount'], 2),
            'payout_method': w['payout_method'],
            'upi_id': w['upi_id'],
            'status': w['status'],
            'utr_number': w['utr_number'],
            'currency': w['currency'] if 'currency' in w.keys() else 'INR',
            'usdt_amount': float(w['usdt_amount']) if 'usdt_amount' in w.keys() and w['usdt_amount'] is not None else None,
            'usdt_inr_rate': float(w['usdt_inr_rate']) if 'usdt_inr_rate' in w.keys() and w['usdt_inr_rate'] is not None else None,
            'usdt_network': w['usdt_network'] if 'usdt_network' in w.keys() else None,
            'provider_reference': w['provider_reference'] if 'provider_reference' in w.keys() else None,
            'transaction_hash': w['transaction_hash'] if 'transaction_hash' in w.keys() else None,
            'requested_at': w['requested_at'],
            'processed_at': w['processed_at']
        })

    conn.close()

    return jsonify({
        'success': True,
        'wallet': {
            'balance': round(user['wallet_balance'], 2),
            'total_earned': round(user['total_earned'], 2),
            'pending_earnings': round(total_pending, 2),
            'total_withdrawn': round(total_withdrawn, 2)
        },
        'transactions': tx_list,
        'withdrawals': wd_list
    })

@app.route('/api/wallet/withdraw', methods=['POST'])
@login_required
def withdraw_funds():
    user = get_current_user()
    data = request.get_json() or {}

    try:
        amount_decimal = Decimal(str(data.get('amount', 0))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except Exception:
        return jsonify({'success': False, 'message': 'Invalid withdrawal amount.'}), 400
    amount_paise = inr_to_paise(amount_decimal)

    method = data.get('payout_method', 'upi')
    upi_id = (data.get('upi_id') or '').strip()
    bank_account = (data.get('bank_account') or '').strip()
    bank_ifsc = (data.get('bank_ifsc') or '').strip()
    bank_holder = (data.get('bank_holder_name') or '').strip()
    usdt_network = (data.get('usdt_network') or '').strip().upper()
    usdt_wallet_address = (data.get('usdt_wallet_address') or '').strip()

    if method == 'upi' and not upi_id:
        return jsonify({'success': False, 'message': 'Valid UPI ID is required (e.g. yourname@upi).'}), 400

    if method == 'bank' and (not bank_account or not bank_ifsc or not bank_holder):
        return jsonify({'success': False, 'message': 'Account number, IFSC code, and Account holder name are required.'}), 400

    conn = database.get_db_connection()
    try:
        # SQLite BEGIN IMMEDIATE serializes competing debits. The conditional
        # update independently verifies the current balance before reserving it.
        conn.execute('BEGIN IMMEDIATE')
        minimum = get_setting_decimal(conn, 'minimum_withdrawal_amount', '50')
        if amount_decimal < minimum:
            conn.rollback()
            return jsonify({'success': False, 'message': f'Minimum withdrawal is ₹{minimum:.2f}.'}), 400
        cursor = conn.cursor()
        currency = 'USDT' if method == 'usdt' else 'INR'
        usdt_amount = None
        usdt_rate = None
        if method == 'usdt':
            configured_network = get_setting_text(conn, 'usdt_network', 'TRC20').upper()
            if usdt_network != configured_network:
                conn.rollback()
                return jsonify({'success': False, 'message': f'USDT withdrawals are currently available only on {configured_network}.'}), 400
            if not valid_usdt_address(usdt_network, usdt_wallet_address):
                conn.rollback()
                return jsonify({'success': False, 'message': f'Invalid {usdt_network} wallet address.'}), 400
            usdt_rate = get_setting_decimal(conn, 'usdt_inr_rate', '90')
            if usdt_rate <= 0:
                conn.rollback()
                return jsonify({'success': False, 'message': 'USDT payout rate is unavailable.'}), 400
            usdt_amount = (amount_decimal / usdt_rate).quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP)
        ensure_exact_user_balance(conn, user['id'])
        cursor.execute("UPDATE users SET wallet_balance_paise = wallet_balance_paise - ?, wallet_balance = wallet_balance - ? WHERE id = ? AND wallet_balance_paise >= ?", (amount_paise, str(paise_to_inr(amount_paise)), user['id'], amount_paise))
        if cursor.rowcount != 1:
            conn.rollback()
            return jsonify({'success': False, 'message': 'Insufficient wallet balance.'}), 400
        new_balance_paise = cursor.execute("SELECT wallet_balance_paise FROM users WHERE id = ?", (user['id'],)).fetchone()['wallet_balance_paise']
        new_balance = str(paise_to_inr(new_balance_paise))

        # 2. Insert withdrawal request
        cursor.execute('''
            INSERT INTO withdrawals (user_id, amount, amount_paise, payout_method, upi_id, bank_account, bank_ifsc, bank_holder_name, status, currency, usdt_amount, usdt_amount_micro, usdt_inr_rate, usdt_inr_rate_scaled, usdt_network, usdt_wallet_address)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'requested', ?, ?, ?, ?, ?, ?, ?)
        ''', (user['id'], str(paise_to_inr(amount_paise)), amount_paise, method, upi_id, bank_account, bank_ifsc, bank_holder, currency, str(usdt_amount) if usdt_amount is not None else None, usdt_to_micro(usdt_amount) if usdt_amount is not None else None, str(usdt_rate) if usdt_rate is not None else None, rate_paise_per_unit(usdt_rate) if usdt_rate is not None else None, usdt_network or None, usdt_wallet_address or None))
        wd_id = cursor.lastrowid

        # 3. The balance reservation is always represented in the ledger.
        desc = f"Withdrawal request via {method.upper()}: {upi_id if method == 'upi' else bank_account[-4:]}"
        insert_wallet_transaction(cursor, user['id'], 'withdrawal', -amount_paise, new_balance_paise, f"WDR-{wd_id}", desc)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return jsonify({
        'success': True,
        'message': f"Withdrawal request for ₹{paise_to_inr(amount_paise):.2f} submitted! Funds will be disbursed to your {method.upper()} shortly.",
        'new_balance': new_balance
        , 'currency': currency, 'usdt_amount': str(usdt_amount) if usdt_amount is not None else None,
        'usdt_inr_rate': str(usdt_rate) if usdt_rate is not None else None
    })

# ----------------- ADMIN APIS -----------------
@app.route('/api/admin/stats', methods=['GET'])
@admin_required
def admin_stats():
    conn = database.get_db_connection()

    total_users = conn.execute("SELECT COUNT(*) as c FROM users WHERE role = 'user'").fetchone()['c']
    pending_subs = conn.execute("SELECT COUNT(*) as c FROM submissions WHERE status = 'pending'").fetchone()['c']
    pending_wds = conn.execute("SELECT COUNT(*) as c FROM withdrawals WHERE status IN ('requested', 'under_review', 'approved', 'processing')").fetchone()['c']
    active_tasks = conn.execute("SELECT COUNT(*) as c FROM tasks WHERE status = 'active'").fetchone()['c']

    disbursed_row = conn.execute("SELECT SUM(amount) as s FROM withdrawals WHERE status = 'paid'").fetchone()
    total_disbursed = disbursed_row['s'] or 0.0

    conn.close()

    return jsonify({
        'success': True,
        'stats': {
            'total_users': total_users,
            'pending_submissions': pending_subs,
            'pending_withdrawals': pending_wds,
            'active_tasks': active_tasks,
            'total_disbursed': round(total_disbursed, 2)
        }
    })

@app.route('/api/admin/submissions', methods=['GET'])
@admin_required
def admin_submissions():
    conn = database.get_db_connection()
    subs = conn.execute('''
        SELECT s.*, u.name as user_name, u.mobile as user_mobile, u.email as user_email,
               t.title as task_title, t.category as task_category
        FROM submissions s
        JOIN users u ON s.user_id = u.id
        JOIN tasks t ON s.task_id = t.id
        ORDER BY CASE WHEN s.status = 'pending' THEN 0 ELSE 1 END, s.id DESC
    ''').fetchall()

    result = []
    for s in subs:
        proof_rows = conn.execute("SELECT file_name FROM task_proofs WHERE submission_id = ? ORDER BY sort_order", (s['id'],)).fetchall()
        proof_files = [row['file_name'] for row in proof_rows if row['file_name']]
        if not proof_files and s['proof_image']:
            proof_files = [s['proof_image']]
        proof_urls = [f"/uploads/proofs/{filename}" for filename in proof_files]
        result.append({
            'id': s['id'],
            'user_id': s['user_id'],
            'user_name': s['user_name'],
            'user_mobile': s['user_mobile'],
            'user_email': s['user_email'],
            'task_id': s['task_id'],
            'task_title': s['task_title'],
            'task_category': s['task_category'],
            'proof_image': s['proof_image'],
            'proof_url': proof_urls[0] if proof_urls else None,
            'proof_urls': proof_urls,
            'proof_count': len(proof_files),
            'notes': s['notes'],
            'status': s['status'],
            'admin_feedback': s['admin_feedback'],
            'payout_amount': round(s['payout_amount'], 2),
            'audit_badge': s['audit_badge'] if 'audit_badge' in s.keys() and s['audit_badge'] else "Audit pending",
            'audit_score': s['audit_score'] if 'audit_score' in s.keys() and s['audit_score'] is not None else 0,
            'numbers_detected': s['numbers_detected'] if 'numbers_detected' in s.keys() and s['numbers_detected'] is not None else 0,
            'numbers_required': s['numbers_required'] if 'numbers_required' in s.keys() and s['numbers_required'] is not None else 0,
            'required_count': s['required_count'] if 'required_count' in s.keys() and s['required_count'] is not None else (s['numbers_required'] or 0),
            'verified_count': s['verified_count'] if 'verified_count' in s.keys() and s['verified_count'] is not None else (s['numbers_detected'] or 0),
            'approval_threshold': s['approval_threshold'] if 'approval_threshold' in s.keys() and s['approval_threshold'] is not None else approval_threshold(s['numbers_required'] or 0),
            'invalid_count': s['invalid_count'] if 'invalid_count' in s.keys() and s['invalid_count'] is not None else 0,
            'approved_valid_count': s['approved_valid_count'] if 'approved_valid_count' in s.keys() and s['approved_valid_count'] is not None else None,
            'auto_approved': bool(s['auto_approved']) if 'auto_approved' in s.keys() and s['auto_approved'] else False,
            'submitted_at': s['submitted_at'],
            'reviewed_at': s['reviewed_at']
        })

    conn.close()
    return jsonify({'success': True, 'submissions': result})

@app.route('/api/admin/settings', methods=['GET'])
@admin_required
def get_admin_settings():
    conn = database.get_db_connection()
    row = conn.execute("SELECT value FROM settings WHERE key = 'auto_approve_enabled'").fetchone()
    per_message_rate = get_setting_number(conn, 'per_message_rate', 3.0)
    referral_limit_tasks = int(get_setting_number(conn, 'referral_limit_tasks', 20))
    referral_commission_percent = get_setting_number(conn, 'referral_commission_percent', 10.0)
    minimum_withdrawal_amount = get_setting_number(conn, 'minimum_withdrawal_amount', 50.0)
    usdt_inr_rate = get_setting_number(conn, 'usdt_inr_rate', 90.0)
    usdt_network = get_setting_text(conn, 'usdt_network', 'TRC20')
    conn.close()
    enabled = row['value'] == '1' if row else False
    return jsonify({
        'success': True,
        'auto_approve_enabled': enabled,
        'per_message_rate': per_message_rate,
        'referral_limit_tasks': referral_limit_tasks,
        'referral_commission_percent': referral_commission_percent,
        'minimum_withdrawal_amount': minimum_withdrawal_amount
        , 'usdt_inr_rate': usdt_inr_rate, 'usdt_network': usdt_network,
        'payout_mode': PAYOUT_MODE
    })

@app.route('/api/withdrawal-settings', methods=['GET'])
@login_required
def withdrawal_settings():
    conn = database.get_db_connection()
    result = {
        'success': True,
        'minimum_withdrawal_amount': get_setting_number(conn, 'minimum_withdrawal_amount', 50.0),
        'usdt_inr_rate': get_setting_number(conn, 'usdt_inr_rate', 90.0),
        'usdt_network': get_setting_text(conn, 'usdt_network', 'TRC20')
    }
    conn.close()
    return jsonify(result)

@app.route('/api/admin/settings/payment', methods=['POST'])
@admin_required
def update_payment_settings():
    data = request.get_json() or {}
    try:
        per_message_rate = decimal_value(data.get('per_message_rate'))
        referral_limit_tasks = int(data.get('referral_limit_tasks'))
        referral_commission_percent = decimal_value(data.get('referral_commission_percent', 10))
        minimum_withdrawal_amount = decimal_value(data.get('minimum_withdrawal_amount', 50))
        usdt_inr_rate = decimal_value(data.get('usdt_inr_rate', 90))
    except (TypeError, ValueError, ArithmeticError):
        return jsonify({'success': False, 'message': 'Enter a valid per-message rate and referral task limit.'}), 400
    if per_message_rate <= 0 or referral_limit_tasks < 1 or referral_commission_percent < 0 or minimum_withdrawal_amount <= 0 or usdt_inr_rate <= 0:
        return jsonify({'success': False, 'message': 'Payment settings must contain positive values (referral commission may be zero).'}), 400

    conn = database.get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        exact_settings = (
            ('per_message_rate', per_message_rate, inr_to_paise(per_message_rate), 100, 'inr_paise', 'INR'),
            ('minimum_withdrawal_amount', minimum_withdrawal_amount, inr_to_paise(minimum_withdrawal_amount), 100, 'inr_paise', 'INR'),
            ('usdt_inr_rate', usdt_inr_rate, inr_to_paise(usdt_inr_rate), 100, 'inr_per_usdt_paise', 'INR/USDT'),
            ('referral_commission_percent', referral_commission_percent, percentage_to_bps(referral_commission_percent), 10000, 'percent_bps', 'percent'),
        )
        history_rows = []
        for setting_key, new_value, new_scaled, scale, kind, unit in exact_settings:
            exact_row = conn.execute(
                'SELECT value_scaled, scale FROM exact_money_settings WHERE key = ?',
                (setting_key,),
            ).fetchone()
            if exact_row and exact_row['value_scaled'] is not None and exact_row['scale']:
                old_scaled = int(exact_row['value_scaled'])
                old_value = Decimal(old_scaled) / Decimal(int(exact_row['scale']))
            else:
                old_value = get_setting_decimal(conn, setting_key, str(new_value))
                old_scaled = percentage_to_bps(old_value) if kind == 'percent_bps' else inr_to_paise(old_value)
            if old_scaled != int(new_scaled):
                history_rows.append((setting_key, format(old_value, 'f'), format(new_value, 'f'), unit))

            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (setting_key, format(new_value, 'f')),
            )
            conn.execute('''
                INSERT INTO exact_money_settings (key, value_scaled, scale, kind, migrated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value_scaled=excluded.value_scaled,
                    scale=excluded.scale, kind=excluded.kind, migrated_at=CURRENT_TIMESTAMP
            ''', (setting_key, int(new_scaled), scale, kind))

        current_limit = int(get_setting_number(conn, 'referral_limit_tasks', 20))
        if current_limit != referral_limit_tasks:
            history_rows.append(('referral_limit_tasks', str(current_limit), str(referral_limit_tasks), 'tasks'))
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('referral_limit_tasks', ?)",
            (str(referral_limit_tasks),),
        )

        changed_by = session.get('user_id')
        reason = (data.get('reason') or '').strip() or None
        for setting_key, old_value, new_value, unit in history_rows:
            conn.execute('''
                INSERT INTO finance_settings_history
                    (setting_name, old_value, new_value, changed_by, unit, reason)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (setting_key, old_value, new_value, changed_by, unit, reason))
        conn.commit()
        return jsonify({'success': True, 'message': 'Payment settings saved.', 'per_message_rate': float(per_message_rate)})
    except sqlite3.Error:
        conn.rollback()
        return jsonify({'success': False, 'message': 'Payment settings could not be saved.'}), 500
    finally:
        conn.close()


@app.route('/api/admin/settings/history', methods=['GET'])
@admin_required
def get_finance_settings_history():
    conn = database.get_db_connection()
    try:
        rows = conn.execute('''
            SELECT h.id, h.setting_name, h.old_value, h.new_value, h.changed_by,
                   h.changed_at, h.unit, h.reason, u.name AS changed_by_name
            FROM finance_settings_history h
            LEFT JOIN users u ON u.id = h.changed_by
            ORDER BY h.changed_at DESC, h.id DESC
            LIMIT 200
        ''').fetchall()
        return jsonify({'success': True, 'history': [dict(row) for row in rows]})
    finally:
        conn.close()

@app.route('/api/admin/settings/toggle-auto-approve', methods=['POST'])
@admin_required
def toggle_auto_approve():
    conn = database.get_db_connection()
    row = conn.execute("SELECT value FROM settings WHERE key = 'auto_approve_enabled'").fetchone()
    current = row['value'] if row else '0'
    new_val = '0' if current == '1' else '1'
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('auto_approve_enabled', ?)", (new_val,))
    conn.commit()
    conn.close()
    is_on = (new_val == '1')
    return jsonify({
        'success': True, 
        'auto_approve_enabled': is_on,
        'message': f"Auto-approval of passed audits is now {'ENABLED' if is_on else 'DISABLED'}."
    })

@app.route('/api/admin/submissions/<int:sub_id>/rescan', methods=['POST'])
@admin_required
def admin_rescan_submission(sub_id):
    conn = database.get_db_connection()
    sub = conn.execute("SELECT * FROM submissions WHERE id = ?", (sub_id,)).fetchone()
    if not sub:
        conn.close()
        return jsonify({'success': False, 'message': 'Submission not found'}), 404
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (sub['task_id'],)).fetchone()
    proof_rows = conn.execute("SELECT file_name FROM task_proofs WHERE submission_id = ? ORDER BY sort_order", (sub_id,)).fetchall()
    proof_files = [row['file_name'] for row in proof_rows] or [sub['proof_image']]
    file_paths = [os.path.join(app.config['UPLOAD_FOLDER'], filename) for filename in proof_files]
    assignment = conn.execute("SELECT * FROM task_assignments WHERE id = ?", (sub['assignment_id'],)).fetchone() if sub['assignment_id'] else None
    target_nums = get_assigned_numbers(assignment) if assignment else task_recipient_pool(task)
    audit_res = ocr_scanner.audit_screenshot_proof(file_paths, target_nums, task['template_text'])
    # A re-scan is allowed to revoke an earlier automatic decision.  This does
    # not touch manually approved work; it only quarantines submissions that
    # were previously approved by the vulnerable automated audit.
    revoke_auto_approval = (
        not audit_res['audit_passed']
        and sub['status'] == 'approved'
        and bool(sub['auto_approved'])
    )

    if revoke_auto_approval:
        feedback = 'Auto-approval revoked after proof re-audit failed. Manual review required.'
        conn.execute('''
            UPDATE submissions
            SET audit_badge = ?, audit_score = ?, numbers_detected = ?, numbers_required = ?, invalid_count = ?,
                required_count = ?, verified_count = ?, approval_threshold = ?,
                status = 'pending', auto_approved = 0, admin_feedback = ?, reviewed_at = NULL
            WHERE id = ?
        ''', (audit_res['audit_badge'], audit_res['match_percentage'], audit_res['numbers_found'], audit_res['total_targets'], audit_res['invalid_count'], audit_res['total_targets'], audit_res['numbers_found'], approval_threshold(audit_res['total_targets']), feedback, sub_id))

        cursor = conn.cursor()
        debit_paise = sub['payout_paise'] if sub['payout_paise'] is not None else inr_to_paise(sub['payout_amount'])
        ensure_exact_user_balance(conn, sub['user_id'])
        user_balance_paise = apply_wallet_delta(cursor, sub['user_id'], -debit_paise, require_balance=True)
        if user_balance_paise is None:
            conn.rollback()
            return jsonify({'success': False, 'message': 'Worker wallet does not contain enough balance for a safe reversal.'}), 409
        cursor.execute('UPDATE users SET referred_user_task_count = MAX(0, referred_user_task_count - 1) WHERE id = ?', (sub['user_id'],))
        insert_wallet_transaction(cursor, sub['user_id'], 'audit_reversal', -debit_paise,
                                  user_balance_paise, f"AUDIT-REVERSAL-{sub_id}",
                                  "Automatic proof approval reversed after failed proof re-audit")
        referral_credit = cursor.execute('''
            SELECT user_id, amount, amount_paise FROM wallet_transactions
            WHERE reference_id = ? AND type = 'referral_task_commission'
            ORDER BY id DESC LIMIT 1
        ''', (f"REF-TASK-{sub_id}",)).fetchone()
        if referral_credit:
            referral_paise = referral_credit['amount_paise'] if referral_credit['amount_paise'] is not None else inr_to_paise(referral_credit['amount'])
            ensure_exact_user_balance(conn, referral_credit['user_id'])
            referrer_balance_paise = apply_wallet_delta(cursor, referral_credit['user_id'], -referral_paise, require_balance=True)
            if referrer_balance_paise is None:
                conn.rollback()
                return jsonify({'success': False, 'message': 'Referrer wallet does not contain enough balance for a safe reversal.'}), 409
            insert_wallet_transaction(cursor, referral_credit['user_id'], 'referral_reversal', -referral_paise,
                                      referrer_balance_paise, f"REF-REVERSAL-{sub_id}",
                                      'Referral commission reversed after proof re-audit')
        cursor.execute('''
            UPDATE task_assignments SET status = 'pending_review', completed_at = NULL
            WHERE id = ? AND status = 'paid'
        ''', (sub['assignment_id'],))
    else:
        conn.execute('''
            UPDATE submissions
            SET audit_badge = ?, audit_score = ?, numbers_detected = ?, numbers_required = ?, invalid_count = ?,
                required_count = ?, verified_count = ?, approval_threshold = ?
            WHERE id = ?
        ''', (audit_res['audit_badge'], audit_res['match_percentage'], audit_res['numbers_found'], audit_res['total_targets'], audit_res['invalid_count'], audit_res['total_targets'], audit_res['numbers_found'], approval_threshold(audit_res['total_targets']), sub_id))
    conn.commit()
    conn.close()
    return jsonify({
        'success': True,
        'audit': audit_res,
        'auto_approval_revoked': revoke_auto_approval,
        'message': f"Screenshot re-audited: {audit_res['audit_badge']}"
    })

@app.route('/api/admin/submissions/<int:sub_id>/approve', methods=['POST'])
@admin_required
def admin_approve_submission(sub_id):
    conn = database.get_db_connection()
    try:
        # Lock the approval decision and all related wallet/ledger mutations.
        conn.execute('BEGIN IMMEDIATE')
        sub = conn.execute("SELECT * FROM submissions WHERE id = ?", (sub_id,)).fetchone()
        if not sub:
            return jsonify({'success': False, 'message': 'Submission not found.'}), 404
        if sub['status'] != 'pending':
            return jsonify({'success': False, 'message': 'Only pending submissions can be approved.'}), 400

        required_count = submission_required_count(sub)
        verified_count = submission_verified_count(sub)
        minimum_required = approval_threshold(required_count)
        if required_count <= 0:
            return jsonify({'success': False, 'message': 'No assigned targets are available for approval.'}), 400
        if verified_count < minimum_required:
            return jsonify({'success': False, 'message': f'Minimum {minimum_required} verified targets are required for approval.'}), 400
        rate_paise = inr_to_paise(submission_per_number_rate(conn, sub))
        payout_paise = rate_paise * verified_count
        payout = float(paise_to_inr(payout_paise))

        task = conn.execute("SELECT title FROM tasks WHERE id = ?", (sub['task_id'],)).fetchone()
        task_title = task['title'] if task else f"Task #{sub['task_id']}"
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE submissions SET status = 'approved', payout_amount = ?, payout_paise = ?, per_number_rate_paise = ?, admin_feedback = 'Approved by Admin', approved_valid_count = ?, reviewed_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'pending'
        ''', (str(paise_to_inr(payout_paise)), payout_paise, rate_paise, verified_count, sub_id))
        if cursor.rowcount != 1:
            return jsonify({'success': False, 'message': 'This submission was already reviewed.'}), 409
        cursor.execute("UPDATE task_assignments SET status = 'paid', completed_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'pending_review'", (sub['assignment_id'],))
        ensure_exact_user_balance(conn, sub['user_id'])
        balance_paise = apply_wallet_delta(cursor, sub['user_id'], payout_paise)
        cursor.execute("UPDATE users SET referred_user_task_count = referred_user_task_count + 1 WHERE id = ?", (sub['user_id'],))
        insert_wallet_transaction(cursor, sub['user_id'], 'task_reward', payout_paise, balance_paise, f"SUB-{sub_id}", f"Approved reward for: {task_title}", sub_id)
        worker = cursor.execute("SELECT * FROM users WHERE id = ?", (sub['user_id'],)).fetchone()
        credit_referrer_for_task(cursor, worker, paise_to_inr(payout_paise), sub_id)
        settle_recipient_allocations_for_approval(conn, sub['assignment_id'])
        finalize_target_records_for_approval(
            conn, sub['assignment_id'], verified_count,
            str(paise_to_inr(rate_paise)),
            submission_id=sub_id, task_id=sub['task_id'], user_id=sub['user_id']
        )
        conn.commit()
        return jsonify({'success': True, 'approved_valid_count': verified_count, 'payout': payout, 'message': f"Submission #{sub_id} approved! ₹{payout:.2f} credited to user's wallet."})
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        # Early validation returns also occur after BEGIN IMMEDIATE; release
        # that write lock explicitly instead of relying on connection close.
        if conn.in_transaction:
            conn.rollback()
        conn.close()

@app.route('/api/admin/submissions/<int:sub_id>/approve-partial', methods=['POST'])
@admin_required
def admin_approve_partial_submission(sub_id):
    """Approve a verified/manual recipient count and credit only its pro-rata value."""
    data = request.get_json() or {}
    try:
        valid_count = int(data.get('valid_count'))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'A whole-number valid recipient count is required.'}), 400

    conn = database.get_db_connection()
    try:
        # Keep partial approval's submission, reward, assignment and referral
        # changes under the same write lock as a full approval.
        conn.execute('BEGIN IMMEDIATE')
        sub = conn.execute("SELECT * FROM submissions WHERE id = ?", (sub_id,)).fetchone()
        if not sub:
            return jsonify({'success': False, 'message': 'Submission not found.'}), 404
        if sub['status'] != 'pending':
            return jsonify({'success': False, 'message': 'Only pending submissions can be approved.'}), 400

        task = conn.execute("SELECT title FROM tasks WHERE id = ?", (sub['task_id'],)).fetchone()
        required_count = submission_required_count(sub)
        verified_count = submission_verified_count(sub)
        minimum_required = approval_threshold(required_count)
        if required_count <= 0:
            return jsonify({'success': False, 'message': 'No valid recipients remain for this submission.'}), 400
        if valid_count < minimum_required:
            return jsonify({'success': False, 'message': f'Minimum {minimum_required} verified targets are required for approval.'}), 400
        if valid_count > verified_count:
            return jsonify({'success': False, 'message': f'Cannot approve {valid_count}; audit verified only {verified_count} unique targets.'}), 400
        if valid_count > required_count:
            return jsonify({'success': False, 'message': f'Valid count cannot exceed {required_count} required targets.'}), 400
        if valid_count == 0:
            return jsonify({'success': False, 'message': 'Use Reject when no valid messages should be paid.'}), 400

    # The submitted payout contains the rate effective for this proof.  Using it
    # preserves historical rewards when an administrator later changes the rate.
        rate = submission_per_number_rate(conn, sub)
        rate_paise = inr_to_paise(rate)
        payout_paise = rate_paise * valid_count
        payout = float(paise_to_inr(payout_paise))
        feedback = f'Partially approved by Admin: {valid_count}/{required_count} valid messages; ₹{payout:.2f} credited.'
        cursor = conn.cursor()
        cursor.execute('''
        UPDATE submissions
        SET status = 'approved', payout_amount = ?, payout_paise = ?, per_number_rate_paise = ?, approved_valid_count = ?,
            auto_approved = 0, admin_feedback = ?, reviewed_at = CURRENT_TIMESTAMP
        WHERE id = ? AND status = 'pending'
        ''', (str(paise_to_inr(payout_paise)), payout_paise, rate_paise, valid_count, feedback, sub_id))
        if cursor.rowcount != 1:
            return jsonify({'success': False, 'message': 'This submission was already reviewed.'}), 409
        cursor.execute('''
        UPDATE users
        SET referred_user_task_count = referred_user_task_count + 1
        WHERE id = ?
        ''', (sub['user_id'],))
        cursor.execute('''
        UPDATE task_assignments SET status = 'paid', completed_at = CURRENT_TIMESTAMP
        WHERE id = ? AND status = 'pending_review'
        ''', (sub['assignment_id'],))
        ensure_exact_user_balance(conn, sub['user_id'])
        balance_paise = apply_wallet_delta(cursor, sub['user_id'], payout_paise)
        insert_wallet_transaction(cursor, sub['user_id'], 'task_reward', payout_paise, balance_paise, f"PARTIAL-SUB-{sub_id}", feedback, sub_id)
        worker = cursor.execute("SELECT * FROM users WHERE id = ?", (sub['user_id'],)).fetchone()
        credit_referrer_for_task(cursor, worker, paise_to_inr(payout_paise), sub_id)
        settle_recipient_allocations_for_approval(conn, sub['assignment_id'])
        finalize_target_records_for_approval(
        conn, sub['assignment_id'], valid_count,
        str(paise_to_inr(rate_paise)),
        submission_id=sub_id, task_id=sub['task_id'], user_id=sub['user_id']
        )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()

    return jsonify({
        'success': True,
        'payout': payout,
        'valid_count': valid_count,
        'required_count': required_count,
        'message': f'Submission #{sub_id} partially approved: ₹{payout:.2f} credited for {valid_count}/{required_count} messages.'
    })

@app.route('/api/admin/submissions/<int:sub_id>/reject', methods=['POST'])
@admin_required
def admin_reject_submission(sub_id):
    data = request.get_json() or {}
    reason = (data.get('reason') or 'Proof screenshot does not meet criteria.').strip()

    conn = database.get_db_connection()
    sub = conn.execute("SELECT * FROM submissions WHERE id = ?", (sub_id,)).fetchone()
    if not sub:
        conn.close()
        return jsonify({'success': False, 'message': 'Submission not found.'}), 404

    if sub['status'] != 'pending':
        conn.close()
        return jsonify({'success': False, 'message': 'Only pending submissions can be rejected. Use the dedicated reversal flow for an approved reward.'}), 409

    cursor = conn.cursor()
    cursor.execute('''
        UPDATE submissions 
        SET status = 'rejected', admin_feedback = ?, reviewed_at = CURRENT_TIMESTAMP 
        WHERE id = ? AND status = 'pending'
    ''', (reason, sub_id))
    if cursor.rowcount != 1:
        conn.rollback()
        conn.close()
        return jsonify({'success': False, 'message': 'Submission was already reviewed.'}), 409
    cursor.execute('''
        UPDATE task_assignments SET status = 'rejected', completed_at = CURRENT_TIMESTAMP
        WHERE id = ? AND status IN ('in_progress', 'pending_review')
    ''', (sub['assignment_id'],))
    cursor.execute("UPDATE recipient_allocations SET status = 'RELEASED' WHERE assignment_id = ? AND status = 'ALLOCATED'", (sub['assignment_id'],))

    # Release 1 completed slot
    cursor.execute("UPDATE tasks SET completed_slots = MAX(0, completed_slots - 1) WHERE id = ?", (sub['task_id'],))

    conn.commit()
    conn.close()

    return jsonify({'success': True, 'message': f"Submission #{sub_id} marked as rejected."})

@app.route('/api/admin/submissions/<int:sub_id>/reverse', methods=['POST'])
@admin_required
def reverse_approved_submission(sub_id):
    """Dedicated audited correction path; normal rejection never debits paid work."""
    reason = ((request.get_json() or {}).get('reason') or '').strip()
    if not reason:
        return jsonify({'success': False, 'message': 'A reversal reason is required.'}), 400
    conn = database.get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        sub = conn.execute("SELECT * FROM submissions WHERE id = ?", (sub_id,)).fetchone()
        if not sub or sub['status'] != 'approved':
            conn.rollback()
            return jsonify({'success': False, 'message': 'Only approved submissions can be reversed.'}), 409
        debit_paise = sub['payout_paise'] if sub['payout_paise'] is not None else inr_to_paise(sub['payout_amount'])
        ensure_exact_user_balance(conn, sub['user_id'])
        balance_after_paise = apply_wallet_delta(conn.cursor(), sub['user_id'], -debit_paise, require_balance=True)
        if balance_after_paise is None:
            conn.rollback()
            return jsonify({'success': False, 'message': 'Worker wallet does not contain enough balance for a safe reversal.'}), 409
        cursor = conn.cursor()
        cursor.execute('UPDATE users SET referred_user_task_count = MAX(0, referred_user_task_count - 1) WHERE id = ?', (sub['user_id'],))
        cursor = conn.execute("UPDATE submissions SET status = 'reversed', admin_feedback = ?, reviewed_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'approved'", (reason, sub_id))
        if cursor.rowcount != 1:
            conn.rollback()
            return jsonify({'success': False, 'message': 'Submission was already changed.'}), 409
        conn.execute("UPDATE task_assignments SET status = 'reversed' WHERE id = ? AND status = 'paid'", (sub['assignment_id'],))
        conn.execute("UPDATE tasks SET completed_slots = MAX(0, completed_slots - 1) WHERE id = ?", (sub['task_id'],))
        insert_wallet_transaction(cursor, sub['user_id'], 'task_reward_reversal', -debit_paise,
                                  balance_after_paise, f"REVERSAL-SUB-{sub_id}", reason)

        # A referral commission belongs to this exact approval. Reverse it in
        # the same transaction so worker/referrer balances and both ledgers
        # remain consistent.
        referral_credit = conn.execute('''
            SELECT user_id, amount, amount_paise FROM wallet_transactions
            WHERE type = 'referral_task_commission' AND reference_id = ?
        ''', (f"REF-TASK-{sub_id}",)).fetchone()
        if referral_credit:
            referral_paise = referral_credit['amount_paise'] if referral_credit['amount_paise'] is not None else inr_to_paise(referral_credit['amount'])
            ensure_exact_user_balance(conn, referral_credit['user_id'])
            referrer_balance_paise = apply_wallet_delta(conn.cursor(), referral_credit['user_id'], -referral_paise, require_balance=True)
            if referrer_balance_paise is None:
                conn.rollback()
                return jsonify({'success': False, 'message': 'Referrer wallet does not contain enough balance for a safe reversal.'}), 409
            insert_wallet_transaction(cursor, referral_credit['user_id'], 'referral_reversal', -referral_paise,
                                      referrer_balance_paise, f"REF-REVERSAL-{sub_id}",
                                      f"Referral commission reversed: {reason}")
        conn.commit()
        return jsonify({'success': True, 'message': 'Approved reward reversed and ledgered.'})
    finally:
        conn.close()

@app.route('/api/admin/withdrawals', methods=['GET'])
@admin_required
def admin_withdrawals():
    conn = database.get_db_connection()
    wds = conn.execute('''
        SELECT w.*, u.name as user_name, u.mobile as user_mobile, u.email as user_email
        FROM withdrawals w
        JOIN users u ON w.user_id = u.id
        ORDER BY CASE WHEN w.status IN ('requested', 'under_review') THEN 0 ELSE 1 END, w.id DESC
    ''').fetchall()

    result = []
    for w in wds:
        result.append({
            'id': w['id'],
            'user_id': w['user_id'],
            'user_name': w['user_name'],
            'user_mobile': w['user_mobile'],
            'user_email': w['user_email'],
            'amount': round(w['amount'], 2),
            'payout_method': w['payout_method'],
            'upi_id': w['upi_id'],
            'bank_account': w['bank_account'],
            'bank_ifsc': w['bank_ifsc'],
            'bank_holder_name': w['bank_holder_name'],
            'status': w['status'],
            'utr_number': w['utr_number'],
            'currency': w['currency'] if 'currency' in w.keys() else 'INR',
            'usdt_amount': float(w['usdt_amount']) if 'usdt_amount' in w.keys() and w['usdt_amount'] is not None else None,
            'usdt_inr_rate': float(w['usdt_inr_rate']) if 'usdt_inr_rate' in w.keys() and w['usdt_inr_rate'] is not None else None,
            'usdt_network': w['usdt_network'] if 'usdt_network' in w.keys() else None,
            'usdt_wallet_address': w['usdt_wallet_address'] if 'usdt_wallet_address' in w.keys() else None,
            'provider_reference': w['provider_reference'] if 'provider_reference' in w.keys() else None,
            'transaction_hash': w['transaction_hash'] if 'transaction_hash' in w.keys() else None,
            'requested_at': w['requested_at'],
            'processed_at': w['processed_at']
        })

    conn.close()
    return jsonify({'success': True, 'withdrawals': result})

@app.route('/api/admin/withdrawals/<int:wd_id>/approve', methods=['POST'])
@admin_required
def admin_approve_withdrawal(wd_id):
    conn = database.get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        cursor = conn.execute("UPDATE withdrawals SET status = 'approved' WHERE id = ? AND status = 'under_review'", (wd_id,))
        if cursor.rowcount != 1:
            conn.rollback()
            return jsonify({'success': False, 'message': 'Only under-review withdrawals can be approved.'}), 409
        conn.commit()
        return jsonify({'success': True, 'message': f'Withdrawal #{wd_id} approved. It is not paid until a provider reference is recorded.'})
    finally:
        conn.close()

@app.route('/api/admin/withdrawals/<int:wd_id>/under-review', methods=['POST'])
@admin_required
def review_withdrawal(wd_id):
    conn = database.get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        cursor = conn.execute("UPDATE withdrawals SET status = 'under_review' WHERE id = ? AND status = 'requested'", (wd_id,))
        if cursor.rowcount != 1:
            conn.rollback()
            return jsonify({'success': False, 'message': 'Only requested withdrawals can enter review.'}), 409
        conn.commit()
        return jsonify({'success': True, 'message': 'Withdrawal moved to under review.'})
    finally:
        conn.close()

@app.route('/api/admin/withdrawals/<int:wd_id>/processing', methods=['POST'])
@admin_required
def process_withdrawal(wd_id):
    conn = database.get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        cursor = conn.execute("UPDATE withdrawals SET status = 'processing' WHERE id = ? AND status = 'approved'", (wd_id,))
        if cursor.rowcount != 1:
            conn.rollback()
            return jsonify({'success': False, 'message': 'Only approved withdrawals can enter processing.'}), 409
        conn.commit()
        return jsonify({'success': True, 'message': 'Withdrawal marked processing. Manual payout reference is required before payment.'})
    finally:
        conn.close()

@app.route('/api/admin/withdrawals/<int:wd_id>/paid', methods=['POST'])
@admin_required
def pay_withdrawal(wd_id):
    data = request.get_json() or {}
    provider_reference = (data.get('provider_reference') or '').strip()
    transaction_hash = (data.get('transaction_hash') or '').strip()
    conn = database.get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        withdrawal = conn.execute("SELECT currency FROM withdrawals WHERE id = ?", (wd_id,)).fetchone()
        if not withdrawal:
            conn.rollback()
            return jsonify({'success': False, 'message': 'Withdrawal not found.'}), 404
        if withdrawal['currency'] == 'USDT' and not transaction_hash:
            conn.rollback()
            return jsonify({'success': False, 'message': 'Blockchain transaction hash is required for a USDT payout.'}), 400
        if withdrawal['currency'] != 'USDT' and not provider_reference:
            conn.rollback()
            return jsonify({'success': False, 'message': 'Actual provider/payment reference is required before marking paid.'}), 400
        cursor = conn.execute('''
            UPDATE withdrawals SET status = 'paid', provider_reference = ?, transaction_hash = ?,
                utr_number = CASE WHEN currency = 'USDT' THEN NULL ELSE ? END, processed_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'processing'
        ''', (provider_reference or None, transaction_hash or None, provider_reference or None, wd_id))
        if cursor.rowcount != 1:
            conn.rollback()
            return jsonify({'success': False, 'message': 'Only processing withdrawals can be marked paid.'}), 409
        conn.commit()
        return jsonify({'success': True, 'message': f'Withdrawal #{wd_id} marked paid with recorded payment evidence.'})
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        integrity_message = str(exc)
        if 'withdrawal_provider_reference_unique' in integrity_message or 'withdrawals.provider_reference' in integrity_message:
            return jsonify({'success': False, 'message': 'This provider reference is already recorded for another withdrawal.'}), 409
        if 'withdrawal_transaction_hash_unique' in integrity_message or 'withdrawals.transaction_hash' in integrity_message:
            return jsonify({'success': False, 'message': 'This transaction hash is already recorded for another withdrawal.'}), 409
        raise
    finally:
        conn.close()

@app.route('/api/admin/withdrawals/<int:wd_id>/reject', methods=['POST'])
@admin_required
def admin_reject_withdrawal(wd_id):
    data = request.get_json() or {}
    reason = (data.get('reason') or 'Bank/UPI verification failed.').strip()

    conn = database.get_db_connection()
    wd = conn.execute("SELECT * FROM withdrawals WHERE id = ?", (wd_id,)).fetchone()
    if not wd:
        conn.close()
        return jsonify({'success': False, 'message': 'Withdrawal request not found.'}), 404

    if wd['status'] not in ('requested', 'under_review', 'approved', 'processing'):
        conn.close()
        return jsonify({'success': False, 'message': 'This withdrawal cannot be rejected or refunded in its current state.'}), 400

    conn.execute('BEGIN IMMEDIATE')
    cursor = conn.cursor()
    # 1. Update withdrawal status
    cursor.execute("UPDATE withdrawals SET status = 'rejected', failure_reason = ?, processed_at = CURRENT_TIMESTAMP WHERE id = ? AND status IN ('requested', 'under_review', 'approved', 'processing')", (reason, wd_id))
    if cursor.rowcount != 1:
        conn.rollback()
        conn.close()
        return jsonify({'success': False, 'message': 'Withdrawal was already processed.'}), 409

    # 2. Refund to user wallet using the exact reserved amount.
    refund_paise = wd['amount_paise'] if wd['amount_paise'] is not None else inr_to_paise(wd['amount'])
    ensure_exact_user_balance(conn, wd['user_id'])
    balance_paise = apply_wallet_delta(cursor, wd['user_id'], refund_paise)
    # 3. Add refund transaction record
    insert_wallet_transaction(cursor, wd['user_id'], 'withdrawal_refund', refund_paise, balance_paise, f"REFUND-{wd_id}", f"Refund for rejected withdrawal: {reason}")

    conn.commit()
    conn.close()

    return jsonify({'success': True, 'message': f"Withdrawal #{wd_id} rejected and ₹{paise_to_inr(refund_paise):.2f} refunded to user wallet."})

@app.route('/api/admin/users', methods=['GET'])
@admin_required
def admin_users():
    conn = database.get_db_connection()
    users = conn.execute('''
        SELECT u.*, 
               (SELECT COUNT(*) FROM submissions WHERE user_id = u.id AND status = 'approved') as approved_tasks,
               (SELECT COUNT(*) FROM users WHERE referred_by = u.id) as total_referrals
        FROM users u 
        ORDER BY u.id DESC
    ''').fetchall()

    result = []
    for u in users:
        result.append({
            'id': u['id'],
            'name': u['name'],
            'mobile': u['mobile'],
            'email': u['email'],
            'role': u['role'],
            'wallet_balance': round(u['wallet_balance'], 2),
            'total_earned': round(u['total_earned'], 2),
            'referral_code': u['referral_code'],
            'approved_tasks': u['approved_tasks'],
            'total_referrals': u['total_referrals'],
            'created_at': u['created_at']
        })

    conn.close()
    return jsonify({'success': True, 'users': result})

@app.route('/api/admin/tasks/create', methods=['POST'])
@admin_required
def admin_create_task():
    is_multipart = request.mimetype == 'multipart/form-data'
    data = request.form.to_dict() if is_multipart else (request.get_json(silent=True) or {})
    title = (data.get('title') or '').strip()
    category = data.get('category', 'Direct Message')
    description = (data.get('description') or '').strip()
    template_text = (data.get('template_text') or '').strip()
    numbers_raw = data.get('target_numbers') or []
    try:
        time_limit = int(data.get('time_limit_minutes', 15))
        total_slots = int(data.get('total_slots', 100))
        recipients_per_slot = int(data.get('recipients_per_slot', 25))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'Slot and time values must be whole numbers.'}), 400
    proof_instructions = (data.get('proof_instructions') or 'Upload screenshot showing sent message.').strip()
    task_type = (data.get('task_type') or '').strip().upper()
    if not task_type:
        task_type = 'IMAGE_TEXT_LINK' if category == 'IMAGE_TEXT_LINK' else 'TEXT_LINK'
    if task_type not in {'TEXT_LINK', 'IMAGE_TEXT_LINK'}:
        return jsonify({'success': False, 'message': 'Unsupported task type.'}), 400
    if task_type == 'IMAGE_TEXT_LINK':
        category = 'IMAGE_TEXT_LINK'

    if not title or not description or not template_text:
        return jsonify({'success': False, 'message': 'Title, description, and template are required.'}), 400
    if time_limit < 1 or total_slots < 1 or recipients_per_slot < 1:
        return jsonify({'success': False, 'message': 'Slot and time values must be positive.'}), 400

    if isinstance(numbers_raw, str):
        # Split by comma, space, or newline
        nums = [n.strip().replace(' ', '').replace('+', '') for n in numbers_raw.replace('\n', ',').split(',') if n.strip()]
    else:
        nums = [str(n).strip().replace(' ', '').replace('+', '') for n in numbers_raw if str(n).strip()]

    nums = list(dict.fromkeys(normalize_phone_number(number) for number in nums if normalize_phone_number(number)))
    required_pool_size = total_slots * recipients_per_slot
    if len(nums) < required_pool_size:
        return jsonify({
            'success': False,
            'message': f'Need {required_pool_size} unique target numbers for {total_slots} slots × {recipients_per_slot} recipients. Received {len(nums)}.'
        }), 400

    use_default = str(data.get('use_default_reward', '1')).strip().lower() not in {'0', 'false', 'no', 'off'}
    conn = database.get_db_connection()
    image_file = request.files.get('campaign_image') or request.files.get('image') if is_multipart else None
    image_path = None
    try:
        default_rate_paise = inr_to_paise(get_setting_decimal(conn, 'per_message_rate', '3'))
        if use_default:
            reward_paise = default_rate_paise
            reward_source = 'default_at_creation'
        else:
            try:
                reward_paise = inr_to_paise(Decimal(str(data.get('reward_per_number', '')).strip()))
            except (ArithmeticError, TypeError, ValueError):
                return jsonify({'success': False, 'message': 'Enter a valid reward per verified number.'}), 400
            if reward_paise <= 0:
                return jsonify({'success': False, 'message': 'Reward per verified number must be positive.'}), 400
            reward_source = 'admin_override'

        destination_link = normalize_task_destination(data.get('destination_link')) if task_type == 'IMAGE_TEXT_LINK' else None
        if task_type == 'IMAGE_TEXT_LINK' and not destination_link:
            return jsonify({'success': False, 'message': 'A valid HTTP(S) destination link is required.'}), 400
        if task_type == 'IMAGE_TEXT_LINK':
            if not image_file or not image_file.filename or not allowed_file(image_file.filename) or not valid_image_content(image_file):
                return jsonify({'success': False, 'message': 'Upload a valid PNG, JPG, JPEG, or WEBP campaign image.'}), 400
            extension = secure_filename(image_file.filename).rsplit('.', 1)[-1].lower()
            image_path = f'campaign_{secrets.token_hex(16)}.{extension}'
            image_file.save(os.path.join(app.config['TASK_ASSET_FOLDER'], image_path))

        payout_paise = reward_paise * recipients_per_slot
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO tasks (title, category, description, payout, payout_paise, template_text,
                target_numbers, recipients_per_slot, time_limit_minutes, proof_instructions,
                total_slots, completed_slots, status, task_type, image_path, destination_link,
                reward_per_number_paise, reward_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'active', ?, ?, ?, ?, ?)
        ''', (title, category, description, float(paise_to_inr(payout_paise)), payout_paise,
              template_text, json.dumps(nums), recipients_per_slot, time_limit,
              proof_instructions, total_slots, task_type, image_path, destination_link,
              reward_paise, reward_source))
        task_id = cursor.lastrowid
        cursor.execute("UPDATE tasks SET campaign_id = ? WHERE id = ?", (task_id, task_id))
        conn.commit()
    except Exception:
        conn.rollback()
        if image_path:
            try:
                os.remove(os.path.join(app.config['TASK_ASSET_FOLDER'], image_path))
            except OSError:
                pass
        raise
    finally:
        conn.close()

    return jsonify({'success': True, 'message': f'Task #{task_id} created and published!', 'task_id': task_id})

@app.route('/api/admin/tasks/<int:task_id>/toggle', methods=['POST'])
@admin_required
def admin_toggle_task(task_id):
    conn = database.get_db_connection()
    task = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        conn.close()
        return jsonify({'success': False, 'message': 'Task not found.'}), 404
    if task['status'] == 'archived':
        conn.close()
        return jsonify({'success': False, 'message': 'Archived tasks must be restored before they can be activated.'}), 409

    new_status = 'paused' if task['status'] == 'active' else 'active'
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (new_status, task_id))
    conn.commit()
    conn.close()

    return jsonify({'success': True, 'new_status': new_status, 'message': f'Task status changed to {new_status}.'})

@app.route('/api/admin/tasks/<int:task_id>/pause', methods=['POST'])
@admin_required
def admin_pause_task(task_id):
    conn = database.get_db_connection()
    try:
        task = conn.execute('SELECT status FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if not task:
            return jsonify({'success': False, 'message': 'Task not found.'}), 404
        if task['status'] == 'archived':
            return jsonify({'success': False, 'message': 'Archived tasks must be restored before changing availability.'}), 409
        if task['status'] == 'paused':
            return jsonify({'success': True, 'message': 'Task is already paused.'})
        conn.execute("UPDATE tasks SET status = 'paused' WHERE id = ? AND status = 'active'", (task_id,))
        conn.commit()
        return jsonify({'success': True, 'message': 'Task paused and hidden from workers.'})
    finally:
        conn.close()

@app.route('/api/admin/tasks/<int:task_id>/activate', methods=['POST'])
@admin_required
def admin_activate_task(task_id):
    conn = database.get_db_connection()
    try:
        task = conn.execute('SELECT status FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if not task:
            return jsonify({'success': False, 'message': 'Task not found.'}), 404
        if task['status'] == 'archived':
            return jsonify({'success': False, 'message': 'Archived tasks must be restored instead of activated.'}), 409
        if task['status'] == 'active':
            return jsonify({'success': True, 'message': 'Task is already active.'})
        conn.execute("UPDATE tasks SET status = 'active' WHERE id = ? AND status = 'paused'", (task_id,))
        conn.commit()
        return jsonify({'success': True, 'message': 'Task activated and available to workers.'})
    finally:
        conn.close()

def _admin_task_history(conn, task_id):
    """Return whether a task has any immutable assignment or financial history."""
    checks = {
        'assignments': "SELECT 1 FROM task_assignments WHERE task_id = ? LIMIT 1",
        'submissions': "SELECT 1 FROM submissions WHERE task_id = ? LIMIT 1",
        'target_records': "SELECT 1 FROM target_number_records WHERE task_id = ? LIMIT 1",
        'target_payments': "SELECT 1 FROM target_payments WHERE task_id = ? LIMIT 1",
        'recipient_allocations': "SELECT 1 FROM recipient_allocations WHERE assignment_id IN (SELECT id FROM task_assignments WHERE task_id = ?) LIMIT 1",
    }
    history = {name: bool(conn.execute(sql, (task_id,)).fetchone()) for name, sql in checks.items()}
    # Current task rewards are linked through submissions. Keep this check
    # compatible with older ledgers that predate submission_id.
    history['wallet_transactions'] = bool(conn.execute('''
        SELECT 1 FROM wallet_transactions wt
        LEFT JOIN submissions s ON s.id = wt.submission_id
        WHERE (s.task_id = ? OR wt.reference_id IN (
            SELECT 'SUB-' || id FROM submissions WHERE task_id = ?
            UNION SELECT 'PARTIAL-SUB-' || id FROM submissions WHERE task_id = ?
            UNION SELECT 'AUTO-SUB-' || id FROM submissions WHERE task_id = ?
        )) LIMIT 1
    ''', (task_id, task_id, task_id, task_id)).fetchone())
    history['has_history'] = any(history.values())
    return history

@app.route('/api/admin/tasks', methods=['GET'])
@admin_required
def admin_list_tasks():
    conn = database.get_db_connection()
    try:
        rows = conn.execute('SELECT * FROM tasks ORDER BY id DESC').fetchall()
        result = []
        for task in rows:
            history = _admin_task_history(conn, task['id'])
            assignments = conn.execute('''
                SELECT id, user_id, status, expires_at, slot_released
                FROM task_assignments WHERE task_id = ? ORDER BY id DESC
            ''', (task['id'],)).fetchall()
            result.append({
                'id': task['id'], 'title': task['title'], 'category': task['category'],
                'task_type': task['task_type'] or 'TEXT_LINK',
                'reward_per_number': float(paise_to_inr(task['reward_per_number_paise'] or inr_to_paise(get_setting_decimal(conn, 'per_message_rate', '3')))),
                'reward_source': task['reward_source'] or 'legacy_snapshot',
                'status': task['status'], 'payout': task['payout'],
                'total_slots': task['total_slots'], 'completed_slots': task['completed_slots'],
                'created_at': task['created_at'], 'campaign_id': task['campaign_id'],
                'has_history': history['has_history'], 'history': history,
                'assignments': [dict(assignment) for assignment in assignments]
            })
        return jsonify({'success': True, 'tasks': result})
    finally:
        conn.close()

@app.route('/api/admin/tasks/<int:task_id>/archive', methods=['POST'])
@admin_required
def admin_archive_task(task_id):
    conn = database.get_db_connection()
    try:
        row = conn.execute('SELECT status FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if not row:
            return jsonify({'success': False, 'message': 'Task not found.'}), 404
        if row['status'] == 'archived':
            return jsonify({'success': True, 'message': 'Task is already archived.'})
        conn.execute("UPDATE tasks SET status = 'archived' WHERE id = ?", (task_id,))
        conn.commit()
        return jsonify({'success': True, 'message': 'Task archived. Historical records were preserved.'})
    finally:
        conn.close()

@app.route('/api/admin/tasks/<int:task_id>/restore', methods=['POST'])
@admin_required
def admin_restore_task(task_id):
    conn = database.get_db_connection()
    try:
        row = conn.execute('SELECT status FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if not row:
            return jsonify({'success': False, 'message': 'Task not found.'}), 404
        if row['status'] != 'archived':
            return jsonify({'success': False, 'message': 'Only archived tasks can be restored.'}), 400
        conn.execute("UPDATE tasks SET status = 'active' WHERE id = ?", (task_id,))
        conn.commit()
        return jsonify({'success': True, 'message': 'Task restored and available to workers.'})
    finally:
        conn.close()

@app.route('/api/admin/tasks/<int:task_id>', methods=['DELETE'])
@admin_required
def admin_delete_task(task_id):
    conn = database.get_db_connection()
    try:
        row = conn.execute('SELECT id FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if not row:
            return jsonify({'success': False, 'message': 'Task not found.'}), 404
        history = _admin_task_history(conn, task_id)
        if history['has_history']:
            return jsonify({'success': False, 'message': 'This task has historical activity and cannot be permanently deleted. Archive it instead.'}), 409
        conn.execute('DELETE FROM tasks WHERE id = ?', (task_id,))
        conn.commit()
        return jsonify({'success': True, 'message': 'Unused task permanently deleted.'})
    finally:
        conn.close()

@app.route('/api/admin/task-assignments/<int:assignment_id>/release', methods=['POST'])
@admin_required
def admin_release_task_assignment(assignment_id):
    data = request.get_json() or {}
    reason = (data.get('reason') or '').strip()
    if not reason:
        return jsonify({'success': False, 'message': 'An administrative release reason is required.'}), 400
    admin = get_current_user()
    conn = database.get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        assignment = conn.execute('''
            SELECT id, user_id, task_id, status, slot_released
            FROM task_assignments WHERE id = ?
        ''', (assignment_id,)).fetchone()
        if not assignment:
            return jsonify({'success': False, 'message': 'Assignment not found.'}), 404
        if assignment['status'] in ('paid', 'completed'):
            return jsonify({'success': False, 'message': 'Paid or completed assignments cannot be released.'}), 409
        if assignment['status'] not in ('in_progress', 'pending_review'):
            existing_audit = conn.execute(
                'SELECT id FROM admin_assignment_release_audit WHERE assignment_id = ?',
                (assignment_id,)
            ).fetchone()
            if not existing_audit and assignment['slot_released']:
                conn.execute('''
                    INSERT INTO admin_assignment_release_audit
                        (assignment_id, admin_user_id, worker_user_id, task_id, old_status, new_status, reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (assignment_id, admin['id'], assignment['user_id'], assignment['task_id'], assignment['status'], assignment['status'], reason))
                conn.commit()
            return jsonify({'success': True, 'already_released': True, 'message': 'Assignment is already released.'})
        old_status = assignment['status']
        conn.execute('''
            UPDATE task_assignments
            SET status = 'cancelled', slot_released = 1
            WHERE id = ? AND status IN ('in_progress', 'pending_review') AND slot_released = 0
        ''', (assignment_id,))
        if conn.execute('SELECT changes()').fetchone()[0] != 1:
            return jsonify({'success': True, 'already_released': True, 'message': 'Assignment is already released.'})
        conn.execute("UPDATE recipient_allocations SET status = 'RELEASED' WHERE assignment_id = ? AND status = 'ALLOCATED'", (assignment_id,))
        conn.execute('''
            INSERT INTO admin_assignment_release_audit
                (assignment_id, admin_user_id, worker_user_id, task_id, old_status, new_status, reason)
            VALUES (?, ?, ?, ?, ?, 'cancelled', ?)
        ''', (assignment_id, admin['id'], assignment['user_id'], assignment['task_id'], old_status, reason))
        conn.commit()
        return jsonify({'success': True, 'assignment_id': assignment_id, 'old_status': old_status, 'new_status': 'cancelled', 'message': 'Assignment released administratively; no payment records were changed.'})
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()

# ----------------- REFERRALS & LEADERBOARD -----------------
@app.route('/api/referrals', methods=['GET'])
@login_required
def referrals():
    user = get_current_user()
    conn = database.get_db_connection()
    commission_percent = get_setting_number(conn, 'referral_commission_percent', 10.0)

    invited_users = conn.execute('''
        SELECT id, name, mobile, created_at 
        FROM users 
        WHERE referred_by = ? 
        ORDER BY id DESC
    ''', (user['id'],)).fetchall()

    ref_bonus_total = conn.execute('''
        SELECT SUM(amount) as s 
        FROM wallet_transactions 
        WHERE user_id = ? AND type IN ('referral_task_commission', 'referral_reversal')
    ''', (user['id'],)).fetchone()['s'] or 0.0

    conn.close()

    friends = []
    for f in invited_users:
        masked_mobile = f['mobile'][:3] + '****' + f['mobile'][-3:] if len(f['mobile']) >= 7 else f['mobile']
        friends.append({
            'name': f['name'],
            'mobile': masked_mobile,
            'joined_at': f['created_at']
        })

    return jsonify({
        'success': True,
        'referral_code': user['referral_code'],
        'referral_commission_percent': commission_percent,
        'total_referrals': len(friends),
        'total_referral_earnings': round(ref_bonus_total, 2),
        'friends': friends
    })

@app.route('/api/leaderboard', methods=['GET'])
def leaderboard():
    conn = database.get_db_connection()
    leaders = conn.execute('''
        SELECT u.id, u.name, u.mobile, u.total_earned,
               (SELECT COUNT(*) FROM submissions WHERE user_id = u.id AND status = 'approved') as tasks_done
        FROM users u
        WHERE u.role = 'user'
        ORDER BY u.total_earned DESC, tasks_done DESC
        LIMIT 10
    ''').fetchall()
    conn.close()

    result = []
    for idx, row in enumerate(leaders):
        masked_mobile = row['mobile'][:3] + '****' + row['mobile'][-3:] if len(row['mobile']) >= 7 else row['mobile']
        result.append({
            'rank': idx + 1,
            'name': row['name'],
            'mobile': masked_mobile,
            'total_earned': round(row['total_earned'], 2),
            'tasks_done': row['tasks_done']
        })

    return jsonify({'success': True, 'leaderboard': result})

if __name__ == '__main__':
    print("TaskPe backend server running at http://127.0.0.1:5000")
    app.run(host='0.0.0.0', port=5000, debug=app.config['DEBUG'])
