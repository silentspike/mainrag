"""Synthetic active-set search, activation binding, and isolation checks."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
import uuid
from contextlib import ExitStack
from pathlib import Path

from eval.storage_v2.harness import TemporaryPostgres
from eval.storage_v2.schema import test_shadow_ingest_schema as fixture


ROOT = Path(__file__).resolve().parents[3]
ADMIN = "00000000-0000-4000-8000-000000000051"
READER = "00000000-0000-4000-8000-000000000052"


class ActiveSetSearchTests(unittest.TestCase):
    make_projection = fixture.ShadowIngestSchemaTests.make_projection
    begin = fixture.ShadowIngestSchemaTests.begin
    stage = fixture.ShadowIngestSchemaTests.stage
    complete_analysis = fixture.ShadowIngestSchemaTests.complete_analysis
    commit = fixture.ShadowIngestSchemaTests.commit

    @classmethod
    def setUpClass(cls) -> None:
        cls.stack = ExitStack()
        temporary = cls.stack.enter_context(tempfile.TemporaryDirectory(prefix="mainrag-active-search-"))
        postgres = cls.stack.enter_context(TemporaryPostgres(Path(temporary)))
        cls.socket = postgres.socket
        cls.database = "storage_v2_active_" + uuid.uuid4().hex
        cls.command("postgres", "CREATE ROLE mainrag")
        subprocess.run(
            ["createdb", "--host", str(cls.socket), cls.database],
            check=True, capture_output=True, text=True,
        )
        cls.command(cls.database, file=ROOT / "schema.sql")

    @classmethod
    def tearDownClass(cls) -> None:
        subprocess.run(
            ["dropdb", "--if-exists", "--force", "--host", str(cls.socket), cls.database],
            check=False, capture_output=True, text=True,
        )
        cls.stack.close()

    @classmethod
    def command(cls, database: str, sql: str | None = None, *,
                file: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
        command = ["psql", "-X", "--no-psqlrc", "-qAt", "--set=ON_ERROR_STOP=1",
                   "--host", str(cls.socket), "--dbname", database]
        if sql is not None:
            command += ["--command", sql]
        if file is not None:
            command += ["--file", str(file)]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        if check and result.returncode:
            raise AssertionError(result.stderr)
        return result

    @classmethod
    def sql(cls, statement: str) -> str:
        return cls.command(cls.database, statement).stdout.strip()

    @staticmethod
    def actor(user_id: str, statement: str) -> str:
        return f"SET app.user_id = '{user_id}'; {statement}"

    @classmethod
    def admin(cls, statement: str) -> str:
        return cls.actor(ADMIN, statement)

    def assert_sql_fails(self, statement: str, expected: str) -> None:
        result = self.command(self.database, statement, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(expected, result.stderr)

    def search(self, actor: str, manifest_digest: str, *, source: int | None = None,
               include_test: bool = False) -> dict:
        source_value = "NULL" if source is None else str(source)
        value = self.sql(self.actor(actor, f"""
SELECT storage_v2_search_active('{manifest_digest}',
    '{{"type":"term","value":"alpha"}}'::JSONB, '{{}}'::JSONB, 10,
    {source_value}, {str(include_test).lower()})::TEXT;
"""))
        return json.loads(value)

    def test_active_set_requires_receipt_and_reads_authorized_sources(self) -> None:
        self.sql(f"""
CREATE TABLE users(id UUID PRIMARY KEY, is_admin BOOLEAN NOT NULL);
CREATE TABLE fixture_source_access(user_id UUID, source_id BIGINT,
    can_read BOOLEAN, can_write BOOLEAN, PRIMARY KEY(user_id, source_id));
INSERT INTO users VALUES ('{ADMIN}', TRUE), ('{READER}', FALSE);
INSERT INTO sources(id,name,type,path) VALUES
    (1,'synthetic-one','fixture','synthetic-one'),
    (2,'synthetic-two','fixture','synthetic-two'),
    (3,'synthetic-benchmark','fixture','synthetic-benchmark');
UPDATE sources SET is_test=TRUE WHERE id=3;
INSERT INTO fixture_source_access VALUES ('{READER}',1,TRUE,FALSE);
CREATE FUNCTION user_can_access_source(
    p_user_id UUID, p_source_id BIGINT, p_action TEXT DEFAULT 'read'
) RETURNS BOOLEAN LANGUAGE SQL STABLE SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
    SELECT EXISTS(SELECT 1 FROM users WHERE id=p_user_id AND is_admin)
        OR EXISTS(SELECT 1 FROM fixture_source_access
                   WHERE user_id=p_user_id AND source_id=p_source_id
                     AND CASE p_action WHEN 'read' THEN can_read
                                       WHEN 'write' THEN can_write ELSE FALSE END)
$$;
CREATE ROLE storage_v2_active_fixture_worker;
GRANT USAGE ON SCHEMA public TO storage_v2_active_fixture_worker;
GRANT INSERT ON storage_v2_activation_set_evidence
    TO storage_v2_active_fixture_worker;
""")
        self.assert_sql_fails(
            self.actor(ADMIN, "SELECT storage_v2_search_active('" + "a" * 64
                       + "','{\"type\":\"term\",\"value\":\"alpha\"}'::jsonb)"),
            "complete activated source set and exact receipt are required",
        )

        entries = []
        for source_id in (1, 2, 3):
            content = f"alpha source{source_id}"
            node, view, digest = self.make_projection(content)
            run = self.begin(source_id, f"{source_id:02x}" * 32,
                             f"{source_id + 3:02x}" * 32, user_id=ADMIN)
            self.stage(run, f"source-{source_id}.txt", content, node, view, digest,
                       user_id=ADMIN)
            self.complete_analysis(digest)
            document = int(self.sql(self.admin(
                "SELECT id FROM storage_v2_put_search_document("
                f"'active-fixture','node',{node},'{content}',ARRAY[]::TEXT[])"
            )))
            self.sql(self.admin(
                f"SELECT storage_v2_bind_search_document({view},0,{document},1.0)"
            ))
            self.commit(run, 1, user_id=ADMIN)
            generation_id = int(self.sql(
                f"SELECT generation_id FROM storage_v2_ingest_run WHERE id={run}"
            ))
            self.sql(self.admin(f"""
SELECT storage_v2_verify_generation({generation_id}, '{'a' * 64}');
SELECT storage_v2_mark_release_candidate({generation_id});
"""))
            evidence_id = str(uuid.uuid4())
            self.sql(f"""
INSERT INTO storage_v2_release_candidate_evidence(
    id,source_id,generation_id,commit_sha,source_watermark_sha256,
    adapter_profile_id,analysis_profile_id,search_profile_id,manifest,manifest_sha256
) VALUES ('{evidence_id}',{source_id},{generation_id},'{'c' * 40}',
    '{'b' * 64}','fixture-adapter','fixture-analysis','fixture-search',
    '{{"status":"PASS"}}',digest(convert_to('{{"status":"PASS"}}'::jsonb::text,'UTF8'),'sha256'));
""")
            evidence_digest = self.sql(
                "SELECT encode(manifest_sha256,'hex') FROM storage_v2_release_candidate_evidence "
                f"WHERE id='{evidence_id}'"
            )
            entries.append({"source_id": source_id, "candidate_generation_id": generation_id,
                            "expected_active_generation_id": None, "evidence_id": evidence_id,
                            "evidence_manifest_sha256": evidence_digest,
                            "source_watermark_sha256": "b" * 64})

        manifest = {"schema_version": "mainrag.storage-v2.activation-set.v1",
                    "activation_id": str(uuid.uuid4()), "code_commit_sha": "c" * 40,
                    "schema_sha256": "d" * 64, "backend_package_sha256": "e" * 64,
                    "aggregate_evidence_sha256": "f" * 64, "sources": entries}
        literal = json.dumps(manifest, sort_keys=True).replace("'", "''")
        digest = self.sql("SELECT encode(digest(convert_to('"
                          + literal + "'::jsonb::text,'UTF8'),'sha256'),'hex')")
        self.sql(self.admin(
            f"SELECT storage_v2_activate_candidate_set('{literal}'::jsonb,'{digest}')"
        ))
        self.assert_sql_fails(
            "SET ROLE storage_v2_active_fixture_worker; "
            f"SET app.user_id='{ADMIN}'; "
            "INSERT INTO storage_v2_activation_set_evidence "
            "(id,manifest_sha256,source_count,pointer_set_sha256) VALUES "
            f"('{uuid.uuid4()}','{digest}',3,'{'0' * 64}')",
            "storage-v2 state changes require a controlled function",
        )
        ordinary = self.search(ADMIN, digest)
        reader = self.search(READER, digest)
        benchmark = self.search(ADMIN, digest, include_test=True)
        self.assertEqual(ordinary["total"], 2)
        self.assertEqual({hit["source_id"] for hit in ordinary["results"]}, {1, 2})
        self.assertEqual({hit["generation_seq"] for hit in ordinary["results"]}, {1})
        self.assertEqual(reader["total"], 1)
        self.assertEqual({hit["source_id"] for hit in reader["results"]}, {1})
        self.assertEqual(self.search(ADMIN, digest, source=1)["total"], 1)
        self.assertEqual(benchmark["total"], 3)
        self.assertEqual({hit["source_id"] for hit in benchmark["results"]}, {1, 2, 3})
        self.command(self.database, file=ROOT / "migrations/058_storage_v2_active_set_search.sql")
        self.assertEqual(self.search(ADMIN, digest), ordinary)
        self.sql("UPDATE sources SET is_test=FALSE WHERE id=3")
        self.assert_sql_fails(
            self.actor(ADMIN, f"SELECT storage_v2_search_active('{digest}',"
                       "'{\"type\":\"term\",\"value\":\"alpha\"}'::jsonb)"),
            "complete activated source set and exact receipt are required",
        )
        self.sql("UPDATE sources SET is_test=TRUE WHERE id=3")
        self.assertEqual(self.search(ADMIN, digest), ordinary)
        self.assert_sql_fails(
            self.actor(READER, f"SELECT storage_v2_search_active('{digest}',"
                       "'{\"type\":\"term\",\"value\":\"alpha\"}'::jsonb,"
                       "'{}'::jsonb,10,NULL,TRUE)"),
            "test scope requires administrator authority",
        )
        self.assert_sql_fails(
            self.actor(READER, f"SELECT storage_v2_search_active('{digest}',"
                       "'{\"type\":\"term\",\"value\":\"alpha\"}'::jsonb,"
                       "'{}'::jsonb,10,2,FALSE)"),
            "source access denied",
        )
        self.assert_sql_fails(
            self.actor(ADMIN, "SELECT storage_v2_search_active('" + "0" * 64
                       + "','{\"type\":\"term\",\"value\":\"alpha\"}'::jsonb)"),
            "complete activated source set and exact receipt are required",
        )
        self.sql("INSERT INTO sources(id,name,type,path) VALUES "
                 "(4,'late-source','fixture','late-source')")
        self.assert_sql_fails(
            self.actor(ADMIN, f"SELECT storage_v2_search_active('{digest}',"
                       "'{\"type\":\"term\",\"value\":\"alpha\"}'::jsonb)"),
            "complete activated source set and exact receipt are required",
        )


if __name__ == "__main__":
    unittest.main()
