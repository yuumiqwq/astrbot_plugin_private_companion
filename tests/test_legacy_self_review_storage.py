"""Retired review tables remain readable across existing database upgrades."""

import unittest
from companion_v2.storage.schema import SCHEMA_VERSION


class SelfReviewStorageTests(unittest.TestCase):
    """v38 -> v39 adds two tables and must reject a partial or replayed migration."""

    def test_v38_upgrade_preserves_rows_and_rejects_partial_migration(self):
        from contextlib import closing
        from pathlib import Path
        import sqlite3
        from tempfile import TemporaryDirectory

        from companion_v2.storage.runtime import StorageOpenError, StorageRuntime
        from companion_v2.storage.schema import (REQUIRED_TABLES, SCHEMA_VERSION_V38,
            V38_REQUIRED_TABLES)
        from tests.execution_support import remove_v39

        with TemporaryDirectory(prefix="companion-v2-self-review-") as temporary:
            root = Path(temporary) / "v2-data"
            runtime = StorageRuntime.open(data_root=root, installation_id="self-review-test")
            with runtime.transaction() as connection:
                connection.execute("INSERT INTO runtime_metadata VALUES (?, ?)", ("review-marker", "kept"))
            database = runtime.database_path
            runtime.close()

            with closing(sqlite3.connect(database)) as connection:
                connection.execute("PRAGMA foreign_keys=OFF")
                self.assertIsNotNone(connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'role_self_review_runs'"
                ).fetchone())
                remove_v39(connection)
                for table in sorted(REQUIRED_TABLES - V38_REQUIRED_TABLES):
                    connection.execute(f'DROP TABLE IF EXISTS "{table}"')
                connection.execute("DELETE FROM schema_migrations WHERE "
                                   "CAST(substr(from_version, length('companion-v2.storage.v') + 1) AS INTEGER) >= 38")
                connection.execute("UPDATE runtime_metadata SET value = ? WHERE key = 'schema_version'",
                                   (SCHEMA_VERSION_V38,))
                connection.commit()

            upgraded = StorageRuntime.open(data_root=root, installation_id="self-review-test")
            self.assertEqual(upgraded.read_one("SELECT value FROM runtime_metadata WHERE key = ?",
                                               ("schema_version",))[0], SCHEMA_VERSION)
            self.assertEqual(upgraded.read_one("SELECT value FROM runtime_metadata WHERE key = ?",
                                               ("review-marker",))[0], "kept")
            self.assertIsNotNone(upgraded.read_one(
                "SELECT migration_id FROM schema_migrations WHERE migration_id = ?",
                ("companion-v2.storage.v38-to-v39",)))
            upgraded.close()

            # A table present without its migration record is a partial upgrade.
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("UPDATE runtime_metadata SET value = ? WHERE key = 'schema_version'",
                                   (SCHEMA_VERSION_V38,))
                connection.commit()
            with self.assertRaises(StorageOpenError):
                StorageRuntime.open(data_root=root, installation_id="self-review-test")
