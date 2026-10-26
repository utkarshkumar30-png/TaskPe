#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# TaskPe WhatsApp Campaign Rollback Script
# Usage: sudo ./rollback.sh /path/to/deployments/backups_<TIMESTAMP>/deployment_<TIMESTAMP>.record
# =============================================================================

DEPLOY_REC="${1:-}"
if [ -z "$DEPLOY_REC" ] || [ ! -f "$DEPLOY_REC" ]; then
  echo "Usage: sudo $0 /path/to/deployments/backups_<TIMESTAMP>/deployment_<TIMESTAMP>.record"
  echo "[ABORT] An explicit, valid deployment record path is required."
  exit 1
fi
DEPLOY_REC=$(realpath "$DEPLOY_REC")

echo "[INFO] Validating deployment record: $DEPLOY_REC"

# -----------------------------------------------------------------------------
# STEP 1: PARSE AND VALIDATE DEPLOYMENT RECORD (STRICT KEY ALLOWLIST)
# -----------------------------------------------------------------------------
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
    echo "[ABORT] Malformed or disallowed line in deployment record: $line"; exit 1;
  fi

  key="${line%%=*}"
  val="${line#*=}"
  val="${val%\"}"
  val="${val#\"}"

  if ! is_allowed_key "$key"; then
    echo "[ABORT] Disallowed key in deployment record: $key"; exit 1;
  fi

  if [ -n "${KEY_SEEN[$key]:-}" ]; then
    echo "[ABORT] Duplicate key detected in deployment record: $key"; exit 1;
  fi

  KEY_SEEN["$key"]=1
  RECORD["$key"]="$val"
done < "$DEPLOY_REC"

# Ensure all 19 required keys are present
for required_key in "${ALLOWED_KEYS_LIST[@]}"; do
  if [ -z "${RECORD[$required_key]:-}" ]; then
    echo "[ABORT] Missing required key in deployment record: $required_key"; exit 1;
  fi
done

if [ ${#RECORD[@]} -ne ${#ALLOWED_KEYS_LIST[@]} ]; then
  echo "[ABORT] Record key count mismatch (expected ${#ALLOWED_KEYS_LIST[@]}, found ${#RECORD[@]})"; exit 1;
fi

# Extract and validate values
FLASK_DIR="${RECORD[FLASK_DIR]}"
RECORDED_FLASK_ENV_FILE="${RECORD[FLASK_ENV_FILE]}"
FLASK_ENV_FILE="$RECORDED_FLASK_ENV_FILE"
FLASK_ENV_MODE="${RECORD[FLASK_ENV_MODE]}"
FLASK_ENV_UID="${RECORD[FLASK_ENV_UID]}"
FLASK_ENV_GID="${RECORD[FLASK_ENV_GID]}"

WA_DIR="${RECORD[WA_DIR]}"
RECORDED_WA_ENV_FILE="${RECORD[WA_ENV_FILE]}"
WA_ENV_FILE="$RECORDED_WA_ENV_FILE"
WA_ENV_MODE="${RECORD[WA_ENV_MODE]}"
WA_ENV_UID="${RECORD[WA_ENV_UID]}"
WA_ENV_GID="${RECORD[WA_ENV_GID]}"

LIVE_DB_PATH="${RECORD[LIVE_DB_PATH]}"
BACKUP_DIR="${RECORD[BACKUP_DIR]}"
BACKUP_FLASK_ENV="${RECORD[BACKUP_FLASK_ENV]}"
BACKUP_WA_ENV="${RECORD[BACKUP_WA_ENV]}"
BACKUP_DB="${RECORD[BACKUP_DB]}"
BACKUP_CODE_TAR="${RECORD[BACKUP_CODE_TAR]}"
BACKUP_WA_CODE_TAR="${RECORD[BACKUP_WA_CODE_TAR]}"
BACKUP_MANIFEST="${RECORD[BACKUP_MANIFEST]}"

# Strict path format and traversal validation
for path_val in "$FLASK_DIR" "$FLASK_ENV_FILE" "$WA_DIR" "$WA_ENV_FILE" "$LIVE_DB_PATH" \
                "$BACKUP_DIR" "$BACKUP_FLASK_ENV" "$BACKUP_WA_ENV" "$BACKUP_DB" \
                "$BACKUP_CODE_TAR" "$BACKUP_WA_CODE_TAR" "$BACKUP_MANIFEST"; do
  [[ "$path_val" =~ ^/[^[:space:]]+$ ]] || {
    echo "[ABORT] Path is not a valid absolute path: '$path_val'"; exit 1;
  }
  if [[ "$path_val" =~ \.\. ]]; then
    echo "[ABORT] Path traversal forbidden: '$path_val'"; exit 1;
  fi
done

# Ensure all backup artifacts are inside BACKUP_DIR
for b_file in "$BACKUP_FLASK_ENV" "$BACKUP_WA_ENV" "$BACKUP_DB" "$BACKUP_CODE_TAR" "$BACKUP_WA_CODE_TAR" "$BACKUP_MANIFEST"; do
  if [[ "$b_file" != "${BACKUP_DIR}/"* ]]; then
    echo "[ABORT] Backup artifact outside recorded backup directory: $b_file"; exit 1;
  fi
done

# Rollback must use the exact environment files resolved and recorded by
# deploy.sh. Do not guess between multiple files or restore an ambiguous path.
validate_authoritative_env_file() {
  local env_file="$1"
  local expected_path="$2"
  [ -f "$env_file" ] && [ -r "$env_file" ] && [ -w "$env_file" ] || {
    echo "[ABORT] Authoritative environment file is missing or inaccessible: $env_file" >&2
    exit 1
  }
  [ "$(realpath "$env_file")" = "$expected_path" ] || {
    echo "[ABORT] Environment file does not match the recorded authoritative path: $env_file" >&2
    exit 1
  }
}

assert_rollback_send_flags_disabled() {
  local env_file="$1"
  local flag_name="$2"
  local matches flag_value
  matches=$(grep -E "^${flag_name}=" "$env_file" 2>/dev/null || true)
  [ "$(printf '%s\n' "$matches" | sed '/^$/d' | wc -l)" -eq 1 ] || {
    echo "[ABORT] ${flag_name} must have exactly one explicit value in ${env_file}" >&2
    exit 1
  }
  flag_value=$(printf '%s\n' "$matches" | cut -d '=' -f2- | tr -d '[:space:]\"' || true)
  case "${flag_value,,}" in
    0|false|no|off) ;;
    *)
      echo "[ABORT] ${flag_name} is not explicitly disabled in ${env_file}" >&2
      exit 1
      ;;
  esac
}

validate_authoritative_env_file "$FLASK_ENV_FILE" "$(realpath "$RECORDED_FLASK_ENV_FILE")"
validate_authoritative_env_file "$WA_ENV_FILE" "$(realpath "$RECORDED_WA_ENV_FILE")"
assert_rollback_send_flags_disabled "$FLASK_ENV_FILE" "TASKPE_WHATSAPP_LINKED_DEVICE_ENABLED"
assert_rollback_send_flags_disabled "$WA_ENV_FILE" "TASKPE_WA_CAMPAIGN_BATCH_HTTP_SEND_ENABLED"
assert_rollback_send_flags_disabled "$BACKUP_FLASK_ENV" "TASKPE_WHATSAPP_LINKED_DEVICE_ENABLED"
assert_rollback_send_flags_disabled "$BACKUP_WA_ENV" "TASKPE_WA_CAMPAIGN_BATCH_HTTP_SEND_ENABLED"
echo "[OK] Current and recorded WhatsApp sending flags are explicitly disabled before rollback mutation."

# -----------------------------------------------------------------------------
# STEP 2: VERIFY ALL REFERENCED BACKUP ARTIFACTS PRIOR TO MODIFICATION
# -----------------------------------------------------------------------------
echo "[INFO] Verifying recorded backup files and targets..."
[ -d "$BACKUP_DIR" ] || { echo "[ABORT] Backup directory does not exist: $BACKUP_DIR"; exit 1; }
[ -d "$FLASK_DIR" ] || { echo "[ABORT] Target Flask directory does not exist: $FLASK_DIR"; exit 1; }
[ -d "$WA_DIR" ] || { echo "[ABORT] Target Node directory does not exist: $WA_DIR"; exit 1; }

[ -s "$BACKUP_FLASK_ENV" ] || { echo "[ABORT] Recorded Flask env backup missing or empty: $BACKUP_FLASK_ENV"; exit 1; }
[ -s "$BACKUP_WA_ENV" ] || { echo "[ABORT] Recorded Node env backup missing or empty: $BACKUP_WA_ENV"; exit 1; }
[ -s "$BACKUP_CODE_TAR" ] || { echo "[ABORT] Recorded Flask code archive missing or empty: $BACKUP_CODE_TAR"; exit 1; }
[ -s "$BACKUP_WA_CODE_TAR" ] || { echo "[ABORT] Recorded Node code archive missing or empty: $BACKUP_WA_CODE_TAR"; exit 1; }
[ -s "$BACKUP_MANIFEST" ] || { echo "[ABORT] Recorded manifest missing or empty: $BACKUP_MANIFEST"; exit 1; }

# Verify archive integrity and test extraction in temporary directory
TEST_EXTRACT=$(mktemp -d)
tar -xzpf "$BACKUP_CODE_TAR" -C "$TEST_EXTRACT" >/dev/null 2>&1 || {
  echo "[ABORT] Flask code backup archive corrupted"; rm -rf "$TEST_EXTRACT"; exit 1;
}
rm -rf "$TEST_EXTRACT"

TEST_EXTRACT_WA=$(mktemp -d)
tar -xzpf "$BACKUP_WA_CODE_TAR" -C "$TEST_EXTRACT_WA" >/dev/null 2>&1 || {
  echo "[ABORT] Node code backup archive corrupted"; rm -rf "$TEST_EXTRACT_WA"; exit 1;
}
rm -rf "$TEST_EXTRACT_WA"

# Verify manifest readability
while read -r svc rel m u g h; do
  [ -z "$svc" ] && continue
  if [ "$svc" != "FLASK" ] && [ "$svc" != "WA" ]; then
    echo "[ABORT] Invalid service in manifest: $svc"; exit 1;
  fi
  [[ "$m" =~ ^[0-7]{3,4}$ ]] || { echo "[ABORT] Invalid mode in manifest: $m"; exit 1; }
  [[ "$u" =~ ^[0-9]+$ ]] || { echo "[ABORT] Invalid UID in manifest: $u"; exit 1; }
  [[ "$g" =~ ^[0-9]+$ ]] || { echo "[ABORT] Invalid GID in manifest: $g"; exit 1; }
done < "$BACKUP_MANIFEST"

echo "[OK] All backup artifacts verified intact."

# -----------------------------------------------------------------------------
# STEP 3: RESTORE ORIGINAL ENVIRONMENT CONFIGURATION AND PERMISSIONS
# -----------------------------------------------------------------------------
echo "[INFO] Restoring environment files and original permissions..."
cp -p "$BACKUP_FLASK_ENV" "$FLASK_ENV_FILE"
chmod "$FLASK_ENV_MODE" "$FLASK_ENV_FILE"
chown "${FLASK_ENV_UID}:${FLASK_ENV_GID}" "$FLASK_ENV_FILE"

cp -p "$BACKUP_WA_ENV" "$WA_ENV_FILE"
chmod "$WA_ENV_MODE" "$WA_ENV_FILE"
chown "${WA_ENV_UID}:${WA_ENV_GID}" "$WA_ENV_FILE"

assert_rollback_send_flags_disabled "$FLASK_ENV_FILE" "TASKPE_WHATSAPP_LINKED_DEVICE_ENABLED"
assert_rollback_send_flags_disabled "$WA_ENV_FILE" "TASKPE_WA_CAMPAIGN_BATCH_HTTP_SEND_ENABLED"
echo "[OK] Restored WhatsApp sending flags remain explicitly disabled."

# -----------------------------------------------------------------------------
# STEP 4: RESTORE PREVIOUS CODE FILES WITH PRESERVED OWNERSHIP/PERMISSIONS
# -----------------------------------------------------------------------------
echo "[INFO] Restoring previous application code..."
tar -xzpf "$BACKUP_CODE_TAR" -C "$FLASK_DIR"
tar -xzpf "$BACKUP_WA_CODE_TAR" -C "$WA_DIR"

# Enforce exact original ownership and permissions on every restored file
while read -r svc rel m u g h; do
  [ -z "$svc" ] && continue
  if [ "$svc" = "FLASK" ]; then
    target="${FLASK_DIR}/${rel}"
  else
    target="${WA_DIR}/${rel}"
  fi
  [ -f "$target" ] || { echo "[ABORT] Restored file missing: $target"; exit 1; }
  chmod "$m" "$target"
  chown "${u}:${g}" "$target"
done < "$BACKUP_MANIFEST"

# -----------------------------------------------------------------------------
# STEP 5: RESTART SERVICES AND VERIFY SAFE FAIL-CLOSED HEALTH
# -----------------------------------------------------------------------------
echo "[INFO] Restarting services to apply pre-deployment configuration..."
systemctl restart taskpe-whatsapp.service
systemctl restart taskpe.service

systemctl is-active --quiet taskpe-whatsapp.service || { echo "[FAIL] taskpe-whatsapp failed to start"; exit 1; }
systemctl is-active --quiet taskpe.service || { echo "[FAIL] taskpe failed to start"; exit 1; }

# Verify Node microservice loopback binding
NODE_PORT=$(grep '^TASKPE_WA_SERVICE_PORT=' "$WA_ENV_FILE" 2>/dev/null | cut -d '=' -f2- | tr -d '[:space:]\"' || true)
NODE_PORT=${NODE_PORT:-8765}
ss -tulpn | grep ":${NODE_PORT} " | grep -q "127.0.0.1" || {
  echo "[FAIL] Node WhatsApp microservice is not bound strictly to 127.0.0.1:${NODE_PORT}"; exit 1;
}

# Verify Flask health endpoint
FLASK_HEALTH=$(curl -fsS --max-time 5 http://127.0.0.1:5000/health || true)
echo "$FLASK_HEALTH" | grep -q '"status":[[:space:]]*"ok"' || {
  echo "[FAIL] Flask /health check failed after rollback: $FLASK_HEALTH"; exit 1;
}

# Verify live database integrity
sqlite3 "$LIVE_DB_PATH" "PRAGMA quick_check;" | grep -q 'ok' || {
  echo "[FAIL] Database quick_check failed after rollback"; exit 1;
}

assert_rollback_send_flags_disabled "$FLASK_ENV_FILE" "TASKPE_WHATSAPP_LINKED_DEVICE_ENABLED"
assert_rollback_send_flags_disabled "$WA_ENV_FILE" "TASKPE_WA_CAMPAIGN_BATCH_HTTP_SEND_ENABLED"
echo "[OK] Both WhatsApp sending flags are disabled after rollback health checks."

echo "============================================================================="
echo "[SUCCESS] Rollback completed. Services running in original fail-closed state."
echo "============================================================================="
