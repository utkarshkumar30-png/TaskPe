# TaskPe - WhatsApp Task Earning Platform 💰

**TaskPe** is a micro-job web platform where everyday users earn money by completing real WhatsApp tasks (e.g., sending campaign templates to verified business contacts, forwarding webinar invites, or sharing status updates), submitting screenshot proof, and withdrawing their earnings directly to UPI (GPay, PhonePe, Paytm) or Bank accounts.

---

## 🌟 Key Features

### 👤 For Earners / Users:
1. **Interactive Landing Page**: High-conversion hero, live earnings ticker, 3-step process guide, earning calculator, trust proofs, and FAQs.
2. **Mobile & OTP Authentication**: Mobile number (+91) with server-validated 6-digit OTP verification, email/password signup, and ₹20 welcome bonus.
3. **Earner Dashboard**: Real-time wallet balance pill, lifetime earnings, completed/pending task metrics, and filterable task categories.
4. **WhatsApp Task Execution Drawer**:
   - Step 1: 1-Click "Copy Campaign Template".
   - Step 2: Recipient numbers list with **1-Click WhatsApp deep links (`https://wa.me/...`)** to open WhatsApp chats with pre-filled message text.
   - Live 15-minute countdown timer.
   - Step 3: Drag-and-drop screenshot uploader with image preview and submission notes.
5. **Wallet & Instant UPI Withdrawals**:
   - Live balance tracking and pending earnings breakdown.
   - Instant withdrawal to UPI (Google Pay, PhonePe, Paytm, BHIM) or IMPS Bank transfer.
   - Complete transaction history ledger and withdrawal tracking with UTR numbers.
6. **Refer & Earn Partner Program**:
   - Unique referral code and shareable link.
   - 1-Click WhatsApp share button.
   - Earn ₹20 cash bonus per referred friend.
7. **Leaderboard**: Top 10 weekly earners with gold, silver, and bronze trophy badges.

---

### 🛡️ For Admins:
1. **Proof Verification Queue**:
   - Inspect all user screenshot submissions.
   - High-resolution modal lightbox.
   - **1-Click Approve**: Automatically credits the earner's wallet balance and creates an audit ledger record.
   - **1-Click Reject**: Rejects with custom feedback reason and re-opens the task slot.
2. **Withdrawal Requests Management**:
   - Approve withdrawal requests with Bank/UPI UTR numbers.
   - Reject with automatic wallet refund.
3. **Campaign / Task Creator**:
   - Create and publish new WhatsApp campaigns (Title, Category, Payout, Template text, Target numbers list, Slots, Time limit, Proof requirements).
   - Toggle campaign status (Active / Paused).
4. **User Management**:
   - Directory of all registered earners with their balances, lifetime earnings, and approved task counts.

---

## 🚀 Quick Start (Local Setup)

### 1. Requirements
- Python 3.12+ (Pre-installed)
- Flask 3.1+ (Pre-installed)

### 2. Run the Application
Open a terminal in the project directory (`C:\Users\utkar\.gemini\antigravity\scratch\taskpe`):

```bash
py -3.12 app.py
```

Open your browser and visit:
```
http://127.0.0.1:5000
```

### Production startup

Install dependencies, configure `.env.example` through the process environment, and start Waitress (do not use Flask's development server):

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\waitress-serve.exe --listen=127.0.0.1:5000 --threads=4 wsgi:app
```

The equivalent Windows launcher is `run_taskpe_prod.bat`; it binds Waitress to
`127.0.0.1:5000` so it is not the public endpoint. Put Nginx or another
HTTPS reverse proxy in front of it. A Windows Nginx template is provided at
`deploy/nginx/taskpe.conf.example`. Copy it to a host-local configuration,
replace the hostname and certificate/key placeholders there, and keep that
configuration and private key outside source control. The proxy must redirect
HTTP to HTTPS and set the `X-Forwarded-*` headers shown in the template.

For a reverse-proxy deployment, set `TRUSTED_PROXY_HOPS=1` so TaskPe trusts
exactly one local proxy hop for the original HTTPS scheme and host. Set
`ENABLE_HSTS=1` only after HTTPS is active and the proxy is enforcing HTTPS;
HSTS is emitted only for requests that Flask recognizes as secure. With no
trusted proxy, leave `TRUSTED_PROXY_HOPS=0` and `ENABLE_HSTS=0`.

Production requires `ENVIRONMENT=production`, a strong `SECRET_KEY`, complete
Fast2SMS settings when enabled, and `PAYOUT_MODE=MANUAL`. Never commit `.env`,
TLS private keys, certificates, or real credentials.

### 3. Development-only demo credentials

These seeded credentials and one-click demo login are available only when `ENVIRONMENT=development`; they are disabled in production.
- **Earner Demo**:
  - Email / Mobile: `earner@taskpe.com` or `9876512345`
  - Password: `user123`
- **Admin Demo**:
  - Email / Mobile: `admin@taskpe.com` or `9876500000`
  - Password: `admin123`

### SMS OTP delivery (Fast2SMS)

TaskPe keeps OTP verification server-side. To deliver real Indian SMS OTPs, set these environment variables in the deployment environment:

```text
ENVIRONMENT=production
SECRET_KEY=your-strong-production-secret
SMS_PROVIDER=fast2sms
FAST2SMS_API_KEY=your-fast2sms-api-key
PAYOUT_MODE=MANUAL
```

Configure Fast2SMS OTP SMS access and any required account/DLT approvals in the Fast2SMS dashboard. TaskPe sends its server-generated six-digit OTP as `variables_values`, with `route=otp` and the normalized ten-digit Indian mobile number to `https://www.fast2sms.com/dev/bulkV2` using the `Authorization` header. Never commit the API key to source control. When `SMS_PROVIDER=fast2sms`, production startup requires `FAST2SMS_API_KEY`. Provider failures return a safe error and do not create a usable OTP record. Fast2SMS account KYC, wallet, sender/template approval, and DLT setup may be required by the provider.

With `ENVIRONMENT=development` and `SMS_PROVIDER=disabled` (the default), TaskPe does not send SMS and returns the generated OTP only for local testing. Production never returns OTP values to the browser. OTP requests have a 60-second server-side resend cooldown and are capped at five requests per mobile per hour; verification is limited to five failed attempts per issued OTP. OTPs expire after 10 minutes, are mobile-bound, and are single-use. In production, set `SMS_PROVIDER=fast2sms` with valid credentials or requests fail safely.

---

### Deployment security controls

Payout mode is restricted to `MANUAL`; unsupported values stop startup and no automatic cryptocurrency transfer is implemented. State-changing browser requests carry a server-generated CSRF token, while headerless API clients remain compatible. Login, registration, OTP, proof submission, withdrawal, and administrative mutation endpoints have server-side throttles. Proof uploads are checked by image signature in addition to extension, and authenticated/private responses use `Cache-Control: no-store`.

## 📁 Project Structure

```
taskpe/
├── app.py                  # Flask backend REST API & routing
├── database.py             # SQLite schema, connection manager & seed data
├── taskpe.db               # SQLite database file
├── requirements.txt        # Python dependencies
├── test_app.py             # 12-point automated test suite
├── static/
│   ├── css/styles.css      # Custom styling, animations, WhatsApp accents
│   └── js/app.js           # Client-side router, WhatsApp links, state manager
├── templates/
│   └── index.html          # Responsive single-page interface
└── uploads/
    └── proofs/             # Uploaded screenshot storage directory
```

---

## 🧪 Running Automated Tests

Run the full end-to-end integration test suite:

```bash
py -3.12 test_app.py
```
