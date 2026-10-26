#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# TaskPe WhatsApp Campaign — Separate Guarded Database Restore Procedure
# Usage: sudo ./restore_database.sh /path/to/deployment_<TIMESTAMP>.record
# =============================================================================

DEPLOY_REC="${1:-}"
if [ -z "$DEPLOY_REC" ] || [ ! -f "$DEPLOY_REC" ]; then
  echo "Usage: sudo $0 /path/to/deployment_<TIMESTAMP>.record"
  echo "[ABORT] An explicit, valid deployment record path is required."
  exit 1
fi
DEPLOY_REC=$(realpath "$DEPLOY_REC")

echo "[INFO] Validating deployment record: $DEPLOY_REC"

REC_PERMS=$(stat -c "%a" "$DEPLOY_REC")
if [ "$REC_PERMS" != "600" ] && [ "$REC_PERMS" != "400" ]; then
  FSTYPE=$(stat -f -c "%T" "$DEPLOY_REC" 2>/dev/null || true)
  if [ "$FSTYPE" = "v9fs" ] || [ "$FSTYPE" = "9p" ] || [ "$FSTYPE" = "cifs" ] || [ "$FSTYPE" = "smb" ] || [ "$FSTYPE" = "ntfs" ] || [ "$FSTYPE" = "drvfs" ]; then
    echo "[WARN] Deployment record is on non-POSIX filesystem ($FSTYPE mode $REC_PERMS); skipping strict 600/400 check."
  else
    echo "[ABORT] Insecure deployment record permissions: $REC_PERMS (expected 600 or 400)"; exit 1;
  fi
fi

declare -A RECORD=()
declare -A KEY_SEEN=()

ALLOWED_KEYS_LIST=(
  "DEPLOYMENT_TIMESTAMP"
  "FLASK_DIR"
  "FLASK_ENV_FILE"
  "FLASK_ENV_MODE"
  "FLASK_ENV_UID"
  "FLASK_ENV_GID"
  "WA_DIR"
  "WA_ENV_FILE"
  "WA_ENV_MODE"
  "WA_ENV_UID"
  "WA_ENV_GID"
  "LIVE_DB_PATH"
  "BACKUP_DIR"
  "BACKUP_FLASK_ENV"
  "BACKUP_WA_ENV"
  "BACKUP_DB"
  "BACKUP_CODE_TAR"
  "BACKUP_WA_CODE_TAR"
  "BACKUP_MANIFEST"
)

is_allowed_key() {
  local k="$1"
  for ak in "${ALLOWED_KEYS_LIST[@]}"; do
    if [ "$ak" = "$k" ]; then
      return 0
    fi
  done
  return 1
}

while IFS= read -r line || [ -n "$line" ]; do
  line=$(echo "$line" | tr -d '\r')
  [ -z "$line" ] && continue

  if [[ ! "$line" =~ ^[A-Z0-9_]+=\"[^\`\$\"]*\"$ ]]; then
    echo "[ABORT] Malformed line in record: $line"; exit 1;
  fi

  key="${line%%=*}"
  val="${line#*=}"
  val="${val%\"}"
  val="${val#\"}"

  if ! is_allowed_key "$key"; then
    echo "[ABORT] Disallowed key in deployment record: $key"; exit 1;
  fi

  if [ -n "${KEY_SEEN[$key]:-}" ]; then
    echo "[ABORT] Duplicate key in deployment record: $key"; exit 1;
  fi

  KEY_SEEN["$key"]=1
  RECORD["$key"]="$val"
done < "$DEPLOY_REC"

for required_key in LIVE_DB_PATH BACKUP_DB BACKUP_DIR DEPLOYMENT_TIMESTAMP FLASK_DIR; do
  if [ -z "${RECORD[$required_key]:-}" ]; then
    echo "[ABORT] Missing required key in record: $required_key"; exit 1;
  fi
done

LIVE_DB_PATH="${RECORD[LIVE_DB_PATH]}"
BACKUP_DB="${RECORD[BACKUP_DB]}"
BACKUP_DIR="${RECORD[BACKUP_DIR]}"
TIMESTAMP="${RECORD[DEPLOYMENT_TIMESTAMP]}"

for path_val in "$LIVE_DB_PATH" "$BACKUP_DB" "$BACKUP_DIR"; do
  [[ "$path_val" =~ ^/[^[:space:]]+$ ]] || {
    echo "[ABORT] Path is not a valid absolute path: '$path_val'"; exit 1;
  }
  if [[ "$path_val" =~ \.\. ]]; then
    echo "[ABORT] Path traversal forbidden: '$path_val'"; exit 1;
  fi
done

if [[ "$BACKUP_DB" != "${BACKUP_DIR}/"* ]]; then
  echo "[ABORT] Database snapshot is outside recorded backup directory: $BACKUP_DB"; exit 1;
fi

[ -f "$LIVE_DB_PATH" ] || { echo "[ABORT] Target LIVE_DB_PATH not found: $LIVE_DB_PATH"; exit 1; }
[ -s "$BACKUP_DB" ] || { echo "[ABORT] Source BACKUP_DB missing or empty: $BACKUP_DB"; exit 1; }

# Verify SQLite integrity of snapshot before touching production
sqlite3 "$BACKUP_DB" "PRAGMA quick_check;" | grep -q 'ok' || {
  echo "[ABORT] Pre-deployment database backup corrupted; restore aborted"; exit 1;
}

echo "============================================================================="
echo "WARNING: DESTRUCTIVE ACTION"
echo "This will OVERWRITE the active production database with the pre-deployment snapshot."
echo "Target Live DB:   $LIVE_DB_PATH"
echo "Source Snapshot:  $BACKUP_DB"
echo "Captured At:      $TIMESTAMP"
echo ""
echo "ALL USER SIGNUPS, SUBMISSIONS, TASKS, AND TRANSACTIONS LOGGED AFTER"
echo "$TIMESTAMP WILL BE PERMANENTLY LOST."
echo "============================================================================="
read -r -p "Type 'RESTORE_DATABASE' to confirm destructive overwrite: " CONFIRMATION

if [ "$CONFIRMATION" != "RESTORE_DATABASE" ]; then
  echo "[ABORT] Database restore cancelled by operator."
  exit 1
fi

echo "[INFO] Stopping taskpe.service before database overwrite..."
systemctl stop taskpe.service

# Preserve ownership and permissions of original database
DB_OWNER=$(stat -c "%u:%g" "$LIVE_DB_PATH")
DB_MODE=$(stat -c "%a" "$LIVE_DB_PATH")

echo "[INFO] Restoring database file from snapshot..."
cp -p "$BACKUP_DB" "$LIVE_DB_PATH"
chmod "$DB_MODE" "$LIVE_DB_PATH"
chown "$DB_OWNER" "$LIVE_DB_PATH"

echo "[INFO] Verifying restored database integrity..."
sqlite3 "$LIVE_DB_PATH" "PRAGMA quick_check;" | grep -q 'ok' || {
  echo "[FAIL] Restored database quick_check failed"; exit 1;
}

echo "[INFO] Starting taskpe.service..."
systemctl start taskpe.service

systemctl is-active --quiet taskpe.service || { echo "[FAIL] taskpe failed to start after restore"; exit 1; }

FLASK_HEALTH=$(curl -fsS --max-time 5 http://127.0.0.1:5000/health || true)
echo "$FLASK_HEALTH" | grep -q '"status":[[:space:]]*"ok"' || {
  echo "[FAIL] Flask /health check failed after database restore: $FLASK_HEALTH"; exit 1;
}

echo "============================================================================="
echo "[SUCCESS] Database restored from pre-deployment snapshot: $BACKUP_DB"
echo "============================================================================="
