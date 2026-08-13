#!/usr/bin/env python3
"""PostgreSQL integration checks for storage-v2 bodies and pack reclamation."""

from __future__ import annotations

import hashlib
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
SCHEMA = ROOT / "schema.sql"
MIGRATION = ROOT / "migrations" / "030_storage_v2_content_bodies.sql"
ADMIN_ID = "00000000-0000-4000-8000-000000000011"


class ContentSchemaTests(unittest.TestCase):
    stack: ExitStack
    socket: Path
    database: str

    @classmethod
    def setUpClass(cls) -> None:
        for command in ("psql", "createdb", "dropdb"):
            if shutil.which(command) is None:
                raise unittest.SkipTest(f"required PostgreSQL command is absent: {command}")
        cls.stack = ExitStack()
        configured_socket = os.environ.get("STORAGE_V2_TEST_SOCKET")
        if configured_socket:
            cls.socket = Path(configured_socket)
        else:
            temporary = cls.stack.enter_context(
                tempfile.TemporaryDirectory(prefix="mainrag-content-schema-")
            )
            postgres = cls.stack.enter_context(TemporaryPostgres(Path(temporary)))
            cls.socket = postgres.socket
        cls.database = f"storage_v2_content_{uuid.uuid4().hex}"
        subprocess.run(
            ["createdb", "--host", str(cls.socket), cls.database],
            check=True,
            capture_output=True,
            text=True,
        )
        cls.sql(
            """
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mainrag') THEN
        CREATE ROLE mainrag;
    END IF;
END $$;
"""
        )
        cls.file(SCHEMA)
        cls.file(MIGRATION)
        cls.sql(
            f"""
CREATE TABLE users(id UUID PRIMARY KEY, is_admin BOOLEAN NOT NULL);
INSERT INTO users VALUES ('{ADMIN_ID}', TRUE);
CREATE ROLE storage_v2_pack_worker;
GRANT USAGE ON SCHEMA public TO storage_v2_pack_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON
    content_pack, content_body, content_pack_entry, content_dictionary,
    content_reader_epoch, content_pack_retirement, storage_v2_gc_epoch
TO storage_v2_pack_worker;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO storage_v2_pack_worker;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO storage_v2_pack_worker;
"""
        )

    @classmethod
    def tearDownClass(cls) -> None:
        subprocess.run(
            [
                "dropdb",
                "--if-exists",
                "--force",
                "--host",
                str(cls.socket),
                cls.database,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        cls.stack.close()

    @classmethod
    def command(cls, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [
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
                cls.database,
                *arguments,
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        if check and result.returncode != 0:
            raise AssertionError(f"psql failed:\n{result.stdout}\n{result.stderr}")
        return result

    @classmethod
    def sql(cls, statement: str) -> str:
        return cls.command("--command", statement).stdout.strip()

    @classmethod
    def file(cls, path: Path) -> None:
        cls.command("--file", str(path))

    def assert_sql_fails(self, statement: str, expected: str) -> None:
        result = self.command("--command", statement, check=False)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(expected, result.stderr)

    @staticmethod
    def admin(statement: str) -> str:
        return f"SET app.user_id = '{ADMIN_ID}'; {statement}"

    @staticmethod
    def worker(statement: str) -> str:
        return f"SET ROLE storage_v2_pack_worker; SET app.user_id = '{ADMIN_ID}'; {statement}"

    @staticmethod
    def digest_hex(value: bytes) -> str:
        return hashlib.sha256(value).hexdigest()

    def create_published_pack(
        self,
        pack_id: str,
        body_id: int,
        stored: bytes,
        logical: bytes,
    ) -> None:
        entry_digest = self.digest_hex(stored)
        manifest = self.digest_hex((pack_id + entry_digest).encode())
        self.sql(
            self.admin(
                f"""
SELECT storage_v2_create_pack(
    '{pack_id}', '{pack_id}.pack', '{uuid.uuid4()}'
);
INSERT INTO content_pack_entry(
    pack_id, ordinal, body_id, pack_offset, stored_length, codec, entry_digest
) VALUES (
    '{pack_id}', 0, {body_id}, 0, {len(stored)}, 'zstd', decode('{entry_digest}', 'hex')
);
SELECT storage_v2_verify_pack(
    '{pack_id}', decode('{manifest}', 'hex'), {len(stored)}
);
SELECT storage_v2_publish_pack('{pack_id}');
"""
            )
        )
        self.assertEqual(self.digest_hex(logical), self.sql(f"SELECT encode(digest, 'hex') FROM content_body WHERE id = {body_id}"))

    def test_body_dedup_pack_publication_repack_and_gc_delay(self) -> None:
        inline = b"synthetic inline body"
        inline_hex = inline.hex()
        body_ids = self.sql(
            self.admin(
                f"""
SELECT id FROM storage_v2_put_inline_body(decode('{inline_hex}', 'hex'));
SELECT id FROM storage_v2_put_inline_body(decode('{inline_hex}', 'hex'));
"""
            )
        ).splitlines()
        self.assertEqual(len(body_ids), 2)
        self.assertEqual(body_ids[0], body_ids[1])

        digest = self.digest_hex(b"packed logical body")
        old_pack = str(uuid.uuid4())
        old_stored = b"old-compressed-frame"
        old_entry_digest = self.digest_hex(old_stored)
        old_manifest = self.digest_hex((old_pack + old_entry_digest).encode())
        self.sql(
            self.admin(
                f"""
BEGIN;
SELECT storage_v2_create_pack('{old_pack}', '{old_pack}.pack', '{uuid.uuid4()}');
WITH inserted_body AS (
    INSERT INTO content_body(digest_algorithm, digest, logical_length, pack_id)
    VALUES ('sha256-v1', decode('{digest}', 'hex'), 19, '{old_pack}')
    RETURNING id
)
INSERT INTO content_pack_entry(
    pack_id, ordinal, body_id, pack_offset, stored_length, codec, entry_digest
)
SELECT '{old_pack}', 0, id, 0, {len(old_stored)}, 'zstd',
       decode('{old_entry_digest}', 'hex')
  FROM inserted_body;
COMMIT;
SELECT storage_v2_verify_pack(
    '{old_pack}', decode('{old_manifest}', 'hex'), {len(old_stored)}
);
SELECT storage_v2_publish_pack('{old_pack}');
"""
            )
        )
        packed_body = int(
            self.sql(f"SELECT id FROM content_body WHERE digest = decode('{digest}', 'hex')")
        )

        self.assert_sql_fails(
            self.admin(
                f"INSERT INTO content_body(digest_algorithm, digest, logical_length) VALUES ('sha256-v1', decode('{digest}', 'hex'), 19)"
            ),
            "violates check constraint",
        )
        self.assert_sql_fails(
            self.admin(
                f"INSERT INTO content_body(digest_algorithm, digest, logical_length, inline_bytes, pack_id) VALUES ('sha256-v1', decode('{digest}', 'hex'), 19, decode('{inline_hex}', 'hex'), '{old_pack}')"
            ),
            "violates check constraint",
        )

        candidate = str(uuid.uuid4())
        self.sql(
            self.admin(
                f"SELECT storage_v2_create_pack('{candidate}', '{candidate}.pack', '{uuid.uuid4()}')"
            )
        )
        self.assert_sql_fails(
            self.admin(f"SELECT storage_v2_publish_pack('{candidate}')"),
            "only a verified pack",
        )
        self.assertEqual(
            self.sql(self.admin(f"SELECT status FROM storage_v2_abandon_pack('{candidate}')")),
            "abandoned",
        )

        replacement = str(uuid.uuid4())
        self.create_published_pack(
            replacement,
            packed_body,
            b"new-compressed-frame",
            b"packed logical body",
        )
        epoch_id = int(
            self.sql(
                self.admin(
                    """
INSERT INTO storage_v2_gc_epoch(
    source_id, status, root_manifest_sha256, code_sha, verified_at
) VALUES (
    NULL, 'verified', repeat('a', 64), repeat('b', 40), NOW()
) RETURNING id
"""
                )
            ).splitlines()[0]
        )
        reader_epoch = self.sql(self.admin("SELECT storage_v2_begin_reader_epoch()"))
        self.assertEqual(
            self.sql(
                self.admin(
                    f"SELECT storage_v2_switch_pack('{old_pack}', '{replacement}', {epoch_id})"
                )
            ),
            "1",
        )
        self.assertEqual(
            self.sql(f"SELECT pack_id FROM content_body WHERE id = {packed_body}"), replacement
        )
        self.assert_sql_fails(
            self.admin(f"SELECT storage_v2_mark_pack_readers_drained('{old_pack}')"),
            "pre-switch readers are still active",
        )
        self.sql(self.admin(f"SELECT storage_v2_end_reader_epoch('{reader_epoch}')"))
        self.sql(self.admin(f"SELECT storage_v2_mark_pack_readers_drained('{old_pack}')"))
        self.sql(f"UPDATE storage_v2_gc_epoch SET status = 'sweeping' WHERE id = {epoch_id}")
        self.assertEqual(
            self.sql(self.admin(f"SELECT status FROM storage_v2_reclaim_pack('{old_pack}')")),
            "reclaimed",
        )

        self.assert_sql_fails(
            self.worker(f"UPDATE content_pack SET status = 'retired' WHERE id = '{replacement}'"),
            "controlled function",
        )
        self.assert_sql_fails(
            self.worker(f"UPDATE content_body SET pack_id = '{old_pack}' WHERE id = {packed_body}"),
            "controlled function",
        )
        self.assertEqual(
            self.sql("SELECT inline_count, packed_count, reclaimed_bytes FROM storage_v2_content_metrics"),
            f"1|1|{len(b'old-compressed-frame')}",
        )


if __name__ == "__main__":
    unittest.main()
