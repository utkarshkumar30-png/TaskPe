"""Disposable-database tests for the explicit consent-history migration."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

import migrate_whatsapp_consent_declaration_history as migration


class TestWhatsAppConsentDeclarationMigration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="taskpe_consent_migration_")
        self.db_path = Path(self.temp_dir.name) / "legacy.db"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_legacy_database(self, *, malformed=False):
        conn = sqlite3.connect(self.db_path)
        created_at_column = "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP," if not malformed else ""
        conn.executescript(
            f"""
            PRAGMA foreign_keys = ON;
            CREATE TABLE users (id INTEGER PRIMARY KEY);
            CREATE TABLE tasks (id INTEGER PRIMARY KEY);
            INSERT INTO users (id) VALUES (7), (8);
            INSERT INTO tasks (id) VALUES (101), (102);
            CREATE TABLE whatsapp_campaign_consent_declarations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL UNIQUE,
                campaign_id INTEGER NOT NULL,
                business_user_id INTEGER NOT NULL,
                declaration_text TEXT NOT NULL,
                normalized_recipient_list TEXT NOT NULL,
                recipient_count INTEGER NOT NULL CHECK (recipient_count > 0),
                recipient_fingerprint TEXT NOT NULL CHECK (length(recipient_fingerprint) = 64),
                {created_at_column}
                FOREIGN KEY (task_id) REFERENCES tasks(id),
                FOREIGN KEY (business_user_id) REFERENCES users(id)
            );
            CREATE INDEX whatsapp_campaign_consent_lookup
                ON whatsapp_campaign_consent_declarations(business_user_id, task_id, campaign_id);
            """
        )
        if malformed:
            conn.execute(
                """INSERT INTO whatsapp_campaign_consent_declarations
                   (task_id, campaign_id, business_user_id, declaration_text,
                    normalized_recipient_list, recipient_count, recipient_fingerprint)
                   VALUES (101, 201, 7, 'confirmed', '[\"+14155550101\"]', 1, ?)""",
                ("a" * 64,),
            )
        else:
            rows = [
                (101, 201, 7, "confirmed one", '["+14155550101"]', "a" * 64),
                (102, 202, 8, "confirmed two", '["+442079460018"]', "b" * 64),
            ]
            conn.executemany(
                """INSERT INTO whatsapp_campaign_consent_declarations
                   (task_id, campaign_id, business_user_id, declaration_text,
                    normalized_recipient_list, recipient_count, recipient_fingerprint)
                   VALUES (?, ?, ?, ?, ?, 1, ?)""",
                rows,
            )
        conn.commit()
        conn.close()

    def _rows(self, table):
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            f"SELECT id, task_id, campaign_id, business_user_id, declaration_text "
            f"FROM {table} ORDER BY id"
        ).fetchall()
        conn.close()
        return rows

    def test_migration_preserves_history_and_allows_same_task_replacement(self):
        self._create_legacy_database()

        result = migration.migrate(str(self.db_path))

        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["declaration_count"], 2)
        expected = [
            (1, 101, 201, 7, "confirmed one"),
            (2, 102, 202, 8, "confirmed two"),
        ]
        self.assertEqual(self._rows(migration.TABLE), expected)
        self.assertEqual(self._rows(migration.ARCHIVE), expected)

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """INSERT INTO whatsapp_campaign_consent_declarations
               (task_id, campaign_id, business_user_id, declaration_text,
                normalized_recipient_list, recipient_count, recipient_fingerprint)
               VALUES (101, 203, 7, 'confirmed replacement',
                       '[\"+14155550102\"]', 1, ?)""",
            ("c" * 64,),
        )
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM whatsapp_campaign_consent_declarations WHERE task_id = 101"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count, 2)

    def test_migration_rerun_is_idempotent(self):
        self._create_legacy_database()

        first = migration.migrate(str(self.db_path))
        second = migration.migrate(str(self.db_path))

        self.assertEqual(first["status"], "applied")
        self.assertEqual(second["status"], "already_applied")
        self.assertEqual(self._rows(migration.TABLE), self._rows(migration.ARCHIVE))

    def test_failed_migration_rolls_back_schema_and_data(self):
        self._create_legacy_database(malformed=True)
        before = self._rows(migration.TABLE)

        with self.assertRaises(sqlite3.OperationalError):
            migration.migrate(str(self.db_path))

        conn = sqlite3.connect(self.db_path)
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (migration.TABLE,),
        ).fetchone()[0]
        archive = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (migration.ARCHIVE,),
        ).fetchone()
        conn.close()

        self.assertIn("UNIQUE", table_sql.upper())
        self.assertIsNone(archive)
        self.assertEqual(self._rows(migration.TABLE), before)


if __name__ == "__main__":
    unittest.main()
