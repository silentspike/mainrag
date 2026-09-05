"""Real PostgreSQL regression checks for indexed, collision-safe document reuse."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid

from eval.storage_v2.schema import test_shadow_ingest_schema as schema


MIGRATION = schema.ROOT / "migrations/043_storage_v2_indexed_search_document_reuse.sql"
PREVIOUS = schema.ROOT / "migrations/040_storage_v2_sparse_search_documents.sql"
PROFILE = "indexed-reuse-fixture-v1"


class SearchDocumentReuseTests(schema.ShadowIngestSchemaTests):
    """Run the complete inherited ingest/retrieval gate plus reuse regressions."""

    def start_client(self, application: str, statement: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            ["psql", "-X", "--no-psqlrc", "--quiet", "--set=ON_ERROR_STOP=1",
             "--tuples-only", "--no-align", "--host", str(self.socket),
             "--dbname", self.database, "--command", statement],
            cwd=schema.ROOT, env={**os.environ, "PGAPPNAME": application},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def wait_for_client(self, application: str, wait_event: str,
                        process: subprocess.Popen[str]) -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            self.assertIsNone(process.poll(), "fixture client exited before its barrier")
            if self.sql(
                "SELECT count(*) FROM pg_stat_activity "
                f"WHERE datname=current_database() AND application_name='{application}' "
                f"AND wait_event='{wait_event}'"
            ) == "1":
                return
            time.sleep(0.05)
        self.fail(f"fixture client never reached {wait_event}")

    def test_concurrent_insert_reuses_identity_and_rejects_conflicting_materializations(self) -> None:
        # Hold the winner's INSERT uncommitted until the second backend really
        # waits on its transaction. The loser must then execute the conflict
        # readback, not the ordinary already-visible-document shortcut.
        self.sql("""
CREATE TABLE fixture_reuse_barrier(released BOOLEAN NOT NULL);
INSERT INTO fixture_reuse_barrier VALUES (FALSE);
GRANT SELECT ON fixture_reuse_barrier TO storage_v2_shadow_worker;
""")
        cases = (
            ("same", "concurrent fixture", "ARRAY[' Concurrent_Key ', 'concurrent_key']"),
            ("text_collision", "different fixture", "ARRAY['concurrent_key']"),
            ("identifier_collision", "concurrent fixture", "ARRAY['different_key']"),
        )
        for kind in ("body", "node"):
            for outcome, text, identifiers in cases:
                with self.subTest(kind=kind, outcome=outcome):
                    node_id, _, _ = self.make_projection(f"concurrent {kind} {outcome}")
                    component_id = node_id if kind == "node" else int(self.sql(
                        f"SELECT body_id FROM content_node WHERE id={node_id}"
                    ))
                    winner_name = "fixture-winner-" + uuid.uuid4().hex
                    loser_name = "fixture-loser-" + uuid.uuid4().hex
                    self.sql("UPDATE fixture_reuse_barrier SET released=FALSE")
                    winner = self.start_client(winner_name, self.admin(f"""
BEGIN;
SET LOCAL statement_timeout='30s';
SELECT id FROM storage_v2_put_search_document(
    '{PROFILE}', '{kind}', {component_id}, 'concurrent fixture', ARRAY['concurrent_key']);
DO $$ BEGIN
    WHILE NOT (SELECT released FROM fixture_reuse_barrier) LOOP
        PERFORM pg_sleep(0.05);
    END LOOP;
END $$;
COMMIT;
"""))
                    loser = None
                    try:
                        self.wait_for_client(winner_name, "PgSleep", winner)
                        loser = self.start_client(loser_name, self.admin(
                            "SET statement_timeout='30s'; "
                            "SELECT id FROM storage_v2_put_search_document("
                            f"'{PROFILE}', '{kind}', {component_id}, '{text}', {identifiers});"
                        ))
                        self.wait_for_client(loser_name, "transactionid", loser)
                        self.sql("UPDATE fixture_reuse_barrier SET released=TRUE")
                        winner_out, winner_error = winner.communicate(timeout=10)
                        loser_out, loser_error = loser.communicate(timeout=10)
                        self.assertEqual(winner.returncode, 0, winner_error)
                        identity = int(winner_out.strip())
                        if outcome == "same":
                            self.assertEqual(loser.returncode, 0, loser_error)
                            self.assertEqual(int(loser_out.strip()), identity)
                        else:
                            self.assertNotEqual(loser.returncode, 0)
                            self.assertIn("search-document profile collision", loser_error)
                        self.assertEqual(self.sql(
                            "SELECT count(*) FROM storage_v2_search_document "
                            f"WHERE profile_id='{PROFILE}' AND component_kind='{kind}' "
                            f"AND {kind}_id={component_id}"
                        ), "1")
                        self.assertEqual(self.sql(
                            "SELECT search_text || ':' || array_to_string(exact_identifiers, ',') "
                            f"FROM storage_v2_search_document WHERE id={identity}"
                        ), "concurrent fixture:concurrent_key")
                        self.assertEqual(self.sql(
                            "SELECT string_agg(term || ':' || term_frequency, ',' ORDER BY term) "
                            f"FROM storage_v2_search_posting WHERE document_id={identity}"
                        ), "concurrent:1,fixture:1")
                    finally:
                        # Both clients belong to this disposable fixture. Always
                        # release its barrier and reap them, including failures.
                        try:
                            self.sql("UPDATE fixture_reuse_barrier SET released=TRUE")
                        finally:
                            for process in (loser, winner):
                                if process is not None:
                                    try:
                                        process.communicate(timeout=5)
                                    except subprocess.TimeoutExpired:
                                        process.kill()
                                        process.communicate(timeout=5)

    def test_indexed_reuse_preserves_rows_and_rejects_profile_collisions(self) -> None:
        node_id, _, _ = self.make_projection("indexed reuse fixture")
        body_id = int(self.sql(f"SELECT body_id FROM content_node WHERE id={node_id}"))
        identities = []
        for kind, component_id in (("body", body_id), ("node", node_id)):
            with self.subTest(kind=kind):
                statement = self.admin(
                    "SELECT id FROM storage_v2_put_search_document("
                    f"'{PROFILE}', '{kind}', {component_id}, 'indexed reuse fixture', "
                    "ARRAY[' Reuse_Key ', 'reuse_key', '']);"
                )
                identity = self.sql(statement)
                identities.append(identity)
                before = self.sql(
                    "SELECT md5(row_to_json(document)::TEXT) FROM "
                    f"storage_v2_search_document document WHERE id={identity}; "
                    "SELECT md5(jsonb_agg(to_jsonb(posting) ORDER BY term)::TEXT) "
                    f"FROM storage_v2_search_posting posting WHERE document_id={identity};"
                )
                self.file(PREVIOUS)
                self.file(MIGRATION)
                self.file(MIGRATION)
                self.assertEqual(self.sql(statement), identity)
                self.assertEqual(
                    self.sql(
                        "SELECT md5(row_to_json(document)::TEXT) FROM "
                        f"storage_v2_search_document document WHERE id={identity}; "
                        "SELECT md5(jsonb_agg(to_jsonb(posting) ORDER BY term)::TEXT) "
                        f"FROM storage_v2_search_posting posting WHERE document_id={identity};"
                    ),
                    before,
                )
                for text, identifiers in (
                    ("changed text", "ARRAY['reuse_key']"),
                    ("indexed reuse fixture", "ARRAY['changed_key']"),
                ):
                    self.assert_sql_fails(
                        self.admin(
                            "SELECT storage_v2_put_search_document("
                            f"'{PROFILE}', '{kind}', {component_id}, '{text}', {identifiers});"
                        ),
                        "search-document profile collision",
                    )
                self.assert_sql_fails(
                    self.actor(
                        schema.WRITER_ID,
                        "SELECT storage_v2_put_search_document("
                        f"'{PROFILE}', '{kind}', {component_id}, 'indexed reuse fixture', "
                        "ARRAY['reuse_key']);",
                    ),
                    "administrator authority",
                )
        self.assertNotEqual(*identities)

    def test_reuse_and_conflict_lookups_use_component_index_with_generic_plans(self) -> None:
        # Populate both component kinds under one profile: a prefix-only index
        # condition would still scan the complete kind/profile partition.
        # The disposable database owner creates this fixture-only relation;
        # production workers deliberately do not have schema CREATE rights.
        self.sql(f"SET app.user_id = '{schema.ADMIN_ID}';" + f"""
CREATE TABLE fixture_indexed_reuse_components AS
WITH bodies AS MATERIALIZED (
    SELECT body.id FROM generate_series(1, 5000) value
    CROSS JOIN LATERAL storage_v2_put_inline_body(
        convert_to('indexed reuse body ' || value, 'UTF8')
    ) body
)
SELECT bodies.id AS body_id, node.id AS node_id FROM bodies
CROSS JOIN LATERAL storage_v2_put_leaf_node('indexed-reuse-fixture', 'text', bodies.id) node;
SELECT COUNT(*) FROM fixture_indexed_reuse_components component
CROSS JOIN LATERAL storage_v2_put_search_document(
    '{PROFILE}', 'body', component.body_id, 'body fixture', ARRAY[]::TEXT[]
) document;
SELECT COUNT(*) FROM fixture_indexed_reuse_components component
CROSS JOIN LATERAL storage_v2_put_search_document(
    '{PROFILE}', 'node', component.node_id, 'node fixture', ARRAY[]::TEXT[]
) document;
""")
        self.sql("ANALYZE storage_v2_search_document;")
        body_id, node_id = map(int, self.sql(
            "SELECT body_id || ':' || node_id FROM fixture_indexed_reuse_components "
            "ORDER BY body_id LIMIT 1"
        ).split(":"))
        definition = self.sql(
            "SELECT pg_get_functiondef('storage_v2_put_search_document"
            "(text,text,bigint,text,text[])'::REGPROCEDURE)"
        )
        # Exercise the actual statements installed by the migration, including
        # the insert-conflict readback, instead of a separately rewritten query.
        lookups = re.findall(
            r"SELECT \* INTO (?:STRICT )?v_document\s+"
            r"FROM storage_v2_search_document\s+WHERE.*?;",
            definition, re.DOTALL,
        )
        self.assertEqual(len(lookups), 4)
        for index, lookup in enumerate(lookups):
            kind = "body" if "component_kind = 'body'" in lookup else "node"
            with self.subTest(kind=kind, statement=index):
                query = re.sub(r" INTO (?:STRICT )?v_document", "", lookup)
                query = query.replace("p_profile_id", "$1").replace("p_component_id", "$2")
                component_id = body_id if kind == "body" else node_id
                plan = json.loads(self.sql(
                    "SET plan_cache_mode=force_generic_plan; "
                    f"PREPARE fixture_lookup(TEXT, BIGINT) AS {query} "
                    "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
                    f"EXECUTE fixture_lookup('{PROFILE}', {component_id});"
                ))[0]["Plan"]
                self.assertEqual(plan["Node Type"], "Index Scan", plan)
                self.assertEqual(plan["Index Name"], "uq_storage_v2_search_document_component")
                self.assertIn(f"{kind}_id = $2", plan["Index Cond"])
                self.assertEqual(plan["Actual Rows"], 1)
                self.assertEqual(plan.get("Rows Removed by Filter", 0), 0)
