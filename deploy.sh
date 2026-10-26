#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# TaskPe WhatsApp Campaign Production Deployment Script
# Usage: sudo ./deploy.sh /path/to/tested_release
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROLLBACK_SCRIPT_PATH="${SCRIPT_DIR}/rollback.sh"

SOURCE_RELEASE_DIR="${1:-}"
if [ -z "$SOURCE_RELEASE_DIR" ] || [ ! -d "$SOURCE_RELEASE_DIR" ]; then
  echo "[ABORT] Path to tested release directory is required."
  echo "Usage: sudo $0 /path/to/tested_release"
  exit 1
fi
SOURCE_RELEASE_DIR=$(realpath "$SOURCE_RELEASE_DIR")

# State tracking for fail-closed automatic rollback
DEPLOYMENT_MODIFIED=0
DEPLOY_REC=""

cleanup_on_error() {
  local exit_code=$?
  trap - ERR EXIT
  if [ "${DEPLOYMENT_MODIFIED:-0}" -eq 1 ]; then
    echo ""
    echo "============================================================================="
    echo "[ERROR] Deployment failed during or after modifying production (exit: $exit_code)."
    echo "[ROLLBACK] Executing automatic rollback to pre-deployment state..."
    echo "============================================================================="
    if [ -n "$DEPLOY_REC" ] && [ -f "$DEPLOY_REC" ] && [ -f "$ROLLBACK_SCRIPT_PATH" ]; then
      if /bin/bash "$ROLLBACK_SCRIPT_PATH" "$DEPLOY_REC"; then
        echo "[ROLLBACK] Automatic rollback succeeded. Pre-deployment fail-closed state restored."
      else
        echo "[CRITICAL] Automatic rollback encountered an error. Manual intervention required."
        echo "Execute manually: sudo $ROLLBACK_SCRIPT_PATH $DEPLOY_REC"
      fi
    else
      echo "[CRITICAL] Deployment record or rollback script not found; automatic rollback could not run."
    fi
  else
    echo "[ABORT] Deployment aborted before modifying any production targets (exit: $exit_code)."
  fi
  exit "$exit_code"
}
trap cleanup_on_error ERR

# -----------------------------------------------------------------------------
# STEP 1: READ-ONLY PRODUCTION PREFLIGHT
# -----------------------------------------------------------------------------
echo "[STEP 1/11] Executing production preflight checks..."
for tool in systemctl sqlite3 node ss curl sha256sum stat python3 tar gzip mktemp rm realpath chmod chown cp; do
  command -v "$tool" >/dev/null 2>&1 || { echo "[ABORT] Required utility not found: $tool"; exit 1; }
done

[ -f "$ROLLBACK_SCRIPT_PATH" ] && [ -r "$ROLLBACK_SCRIPT_PATH" ] || {
  echo "[ABORT] Rollback script missing or unreadable: $ROLLBACK_SCRIPT_PATH"; exit 1;
}

systemctl list-unit-files taskpe.service taskpe-whatsapp.service >/dev/null 2>&1 || {
  echo "[ABORT] Required systemd service definitions missing"; exit 1;
}
systemctl is-active --quiet taskpe.service || { echo "[ABORT] taskpe.service is not currently active"; exit 1; }
systemctl is-active --quiet taskpe-whatsapp.service || { echo "[ABORT] taskpe-whatsapp.service is not currently active"; exit 1; }

FLASK_DIR=$(systemctl show -p WorkingDirectory --value taskpe.service 2>/dev/null || true)
WA_DIR=$(systemctl show -p WorkingDirectory --value taskpe-whatsapp.service 2>/dev/null || true)
FLASK_USER=$(systemctl show -p User --value taskpe.service 2>/dev/null || true)
WA_USER=$(systemctl show -p User --value taskpe-whatsapp.service 2>/dev/null || true)

[ -n "$FLASK_DIR" ] && [ -d "$FLASK_DIR" ] || { echo "[ABORT] Invalid FLASK_DIR: '$FLASK_DIR'"; exit 1; }
[ -n "$WA_DIR" ] && [ -d "$WA_DIR" ] || { echo "[ABORT] Invalid WA_DIR: '$WA_DIR'"; exit 1; }
[ -n "$FLASK_USER" ] || { echo "[ABORT] Unable to resolve taskpe.service User"; exit 1; }
[ -n "$WA_USER" ] || { echo "[ABORT] Unable to resolve taskpe-whatsapp.service User"; exit 1; }

FLASK_DIR=$(realpath "$FLASK_DIR")
WA_DIR=$(realpath "$WA_DIR")

# Robust EnvironmentFile resolver handling optional prefixes, multiple entries, and ambiguity
resolve_env_file() {
  local service="$1"
  local target_var="$2"
  local candidate_files=()

  # 1. Parse unit definitions from systemctl cat
  while IFS= read -r line; do
    if [[ "$line" =~ ^[[:space:]]*EnvironmentFile=-?([^[:space:]#]+) ]]; then
      local raw_path="${BASH_REMATCH[1]}"
      raw_path=$(echo "$raw_path" | tr -d '\"'\''')
      if [ -n "$raw_path" ]; then
        candidate_files+=("$raw_path")
      fi
    fi
  done < <(systemctl cat "$service" 2>/dev/null || true)

  # 2. Parse EnvironmentFiles property
  local show_env
  show_env=$(systemctl show -p EnvironmentFiles --value "$service" 2>/dev/null || true)
  if [ -n "$show_env" ]; then
    for item in $show_env; do
      if [[ "$item" =~ ^/ ]]; then
        local clean_path="${item%%(*}"
        clean_path="${clean_path%"${clean_path##*[![:space:]]}"}"
        if [ -n "$clean_path" ]; then
          candidate_files+=("$clean_path")
        fi
      fi
    done
  fi

  # Deduplicate existing candidate files
  local existing_files=()
  for cand in "${candidate_files[@]}"; do
    if [ -f "$cand" ]; then
      local rpath
      rpath=$(realpath "$cand")
      local already=0
      for ef in "${existing_files[@]}"; do
        if [ "$ef" = "$rpath" ]; then
          already=1
          break
        fi
      done
      if [ $already -eq 0 ]; then
        existing_files+=("$rpath")
      fi
    fi
  done

  if [ ${#existing_files[@]} -eq 0 ]; then
    echo "[ABORT] No existing EnvironmentFile found for $service" >&2
    exit 1
  fi

  if [ ${#existing_files[@]} -eq 1 ]; then
    echo "${existing_files[0]}"
    return 0
  fi

  # Disambiguate when multiple files exist: identify file defining target variable
  local matched_files=()
  for ef in "${existing_files[@]}"; do
    if grep -q "^${target_var}=" "$ef" 2>/dev/null; then
      matched_files+=("$ef")
    fi
  done

  if [ ${#matched_files[@]} -eq 1 ]; then
    echo "${matched_files[0]}"
    return 0
  fi

  # Fallback to general service prefix
  if [ ${#matched_files[@]} -eq 0 ]; then
    local prefix_matched=()
    for ef in "${existing_files[@]}"; do
      if grep -q "^TASKPE_" "$ef" 2>/dev/null; then
        prefix_matched+=("$ef")
      fi
    done
    if [ ${#prefix_matched[@]} -eq 1 ]; then
      echo "${prefix_matched[0]}"
      return 0
    fi
  fi

  echo "[ABORT] Ambiguous EnvironmentFiles for $service: multiple candidates found (${existing_files[*]}). Aborting to prevent guessing." >&2
  exit 1
}

FLASK_ENV_FILE=$(resolve_env_file taskpe.service "TASKPE_WHATSAPP_CONSENT_GATE_ENABLED")
WA_ENV_FILE=$(resolve_env_file taskpe-whatsapp.service "TASKPE_WA_CAMPAIGN_BATCH_HTTP_SEND_ENABLED")

[ -r "$FLASK_ENV_FILE" ] && [ -w "$FLASK_ENV_FILE" ] || {
  echo "[ABORT] Flask EnvironmentFile not readable/writable: $FLASK_ENV_FILE"; exit 1;
}
[ -r "$WA_ENV_FILE" ] && [ -w "$WA_ENV_FILE" ] || {
  echo "[ABORT] Node EnvironmentFile not readable/writable: $WA_ENV_FILE"; exit 1;
}

# WhatsApp sending must remain explicitly disabled throughout this deployment.
# Missing variables are treated as disabled; any enabled value is a hard stop.
assert_whatsapp_send_flag_disabled() {
  local env_file="$1"
  local flag_name="$2"
  local flag_value
  flag_value=$(grep -E "^${flag_name}=" "$env_file" 2>/dev/null | tail -n 1 | cut -d '=' -f2- | tr -d '[:space:]\"' || true)
  case "${flag_value,,}" in
    1|true|yes|on)
      echo "[ABORT] ${flag_name} is enabled in ${env_file}; deployment requires it to remain disabled." >&2
      exit 1
      ;;
  esac
}

assert_whatsapp_send_flag_disabled "$FLASK_ENV_FILE" "TASKPE_WHATSAPP_LINKED_DEVICE_ENABLED"
assert_whatsapp_send_flag_disabled "$WA_ENV_FILE" "TASKPE_WA_CAMPAIGN_BATCH_HTTP_SEND_ENABLED"
echo "[OK] WhatsApp linked-device and batch-send flags are disabled before deployment."

if [ -x "${FLASK_DIR}/.venv/bin/python3" ]; then
  FLASK_PYTHON="${FLASK_DIR}/.venv/bin/python3"
elif [ -x "${FLASK_DIR}/venv/bin/python3" ]; then
  FLASK_PYTHON="${FLASK_DIR}/venv/bin/python3"
else
  FLASK_PYTHON=$(command -v python3)
fi
[ -x "$FLASK_PYTHON" ] || { echo "[ABORT] Python executable not found"; exit 1; }

# -----------------------------------------------------------------------------
# STEP 2: VALIDATE TESTED RELEASE ARTIFACTS AND CHECKSUMS
# -----------------------------------------------------------------------------
echo "[STEP 2/11] Validating tested release files and cryptographic checksums..."
# Candidate-specific allowlist for taskpe-release-candidate-20261026.  This is
# deliberately separate from historical manifests; changing it requires a
# reviewed candidate update.
FLASK_REPLACE_FILES=(
  "app.py"
  "database.py"
  "templates/admin.html"
  "templates/index.html"
  "static/js/admin.js"
  "static/js/app.js"
)
WA_REPLACE_FILES=(
  "server.js"
  "whatsapp_ledger.js"
)
RELEASE_FILES=("${FLASK_REPLACE_FILES[@]}" "${WA_REPLACE_FILES[@]}")

EXPECTED_CHECKSUMS="
b52975ff3a25bbb8f040dcf98e37a75084ee6c08d50b5c3bf54fc9659c352675  app.py
718abfa6dcbd2571b12bee0fade93909f092338961c8296a2af6947e21b63c1a  database.py
9dc706fb627755f4735ecb0feaffd364d5f1c4e5da1c0d1f10a594741476038c  server.js
a13f60b0e7438c1d2c642af7586600c83f71596f62f9e7107409a47c70d6a5be  whatsapp_ledger.js
aee29fe7db6051b416514f06323851cb901f9161cb1d673d2b6d3fbb24d40b0e  templates/admin.html
e4e9ddf5c8745ef4d4c6a7681915107bd798257a7919332cad2993d3062a230d  templates/index.html
185df821f622ad00e6a1ecb87eb0b8dc6f6a57916cf77dc9fef15ba5ac0509f9  static/js/admin.js
4b92ea0229f6aa2562fca3123124b33bdba0d1de0ef54d9a398603506f8d4cdb  static/js/app.js
"

VALIDATED_FILES=()
while read -r expected_hash rel_path; do
  [ -z "$expected_hash" ] && continue
  VALIDATED_FILES+=("$rel_path")
  is_release_file=0
  for release_path in "${RELEASE_FILES[@]}"; do
    [ "$release_path" = "$rel_path" ] && is_release_file=1
  done
  [ "$is_release_file" -eq 1 ] || {
    echo "[ABORT] Checksum allowlist contains an unapproved release file: $rel_path"; exit 1;
  }
  full_path="${SOURCE_RELEASE_DIR}/${rel_path}"
  [ -f "$full_path" ] && [ -r "$full_path" ] || {
    echo "[ABORT] Required release file missing or unreadable: $full_path"; exit 1;
  }
  actual_hash=$("$FLASK_PYTHON" -c '
import hashlib
from pathlib import Path
import sys

data = Path(sys.argv[1]).read_bytes()
data = data.replace(bytes((13, 10)), bytes((10,))).replace(bytes((13,)), bytes((10,)))
print(hashlib.sha256(data).hexdigest())
' "$full_path")
  if [ "$actual_hash" != "$expected_hash" ]; then
    echo "[ABORT] Checksum mismatch for $rel_path:"
    echo "  Expected: $expected_hash"
    echo "  Actual:   $actual_hash"
    exit 1
  fi
done <<< "$EXPECTED_CHECKSUMS"

[ "${#VALIDATED_FILES[@]}" -eq "${#RELEASE_FILES[@]}" ] || {
  echo "[ABORT] Checksum allowlist and replacement file list differ in size"; exit 1;
}
for release_path in "${RELEASE_FILES[@]}"; do
  is_validated=0
  for validated_path in "${VALIDATED_FILES[@]}"; do
    [ "$validated_path" = "$release_path" ] && is_validated=1
  done
  [ "$is_validated" -eq 1 ] || {
    echo "[ABORT] Replacement file is not checksum-validated: $release_path"; exit 1;
  }
done
echo "[OK] Candidate taskpe-release-candidate-20261026: all ${#RELEASE_FILES[@]} replacement files validated against exact SHA-256 signatures."

# -----------------------------------------------------------------------------
# STEP 3: CONFIRM THE AUTHORITATIVE LIVE DATABASE PATH
# -----------------------------------------------------------------------------
echo "[STEP 3/11] Determining live production database path through application loader..."
LIVE_DB_PATH=$(
  sudo -u "$FLASK_USER" "$FLASK_PYTHON" -c "
import os, sys
env_path = '${FLASK_ENV_FILE}'
if os.path.isfile(env_path):
    with open(env_path, 'r', encoding='utf-8') as fp:
        for line in fp:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip().strip('\'\"'))
sys.path.insert(0, '${FLASK_DIR}')
import database
print(os.path.realpath(database.DB_PATH))
" 2>/dev/null || true
)

[ -n "$LIVE_DB_PATH" ] || { echo "[ABORT] Application configuration failed to resolve DB_PATH"; exit 1; }
[[ "$LIVE_DB_PATH" =~ ^/ ]] || { echo "[ABORT] Resolved DB_PATH is not an absolute path: $LIVE_DB_PATH"; exit 1; }
[ -f "$LIVE_DB_PATH" ] || { echo "[ABORT] Database file does not exist at resolved path: $LIVE_DB_PATH"; exit 1; }
[ -r "$LIVE_DB_PATH" ] || { echo "[ABORT] Database file is not readable: $LIVE_DB_PATH"; exit 1; }
[ -w "$LIVE_DB_PATH" ] || { echo "[ABORT] Database file is not writable: $LIVE_DB_PATH"; exit 1; }

DB_CHECK=$(sqlite3 "$LIVE_DB_PATH" "PRAGMA quick_check;" 2>/dev/null || true)
if [ "$DB_CHECK" != "ok" ]; then
  echo "[ABORT] SQLite quick_check failed on live database: $LIVE_DB_PATH ($DB_CHECK)"; exit 1;
fi

# Validate the intended release code against the authoritative live schema
# before creating deployment artifacts or replacing active files. This is a
# read-only check; schema initialization and migration remain explicit steps.
echo "[STEP 3A/11] Verifying intended release schema read-only before replacement..."
sudo -u "$FLASK_USER" env TASKPE_DB_PATH="$LIVE_DB_PATH" ENVIRONMENT=production "$FLASK_PYTHON" -c "
import sqlite3, sys
sys.path.insert(0, '${SOURCE_RELEASE_DIR}')
import database
conn = sqlite3.connect('file:' + '${LIVE_DB_PATH}' + '?mode=ro', uri=True)
try:
    database.verify_required_schema(conn)
finally:
    conn.close()
print('[OK] Intended release schema is compatible with the live database.')
"

FLASK_PID=$(systemctl show -p MainPID --value taskpe.service 2>/dev/null || true)
if [ -n "$FLASK_PID" ] && [ "$FLASK_PID" -gt 0 ] 2>/dev/null; then
  if [ -d "/proc/$FLASK_PID/fd" ]; then
    PROC_OPEN_DBS=$(find -L "/proc/$FLASK_PID/fd" -maxdepth 1 -type f 2>/dev/null | grep -E '\.(db|sqlite3?)$' | grep -v 'ledger' || true)
    if [ -n "$PROC_OPEN_DBS" ]; then
      MATCH_FOUND=0
      while read -r open_db; do
        [ -z "$open_db" ] && continue
        if [ "$(realpath "$open_db" 2>/dev/null)" = "$LIVE_DB_PATH" ]; then
          MATCH_FOUND=1
        else
          echo "[ABORT] Active Flask PID $FLASK_PID has open DB ($open_db) differing from resolved DB ($LIVE_DB_PATH)"; exit 1;
        fi
      done <<< "$PROC_OPEN_DBS"
      if [ "$MATCH_FOUND" -ne 1 ]; then
        echo "[ABORT] Active Flask process has open databases, but none match resolved DB: $LIVE_DB_PATH"; exit 1;
      fi
    fi
  fi
fi
echo "[OK] Authoritative live database verified: $LIVE_DB_PATH"

# -----------------------------------------------------------------------------
# STEP 4: CREATE AND VERIFY UNBREAKABLE BACKUPS BEFORE TOUCHING PRODUCTION
# -----------------------------------------------------------------------------
echo "[STEP 4/11] Creating atomic backups of configuration, database, and replaced code..."
TS=$(date +%Y%m%d_%H%M%S)
[[ "$TS" =~ ^[0-9]{8}_[0-9]{6}$ ]] || { echo "[ABORT] Invalid timestamp generated"; exit 1; }

BACKUP_DIR="${FLASK_DIR}/deployments/backups_${TS}"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

BACKUP_FLASK_ENV="${BACKUP_DIR}/flask_env_${TS}"
BACKUP_WA_ENV="${BACKUP_DIR}/wa_env_${TS}"
BACKUP_DB="${BACKUP_DIR}/database_${TS}.db"
BACKUP_CODE_TAR="${BACKUP_DIR}/code_pre_deployment_${TS}.tar.gz"
BACKUP_WA_CODE_TAR="${BACKUP_DIR}/wa_code_pre_deployment_${TS}.tar.gz"
BACKUP_MANIFEST="${BACKUP_DIR}/backup_manifest_${TS}.txt"
DEPLOY_REC="${BACKUP_DIR}/deployment_${TS}.record"

FLASK_ENV_MODE=$(stat -c "%a" "$FLASK_ENV_FILE")
FLASK_ENV_UID=$(stat -c "%u" "$FLASK_ENV_FILE")
FLASK_ENV_GID=$(stat -c "%g" "$FLASK_ENV_FILE")

WA_ENV_MODE=$(stat -c "%a" "$WA_ENV_FILE")
WA_ENV_UID=$(stat -c "%u" "$WA_ENV_FILE")
WA_ENV_GID=$(stat -c "%g" "$WA_ENV_FILE")

# 1. Backup environment files and verify checksum equality
cp -p "$FLASK_ENV_FILE" "$BACKUP_FLASK_ENV"
cp -p "$WA_ENV_FILE" "$BACKUP_WA_ENV"
[ -s "$BACKUP_FLASK_ENV" ] || { echo "[ABORT] Flask env backup is empty"; exit 1; }
[ -s "$BACKUP_WA_ENV" ] || { echo "[ABORT] Node env backup is empty"; exit 1; }

if [ "$(sha256sum "$FLASK_ENV_FILE" | awk '{print $1}')" != "$(sha256sum "$BACKUP_FLASK_ENV" | awk '{print $1}')" ]; then
  echo "[ABORT] Flask environment backup checksum mismatch"; exit 1;
fi
if [ "$(sha256sum "$WA_ENV_FILE" | awk '{print $1}')" != "$(sha256sum "$BACKUP_WA_ENV" | awk '{print $1}')" ]; then
  echo "[ABORT] Node environment backup checksum mismatch"; exit 1;
fi

# 2. Backup SQLite database using online backup API and verify integrity
sqlite3 "$LIVE_DB_PATH" ".backup '$BACKUP_DB'"
[ -s "$BACKUP_DB" ] || { echo "[ABORT] Database backup is empty"; exit 1; }
sqlite3 "$BACKUP_DB" "PRAGMA quick_check;" | grep -q 'ok' || { echo "[ABORT] Database backup corrupted"; exit 1; }

# 3. Capture metadata and create manifest of EVERY code file being replaced.
# The replacement lists were validated in Step 2 and are intentionally reused.

> "$BACKUP_MANIFEST"
for rel in "${FLASK_REPLACE_FILES[@]}"; do
  full="${FLASK_DIR}/${rel}"
  [ -f "$full" ] || { echo "[ABORT] Existing production file missing: $full"; exit 1; }
  m=$(stat -c "%a" "$full")
  u=$(stat -c "%u" "$full")
  g=$(stat -c "%g" "$full")
  h=$(sha256sum "$full" | awk '{print $1}')
  echo "FLASK ${rel} ${m} ${u} ${g} ${h}" >> "$BACKUP_MANIFEST"
done

for rel in "${WA_REPLACE_FILES[@]}"; do
  full="${WA_DIR}/${rel}"
  [ -f "$full" ] || { echo "[ABORT] Existing production file missing: $full"; exit 1; }
  m=$(stat -c "%a" "$full")
  u=$(stat -c "%u" "$full")
  g=$(stat -c "%g" "$full")
  h=$(sha256sum "$full" | awk '{print $1}')
  echo "WA ${rel} ${m} ${u} ${g} ${h}" >> "$BACKUP_MANIFEST"
done
chmod 600 "$BACKUP_MANIFEST"

# 4. Create code archives preserving permissions
tar -czpf "$BACKUP_CODE_TAR" -C "$FLASK_DIR" "${FLASK_REPLACE_FILES[@]}"
[ -s "$BACKUP_CODE_TAR" ] || { echo "[ABORT] Flask code backup tar is empty"; exit 1; }
tar -tzf "$BACKUP_CODE_TAR" >/dev/null 2>&1 || { echo "[ABORT] Flask code backup corrupted"; exit 1; }

tar -czpf "$BACKUP_WA_CODE_TAR" -C "$WA_DIR" "${WA_REPLACE_FILES[@]}"
[ -s "$BACKUP_WA_CODE_TAR" ] || { echo "[ABORT] Node code backup tar is empty"; exit 1; }
tar -tzf "$BACKUP_WA_CODE_TAR" >/dev/null 2>&1 || { echo "[ABORT] Node code backup corrupted"; exit 1; }

# 5. Test extract both archives into temporary directory and verify checksums
EXTRACT_TEST_DIR=$(mktemp -d)
tar -xzpf "$BACKUP_CODE_TAR" -C "$EXTRACT_TEST_DIR"
for rel in "${FLASK_REPLACE_FILES[@]}"; do
  [ -f "${EXTRACT_TEST_DIR}/${rel}" ] || {
    echo "[ABORT] Extraction verification failed for Flask file: $rel"; rm -rf "$EXTRACT_TEST_DIR"; exit 1;
  }
  orig_hash=$(sha256sum "${FLASK_DIR}/${rel}" | awk '{print $1}')
  ext_hash=$(sha256sum "${EXTRACT_TEST_DIR}/${rel}" | awk '{print $1}')
  if [ "$orig_hash" != "$ext_hash" ]; then
    echo "[ABORT] Extraction hash mismatch for $rel"; rm -rf "$EXTRACT_TEST_DIR"; exit 1;
  fi
done
rm -rf "$EXTRACT_TEST_DIR"

EXTRACT_TEST_DIR_WA=$(mktemp -d)
tar -xzpf "$BACKUP_WA_CODE_TAR" -C "$EXTRACT_TEST_DIR_WA"
for rel in "${WA_REPLACE_FILES[@]}"; do
  [ -f "${EXTRACT_TEST_DIR_WA}/${rel}" ] || {
    echo "[ABORT] Extraction verification failed for Node file: $rel"; rm -rf "$EXTRACT_TEST_DIR_WA"; exit 1;
  }
  orig_hash=$(sha256sum "${WA_DIR}/${rel}" | awk '{print $1}')
  ext_hash=$(sha256sum "${EXTRACT_TEST_DIR_WA}/${rel}" | awk '{print $1}')
  if [ "$orig_hash" != "$ext_hash" ]; then
    echo "[ABORT] Extraction hash mismatch for $rel"; rm -rf "$EXTRACT_TEST_DIR_WA"; exit 1;
  fi
done
rm -rf "$EXTRACT_TEST_DIR_WA"

# 6. Write strict deployment record
(
  umask 077
  cat <<EOF > "$DEPLOY_REC"
DEPLOYMENT_TIMESTAMP="${TS}"
FLASK_DIR="${FLASK_DIR}"
FLASK_ENV_FILE="${FLASK_ENV_FILE}"
FLASK_ENV_MODE="${FLASK_ENV_MODE}"
FLASK_ENV_UID="${FLASK_ENV_UID}"
FLASK_ENV_GID="${FLASK_ENV_GID}"
WA_DIR="${WA_DIR}"
WA_ENV_FILE="${WA_ENV_FILE}"
WA_ENV_MODE="${WA_ENV_MODE}"
WA_ENV_UID="${WA_ENV_UID}"
WA_ENV_GID="${WA_ENV_GID}"
LIVE_DB_PATH="${LIVE_DB_PATH}"
BACKUP_DIR="${BACKUP_DIR}"
BACKUP_FLASK_ENV="${BACKUP_FLASK_ENV}"
BACKUP_WA_ENV="${BACKUP_WA_ENV}"
BACKUP_DB="${BACKUP_DB}"
BACKUP_CODE_TAR="${BACKUP_CODE_TAR}"
BACKUP_WA_CODE_TAR="${BACKUP_WA_CODE_TAR}"
BACKUP_MANIFEST="${BACKUP_MANIFEST}"
EOF
)
chmod 600 "$DEPLOY_REC"
echo "[OK] Backups created, verified, and locked. Deployment record at: $DEPLOY_REC"

# -----------------------------------------------------------------------------
# STEP 5: DEPLOY VALIDATED CODE (BATCH SENDING REMAINS DISABLED)
# -----------------------------------------------------------------------------
echo "[STEP 5/11] Deploying validated code files while sending flags remain disabled..."
DEPLOYMENT_MODIFIED=1

while read -r svc rel m u g h; do
  [ -z "$svc" ] && continue
  src="${SOURCE_RELEASE_DIR}/${rel}"
  if [ "$svc" = "FLASK" ]; then
    dst="${FLASK_DIR}/${rel}"
  else
    dst="${WA_DIR}/${rel}"
  fi
  cp "${src}" "${dst}"
  chmod "$m" "$dst"
  chown "${u}:${g}" "$dst"
done < "$BACKUP_MANIFEST"

echo "[OK] Code files deployed with original ownership and permissions preserved."

# -----------------------------------------------------------------------------
# STEP 6: RUN SYNTAX CHECKS AND VERIFY SAFEGUARDS IN DEPLOYED FILES
# -----------------------------------------------------------------------------
echo "[STEP 6/11] Verifying safeguards and compiling installed files..."
grep -q 'def _evaluate_recipients_consent' "${FLASK_DIR}/app.py" || { echo "[ABORT] Consent gate missing in deployed app.py"; exit 1; }
grep -q 'shop_consent_declarations' "${FLASK_DIR}/database.py" || { echo "[ABORT] Consent table missing in deployed database.py"; exit 1; }
grep -q 'whatsapp_opt_outs' "${FLASK_DIR}/database.py" || { echo "[ABORT] Opt-out table missing in deployed database.py"; exit 1; }
grep -q 'verifyDispatchAuthorizationToken' "${WA_DIR}/server.js" || { echo "[ABORT] Auth verification missing in deployed server.js"; exit 1; }
grep -q 'consumeDispatchAuthorization' "${WA_DIR}/whatsapp_ledger.js" || { echo "[ABORT] Ledger auth consumption missing in deployed ledger"; exit 1; }

"$FLASK_PYTHON" -m py_compile "${FLASK_DIR}/app.py" || { echo "[ABORT] Compilation failed on app.py"; exit 1; }
"$FLASK_PYTHON" -m py_compile "${FLASK_DIR}/database.py" || { echo "[ABORT] Compilation failed on database.py"; exit 1; }
node --check "${WA_DIR}/server.js" || { echo "[ABORT] Node check failed on server.js"; exit 1; }
node --check "${WA_DIR}/whatsapp_ledger.js" || { echo "[ABORT] Node check failed on whatsapp_ledger.js"; exit 1; }
echo "[OK] Deployed code safeguards and syntax verified."

# -----------------------------------------------------------------------------
# STEP 7: VERIFY REQUIRED SCHEMA WITHOUT MUTATING THE LIVE DATABASE
# -----------------------------------------------------------------------------
echo "[STEP 7/11] Verifying required schema read-only on $LIVE_DB_PATH..."
sudo -u "$FLASK_USER" "$FLASK_PYTHON" -c "
import os, sys, sqlite3
env_path = '${FLASK_ENV_FILE}'
if os.path.isfile(env_path):
    with open(env_path, 'r', encoding='utf-8') as fp:
        for line in fp:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip().strip('\'\"'))
sys.path.insert(0, '${FLASK_DIR}')
import database
assert os.path.realpath(database.DB_PATH) == os.path.realpath('${LIVE_DB_PATH}'), 'Database mismatch'
db_path = os.path.realpath('${LIVE_DB_PATH}')
conn = sqlite3.connect('file:' + db_path + '?mode=ro', uri=True)
try:
    database.verify_required_schema(conn)
finally:
    conn.close()
print('[OK] Required schema verified without applying migrations.')
"
sqlite3 "$LIVE_DB_PATH" "PRAGMA query_only=ON; PRAGMA quick_check;" | grep -q 'ok' || { echo "[ABORT] Live database read-only integrity check failed"; exit 1; }

# -----------------------------------------------------------------------------
# STEP 8: VERIFY SCHEMA AND APPLICATION HEALTH BEFORE ENABLING SENDING
# -----------------------------------------------------------------------------
echo "[STEP 8/11] Verifying application health endpoint with new code and schema..."
systemctl restart taskpe.service
FLASK_PRE_HEALTH=$(curl -fsS --max-time 5 http://127.0.0.1:5000/health || true)
echo "$FLASK_PRE_HEALTH" | grep -q '"status":[[:space:]]*"ok"' || {
  echo "[ABORT] Application /health endpoint unhealthy prior to flag activation: $FLASK_PRE_HEALTH"; exit 1;
}

# -----------------------------------------------------------------------------
# STEP 9: PRESERVE DISABLED WHATSAPP SENDING FLAGS
# -----------------------------------------------------------------------------
echo "[STEP 9/11] Confirming WhatsApp sending flags remain disabled..."
assert_whatsapp_send_flag_disabled "$FLASK_ENV_FILE" "TASKPE_WHATSAPP_LINKED_DEVICE_ENABLED"
assert_whatsapp_send_flag_disabled "$WA_ENV_FILE" "TASKPE_WA_CAMPAIGN_BATCH_HTTP_SEND_ENABLED"
echo "[OK] No WhatsApp sending flag was changed; a separate approved release process is required to change them."

# -----------------------------------------------------------------------------
# STEP 10: RESTART SERVICES IN ORDER AND VERIFY FULL HEALTH
# -----------------------------------------------------------------------------
echo "[STEP 10/11] Restarting services in dependency order..."
systemctl restart taskpe-whatsapp.service
systemctl restart taskpe.service

systemctl is-active --quiet taskpe-whatsapp.service || { echo "[FAIL] taskpe-whatsapp service failed to start"; exit 1; }
systemctl is-active --quiet taskpe.service || { echo "[FAIL] taskpe service failed to start"; exit 1; }

assert_whatsapp_send_flag_disabled "$FLASK_ENV_FILE" "TASKPE_WHATSAPP_LINKED_DEVICE_ENABLED"
assert_whatsapp_send_flag_disabled "$WA_ENV_FILE" "TASKPE_WA_CAMPAIGN_BATCH_HTTP_SEND_ENABLED"

# Verify Node listener strictly bound to 127.0.0.1
NODE_PORT=$(grep '^TASKPE_WA_SERVICE_PORT=' "$WA_ENV_FILE" 2>/dev/null | cut -d '=' -f2- | tr -d '[:space:]\"' || true)
NODE_PORT=${NODE_PORT:-8765}
ss -tulpn | grep ":${NODE_PORT} " | grep -q "127.0.0.1" || {
  echo "[FAIL] Node WhatsApp microservice is not bound strictly to 127.0.0.1:${NODE_PORT}"; exit 1;
}

# Verify Flask application health check
FLASK_FINAL_HEALTH=$(curl -fsS --max-time 5 http://127.0.0.1:5000/health || true)
echo "$FLASK_FINAL_HEALTH" | grep -q '"status":[[:space:]]*"ok"' || {
  echo "[FAIL] Final /health check failed: $FLASK_FINAL_HEALTH"; exit 1;
}

# -----------------------------------------------------------------------------
# STEP 11: FINAL CONFIRMATION
# -----------------------------------------------------------------------------
DEPLOYMENT_MODIFIED=0
trap - ERR EXIT

echo "============================================================================="
echo "[SUCCESS] Deployment completed safely and verified operational."
echo "Deployment Record: $DEPLOY_REC"
echo "Rollback Command:  sudo ./rollback.sh $DEPLOY_REC"
echo "============================================================================="
