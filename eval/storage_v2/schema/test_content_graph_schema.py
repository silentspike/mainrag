#!/usr/bin/env python3
"""PostgreSQL invariants for content graphs, views, occurrences, and mappings."""

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
MIGRATION = ROOT / "migrations" / "031_storage_v2_content_graph.sql"
ADMIN_ID = "00000000-0000-4000-8000-000000000021"
READER_ID = "00000000-0000-4000-8000-000000000022"
DENIED_ID = "00000000-0000-4000-8000-000000000023"


class ContentGraphSchemaTests(unittest.TestCase):
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
                tempfile.TemporaryDirectory(prefix="mainrag-content-graph-")
            )
            postgres = cls.stack.enter_context(TemporaryPostgres(Path(temporary)))
            cls.socket = postgres.socket
        cls.database = f"storage_v2_graph_{uuid.uuid4().hex}"
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
CREATE TABLE fixture_source_access(
    user_id UUID NOT NULL,
    source_id BIGINT NOT NULL,
    can_read BOOLEAN NOT NULL,
    can_write BOOLEAN NOT NULL,
    PRIMARY KEY(user_id, source_id)
);
INSERT INTO users VALUES
    ('{ADMIN_ID}', TRUE), ('{READER_ID}', FALSE), ('{DENIED_ID}', FALSE);
INSERT INTO sources(id, name, type, path) VALUES
    (1, 'fixture-one', 'fixture', 'fixture-one'),
    (2, 'fixture-two', 'fixture', 'fixture-two');
INSERT INTO fixture_source_access VALUES ('{READER_ID}', 1, TRUE, FALSE);
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
CREATE ROLE storage_v2_graph_reader;
GRANT USAGE ON SCHEMA public TO storage_v2_graph_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO storage_v2_graph_reader;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO storage_v2_graph_reader;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO storage_v2_graph_reader;
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

    def assert_sql_fails(self, statement: str, expected: str) -> None:
        result = self.command("--command", statement, check=False)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(expected, result.stderr)

    @staticmethod
    def admin(statement: str) -> str:
        return f"SET app.user_id = '{ADMIN_ID}'; {statement}"

    @staticmethod
    def reader(user_id: str, statement: str) -> str:
        return f"SET ROLE storage_v2_graph_reader; SET app.user_id = '{user_id}'; {statement}"

    def test_lossless_identity_occurrence_isolation_and_legacy_mapping(self) -> None:
        self.assertEqual(
            self.sql(
                "SELECT encode(storage_v2_hash_parts('mainrag.compat.v1', ARRAY[convert_to('ab','UTF8'), convert_to('c','UTF8')]), 'hex')"
            ),
            "9ed0431a7ac7cebd650bb97fbaa8adbc53c9899f35f77c353a4dc474ffd98bbd",
        )
        self.sql(
            self.admin(
                """
INSERT INTO logical_source(id) VALUES (1), (2);
INSERT INTO source_item(source_id, item_key, item_kind) VALUES
    (1, 'artifact-one', 'document'),
    (2, 'artifact-two', 'document');
"""
            )
        )
        body_rows = self.sql(
            self.admin(
                """
SELECT id || ':' || encode(digest, 'hex')
  FROM storage_v2_put_inline_body(convert_to('alpha ', 'UTF8'));
SELECT id || ':' || encode(digest, 'hex')
  FROM storage_v2_put_inline_body(convert_to('omega ', 'UTF8'));
SELECT id || ':' || encode(digest, 'hex')
  FROM storage_v2_put_inline_body(convert_to(E'/* unknown */\n', 'UTF8'));
"""
            )
        ).splitlines()
        bodies = [row.split(":") for row in body_rows if ":" in row]
        self.assertEqual(len(bodies), 3)
        self.assertNotEqual(bodies[0][1], bodies[1][1])
        body_a, body_b, body_unknown = (int(row[0]) for row in bodies)

        leaf_rows = self.sql(
            self.admin(
                f"""
SELECT id || ':' || encode(node_digest, 'hex')
  FROM storage_v2_put_leaf_node('fixture', 'text', {body_a});
SELECT id || ':' || encode(node_digest, 'hex')
  FROM storage_v2_put_leaf_node('fixture', 'text', {body_b});
SELECT id || ':' || encode(node_digest, 'hex')
  FROM storage_v2_put_leaf_node('fixture', 'opaque-provider-block', {body_unknown});
"""
            )
        ).splitlines()
        leaves = [row.split(":") for row in leaf_rows if ":" in row]
        leaf_a, leaf_b, leaf_unknown = (int(row[0]) for row in leaves)
        self.assertNotEqual(leaves[0][1], leaves[1][1])

        internal_rows = self.sql(
            self.admin(
                f"""
SELECT id || ':' || encode(node_digest, 'hex')
  FROM storage_v2_put_internal_node(
    'fixture', 'artifact-root', 26,
    ARRAY['content', 'content', 'opaque'], ARRAY[{leaf_a}, {leaf_b}, {leaf_unknown}]
  );
SELECT id || ':' || encode(node_digest, 'hex')
  FROM storage_v2_put_internal_node(
    'fixture', 'artifact-root', 26,
    ARRAY['content', 'content', 'opaque'], ARRAY[{leaf_b}, {leaf_a}, {leaf_unknown}]
  );
SELECT id || ':' || encode(node_digest, 'hex')
  FROM storage_v2_put_internal_node(
    'fixture', 'artifact-root', 26,
    ARRAY['prefix', 'content', 'opaque'], ARRAY[{leaf_a}, {leaf_b}, {leaf_unknown}]
  );
SELECT id || ':' || encode(node_digest, 'hex')
  FROM storage_v2_put_internal_node(
    'fixture', 'different-root', 26,
    ARRAY['content', 'content', 'opaque'], ARRAY[{leaf_a}, {leaf_b}, {leaf_unknown}]
  );
"""
            )
        ).splitlines()
        internals = [row.split(":") for row in internal_rows if ":" in row]
        root_id = int(internals[0][0])
        self.assertEqual(len({row[1] for row in internals}), 4)

        self.assert_sql_fails(
            self.admin(
                f"SELECT storage_v2_put_internal_node('fixture', 'bad-length', 27, ARRAY['content', 'content', 'opaque'], ARRAY[{leaf_a}, {leaf_b}, {leaf_unknown}])"
            ),
            "internal node logical length is inconsistent",
        )

        self.assert_sql_fails(
            self.admin(
                f"INSERT INTO content_node_edge(parent_node_id, ordinal, edge_type, child_kind, child_node_id) VALUES ({leaf_a}, 0, 'invalid', 'text', {leaf_b})"
            ),
            "leaf content nodes cannot have children",
        )
        self.assert_sql_fails(
            self.admin(
                "INSERT INTO content_node(digest_schema, domain, node_type, logical_length, node_digest) VALUES ('content-node-v1', 'fixture', 'invalid-internal', 1, decode(repeat('00', 32), 'hex'))"
            ),
            "internal content nodes require at least one child",
        )

        view_rows = self.sql(
            self.admin(
                f"""
SELECT id || ':' || encode(view_digest, 'hex')
  FROM storage_v2_put_retrieval_view(
    'composed', 'profile-v1', 'unknown', 'tokenizer-v1', 3,
    ARRAY['prefix', 'body'], ARRAY['node', 'body'], ARRAY[{leaf_unknown}, {body_a}],
    ARRAY[0, 14], ARRAY[14, 20]
  );
SELECT id || ':' || encode(view_digest, 'hex')
  FROM storage_v2_put_retrieval_view(
    'composed', 'profile-v1', 'unknown', 'tokenizer-v1', 3,
    ARRAY['prefix', 'body'], ARRAY['node', 'body'], ARRAY[{leaf_unknown}, {body_a}],
    ARRAY[0, 14], ARRAY[14, 20]
  );
SELECT id || ':' || encode(view_digest, 'hex')
  FROM storage_v2_put_retrieval_view(
    'composed', 'profile-v1', 'unknown', 'tokenizer-v1', 3,
    ARRAY['context', 'body'], ARRAY['node', 'body'], ARRAY[{leaf_unknown}, {body_a}],
    ARRAY[0, 14], ARRAY[14, 20]
  );
"""
            )
        ).splitlines()
        views = [row.split(":") for row in view_rows if ":" in row]
        self.assertEqual(views[0], views[1])
        self.assertNotEqual(views[0][1], views[2][1])
        view_id = int(views[0][0])

        item_ids = self.sql("SELECT id FROM source_item ORDER BY source_id").splitlines()
        artifact_rows = self.sql(
            self.admin(
                f"""
INSERT INTO artifact_version(
    item_id, source_id, witness_type, witness, adapter_profile_id,
    content_root_node_id, expected_content_hash, byte_length
) VALUES
    ({item_ids[0]}, 1, 'fixture', '{{}}', 'fixture-v1', {root_id}, 'fixture-hash', 26),
    ({item_ids[1]}, 2, 'fixture', '{{}}', 'fixture-v1', {root_id}, 'fixture-hash', 26)
RETURNING id;
"""
            )
        ).splitlines()
        artifact_ids = [int(row) for row in artifact_rows if row.isdigit()]
        self.assertEqual(len(artifact_ids), 2)

        occurrence_rows = self.sql(
            self.admin(
                f"""
INSERT INTO occurrence(
    source_id, artifact_version_id, view_id, role, ordinal, source_path,
    locator, derivation_recipe, occurred_at
) VALUES
    (1, {artifact_ids[0]}, {view_id}, 'primary', 0, 'visible/path.txt',
     '{{"byte_start":0,"byte_end":20}}', '{{"kind":"ordered-components","unknown":{{"provider_field":7}}}}', NOW()),
    (2, {artifact_ids[1]}, {view_id}, 'primary', 0, 'secret/path.txt',
     '{{"byte_start":0,"byte_end":20}}', '{{"kind":"ordered-components"}}', NOW())
RETURNING id;
"""
            )
        ).splitlines()
        occurrence_ids = [int(row) for row in occurrence_rows if row.isdigit()]
        self.assertEqual(len(occurrence_ids), 2)

        self.assertEqual(
            self.sql(
                self.reader(
                    READER_ID,
                    "SELECT source_id || ':' || source_path FROM storage_v2_visible_occurrences()",
                )
            ),
            "1:visible/path.txt",
        )
        self.assertEqual(
            self.sql(
                self.reader(DENIED_ID, "SELECT COUNT(*) FROM storage_v2_visible_occurrences()")
            ),
            "0",
        )

        self.assert_sql_fails(
            self.admin(
                f"UPDATE occurrence SET source_path = 'mutated' WHERE id = {occurrence_ids[0]}"
            ),
            "occurrence rows are immutable",
        )
        self.assertEqual(
            self.sql(self.reader(READER_ID, "SELECT COUNT(*) FROM retrieval_view")),
            "0",
        )

        third_occurrence = int(
            self.sql(
                self.admin(
                    f"""
INSERT INTO occurrence(
    source_id, artifact_version_id, view_id, role, ordinal, source_path, locator
) VALUES (
    1, {artifact_ids[0]}, {view_id}, 'secondary', 1, 'visible/secondary.txt', '{{"byte_start":4}}'
) RETURNING id
"""
                )
            ).splitlines()[0]
        )
        mapping = self.sql(
            self.admin(
                f"""
SELECT occurrence_id || ':' || ordinal
  FROM storage_v2_replace_legacy_hit_mapping(
    'legacy-split', ARRAY[{occurrence_ids[0]}, {third_occurrence}], 'split',
    ARRAY[10, 10], ARRAY[8, 4]
  );
SELECT occurrence_id || ':' || ordinal
  FROM storage_v2_replace_legacy_hit_mapping(
    'legacy-merged', ARRAY[{third_occurrence}, {occurrence_ids[0]}], 'merged',
    ARRAY[5, 9], ARRAY[1, 9]
  );
SELECT occurrence_id || ':' || ordinal
  FROM storage_v2_replace_legacy_hit_mapping(
    'legacy-exact', ARRAY[{occurrence_ids[0]}], 'exact', ARRAY[20], ARRAY[0]
  );
"""
            )
        ).splitlines()
        mapping_rows = [row for row in mapping if ":" in row]
        self.assertEqual(mapping_rows[:2], [f"{third_occurrence}:0", f"{occurrence_ids[0]}:1"])
        self.assertEqual(mapping_rows[2:4], [f"{occurrence_ids[0]}:0", f"{third_occurrence}:1"])
        self.assertEqual(mapping_rows[4:], [f"{occurrence_ids[0]}:0"])
        self.assert_sql_fails(
            self.admin(
                f"INSERT INTO legacy_hit_mapping(old_hit_id, occurrence_id, ordinal, relation_kind, byte_overlap, source_offset) VALUES ('direct-write', {occurrence_ids[0]}, 0, 'exact', 20, 0)"
            ),
            "legacy mappings require the controlled replacement function",
        )
        self.assertEqual(
            self.sql(
                """
SELECT COUNT(*)
  FROM pg_constraint
 WHERE conrelid = 'legacy_hit_mapping'::REGCLASS
   AND pg_get_constraintdef(oid) LIKE '%old_hit_id%REFERENCES%'
"""
            ),
            "0",
        )


if __name__ == "__main__":
    unittest.main()
