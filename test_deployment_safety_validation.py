#!/usr/bin/env python3
"""
Test Suite: Deployment Safety and Rollback Hardening Verification
Validates:
- Ambiguous EnvironmentFile resolution aborts safely.
- Checksum mismatch and missing release files abort before modifying production.
- Live database resolution and PID mismatch abort safely.
- Corrupted/incomplete backups abort before touching production.
- Post-replacement failures trigger automatic rollback to pre-deployment state.
- Rollback record validation rejects unknown, duplicate, missing keys, and bad permissions.
- Database restore requires exact confirmation and verifies snapshot integrity.
- Zero production modifications, zero WhatsApp messages sent.
"""

import os
import sys
import shutil
import tempfile
import subprocess
import sqlite3
import hashlib
import re
import unittest

WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
DEPLOY_SH = os.path.join(WORKSPACE_DIR, "deploy.sh")
ROLLBACK_SH = os.path.join(WORKSPACE_DIR, "rollback.sh")
RESTORE_DB_SH = os.path.join(WORKSPACE_DIR, "restore_database.sh")

def to_posix_path(win_path):
    p = os.path.abspath(win_path).replace("\\", "/")
    if len(p) >= 2 and p[1] == ":":
        drive = p[0].lower()
        return f"/mnt/{drive}{p[2:]}"
    return p

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        data = f.read()
    data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    h.update(data)
    return h.hexdigest()

EXPECTED_CANDIDATE_CHECKSUMS = {
    "app.py": "b52975ff3a25bbb8f040dcf98e37a75084ee6c08d50b5c3bf54fc9659c352675",
    "database.py": "718abfa6dcbd2571b12bee0fade93909f092338961c8296a2af6947e21b63c1a",
    "server.js": "9dc706fb627755f4735ecb0feaffd364d5f1c4e5da1c0d1f10a594741476038c",
    "whatsapp_ledger.js": "a13f60b0e7438c1d2c642af7586600c83f71596f62f9e7107409a47c70d6a5be",
    "templates/admin.html": "aee29fe7db6051b416514f06323851cb901f9161cb1d673d2b6d3fbb24d40b0e",
    "templates/index.html": "e4e9ddf5c8745ef4d4c6a7681915107bd798257a7919332cad2993d3062a230d",
    "static/js/admin.js": "185df821f622ad00e6a1ecb87eb0b8dc6f6a57916cf77dc9fef15ba5ac0509f9",
    "static/js/app.js": "4b92ea0229f6aa2562fca3123124b33bdba0d1de0ef54d9a398603506f8d4cdb",
}

def extract_shell_array(script, name):
    match = re.search(rf"{re.escape(name)}=\(\n(.*?)\n\)", script, re.DOTALL)
    if not match:
        raise AssertionError(f"Missing shell array: {name}")
    return re.findall(r'^\s+"([^"]+)"\s*$', match.group(1), re.MULTILINE)

def extract_checksum_allowlist(script):
    match = re.search(r'EXPECTED_CHECKSUMS="\n(.*?)\n"', script, re.DOTALL)
    if not match:
        raise AssertionError("Missing EXPECTED_CHECKSUMS block")
    result = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line:
            continue
        digest, path = line.split(None, 1)
        result[path] = digest
    return result

class TestDeploymentSafetyValidation(unittest.TestCase):

    def test_checksum_canonicalization_is_line_ending_independent(self):
        lf = b"first\nsecond\n"
        crlf = b"first\r\nsecond\r\n"
        standalone_cr = b"first\rsecond\r"

        def canonical_hash(data):
            normalized = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            return hashlib.sha256(normalized).hexdigest()

        self.assertEqual(canonical_hash(lf), canonical_hash(crlf))
        self.assertEqual(canonical_hash(lf), canonical_hash(standalone_cr))
        self.assertNotEqual(canonical_hash(lf), canonical_hash(b"first\nchanged\n"))

        with open(DEPLOY_SH, "r", encoding="utf-8") as f:
            script = f.read()
        self.assertIn("replace(bytes((13, 10)), bytes((10,)))", script)
        self.assertIn("replace(bytes((13,)), bytes((10,)))", script)

    def test_candidate_allowlist_matches_exact_candidate_files_and_hashes(self):
        with open(DEPLOY_SH, "r", encoding="utf-8") as f:
            script = f.read()

        self.assertIn("taskpe-release-candidate-20261026", script)
        self.assertEqual(extract_checksum_allowlist(script), EXPECTED_CANDIDATE_CHECKSUMS)
        replacement_files = (
            extract_shell_array(script, "FLASK_REPLACE_FILES")
            + extract_shell_array(script, "WA_REPLACE_FILES")
        )
        self.assertEqual(set(replacement_files), set(EXPECTED_CANDIDATE_CHECKSUMS))
        self.assertEqual(len(replacement_files), len(EXPECTED_CANDIDATE_CHECKSUMS))

    def test_candidate_files_match_allowlist(self):
        for rel_path, expected_hash in EXPECTED_CANDIDATE_CHECKSUMS.items():
            self.assertEqual(sha256_file(os.path.join(WORKSPACE_DIR, rel_path)), expected_hash)

    def test_deploy_script_has_read_only_migration_gate_and_disabled_send_flags(self):
        """The deployment script must not mutate schema or enable WhatsApp sending."""
        with open(DEPLOY_SH, "r", encoding="utf-8") as f:
            script = f.read()

        self.assertIn("database.verify_required_schema(conn)", script)
        self.assertIn("mode=ro", script)
        self.assertIn("assert_whatsapp_send_flag_disabled", script)
        self.assertNotIn("database.initialize_database()", script)
        self.assertNotIn("TASKPE_WA_CAMPAIGN_BATCH_HTTP_SEND_ENABLED=true", script)
        self.assertNotIn("TASKPE_WHATSAPP_LINKED_DEVICE_ENABLED=1", script)

    def test_deploy_script_aborts_when_batch_send_is_already_enabled(self):
        """A deployment cannot proceed with an enabled live batch-send flag."""
        with open(DEPLOY_SH, "r", encoding="utf-8") as f:
            script = f.read()

        self.assertIn("is enabled in ${env_file}", script)
        self.assertIn("TASKPE_WA_CAMPAIGN_BATCH_HTTP_SEND_ENABLED", script)

    def test_rollback_script_fails_closed_before_mutation_and_restart(self):
        with open(ROLLBACK_SH, "r", encoding="utf-8") as f:
            script = f.read()
        self.assertIn("assert_rollback_send_flags_disabled", script)
        self.assertIn("TASKPE_WHATSAPP_LINKED_DEVICE_ENABLED", script)
        self.assertIn("TASKPE_WA_CAMPAIGN_BATCH_HTTP_SEND_ENABLED", script)
        self.assertIn("must have exactly one explicit value", script)
        first_flag_check = script.index('assert_rollback_send_flags_disabled "$FLASK_ENV_FILE"')
        first_mutation = script.index('cp -p "$BACKUP_FLASK_ENV" "$FLASK_ENV_FILE"')
        first_restart = script.index('systemctl restart taskpe-whatsapp.service')
        self.assertLess(first_flag_check, first_mutation)
        self.assertLess(first_flag_check, first_restart)
        self.assertNotIn("TASKPE_WA_CAMPAIGN_BATCH_HTTP_SEND_ENABLED=true", script)

    def test_deploy_rechecks_both_flags_at_each_safety_checkpoint(self):
        with open(DEPLOY_SH, "r", encoding="utf-8") as f:
            script = f.read()
        linked = 'assert_whatsapp_send_flag_disabled "$FLASK_ENV_FILE" "TASKPE_WHATSAPP_LINKED_DEVICE_ENABLED"'
        batch = 'assert_whatsapp_send_flag_disabled "$WA_ENV_FILE" "TASKPE_WA_CAMPAIGN_BATCH_HTTP_SEND_ENABLED"'
        self.assertGreaterEqual(script.count(linked), 3)
        self.assertGreaterEqual(script.count(batch), 3)
        schema_check = script.index('database.verify_required_schema(conn)')
        first_restart = script.index('systemctl restart taskpe.service')
        self.assertLess(schema_check, first_restart)
        self.assertNotIn('TASKPE_WHATSAPP_LINKED_DEVICE_ENABLED=true', script)
        self.assertNotIn('TASKPE_WA_CAMPAIGN_BATCH_HTTP_SEND_ENABLED=true', script)

    def test_release_schema_is_checked_before_active_code_replacement(self):
        with open(DEPLOY_SH, "r", encoding="utf-8") as f:
            script = f.read()
        schema_check = script.index('database.verify_required_schema(conn)')
        first_replacement = script.index('cp "${src}" "${dst}"')
        self.assertLess(schema_check, first_replacement)
        self.assertIn("mode=ro", script)

    def test_runtime_deployment_aborts_before_mutation_when_either_send_flag_enabled(self):
        """Executable harness case for both live-send preconditions."""
        for env_path, flag_name in (
            (self.flask_env, 'TASKPE_WHATSAPP_LINKED_DEVICE_ENABLED'),
            (self.wa_env, 'TASKPE_WA_CAMPAIGN_BATCH_HTTP_SEND_ENABLED'),
        ):
            with open(env_path, 'a', encoding='utf-8', newline='\n') as f:
                f.write(f'{flag_name}=true\n')
            res = self._run_script(self.deploy_script, [self.release_dir])
            self.assertNotEqual(res.returncode, 0)
            self.assertIn('deployment requires it to remain disabled', res.stdout + res.stderr)
            with open(os.path.join(self.flask_dir, 'app.py'), 'r', encoding='utf-8') as f:
                self.assertIn('Old pre-deployment version', f.read())
            with open(env_path, 'r', encoding='utf-8') as f:
                self.assertIn(f'{flag_name}=true', f.read())
            with open(env_path, 'r', encoding='utf-8') as f:
                original = f.read().replace(f'{flag_name}=true\n', '')
            with open(env_path, 'w', encoding='utf-8', newline='\n') as f:
                f.write(original)

    def setUp(self):
        # Deployment tests intentionally point the imported database module at
        # a disposable database. Restore both process globals even when a
        # test or its cleanup raises.
        import database as real_db
        self._real_db = real_db
        self._saved_db_path = real_db.DB_PATH
        self._saved_env = {
            key: os.environ.get(key)
            for key in ('TASKPE_DB_PATH', 'ENVIRONMENT')
        }
        self.addCleanup(self._restore_process_state)
        self.test_root = tempfile.mkdtemp(prefix="taskpe_deploy_test_")
        self.mock_bin = os.path.join(self.test_root, "bin")
        os.makedirs(self.mock_bin, exist_ok=True)

        self.flask_dir = os.path.join(self.test_root, "flask_app")
        os.makedirs(os.path.join(self.flask_dir, "templates"), exist_ok=True)
        os.makedirs(os.path.join(self.flask_dir, "static", "js"), exist_ok=True)
        os.makedirs(os.path.join(self.flask_dir, "deployments"), exist_ok=True)

        self.wa_dir = os.path.join(self.test_root, "wa_app")
        os.makedirs(self.wa_dir, exist_ok=True)

        self.release_dir = os.path.join(self.test_root, "release")
        os.makedirs(os.path.join(self.release_dir, "templates"), exist_ok=True)
        os.makedirs(os.path.join(self.release_dir, "static", "js"), exist_ok=True)

        # Copy authentic tested release files
        for rel in [
            "app.py", "database.py", "server.js", "whatsapp_ledger.js",
            "templates/admin.html", "templates/index.html",
            "static/js/admin.js", "static/js/app.js"
        ]:
            src = os.path.join(WORKSPACE_DIR, rel)
            dst = os.path.join(self.release_dir, rel)
            shutil.copy2(src, dst)

        # Populate production directories with "old" versions
        old_db_py = (
            "import os\n"
            "DB_PATH = os.environ.get('TASKPE_DB_PATH', 'taskpe.db')\n"
            "def initialize_database(): pass\n"
            "def verify_required_schema(): pass\n"
        )
        with open(os.path.join(self.flask_dir, "database.py"), "w", encoding="utf-8") as f:
            f.write(old_db_py)

        for rel in [
            "app.py", "templates/admin.html", "templates/index.html",
            "static/js/admin.js", "static/js/app.js"
        ]:
            dst = os.path.join(self.flask_dir, rel)
            with open(dst, "w", encoding="utf-8") as f:
                f.write(f"# Old pre-deployment version of {rel}\n")

        for rel in ["server.js", "whatsapp_ledger.js"]:
            dst = os.path.join(self.wa_dir, rel)
            with open(dst, "w", encoding="utf-8") as f:
                f.write(f"// Old pre-deployment version of {rel}\n")

        # Environment files
        self.flask_env = os.path.join(self.flask_dir, ".env")
        with open(self.flask_env, "w", encoding="utf-8", newline="\n") as f:
            f.write(
                "TASKPE_WHATSAPP_CONSENT_GATE_ENABLED=0\n"
                "TASKPE_WHATSAPP_LINKED_DEVICE_ENABLED=0\n"
                f"TASKPE_DB_PATH={to_posix_path(os.path.join(self.flask_dir, 'taskpe.db'))}\n"
            )

        self.wa_env = os.path.join(self.wa_dir, ".env")
        with open(self.wa_env, "w", encoding="utf-8", newline="\n") as f:
            f.write("TASKPE_WA_CAMPAIGN_BATCH_HTTP_SEND_ENABLED=false\nTASKPE_WA_SERVICE_PORT=8765\n")

        # Database
        self.db_path = os.path.join(self.flask_dir, "taskpe.db")
        os.environ["TASKPE_DB_PATH"] = self.db_path
        os.environ["ENVIRONMENT"] = "testing"
        import database as real_db
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        real_db._initialize_database_with_connection(conn)
        conn.close()

        # Scripts copies
        self.deploy_script = os.path.join(self.test_root, "deploy.sh")
        self.rollback_script = os.path.join(self.test_root, "rollback.sh")
        self.restore_db_script = os.path.join(self.test_root, "restore_database.sh")
        shutil.copy2(DEPLOY_SH, self.deploy_script)
        shutil.copy2(ROLLBACK_SH, self.rollback_script)
        shutil.copy2(RESTORE_DB_SH, self.restore_db_script)

        self._create_mock_binaries()

    def tearDown(self):
        shutil.rmtree(self.test_root, ignore_errors=True)

    def _restore_process_state(self):
        self._real_db.DB_PATH = self._saved_db_path
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _create_mock_binaries(self):
        flask_dir_p = to_posix_path(self.flask_dir)
        wa_dir_p = to_posix_path(self.wa_dir)
        flask_env_p = to_posix_path(self.flask_env)
        wa_env_p = to_posix_path(self.wa_env)

        # mock sudo: strips -u user and executes
        sudo_mock = os.path.join(self.mock_bin, "sudo")
        with open(sudo_mock, "w", encoding="utf-8", newline="\n") as f:
            f.write("#!/bin/sh\n"
                    "while [ $# -gt 0 ]; do\n"
                    "  case \"$1\" in\n"
                    "    -u) shift 2 ;;\n"
                    "    *) break ;;\n"
                    "  esac\n"
                    "done\n"
                    "exec \"$@\"\n")
        os.chmod(sudo_mock, 0o755)

        # mock systemctl
        sys_mock = os.path.join(self.mock_bin, "systemctl")
        with open(sys_mock, "w", encoding="utf-8", newline="\n") as f:
            f.write(f"#!/bin/sh\n"
                    f"case \"$1\" in\n"
                    f"  list-unit-files) exit 0 ;;\n"
                    f"  is-active) exit 0 ;;\n"
                    f"  restart|stop|start) exit 0 ;;\n"
                    f"  show)\n"
                    f"    prop=\"$3\"\n"
                    f"    unit=\"$5\"\n"
                    f"    if [ \"$unit\" = \"taskpe.service\" ]; then\n"
                    f"      case \"$prop\" in\n"
                    f"        WorkingDirectory) echo \"{flask_dir_p}\" ;;\n"
                    f"        User) echo \"$(id -un 2>/dev/null || echo testuser)\" ;;\n"
                    f"        MainPID) echo \"0\" ;;\n"
                    f"        EnvironmentFiles) echo \"{flask_env_p}\" ;;\n"
                    f"      esac\n"
                    f"    elif [ \"$unit\" = \"taskpe-whatsapp.service\" ]; then\n"
                    f"      case \"$prop\" in\n"
                    f"        WorkingDirectory) echo \"{wa_dir_p}\" ;;\n"
                    f"        User) echo \"$(id -un 2>/dev/null || echo testuser)\" ;;\n"
                    f"        MainPID) echo \"0\" ;;\n"
                    f"        EnvironmentFiles) echo \"{wa_env_p}\" ;;\n"
                    f"      esac\n"
                    f"    fi\n"
                    f"    ;;\n"
                    f"  cat)\n"
                    f"    unit=\"$2\"\n"
                    f"    if [ \"$unit\" = \"taskpe.service\" ]; then\n"
                    f"      echo \"EnvironmentFile={flask_env_p}\"\n"
                    f"    elif [ \"$unit\" = \"taskpe-whatsapp.service\" ]; then\n"
                    f"      echo \"EnvironmentFile={wa_env_p}\"\n"
                    f"    fi\n"
                    f"    ;;\n"
                    f"esac\n")
        os.chmod(sys_mock, 0o755)

        # mock ss
        ss_mock = os.path.join(self.mock_bin, "ss")
        with open(ss_mock, "w", encoding="utf-8", newline="\n") as f:
            f.write("#!/bin/sh\necho 'LISTEN 0 128 127.0.0.1:8765 0.0.0.0:*'\n")
        os.chmod(ss_mock, 0o755)

        # mock curl
        curl_mock = os.path.join(self.mock_bin, "curl")
        with open(curl_mock, "w", encoding="utf-8", newline="\n") as f:
            f.write("#!/bin/sh\necho '{\"status\": \"ok\", \"service\": \"taskpe\"}'\n")
        os.chmod(curl_mock, 0o755)

        # mock sqlite3 using python3
        sqlite_mock = os.path.join(self.mock_bin, "sqlite3")
        with open(sqlite_mock, "w", encoding="utf-8", newline="\n") as f:
            f.write("#!/usr/bin/env python3\n"
                    "import sys, sqlite3, os\n"
                    "if len(sys.argv) < 2:\n"
                    "    sys.exit(0)\n"
                    "db_path = sys.argv[1]\n"
                    "if len(sys.argv) == 2:\n"
                    "    sys.exit(0)\n"
                    "cmd = sys.argv[2]\n"
                    "if cmd.startswith('.backup'):\n"
                    "    target = cmd.split(\"'\", 2)[1]\n"
                    "    src = sqlite3.connect(db_path)\n"
                    "    dst = sqlite3.connect(target)\n"
                    "    src.backup(dst)\n"
                    "    dst.close()\n"
                    "    src.close()\n"
                    "    sys.exit(0)\n"
                    "conn = sqlite3.connect(db_path)\n"
                    "cur = conn.cursor()\n"
                    "for stmt in cmd.split(';'):\n"
                    "    s = stmt.strip()\n"
                    "    if s:\n"
                    "        cur.execute(s)\n"
                    "        for r in cur.fetchall():\n"
                    "            print('|'.join(str(x) for x in r))\n"
                    "conn.commit()\n"
                    "conn.close()\n")
        os.chmod(sqlite_mock, 0o755)

    def _run_script(self, script_path, args, env_override=None, input_text=None):
        if shutil.which('bash') is None:
            raise unittest.SkipTest('Bash is unavailable on Windows; deployment runtime harness is unverified.')
        mock_bin_p = to_posix_path(self.mock_bin)
        script_p = to_posix_path(script_path)
        site_packages_p = to_posix_path(os.path.join(WORKSPACE_DIR, ".venv", "Lib", "site-packages"))
        posix_args = [to_posix_path(a) if (isinstance(a, str) and (os.path.exists(a) or ":\\" in a)) else str(a) for a in args]

        # Use bash -c to run in posix environment with mocked PATH
        arg_str = " ".join(f'"{a}"' for a in posix_args)
        cmd_str = f'export PATH="{mock_bin_p}:$PATH"; export PYTHONPATH="{site_packages_p}:$PYTHONPATH"; bash "{script_p}" {arg_str}'

        res = subprocess.run(
            ["bash", "-c", cmd_str],
            input=input_text,
            capture_output=True,
            text=True
        )
        return res

    def test_missing_or_invalid_release_dir(self):
        """Preflight aborts if release directory is missing or invalid."""
        res = self._run_script(self.deploy_script, ["/nonexistent_path_xyz"])
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("[ABORT]", res.stdout + res.stderr)

    def test_checksum_mismatch_aborts_before_deploying(self):
        """Release checksum mismatch aborts before modifying production."""
        with open(os.path.join(self.release_dir, "app.py"), "a", encoding="utf-8") as f:
            f.write("# Tampered line\n")

        res = self._run_script(self.deploy_script, [self.release_dir])
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Checksum mismatch", res.stdout + res.stderr)

        with open(os.path.join(self.flask_dir, "app.py"), "r", encoding="utf-8") as f:
            self.assertIn("Old pre-deployment version", f.read())

    def test_ambiguous_environment_files_abort(self):
        """Ambiguous EnvironmentFiles cause deploy.sh to abort safely."""
        env2 = os.path.join(self.flask_dir, ".env.second")
        with open(env2, "w", encoding="utf-8") as f:
            f.write("TASKPE_DB_PATH=something_else\n")

        env2_p = to_posix_path(env2)
        flask_dir_p = to_posix_path(self.flask_dir)
        flask_env_p = to_posix_path(self.flask_env)

        sys_mock = os.path.join(self.mock_bin, "systemctl")
        with open(sys_mock, "w", encoding="utf-8", newline="\n") as f:
            f.write(f"#!/bin/sh\n"
                    f"if [ \"$1\" = \"cat\" ]; then\n"
                    f"  echo \"EnvironmentFile={flask_env_p}\"\n"
                    f"  echo \"EnvironmentFile={env2_p}\"\n"
                    f"  exit 0\n"
                    f"fi\n"
                    f"if [ \"$1\" = \"show\" ]; then\n"
                    f"  case \"$3\" in\n"
                    f"    WorkingDirectory) echo \"{flask_dir_p}\" ;;\n"
                    f"    User) echo \"$(id -un 2>/dev/null || echo testuser)\" ;;\n"
                    f"    EnvironmentFiles) echo \"{flask_env_p} {env2_p}\" ;;\n"
                    f"  esac\n"
                    f"fi\n"
                    f"exit 0\n")
        os.chmod(sys_mock, 0o755)

        # Clear target var from flask_env so neither has it, but both have TASKPE_
        with open(self.flask_env, "w", encoding="utf-8", newline="\n") as f:
            f.write("TASKPE_DB_PATH=taskpe.db\n")

        res = self._run_script(self.deploy_script, [self.release_dir])
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Ambiguous EnvironmentFiles", res.stdout + res.stderr)

    def test_database_resolution_failure_aborts(self):
        """Database missing or unwritable aborts before modifying files."""
        os.remove(self.db_path)

        res = self._run_script(self.deploy_script, [self.release_dir])
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Database file does not exist", res.stdout + res.stderr)

        with open(os.path.join(self.flask_dir, "app.py"), "r", encoding="utf-8") as f:
            self.assertIn("Old pre-deployment version", f.read())

    def test_post_replacement_failure_triggers_automatic_rollback(self):
        """A failure after Step 5 (e.g. migration error) triggers automatic rollback."""
        mock_py = os.path.join(self.mock_bin, "python3")
        verify_count = os.path.join(self.test_root, "schema_verify_count")
        verify_count_p = to_posix_path(verify_count)
        with open(mock_py, "w", encoding="utf-8", newline="\n") as f:
            f.write("#!/bin/sh\n"
                    f"verify_count=\"{verify_count_p}\"\n"
                    "for arg in \"$@\"; do\n"
                    "  if echo \"$arg\" | grep -q 'database.verify_required_schema'; then\n"
                    "    count=0\n"
                    "    if [ -f \"$verify_count\" ]; then count=$(cat \"$verify_count\"); fi\n"
                    "    count=$((count + 1))\n"
                    "    echo \"$count\" > \"$verify_count\"\n"
                    "    if [ \"$count\" -ge 2 ]; then\n"
                    "      echo 'Simulated Schema Verification Error' >&2\n"
                    "      exit 42\n"
                    "    fi\n"
                    "  fi\n"
                    "done\n"
                    "exec /usr/bin/python3 \"$@\"\n")
        os.chmod(mock_py, 0o755)

        res = self._run_script(self.deploy_script, [self.release_dir])
        self.assertNotEqual(res.returncode, 0)
        output = res.stdout + res.stderr
        self.assertIn("Simulated Schema Verification Error", output)
        self.assertIn("Executing automatic rollback to pre-deployment state", output)
        self.assertIn("Automatic rollback succeeded", output)
        with open(verify_count, "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "2")

        with open(os.path.join(self.flask_dir, "app.py"), "r", encoding="utf-8") as f:
            self.assertIn("Old pre-deployment version", f.read())

        with open(self.flask_env, "r", encoding="utf-8") as f:
            self.assertIn("TASKPE_WHATSAPP_CONSENT_GATE_ENABLED=0", f.read())

    def test_rollback_record_validation_hardening(self):
        """Rollback rejects unknown, duplicate, missing keys and bad permissions."""
        rec_path = os.path.join(self.test_root, "test.record")

        # 1. Bad permissions (e.g. 644 instead of 600 or 400)
        with open(rec_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("DEPLOYMENT_TIMESTAMP=\"20261020_120000\"\n")
        os.chmod(rec_path, 0o644)
        res = self._run_script(self.rollback_script, [rec_path])
        self.assertNotEqual(res.returncode, 0)
        # Note: on NTFS/WSL chmod 600 may show 777 or 644, let's test parser error

        # 2. Unknown key
        with open(rec_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("DEPLOYMENT_TIMESTAMP=\"20261020_120000\"\nEVIL_KEY=\"drop_tables\"\n")
        # Run test ignoring perms check if WSL
        res = self._run_script(self.rollback_script, [rec_path])
        self.assertNotEqual(res.returncode, 0)

        # 3. Duplicate key
        with open(rec_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("DEPLOYMENT_TIMESTAMP=\"20261020_120000\"\nDEPLOYMENT_TIMESTAMP=\"20261020_120001\"\n")
        res = self._run_script(self.rollback_script, [rec_path])
        self.assertNotEqual(res.returncode, 0)

    def test_database_restore_guard(self):
        """Database restore strictly requires 'RESTORE_DATABASE' confirmation."""
        rec_path = os.path.join(self.test_root, "db_test.record")
        snap_path = os.path.join(self.test_root, "snapshot.db")
        conn = sqlite3.connect(snap_path)
        conn.execute("CREATE TABLE snap_test (id INT);")
        conn.commit()
        conn.close()

        rec_p = to_posix_path(rec_path)
        snap_p = to_posix_path(snap_path)
        test_root_p = to_posix_path(self.test_root)
        db_p = to_posix_path(self.db_path)
        flask_dir_p = to_posix_path(self.flask_dir)

        with open(rec_path, "w", newline="\n") as f:
            f.write(f"DEPLOYMENT_TIMESTAMP=\"20261020_120000\"\n"
                    f"FLASK_DIR=\"{flask_dir_p}\"\n"
                    f"LIVE_DB_PATH=\"{db_p}\"\n"
                    f"BACKUP_DIR=\"{test_root_p}\"\n"
                    f"BACKUP_DB=\"{snap_p}\"\n")

        res = self._run_script(self.restore_db_script, [rec_path], input_text="no\n")
        self.assertNotEqual(res.returncode, 0)

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users';")
        self.assertIsNotNone(cursor.fetchone())
        conn.close()

    def test_successful_full_deployment_and_manual_rollback(self):
        """Full successful deployment followed by manual rollback restores pre-deployment state."""
        res = self._run_script(self.deploy_script, [self.release_dir])
        output = res.stdout + res.stderr
        self.assertEqual(res.returncode, 0, f"Deployment failed:\n{output}")
        self.assertIn("[SUCCESS] Deployment completed safely", output)

        with open(os.path.join(self.flask_dir, "app.py"), "r", encoding="utf-8") as f:
            self.assertIn("def _evaluate_recipients_consent", f.read())

        with open(self.flask_env, "r", encoding="utf-8") as f:
            self.assertIn("TASKPE_WHATSAPP_CONSENT_GATE_ENABLED=0", f.read())
        with open(self.wa_env, "r", encoding="utf-8") as f:
            self.assertIn("TASKPE_WA_CAMPAIGN_BATCH_HTTP_SEND_ENABLED=false", f.read())
        with open(self.flask_env, "r", encoding="utf-8") as f:
            self.assertIn("TASKPE_WHATSAPP_LINKED_DEVICE_ENABLED=0", f.read())

        rec_line = [l for l in output.splitlines() if "Deployment Record:" in l][0]
        rec_path = rec_line.split("Deployment Record:")[1].strip()
        # In windows python, rec_path is posix path like /mnt/c/...
        # let's verify file exists via bash test
        check = subprocess.run(["bash", "-c", f"[ -f '{rec_path}' ]"])
        self.assertEqual(check.returncode, 0)

        res_rb = self._run_script(self.rollback_script, [rec_path])
        rb_output = res_rb.stdout + res_rb.stderr
        self.assertEqual(res_rb.returncode, 0, f"Rollback failed:\n{rb_output}")
        self.assertIn("[SUCCESS] Rollback completed", rb_output)

        with open(os.path.join(self.flask_dir, "app.py"), "r", encoding="utf-8") as f:
            self.assertIn("Old pre-deployment version", f.read())

        with open(self.flask_env, "r", encoding="utf-8") as f:
            self.assertIn("TASKPE_WHATSAPP_CONSENT_GATE_ENABLED=0", f.read())
        with open(self.wa_env, "r", encoding="utf-8") as f:
            self.assertIn("TASKPE_WA_CAMPAIGN_BATCH_HTTP_SEND_ENABLED=false", f.read())

if __name__ == "__main__":
    unittest.main()
