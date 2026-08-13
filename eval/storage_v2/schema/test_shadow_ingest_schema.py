#!/usr/bin/env python3
"""PostgreSQL invariants for the generation-aware storage-v2 shadow writer."""

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
SCHEMA = ROOT / "schema.sql"
ADMIN_ID = "00000000-0000-4000-8000-000000000031"
WRITER_ID = "00000000-0000-4000-8000-000000000032"
OTHER_ID = "00000000-0000-4000-8000-000000000033"


class ShadowIngestSchemaTests(unittest.TestCase):
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
                tempfile.TemporaryDirectory(prefix="mainrag-shadow-ingest-")
            )
            postgres = cls.stack.enter_context(TemporaryPostgres(Path(temporary)))
            cls.socket = postgres.socket
        cls.database = f"storage_v2_ingest_{uuid.uuid4().hex}"
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
        cls.sql(
            f"""
CREATE TABLE users(id UUID PRIMARY KEY, is_admin BOOLEAN NOT NULL);
CREATE TABLE fixture_source_access(
    user_id UUID NOT NULL,
    source_id BIGINT NOT NULL,
    can_read BOOLEAN NOT NULL,
    can_write BOOLEAN NOT NULL,
    PRIMARY KEY(user_id, source_id)
);
INSERT INTO users VALUES
    ('{ADMIN_ID}', TRUE), ('{WRITER_ID}', FALSE), ('{OTHER_ID}', FALSE);
INSERT INTO sources(id, name, type, path) VALUES
    (1, 'synthetic-one', 'fixture', 'synthetic-one'),
    (2, 'synthetic-two', 'fixture', 'synthetic-two'),
    (3, 'synthetic-writer', 'fixture', 'synthetic-writer'),
    (4, 'synthetic-intelligence-export', 'fixture', 'synthetic-intelligence-export'),
    (5, 'synthetic-intelligence-import', 'fixture', 'synthetic-intelligence-import');
INSERT INTO fixture_source_access VALUES
    ('{WRITER_ID}', 1, TRUE, TRUE), ('{WRITER_ID}', 3, TRUE, TRUE),
    ('{OTHER_ID}', 2, TRUE, TRUE);
CREATE FUNCTION user_can_access_source(
    p_user_id UUID, p_source_id BIGINT, p_action TEXT DEFAULT 'read'
) RETURNS BOOLEAN
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT EXISTS (SELECT 1 FROM users WHERE id = p_user_id AND is_admin)
        OR EXISTS (
            SELECT 1 FROM fixture_source_access
             WHERE user_id = p_user_id AND source_id = p_source_id
               AND CASE p_action WHEN 'read' THEN can_read WHEN 'write' THEN can_write ELSE FALSE END
        )
$$;
CREATE ROLE storage_v2_shadow_worker;
GRANT USAGE ON SCHEMA public TO storage_v2_shadow_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO storage_v2_shadow_worker;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO storage_v2_shadow_worker;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO storage_v2_shadow_worker;
"""
        )

    @classmethod
    def tearDownClass(cls) -> None:
        subprocess.run(
            ["dropdb", "--if-exists", "--force", "--host", str(cls.socket), cls.database],
            check=False,
            capture_output=True,
            text=True,
        )
        cls.stack.close()

    @classmethod
    def command(cls, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [
                "psql", "-X", "--no-psqlrc", "--quiet", "--set=ON_ERROR_STOP=1",
                "--tuples-only", "--no-align", "--host", str(cls.socket),
                "--dbname", cls.database, *arguments,
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

    @staticmethod
    def actor(user_id: str, statement: str) -> str:
        return f"SET ROLE storage_v2_shadow_worker; SET app.user_id = '{user_id}'; {statement}"

    @classmethod
    def admin(cls, statement: str) -> str:
        return cls.actor(ADMIN_ID, statement)

    def assert_sql_fails(self, statement: str, expected: str) -> None:
        result = self.command("--command", statement, check=False)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(expected, result.stderr)

    def make_projection(self, content: str, language: str = "text") -> tuple[int, int, str]:
        encoded = content.replace("'", "''")
        row = self.sql(
            self.admin(
                f"""
WITH body AS (
    SELECT id, digest FROM storage_v2_put_inline_body(convert_to('{encoded}', 'UTF8'))
), node AS (
    SELECT leaf.id, body.digest
      FROM body
      CROSS JOIN LATERAL storage_v2_put_leaf_node('shadow-fixture', 'text', body.id) leaf
), view_row AS (
    SELECT view_value.id, node.id AS node_id, node.digest
      FROM node
      CROSS JOIN LATERAL storage_v2_put_retrieval_view(
        'chunk', 'fixture-view-v1', '{language}', 'fixture-tokenizer-v1', 0,
        ARRAY['content'], ARRAY['node'], ARRAY[node.id], ARRAY[0::BIGINT],
        ARRAY[octet_length(convert_to('{encoded}', 'UTF8'))::BIGINT]
      ) view_value
)
SELECT node_id || ':' || id || ':' || encode(digest, 'hex') FROM view_row;
"""
            )
        )
        node_id, view_id, digest_hex = row.split(":")
        return int(node_id), int(view_id), digest_hex

    def begin(
        self,
        source_id: int,
        key: str,
        manifest: str,
        *,
        force: bool = False,
        user_id: str = ADMIN_ID,
    ) -> int:
        return int(
            self.sql(
                self.actor(
                    user_id,
                    f"""
SELECT (storage_v2_begin_shadow_ingest(
    {source_id}, '{key}', '{manifest}', 'fixture-adapter-v1',
    'synthetic-snapshot', '{{"fixture":true}}'::JSONB, {str(force).lower()}
)).id;
"""
                )
            )
        )

    def stage(
        self,
        run_id: int,
        item_key: str,
        content: str,
        node_id: int,
        view_id: int,
        digest_hex: str,
        user_id: str = ADMIN_ID,
    ) -> None:
        content_quoted = content.replace("'", "''")
        self.sql(
            self.actor(
                user_id,
                f"""
SELECT (storage_v2_stage_shadow_item(
    {run_id}, '{item_key}', 'document', 'synthetic-item',
    '{{"item":"{item_key}"}}'::JSONB, 'fixture-adapter-v1', {node_id}, NULL,
    '{digest_hex}', octet_length(convert_to('{content_quoted}', 'UTF8')),
    decode('{digest_hex}', 'hex'), 'fixture-analysis-v1', {view_id},
    '/synthetic/{item_key}', '{{"byte_start":0}}'::JSONB
)).source_item_id;
"""
            )
        )

    def complete_analysis(self, digest_hex: str) -> None:
        status = self.sql(
            self.admin(
                f"""
SELECT (storage_v2_begin_analysis_attempt(
    decode('{digest_hex}', 'hex'), 'fixture-analysis-v1'
)).status;
"""
            )
        )
        if status == "pending":
            self.sql(
                self.admin(
                    f"""
SELECT (storage_v2_finish_analysis_attempt(
    decode('{digest_hex}', 'hex'), 'fixture-analysis-v1',
    '{{"symbols":[]}}'::JSONB, NULL
)).status;
"""
                )
            )

    def commit(self, run_id: int, count: int, user_id: str = ADMIN_ID) -> None:
        root = self.sql(
            self.actor(
                user_id,
                f"SELECT storage_v2_shadow_generation_root({run_id});",
            )
        )
        self.assertEqual(
            self.sql(
                self.actor(
                    user_id,
                    f"SELECT (storage_v2_commit_shadow_ingest({run_id}, {count}, '{root}')).status;"
                )
            ),
            "sealed",
        )

    def test_initial_noop_delta_reuse_and_a_to_b_to_a(self) -> None:
        node_a, view_a, digest_a = self.make_projection("alpha")
        node_b, view_b, digest_b = self.make_projection("beta")

        run_one = self.begin(1, "1" * 64, "a" * 64)
        self.stage(run_one, "one.txt", "alpha", node_a, view_a, digest_a)
        self.stage(run_one, "delete.txt", "beta", node_b, view_b, digest_b)
        self.complete_analysis(digest_a)
        self.complete_analysis(digest_b)
        self.commit(run_one, 2)

        self.assertEqual(
            self.sql(
                "SELECT generation.status || ':' || generation.item_count "
                "FROM source_generation generation JOIN storage_v2_ingest_run run "
                f"ON run.generation_id = generation.id WHERE run.id = {run_one}"
            ),
            "sealed:2",
        )
        self.assertEqual(self.sql("SELECT active_generation_id IS NULL FROM logical_source WHERE id = 1"), "t")
        self.assertEqual(self.begin(1, "2" * 64, "a" * 64), run_one)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM source_generation WHERE source_id = 1"), "1")

        original_artifact = self.sql(
            "SELECT artifact_version_id FROM generation_item_version membership "
            "JOIN source_item item ON item.id = membership.source_item_id "
            "WHERE item.item_key = 'one.txt' AND membership.valid_from_seq = 1"
        )
        run_two = self.begin(1, "3" * 64, "c" * 64)
        self.stage(run_two, "one.txt", "beta", node_b, view_b, digest_b)
        self.stage(run_two, "added.txt", "alpha", node_a, view_a, digest_a)
        self.commit(run_two, 2)

        self.assertEqual(
            self.sql(
                "SELECT COUNT(*) FROM generation_item_version WHERE source_id = 1 "
                "AND valid_to_seq = 2"
            ),
            "2",
        )
        self.assertEqual(
            self.sql(
                "SELECT string_agg(item.item_key, ',' ORDER BY item.item_key) "
                "FROM generation_item_version membership JOIN source_item item ON item.id = membership.source_item_id "
                "WHERE membership.source_id = 1 AND membership.valid_from_seq <= 2 "
                "AND (membership.valid_to_seq IS NULL OR membership.valid_to_seq > 2)"
            ),
            "added.txt,one.txt",
        )

        run_three = self.begin(1, "4" * 64, "e" * 64)
        self.stage(run_three, "one.txt", "alpha", node_a, view_a, digest_a)
        self.stage(run_three, "added.txt", "alpha", node_a, view_a, digest_a)
        self.commit(run_three, 2)
        returned_artifact = self.sql(
            "SELECT artifact_version_id FROM generation_item_version membership "
            "JOIN source_item item ON item.id = membership.source_item_id "
            "WHERE item.item_key = 'one.txt' AND membership.valid_from_seq = 3"
        )
        self.assertEqual(returned_artifact, original_artifact)
        self.assertEqual(
            self.sql(
                "SELECT COUNT(*) FROM generation_item_version membership "
                "JOIN source_item item ON item.id = membership.source_item_id "
                "WHERE item.item_key = 'added.txt'"
            ),
            "1",
            "unchanged items must keep their open interval",
        )
        self.assertEqual(
            self.sql(
                "SELECT COUNT(DISTINCT occurrence.source_id) || ':' || "
                "COUNT(DISTINCT occurrence.view_id) FROM occurrence "
                "JOIN artifact_version artifact ON artifact.id = occurrence.artifact_version_id "
                f"WHERE artifact.expected_content_hash = '{digest_a}' AND occurrence.source_id IN (1, 2)"
            ),
            "2:1",
            "global projections must be reusable without merging authorized occurrences",
        )
        self.assertEqual(
            self.sql(
                "SELECT string_agg(convert_from(body.inline_bytes, 'UTF8'), ',' ORDER BY item.item_key) "
                "FROM generation_item_version membership "
                "JOIN source_item item ON item.id = membership.source_item_id "
                "JOIN artifact_version artifact ON artifact.id = membership.artifact_version_id "
                "JOIN content_node node ON node.id = artifact.content_root_node_id "
                "JOIN content_body body ON body.id = node.body_id "
                "WHERE membership.source_id = 1 AND membership.valid_from_seq <= 3 "
                "AND (membership.valid_to_seq IS NULL OR membership.valid_to_seq > 3)"
            ),
            "alpha,alpha",
            "the sealed synthetic generation must reconstruct exactly from content anchors",
        )
        self.assertEqual(self.sql("SELECT active_generation_id IS NULL FROM logical_source WHERE id = 1"), "t")

    def test_analysis_retry_append_frontier_cancellation_and_isolation(self) -> None:
        node, view_id, digest_hex = self.make_projection("alpha")
        run_id = self.begin(2, "5" * 64, "6" * 64)
        self.stage(run_id, "shared.txt", "alpha", node, view_id, digest_hex)
        self.sql(
            self.admin(
                f"""
SELECT storage_v2_begin_analysis_attempt(decode('{digest_hex}', 'hex'), 'fixture-analysis-v1');
SELECT storage_v2_finish_analysis_attempt(
    decode('{digest_hex}', 'hex'), 'fixture-analysis-v1', NULL, 'synthetic-parser-failure'
);
"""
            )
        )
        self.assert_sql_fails(
            self.admin(
                f"SELECT storage_v2_commit_shadow_ingest({run_id}, 1, "
                f"storage_v2_shadow_generation_root({run_id}));"
            ),
            "all staged analysis must be complete",
        )
        self.complete_analysis(digest_hex)
        self.assert_sql_fails(
            self.admin(f"SELECT storage_v2_commit_shadow_ingest({run_id}, 1, '{'7' * 64}');"),
            "generation root does not match staged items",
        )
        self.commit(run_id, 1)
        self.assertEqual(
            self.sql(
                f"SELECT attempt_count || ':' || status FROM storage_v2_analysis_cache "
                f"WHERE content_identity_sha256 = decode('{digest_hex}', 'hex')"
            ),
            "2:complete",
        )

        item_id = int(self.sql("SELECT id FROM source_item WHERE source_id = 2 AND item_key = 'shared.txt'"))
        self.sql(
            self.admin(
                f"""
SELECT storage_v2_update_append_frontier(
    2, {item_id}, 'fixture-adapter-v1', 0, NULL, 5,
    decode('{digest_hex}', 'hex'), decode('{digest_hex}', 'hex')
);
SELECT storage_v2_update_append_frontier(
    2, {item_id}, 'fixture-adapter-v1', 5, decode('{digest_hex}', 'hex'), 10,
    digest(convert_to('alphaalpha', 'UTF8'), 'sha256'), NULL
);
"""
            )
        )
        self.assertEqual(
            self.sql(
                "SELECT prefix_bytes || ':' || appends_since_full FROM storage_v2_append_frontier "
                "WHERE source_id = 2"
            ),
            "10:1",
        )
        self.assert_sql_fails(
            self.admin(
                f"SELECT storage_v2_update_append_frontier(2, {item_id}, 'fixture-adapter-v1', "
                f"5, decode('{digest_hex}', 'hex'), 11, decode('{digest_hex}', 'hex'), NULL);"
            ),
            "append frontier drift",
        )
        self.assert_sql_fails(
            self.admin(
                f"SELECT storage_v2_update_append_frontier(2, {item_id}, 'fixture-adapter-v1', "
                "10, digest(convert_to('alphaalpha', 'UTF8'), 'sha256'), 11, "
                "digest(convert_to('alphaalpha!', 'UTF8'), 'sha256'), NULL, 1);"
            ),
            "scheduled full comparison required",
        )

        cancelled = self.begin(2, "8" * 64, "6" * 64, force=True)
        self.assertEqual(
            self.sql(self.admin(f"SELECT (storage_v2_cancel_shadow_ingest({cancelled}, 1)).status;")),
            "cancelled",
        )
        self.assertEqual(
            self.sql(f"SELECT status FROM source_generation WHERE id = (SELECT generation_id FROM storage_v2_ingest_run WHERE id = {cancelled})"),
            "building",
        )

        self.assertEqual(
            self.sql(self.actor(WRITER_ID, "SELECT COUNT(*) FROM storage_v2_ingest_run WHERE source_id = 2;")),
            "0",
        )
        self.assertEqual(
            self.sql(
                self.actor(
                    WRITER_ID,
                    "SELECT "
                    "(SELECT COUNT(*) FROM storage_v2_ingest_run_item WHERE source_id = 2) || ':' || "
                    "(SELECT COUNT(*) FROM storage_v2_artifact_identity WHERE source_id = 2) || ':' || "
                    "(SELECT COUNT(*) FROM storage_v2_occurrence_identity WHERE source_id = 2) || ':' || "
                    "(SELECT COUNT(*) FROM storage_v2_append_frontier WHERE source_id = 2);",
                )
            ),
            "0:0:0:0",
        )
        self.assert_sql_fails(
            self.actor(
                WRITER_ID,
                f"SELECT storage_v2_begin_shadow_ingest(2, '{'c' * 64}', '{'d' * 64}', "
                "'fixture-adapter-v1', 'denied', '{}'::JSONB, FALSE);",
            ),
            "source write access denied",
        )
        self.assert_sql_fails(
            self.actor(
                WRITER_ID,
                "INSERT INTO storage_v2_ingest_run(source_id, generation_id, idempotency_key, "
                "semantic_manifest_sha256, adapter_profile_id) VALUES "
                f"(1, {run_id}, '{'a' * 64}', '{'b' * 64}', 'bypass');",
            ),
            "controlled function",
        )

    def test_authorized_writer_can_use_source_local_shadow_functions(self) -> None:
        node_id, view_id, digest_hex = self.make_projection("writer-owned")
        run_id = self.begin(3, "d" * 64, "e" * 64, user_id=WRITER_ID)
        self.stage(
            run_id,
            "writer.txt",
            "writer-owned",
            node_id,
            view_id,
            digest_hex,
            user_id=WRITER_ID,
        )
        self.complete_analysis(digest_hex)
        self.commit(run_id, 1, user_id=WRITER_ID)
        self.assertEqual(
            self.sql(
                self.actor(
                    WRITER_ID,
                    "SELECT status || ':' || staged_item_count FROM storage_v2_ingest_run "
                    f"WHERE id = {run_id};",
                )
            ),
            "sealed:1",
        )
        self.assertEqual(
            self.sql(
                f"SELECT (membership_delta_us >= 0 AND sealing_us >= 0) "
                f"FROM storage_v2_ingest_run WHERE id = {run_id};"
            ),
            "t",
        )
        self.assertEqual(
            self.sql(
                self.actor(
                    OTHER_ID,
                    f"SELECT COUNT(*) FROM storage_v2_ingest_run WHERE id = {run_id};",
                )
            ),
            "0",
        )
        self.assertEqual(
            self.sql(
                self.actor(
                    WRITER_ID,
                    f"SELECT COUNT(*) FROM storage_v2_analysis_cache "
                    f"WHERE content_identity_sha256 = decode('{digest_hex}', 'hex');",
                )
            ),
            "0",
            "global analysis cache must not be readable through source authority",
        )

    def test_intelligence_provenance_retry_and_round_trip(self) -> None:
        node_id, view_id, digest_hex = self.make_projection("fn alpha() {}")
        run_export = self.begin(4, "1" * 63 + "4", "2" * 63 + "4")
        self.stage(
            run_export,
            "src/lib.rs",
            "fn alpha() {}",
            node_id,
            view_id,
            digest_hex,
        )
        self.complete_analysis(digest_hex)
        self.commit(run_export, 1)
        run_import = self.begin(5, "1" * 63 + "5", "2" * 63 + "5")
        self.stage(
            run_import,
            "src/lib.rs",
            "fn alpha() {}",
            node_id,
            view_id,
            digest_hex,
        )
        self.commit(run_import, 1)

        artifact_id, occurrence_id = map(
            int,
            self.sql(
                f"SELECT artifact_version_id || ':' || occurrence_id "
                f"FROM storage_v2_ingest_run_item WHERE run_id = {run_export};"
            ).split(":"),
        )
        symbol_occurrence_id, symbol_id = map(
            int,
            self.sql(
                self.admin(
                    f"""
WITH occurrence_row AS (
    SELECT storage_v2_put_symbol_occurrence(
        4, {artifact_id}, {occurrence_id}, 'rust:src/lib.rs:alpha:function',
        'rust', 'function', 'crate::alpha', 'fn alpha()', 'synthetic docs',
        'public', '{{"kind":"function","calls":["crate::beta"]}}'::JSONB,
        '{{"line_start":1,"line_end":1}}'::JSONB
    ) AS value
)
SELECT (value).id || ':' || (value).symbol_id FROM occurrence_row;
"""
                )
            ).split(":"),
        )
        beta_occurrence_id, beta_symbol_id = map(
            int,
            self.sql(
                self.admin(
                    f"""
WITH occurrence_row AS (
    SELECT storage_v2_put_symbol_occurrence(
        4, {artifact_id}, {occurrence_id}, 'rust:src/lib.rs:beta:function',
        'rust', 'function', 'crate::beta', 'fn beta()', NULL,
        'private', '{{"kind":"function"}}'::JSONB,
        '{{"line_start":2,"line_end":2}}'::JSONB
    ) AS value
)
SELECT (value).id || ':' || (value).symbol_id FROM occurrence_row;
"""
                )
            ).split(":"),
        )

        self.sql(
            self.admin(
                "SELECT storage_v2_put_intelligence_profile("
                "4, 'fixture-domain', 1, "
                "'{\"fields\":{\"layer\":{\"rule\":\"public-api\"}}}'::JSONB);"
            )
        )
        provenance = (
            '{"layer":{"profile_id":"fixture-domain","profile_version":1,'
            '"rule_id":"public-api","evidence":"visibility=public"}}'
        )
        card_sql = self.admin(
            f"""
SELECT encode(output_sha256, 'hex') FROM storage_v2_put_symbol_card(
    {symbol_occurrence_id}, 'structural-v1/domain-fixture@1',
    '{{"name":"alpha","kind":"function","signature":"fn alpha()",'
    '"documentation":"synthetic docs","structure":{{"calls":1}}}}'::JSONB,
    '{{"layer":"api","side_effect":"unknown","resource":"unknown",'
    '"delegation_target":"unknown"}}'::JSONB,
    '{provenance}'::JSONB, 'fixture-domain', 1
);
"""
        )
        first_hash = self.sql(card_sql)
        self.assertEqual(self.sql(card_sql), first_hash, "normalized card output must be byte-stable")
        self.assert_sql_fails(
            self.admin(
                f"SELECT storage_v2_put_symbol_card({symbol_occurrence_id}, "
                "'structural-v1/domain-fixture@1', "
                "'{\"name\":\"alpha\",\"kind\":\"function\",\"structure\":{\"calls\":99}}'::JSONB, "
                "'{\"layer\":\"api\",\"side_effect\":\"unknown\","
                "\"resource\":\"unknown\",\"delegation_target\":\"unknown\"}'::JSONB, "
                f"'{provenance}'::JSONB, 'fixture-domain', 1);"
            ),
            "analysis profile output collision",
        )
        self.sql(
            self.admin(
                f"SELECT storage_v2_put_symbol_card({beta_occurrence_id}, 'structural-v1', "
                "'{\"name\":\"beta\",\"kind\":\"function\",\"signature\":\"fn beta()\","
                "\"documentation\":null,\"structure\":{}}'::JSONB, "
                "'{\"layer\":\"unknown\",\"side_effect\":\"unknown\","
                "\"resource\":\"unknown\",\"delegation_target\":\"unknown\"}'::JSONB, "
                "'{}'::JSONB, NULL, NULL);"
            )
        )
        bodies_before_profile_change = self.sql("SELECT COUNT(*) FROM content_body;")
        self.sql(
            self.admin(
                "SELECT storage_v2_put_intelligence_profile("
                "4, 'fixture-domain', 2, "
                "'{\"fields\":{\"layer\":{\"rule\":\"internal-api-v2\"}}}'::JSONB);"
            )
        )
        provenance_v2 = (
            '{"layer":{"profile_id":"fixture-domain","profile_version":2,'
            '"rule_id":"internal-api-v2","evidence":"fixture profile v2"}}'
        )
        second_profile_hash = self.sql(
            self.admin(
                f"SELECT encode(output_sha256, 'hex') FROM storage_v2_put_symbol_card("
                f"{symbol_occurrence_id}, 'structural-v1/domain-fixture@2', "
                "'{\"name\":\"alpha\",\"kind\":\"function\",\"signature\":\"fn alpha()\","
                "\"documentation\":\"synthetic docs\",\"structure\":{\"calls\":1}}'::JSONB, "
                "'{\"layer\":\"internal\",\"side_effect\":\"unknown\","
                "\"resource\":\"unknown\",\"delegation_target\":\"unknown\"}'::JSONB, "
                f"'{provenance_v2}'::JSONB, 'fixture-domain', 2);"
            )
        )
        self.assertNotEqual(first_hash, second_profile_hash)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM content_body;"), bodies_before_profile_change)
        self.assert_sql_fails(
            self.admin(
                f"SELECT storage_v2_put_symbol_card({beta_occurrence_id}, 'invalid', "
                "'{\"name\":\"beta\"}'::JSONB, '{\"layer\":\"guessed\"}'::JSONB, "
                "'{}'::JSONB, NULL, NULL);"
            ),
            "requires matching profile provenance",
        )
        self.assert_sql_fails(
            self.admin(
                f"SELECT storage_v2_put_symbol_card({beta_occurrence_id}, 'invented', "
                "'{\"name\":\"beta\"}'::JSONB, "
                "'{\"layer\":\"unknown\",\"invented\":\"guess\"}'::JSONB, "
                "'{}'::JSONB, NULL, NULL);"
            ),
            "unsupported domain field or provenance",
        )

        self.assertEqual(
            self.sql(
                self.admin(
                    f"SELECT status FROM storage_v2_begin_intelligence_analysis("
                    f"{symbol_occurrence_id}, 'structural-v1/domain-fixture@1');"
                )
            ),
            "pending",
        )
        self.assertEqual(
            self.sql(
                self.admin(
                    f"SELECT status FROM storage_v2_finish_intelligence_analysis("
                    f"{symbol_occurrence_id}, 'structural-v1/domain-fixture@1', NULL, 'fixture-failure');"
                )
            ),
            "failed",
        )
        self.sql(
            self.admin(
                f"SELECT storage_v2_begin_intelligence_analysis("
                f"{symbol_occurrence_id}, 'structural-v1/domain-fixture@1'); "
                f"SELECT storage_v2_finish_intelligence_analysis("
                f"{symbol_occurrence_id}, 'structural-v1/domain-fixture@1', "
                f"decode('{first_hash}', 'hex'), NULL);"
            )
        )
        self.assertEqual(
            self.sql(
                f"SELECT attempt_count || ':' || status FROM storage_v2_intelligence_analysis "
                f"WHERE symbol_occurrence_id = {symbol_occurrence_id};"
            ),
            "2:complete",
        )

        self.assertEqual(
            self.sql(
                self.admin(
                    f"SELECT storage_v2_record_call({symbol_occurrence_id}, {beta_symbol_id}, "
                    "'crate::beta', 'direct', "
                    "'{\"resolution_kind\":\"parser_symbol_id\",\"line\":1}'::JSONB);"
                )
            ),
            "proven",
        )
        self.assertEqual(
            self.sql(
                self.admin(
                    f"SELECT storage_v2_record_call({symbol_occurrence_id}, NULL, "
                    "'ambiguous', 'direct', '{\"resolution_kind\":\"ambiguous\",\"line\":1}'::JSONB, "
                    "'[\"candidate-a\",\"candidate-b\"]'::JSONB);"
                )
            ),
            "unresolved",
        )
        self.sql(
            self.admin(
                f"""
SELECT storage_v2_put_symbol_annotation(
    4, {symbol_id}, {symbol_occurrence_id}, 'review-note',
    '{{"text":"keep stable"}}'::JSONB, '{{"evidence":"user"}}'::JSONB,
    'user', NULL, NULL, 'synthetic-user'
);
WITH source_entity AS (
    SELECT storage_v2_put_intelligence_entity(
        4, 'entity:alpha', {symbol_id}, 'alpha', 'function', '{{"synthetic":true}}'::JSONB
    ) AS value
), target_entity AS (
    SELECT storage_v2_put_intelligence_entity(
        4, 'entity:beta', {beta_symbol_id}, 'beta', 'function', '{{"synthetic":true}}'::JSONB
    ) AS value
)
SELECT storage_v2_put_intelligence_relation(
    4, (source_entity.value).id, (target_entity.value).id, 'calls',
    '{{"resolution_kind":"parser_symbol_id","line":1}}'::JSONB
) FROM source_entity, target_entity;
SELECT storage_v2_put_negative_evidence(
    4, 'negative:no-gamma', 'gamma path', 'alpha -> gamma', 'no resolved target',
    '["rust:src/lib.rs:alpha:function"]'::JSONB, 'warning', 'synthetic-user'
);
"""
            )
        )

        changed_node, changed_view, changed_digest = self.make_projection(
            "fn alpha() { beta(); }"
        )
        changed_run = self.begin(4, "3" * 63 + "4", "4" * 63 + "4")
        self.stage(
            changed_run,
            "src/lib.rs",
            "fn alpha() { beta(); }",
            changed_node,
            changed_view,
            changed_digest,
        )
        self.complete_analysis(changed_digest)
        self.commit(changed_run, 1)
        changed_artifact_id, changed_occurrence_id = map(
            int,
            self.sql(
                f"SELECT artifact_version_id || ':' || occurrence_id "
                f"FROM storage_v2_ingest_run_item WHERE run_id = {changed_run};"
            ).split(":"),
        )
        changed_symbol_id = self.sql(
            self.admin(
                f"""
SELECT symbol_id FROM storage_v2_put_symbol_occurrence(
    4, {changed_artifact_id}, {changed_occurrence_id},
    'rust:src/lib.rs:alpha:function', 'rust', 'function', 'crate::alpha',
    'fn alpha()', 'synthetic docs', 'public',
    '{{"kind":"function","calls":["crate::beta"]}}'::JSONB,
    '{{"line_start":1,"line_end":1}}'::JSONB
);
"""
            )
        )
        self.assertEqual(changed_symbol_id, str(symbol_id))

        self.assertEqual(
            self.sql(
                self.admin(
                    "SELECT jsonb_array_length(storage_v2_intelligence_command("
                    "4, '1', 'card', '{\"name\":\"alpha\"}'::JSONB));"
                )
            ),
            "2",
        )
        self.assertEqual(
            self.sql(
                self.admin(
                    "SELECT storage_v2_intelligence_command("
                    "4, '1', 'card', '{\"name\":\"beta\"}'::JSONB) "
                    "-> 0 -> 'domain_fields' ->> 'layer';"
                )
            ),
            "unknown",
        )
        self.assertEqual(
            self.sql(
                self.admin(
                    "SELECT jsonb_array_length(storage_v2_intelligence_command("
                    "4, '1', 'layers', '{\"layer\":\"api\"}'::JSONB));"
                )
            ),
            "1",
        )
        self.assertEqual(
            self.sql(
                self.admin(
                    "SELECT jsonb_array_length(storage_v2_intelligence_command("
                    "4, '1', 'explain', '{\"name\":\"alpha\"}'::JSONB) -> 'proven');"
                )
            ),
            "1",
        )
        self.assertEqual(
            self.sql(
                self.admin(
                    "SELECT jsonb_array_length(storage_v2_export_intelligence("
                    "4, '2', 'protected') -> 'payload' -> 'call_edges');"
                )
            ),
            "0",
            "call evidence from an older occurrence must not leak into a changed generation",
        )
        self.assertEqual(
            self.sql(
                self.admin(
                    "SELECT jsonb_array_length(storage_v2_intelligence_command("
                    "4, '1', 'ownership', '{\"name\":\"alpha\"}'::JSONB));"
                )
            ),
            "1",
        )
        self.assert_sql_fails(
            self.actor(
                WRITER_ID,
                "SELECT storage_v2_intelligence_command("
                "4, '1', 'card', '{\"name\":\"alpha\"}'::JSONB);",
            ),
            "authorized generation selector required",
        )

        public_bundle = self.sql(
            self.admin("SELECT storage_v2_export_intelligence(4, '1', 'public')::TEXT;")
        )
        self.assertEqual(
            self.sql(self.admin("SELECT storage_v2_export_intelligence(4, '1', 'public')::TEXT;")),
            public_bundle,
        )
        self.assertNotIn("synthetic-user", public_bundle, "public export must redact authors")
        self.assertNotIn("synthetic docs", public_bundle, "public export must omit record content")
        self.assertEqual(
            self.sql(
                self.admin(
                    "SELECT (storage_v2_export_intelligence(4, '1', 'public') "
                    "-> 'payload' ->> 'protected_payload_sha256') = "
                    "(storage_v2_export_intelligence(4, '1', 'protected') "
                    "->> 'payload_sha256');"
                )
            ),
            "t",
            "public evidence must identify the exact protected payload without exposing it",
        )
        self.assertEqual(
            self.sql(
                self.admin(
                    "SELECT storage_v2_export_intelligence(4, '1', 'public') "
                    "-> 'payload' -> 'record_counts' ->> 'cards';"
                )
            ),
            "3",
        )
        escaped_public_bundle = public_bundle.replace("'", "''")
        self.assert_sql_fails(
            self.admin(
                f"SELECT storage_v2_import_intelligence(5, '1', "
                f"'{escaped_public_bundle}'::JSONB);"
            ),
            "authorized versioned intelligence bundle required",
        )
        protected_bundle = self.sql(
            self.admin("SELECT storage_v2_export_intelligence(4, '1', 'protected')::TEXT;")
        )
        self.assertIn("synthetic-user", protected_bundle)
        escaped_bundle = protected_bundle.replace("'", "''")
        import_counts = self.sql(
            self.admin(
                f"WITH imported AS (SELECT storage_v2_import_intelligence("
                f"5, '1', '{escaped_bundle}'::JSONB) AS value) "
                "SELECT (value ->> 'cards') || ':' || (value ->> 'negative_evidence') "
                "FROM imported;"
            )
        )
        self.assertEqual(import_counts, "3:1")
        self.assertEqual(
            self.sql(
                self.admin(
                    f"SELECT storage_v2_import_intelligence(5, '1', "
                    f"'{escaped_bundle}'::JSONB) ->> 'payload_sha256';"
                )
            ),
            self.sql(
                self.admin(
                    "SELECT storage_v2_export_intelligence(4, '1', 'protected') "
                    "->> 'payload_sha256';"
                )
            ),
            "reimporting the same bundle must be idempotent",
        )
        imported_payload = self.sql(
            self.admin(
                "SELECT storage_v2_export_intelligence(5, '1', 'protected') -> 'payload';"
            )
        )
        exported_payload = self.sql(
            self.admin(
                "SELECT storage_v2_export_intelligence(4, '1', 'protected') -> 'payload';"
            )
        )
        self.assertEqual(imported_payload, exported_payload)
        self.assertEqual(
            self.sql(
                "SELECT COUNT(*) FROM storage_v2_symbol_annotation "
                "WHERE source_id = 5 AND symbol_occurrence_id IS NOT NULL;"
            ),
            "1",
            "occurrence-bound annotations must remain occurrence-bound after import",
        )
        self.assertEqual(
            self.sql(
                self.actor(
                    WRITER_ID,
                    "SELECT COUNT(*) FROM storage_v2_symbol_card card "
                    "JOIN storage_v2_symbol_occurrence occurrence_row "
                    "ON occurrence_row.id = card.symbol_occurrence_id "
                    "WHERE occurrence_row.source_id = 4;",
                )
            ),
            "0",
        )


if __name__ == "__main__":
    unittest.main()
