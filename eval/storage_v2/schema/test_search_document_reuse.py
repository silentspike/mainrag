"""Real PostgreSQL regression checks for indexed, collision-safe document reuse."""

from __future__ import annotations

import json
import re

from eval.storage_v2.schema import test_shadow_ingest_schema as schema


MIGRATION = schema.ROOT / "migrations/043_storage_v2_indexed_search_document_reuse.sql"
PREVIOUS = schema.ROOT / "migrations/040_storage_v2_sparse_search_documents.sql"
PROFILE = "indexed-reuse-fixture-v1"


class SearchDocumentReuseTests(schema.ShadowIngestSchemaTests):
    """Run the complete inherited ingest/retrieval gate plus reuse regressions."""

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
