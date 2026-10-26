import os
import base64
import csv
import json
import io
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
from flask import Flask, request, jsonify, render_template, session, send_from_directory, Response, send_file
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
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
WHATSAPP_LINKED_DEVICE_ENABLED = os.environ.get(
    'TASKPE_WHATSAPP_LINKED_DEVICE_ENABLED', '0'
).strip().lower() in {'1', 'true', 'yes'}
WHATSAPP_TEST_TRANSPORT_ENABLED = (
    os.environ.get('TASKPE_TEST_FAKE_WHATSAPP_TRANSPORT', '0').strip().lower()
    in {'1', 'true', 'yes'} and os.environ.get('ENVIRONMENT', '').strip().lower() == 'development'
)
WHATSAPP_SERVICE_URL = os.environ.get(
    'TASKPE_WA_SERVICE_URL', 'http://127.0.0.1:8765'
).strip().rstrip('/')
WHATSAPP_SERVICE_SHARED_SECRET = os.environ.get(
    'TASKPE_WA_SERVICE_SHARED_SECRET', ''
).strip()
try:
    WHATSAPP_SERVICE_TIMEOUT_SECONDS = float(
        os.environ.get('TASKPE_WA_SERVICE_TIMEOUT_SECONDS', '8')
    )
except ValueError:
    raise RuntimeError('TASKPE_WA_SERVICE_TIMEOUT_SECONDS must be a number.')
if WHATSAPP_SERVICE_TIMEOUT_SECONDS <= 0 or WHATSAPP_SERVICE_TIMEOUT_SECONDS > 30:
    raise RuntimeError('TASKPE_WA_SERVICE_TIMEOUT_SECONDS must be between 0 and 30.')
if WHATSAPP_LINKED_DEVICE_ENABLED and len(WHATSAPP_SERVICE_SHARED_SECRET) < 32:
    raise RuntimeError(
        'TASKPE_WA_SERVICE_SHARED_SECRET must be at least 32 characters when linked-device integration is enabled.'
    )

class _RejectWhatsAppRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

_WHATSAPP_SERVICE_OPENER = urllib.request.build_opener(_RejectWhatsAppRedirects())


def whatsapp_feature_enabled():
    """Return whether a real or explicitly injected local test path is enabled."""
    return WHATSAPP_LINKED_DEVICE_ENABLED or WHATSAPP_TEST_TRANSPORT_ENABLED

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
    '/api/campaign-inquiries': (5, 600),
    '/api/wallet/withdraw': (5, 600),
    '/api/whatsapp/connection/qr': (5, 600),
    '/api/whatsapp/connection/disconnect': (10, 600),
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

def normalize_phone_number(value, region=None):
    """Normalize a recipient to E.164; never guess a national region."""
    try:
        return database.normalize_recipient_number(value, region=region)
    except ValueError:
        return ''


def recipient_number_to_jid(value, region=None):
    """Convert a validated E.164 recipient into the Node JID form."""
    normalized = database.normalize_recipient_number(value, region=region)
    return f'{normalized[1:]}@s.whatsapp.net'

def normalize_auth_mobile(value):
    """Normalize Indian signup/OTP input to its ten-digit local form."""
    digits = database.normalize_legacy_target_number(value)
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

def task_recipient_pool(task, allow_legacy=False):
    try:
        numbers = json.loads(task['target_numbers'])
    except Exception:
        numbers = [task['target_numbers']]
    # Preserve order while eliminating formatting duplicates in the source pool.
    unique_numbers = []
    task_region = task['recipient_region'] if 'recipient_region' in task.keys() else None
    seen = set()
    for number in numbers:
        clean = normalize_phone_number(number, region=task_region)
        if not clean and allow_legacy:
            # Compatibility only for pre-existing manual assignment data. New
            # task creation and automated campaign planning remain strict.
            clean = database.normalize_legacy_target_number(number)
        if clean and clean not in seen:
            seen.add(clean)
            unique_numbers.append(clean)
    return unique_numbers


def _campaign_recipient_fingerprint(numbers):
    canonical_numbers = sorted(set(numbers))
    encoded = json.dumps(canonical_numbers, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def business_campaign_is_approved(conn, task):
    """Return whether a business-owned campaign has an intact approval snapshot.

    Admin-created tasks have no owner and retain their established lifecycle.
    Business campaigns must have both an admin approval and a declaration that
    still matches the task's current normalized recipient list.
    """
    if not task or 'owner_user_id' not in task.keys() or task['owner_user_id'] is None:
        return True
    if 'campaign_review_status' in task.keys() and task['campaign_review_status'] != 'approved':
        return False
    try:
        recipients = task_recipient_pool(task)
        declaration = database.get_whatsapp_campaign_consent_declaration(
            conn, task['id'], task['campaign_id'] or task['id'])
        fingerprint = _campaign_recipient_fingerprint(recipients)
        return bool(
            declaration and declaration['business_user_id'] == task['owner_user_id']
            and declaration['campaign_id'] == (task['campaign_id'] or task['id'])
            and declaration['declaration_text'] == database.SHOP_CONSENT_DECLARATION_DEFAULT_TEXT
            and declaration['recipient_fingerprint'] == fingerprint
            and declaration['recipient_count'] == len(recipients)
            and task['campaign_approval_fingerprint'] == fingerprint
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, sqlite3.Error):
        return False

def build_recipient_eligibility(conn, task_or_id, exclude_plan_id=None):
    """Return a read-only, deterministic eligibility snapshot for a task.

    This deliberately mirrors ``take_task``'s current allocation semantics:
    same-campaign ALLOCATED and COMPLETED rows are excluded, as are active
    claims belonging to another plan. Recorded WhatsApp opt-outs are also
    excluded before a caller can create claims or reservations. Callers
    receive a snapshot and must not treat it as a reservation.
    """
    if isinstance(task_or_id, bool):
        raise ValueError('A valid task is required.')
    if isinstance(task_or_id, int):
        task = conn.execute('SELECT * FROM tasks WHERE id = ?', (task_or_id,)).fetchone()
    else:
        task = task_or_id
    if not task:
        raise ValueError('Task not found.')

    task_id = task['id']
    campaign_id = task['campaign_id'] or task_id
    source_numbers = task_recipient_pool(task)
    allocation_rows = conn.execute('''
        SELECT phone_number, status
        FROM recipient_allocations
        WHERE campaign_id = ? AND status IN ('ALLOCATED', 'COMPLETED')
    ''', (campaign_id,)).fetchall()
    allocation_states = {}
    for row in allocation_rows:
        stored_number = str(row['phone_number'] or '')
        allocation_states.setdefault(stored_number, set()).add(row['status'])
        if stored_number.startswith('+'):
            allocation_states.setdefault(stored_number[1:], set()).update(
                allocation_states[stored_number]
            )
        elif stored_number.isdigit():
            allocation_states.setdefault('+' + stored_number, set()).update(
                allocation_states[stored_number]
            )

    claim_query = '''
        SELECT recipient_number
        FROM whatsapp_campaign_recipient_claims
        WHERE campaign_id = ? AND state = 'claimed'
    '''
    claim_params = [campaign_id]
    if exclude_plan_id is not None:
        claim_query += ' AND plan_id != ?'
        claim_params.append(exclude_plan_id)
    claimed_numbers = set()
    for row in conn.execute(claim_query, claim_params).fetchall():
        stored_number = str(row['recipient_number'] or '')
        claimed_numbers.add(stored_number)
        claimed_numbers.add(stored_number[1:] if stored_number.startswith('+') else '+' + stored_number)

    opted_out_numbers = _authoritative_whatsapp_opt_out_keys(conn)

    eligible = []
    exclusion_counts = {
        'allocated_same_campaign': 0,
        'completed_same_campaign': 0,
        'claimed_same_campaign': 0,
    }
    for number in source_numbers:
        if number in opted_out_numbers:
            exclusion_counts['opted_out'] = exclusion_counts.get('opted_out', 0) + 1
            continue
        states = allocation_states.get(number, set())
        # Match take_task's exclusion semantics.  If inconsistent historical
        # data has both states, report the active allocation as the reason.
        if 'ALLOCATED' in states:
            exclusion_counts['allocated_same_campaign'] += 1
        elif 'COMPLETED' in states:
            exclusion_counts['completed_same_campaign'] += 1
        elif number in claimed_numbers:
            exclusion_counts['claimed_same_campaign'] += 1
        else:
            eligible.append(number)

    return {
        'task_id': task_id,
        'campaign_id': campaign_id,
        'source_recipient_count': len(source_numbers),
        'eligible_recipient_count': len(eligible),
        'eligible_recipients': eligible,
        'exclusion_counts': exclusion_counts,
    }


def _opt_out_lookup_keys(value):
    """Return canonical and supported legacy forms for an opt-out value."""
    raw = str(value or '').strip()
    if not raw:
        return set()
    keys = {raw}
    if raw.startswith('+'):
        keys.add(raw[1:])
    elif raw.isdigit():
        keys.add('+' + raw)
    try:
        canonical = database.normalize_recipient_number(raw)
    except (TypeError, ValueError):
        canonical = ''
    if canonical:
        keys.update({canonical, canonical[1:]})
    return keys


def _authoritative_whatsapp_opt_out_keys(conn):
    """Read opt-outs fail closed and retain supported legacy storage forms."""
    table_check = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='whatsapp_opt_outs'"
    ).fetchone()
    if not table_check:
        raise ValueError('Authoritative WhatsApp opt-out data is unavailable.')
    keys = set()
    for row in conn.execute('SELECT phone_number FROM whatsapp_opt_outs').fetchall():
        keys.update(_opt_out_lookup_keys(row['phone_number']))
    return keys

MAX_CAMPAIGN_PLAN_RECIPIENTS = 80
CAMPAIGN_BATCH_ID_PREFIX = 'taskpe-wa-batch-v1:'
CAMPAIGN_BATCH_STATE_VALUES = frozenset({'pending', 'reserving', 'reserved', 'unknown', 'failed'})
CAMPAIGN_CANCELLATION_STATE_VALUES = frozenset({'not_requested', 'checking', 'blocked', 'cancelled'})

def _campaign_batch_id(plan_id):
    if isinstance(plan_id, bool) or not isinstance(plan_id, int) or plan_id < 1:
        raise ValueError('Invalid campaign plan identity.')
    return f'{CAMPAIGN_BATCH_ID_PREFIX}{plan_id}'

def _campaign_batch_fingerprint(batch_id, sender_worker_id, campaign_id,
                                task_id, plan_id, recipient_rows,
                                content_reference, content_type='text'):
    """Match Node's canonical batch fingerprint byte-for-byte."""
    items = []
    for row in recipient_rows:
        key = campaign_plan_recipient_idempotency_key(plan_id, row['id'])
        items.append({
            'position': row['position'],
            'planRecipientId': row['id'],
            'idempotencyKey': key,
            'messageId': key,
            'recipientJid': recipient_number_to_jid(row['recipient_number']),
            'contentType': content_type,
            'payloadReference': content_reference,
        })
    canonical = {
        'batchId': batch_id,
        'senderWorkerId': sender_worker_id,
        'campaignId': campaign_id,
        'taskId': task_id,
        'planId': plan_id,
        'items': items,
    }
    return hashlib.sha256(json.dumps(
        canonical, separators=(',', ':'), ensure_ascii=False
    ).encode('utf-8')).hexdigest()

def select_eligible_recipients(eligibility, quantity=None):
    """Select the first N eligible recipients, or all when quantity is omitted."""
    if not isinstance(eligibility, dict) or not isinstance(
        eligibility.get('eligible_recipients'), list
    ):
        raise ValueError('A valid eligibility snapshot is required.')
    eligible = eligibility['eligible_recipients']
    if quantity is None or quantity == 'all':
        if len(eligible) > MAX_CAMPAIGN_PLAN_RECIPIENTS:
            raise ValueError(f'Campaign plans are limited to {MAX_CAMPAIGN_PLAN_RECIPIENTS} recipients.')
        return list(eligible)
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise ValueError('Recipient quantity must be a positive integer or all.')
    if quantity > MAX_CAMPAIGN_PLAN_RECIPIENTS:
        raise ValueError(f'Campaign plans are limited to {MAX_CAMPAIGN_PLAN_RECIPIENTS} recipients.')
    if quantity > len(eligible):
        raise ValueError('Recipient quantity exceeds eligible recipients.')
    return list(eligible[:quantity])

def create_campaign_send_plan(conn, task_or_id, sender_worker_id,
                              plan_key, quantity=None, content_reference=None,
                              content_type='text'):
    """Atomically persist a deterministic audience snapshot before sending.

    This is intentionally a local Flask service. It does not reserve Node
    quota, call a socket, or apply consent/opt-out policy. A duplicate plan
    key is idempotent only when the
    validated request is identical.
    """
    if isinstance(sender_worker_id, bool) or not isinstance(sender_worker_id, int) or sender_worker_id < 1:
        raise ValueError('A valid sender worker is required.')
    if not isinstance(plan_key, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,199}', plan_key):
        raise ValueError('A valid plan key is required.')
    if not isinstance(content_reference, str) or not content_reference.strip() or len(content_reference) > 500:
        raise ValueError('A safe content reference is required.')
    if content_type not in {'text', 'image', 'video'}:
        raise ValueError('Invalid message content type.')
    if re.search(r'[\x00-\x1f\x7f]', content_reference):
        raise ValueError('A safe content reference is required.')

    conn.execute('BEGIN IMMEDIATE')
    try:
        existing = conn.execute('''
            SELECT id, request_fingerprint, recipient_fingerprint, task_id,
                   recipient_count, requested_quantity, state, batch_id,
                   batch_request_fingerprint, batch_content_type,
                   batch_state, created_at
            FROM whatsapp_campaign_plans WHERE plan_key = ?
        ''', (plan_key,)).fetchone()

        task = conn.execute('SELECT * FROM tasks WHERE id = ?', (task_or_id,)).fetchone() if isinstance(task_or_id, int) else task_or_id
        if not task or not business_campaign_is_approved(conn, task):
            raise ValueError('Campaign approval or recipient declaration is invalid.')
        eligibility = build_recipient_eligibility(
            conn, task, exclude_plan_id=existing['id'] if existing else None
        )
        source_fingerprint = _campaign_recipient_fingerprint(task_recipient_pool(task))
        selected = select_eligible_recipients(eligibility, quantity)
        if not selected:
            raise ValueError('At least one eligible recipient is required.')
        request_fingerprint = hashlib.sha256(json.dumps({
            'task_id': eligibility['task_id'],
            'campaign_id': eligibility['campaign_id'],
            'sender_worker_id': sender_worker_id,
            'content_reference': content_reference,
            'recipients': selected,
        }, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()

        if existing:
            if (existing['request_fingerprint'] != request_fingerprint or
                    existing['recipient_fingerprint'] != source_fingerprint):
                raise ValueError('Plan key is already used for a different request.')
            conn.commit()
            return {
                'created': False,
                'idempotent': True,
                'plan_id': existing['id'],
                'state': existing['state'],
                'recipient_count': existing['recipient_count'],
                'requested_quantity': existing['requested_quantity'],
                'batch_id': existing['batch_id'],
                'batch_request_fingerprint': existing['batch_request_fingerprint'],
                'batch_content_type': existing['batch_content_type'],
                'batch_state': existing['batch_state'],
                'created_at': existing['created_at'],
            }

        cursor = conn.execute('''
            INSERT INTO whatsapp_campaign_plans
                (plan_key, request_fingerprint, recipient_fingerprint, task_id, campaign_id,
                 sender_worker_id, content_reference, recipient_count,
                 requested_quantity, state)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'creating')
        ''', (
            plan_key, request_fingerprint, source_fingerprint, eligibility['task_id'],
            eligibility['campaign_id'], sender_worker_id, content_reference,
            len(selected), 'all' if quantity == 'all' else str(quantity),
        ))
        plan_id = cursor.lastrowid
        conn.executemany('''
            INSERT INTO whatsapp_campaign_plan_recipients
                (plan_id, recipient_number, position, idempotency_key)
            VALUES (?, ?, ?, ?)
        ''', [
            (plan_id, recipient, position, f'pending:{plan_id}:{position}')
            for position, recipient in enumerate(selected, start=1)
        ])
        for row in conn.execute('''
            SELECT id, plan_id FROM whatsapp_campaign_plan_recipients
            WHERE plan_id = ? ORDER BY position
        ''', (plan_id,)).fetchall():
            conn.execute('''
                UPDATE whatsapp_campaign_plan_recipients
                SET idempotency_key = ? WHERE id = ?
            ''', (
                database.whatsapp_campaign_plan_recipient_idempotency_key(row['plan_id'], row['id']),
                row['id'],
            ))
        recipient_rows = conn.execute('''
            SELECT id, recipient_number, position FROM whatsapp_campaign_plan_recipients
            WHERE plan_id = ? ORDER BY position
        ''', (plan_id,)).fetchall()
        batch_id = _campaign_batch_id(plan_id)
        batch_fingerprint = _campaign_batch_fingerprint(
            batch_id, sender_worker_id, eligibility['campaign_id'],
            eligibility['task_id'], plan_id, recipient_rows, content_reference,
            content_type,
        )
        conn.execute('''
            UPDATE whatsapp_campaign_plans
            SET batch_id = ?, batch_request_fingerprint = ?,
                batch_content_type = ?, batch_state = 'pending',
                batch_updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND batch_id IS NULL
        ''', (batch_id, batch_fingerprint, content_type, plan_id))
        conn.executemany('''
            INSERT INTO whatsapp_campaign_recipient_claims
                (campaign_id, task_id, recipient_number, plan_id, plan_recipient_id, state)
            VALUES (?, ?, ?, ?, ?, 'claimed')
        ''', [
            (eligibility['campaign_id'], eligibility['task_id'], row['recipient_number'], plan_id, row['id'])
            for row in recipient_rows
        ])
        conn.execute('''
            UPDATE whatsapp_campaign_plans SET state = 'ready' WHERE id = ? AND state = 'creating'
        ''', (plan_id,))
        if conn.execute('SELECT changes() AS count').fetchone()['count'] != 1:
            raise RuntimeError('Unable to finalize campaign send plan.')
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    row = conn.execute('''
        SELECT id, state, recipient_count, requested_quantity, batch_id,
               batch_request_fingerprint, batch_content_type,
               batch_state, created_at
        FROM whatsapp_campaign_plans WHERE id = ?
    ''', (plan_id,)).fetchone()
    return {
        'created': True,
        'idempotent': False,
        'plan_id': row['id'],
        'state': row['state'],
        'recipient_count': row['recipient_count'],
        'requested_quantity': row['requested_quantity'],
        'batch_id': row['batch_id'],
        'batch_request_fingerprint': row['batch_request_fingerprint'],
        'batch_content_type': row['batch_content_type'],
        'batch_state': row['batch_state'],
        'created_at': row['created_at'],
    }

def release_campaign_send_plan_claims(conn, plan_id):
    """Release claims only for an explicitly cancelled owning plan.

    This is intentionally not automatic and does not touch Node quota or
    outgoing-message rows. Ownership is part of the conditional update so an
    old plan cannot release a newer plan's claim.
    """
    if isinstance(plan_id, bool) or not isinstance(plan_id, int) or plan_id < 1:
        raise ValueError('A valid campaign plan is required.')
    conn.execute('BEGIN IMMEDIATE')
    try:
        plan = conn.execute('''
            SELECT state, cancellation_state, cancellation_evidence_status
            FROM whatsapp_campaign_plans WHERE id = ?
        ''', (plan_id,)).fetchone()
        if not plan:
            raise ValueError('Campaign plan not found.')
        if (plan['state'] != 'cancelled' or
                plan['cancellation_state'] != 'cancelled' or
                plan['cancellation_evidence_status'] != 'absent'):
            raise ValueError('Campaign plan must complete validated cancellation before claims are released.')
        result = conn.execute('''
            UPDATE whatsapp_campaign_recipient_claims
            SET state = 'released', updated_at = CURRENT_TIMESTAMP
            WHERE plan_id = ? AND state = 'claimed'
        ''', (plan_id,))
        conn.commit()
        return {'released_count': result.rowcount}
    except Exception:
        conn.rollback()
        raise

def _mark_campaign_cancellation_blocked(conn, plan_id, batch_id, evidence_status, now):
    """Record a non-release cancellation outcome without touching claims."""
    conn.execute('BEGIN IMMEDIATE')
    try:
        changed = conn.execute('''
            UPDATE whatsapp_campaign_plans
            SET cancellation_state = 'blocked',
                cancellation_evidence_status = ?,
                cancellation_checked_at = ?
            WHERE id = ? AND state = 'ready'
              AND batch_id = ? AND cancellation_state = 'checking'
        ''', (evidence_status, now, plan_id, batch_id))
        conn.commit()
        return changed.rowcount == 1
    except Exception:
        conn.rollback()
        raise

def _cancellation_absent_response_is_valid(response, batch_id):
    return (
        isinstance(response, dict) and
        response.get('success') is True and
        response.get('status') == 'absent' and
        response.get('batch_id') == batch_id
    )

def _cancellation_present_response_is_valid(response, batch_id, request_fingerprint):
    return (
        isinstance(response, dict) and
        response.get('success') is True and
        response.get('status') == 'present' and
        response.get('batch_id') == batch_id and
        response.get('request_fingerprint') == request_fingerprint
    )

def cancel_campaign_send_plan(conn, plan_id, node_call=None, now=None, recover=False):
    """Cancel a plan only after authenticated, validated Node absence evidence.

    ``recover=True`` is an explicit retry of a prior checking/blocked attempt;
    it always reuses the persisted batch identity.
    """
    if isinstance(plan_id, bool) or not isinstance(plan_id, int) or plan_id < 1:
        raise ValueError('A valid campaign plan is required.')
    worker_id = session.get('user_id')
    if isinstance(worker_id, bool) or not isinstance(worker_id, int) or worker_id < 1:
        raise PermissionError('Authenticated worker is required.')
    now = _campaign_reservation_now(now)

    conn.execute('BEGIN IMMEDIATE')
    try:
        plan = _campaign_batch_plan_row(conn, plan_id)
        if not plan:
            raise ValueError('Campaign plan not found.')
        if plan['sender_worker_id'] != worker_id:
            raise PermissionError('Campaign plan ownership mismatch.')
        if plan['cancellation_state'] == 'cancelled' and plan['state'] == 'cancelled':
            conn.commit()
            return {'status': 'cancelled', 'batch_id': plan['batch_id'], 'idempotent': True, 'released_count': 0}
        if plan['cancellation_state'] == 'checking' and not recover:
            conn.commit()
            return {'status': 'cancellation_pending', 'batch_id': plan['batch_id'], 'recovery_required': True}
        if plan['state'] != 'ready':
            raise ValueError('Campaign plan is not cancellable.')
        if not plan['batch_id'] or not plan['batch_request_fingerprint']:
            raise RuntimeError('Campaign batch identity is unavailable.')
        changed = conn.execute('''
            UPDATE whatsapp_campaign_plans
            SET cancellation_state = 'checking',
                cancellation_evidence_status = NULL,
                cancellation_requested_at = ?,
                cancellation_checked_at = NULL
            WHERE id = ? AND sender_worker_id = ? AND state = 'ready'
              AND batch_id = ? AND cancellation_state IN ('not_requested', 'blocked', 'checking')
        ''', (now, plan_id, worker_id, plan['batch_id']))
        if changed.rowcount != 1:
            conn.rollback()
            return {'status': 'cancellation_pending', 'batch_id': plan['batch_id'], 'recovery_required': True}
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    try:
        response, status = _campaign_batch_adapter_call(
            node_call, 'cancellation_evidence', worker_id,
            f"/v1/users/{worker_id}/campaign-batches/{plan['batch_id']}/cancellation-status"
        )
    except Exception:
        _mark_campaign_cancellation_blocked(conn, plan_id, plan['batch_id'], 'unavailable', now)
        return {'status': 'cancellation_blocked', 'batch_id': plan['batch_id'], 'recovery_required': True, 'evidence_status': 'unavailable'}

    if status != 200 or not isinstance(response, dict) or response.get('success') is not True:
        _mark_campaign_cancellation_blocked(conn, plan_id, plan['batch_id'], 'unavailable', now)
        return {'status': 'cancellation_blocked', 'batch_id': plan['batch_id'], 'recovery_required': True, 'evidence_status': 'unavailable'}
    evidence_status = response.get('status')
    if evidence_status != 'absent':
        if evidence_status == 'present':
            if not _cancellation_present_response_is_valid(
                    response, plan['batch_id'], plan['batch_request_fingerprint']):
                evidence_status = 'invalid'
        elif evidence_status not in {'invalid'} or response.get('batch_id') != plan['batch_id']:
            evidence_status = 'invalid'
        _mark_campaign_cancellation_blocked(conn, plan_id, plan['batch_id'], evidence_status, now)
        return {'status': 'cancellation_blocked', 'batch_id': plan['batch_id'], 'recovery_required': True, 'evidence_status': evidence_status}
    if not _cancellation_absent_response_is_valid(response, plan['batch_id']):
        _mark_campaign_cancellation_blocked(conn, plan_id, plan['batch_id'], 'invalid', now)
        return {'status': 'cancellation_blocked', 'batch_id': plan['batch_id'], 'recovery_required': True, 'evidence_status': 'invalid'}

    conn.execute('BEGIN IMMEDIATE')
    try:
        current = _campaign_batch_plan_row(conn, plan_id)
        rows = _campaign_batch_recipient_rows(conn, plan_id)
        claims = conn.execute('''
            SELECT id, plan_recipient_id, recipient_number, state
            FROM whatsapp_campaign_recipient_claims
            WHERE plan_id = ? ORDER BY plan_recipient_id
        ''', (plan_id,)).fetchall()
        safe_batch_state = current and current['batch_state'] in {'pending', 'failed'}
        safe_recipient_states = all(
            row['ledger_message_id'] is None and
            ((current['batch_state'] == 'pending' and row['state'] == 'planned') or
             (current['batch_state'] == 'failed' and row['state'] == 'failed'))
            for row in rows
        ) if current else False
        safe_claims = (
            current and current['sender_worker_id'] == worker_id and
            current['state'] == 'ready' and current['batch_id'] == plan['batch_id'] and
            current['batch_request_fingerprint'] == plan['batch_request_fingerprint'] and
            current['cancellation_state'] == 'checking' and
            len(rows) == current['recipient_count'] == len(claims) and
            all(claim['state'] == 'claimed' and claim['plan_recipient_id'] == row['id'] and
                claim['recipient_number'] == row['recipient_number']
                for claim, row in zip(claims, rows))
        )
        if not (safe_batch_state and safe_recipient_states and safe_claims):
            conn.rollback()
            _mark_campaign_cancellation_blocked(conn, plan_id, plan['batch_id'], 'invalid', now)
            return {'status': 'cancellation_blocked', 'batch_id': plan['batch_id'], 'recovery_required': True, 'evidence_status': 'invalid'}
        released = conn.execute('''
            UPDATE whatsapp_campaign_recipient_claims
            SET state = 'released', updated_at = CURRENT_TIMESTAMP
            WHERE plan_id = ? AND state = 'claimed'
        ''', (plan_id,))
        if released.rowcount != len(claims):
            raise RuntimeError('Campaign claim ownership changed during cancellation.')
        changed = conn.execute('''
            UPDATE whatsapp_campaign_plans
            SET state = 'cancelled', cancellation_state = 'cancelled',
                cancellation_evidence_status = 'absent', cancellation_checked_at = ?
            WHERE id = ? AND sender_worker_id = ? AND state = 'ready'
              AND batch_id = ? AND batch_request_fingerprint = ?
              AND cancellation_state = 'checking'
        ''', (now, plan_id, worker_id, plan['batch_id'], plan['batch_request_fingerprint']))
        if changed.rowcount != 1:
            raise RuntimeError('Campaign cancellation state changed during release.')
        conn.commit()
        return {'status': 'cancelled', 'batch_id': plan['batch_id'], 'released_count': released.rowcount, 'evidence_status': 'absent'}
    except Exception:
        conn.rollback()
        raise

CAMPAIGN_RESERVATION_STATES = frozenset({
    'planned', 'reserving', 'reserved', 'attempted',
    'acknowledged', 'failed', 'delivered_unknown',
})
CAMPAIGN_RESERVATION_LEASE_SECONDS = 300

def campaign_plan_recipient_idempotency_key(plan_id, plan_recipient_id):
    """Return the fixed-length server-derived Node ledger idempotency key."""
    return database.whatsapp_campaign_plan_recipient_idempotency_key(plan_id, plan_recipient_id)

def _campaign_reservation_now(now=None):
    if now is None:
        return datetime.now().replace(microsecond=0).strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(now, datetime):
        return now.replace(microsecond=0).strftime('%Y-%m-%d %H:%M:%S')
    if not isinstance(now, str):
        raise ValueError('Invalid reservation timestamp.')
    try:
        return datetime.fromisoformat(now.replace('Z', '+00:00')).replace(tzinfo=None, microsecond=0).strftime('%Y-%m-%d %H:%M:%S')
    except ValueError as exc:
        raise ValueError('Invalid reservation timestamp.') from exc

def _campaign_reservation_row(conn, plan_recipient_id):
    return conn.execute('''
        SELECT pr.id AS plan_recipient_id, pr.plan_id, pr.recipient_number,
               pr.state, pr.reservation_claim_id, pr.reservation_lease_until,
               pr.ledger_message_id, pr.failure_code,
               p.task_id, p.campaign_id, p.sender_worker_id, p.content_reference,
               p.state AS plan_state, p.cancellation_state
        FROM whatsapp_campaign_plan_recipients pr
        JOIN whatsapp_campaign_plans p ON p.id = pr.plan_id
        WHERE pr.id = ?
    ''', (plan_recipient_id,)).fetchone()

def _campaign_reservation_result(row):
    if not row:
        return {'status': 'not_found'}
    return {
        'status': row['state'],
        'plan_id': row['plan_id'],
        'plan_recipient_id': row['plan_recipient_id'],
        'ledger_message_id': row['ledger_message_id'],
        'failure_code': row['failure_code'],
    }

def _claim_campaign_plan_recipient(conn, plan_recipient_id, claim_id, lease_until, now):
    conn.execute('BEGIN IMMEDIATE')
    try:
        cursor = conn.execute('''
            UPDATE whatsapp_campaign_plan_recipients
            SET state = 'reserving', reservation_claim_id = ?,
                reservation_lease_until = ?, updated_at = ?
            WHERE id = ? AND (
                state = 'planned' OR
                (state = 'reserving' AND reservation_lease_until IS NOT NULL
                 AND reservation_lease_until <= ?)
            ) AND EXISTS (
                SELECT 1 FROM whatsapp_campaign_plans p
                WHERE p.id = whatsapp_campaign_plan_recipients.plan_id
                  AND p.state = 'ready'
                  AND p.cancellation_state = 'not_requested'
            )
        ''', (claim_id, lease_until, now, plan_recipient_id, now))
        conn.commit()
        return cursor.rowcount == 1
    except Exception:
        conn.rollback()
        raise

def _project_campaign_reservation(conn, plan_recipient_id, claim_id,
                                   state, ledger_message_id=None,
                                   failure_code=None, now=None):
    if state not in CAMPAIGN_RESERVATION_STATES - {'reserving'}:
        raise ValueError('Invalid reservation projection state.')
    now = _campaign_reservation_now(now)
    conn.execute('BEGIN IMMEDIATE')
    try:
        cursor = conn.execute('''
            UPDATE whatsapp_campaign_plan_recipients
            SET state = ?, ledger_message_id = COALESCE(?, ledger_message_id),
                failure_code = ?, reservation_claim_id = NULL,
                reservation_lease_until = NULL, updated_at = ?
            WHERE id = ? AND state = 'reserving'
              AND reservation_claim_id = ?
              AND (? IS NULL OR ledger_message_id IS NULL OR ledger_message_id = ?)
        ''', (
            state, ledger_message_id, failure_code, now,
            plan_recipient_id, claim_id, ledger_message_id, ledger_message_id,
        ))
        conn.commit()
        return cursor.rowcount == 1
    except Exception:
        conn.rollback()
        raise

def reserve_campaign_plan_recipient(conn, plan_recipient_id, ledger_reserve,
                                    now=None, lease_seconds=CAMPAIGN_RESERVATION_LEASE_SECONDS,
                                    content_type='text'):
    """Reserve one plan recipient without invoking a WhatsApp socket.

    ``ledger_reserve`` is an injected, reservation-only adapter. It must
    implement the Node ledger contract and must not send. Unknown adapter
    failures leave the row in ``reserving`` for explicit recovery.
    """
    if not callable(ledger_reserve):
        raise ValueError('A ledger reservation adapter is required.')
    if content_type not in {'text', 'image', 'video'}:
        raise ValueError('Invalid message content type.')
    if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds < 1:
        raise ValueError('Invalid reservation lease.')
    now = _campaign_reservation_now(now)
    lease_until = (datetime.strptime(now, '%Y-%m-%d %H:%M:%S') + timedelta(seconds=lease_seconds)).strftime('%Y-%m-%d %H:%M:%S')
    row = _campaign_reservation_row(conn, plan_recipient_id)
    if not row:
        return {'status': 'not_found'}
    if row['cancellation_state'] in {'checking', 'blocked', 'cancelled'} or row['plan_state'] == 'cancelled':
        return {'status': 'cancellation_pending', 'recovery_required': True}
    if row['state'] != 'planned' and not (
        row['state'] == 'reserving' and row['reservation_lease_until'] and row['reservation_lease_until'] <= now
    ):
        return _campaign_reservation_result(row)

    claim_id = secrets.token_urlsafe(24)
    if not _claim_campaign_plan_recipient(conn, plan_recipient_id, claim_id, lease_until, now):
        return _campaign_reservation_result(_campaign_reservation_row(conn, plan_recipient_id))

    row = _campaign_reservation_row(conn, plan_recipient_id)
    idempotency_key = campaign_plan_recipient_idempotency_key(row['plan_id'], plan_recipient_id)
    try:
        response = ledger_reserve({
            'messageId': idempotency_key,
            'idempotencyKey': idempotency_key,
            'senderWorkerId': row['sender_worker_id'],
            'recipientJid': recipient_number_to_jid(row['recipient_number']),
            'campaignId': row['campaign_id'],
            'taskId': row['task_id'],
            'contentType': content_type,
            'payloadReference': row['content_reference'],
        })
    except Exception as exc:
        if getattr(exc, 'code', None) == 'QUOTA_EXCEEDED':
            projected = _project_campaign_reservation(conn, plan_recipient_id, claim_id, 'failed', failure_code='QUOTA_EXCEEDED', now=now)
            return {'status': 'failed', 'failure_code': 'QUOTA_EXCEEDED', 'projected': projected}
        return {'status': 'reserving', 'recovery_required': True}

    record = response.get('record') if isinstance(response, dict) else None
    ledger_state = record.get('state') if isinstance(record, dict) else None
    ledger_message_id = record.get('message_id') if isinstance(record, dict) else None
    state_map = {
        'queued': 'reserved', 'attempted': 'attempted',
        'acknowledged': 'acknowledged', 'failed': 'failed',
        'delivered_unknown': 'delivered_unknown',
    }
    projected_state = state_map.get(ledger_state)
    if (not projected_state or
            not database.is_valid_whatsapp_ledger_token(ledger_message_id)):
        return {'status': 'reserving', 'recovery_required': True}
    projected = _project_campaign_reservation(
        conn, plan_recipient_id, claim_id, projected_state,
        ledger_message_id=ledger_message_id,
        failure_code=record.get('failure_code') if projected_state == 'failed' else None,
        now=now,
    )
    return {
        'status': projected_state if projected else 'reserving',
        'ledger_message_id': ledger_message_id,
        'projected': projected,
        'recovery_required': not projected,
    }

def recover_campaign_plan_recipient(conn, plan_recipient_id, ledger_lookup,
                                    now=None):
    """Recover a reservation projection without reserving or sending."""
    if not callable(ledger_lookup):
        raise ValueError('A ledger lookup adapter is required.')
    now = _campaign_reservation_now(now)
    row = _campaign_reservation_row(conn, plan_recipient_id)
    if not row:
        return {'status': 'not_found'}
    if row['cancellation_state'] in {'checking', 'blocked', 'cancelled'} or row['plan_state'] == 'cancelled':
        return {'status': 'cancellation_pending', 'recovery_required': True}
    if row['state'] in {'reserved', 'attempted', 'acknowledged', 'failed', 'delivered_unknown'}:
        return _campaign_reservation_result(row)
    if row['state'] != 'reserving' or not row['reservation_lease_until'] or row['reservation_lease_until'] > now:
        return _campaign_reservation_result(row)

    claim_id = secrets.token_urlsafe(24)
    lease_until = (datetime.strptime(now, '%Y-%m-%d %H:%M:%S') + timedelta(seconds=CAMPAIGN_RESERVATION_LEASE_SECONDS)).strftime('%Y-%m-%d %H:%M:%S')
    if not _claim_campaign_plan_recipient(conn, plan_recipient_id, claim_id, lease_until, now):
        return _campaign_reservation_result(_campaign_reservation_row(conn, plan_recipient_id))
    row = _campaign_reservation_row(conn, plan_recipient_id)
    key = campaign_plan_recipient_idempotency_key(row['plan_id'], plan_recipient_id)
    try:
        record = ledger_lookup(key)
    except Exception:
        # A lookup failure is not evidence that the reservation is absent.
        # Keep the claim recoverable and require an operator/recovery retry.
        return {
            'status': 'reserving',
            'recovery_required': True,
            'error_code': 'LEDGER_LOOKUP_UNAVAILABLE',
        }
    if record is None:
        projected = _project_campaign_reservation(conn, plan_recipient_id, claim_id, 'planned', now=now)
        return {'status': 'planned' if projected else 'reserving', 'projected': projected}
    if not isinstance(record, dict):
        return {
            'status': 'reserving',
            'recovery_required': True,
            'error_code': 'INVALID_LEDGER_RECORD',
        }

    state_map = {
        'queued': 'reserved', 'attempted': 'attempted',
        'acknowledged': 'acknowledged', 'failed': 'failed',
        'delivered_unknown': 'delivered_unknown',
    }
    projected_state = state_map.get(record.get('state'))
    ledger_message_id = record.get('message_id')
    if (not projected_state or
            not database.is_valid_whatsapp_ledger_token(ledger_message_id)):
        return {'status': 'reserving', 'recovery_required': True}
    projected = _project_campaign_reservation(
        conn, plan_recipient_id, claim_id, projected_state,
        ledger_message_id=ledger_message_id,
        failure_code=record.get('failure_code') if projected_state == 'failed' else None,
        now=now,
    )
    return {
        'status': projected_state if projected else 'reserving',
        'ledger_message_id': ledger_message_id,
        'projected': projected,
        'recovery_required': not projected,
    }

def _campaign_batch_plan_row(conn, plan_id):
    return conn.execute('''
        SELECT id, task_id, campaign_id, recipient_fingerprint,
               sender_worker_id, content_reference,
               recipient_count, requested_quantity, state, batch_id, batch_request_fingerprint,
               batch_content_type, batch_state, batch_updated_at,
               dispatch_state, dispatch_operation_id, dispatch_started_at,
               cancellation_state, cancellation_evidence_status,
               cancellation_requested_at, cancellation_checked_at
        FROM whatsapp_campaign_plans WHERE id = ?
    ''', (plan_id,)).fetchone()

def _campaign_batch_recipient_rows(conn, plan_id):
    return conn.execute('''
        SELECT id, recipient_number, position, state, reservation_claim_id,
               reservation_lease_until, ledger_message_id, failure_code,
               idempotency_key
        FROM whatsapp_campaign_plan_recipients
        WHERE plan_id = ? ORDER BY position
    ''', (plan_id,)).fetchall()

def _ensure_campaign_batch_metadata(conn, plan_id):
    plan = _campaign_batch_plan_row(conn, plan_id)
    if not plan:
        raise ValueError('Campaign plan not found.')
    if plan['batch_id'] and plan['batch_request_fingerprint']:
        return plan
    rows = _campaign_batch_recipient_rows(conn, plan_id)
    batch_id = _campaign_batch_id(plan_id)
    fingerprint = _campaign_batch_fingerprint(
        batch_id, plan['sender_worker_id'], plan['campaign_id'], plan['task_id'],
        plan_id, rows, plan['content_reference'], plan['batch_content_type'] or 'text',
    )
    conn.execute('BEGIN IMMEDIATE')
    try:
        current = _campaign_batch_plan_row(conn, plan_id)
        if current['batch_id'] and current['batch_request_fingerprint']:
            if current['batch_id'] != batch_id or current['batch_request_fingerprint'] != fingerprint:
                raise RuntimeError('Campaign batch identity is inconsistent.')
        else:
            conn.execute('''
                UPDATE whatsapp_campaign_plans
                SET batch_id = ?, batch_request_fingerprint = ?,
                    batch_state = COALESCE(batch_state, 'pending'),
                    batch_updated_at = COALESCE(batch_updated_at, CURRENT_TIMESTAMP)
                WHERE id = ? AND batch_id IS NULL
            ''', (batch_id, fingerprint, plan_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return _campaign_batch_plan_row(conn, plan_id)

def _set_campaign_batch_state(conn, plan_id, expected_states, next_state, now=None):
    if next_state not in CAMPAIGN_BATCH_STATE_VALUES:
        raise ValueError('Invalid campaign batch state.')
    now = _campaign_reservation_now(now)
    states = tuple(expected_states)
    placeholders = ','.join('?' for _ in states)
    conn.execute('BEGIN IMMEDIATE')
    try:
        result = conn.execute(f'''
            UPDATE whatsapp_campaign_plans
            SET batch_state = ?, batch_updated_at = ?
            WHERE id = ? AND batch_state IN ({placeholders})
        ''', (next_state, now, plan_id, *states))
        conn.commit()
        return result.rowcount == 1
    except Exception:
        conn.rollback()
        raise

def _claim_campaign_batch(conn, plan_id, now, allow_existing=False):
    now = _campaign_reservation_now(now)
    claim_id = secrets.token_urlsafe(24)
    lease_until = (datetime.strptime(now, '%Y-%m-%d %H:%M:%S') + timedelta(
        seconds=CAMPAIGN_RESERVATION_LEASE_SECONDS
    )).strftime('%Y-%m-%d %H:%M:%S')
    conn.execute('BEGIN IMMEDIATE')
    try:
        plan = _campaign_batch_plan_row(conn, plan_id)
        rows = _campaign_batch_recipient_rows(conn, plan_id)
        if not plan or not rows:
            raise ValueError('Campaign batch is not available.')
        if plan['state'] != 'ready' or plan['cancellation_state'] != 'not_requested':
            raise RuntimeError('Campaign batch is unavailable during cancellation.')
        active_claims = {
            row['reservation_claim_id'] for row in rows
            if row['state'] == 'reserving' and row['reservation_claim_id']
            and row['reservation_lease_until'] and row['reservation_lease_until'] > now
        }
        if len(active_claims) > 1:
            raise RuntimeError('Campaign batch has conflicting active claims.')
        if active_claims:
            existing_claim = next(iter(active_claims))
            if any(row['state'] == 'planned' for row in rows):
                raise RuntimeError('Campaign batch has a partial active claim.')
            if allow_existing:
                conn.commit()
                return {'claimed': True, 'in_progress': False, 'claim_id': existing_claim}
            conn.commit()
            return {'claimed': False, 'in_progress': True, 'claim_id': existing_claim}

        for row in rows:
            if row['state'] == 'reserving' and row['reservation_lease_until'] and row['reservation_lease_until'] > now:
                raise RuntimeError('Campaign batch has an active reservation claim.')
            if row['state'] in {'reserved', 'attempted', 'acknowledged', 'failed', 'delivered_unknown'}:
                continue
            if row['state'] != 'planned':
                raise RuntimeError('Campaign batch has an invalid recipient state.')
            conn.execute('''
                UPDATE whatsapp_campaign_plan_recipients
                SET state = 'reserving', reservation_claim_id = ?,
                    reservation_lease_until = ?, updated_at = ?
                WHERE id = ? AND state = 'planned'
            ''', (claim_id, lease_until, now, row['id']))
        result = conn.execute('''
            UPDATE whatsapp_campaign_plans
            SET batch_state = 'reserving', batch_updated_at = ?
            WHERE id = ? AND state = 'ready'
              AND cancellation_state = 'not_requested'
              AND batch_state IN ('pending', 'unknown', 'reserving')
        ''', (now, plan_id))
        if result.rowcount != 1:
            raise RuntimeError('Campaign batch state changed during claim.')
        conn.commit()
        return {'claimed': True, 'in_progress': False, 'claim_id': claim_id}
    except Exception:
        conn.rollback()
        raise

def _validate_campaign_batch_response(plan, rows, result):
    if not isinstance(result, dict) or result.get('success') is not True:
        raise ValueError('Invalid campaign batch response.')
    batch = result.get('batch')
    if not isinstance(batch, dict):
        raise ValueError('Invalid campaign batch response.')
    expected = {
        'batch_id': plan['batch_id'],
        'request_fingerprint': plan['batch_request_fingerprint'],
        'sender_worker_id': plan['sender_worker_id'],
        'campaign_id': plan['campaign_id'],
        'task_id': plan['task_id'],
        'plan_id': plan['id'],
        'item_count': len(rows),
        'state': 'reserved',
    }
    for field, value in expected.items():
        if batch.get(field) != value:
            raise ValueError('Campaign batch identity validation failed.')
    items = batch.get('items')
    if not isinstance(items, list) or len(items) != len(rows):
        raise ValueError('Campaign batch item count validation failed.')
    by_position = {}
    for item in items:
        if not isinstance(item, dict) or item.get('position') in by_position:
            raise ValueError('Campaign batch item identity validation failed.')
        by_position[item.get('position')] = item
    validated = []
    expected_content_type = plan['batch_content_type'] or 'text'
    for row in rows:
        item = by_position.get(row['position'])
        key = campaign_plan_recipient_idempotency_key(plan['id'], row['id'])
        expected_jid = recipient_number_to_jid(row['recipient_number'])
        if (
            not item or item.get('plan_id') != plan['id'] or
            item.get('plan_recipient_id') != row['id'] or
            item.get('idempotency_key') != key or
            item.get('message_id') != key or
            not database.is_valid_whatsapp_ledger_token(item.get('message_id')) or
            item.get('state') != 'reserved' or
            item.get('recipient_jid') != expected_jid or
            item.get('content_type') != expected_content_type or
            item.get('payload_reference') != plan['content_reference']
        ):
            raise ValueError('Campaign batch item integrity validation failed.')
        validated.append((row, item))
    return validated

def _project_campaign_batch(conn, plan_id, claim_id, validated, now):
    now = _campaign_reservation_now(now)
    conn.execute('BEGIN IMMEDIATE')
    try:
        for row, item in validated:
            current = conn.execute('''
                SELECT state, reservation_claim_id, ledger_message_id
                FROM whatsapp_campaign_plan_recipients WHERE id = ?
            ''', (row['id'],)).fetchone()
            if not current:
                raise RuntimeError('Campaign recipient disappeared during projection.')
            if current['state'] in {'reserved', 'attempted', 'acknowledged', 'failed', 'delivered_unknown'}:
                if current['ledger_message_id'] != item['message_id']:
                    raise RuntimeError('Campaign recipient ledger identity changed.')
                continue
            if current['state'] != 'reserving' or current['reservation_claim_id'] != claim_id:
                raise RuntimeError('Campaign recipient claim ownership changed.')
            changed = conn.execute('''
                UPDATE whatsapp_campaign_plan_recipients
                SET state = 'reserved', ledger_message_id = ?,
                    reservation_claim_id = NULL, reservation_lease_until = NULL,
                    failure_code = NULL, updated_at = ?
                WHERE id = ? AND state = 'reserving'
                  AND reservation_claim_id = ?
                  AND (ledger_message_id IS NULL OR ledger_message_id = ?)
            ''', (item['message_id'], now, row['id'], claim_id, item['message_id']))
            if changed.rowcount != 1:
                raise RuntimeError('Campaign recipient projection lost ownership.')
        result = conn.execute('''
            UPDATE whatsapp_campaign_plans
            SET batch_state = 'reserved', batch_updated_at = ?
            WHERE id = ? AND batch_state IN ('reserving', 'unknown', 'reserved')
        ''', (now, plan_id))
        if result.rowcount != 1:
            raise RuntimeError('Campaign batch projection state changed.')
        conn.commit()
    except Exception:
        conn.rollback()
        raise

def _project_campaign_batch_failure(conn, plan_id, claim_id, failure_code, now):
    now = _campaign_reservation_now(now)
    conn.execute('BEGIN IMMEDIATE')
    try:
        rows = _campaign_batch_recipient_rows(conn, plan_id)
        for row in rows:
            if row['state'] != 'reserving' or row['reservation_claim_id'] != claim_id:
                raise RuntimeError('Campaign batch failure projection lost ownership.')
        for row in rows:
            conn.execute('''
                UPDATE whatsapp_campaign_plan_recipients
                SET state = 'failed', failure_code = ?, reservation_claim_id = NULL,
                    reservation_lease_until = NULL, updated_at = ?
                WHERE id = ? AND state = 'reserving' AND reservation_claim_id = ?
            ''', (failure_code, now, row['id'], claim_id))
        result = conn.execute('''
            UPDATE whatsapp_campaign_plans
            SET batch_state = 'failed', batch_updated_at = ?
            WHERE id = ? AND batch_state = 'reserving'
        ''', (now, plan_id))
        if result.rowcount != 1:
            raise RuntimeError('Campaign batch failure state changed.')
        conn.commit()
    except Exception:
        conn.rollback()
        raise

DISPATCH_AUTHORIZATION_TTL_SECONDS = 60
DISPATCH_AUTHORIZATION_MAX_TTL_SECONDS = 120
DISPATCH_AUTHORIZATION_VERSION = 1

def derive_dispatch_authorization_key(secret):
    if not secret or not isinstance(secret, str):
        raise ValueError('A non-empty shared secret is required.')
    return hmac.new(
        secret.encode('utf-8'),
        b'taskpe-wa-dispatch-authorization-v1',
        hashlib.sha256,
    ).digest()

def create_dispatch_authorization_token(plan, worker_id, secret, now=None, ttl_seconds=DISPATCH_AUTHORIZATION_TTL_SECONDS, operation_id=None):
    if isinstance(worker_id, bool) or not isinstance(worker_id, int) or worker_id < 1:
        raise ValueError('Invalid worker ID for dispatch authorization.')
    if not plan or not isinstance(plan, (dict, sqlite3.Row)):
        raise ValueError('Invalid plan for dispatch authorization.')
    plan_dict = dict(plan)
    plan_id = plan_dict.get('id', plan_dict.get('plan_id'))
    batch_id = plan_dict.get('batch_id')
    op_id = operation_id if operation_id is not None else plan_dict.get('dispatch_operation_id')
    request_fingerprint = plan_dict.get('batch_request_fingerprint', plan_dict.get('request_fingerprint'))
    if isinstance(plan_id, bool) or not isinstance(plan_id, int) or plan_id < 1:
        raise ValueError('Plan is missing a valid plan ID for dispatch authorization.')
    if not isinstance(batch_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,199}', batch_id):
        raise ValueError('Plan is missing a valid batch ID for dispatch authorization.')
    if not isinstance(op_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,199}', op_id):
        raise ValueError('Plan is missing a valid dispatch operation ID.')
    if not isinstance(request_fingerprint, str) or not re.fullmatch(r'[a-f0-9]{64}', request_fingerprint):
        raise ValueError('Plan is missing a valid request fingerprint.')
    persisted_worker_id = plan_dict.get('sender_worker_id')
    if persisted_worker_id is not None and persisted_worker_id != worker_id:
        raise ValueError('Dispatch authorization worker mismatch.')
    if not isinstance(secret, str) or len(secret) < 32:
        raise ValueError('A sufficiently strong shared secret is required.')
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= DISPATCH_AUTHORIZATION_MAX_TTL_SECONDS:
        raise ValueError('Invalid dispatch authorization lifetime.')

    if now is None:
        issued_dt = datetime.now()
    elif isinstance(now, datetime):
        issued_dt = now
    elif isinstance(now, (int, float)):
        issued_dt = datetime.fromtimestamp(now)
    elif isinstance(now, str):
        try:
            issued_dt = datetime.fromisoformat(now.replace('Z', '+00:00')).replace(tzinfo=None)
        except ValueError:
            issued_dt = datetime.strptime(now, '%Y-%m-%d %H:%M:%S')
    else:
        issued_dt = datetime.now()

    issued_at = int(issued_dt.timestamp())
    expires_at = issued_at + ttl_seconds

    payload = {
        'v': DISPATCH_AUTHORIZATION_VERSION,
        'auth_id': secrets.token_urlsafe(24),
        'worker_id': worker_id,
        'plan_id': plan_id,
        'batch_id': batch_id,
        'operation_id': op_id,
        'request_fingerprint': request_fingerprint,
        'issued_at': issued_at,
        'expires_at': expires_at,
    }
    payload_bytes = json.dumps(payload, separators=(',', ':'), sort_keys=True).encode('utf-8')
    b64_payload = base64.urlsafe_b64encode(payload_bytes).decode('ascii').rstrip('=')
    canonical_data = f"v{DISPATCH_AUTHORIZATION_VERSION}.{b64_payload}".encode('utf-8')
    derived_key = derive_dispatch_authorization_key(secret)
    signature = hmac.new(derived_key, canonical_data, hashlib.sha256).hexdigest()
    return f"v{DISPATCH_AUTHORIZATION_VERSION}.{b64_payload}.{signature}"

def _campaign_batch_adapter_call(node_call, operation, worker_id, path, payload=None, extra_headers=None):
    if node_call is not None:
        try:
            response = node_call(operation, payload, extra_headers=extra_headers)
        except TypeError:
            response = node_call(operation, payload)
        if not isinstance(response, tuple) or len(response) != 2:
            raise RuntimeError('Invalid internal WhatsApp adapter response.')
        return response
    method = 'POST' if operation in {'reserve', 'send'} else 'GET'
    return _call_whatsapp_service(method, path, worker_id, payload, extra_headers=extra_headers)

def reserve_campaign_send_plan_with_node(conn, plan_id, node_call=None,
                                         now=None, content_type='text'):
    """Reserve a Flask plan in Node and project the complete durable result.

    This is an internal server-side operation. ``worker_id`` is trusted only
    after matching the plan owner; browser input is never used as identity.
    Unknown Node outcomes remain recoverable and never release claims.
    """
    if content_type not in {'text', 'image', 'video'}:
        raise ValueError('Invalid message content type.')
    now = _campaign_reservation_now(now)
    plan = _ensure_campaign_batch_metadata(conn, plan_id)
    authenticated_worker_id = session.get('user_id')
    if isinstance(authenticated_worker_id, bool) or not isinstance(authenticated_worker_id, int) or authenticated_worker_id < 1:
        raise PermissionError('Authenticated worker is required.')
    if plan['sender_worker_id'] != authenticated_worker_id:
        raise PermissionError('Campaign plan ownership mismatch.')
    if content_type != (plan['batch_content_type'] or 'text'):
        raise ValueError('Campaign batch content type does not match the persisted plan.')
    if plan['state'] == 'cancelled' or plan['cancellation_state'] in {'checking', 'blocked', 'cancelled'}:
        return {
            'status': 'cancellation_pending' if plan['state'] != 'cancelled' else 'cancelled',
            'batch_id': plan['batch_id'],
            'recovery_required': plan['state'] != 'cancelled',
        }
    if plan['state'] != 'ready':
        return {'status': plan['batch_state'], 'batch_id': plan['batch_id'], 'recovery_required': plan['batch_state'] == 'unknown'}
    if plan['batch_state'] == 'reserved':
        return {'status': 'reserved', 'batch_id': plan['batch_id'], 'idempotent': True}
    rows = _campaign_batch_recipient_rows(conn, plan_id)
    if not rows or len(rows) > MAX_CAMPAIGN_PLAN_RECIPIENTS:
        raise RuntimeError('Campaign batch recipient set is invalid.')

    def lookup_and_project():
        try:
            response, status = _campaign_batch_adapter_call(
                node_call, 'lookup', authenticated_worker_id,
                f"/v1/users/{authenticated_worker_id}/campaign-batches/{plan['batch_id']}"
            )
        except Exception:
            _set_campaign_batch_state(conn, plan_id, ('reserving',), 'unknown', now)
            return {'status': 'unknown', 'batch_id': plan['batch_id'], 'recovery_required': True, 'error_code': 'NODE_LOOKUP_UNAVAILABLE'}
        if status != 200 or not isinstance(response, dict) or response.get('success') is not True:
            _set_campaign_batch_state(conn, plan_id, ('reserving',), 'unknown', now)
            return {'status': 'unknown', 'batch_id': plan['batch_id'], 'recovery_required': True, 'error_code': 'NODE_LOOKUP_UNAVAILABLE'}
        if response.get('found') is False:
            return None
        try:
            validated = _validate_campaign_batch_response(plan, rows, response)
            claim = next((r['reservation_claim_id'] for r in rows if r['reservation_claim_id']), None)
            _project_campaign_batch(conn, plan_id, claim, validated, now)
            return {'status': 'reserved', 'batch_id': plan['batch_id'], 'recovered': True}
        except Exception:
            _set_campaign_batch_state(conn, plan_id, ('reserving',), 'unknown', now)
            return {'status': 'unknown', 'batch_id': plan['batch_id'], 'recovery_required': True, 'error_code': 'INVALID_NODE_BATCH_RESPONSE'}

    if plan['batch_state'] in {'reserving', 'unknown'}:
        recovered = lookup_and_project()
        if recovered is not None:
            return recovered

    try:
        claim = _claim_campaign_batch(
            conn, plan_id, now,
            allow_existing=plan['batch_state'] in {'reserving', 'unknown'},
        )
    except Exception:
        return {'status': 'unknown', 'batch_id': plan['batch_id'], 'recovery_required': True, 'error_code': 'CLAIM_UNAVAILABLE'}
    if claim.get('in_progress'):
        return {'status': 'reserving', 'batch_id': plan['batch_id'], 'recovery_required': True}

    items = []
    for row in rows:
        key = campaign_plan_recipient_idempotency_key(plan_id, row['id'])
        items.append({
            'position': row['position'], 'planRecipientId': row['id'],
            'recipientJid': recipient_number_to_jid(row['recipient_number']),
            'contentType': content_type, 'payloadReference': plan['content_reference'],
        })
    payload = {
        'batchId': plan['batch_id'],
        'requestFingerprint': plan['batch_request_fingerprint'],
        'senderWorkerId': authenticated_worker_id,
        'campaignId': plan['campaign_id'], 'taskId': plan['task_id'],
        'planId': plan_id, 'items': items,
    }
    try:
        response, status = _campaign_batch_adapter_call(
            node_call, 'reserve', authenticated_worker_id,
            f"/v1/users/{authenticated_worker_id}/campaign-batches/reserve", payload
        )
    except Exception:
        _set_campaign_batch_state(conn, plan_id, ('reserving',), 'unknown', now)
        return {'status': 'unknown', 'batch_id': plan['batch_id'], 'recovery_required': True, 'error_code': 'NODE_RESERVATION_UNAVAILABLE'}
    if status == 409 and isinstance(response, dict) and response.get('status') == 'quota_exceeded':
        try:
            _project_campaign_batch_failure(conn, plan_id, claim['claim_id'], 'QUOTA_EXCEEDED', now)
            return {'status': 'failed', 'batch_id': plan['batch_id'], 'failure_code': 'QUOTA_EXCEEDED'}
        except Exception:
            return {'status': 'unknown', 'batch_id': plan['batch_id'], 'recovery_required': True, 'error_code': 'PROJECTION_FAILED'}
    try:
        validated = _validate_campaign_batch_response(plan, rows, response)
        _project_campaign_batch(conn, plan_id, claim['claim_id'], validated, now)
        return {'status': 'reserved', 'batch_id': plan['batch_id'], 'reserved_count': len(validated)}
    except Exception:
        _set_campaign_batch_state(conn, plan_id, ('reserving',), 'unknown', now)
        return {'status': 'unknown', 'batch_id': plan['batch_id'], 'recovery_required': True, 'error_code': 'INVALID_NODE_BATCH_RESPONSE'}


def _validate_campaign_send_plan_for_dispatch(conn, plan_id, worker_id,
                                              allow_terminal=False):
    """Validate a reserved plan without accepting any browser-supplied payload."""
    plan = _campaign_batch_plan_row(conn, plan_id)
    if not plan or plan['sender_worker_id'] != worker_id:
        raise PermissionError('Campaign plan ownership validation failed.')
    task = conn.execute(
        'SELECT * FROM tasks WHERE id = ?', (plan['task_id'],)
    ).fetchone()
    if not task or task['status'] != 'active' or task['task_type'] not in AUTOMATED_WHATSAPP_TASK_TYPES:
        raise ValueError('Campaign is not available for sending.')
    if not business_campaign_is_approved(conn, task):
        raise ValueError('Campaign approval or recipient declaration is invalid.')
    expected_content_type = 'image' if task['task_type'] == 'IMAGE_TEXT_LINK' else 'text'
    expected_content_reference = f"task:{plan['task_id']}:{task['task_type'].lower()}:v1"
    if (
        plan['state'] != 'ready' or plan['batch_state'] != 'reserved' or
        plan['cancellation_state'] != 'not_requested' or
        not plan['batch_id'] or not plan['batch_request_fingerprint'] or
        not plan['content_reference'] or plan['content_reference'] != expected_content_reference or
        plan['batch_content_type'] != expected_content_type
    ):
        raise ValueError('Campaign reservation is not sendable.')
    requested = plan['requested_quantity']
    if requested is None or (requested != 'all' and (not requested.isdigit() or int(requested) < 1)):
        raise ValueError('Campaign quantity is not persisted safely.')
    rows = _campaign_batch_recipient_rows(conn, plan_id)
    if not rows or len(rows) != plan['recipient_count']:
        raise ValueError('Campaign recipient snapshot is invalid.')
    if requested != 'all' and int(requested) != len(rows):
        raise ValueError('Campaign quantity does not match the persisted recipient snapshot.')
    expected_fingerprint = _campaign_batch_fingerprint(
        plan['batch_id'], worker_id, plan['campaign_id'], plan['task_id'],
        plan_id, rows, plan['content_reference'], plan['batch_content_type'],
    )
    if plan['batch_request_fingerprint'] != expected_fingerprint:
        raise ValueError('Campaign batch fingerprint is invalid.')
    claimed = conn.execute('''
        SELECT COUNT(*) AS count
        FROM whatsapp_campaign_recipient_claims
        WHERE plan_id = ? AND state = 'claimed'
    ''', (plan_id,)).fetchone()['count']
    if claimed != len(rows):
        raise ValueError('Campaign recipient claims are not intact.')
    for row in rows:
        expected_key = database.whatsapp_campaign_plan_recipient_idempotency_key(plan_id, row['id'])
        if (
            row['state'] not in ({'reserved', 'attempted', 'acknowledged', 'failed', 'delivered_unknown'} if allow_terminal else {'reserved'}) or
            not row['ledger_message_id'] or
            not database.is_valid_whatsapp_ledger_token(row['ledger_message_id']) or
            row['idempotency_key'] != expected_key
        ):
            raise ValueError('Campaign recipient reservation is invalid.')
    return plan, rows


def _validate_campaign_send_response(plan, rows, response):
    """Validate all Node send results before projecting any local outcomes."""
    if not isinstance(response, dict) or response.get('success') is not True:
        raise ValueError('Invalid campaign send response.')
    if response.get('batch_id') != plan['batch_id'] or not isinstance(response.get('items'), list):
        raise ValueError('Campaign send identity validation failed.')
    items = response['items']
    if len(items) != len(rows):
        raise ValueError('Campaign send item count validation failed.')
    by_position = {}
    allowed = {'acknowledged', 'failed', 'uncertain', 'not_sendable'}
    for item in items:
        if not isinstance(item, dict) or item.get('position') in by_position:
            raise ValueError('Campaign send item identity validation failed.')
        if item.get('status') not in allowed or not isinstance(item.get('message_id'), str):
            raise ValueError('Campaign send item result is invalid.')
        by_position[item['position']] = item
    validated = []
    for row in rows:
        item = by_position.get(row['position'])
        if not item or item['message_id'] != row['ledger_message_id']:
            raise ValueError('Campaign send item identity validation failed.')
        validated.append((row, item))
    return validated


def _project_campaign_send_outcomes(conn, validated, now=None,
                                    plan_id=None, dispatch_operation_id=None):
    """Project only validated outcomes; never downgrade a terminal local state."""
    now = _campaign_reservation_now(now)
    conn.execute('BEGIN IMMEDIATE')
    try:
        counts = {'acknowledged': 0, 'failed': 0, 'uncertain': 0, 'not_sendable': 0}
        for row, item in validated:
            status = item['status']
            counts[status] += 1
            if status == 'not_sendable':
                continue
            next_state = 'delivered_unknown' if status == 'uncertain' else status
            current = conn.execute(
                'SELECT state, ledger_message_id FROM whatsapp_campaign_plan_recipients WHERE id = ?',
                (row['id'],),
            ).fetchone()
            if not current or current['ledger_message_id'] != row['ledger_message_id']:
                raise RuntimeError('Campaign send projection identity changed.')
            if current['state'] in {'acknowledged', 'failed', 'delivered_unknown'}:
                if current['state'] != next_state:
                    raise RuntimeError('Campaign send outcome conflicts with a terminal local state.')
                continue
            changed = conn.execute('''
                UPDATE whatsapp_campaign_plan_recipients
                SET state = ?, updated_at = ?
                WHERE id = ? AND ledger_message_id = ? AND state IN ('reserved', 'attempted')
            ''', (next_state, now, row['id'], row['ledger_message_id']))
            if changed.rowcount != 1:
                raise RuntimeError('Campaign send projection lost ownership.')
        if plan_id is not None:
            changed = conn.execute('''
                UPDATE whatsapp_campaign_plans
                SET dispatch_state = 'complete', dispatch_started_at = COALESCE(dispatch_started_at, ?)
                WHERE id = ? AND dispatch_state IN ('in_progress', 'unknown')
                  AND dispatch_operation_id = ?
            ''', (now, plan_id, dispatch_operation_id))
            if changed.rowcount != 1:
                raise RuntimeError('Campaign dispatch ownership changed.')
        conn.commit()
        return counts
    except Exception:
        conn.rollback()
        raise


def _claim_campaign_dispatch(conn, plan_id, worker_id, now):
    operation_id = secrets.token_urlsafe(24)
    conn.execute('BEGIN IMMEDIATE')
    try:
        plan = _campaign_batch_plan_row(conn, plan_id)
        if not plan or plan['sender_worker_id'] != worker_id:
            raise PermissionError('Campaign plan ownership validation failed.')
        if plan['dispatch_state'] != 'not_started':
            conn.commit()
            return {'claimed': False, 'state': plan['dispatch_state'], 'operation_id': plan['dispatch_operation_id']}
        changed = conn.execute('''
            UPDATE whatsapp_campaign_plans
            SET dispatch_state = 'in_progress', dispatch_operation_id = ?,
                dispatch_started_at = ?
            WHERE id = ? AND sender_worker_id = ?
              AND state = 'ready' AND batch_state = 'reserved'
              AND cancellation_state = 'not_requested'
              AND dispatch_state = 'not_started'
        ''', (operation_id, now, plan_id, worker_id))
        if changed.rowcount != 1:
            conn.commit()
            return {'claimed': False, 'state': 'in_progress', 'operation_id': None}
        conn.commit()
        return {'claimed': True, 'state': 'in_progress', 'operation_id': operation_id}
    except Exception:
        conn.rollback()
        raise


def _mark_campaign_dispatch_unknown(conn, plan_id, operation_id, now):
    conn.execute('BEGIN IMMEDIATE')
    try:
        changed = conn.execute('''
            UPDATE whatsapp_campaign_plans
            SET dispatch_state = 'unknown', dispatch_started_at = COALESCE(dispatch_started_at, ?)
            WHERE id = ? AND dispatch_state = 'in_progress'
              AND dispatch_operation_id = ?
        ''', (now, plan_id, operation_id))
        conn.commit()
        return changed.rowcount == 1
    except Exception:
        conn.rollback()
        raise


def _campaign_dispatch_recovery(conn, plan, rows, worker_id, node_call, now):
    """Recover terminal Node outcomes without ever calling the send endpoint."""
    try:
        response, status = _campaign_batch_adapter_call(
            node_call, 'lookup', worker_id,
            f"/v1/users/{worker_id}/campaign-batches/{plan['batch_id']}",
        )
        if status != 200 or not isinstance(response, dict) or response.get('success') is not True or response.get('found') is not True:
            return {'status': 'recovery_required', 'recovery_required': True, 'delivered': False}
        batch = response.get('batch')
        items = response.get('items')
        if not isinstance(batch, dict) or not isinstance(items, list):
            raise ValueError('Invalid campaign recovery response.')
        for field, expected in {
            'batch_id': plan['batch_id'],
            'request_fingerprint': plan['batch_request_fingerprint'],
            'sender_worker_id': worker_id,
            'campaign_id': plan['campaign_id'],
            'task_id': plan['task_id'],
            'plan_id': plan['id'],
            'item_count': len(rows),
        }.items():
            if batch.get(field) != expected:
                raise ValueError('Campaign recovery identity validation failed.')
        if len(items) != len(rows):
            raise ValueError('Campaign recovery item count validation failed.')
        by_position = {item.get('position'): item for item in items if isinstance(item, dict)}
        if len(by_position) != len(items):
            raise ValueError('Campaign recovery item identity validation failed.')
        validated = []
        for row in rows:
            item = by_position.get(row['position'])
            if not item or item.get('message_id') != row['ledger_message_id']:
                raise ValueError('Campaign recovery item identity validation failed.')
            state = item.get('state')
            if state not in {'acknowledged', 'failed', 'delivered_unknown'}:
                return {'status': 'recovery_required', 'recovery_required': True, 'delivered': False}
            validated.append((row, {'status': 'uncertain' if state == 'delivered_unknown' else state, 'message_id': item['message_id']}))
        counts = _project_campaign_send_outcomes(
            conn, validated, now=now, plan_id=plan['id'],
            dispatch_operation_id=plan['dispatch_operation_id'],
        )
        if counts['failed']:
            return {'status': 'completed_with_failures', 'delivered': False, **counts}
        if counts['uncertain']:
            return {'status': 'recovery_required', 'recovery_required': True, 'delivered': False, **counts}
        return {'status': 'acknowledged', 'delivered': False, **counts}
    except Exception:
        return {'status': 'recovery_required', 'recovery_required': True, 'delivered': False}


def _evaluate_recipients_consent(conn, recipients):
    """Evaluate recipient consent and opt-out status.

    Returns (is_valid, error_message).
    - If any recipient is opted out, is_valid is False and error_message identifies opted-out numbers.
    - If any recipient is missing a shop declaration ('shop_attested'), is_valid is False.
    - If all recipients have a valid shop declaration and are not opted out, is_valid is True.
    """
    if not recipients:
        return True, None
    res = database.check_recipients_consent_and_opt_out(conn, recipients)
    if res['opted_out']:
        return False, f"Recipients opted out: {res['opted_out']}"
    if res['unattested']:
        return False, f"Recipients missing shop declaration consent: {res['unattested']}"
    return True, None


def _validate_campaign_consent_declaration_for_dispatch(conn, plan, rows):
    """Require an immutable declaration for the complete source audience.

    Planned recipients may be a strict subset when eligibility excluded
    recorded opt-outs, but every planned recipient must remain declared and
    non-opted-out. Missing, changed, malformed, or opted-out data fails closed.
    """
    if not hasattr(conn, 'execute') or not plan or not rows:
        return False
    try:
        task_id = plan['task_id']
        campaign_id = plan['campaign_id']
        declaration = database.get_whatsapp_campaign_consent_declaration(conn, task_id, campaign_id)
        if not declaration:
            return False
        if declaration['declaration_text'] != database.SHOP_CONSENT_DECLARATION_DEFAULT_TEXT:
            return False
        declared = json.loads(declaration['normalized_recipient_list'])
        if not isinstance(declared, list) or any(not isinstance(item, str) for item in declared):
            return False
        task = conn.execute(
            'SELECT * FROM tasks WHERE id = ? AND (campaign_id = ? OR campaign_id IS NULL)',
            (task_id, campaign_id),
        ).fetchone()
        source_recipients = task_recipient_pool(task) if task else None
        if not task or sorted(source_recipients) != declared:
            return False
        if (task['owner_user_id'] is not None and
                (declaration['business_user_id'] != task['owner_user_id'] or
                 declaration['campaign_id'] != (task['campaign_id'] or task['id']))):
            return False
        if declaration['recipient_count'] != len(source_recipients):
            return False
        encoded = json.dumps(sorted(set(source_recipients)), ensure_ascii=False, separators=(',', ':'))
        fingerprint = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
        if (declaration['recipient_fingerprint'] != fingerprint or
                plan['recipient_fingerprint'] != fingerprint):
            return False
        recipients = [row['recipient_number'] for row in rows]
        if (not recipients or any(not isinstance(item, str) for item in recipients)
                or len(recipients) != len(set(recipients))
                or any(item not in declared for item in recipients)):
            return False
        opted_out_keys = _authoritative_whatsapp_opt_out_keys(conn)
        return not any(_opt_out_lookup_keys(item) & opted_out_keys for item in recipients)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, sqlite3.Error):
        return False


def send_reserved_whatsapp_campaign_batch(conn, plan_id, worker_id,
                                           node_call=None, now=None):
    """Send an already-reserved batch through Node; no quota reservation occurs here."""
    if not WHATSAPP_CONSENT_GATE_AVAILABLE:
        return {'status': 'consent_required', 'recovery_required': False, 'delivered': False}
    if isinstance(worker_id, bool) or not isinstance(worker_id, int) or worker_id < 1:
        raise PermissionError('Authenticated worker is required.')
    now = _campaign_reservation_now(now)
    plan = _campaign_batch_plan_row(conn, plan_id)
    if not plan or plan['sender_worker_id'] != worker_id:
        raise PermissionError('Campaign plan ownership validation failed.')
    if plan['dispatch_state'] in {'in_progress', 'unknown'}:
        rows = _campaign_batch_recipient_rows(conn, plan_id)
        _validate_campaign_send_plan_for_dispatch(conn, plan_id, worker_id, allow_terminal=True)
        return _campaign_dispatch_recovery(conn, plan, rows, worker_id, node_call, now)
    if plan['dispatch_state'] == 'complete':
        return {'status': 'already_processed', 'delivered': False}
    plan, rows = _validate_campaign_send_plan_for_dispatch(conn, plan_id, worker_id)
    if hasattr(conn, 'execute') and not _validate_campaign_consent_declaration_for_dispatch(conn, plan, rows):
        return {'status': 'consent_required', 'recovery_required': False, 'delivered': False}
    claim = _claim_campaign_dispatch(conn, plan_id, worker_id, now)
    if not claim['claimed']:
        refreshed = _campaign_batch_plan_row(conn, plan_id)
        if refreshed['dispatch_state'] in {'in_progress', 'unknown'}:
            return _campaign_dispatch_recovery(conn, refreshed, rows, worker_id, node_call, now)
        return {'status': 'already_processed', 'delivered': False}
    operation_id = claim['operation_id']

    def uncertain_result():
        try:
            _mark_campaign_dispatch_unknown(conn, plan_id, operation_id, now)
        except Exception:
            # The in-progress durable claim remains non-dispatchable even if
            # recording the more specific unknown state fails.
            return {'status': 'recovery_required', 'recovery_required': True, 'delivered': False}
        return {'status': 'recovery_required', 'recovery_required': True, 'delivered': False}
    # Recheck the immutable declaration and opt-out list after the durable claim,
    # immediately before authorization/request creation.  A failed recheck is
    # made non-dispatchable rather than returning the batch to the queue.
    if hasattr(conn, 'execute') and not _validate_campaign_consent_declaration_for_dispatch(conn, plan, rows):
        return uncertain_result()
    try:
        authorization_token = create_dispatch_authorization_token(
            plan, worker_id, WHATSAPP_SERVICE_SHARED_SECRET, now=now, operation_id=operation_id,
        )
    except Exception:
        return uncertain_result()
    try:
        response, status = _campaign_batch_adapter_call(
            node_call, 'send', worker_id,
            f"/v1/users/{worker_id}/campaign-batches/{plan['batch_id']}" + '/send',
            extra_headers={'X-TaskPe-Dispatch-Authorization': authorization_token},
        )
    except Exception:
        return uncertain_result()
    if status != 200:
        return uncertain_result()
    try:
        validated = _validate_campaign_send_response(plan, rows, response)
        counts = _project_campaign_send_outcomes(
            conn, validated, now=now, plan_id=plan_id,
            dispatch_operation_id=operation_id,
        )
    except Exception:
        return uncertain_result()
    if counts['uncertain'] or counts['not_sendable']:
        result = uncertain_result()
        result.update(counts)
        return result
    if counts['failed']:
        return {'status': 'completed_with_failures', 'delivered': False, **counts}
    return {'status': 'acknowledged', 'delivered': False, **counts}

def get_assigned_numbers(assignment):
    try:
        return json.loads(assignment['assigned_numbers'])
    except Exception:
        return []


def create_assignment_send_plan(conn, assignment_id, sender_worker_id,
                                plan_key, selected_recipients=None):
    """Atomically create a deterministic campaign plan strictly from an assignment's allocated recipients."""
    if isinstance(sender_worker_id, bool) or not isinstance(sender_worker_id, int) or sender_worker_id < 1:
        raise ValueError('A valid sender worker is required.')
    if isinstance(assignment_id, bool) or not isinstance(assignment_id, int) or assignment_id < 1:
        raise ValueError('A valid assignment identity is required.')
    if not isinstance(plan_key, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,199}', plan_key):
        raise ValueError('A valid plan key is required.')

    assignment = conn.execute('''
        SELECT ta.id AS assignment_id, ta.user_id, ta.task_id, ta.campaign_id,
               ta.assigned_numbers, ta.status AS assignment_status, ta.expires_at,
               t.id AS t_id, t.title, t.task_type, t.template_text, t.destination_link,
               t.image_path, t.recipient_region, t.status AS task_status,
               t.target_numbers,
               t.campaign_id AS task_campaign_id, t.owner_user_id,
               t.campaign_review_status, t.campaign_approval_fingerprint
        FROM task_assignments ta
        JOIN tasks t ON t.id = ta.task_id
        WHERE ta.id = ?
    ''', (assignment_id,)).fetchone()

    if not assignment:
        raise ValueError('Assignment not found.')
    if assignment['user_id'] != sender_worker_id:
        raise PermissionError('Assignment does not belong to the authenticated worker.')
    if assignment['assignment_status'] != 'in_progress':
        raise ValueError(f'Assignment is not in progress (status: {assignment["assignment_status"]}).')
    if assignment['expires_at']:
        exp_row = conn.execute("SELECT 1 WHERE datetime(?) <= CURRENT_TIMESTAMP", (assignment['expires_at'],)).fetchone()
        if exp_row:
            raise ValueError('Assignment has expired.')
    if assignment['task_status'] != 'active':
        raise ValueError('Task is not active.')
    if not business_campaign_is_approved(conn, assignment):
        raise ValueError('Business campaign requires administrator approval.')
    source_fingerprint = _campaign_recipient_fingerprint(task_recipient_pool(assignment))

    task_type = assignment['task_type'] or ''
    if 'VIDEO' in task_type.upper():
        raise ValueError('Unsupported video content: video sending is not supported.')
    if task_type not in AUTOMATED_WHATSAPP_TASK_TYPES:
        raise ValueError(f'Task type {task_type} is not supported for automated WhatsApp sending.')

    content_type = 'image' if task_type == 'IMAGE_TEXT_LINK' else 'text'
    content_reference = f"task:{assignment['task_id']}:{task_type.lower()}:v1"
    campaign_id = assignment['campaign_id'] or assignment['task_id']

    assigned_raw = get_assigned_numbers(assignment)
    if not assigned_raw or not isinstance(assigned_raw, list):
        raise ValueError('Assignment has no assigned recipients.')
    assigned_numbers = [normalize_phone_number(n, region=assignment['recipient_region']) for n in assigned_raw]
    assigned_numbers = [number for number in assigned_numbers if number]
    if not assigned_numbers:
        raise ValueError('Assignment has no valid assigned recipient numbers.')
    assigned_set = set(assigned_numbers)

    if selected_recipients is not None:
        if not isinstance(selected_recipients, list) or len(selected_recipients) == 0:
            raise ValueError('Selected recipients must be a non-empty list.')
        selected_normalized = []
        for r in selected_recipients:
            norm_r = normalize_phone_number(r, region=assignment['recipient_region'])
            if not norm_r:
                raise ValueError(f'Invalid recipient number: {r}')
            if norm_r not in assigned_set:
                raise ValueError(f'Recipient {r} is outside the assigned numbers.')
            selected_normalized.append(norm_r)
        seen = set()
        recipients = []
        for r in selected_normalized:
            if r not in seen:
                seen.add(r)
                recipients.append(r)
    else:
        recipients = list(assigned_numbers)

    if len(recipients) > MAX_CAMPAIGN_PLAN_RECIPIENTS:
        raise ValueError(f'Campaign plans are limited to {MAX_CAMPAIGN_PLAN_RECIPIENTS} recipients.')

    placeholders = ','.join('?' for _ in recipients)
    allocated_rows = conn.execute(f'''
        SELECT phone_number, status, user_id, assignment_id
        FROM recipient_allocations
        WHERE campaign_id = ? AND assignment_id = ? AND user_id = ?
          AND phone_number IN ({placeholders})
    ''', (campaign_id, assignment_id, sender_worker_id, *recipients)).fetchall()

    allocated_map = {row['phone_number']: row['status'] for row in allocated_rows}
    for r in recipients:
        if r not in allocated_map:
            raise ValueError(f'Recipient {r} does not have an allocation row for this assignment.')
        if allocated_map[r] != 'ALLOCATED':
            raise ValueError(f'Recipient {r} is not in ALLOCATED state (status: {allocated_map[r]}).')

    request_fingerprint = hashlib.sha256(json.dumps({
        'assignment_id': assignment_id,
        'task_id': assignment['task_id'],
        'campaign_id': campaign_id,
        'sender_worker_id': sender_worker_id,
        'content_reference': content_reference,
        'recipients': recipients,
    }, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()

    conn.execute('BEGIN IMMEDIATE')
    try:
        existing = conn.execute('''
            SELECT id, request_fingerprint, recipient_fingerprint, task_id, campaign_id, sender_worker_id,
                   recipient_count, requested_quantity, state, batch_id,
                   batch_request_fingerprint, batch_content_type, batch_state,
                   created_at
            FROM whatsapp_campaign_plans WHERE plan_key = ?
        ''', (plan_key,)).fetchone()

        if existing:
            if (existing['request_fingerprint'] != request_fingerprint or
                    existing['recipient_fingerprint'] != source_fingerprint):
                raise ValueError('Plan key is already used for a different request.')
            conn.commit()
            return {
                'created': False,
                'idempotent': True,
                'plan_id': existing['id'],
                'state': existing['state'],
                'recipient_count': existing['recipient_count'],
                'requested_quantity': existing['requested_quantity'],
                'batch_id': existing['batch_id'],
                'batch_request_fingerprint': existing['batch_request_fingerprint'],
                'batch_content_type': existing['batch_content_type'],
                'batch_state': existing['batch_state'],
                'created_at': existing['created_at'],
            }

        claimed = conn.execute(f'''
            SELECT recipient_number FROM whatsapp_campaign_recipient_claims
            WHERE campaign_id = ? AND state = 'claimed' AND recipient_number IN ({placeholders})
        ''', (campaign_id, *recipients)).fetchall()
        if claimed:
            claimed_nums = [r['recipient_number'] for r in claimed]
            raise ValueError(f'Recipients already claimed by another active plan: {claimed_nums}')

        cursor = conn.execute('''
            INSERT INTO whatsapp_campaign_plans
                (plan_key, request_fingerprint, recipient_fingerprint, task_id, campaign_id,
                 sender_worker_id, content_reference, recipient_count,
                 requested_quantity, state)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'creating')
        ''', (
            plan_key, request_fingerprint, source_fingerprint, assignment['task_id'], campaign_id,
            sender_worker_id, content_reference, len(recipients), str(len(recipients)),
        ))
        plan_id = cursor.lastrowid

        conn.executemany('''
            INSERT INTO whatsapp_campaign_plan_recipients
                (plan_id, recipient_number, position, idempotency_key)
            VALUES (?, ?, ?, ?)
        ''', [
            (plan_id, recipient, pos, f'pending:{plan_id}:{pos}')
            for pos, recipient in enumerate(recipients, start=1)
        ])

        for row in conn.execute('''
            SELECT id, plan_id FROM whatsapp_campaign_plan_recipients
            WHERE plan_id = ? ORDER BY position
        ''', (plan_id,)).fetchall():
            conn.execute('''
                UPDATE whatsapp_campaign_plan_recipients
                SET idempotency_key = ? WHERE id = ?
            ''', (
                database.whatsapp_campaign_plan_recipient_idempotency_key(row['plan_id'], row['id']),
                row['id'],
            ))

        recipient_rows = conn.execute('''
            SELECT id, recipient_number, position FROM whatsapp_campaign_plan_recipients
            WHERE plan_id = ? ORDER BY position
        ''', (plan_id,)).fetchall()

        batch_id = _campaign_batch_id(plan_id)
        batch_fingerprint = _campaign_batch_fingerprint(
            batch_id, sender_worker_id, campaign_id, assignment['task_id'],
            plan_id, recipient_rows, content_reference, content_type,
        )

        conn.execute('''
            UPDATE whatsapp_campaign_plans
            SET batch_id = ?, batch_request_fingerprint = ?,
                batch_content_type = ?, batch_state = 'pending',
                batch_updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND batch_id IS NULL
        ''', (batch_id, batch_fingerprint, content_type, plan_id))

        conn.executemany('''
            INSERT INTO whatsapp_campaign_recipient_claims
                (campaign_id, task_id, recipient_number, plan_id, plan_recipient_id, state)
            VALUES (?, ?, ?, ?, ?, 'claimed')
        ''', [
            (campaign_id, assignment['task_id'], row['recipient_number'], plan_id, row['id'])
            for row in recipient_rows
        ])

        conn.execute('''
            UPDATE whatsapp_campaign_plans SET state = 'ready' WHERE id = ? AND state = 'creating'
        ''', (plan_id,))

        if conn.execute('SELECT changes() AS count').fetchone()['count'] != 1:
            raise RuntimeError('Unable to finalize assignment send plan.')
        conn.commit()

        return {
            'created': True,
            'idempotent': False,
            'plan_id': plan_id,
            'state': 'ready',
            'recipient_count': len(recipients),
            'requested_quantity': str(len(recipients)),
            'batch_id': batch_id,
            'batch_request_fingerprint': batch_fingerprint,
            'batch_content_type': content_type,
            'batch_state': 'pending',
        }
    except Exception:
        conn.rollback()
        raise


def send_assignment_whatsapp_batch(conn, assignment_id, worker_id, task_id=None,
                                   plan_key=None, selected_recipients=None,
                                   node_call=None, now=None):
    """Execute reservation and sending for an assignment-bound WhatsApp batch.

    Validates assignment ownership, task binding, status, expiry, content, and allocations.
    Uses reservation first, recovering uncertain reservation by status lookup.
    Uses Node send endpoint only after confirmed reservation.
    Recovers uncertain send outcomes by status lookup without blind retries.
    """
    if not WHATSAPP_CONSENT_GATE_AVAILABLE:
        return {
            'success': False,
            'status': 'consent_required',
            'recovery_required': False,
            'delivered': False,
            'message': 'WhatsApp sending is unavailable until consent and opt-out enforcement is implemented.',
        }
    if isinstance(worker_id, bool) or not isinstance(worker_id, int) or worker_id < 1:
        raise PermissionError('Authenticated worker is required.')
    if isinstance(assignment_id, bool) or not isinstance(assignment_id, int) or assignment_id < 1:
        raise ValueError('A valid assignment identity is required.')

    plan_key = plan_key or f"assignment-send-{assignment_id}"
    if not isinstance(plan_key, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,199}', plan_key):
        raise ValueError('A valid idempotency key is required.')

    # Verify task_id matches if provided
    if task_id is not None:
        if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id < 1:
            raise ValueError('A valid task identity is required.')
        assignment_check = conn.execute(
            'SELECT task_id, user_id FROM task_assignments WHERE id = ?', (assignment_id,)
        ).fetchone()
        if not assignment_check:
            raise ValueError('Assignment not found.')
        if assignment_check['user_id'] != worker_id:
            raise PermissionError('Assignment does not belong to the authenticated worker.')
        if assignment_check['task_id'] != task_id:
            raise ValueError('Assignment does not match requested task.')

    # Create or retrieve assignment plan
    plan = create_assignment_send_plan(
        conn, assignment_id, worker_id, plan_key,
        selected_recipients=selected_recipients,
    )

    # Ensure session contains authenticated worker ID for internal node helpers
    try:
        if session.get('user_id') != worker_id:
            session['user_id'] = worker_id
    except RuntimeError:
        pass

    # Check quota if not already reserved
    if plan['batch_state'] not in {'reserved'}:
        quota = _worker_whatsapp_quota_status(worker_id)
        if quota.get('status') != 'available':
            return {
                'success': False,
                'status': 'quota_unavailable',
                'message': 'Daily WhatsApp quota is currently unavailable. Please try again later.',
            }
        if plan['recipient_count'] > quota.get('remaining', 0):
            return {
                'success': False,
                'status': 'quota_exceeded',
                'message': 'The requested quantity exceeds the remaining daily quota.',
            }

    # Reserve with Node
    content_type = plan['batch_content_type'] or 'text'
    reservation = reserve_campaign_send_plan_with_node(
        conn, plan['plan_id'], node_call=node_call, now=now, content_type=content_type,
    )
    if reservation.get('status') == 'failed':
        failure_code = reservation.get('failure_code')
        if failure_code == 'QUOTA_EXCEEDED':
            return {
                'success': False,
                'status': 'quota_exceeded',
                'message': 'The daily WhatsApp quota was exhausted before reservation completed.',
            }
        return {
            'success': False,
            'status': 'reservation_failed',
            'message': 'Campaign batch reservation failed.',
        }

    if reservation.get('status') != 'reserved':
        # Uncertain reservation: attempt recovery lookup
        recovery = reserve_campaign_send_plan_with_node(
            conn, plan['plan_id'], node_call=node_call, now=now, content_type=content_type,
        )
        if recovery.get('status') != 'reserved':
            return {
                'success': False,
                'status': 'recovery_required',
                'recovery_required': True,
                'delivered': False,
                'message': 'Reservation status is uncertain. No message was sent; please retry recovery later.',
            }

    # Send reserved batch
    send_result = send_reserved_whatsapp_campaign_batch(
        conn, plan['plan_id'], worker_id, node_call=node_call, now=now,
    )

    if send_result.get('status') == 'recovery_required':
        # If uncertain, query status lookup via send_reserved_whatsapp_campaign_batch again
        recovered = send_reserved_whatsapp_campaign_batch(
            conn, plan['plan_id'], worker_id, node_call=node_call, now=now,
        )
        return {
            'success': False if recovered.get('status') == 'recovery_required' else True,
            'status': recovered.get('status'),
            'recovery_required': recovered.get('recovery_required', False),
            'delivered': False,
            **recovered,
        }

    is_success = send_result.get('status') in {'acknowledged', 'completed_with_failures', 'already_processed'}
    return {
        'success': is_success,
        'status': send_result.get('status'),
        'delivered': False,
        **send_result,
    }

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
    count = recipient_count if recipient_count is not None else (task['recipients_per_slot'] or len(task_recipient_pool(task, allow_legacy=True)))
    return float(paise_to_inr(calculate_task_payout_paise(conn, task, count)))

def calculate_task_payout_paise(conn, task, recipient_count=None):
    count = recipient_count if recipient_count is not None else (task['recipients_per_slot'] or len(task_recipient_pool(task, allow_legacy=True)))
    rate_paise = task.get('reward_per_number_paise') if isinstance(task, dict) else None
    if rate_paise is None and hasattr(task, 'keys') and 'reward_per_number_paise' in task.keys():
        rate_paise = task['reward_per_number_paise']
    if rate_paise is None:
        rate_paise = inr_to_paise(get_setting_decimal(conn, 'per_message_rate', '3'))
    rate_paise = int(rate_paise)
    return rate_paise * int(count)

def approval_threshold(required_count):
    """Return the minimum positive count required for approval."""
    required = max(0, int(required_count or 0))
    return 1 if required > 0 else 0

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

def _target_record_lookup_key(value):
    """Return the storage key used by target_number_records.

    Target records predate the public E.164 recipient contract and store a
    digit-only lookup key.  Convert valid E.164 input to that key, while also
    accepting an already-canonical persisted key for legacy callers.  This is
    a record-selection helper only; it does not authorize or send to a
    recipient, and it deliberately does not guess a country code.
    """
    raw = str(value or '').strip()
    if not raw:
        return ''
    canonical = normalize_phone_number(raw)
    if canonical:
        return database.normalize_legacy_target_number(canonical)
    digits = database.normalize_legacy_target_number(raw)
    if raw == digits and 8 <= len(digits) <= 15:
        return digits
    return ''


def get_reusable_target_numbers(conn, campaign_id=None, candidate_numbers=None, user_id=None, limit=None):
    """Return deterministic remaining targets that can be assigned again.

    This function only selects numbers. The caller must be inside the existing
    BEGIN IMMEDIATE allocation transaction before inserting the new assignment
    and recipient_allocations rows.
    """
    candidate_set = None
    if candidate_numbers is not None:
        candidate_set = {
            lookup_key for number in candidate_numbers
            if (lookup_key := _target_record_lookup_key(number))
        }
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

def business_campaign_required(f):
    """Require a server-assigned business role for campaign mutations/views.

    Administrators retain access for existing operational workflows; ordinary
    authenticated workers do not gain business privileges merely by logging in.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_current_user()
        if not session.get('user_id'):
            return jsonify({'success': False, 'message': 'Authentication required. Please log in.'}), 401
        if not user or user['role'] not in {'business', 'admin'}:
            return jsonify({'success': False, 'message': 'Business campaign privileges required.'}), 403
        return f(*args, **kwargs)
    return decorated_function

def _whatsapp_feature_disabled_response():
    return jsonify({
        'success': False,
        'message': 'Linked-device WhatsApp integration is disabled.'
    }), 404

def _validated_whatsapp_service_url():
    """Return the approved loopback base URL or fail closed."""
    try:
        parsed = urllib.parse.urlsplit(WHATSAPP_SERVICE_URL)
        configured_port = int(os.environ.get('TASKPE_WA_SERVICE_PORT', '8765'))
        port = parsed.port
        hostname = parsed.hostname
    except (TypeError, ValueError):
        return None
    if not (1 <= configured_port <= 65535):
        return None
    if (
        parsed.scheme != 'http'
        or hostname != '127.0.0.1'
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {'', '/'}
        or parsed.query
        or parsed.fragment
        or port != configured_port
    ):
        return None
    return f'http://127.0.0.1:{configured_port}'

def _call_whatsapp_service(method, path, user_id, payload=None, extra_headers=None, allow_when_disabled=False):
    """Call the localhost-only prototype service with an authenticated HMAC request."""
    if not WHATSAPP_LINKED_DEVICE_ENABLED and WHATSAPP_TEST_TRANSPORT_ENABLED and not allow_when_disabled:
        return {'success': False, 'message': 'A test WhatsApp transport must be injected.'}, 503
    if not whatsapp_feature_enabled() and not allow_when_disabled:
        return {'success': False, 'message': 'Linked-device WhatsApp integration is disabled.'}, 404
    if not WHATSAPP_SERVICE_SHARED_SECRET:
        logger.error('WhatsApp service secret is not configured.')
        return {'success': False, 'message': 'WhatsApp service is not configured.'}, 503
    service_base_url = _validated_whatsapp_service_url()
    if not service_base_url:
        logger.error('WhatsApp service URL failed local allowlist validation.')
        return {'success': False, 'message': 'WhatsApp service is unavailable.'}, 503

    method = method.upper()
    body_text = json.dumps(payload, separators=(',', ':')) if payload is not None else ''
    timestamp = str(int(time.time()))
    request_id = secrets.token_urlsafe(32)
    signing_value = f'{timestamp}.{request_id}.{method}.{path}.{body_text}'.encode('utf-8')
    signature = hmac.new(
        WHATSAPP_SERVICE_SHARED_SECRET.encode('utf-8'),
        signing_value,
        hashlib.sha256,
    ).hexdigest()
    request_data = body_text.encode('utf-8') if body_text else None
    headers = {
        'Accept': 'application/json',
        'X-TaskPe-Timestamp': timestamp,
        'X-TaskPe-Request-ID': request_id,
        'X-TaskPe-Signature': signature,
        'X-TaskPe-User-ID': str(user_id),
        **({'Content-Type': 'application/json'} if request_data else {}),
        **(extra_headers or {}),
    }
    service_request = urllib.request.Request(
        f'{service_base_url}{path}',
        data=request_data,
        method=method,
        headers=headers,
    )
    try:
        with _WHATSAPP_SERVICE_OPENER.open(service_request, timeout=WHATSAPP_SERVICE_TIMEOUT_SECONDS) as response:
            raw = response.read(256 * 1024).decode('utf-8', errors='replace')
            status = int(getattr(response, 'status', response.getcode()))
        result = json.loads(raw) if raw else {'success': status < 400}
        if not isinstance(result, dict):
            result = {'success': False, 'message': 'Invalid WhatsApp service response.'}
        return result, status
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(256 * 1024).decode('utf-8', errors='replace')
            result = json.loads(raw) if raw else {}
        except (OSError, ValueError):
            result = {}
        if not isinstance(result, dict):
            result = {}
        result.setdefault('success', False)
        result.setdefault('message', 'WhatsApp service rejected the request.')
        return result, int(exc.code)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        logger.warning(
            'WhatsApp service request failed: method=%s path=%s error=%s',
            method, path, type(exc).__name__,
        )
        return {'success': False, 'message': 'WhatsApp service is unavailable.'}, 503

def _require_whatsapp_csrf():
    """Require the existing session CSRF token for linked-device mutations."""
    expected = session.get('_csrf_token')
    supplied = request.headers.get('X-CSRF-Token')
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        return jsonify({'success': False, 'message': 'Invalid CSRF token.'}), 403
    return None

@app.route('/api/whatsapp/opt-outs', methods=['POST'])
@admin_required
def register_whatsapp_opt_out():
    """Register an admin/support platform-wide opt-out through Node first.

    There is no business-recipient ownership relation in the current schema,
    so ordinary users cannot submit this global suppression. The Flask table is
    only a compatibility mirror and is written after Node durably accepts it.
    """
    csrf_error = _require_whatsapp_csrf()
    if csrf_error:
        return csrf_error
    user = get_current_user()
    if not user:
        return jsonify({'success': False, 'message': 'Authentication required.'}), 401
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({'success': False, 'message': 'Invalid opt-out request.'}), 400
    try:
        phone_number = database.normalize_recipient_number(data.get('phone_number'), region=data.get('region'))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'A valid international phone number is required.'}), 400
    reason = data.get('reason', 'user_opt_out')
    request_id = data.get('request_id') or f"optout-{secrets.token_urlsafe(24)}"
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,199}', request_id):
        return jsonify({'success': False, 'message': 'Invalid opt-out request ID.'}), 400
    if not isinstance(reason, str) or not 1 <= len(reason) <= 200 or any(ord(ch) < 32 or ord(ch) == 127 for ch in reason):
        return jsonify({'success': False, 'message': 'Invalid opt-out reason.'}), 400

    conn = database.get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        existing = conn.execute(
            'SELECT request_id, actor_user_id, phone_number, reason, state FROM whatsapp_opt_out_operations WHERE request_id = ?',
            (request_id,),
        ).fetchone()
        if existing:
            if (existing['actor_user_id'] != user['id'] or existing['phone_number'] != phone_number or existing['reason'] != reason):
                conn.rollback()
                return jsonify({'success': False, 'message': 'Opt-out request conflicts with an existing request.'}), 409
            if existing['state'] == 'applied':
                conn.rollback()
                return jsonify({'success': True, 'status': 'already_applied', 'request_id': request_id}), 200
        else:
            conn.execute('''
                INSERT INTO whatsapp_opt_out_operations
                    (request_id, actor_user_id, phone_number, reason, state)
                VALUES (?, ?, ?, ?, 'pending')
            ''', (request_id, user['id'], phone_number, reason))
        conn.commit()
    except Exception:
        conn.rollback()
        return jsonify({'success': False, 'message': 'Opt-out request could not be recorded.'}), 503
    finally:
        conn.close()

    result, status = _call_whatsapp_service(
        'POST', f'/v1/users/{user["id"]}/coordination/opt-outs', user['id'], {
            'requestId': request_id,
            'recipientE164': phone_number,
            'actorPrincipalId': user['id'],
            'scope': 'platform',
            'reason': reason,
        }, allow_when_disabled=True,
    )
    node_confirmed = status == 200 and isinstance(result, dict) and result.get('success') is True and result.get('request_id') == request_id
    conn = database.get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        if not node_confirmed:
            conn.execute("UPDATE whatsapp_opt_out_operations SET state = ?, updated_at = CURRENT_TIMESTAMP WHERE request_id = ? AND actor_user_id = ?", ('unknown' if status >= 500 else 'failed', request_id, user['id']))
            conn.commit()
            return jsonify({'success': False, 'status': 'recovery_required' if status >= 500 else 'rejected', 'request_id': request_id, 'message': 'Opt-out coordination is unavailable.' if status >= 500 else 'Opt-out was not accepted.'}), (503 if status >= 500 else 409)
        database.record_whatsapp_opt_out(conn, phone_number, reason=reason, region=None)
        updated = conn.execute("UPDATE whatsapp_opt_out_operations SET state = 'applied', updated_at = CURRENT_TIMESTAMP WHERE request_id = ? AND actor_user_id = ?", (request_id, user['id']))
        if updated.rowcount != 1:
            raise RuntimeError('Opt-out operation ownership changed.')
        conn.commit()
    except Exception:
        conn.rollback()
        return jsonify({'success': False, 'status': 'recovery_required', 'request_id': request_id, 'message': 'Opt-out requires recovery.'}), 503
    finally:
        conn.close()
    return jsonify({'success': True, 'status': 'applied', 'request_id': request_id}), 200

@app.route('/api/admin/whatsapp/legacy-opt-outs/<int:quarantine_id>/resolve', methods=['POST'])
@admin_required
def resolve_whatsapp_legacy_opt_out(quarantine_id):
    """Resolve legacy quarantine only by retaining a reviewed suppression."""
    csrf_error = _require_whatsapp_csrf()
    if csrf_error:
        return csrf_error
    user = get_current_user()
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict) or data.get('decision') != 'suppress':
        return jsonify({'success': False, 'message': 'A suppression decision is required.'}), 400
    resolution_id = data.get('resolution_id')
    if not isinstance(resolution_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,199}', resolution_id):
        return jsonify({'success': False, 'message': 'Invalid resolution ID.'}), 400
    reason = data.get('reason', 'legacy_opt_out_reviewed')
    if not isinstance(reason, str) or not 1 <= len(reason) <= 200 or any(ord(ch) < 32 or ord(ch) == 127 for ch in reason):
        return jsonify({'success': False, 'message': 'Invalid resolution reason.'}), 400
    try:
        recipient_e164 = database.normalize_recipient_number(data.get('recipient_e164'), region=data.get('region'))
        approved_mapping = database.normalize_recipient_number(data.get('approved_mapping'), region=data.get('region')) if data.get('approved_mapping') else None
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'A valid international recipient number is required.'}), 400
    result, status = _call_whatsapp_service(
        'POST', f'/v1/users/{user["id"]}/coordination/legacy-opt-outs/{quarantine_id}/resolve', user['id'], {
            'quarantineId': quarantine_id,
            'resolutionId': resolution_id,
            'reviewerPrincipalId': user['id'],
            'decision': 'suppress',
            'recipientE164': recipient_e164,
            'approvedMapping': approved_mapping,
            'reason': reason,
        }, allow_when_disabled=True,
    )
    if status != 200 or not isinstance(result, dict) or result.get('success') is not True:
        return jsonify({'success': False, 'status': 'recovery_required' if status >= 500 else 'rejected', 'message': 'Legacy opt-out resolution requires review.'}), (503 if status >= 500 else 409)
    return jsonify({'success': True, 'status': result.get('status'), 'resolution_id': resolution_id}), 200

# Stage 1 only: these routes manage a user's linked-device prototype session.
# They intentionally do not send messages or change task, proof, approval, or
# payout state. The browser-provided user ID is ignored; the Flask session is
# the sole source of identity.
@app.route('/api/whatsapp/connection/qr', methods=['POST'])
@login_required
def request_whatsapp_connection_qr():
    csrf_error = _require_whatsapp_csrf()
    if csrf_error:
        return csrf_error
    if not whatsapp_feature_enabled():
        return _whatsapp_feature_disabled_response()
    user = get_current_user()
    result, status = _call_whatsapp_service(
        'POST', f'/v1/users/{user["id"]}/qr', user['id']
    )
    return jsonify(result), status

@app.route('/api/whatsapp/connection/status', methods=['GET'])
@login_required
def whatsapp_connection_status():
    if not whatsapp_feature_enabled():
        return _whatsapp_feature_disabled_response()
    user = get_current_user()
    result, status = _call_whatsapp_service(
        'GET', f'/v1/users/{user["id"]}/status', user['id']
    )
    return jsonify(result), status

@app.route('/api/whatsapp/connection/disconnect', methods=['POST'])
@login_required
def disconnect_whatsapp_connection():
    csrf_error = _require_whatsapp_csrf()
    if csrf_error:
        return csrf_error
    if not whatsapp_feature_enabled():
        return _whatsapp_feature_disabled_response()
    user = get_current_user()
    result, status = _call_whatsapp_service(
        'POST', f'/v1/users/{user["id"]}/disconnect', user['id']
    )
    return jsonify(result), status

# ----------------- WEB PAGES -----------------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/campaign-inquiries', methods=['POST'])
def create_campaign_inquiry():
    """Capture a business lead without creating or changing a TaskPe campaign."""
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({'success': False, 'message': 'Invalid campaign request.'}), 400

    limits = {
        'company_name': 160,
        'contact_person': 120,
        'email': 160,
        'phone': 40,
        'task_type': 64,
        'target_location': 160,
        'campaign_description': 4000,
        'estimated_budget': 80,
    }
    required = ('company_name', 'contact_person', 'email', 'task_type', 'campaign_description')
    text_values = {}
    for key, limit in limits.items():
        raw = data.get(key)
        if raw is None:
            value = ''
        elif isinstance(raw, (dict, list, tuple, set, bool)):
            return jsonify({'success': False, 'message': 'Invalid campaign request.'}), 400
        else:
            value = str(raw).strip()
        if len(value) > limit:
            return jsonify({'success': False, 'message': f'{key.replace("_", " ").capitalize()} is too long.'}), 400
        text_values[key] = value

    values = {key: text_values[key] for key in required}
    if any(not values[key] for key in required):
        return jsonify({'success': False, 'message': 'Please complete all required fields.'}), 400
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', values['email']):
        return jsonify({'success': False, 'message': 'Please enter a valid email address.'}), 400
    allowed_task_types = {
        'App & Website Testing', 'Customer Feedback', 'Survey / Market Research',
        'Search / Data Research', 'Local Business Research', 'Data / AI Task',
        'WhatsApp Campaign', 'Other',
    }
    if values['task_type'] not in allowed_task_types:
        return jsonify({'success': False, 'message': 'Please select a valid task type.'}), 400

    participant_count = data.get('participant_count')
    if participant_count not in (None, ''):
        try:
            if isinstance(participant_count, bool):
                raise ValueError
            participant_text = str(participant_count).strip()
            if len(participant_text) > 7:
                raise ValueError
            participant_count = int(participant_count)
            if participant_count < 0 or participant_count > 1000000:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({'success': False, 'message': 'Participant count must be between 0 and 1000000.'}), 400
    else:
        participant_count = None

    conn = database.get_db_connection()
    try:
        cursor = conn.execute('''
            INSERT INTO campaign_inquiries
                (company_name, contact_person, email, phone, task_type,
                 target_location, participant_count, campaign_description,
                 estimated_budget, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new')
        ''', (
            values['company_name'], values['contact_person'], values['email'],
            text_values['phone'] or None, values['task_type'],
            text_values['target_location'] or None, participant_count,
            values['campaign_description'], text_values['estimated_budget'] or None,
        ))
        conn.commit()
        inquiry_id = cursor.lastrowid
    finally:
        conn.close()
    return jsonify({'success': True, 'message': 'Thanks! Your campaign request has been submitted. The TaskPe team will review your requirements and contact you shortly.', 'inquiry_id': inquiry_id}), 201

def _business_recipient_input(data):
    """Return raw recipient values from the business campaign form."""
    raw = data.get('target_numbers') or data.get('recipient_numbers') or []
    if isinstance(raw, str):
        return [value.strip() for value in re.split(r'[,\n;]+', raw) if value.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(value).strip() for value in raw if str(value).strip()]
    raise ValueError('Recipient numbers must be supplied as text or a list.')


def _business_recipient_preview(values, region):
    valid = []
    invalid = []
    duplicates = []
    seen = set()
    for value in values:
        try:
            normalized = database.normalize_recipient_number(value, region=region)
        except ValueError:
            invalid.append(value)
            continue
        if normalized in seen:
            duplicates.append(value)
            continue
        seen.add(normalized)
        valid.append(normalized)
    return {
        'valid_numbers': valid,
        'invalid_numbers': invalid,
        'duplicate_numbers': duplicates,
        'valid_count': len(valid),
        'invalid_count': len(invalid),
        'duplicate_count': len(duplicates),
    }


@app.route('/api/business/campaigns/recipients/preview', methods=['POST'])
@business_campaign_required
def preview_business_campaign_recipients():
    csrf_error = _require_whatsapp_csrf()
    if csrf_error:
        return csrf_error
    data = request.get_json(silent=True) or {}
    try:
        region = database.validate_phone_region(data.get('recipient_region') or data.get('phone_region'))
        values = _business_recipient_input(data)
        if not values:
            raise ValueError('At least one recipient number is required.')
        preview = _business_recipient_preview(values, region)
    except ValueError as exc:
        return jsonify({'success': False, 'message': str(exc)}), 400
    return jsonify({'success': True, 'preview': preview, 'advisory': True})


@app.route('/api/business/campaigns', methods=['GET'])
@business_campaign_required
def list_business_campaigns():
    user = get_current_user()
    conn = database.get_db_connection()
    try:
        rows = conn.execute('''
            SELECT id, campaign_id, title, task_type, status, created_at,
                   target_numbers, recipient_region
            FROM tasks WHERE owner_user_id = ? ORDER BY id DESC
        ''', (user['id'],)).fetchall()
        campaigns = []
        for row in rows:
            recipients = json.loads(row['target_numbers'])
            declaration = database.get_whatsapp_campaign_consent_declaration(
                conn, row['id'], row['campaign_id'] or row['id'])
            campaigns.append({
                'task_id': row['id'],
                'campaign_id': row['campaign_id'] or row['id'],
                'title': row['title'],
                'task_type': row['task_type'],
                'status': row['status'],
                'review_status': row['campaign_review_status'],
                'recipient_count': len(recipients) if isinstance(recipients, list) else 0,
                'consent_status': 'recorded' if declaration else 'missing',
                'created_at': row['created_at'],
            })
        return jsonify({'success': True, 'campaigns': campaigns})
    finally:
        conn.close()


@app.route('/api/admin/business-campaigns', methods=['GET'])
@admin_required
def admin_list_business_campaigns():
    conn = database.get_db_connection()
    try:
        rows = conn.execute('''
            SELECT t.id, t.campaign_id, t.title, t.description, t.task_type,
                   t.target_numbers, t.recipient_region, t.status,
                   t.campaign_review_status, t.campaign_approved_by,
                   t.campaign_approved_at, t.created_at, t.owner_user_id,
                   u.name AS owner_name, u.email AS owner_email
            FROM tasks t LEFT JOIN users u ON u.id = t.owner_user_id
            WHERE t.owner_user_id IS NOT NULL
            ORDER BY CASE WHEN t.campaign_review_status = 'pending_review' THEN 0 ELSE 1 END,
                     t.id DESC
        ''').fetchall()
        result = []
        for row in rows:
            recipients = json.loads(row['target_numbers'])
            declaration = database.get_whatsapp_campaign_consent_declaration(
                conn, row['id'], row['campaign_id'] or row['id'])
            result.append({
                'task_id': row['id'], 'campaign_id': row['campaign_id'] or row['id'],
                'title': row['title'], 'description': row['description'],
                'task_type': row['task_type'], 'recipient_region': row['recipient_region'],
                'recipient_count': len(recipients), 'recipients': recipients,
                'status': row['status'], 'review_status': row['campaign_review_status'],
                'owner_user_id': row['owner_user_id'], 'owner_name': row['owner_name'],
                'owner_email': row['owner_email'], 'created_at': row['created_at'],
                'approved_by': row['campaign_approved_by'],
                'approved_at': row['campaign_approved_at'],
                'consent_declaration': declaration['declaration_text'] if declaration else None,
                'consent_fingerprint': declaration['recipient_fingerprint'] if declaration else None,
            })
        return jsonify({'success': True, 'campaigns': result})
    finally:
        conn.close()


@app.route('/api/admin/business-campaigns/<int:task_id>/approve', methods=['POST'])
@admin_required
def admin_approve_business_campaign(task_id):
    csrf_error = _require_whatsapp_csrf()
    if csrf_error:
        return csrf_error
    admin = get_current_user()
    conn = database.get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        task = conn.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if not task or task['owner_user_id'] is None:
            conn.rollback()
            return jsonify({'success': False, 'message': 'Business campaign not found.'}), 404
        recipients = task_recipient_pool(task)
        declaration = database.get_whatsapp_campaign_consent_declaration(
            conn, task_id, task['campaign_id'] or task_id)
        fingerprint = _campaign_recipient_fingerprint(recipients)
        if (not declaration or
                (task['owner_user_id'] is not None and declaration['business_user_id'] != task['owner_user_id'])
                or declaration['campaign_id'] != (task['campaign_id'] or task['id'])
                or declaration['declaration_text'] != database.SHOP_CONSENT_DECLARATION_DEFAULT_TEXT
                or declaration['recipient_count'] != len(recipients)
                or declaration['recipient_fingerprint'] != fingerprint):
            conn.rollback()
            return jsonify({'success': False, 'message': 'Recipient list changed or consent declaration is invalid; a fresh declaration is required.'}), 409
        if task['campaign_review_status'] == 'approved':
            if task['campaign_approval_fingerprint'] != fingerprint:
                conn.rollback()
                return jsonify({'success': False, 'message': 'Approved campaign recipients changed; re-submit a fresh declaration.'}), 409
            conn.rollback()
            return jsonify({'success': True, 'idempotent': True, 'status': 'approved', 'task_id': task_id})
        cursor = conn.execute('''
            UPDATE tasks
            SET status = 'active', campaign_review_status = 'approved', campaign_approved_by = ?,
                campaign_approved_at = CURRENT_TIMESTAMP, campaign_approval_fingerprint = ?
            WHERE id = ? AND owner_user_id IS NOT NULL AND campaign_review_status = 'pending_review'
        ''', (admin['id'], fingerprint, task_id))
        if cursor.rowcount != 1:
            conn.rollback()
            return jsonify({'success': False, 'message': 'Campaign approval state changed; refresh and retry.'}), 409
        conn.commit()
        return jsonify({'success': True, 'status': 'approved', 'task_id': task_id,
                        'message': 'Business campaign approved and available to workers.'})
    except (ValueError, json.JSONDecodeError):
        conn.rollback()
        return jsonify({'success': False, 'message': 'Campaign recipient snapshot is invalid.'}), 409
    finally:
        conn.close()


@app.route('/api/business/campaigns', methods=['POST'])
@business_campaign_required
def create_business_campaign():
    csrf_error = _require_whatsapp_csrf()
    if csrf_error:
        return csrf_error
    user = get_current_user()
    is_multipart = request.mimetype == 'multipart/form-data'
    data = request.form.to_dict() if not request.is_json else (request.get_json(silent=True) or {})
    try:
        title = str(data.get('title') or '').strip()
        template_text = str(data.get('template_text') or data.get('message') or '').strip()
        description = str(data.get('description') or template_text).strip()
        task_type = str(data.get('task_type') or 'TEXT_LINK').strip().upper()
        region = database.validate_phone_region(data.get('recipient_region') or data.get('phone_region'))
        values = _business_recipient_input(data)
        preview = _business_recipient_preview(values, region)
        if not title or not template_text or not description:
            raise ValueError('Campaign title, description, and message text are required.')
        if task_type not in AUTOMATED_WHATSAPP_TASK_TYPES:
            raise ValueError('Unsupported campaign content type.')
        if preview['invalid_count']:
            raise ValueError('Every recipient must be a valid international number or an unambiguous number for the selected region.')
        if not preview['valid_numbers']:
            raise ValueError('At least one valid recipient number is required.')
        if str(data.get('consent_declaration_confirmed', data.get('counter_declaration_confirmed', ''))).strip().lower() not in {'1', 'true', 'yes', 'on'}:
            raise ValueError('The business consent declaration is required.')
        submitted_text = data.get('consent_declaration_text')
        if submitted_text is not None and str(submitted_text).strip() != database.SHOP_CONSENT_DECLARATION_DEFAULT_TEXT:
            raise ValueError('The approved business declaration text is required.')
        destination_link = normalize_task_destination(data.get('destination_link'))
        if not destination_link or not destination_link.lower().startswith('https://'):
            raise ValueError('A valid HTTPS destination link is required.')
        image_file = request.files.get('campaign_image') if is_multipart else None
        image_path = None
        if task_type == 'IMAGE_TEXT_LINK':
            if not image_file or not image_file.filename or secure_filename(image_file.filename) != image_file.filename:
                raise ValueError('A campaign image is required for image campaigns.')
            extension = image_file.filename.rsplit('.', 1)[-1].lower() if '.' in image_file.filename else ''
            if extension not in {'png', 'jpg', 'jpeg', 'webp'} or not valid_image_content(image_file):
                raise ValueError('Upload a valid PNG, JPG, JPEG, or WEBP campaign image.')
            image_path = f'campaign_{secrets.token_hex(16)}.{extension}'
            image_file.save(os.path.join(app.config['TASK_ASSET_FOLDER'], image_path))
        conn = database.get_db_connection()
        try:
            rate_paise = inr_to_paise(get_setting_decimal(conn, 'per_message_rate', '3'))
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO tasks
                    (title, category, description, payout, payout_paise, template_text,
                     target_numbers, recipient_region, campaign_id, owner_user_id,
                     recipients_per_slot, time_limit_minutes, proof_instructions,
                     total_slots, completed_slots, status, campaign_review_status, task_type, image_path,
                     destination_link, reward_per_number_paise, reward_source)
                 VALUES (?, 'Direct Message', ?, ?, ?, ?, ?, ?, NULL, ?, ?, 15, ?, 1, 0, 'pending_review', 'pending_review', ?, ?, ?, ?, 'business_at_creation')
            ''', (title, description, float(paise_to_inr(rate_paise * len(preview['valid_numbers']))),
                  rate_paise * len(preview['valid_numbers']), template_text,
                  json.dumps(preview['valid_numbers']), region, user['id'],
                  len(preview['valid_numbers']), 'Upload screenshot showing sent message.',
                  task_type, image_path, destination_link, rate_paise))
            task_id = cursor.lastrowid
            cursor.execute('UPDATE tasks SET campaign_id = ? WHERE id = ?', (task_id, task_id))
            database.create_whatsapp_campaign_consent_declaration(
                conn, task_id=task_id, campaign_id=task_id,
                business_user_id=user['id'], phone_numbers=preview['valid_numbers'],
                declaration_text=database.SHOP_CONSENT_DECLARATION_DEFAULT_TEXT,
                region=region,
            )
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
    except ValueError as exc:
        return jsonify({'success': False, 'message': str(exc)}), 400
    except Exception:
        app.logger.exception('Unable to create business campaign.')
        return jsonify({'success': False, 'message': 'Campaign creation is unavailable.'}), 503
    return jsonify({
        'success': True,
        'status': 'pending_review',
        'review_status': 'pending_review',
        'task_id': task_id,
        'campaign_id': task_id,
        'recipient_count': len(preview['valid_numbers']),
        'duplicate_count': preview['duplicate_count'],
        'consent_status': 'recorded',
        'sending_enabled': False,
        'message': 'Campaign created and queued for administrator approval. Automated sending remains disabled.',
    }), 201


@app.route('/api/admin/campaign-inquiries', methods=['GET'])
@admin_required
def admin_campaign_inquiries():
    conn = database.get_db_connection()
    try:
        rows = conn.execute('''
            SELECT id, company_name, contact_person, email, phone, task_type,
                   target_location, participant_count, campaign_description,
                   estimated_budget, created_at, status
            FROM campaign_inquiries
            ORDER BY created_at DESC, id DESC
        ''').fetchall()
    finally:
        conn.close()
    return jsonify({'success': True, 'inquiries': [dict(row) for row in rows]})

CAMPAIGN_INQUIRY_STATUSES = ('new', 'contacted', 'negotiating', 'approved', 'active', 'completed', 'closed')
CAMPAIGN_INQUIRY_STATUS_SET = set(CAMPAIGN_INQUIRY_STATUSES)
CAMPAIGN_INQUIRY_NOTES_LIMIT = 4000

@app.route('/api/admin/campaign-inquiries/<int:inquiry_id>', methods=['GET'])
@admin_required
def admin_campaign_inquiry_detail(inquiry_id):
    conn = database.get_db_connection()
    try:
        row = conn.execute('''
            SELECT id, company_name, contact_person, email, phone, task_type,
                   target_location, participant_count, campaign_description,
                   estimated_budget, created_at, status, admin_notes
            FROM campaign_inquiries
            WHERE id = ?
        ''', (inquiry_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({'success': False, 'message': 'Campaign request not found.'}), 404
    return jsonify({'success': True, 'inquiry': dict(row)})

@app.route('/api/admin/campaign-inquiries/<int:inquiry_id>/status', methods=['PATCH'])
@app.route('/api/admin/campaign-inquiries/<int:inquiry_id>', methods=['PATCH'])
@admin_required
def update_campaign_inquiry_status(inquiry_id):
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({'success': False, 'message': 'Invalid campaign request update.'}), 400
    raw_status = data.get('status')
    status = str(raw_status or '').strip().lower()
    if status not in CAMPAIGN_INQUIRY_STATUS_SET:
        return jsonify({'success': False, 'message': 'Invalid campaign request status.'}), 400
    raw_notes = data.get('admin_notes', '')
    if raw_notes is None:
        raw_notes = ''
    if isinstance(raw_notes, (dict, list, tuple, set, bool)):
        return jsonify({'success': False, 'message': 'Invalid admin notes.'}), 400
    admin_notes = str(raw_notes).strip()
    if len(admin_notes) > CAMPAIGN_INQUIRY_NOTES_LIMIT:
        return jsonify({'success': False, 'message': f'Admin notes must be {CAMPAIGN_INQUIRY_NOTES_LIMIT} characters or fewer.'}), 400
    conn = database.get_db_connection()
    try:
        cursor = conn.execute(
            'UPDATE campaign_inquiries SET status = ?, admin_notes = ? WHERE id = ?',
            (status, admin_notes or None, inquiry_id),
        )
        conn.commit()
    finally:
        conn.close()
    if cursor.rowcount == 0:
        return jsonify({'success': False, 'message': 'Campaign request not found.'}), 404
    return jsonify({'success': True, 'message': 'Campaign request status updated.'})

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
    raw_mobile = database.normalize_legacy_target_number(data.get('mobile') or '')
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
AUTOMATED_WHATSAPP_TASK_TYPES = frozenset({'TEXT_LINK', 'IMAGE_TEXT_LINK'})
AUTOMATED_WHATSAPP_QUANTITY_CHOICES = (20, 50, 80)
WHATSAPP_CONSENT_GATE_AVAILABLE = os.environ.get(
    'TASKPE_WHATSAPP_CONSENT_GATE_ENABLED', '0'
).strip().lower() in {'1', 'true', 'yes'}

def _worker_whatsapp_quota_status(worker_id):
    """Read authoritative Node quota without creating or reserving anything."""
    response, status = _call_whatsapp_service(
        'GET', f'/v1/users/{worker_id}/quota', worker_id
    )
    if status != 200 or not isinstance(response, dict) or response.get('success') is not True:
        return {'status': 'unavailable'}
    required = ('worker_id', 'quota_day', 'limit', 'reserved', 'remaining', 'timezone')
    if any(field not in response for field in required):
        return {'status': 'unavailable'}
    if response['worker_id'] != worker_id or response['timezone'] != 'Asia/Kolkata':
        return {'status': 'unavailable'}
    if any(isinstance(response[field], bool) or not isinstance(response[field], int)
           for field in ('limit', 'reserved', 'remaining')):
        return {'status': 'unavailable'}
    if response['limit'] < 0 or response['reserved'] < 0 or response['remaining'] < 0:
        return {'status': 'unavailable'}
    return {
        'status': 'available',
        'quota_day': response['quota_day'],
        'limit': response['limit'],
        'reserved': response['reserved'],
        'remaining': response['remaining'],
        'timezone': response['timezone'],
    }

@app.route('/api/whatsapp/campaigns/eligible', methods=['GET'])
@login_required
def get_eligible_whatsapp_campaigns():
    """Return advisory campaign availability; never creates plans or claims."""
    if not whatsapp_feature_enabled():
        return _whatsapp_feature_disabled_response()
    user = get_current_user()
    quota = _worker_whatsapp_quota_status(user['id'])
    conn = database.get_db_connection()
    try:
        tasks = conn.execute('''
            SELECT * FROM tasks
            WHERE status = 'active' AND task_type IN (?, ?)
            ORDER BY id DESC
        ''', tuple(AUTOMATED_WHATSAPP_TASK_TYPES)).fetchall()
        campaigns = []
        for task in tasks:
            if not business_campaign_is_approved(conn, task):
                continue
            eligibility = build_recipient_eligibility(conn, task)
            eligible_count = eligibility['eligible_recipient_count']
            quota_remaining = quota.get('remaining') if quota['status'] == 'available' else None
            maximum = min(MAX_CAMPAIGN_PLAN_RECIPIENTS, eligible_count, quota_remaining) if quota_remaining is not None else None
            choices = [choice for choice in AUTOMATED_WHATSAPP_QUANTITY_CHOICES if maximum is not None and choice <= maximum]
            if maximum is not None and maximum > 0 and maximum not in choices:
                choices.append(maximum)
            campaigns.append({
                'task_id': task['id'],
                'campaign_id': task['campaign_id'] or task['id'],
                'title': task['title'],
                'task_type': task['task_type'],
                'source_recipient_count': eligibility['source_recipient_count'],
                'eligible_recipient_count': eligible_count,
                'exclusion_counts': eligibility['exclusion_counts'],
                'max_selectable_quantity': maximum,
                'quantity_choices': sorted(set(choices)),
                'availability': 'available' if maximum and maximum > 0 else ('quota_unavailable' if quota['status'] != 'available' else 'no_eligible_recipients'),
            })
        return jsonify({
            'success': True,
            'advisory': True,
            'consent_opt_out_enforced': True,
            'quota': quota,
            'campaigns': campaigns,
        })
    except Exception:
        app.logger.exception('Unable to read WhatsApp campaign availability.')
        return jsonify({'success': False, 'message': 'Campaign availability is unavailable.'}), 503
    finally:
        conn.close()

@app.route('/api/whatsapp/campaigns/prepare', methods=['POST'])
@login_required
def prepare_whatsapp_campaign():
    """Create or reserve a worker-owned campaign plan without sending."""
    csrf_error = _require_whatsapp_csrf()
    if csrf_error:
        return csrf_error
    if not whatsapp_feature_enabled():
        return _whatsapp_feature_disabled_response()

    user = get_current_user()
    if not user:
        return jsonify({'success': False, 'message': 'Authentication required. Please log in.'}), 401
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'success': False, 'message': 'A valid campaign request is required.'}), 400

    campaign_id = data.get('campaign_id')
    quantity = data.get('quantity')
    plan_key = data.get('idempotency_key')
    if (isinstance(campaign_id, bool) or not isinstance(campaign_id, int) or campaign_id < 1 or
            (quantity != 'all' and (isinstance(quantity, bool) or not isinstance(quantity, int))) or
            not isinstance(plan_key, str) or
            not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,199}', plan_key)):
        return jsonify({'success': False, 'message': 'Invalid campaign preparation request.'}), 400

    conn = database.get_db_connection()
    try:
        existing_plan = conn.execute('''
            SELECT * FROM whatsapp_campaign_plans
            WHERE plan_key = ? AND sender_worker_id = ?
        ''', (plan_key, user['id'])).fetchone()
        key_in_use = conn.execute('''
            SELECT 1 FROM whatsapp_campaign_plans WHERE plan_key = ? LIMIT 1
        ''', (plan_key,)).fetchone()
        if key_in_use and not existing_plan:
            return jsonify({'success': False, 'message': 'Campaign preparation conflicted with another request.'}), 409
        if existing_plan:
            task = conn.execute(
                'SELECT * FROM tasks WHERE id = ?', (existing_plan['task_id'],)
            ).fetchone()
            persisted_campaign_id = task['campaign_id'] or task['id'] if task else None
            requested_quantity = 'all' if quantity == 'all' else str(quantity)
            if (not task or campaign_id != persisted_campaign_id or
                    existing_plan['requested_quantity'] != requested_quantity or
                    task['task_type'] not in AUTOMATED_WHATSAPP_TASK_TYPES):
                return jsonify({'success': False, 'message': 'Campaign preparation identity conflict.'}), 409
            if not business_campaign_is_approved(conn, task):
                return jsonify({'success': False, 'message': 'Campaign requires fresh administrator approval and consent before preparation.'}), 403
            if existing_plan['recipient_fingerprint'] != _campaign_recipient_fingerprint(task_recipient_pool(task)):
                return jsonify({'success': False, 'message': 'Campaign recipients changed; a fresh preparation is required.'}), 409
        else:
            task = conn.execute('''
                SELECT * FROM tasks
                WHERE status = 'active'
                  AND task_type IN (?, ?)
                  AND (campaign_id = ? OR (campaign_id IS NULL AND id = ?))
                ORDER BY id DESC LIMIT 1
            ''', (*AUTOMATED_WHATSAPP_TASK_TYPES, campaign_id, campaign_id)).fetchone()
            if not task:
                return jsonify({'success': False, 'message': 'Campaign is not available for automated WhatsApp preparation.'}), 404
            if not business_campaign_is_approved(conn, task):
                return jsonify({'success': False, 'message': 'Campaign requires administrator approval before preparation.'}), 403

        content_type = 'image' if task['task_type'] == 'IMAGE_TEXT_LINK' else 'text'
        content_reference = f'task:{task["id"]}:{task["task_type"].lower()}:v1'
        if existing_plan and (
                existing_plan['batch_content_type'] != content_type or
                existing_plan['content_reference'] != content_reference):
            return jsonify({'success': False, 'message': 'Campaign preparation identity conflict.'}), 409
        if not existing_plan:
            eligibility = build_recipient_eligibility(conn, task)
            try:
                selected = select_eligible_recipients(eligibility, quantity)
            except ValueError as exc:
                return jsonify({'success': False, 'message': str(exc)}), 400
            if not selected:
                return jsonify({'success': False, 'message': 'No eligible recipients are currently available.'}), 409

            quota = _worker_whatsapp_quota_status(user['id'])
            if quota['status'] != 'available':
                return jsonify({
                    'success': False,
                    'status': 'quota_unavailable',
                    'message': 'Daily WhatsApp quota is currently unavailable. Please try again later.',
                }), 503
            if len(selected) > quota['remaining']:
                return jsonify({
                    'success': False,
                    'status': 'quota_exceeded',
                    'message': 'The requested quantity exceeds the remaining daily quota.',
                }), 409

        try:
            plan = create_campaign_send_plan(
                conn, task, user['id'], plan_key, quantity,
                content_reference=content_reference, content_type=content_type,
            )
        except ValueError as exc:
            return jsonify({'success': False, 'message': 'Campaign preparation conflicted with another request.'}), 409

        result = reserve_campaign_send_plan_with_node(
            conn, plan['plan_id'], content_type=content_type,
        )
        if result.get('status') == 'reserved':
            return jsonify({
                'success': True,
                'status': 'reserved',
                'idempotent': bool(plan.get('idempotent')),
                'recipient_count': plan['recipient_count'],
            })
        if result.get('status') == 'failed' and result.get('failure_code') == 'QUOTA_EXCEEDED':
            return jsonify({
                'success': False,
                'status': 'quota_exceeded',
                'message': 'The daily WhatsApp quota was exhausted before reservation completed.',
            }), 409
        return jsonify({
            'success': False,
            'status': 'recovery_required',
            'recovery_required': True,
            'message': 'Reservation status is uncertain. No message was sent; please retry recovery later.',
        }), 202
    except PermissionError:
        return jsonify({'success': False, 'message': 'Campaign ownership validation failed.'}), 403
    except sqlite3.IntegrityError:
        return jsonify({'success': False, 'message': 'Campaign preparation conflicted with another request.'}), 409
    except Exception:
        app.logger.exception('Unable to prepare WhatsApp campaign plan.')
        return jsonify({'success': False, 'message': 'Campaign preparation is unavailable.'}), 503
    finally:
        conn.close()


@app.route('/api/whatsapp/campaigns/send', methods=['POST'])
@login_required
def send_whatsapp_campaign():
    """Fail closed until consent/opt-out enforcement exists.

    The internal dispatcher is separately testable, but this browser route must
    not create a live sending path while recipient consent is unresolved.
    """
    csrf_error = _require_whatsapp_csrf()
    if csrf_error:
        return csrf_error
    if not whatsapp_feature_enabled():
        return _whatsapp_feature_disabled_response()
    if not WHATSAPP_CONSENT_GATE_AVAILABLE:
        return jsonify({
            'success': False,
            'status': 'consent_required',
            'message': 'WhatsApp sending is unavailable until consent and opt-out enforcement is implemented.',
        }), 412
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or set(data) != {'plan_id'}:
        return jsonify({'success': False, 'message': 'A valid reserved campaign plan is required.'}), 400
    plan_id = data.get('plan_id')
    if isinstance(plan_id, bool) or not isinstance(plan_id, int) or plan_id < 1:
        return jsonify({'success': False, 'message': 'A valid reserved campaign plan is required.'}), 400
    user = get_current_user()
    conn = database.get_db_connection()
    try:
        result = send_reserved_whatsapp_campaign_batch(conn, plan_id, user['id'])
        if result.get('status') == 'consent_required':
            return jsonify({'success': False, **result}), 412
        if result.get('status') == 'recovery_required':
            return jsonify({
                'success': False, 'status': 'recovery_required',
                'recovery_required': True, 'delivered': False,
            }), 202
        return jsonify({'success': True, **result}), 200
    except PermissionError:
        return jsonify({'success': False, 'message': 'Campaign ownership validation failed.'}), 403
    except ValueError:
        return jsonify({'success': False, 'message': 'Campaign reservation is not sendable.'}), 409
    except Exception:
        app.logger.exception('Unable to send reserved WhatsApp campaign.')
        return jsonify({'success': False, 'message': 'Campaign sending is unavailable.'}), 503
    finally:
        conn.close()


@app.route('/api/tasks/<int:task_id>/assignments/<int:assignment_id>/whatsapp-send', methods=['POST'])
@app.route('/api/tasks/<int:task_id>/assignments/<int:assignment_id>/whatsapp/send', methods=['POST'])
@app.route('/api/tasks/<int:task_id>/whatsapp-send', methods=['POST'])
@app.route('/api/whatsapp/assignments/<int:assignment_id>/send', methods=['POST'])
@login_required
def send_assignment_whatsapp(task_id=None, assignment_id=None):
    csrf_error = _require_whatsapp_csrf()
    if csrf_error:
        return csrf_error
    if not whatsapp_feature_enabled():
        return _whatsapp_feature_disabled_response()
    if not WHATSAPP_CONSENT_GATE_AVAILABLE:
        return jsonify({
            'success': False,
            'status': 'consent_required',
            'message': 'WhatsApp sending is unavailable until consent and opt-out enforcement is implemented.',
        }), 412

    user = get_current_user()
    if not user:
        return jsonify({'success': False, 'message': 'Authentication required. Please log in.'}), 401

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({'success': False, 'message': 'A valid request body is required.'}), 400

    if assignment_id is None:
        assignment_id = data.get('assignment_id')
    if isinstance(assignment_id, bool) or not isinstance(assignment_id, int) or assignment_id < 1:
        return jsonify({'success': False, 'message': 'A valid assignment_id is required.'}), 400

    if task_id is None:
        task_id = data.get('task_id')
    if task_id is not None and (isinstance(task_id, bool) or not isinstance(task_id, int) or task_id < 1):
        return jsonify({'success': False, 'message': 'A valid task_id is required.'}), 400

    plan_key = data.get('idempotency_key') or f"assignment-send-{assignment_id}"
    selected_recipients = data.get('recipients')

    conn = database.get_db_connection()
    try:
        release_expired_assignments(conn)
        conn.commit()

        result = send_assignment_whatsapp_batch(
            conn, assignment_id, user['id'], task_id=task_id,
            plan_key=plan_key, selected_recipients=selected_recipients,
        )

        status_name = result.get('status')
        if status_name == 'consent_required':
            return jsonify(result), 412
        if status_name == 'recovery_required':
            return jsonify(result), 202
        if status_name in {'quota_exceeded', 'reservation_failed'}:
            return jsonify(result), 409
        if status_name in {'quota_unavailable'}:
            return jsonify(result), 503
        if result.get('success'):
            return jsonify(result), 200
        return jsonify(result), 400
    except PermissionError as exc:
        return jsonify({'success': False, 'message': str(exc)}), 403
    except ValueError as exc:
        err_msg = str(exc)
        if 'not found' in err_msg.lower():
            return jsonify({'success': False, 'message': err_msg}), 404
        return jsonify({'success': False, 'message': err_msg}), 400
    except Exception:
        app.logger.exception('Unable to send assignment WhatsApp messages.')
        return jsonify({'success': False, 'message': 'Assignment WhatsApp sending is unavailable.'}), 503
    finally:
        conn.close()


@app.route('/api/tasks', methods=['GET'])
def get_tasks():
    user_id = session.get('user_id')
    conn = database.get_db_connection()
    release_expired_assignments(conn)
    conn.commit()
    # Workers may only discover and take currently published tasks. Archived
    # and paused tasks remain available to administrators for history review.
    tasks = conn.execute("""
        SELECT * FROM tasks
        WHERE status = 'active'
          AND (owner_user_id IS NULL OR campaign_review_status = 'approved')
        ORDER BY id DESC
    """).fetchall()
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
        if not business_campaign_is_approved(conn, task):
            return jsonify({'success': False, 'message': 'This campaign is not available.'}), 404
        campaign_id = task['campaign_id'] or task['id']
        required = task['recipients_per_slot'] or len(task_recipient_pool(task, allow_legacy=True))
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
        for number in task_recipient_pool(task, allow_legacy=True):
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

@app.route('/api/tasks/<int:task_id>/recipient-audit', methods=['POST'])
@login_required
def record_recipient_audit_event(task_id):
    """Record the worker-triggered WhatsApp open for one assigned recipient."""
    data = request.get_json(silent=True) or {}
    try:
        assignment_id = int(data.get('assignment_id'))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'A valid assignment is required.'}), 400

    recipient_number = str(data.get('recipient_number') or '').strip()

    user = get_current_user()
    conn = database.get_db_connection()
    try:
        assignment = conn.execute('''
            SELECT ta.id, ta.task_id, ta.campaign_id, ta.user_id, ta.assigned_numbers,
                   t.template_text, t.destination_link, t.recipient_region
            FROM task_assignments ta
            JOIN tasks t ON t.id = ta.task_id
            WHERE ta.id = ? AND ta.task_id = ? AND ta.user_id = ?
              AND ta.status = 'in_progress'
              AND ta.expires_at IS NOT NULL
              AND ta.expires_at > CURRENT_TIMESTAMP
        ''', (assignment_id, task_id, user['id'])).fetchone()
        if not assignment:
            return jsonify({'success': False, 'message': 'Active assignment not found.'}), 404

        normalized_recipient = normalize_phone_number(
            recipient_number, region=assignment['recipient_region']
        )
        if not normalized_recipient:
            normalized_recipient = ''
        if not normalized_recipient:
            return jsonify({'success': False, 'message': 'A valid recipient number is required.'}), 400

        assigned_numbers = {
            normalize_phone_number(number, region=assignment['recipient_region']) or
            ''
            for number in get_assigned_numbers(assignment)
        }
        if normalized_recipient not in assigned_numbers:
            return jsonify({'success': False, 'message': 'Recipient is not assigned to this worker slot.'}), 403

        allocation = conn.execute('''
            SELECT id FROM recipient_allocations
            WHERE assignment_id = ? AND phone_number = ?
            LIMIT 1
        ''', (assignment_id, normalized_recipient)).fetchone()
        if not allocation:
            return jsonify({'success': False, 'message': 'Recipient allocation not found.'}), 404

        message_text = assignment['template_text'] or ''
        if assignment['destination_link'] and assignment['destination_link'] not in message_text:
            message_text = f'{message_text}\n\n{assignment["destination_link"]}'
        if len(message_text) > 10000:
            return jsonify({'success': False, 'message': 'Message template is too long.'}), 400

        target_record = conn.execute('''
            SELECT id FROM target_number_records
            WHERE assignment_id = ? AND normalized_target_number = ?
            LIMIT 1
        ''', (assignment_id, normalized_recipient)).fetchone()
        conn.execute('''
            INSERT INTO message_audit_records
                (task_id, campaign_id, assignment_id, user_id,
                 target_number_record_id, recipient_allocation_id,
                 recipient_number, normalized_recipient_number,
                 message_text, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPENED')
            ON CONFLICT(assignment_id, normalized_recipient_number) DO UPDATE SET
                message_text = CASE
                    WHEN excluded.message_text != '' THEN excluded.message_text
                    ELSE message_audit_records.message_text
                END,
                last_opened_at = CURRENT_TIMESTAMP,
                open_count = message_audit_records.open_count + 1,
                updated_at = CURRENT_TIMESTAMP
        ''', (
            assignment['task_id'], assignment['campaign_id'], assignment['id'], assignment['user_id'],
            target_record['id'] if target_record else None, allocation['id'],
            normalized_recipient, normalized_recipient, message_text,
        ))
        row = conn.execute('''
            SELECT id, status, first_opened_at, last_opened_at, open_count
            FROM message_audit_records
            WHERE assignment_id = ? AND normalized_recipient_number = ?
        ''', (assignment_id, normalized_recipient)).fetchone()
        conn.commit()
        return jsonify({
            'success': True,
            'audit_id': row['id'],
            'status': row['status'],
            'first_opened_at': row['first_opened_at'],
            'last_opened_at': row['last_opened_at'],
            'open_count': row['open_count'],
        })
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
    if task['owner_user_id'] is not None and not business_campaign_is_approved(conn, task):
        current_user = get_current_user()
        if not current_user or current_user['role'] != 'admin':
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
    task_region = task['recipient_region'] if 'recipient_region' in task.keys() else None
    def normalize_manual_target(value):
        return normalize_phone_number(value, region=task_region)
    matched_targets = {normalize_manual_target(number) for number in audit_res.get('matched_numbers', [])}
    invalid_targets = {normalize_manual_target(number) for number in audit_res.get('invalid_numbers', [])}
    target_rows = []
    for target in target_nums:
        normalized = normalize_manual_target(target)
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
    try:
        cursor.execute('''
            UPDATE message_audit_records
            SET target_number_record_id = (
                    SELECT tnr.id
                    FROM target_number_records tnr
                    WHERE tnr.assignment_id = message_audit_records.assignment_id
                      AND tnr.normalized_target_number = message_audit_records.normalized_recipient_number
                    LIMIT 1
                ),
                submission_id = ?, updated_at = CURRENT_TIMESTAMP
            WHERE assignment_id = ?
        ''', (sub_id, assignment['id']))
    except sqlite3.Error:
        logger.warning(
            'Optional message audit synchronization failed for submission %s',
            sub_id,
            exc_info=True,
        )
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

@app.route('/api/admin/audit-report.csv', methods=['GET'])
@admin_required
def admin_audit_report():
    """Export per-recipient audit attempts with existing proof and payment state."""
    conn = database.get_db_connection()
    try:
        rows = conn.execute('''
            SELECT mar.id AS audit_id, mar.task_id, mar.campaign_id,
                   mar.assignment_id, mar.user_id, mar.sender_number,
                   mar.recipient_number, mar.message_text,
                   mar.first_opened_at, mar.last_opened_at, mar.open_count,
                   mar.target_number_record_id, mar.submission_id,
                   t.title AS task_title,
                   ta.status AS assignment_status,
                   tnr.status AS target_status, tnr.verified_at,
                   tnr.paid_at AS target_paid_at,
                   s.status AS submission_status, s.submitted_at,
                   s.reviewed_at, s.audit_score, s.admin_feedback,
                   tp.amount AS paid_amount, tp.paid_at AS payment_timestamp
            FROM message_audit_records mar
            LEFT JOIN tasks t ON t.id = mar.task_id
            LEFT JOIN task_assignments ta ON ta.id = mar.assignment_id
            LEFT JOIN target_number_records tnr ON tnr.id = mar.target_number_record_id
            LEFT JOIN submissions s ON s.id = mar.submission_id
            LEFT JOIN target_payments tp ON tp.target_number_record_id = mar.target_number_record_id
            ORDER BY mar.id
        ''').fetchall()
    finally:
        conn.close()

    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow([
        'No', 'ID Data', 'BLAST ID / Campaign', 'Task ID', 'Task Title',
        'Assignment ID', 'User ID', 'Pengirim', 'Penerima', 'Teks',
        'Status', 'Alasan', 'Jam Audit Event', 'Open Count',
        'Proof/OCR Status', 'Approval Status', 'Paid Amount', 'Payment Timestamp',
    ])
    for row_number, row in enumerate(rows, start=1):
        target_status = row['target_status']
        submission_status = row['submission_status']
        paid = row['paid_amount'] is not None or target_status == 'paid'
        if paid:
            attempt_status = 'PAID'
        elif target_status == 'verified':
            attempt_status = 'VERIFIED'
        elif target_status == 'rejected':
            attempt_status = 'FAILED/UNVERIFIED'
        elif target_status == 'remaining':
            attempt_status = 'REMAINING/UNSETTLED'
        elif submission_status == 'rejected':
            attempt_status = 'PROOF_REJECTED'
        else:
            attempt_status = 'OPENED'

        if target_status in ('verified', 'paid'):
            proof_status = 'VERIFIED'
        elif target_status == 'rejected':
            proof_status = 'UNVERIFIED'
        elif target_status == 'remaining':
            proof_status = 'REMAINING/UNSETTLED'
        elif submission_status == 'rejected':
            proof_status = 'REJECTED'
        elif submission_status:
            proof_status = 'SUBMITTED'
        else:
            proof_status = 'NOT_SUBMITTED'

        if paid or target_status == 'paid':
            approval_status = 'PAID'
        elif submission_status == 'approved':
            approval_status = 'APPROVED'
        elif submission_status == 'pending':
            approval_status = 'PENDING'
        elif submission_status == 'rejected':
            approval_status = 'REJECTED'
        else:
            approval_status = 'NOT_SUBMITTED'

        if target_status == 'rejected':
            reason = 'Not verified by available proof.'
        elif target_status == 'remaining':
            reason = 'Not selected for payment; remains unsettled.'
        elif submission_status == 'rejected':
            reason = row['admin_feedback'] or 'Proof submission rejected by admin.'
        else:
            reason = ''

        writer.writerow([
            row_number,
            row['audit_id'],
            row['campaign_id'],
            row['task_id'],
            row['task_title'] or '',
            row['assignment_id'],
            row['user_id'],
            row['sender_number'] or '',
            row['recipient_number'],
            row['message_text'],
            attempt_status,
            reason,
            row['first_opened_at'],
            row['open_count'],
            proof_status,
            approval_status,
            row['paid_amount'] if row['paid_amount'] is not None else '',
            row['payment_timestamp'] or row['target_paid_at'] or '',
        ])

    response = Response(output.getvalue(), mimetype='text/csv')
    response.headers['Content-Disposition'] = 'attachment; filename=taskpe-message-audit.csv'
    return response

@app.route('/api/admin/audit-report.xlsx', methods=['GET'])
@admin_required
def admin_audit_report_xlsx():
    """Export approved submissions and their recipient audit records as XLSX."""
    conn = database.get_db_connection()
    try:
        campaign_rows = conn.execute('''
            SELECT s.id AS submission_id, s.task_id, s.assignment_id, s.user_id,
                   COALESCE(t.title, 'Task #' || s.task_id) AS task_title,
                   u.name AS worker_name, u.mobile AS worker_mobile, u.email AS worker_email,
                   COALESCE(s.required_count, s.numbers_required, 0) AS required_numbers,
                   COALESCE(s.verified_count, s.numbers_detected, 0) AS verified_numbers,
                   COALESCE(s.approved_valid_count, s.verified_count, s.numbers_detected, 0) AS approved_valid_numbers,
                   COALESCE(s.invalid_count, 0) AS invalid_numbers,
                   s.audit_score, s.audit_badge, s.payout_amount, s.payout_paise,
                   s.submitted_at, s.reviewed_at AS approved_at,
                   ta.status AS assignment_status
            FROM submissions s
            JOIN users u ON u.id = s.user_id
            LEFT JOIN tasks t ON t.id = s.task_id
            LEFT JOIN task_assignments ta ON ta.id = s.assignment_id
            WHERE s.status = 'approved'
            ORDER BY s.id
        ''').fetchall()
        recipient_rows = conn.execute('''
            SELECT mar.id AS audit_id, mar.task_id,
                   COALESCE(t.title, 'Task #' || mar.task_id) AS task_title,
                   mar.submission_id, mar.assignment_id, mar.user_id,
                   mar.sender_number, mar.recipient_number, mar.message_text,
                   mar.open_count, mar.first_opened_at, mar.last_opened_at,
                   tnr.status AS target_status, tnr.verified_at,
                   tnr.paid_at AS target_paid_at,
                   tp.amount AS paid_amount, tp.paid_at AS payment_timestamp
            FROM message_audit_records mar
            JOIN submissions s ON s.id = mar.submission_id AND s.status = 'approved'
            LEFT JOIN tasks t ON t.id = mar.task_id
            LEFT JOIN target_number_records tnr ON tnr.id = mar.target_number_record_id
            LEFT JOIN target_payments tp ON tp.target_number_record_id = mar.target_number_record_id
            ORDER BY mar.id
        ''').fetchall()
    finally:
        conn.close()

    workbook = Workbook()
    campaigns = workbook.active
    campaigns.title = 'Approved Campaigns'
    campaign_headers = [
        'Campaign / Task Title', 'Task ID', 'Submission ID', 'Assignment ID',
        'Worker ID', 'Worker Name', 'Worker Mobile', 'Worker Email',
        'Required Numbers', 'Verified Numbers', 'Approved Valid Numbers',
        'Invalid Numbers', 'Audit Score', 'Audit Badge', 'Payout',
        'Submitted At', 'Approved At', 'Assignment Status',
    ]
    campaigns.append(campaign_headers)
    def excel_currency(amount, amount_paise):
        if amount is not None:
            try:
                return float(amount)
            except (TypeError, ValueError):
                pass
        return (amount_paise or 0) / 100

    for row in campaign_rows:
        campaigns.append([
            row['task_title'], row['task_id'], row['submission_id'], row['assignment_id'],
            row['user_id'], row['worker_name'], row['worker_mobile'], row['worker_email'],
            row['required_numbers'], row['verified_numbers'], row['approved_valid_numbers'],
            row['invalid_numbers'], row['audit_score'], row['audit_badge'],
            excel_currency(row['payout_amount'], row['payout_paise']),
            row['submitted_at'], row['approved_at'], row['assignment_status'],
        ])

    recipient = workbook.create_sheet('Recipient Audit')
    recipient_headers = [
        'Audit ID', 'Task ID', 'Campaign / Task Title', 'Submission ID',
        'Assignment ID', 'Worker ID', 'Sender Number', 'Recipient Number',
        'Message Text', 'Open Count', 'First Opened At', 'Last Opened At',
        'Target Status', 'Verified At', 'Target Paid At', 'Paid Amount',
        'Payment Timestamp',
    ]
    recipient.append(recipient_headers)
    for row in recipient_rows:
        recipient.append([
            row['audit_id'], row['task_id'], row['task_title'], row['submission_id'],
            row['assignment_id'], row['user_id'], row['sender_number'], row['recipient_number'],
            row['message_text'], row['open_count'], row['first_opened_at'], row['last_opened_at'],
            row['target_status'], row['verified_at'], row['target_paid_at'], row['paid_amount'],
            row['payment_timestamp'],
        ])

    for sheet in workbook.worksheets:
        sheet.freeze_panes = 'A2'
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.font = Font(bold=True, color='FFFFFF')
            cell.fill = PatternFill('solid', fgColor='1F4E78')
        for column_cells in sheet.columns:
            width = min(max(max(len(str(cell.value or '')) for cell in column_cells) + 2, 12), 42)
            sheet.column_dimensions[get_column_letter(column_cells[0].column)].width = width

    for row in campaigns.iter_rows(min_row=2, min_col=15, max_col=15):
        row[0].number_format = '₹#,##0.00'
    for row in recipient.iter_rows(min_row=2, min_col=16, max_col=16):
        row[0].number_format = '₹#,##0.00'

    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)
    filename = f"TaskPe_Audit_Report_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    return send_file(
        output,
        as_attachment=True,
        download_name=filename,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )

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
    target_nums = get_assigned_numbers(assignment) if assignment else task_recipient_pool(task, allow_legacy=True)
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

    try:
        recipient_region = database.validate_phone_region(
            data.get('recipient_region') or data.get('phone_region')
        )
    except ValueError:
        return jsonify({'success': False, 'message': 'A valid recipient region is required.'}), 400

    if isinstance(numbers_raw, str):
        # Split by comma, space, or newline
        nums = [n.strip() for n in numbers_raw.replace('\n', ',').split(',') if n.strip()]
    else:
        nums = [str(n).strip() for n in numbers_raw if str(n).strip()]
    try:
        normalized_nums = [database.normalize_recipient_number(number, region=recipient_region) for number in nums]
    except ValueError:
        return jsonify({'success': False, 'message': 'Every recipient must be a valid international number or an unambiguous number for the selected region.'}), 400
    nums = list(dict.fromkeys(normalized_nums))
    required_pool_size = total_slots * recipients_per_slot
    if len(nums) < required_pool_size:
        return jsonify({
            'success': False,
            'message': f'Need {required_pool_size} unique target numbers for {total_slots} slots × {recipients_per_slot} recipients. Received {len(nums)}.'
        }), 400

    counter_declaration_confirmed = str(
        data.get('counter_declaration_confirmed', data.get('counter_declaration', data.get('consent_declaration', '0')))
    ).strip().lower() in {'1', 'true', 'yes', 'on'}
    if not counter_declaration_confirmed:
        return jsonify({'success': False, 'message': 'A business consent declaration is required.'}), 400
    user = get_current_user()
    if not user or isinstance(user['id'], bool) or not isinstance(user['id'], int) or user['id'] < 1:
        return jsonify({'success': False, 'message': 'Authenticated business user is required.'}), 403
    submitted_declaration_text = data.get('consent_declaration_text')
    if submitted_declaration_text is not None and str(submitted_declaration_text).strip() != database.SHOP_CONSENT_DECLARATION_DEFAULT_TEXT:
        return jsonify({'success': False, 'message': 'The approved business declaration text is required.'}), 400
    declaration_text = database.SHOP_CONSENT_DECLARATION_DEFAULT_TEXT

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
                target_numbers, recipient_region, recipients_per_slot, time_limit_minutes, proof_instructions,
                total_slots, completed_slots, status, task_type, image_path, destination_link,
                reward_per_number_paise, reward_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'active', ?, ?, ?, ?, ?)
        ''', (title, category, description, float(paise_to_inr(payout_paise)), payout_paise,
              template_text, json.dumps(nums), recipient_region, recipients_per_slot, time_limit,
              proof_instructions, total_slots, task_type, image_path, destination_link,
              reward_paise, reward_source))
        task_id = cursor.lastrowid
        cursor.execute("UPDATE tasks SET campaign_id = ? WHERE id = ?", (task_id, task_id))

        database.create_whatsapp_campaign_consent_declaration(
            conn, task_id=task_id, campaign_id=task_id,
            business_user_id=user['id'], phone_numbers=nums,
            declaration_text=declaration_text,
            region=recipient_region,
        )

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
