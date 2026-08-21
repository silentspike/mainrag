#!/usr/bin/env python3
"""Executable PostgreSQL invariants for storage-v2 generation schema."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from contextlib import ExitStack
from pathlib import Path

from eval.storage_v2.harness import TemporaryPostgres


ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "migrations" / "029_storage_v2_generations.sql"
SCHEMA = ROOT / "schema.sql"
ADMIN_ID = "00000000-0000-4000-8000-000000000001"
WRITER_ID = "00000000-0000-4000-8000-000000000002"
DENIED_ID = "00000000-0000-4000-8000-000000000003"


class GenerationSchemaTests(unittest.TestCase):
    stack: ExitStack
    socket: Path
    databases: list[str]

    @classmethod
    def setUpClass(cls) -> None:
        for command in ("psql", "createdb", "dropdb"):
            if shutil.which(command) is None:
                raise unittest.SkipTest(f"required PostgreSQL command is absent: {command}")

        cls.stack = ExitStack()
        cls.databases = []
        configured_socket = os.environ.get("STORAGE_V2_TEST_SOCKET")
        if configured_socket:
            cls.socket = Path(configured_socket)
        else:
            temporary = cls.stack.enter_context(
                tempfile.TemporaryDirectory(prefix="mainrag-generation-schema-")
            )
            postgres = cls.stack.enter_context(TemporaryPostgres(Path(temporary)))
            cls.socket = postgres.socket

        cls.run_sql(
            "postgres",
            """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mainrag') THEN
        CREATE ROLE mainrag;
    END IF;
END
$$;
""",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        for database in reversed(cls.databases):
            subprocess.run(
                [
                    "dropdb",
                    "--if-exists",
                    "--force",
                    "--host",
                    str(cls.socket),
                    database,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        cls.stack.close()

    @classmethod
    def create_database(cls) -> str:
        database = f"storage_v2_{uuid.uuid4().hex}"
        subprocess.run(
            ["createdb", "--host", str(cls.socket), database],
            check=True,
            capture_output=True,
            text=True,
        )
        cls.databases.append(database)
        return database

    @classmethod
    def psql(
        cls,
        database: str,
        *,
        sql: str | None = None,
        file: Path | None = None,
        transaction_rollback: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            "psql",
            "-X",
            "--no-psqlrc",
            "--quiet",
            "--set=ON_ERROR_STOP=1",
            "--tuples-only",
            "--no-align",
            "--host",
            str(cls.socket),
            "--dbname",
            database,
        ]
        if transaction_rollback:
            command.extend(["--command", "BEGIN"])
        if file is not None:
            command.extend(["--file", str(file)])
        if sql is not None:
            command.extend(["--command", sql])
        if transaction_rollback:
            command.extend(["--command", "ROLLBACK"])
        result = subprocess.run(
            command,
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        if check and result.returncode != 0:
            raise AssertionError(
                f"psql failed with exit {result.returncode}:\n{result.stdout}\n{result.stderr}"
            )
        return result

    @classmethod
    def run_sql(cls, database: str, sql: str) -> str:
        return cls.psql(database, sql=sql).stdout.strip()

    def assert_sql_fails(self, database: str, sql: str, message: str) -> None:
        result = self.psql(database, sql=sql, check=False)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(message, result.stderr)

    def install_upgrade_fixture(self, database: str) -> None:
        self.run_sql(
            database,
            f"""
CREATE TABLE sources (
    id BIGINT PRIMARY KEY,
    legacy_counter BIGINT NOT NULL DEFAULT 0
);
CREATE TABLE users (
    id UUID PRIMARY KEY,
    is_admin BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE TABLE fixture_source_access (
    user_id UUID NOT NULL REFERENCES users(id),
    source_id BIGINT NOT NULL REFERENCES sources(id),
    can_read BOOLEAN NOT NULL,
    can_write BOOLEAN NOT NULL,
    PRIMARY KEY (user_id, source_id)
);
INSERT INTO sources(id, legacy_counter) VALUES (1, 7), (2, 11);
INSERT INTO users(id, is_admin) VALUES
    ('{ADMIN_ID}', TRUE),
    ('{WRITER_ID}', FALSE),
    ('{DENIED_ID}', FALSE);
INSERT INTO fixture_source_access(user_id, source_id, can_read, can_write)
VALUES ('{WRITER_ID}', 1, TRUE, TRUE);
CREATE FUNCTION user_can_access_source(
    p_user_id UUID,
    p_source_id BIGINT,
    p_action TEXT DEFAULT 'read'
) RETURNS BOOLEAN
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT EXISTS (
        SELECT 1 FROM users WHERE id = p_user_id AND is_admin
    ) OR EXISTS (
        SELECT 1
          FROM fixture_source_access
         WHERE user_id = p_user_id
           AND source_id = p_source_id
           AND CASE p_action
               WHEN 'read' THEN can_read
               WHEN 'write' THEN can_write
               ELSE FALSE
           END
    )
$$;
""",
        )

    def grant_worker_access(self, database: str) -> None:
        self.run_sql(
            database,
            """
CREATE ROLE storage_v2_worker;
GRANT USAGE ON SCHEMA public TO storage_v2_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON
    logical_source,
    source_generation,
    source_item,
    artifact_version,
    generation_item_version,
    storage_v2_gc_epoch
TO storage_v2_worker;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO storage_v2_worker;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO storage_v2_worker;
""",
        )

    @staticmethod
    def as_user(user_id: str, sql: str) -> str:
        return f"SET ROLE storage_v2_worker; SET app.user_id = '{user_id}'; {sql}"

    def test_full_schema_bootstrap_rolls_back_and_commits(self) -> None:
        rollback_database = self.create_database()
        self.psql(rollback_database, file=SCHEMA, transaction_rollback=True)
        self.assertEqual(
            self.run_sql(rollback_database, "SELECT to_regclass('public.sources') IS NULL"),
            "t",
        )

        commit_database = self.create_database()
        self.psql(commit_database, file=SCHEMA)
        self.assertEqual(
            self.run_sql(
                commit_database,
                """
SELECT COUNT(*)
  FROM (VALUES
    (to_regclass('public.sources')),
    (to_regclass('public.logical_source')),
    (to_regclass('public.source_generation')),
    (to_regclass('public.source_item')),
    (to_regclass('public.artifact_version')),
    (to_regclass('public.generation_item_version')),
    (to_regclass('public.storage_v2_gc_epoch'))
  ) AS required_table(value)
 WHERE value IS NOT NULL
""",
            ),
            "7",
        )

    def test_migration_is_transactional_idempotent_and_additive(self) -> None:
        database = self.create_database()
        self.install_upgrade_fixture(database)

        self.psql(database, file=MIGRATION, transaction_rollback=True)
        self.assertEqual(
            self.run_sql(database, "SELECT to_regclass('public.logical_source') IS NULL"),
            "t",
        )

        self.psql(database, file=MIGRATION)
        self.psql(database, file=MIGRATION)
        self.assertEqual(
            self.run_sql(database, "UPDATE sources SET legacy_counter = legacy_counter + 1 WHERE id = 1 RETURNING legacy_counter"),
            "8",
        )
        self.assertEqual(self.run_sql(database, "SELECT COUNT(*) FROM sources"), "2")

    def test_generation_identity_activation_membership_and_isolation(self) -> None:
        database = self.create_database()
        self.install_upgrade_fixture(database)
        self.psql(database, file=MIGRATION)
        self.grant_worker_access(database)

        generation_rows = self.run_sql(
            database,
            self.as_user(
                WRITER_ID,
                """
SELECT id || ':' || generation_seq
  FROM storage_v2_allocate_generation(1, 'fixture', '{"generation":1}');
SELECT id || ':' || generation_seq
  FROM storage_v2_allocate_generation(1, 'fixture', '{"generation":2}');
SELECT id || ':' || generation_seq
  FROM storage_v2_allocate_generation(1, 'fixture', '{"generation":3}');
""",
            ),
        ).splitlines()
        generation_pairs = [row for row in generation_rows if ":" in row]
        self.assertEqual([row.split(":")[1] for row in generation_pairs], ["1", "2", "3"])
        generation_ids = [int(row.split(":")[0]) for row in generation_pairs]
        generation_1, generation_2, generation_3 = generation_ids

        source_2_generation = int(
            self.run_sql(
                database,
                self.as_user(
                    ADMIN_ID,
                    "SELECT id FROM storage_v2_allocate_generation(2, 'fixture', '{\"generation\":1}')",
                ),
            )
        )

        item_id = int(
            self.run_sql(
                database,
                self.as_user(
                    WRITER_ID,
                    "INSERT INTO source_item(source_id, item_key, item_kind) VALUES (1, 'item-a', 'document') RETURNING id",
                ),
            ).splitlines()[0]
        )
        artifact_rows = self.run_sql(
            database,
            self.as_user(
                WRITER_ID,
                f"""
INSERT INTO artifact_version(
    item_id, source_id, witness_type, witness, adapter_profile_id,
    raw_body_id, expected_content_hash, byte_length
) VALUES ({item_id}, 1, 'fixture', '{{"version":"A"}}', 'fixture-v1', 101, 'hash-a', 10)
RETURNING id;
INSERT INTO artifact_version(
    item_id, source_id, witness_type, witness, adapter_profile_id,
    raw_body_id, expected_content_hash, byte_length
) VALUES ({item_id}, 1, 'fixture', '{{"version":"B"}}', 'fixture-v1', 102, 'hash-b', 20)
RETURNING id;
""",
            ),
        ).splitlines()
        artifact_ids = [int(row) for row in artifact_rows if row.isdigit()]
        self.assertEqual(len(artifact_ids), 2)
        artifact_a, artifact_b = artifact_ids

        self.run_sql(
            database,
            self.as_user(
                WRITER_ID,
                f"""
INSERT INTO generation_item_version VALUES (1, {item_id}, {artifact_a}, 1, NULL, NOW());
SELECT storage_v2_close_membership(1, {item_id}, 1, 2);
INSERT INTO generation_item_version VALUES (1, {item_id}, {artifact_b}, 2, NULL, NOW());
SELECT storage_v2_close_membership(1, {item_id}, 2, 3);
INSERT INTO generation_item_version VALUES (1, {item_id}, {artifact_a}, 3, NULL, NOW());
""",
            ),
        )
        self.assertEqual(
            self.run_sql(
                database,
                self.as_user(
                    WRITER_ID,
                    f"""
SELECT artifact_version_id
  FROM generation_item_version
 WHERE source_id = 1
   AND source_item_id = {item_id}
   AND valid_from_seq <= 1
   AND (valid_to_seq IS NULL OR 1 < valid_to_seq)
""",
                ),
            ),
            str(artifact_a),
        )
        self.assertEqual(
            self.run_sql(
                database,
                self.as_user(
                    WRITER_ID,
                    f"SELECT COUNT(*) FROM generation_item_version WHERE artifact_version_id = {artifact_a}",
                ),
            ),
            "2",
        )

        overlap_item = int(
            self.run_sql(
                database,
                self.as_user(
                    WRITER_ID,
                    "INSERT INTO source_item(source_id, item_key, item_kind) VALUES (1, 'item-overlap', 'document') RETURNING id",
                ),
            ).splitlines()[0]
        )
        overlap_artifact = int(
            self.run_sql(
                database,
                self.as_user(
                    WRITER_ID,
                    f"""
INSERT INTO artifact_version(
    item_id, source_id, witness_type, witness, adapter_profile_id,
    raw_body_id, expected_content_hash, byte_length
) VALUES ({overlap_item}, 1, 'fixture', '{{}}', 'fixture-v1', 103, 'hash-overlap', 1)
RETURNING id
""",
                ),
            ).splitlines()[0]
        )
        self.run_sql(
            database,
            self.as_user(
                WRITER_ID,
                f"INSERT INTO generation_item_version VALUES (1, {overlap_item}, {overlap_artifact}, 1, 3, NOW())",
            ),
        )
        self.assert_sql_fails(
            database,
            self.as_user(
                WRITER_ID,
                f"INSERT INTO generation_item_version VALUES (1, {overlap_item}, {overlap_artifact}, 2, NULL, NOW())",
            ),
            "conflicting key value violates exclusion constraint",
        )

        manifest_a = "a" * 64
        manifest_b = "b" * 64
        manifest_c = "c" * 64
        self.run_sql(
            database,
            self.as_user(
                WRITER_ID,
                f"""
SELECT storage_v2_seal_generation({generation_1}, 1);
SELECT storage_v2_verify_generation({generation_1}, '{manifest_a}');
SELECT storage_v2_mark_release_candidate({generation_1});
""",
            ),
        )
        self.assert_sql_fails(
            database,
            self.as_user(
                WRITER_ID,
                f"SELECT storage_v2_activate_generation(1, {generation_1}, NULL)",
            ),
            "activation requires administrator authority",
        )
        self.run_sql(
            database,
            self.as_user(
                ADMIN_ID,
                f"SELECT storage_v2_activate_generation(1, {generation_1}, NULL)",
            ),
        )
        self.run_sql(
            database,
            self.as_user(
                WRITER_ID,
                f"""
SELECT storage_v2_seal_generation({generation_2}, 1);
SELECT storage_v2_verify_generation({generation_2}, '{manifest_b}');
SELECT storage_v2_mark_release_candidate({generation_2});
""",
            ),
        )
        self.run_sql(
            database,
            self.as_user(
                ADMIN_ID,
                f"SELECT storage_v2_activate_generation(1, {generation_2}, {generation_1})",
            ),
        )
        self.assertEqual(
            self.run_sql(
                database,
                f"SELECT active_generation_id FROM logical_source WHERE id = 1; SELECT string_agg(id || ':' || status, ',' ORDER BY id) FROM source_generation WHERE id IN ({generation_1}, {generation_2})",
            ).splitlines(),
            [str(generation_2), f"{generation_1}:superseded,{generation_2}:active"],
        )

        self.assertEqual(
            self.run_sql(
                database,
                self.as_user(
                    WRITER_ID,
                    f"SELECT status FROM storage_v2_requalify_generation({generation_1}, '{manifest_c}')",
                ),
            ),
            "verified",
        )
        self.run_sql(
            database,
            self.as_user(
                WRITER_ID,
                f"SELECT storage_v2_mark_release_candidate({generation_1})",
            ),
        )
        self.run_sql(
            database,
            self.as_user(
                ADMIN_ID,
                f"SELECT storage_v2_activate_generation(1, {generation_1}, {generation_2})",
            ),
        )
        self.assertEqual(
            self.run_sql(
                database,
                f"SELECT active_generation_id FROM logical_source WHERE id = 1; SELECT string_agg(id || ':' || status, ',' ORDER BY id) FROM source_generation WHERE id IN ({generation_1}, {generation_2})",
            ).splitlines(),
            [str(generation_1), f"{generation_1}:active,{generation_2}:superseded"],
        )

        generation_4 = int(
            self.run_sql(
                database,
                self.as_user(
                    WRITER_ID,
                    "SELECT id FROM storage_v2_allocate_generation(1, 'fixture', '{\"generation\":4}')",
                ),
            )
        )
        self.assert_sql_fails(
            database,
            self.as_user(WRITER_ID, f"SELECT storage_v2_mark_release_candidate({generation_4})"),
            "only a verified generation",
        )
        self.assert_sql_fails(
            database,
            self.as_user(
                WRITER_ID,
                "INSERT INTO source_generation(source_id, generation_seq, witness_type, witness) VALUES (1, 99, 'fixture', '{}')",
            ),
            "controlled function",
        )
        self.assert_sql_fails(
            database,
            self.as_user(WRITER_ID, f"UPDATE source_generation SET status = 'sealed' WHERE id = {generation_4}"),
            "controlled function",
        )
        self.assert_sql_fails(
            database,
            self.as_user(WRITER_ID, "UPDATE logical_source SET active_generation_id = NULL WHERE id = 1"),
            "controlled function",
        )
        self.assert_sql_fails(
            database,
            f"UPDATE source_generation SET generation_seq = 99 WHERE id = {generation_4}",
            "source generation identity is immutable",
        )
        self.assert_sql_fails(
            database,
            f"DELETE FROM source_generation WHERE id = {generation_4}",
            "source generation rows cannot be deleted",
        )
        self.assert_sql_fails(
            database,
            f"UPDATE source_item SET item_key = 'mutated' WHERE id = {item_id}",
            "source_item rows are immutable",
        )
        self.assert_sql_fails(
            database,
            f"UPDATE artifact_version SET byte_length = 99 WHERE id = {artifact_a}",
            "artifact_version rows are immutable",
        )
        self.assert_sql_fails(
            database,
            f"UPDATE generation_item_version SET artifact_version_id = {artifact_b} WHERE source_id = 1 AND source_item_id = {item_id} AND valid_from_seq = 1",
            "membership identity is immutable",
        )

        common_artifact = f"""
item_id, source_id, witness_type, witness, adapter_profile_id,
content_root_node_id, raw_body_id, expected_content_hash, byte_length
"""
        self.assert_sql_fails(
            database,
            self.as_user(
                WRITER_ID,
                f"INSERT INTO artifact_version({common_artifact}) VALUES ({item_id}, 1, 'fixture', '{{}}', 'fixture-v1', NULL, NULL, 'missing-anchor', 1)",
            ),
            "violates check constraint",
        )
        self.assert_sql_fails(
            database,
            self.as_user(
                WRITER_ID,
                f"INSERT INTO artifact_version({common_artifact}) VALUES ({item_id}, 1, 'fixture', '{{}}', 'fixture-v1', 201, 202, 'two-anchors', 1)",
            ),
            "violates check constraint",
        )
        self.assert_sql_fails(
            database,
            self.as_user(
                ADMIN_ID,
                f"""
INSERT INTO artifact_version(
    item_id, source_id, witness_type, witness, adapter_profile_id,
    raw_body_id, expected_content_hash, byte_length
) VALUES ({item_id}, 2, 'fixture', '{{}}', 'fixture-v1', 203, 'cross-source', 1)
""",
            ),
            "violates foreign key constraint",
        )
        self.assert_sql_fails(
            database,
            self.as_user(
                ADMIN_ID,
                f"INSERT INTO generation_item_version VALUES (2, {item_id}, {artifact_a}, 1, NULL, NOW())",
            ),
            "violates foreign key constraint",
        )
        self.assert_sql_fails(
            database,
            f"UPDATE logical_source SET active_generation_id = {generation_1} WHERE id = 2",
            "violates foreign key constraint",
        )
        self.assert_sql_fails(
            database,
            f"UPDATE logical_source SET active_generation_id = {source_2_generation} WHERE id = 2",
            "active pointer and active generation disagree",
        )
        self.assert_sql_fails(
            database,
            f"UPDATE source_generation SET status = 'active', activated_at = NOW() WHERE id = {generation_2}",
            "duplicate key value violates unique constraint",
        )

        self.assertEqual(
            self.run_sql(
                database,
                self.as_user(WRITER_ID, "SELECT string_agg(id::TEXT, ',' ORDER BY id) FROM logical_source"),
            ),
            "1",
        )
        self.assertEqual(
            self.run_sql(
                database,
                self.as_user(DENIED_ID, "SELECT COUNT(*) FROM logical_source; SELECT COUNT(*) FROM artifact_version"),
            ).splitlines(),
            ["0", "0"],
        )
        self.assertEqual(
            self.run_sql(
                database,
                self.as_user(ADMIN_ID, "SELECT string_agg(id::TEXT, ',' ORDER BY id) FROM logical_source"),
            ),
            "1,2",
        )


if __name__ == "__main__":
    unittest.main()
