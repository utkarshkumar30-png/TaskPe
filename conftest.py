"""Isolated defaults for the local pytest process.

These values are intentionally process-local test defaults. Individual tests
may replace them with their own disposable paths before importing the app.
Production configuration and application runtime defaults are unchanged.
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent
_TEST_ROOT = Path(tempfile.mkdtemp(prefix='.pytest-taskpe-', dir=_REPO_ROOT))
# Most legacy fixtures call tempfile.mkdtemp()/TemporaryDirectory() without a
# directory argument. Keep those disposable resources inside the writable
# workspace rather than the restricted OS temp location.
tempfile.tempdir = str(_TEST_ROOT)

os.environ['ENVIRONMENT'] = 'development'
os.environ['SECRET_KEY'] = 'pytest-local-secret-not-for-production'
os.environ['TASKPE_DB_PATH'] = str(_TEST_ROOT / 'taskpe.sqlite3')
os.environ['TASKPE_UPLOAD_FOLDER'] = str(_TEST_ROOT / 'proofs')
os.environ['TASKPE_TASK_ASSET_FOLDER'] = str(_TEST_ROOT / 'assets')
os.environ['SMS_PROVIDER'] = 'disabled'
os.environ['TASKPE_WHATSAPP_LINKED_DEVICE_ENABLED'] = '0'
os.environ.setdefault('TASKPE_WA_SERVICE_SHARED_SECRET', 'pytest-local-wa-secret-that-is-at-least-32-chars')



@atexit.register
def _cleanup_test_root():
    shutil.rmtree(_TEST_ROOT, ignore_errors=True)
