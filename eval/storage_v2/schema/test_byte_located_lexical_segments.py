"""UTF-8 locator parity, independent validation and immutable replay guards."""

from __future__ import annotations

import unittest
import uuid

from eval.storage_v2.schema import test_shadow_ingest_schema as schema
from eval.storage_v2.schema.test_candidate_projection_restore_schema import (
    CandidateProjectionSchemaTests,
)


SIGNATURE = (
    "storage_v2_put_lexical_segments_located"
    "(bigint,bigint,bigint[],text[],text[],text[],bigint[],bigint[])"
)


class ByteLocatedLexicalTests(unittest.TestCase):
    schema = schema
    command = classmethod(CandidateProjectionSchemaTests.command.__func__)
    sql = classmethod(schema.ShadowIngestSchemaTests.sql.__func__)
    file = classmethod(schema.ShadowIngestSchemaTests.file.__func__)
    admin = classmethod(schema.ShadowIngestSchemaTests.admin.__func__)
    actor = staticmethod(schema.ShadowIngestSchemaTests.actor)
    make_projection = schema.ShadowIngestSchemaTests.make_projection
    begin = schema.ShadowIngestSchemaTests.begin
    stage = schema.ShadowIngestSchemaTests.stage
    complete_analysis = schema.ShadowIngestSchemaTests.complete_analysis
    commit = schema.ShadowIngestSchemaTests.commit
    assert_sql_fails = schema.ShadowIngestSchemaTests.assert_sql_fails

    @classmethod
    def setUpClass(cls) -> None:
        schema.ShadowIngestSchemaTests.setUpClass.__func__(cls)
        for number in (66, 67, 68, 77, 85, 97):
            paths = list((schema.ROOT / "migrations").glob(f"{number:03}_*.sql"))
            if len(paths) != 1:
                raise AssertionError(f"one migration required for {number}")
            cls.file(paths[0])
        cls.sql("GRANT SELECT ON ALL TABLES IN SCHEMA public TO mainrag;")
        cls.sql(
            "GRANT EXECUTE ON FUNCTION " + SIGNATURE + ","
            "storage_v2_put_lexical_segments_at"
            "(bigint,bigint,bigint[],text[],text[],text[],bigint[]) "
            "TO storage_v2_shadow_worker;"
        )

    @classmethod
    def tearDownClass(cls) -> None:
        schema.ShadowIngestSchemaTests.tearDownClass.__func__(cls)

    def fixture(self, content: str) -> tuple[int, int]:
        source = int(self.sql(
            "INSERT INTO sources(id,name,type,path) "
            f"SELECT max(id)+1,'byte-locator-{uuid.uuid4().hex}','fixture','synthetic-byte-locator' "
            "FROM sources RETURNING id;"
        ))
        node, view, digest = self.make_projection(content)
        run = self.begin(source, uuid.uuid4().hex * 2, uuid.uuid4().hex * 2)
        self.stage(run, "locator.txt", content, node, view, digest)
        self.complete_analysis(digest)
        quoted = content.replace("'", "''")
        document = self.sql(self.admin(
            "SELECT id FROM storage_v2_put_search_document("
            f"'mainrag.lexical-simple.v1','node',{node},'{quoted}',ARRAY[]::TEXT[])"
        ))
        self.sql(self.admin(
            f"SELECT storage_v2_bind_search_document({view},0,{document},1.0)"
        ))
        self.commit(run, 1)
        return tuple(map(int, self.sql(
            "SELECT id::TEXT||':'||artifact_version_id::TEXT FROM occurrence "
            f"WHERE source_id={source}"
        ).split(":")))

    def call(self, occurrence, artifact, *, byte=8, character=4, order=10,
             text="甲Ωz", prefix="") -> str:
        return (
            f"SELECT storage_v2_put_lexical_segments_located({occurrence},{artifact},"
            f"ARRAY[{order}]::BIGINT[],ARRAY['{text}'],ARRAY['{prefix}'],"
            f"ARRAY['text'],ARRAY[{character}]::BIGINT[],ARRAY[{byte}]::BIGINT[]);"
        )

    def test_exact_parity_for_unordered_duplicate_unicode_locators(self):
        occurrence, artifact = self.fixture("é🙂x甲Ωz / é🙂x")
        arguments = (
            "ARRAY['甲Ωz','é🙂x','é🙂x','Ωz'],"
            "ARRAY['heading','','',''],ARRAY['text','text','text','text'],"
            "ARRAY[4,1,1,5]::BIGINT[]"
        )
        self.assertEqual(self.sql(self.admin(
            f"SELECT storage_v2_put_lexical_segments_at({occurrence},{artifact},"
            f"ARRAY[0,1,2,3]::BIGINT[],{arguments});"
        )), "4")
        located = (
            f"SELECT storage_v2_put_lexical_segments_located({occurrence},{artifact},"
            f"ARRAY[10,11,12,13]::BIGINT[],{arguments},ARRAY[8,1,1,11]::BIGINT[]);"
        )
        self.assertEqual(self.sql(self.admin(located)), "4")
        self.assertEqual(self.sql(self.admin(located)), "4")
        self.assertEqual(self.sql(
            "SELECT count(*) FROM storage_v2_lexical_segment old "
            "JOIN storage_v2_lexical_segment new ON new.occurrence_id=old.occurrence_id "
            "AND new.segment_order=old.segment_order+10 "
            f"WHERE old.occurrence_id={occurrence} AND old.segment_order<4 "
            "AND (to_jsonb(old)-'segment_order') IS NOT DISTINCT FROM "
            "(to_jsonb(new)-'segment_order');"
        ), "4")

    def test_rejects_forged_positions_content_collision_and_unauthorized_writer(self):
        occurrence, artifact = self.fixture("é🙂x甲Ωz")
        self.assert_sql_fails(self.admin(self.call(
            occurrence, artifact, character=3)), "valid source-backed")
        self.assert_sql_fails(self.admin(self.call(
            occurrence, artifact, byte=9, character=5, text="Ωz")), "UTF8")
        self.assert_sql_fails(self.admin(self.call(
            occurrence, artifact, text="甲Ωx")), "valid source-backed")
        self.assert_sql_fails(self.actor(schema.OTHER_ID, self.call(
            occurrence, artifact)), "authorized source-backed")
        self.assertEqual(self.sql(self.admin(self.call(occurrence, artifact))), "1")
        self.assert_sql_fails(self.admin(self.call(
            occurrence, artifact, prefix="different")), "identity collision")
        self.assertEqual(self.sql(
            f"SELECT count(*) FROM storage_v2_lexical_segment WHERE occurrence_id={occurrence}"
        ), "1")

    def test_late_unicode_prefix_is_counted_independently_and_batch_is_bounded(self):
        occurrence, artifact = self.fixture("é🙂x\n" * 2000 + "boundary end")
        self.assertEqual(self.sql(self.admin(self.call(
            occurrence, artifact, text="boundary", character=8001, byte=16001
        ))), "1")
        self.assert_sql_fails(self.admin(self.call(
            occurrence, artifact, text="boundary", character=16001, byte=16001,
            order=11
        )), "bounded source character and byte window")
        self.assert_sql_fails(self.admin(
            f"SELECT storage_v2_put_lexical_segments_located({occurrence},{artifact},"
            "ARRAY(SELECT n::BIGINT FROM generate_series(1,257) n),"
            "array_fill('é'::TEXT,ARRAY[257]),array_fill(''::TEXT,ARRAY[257]),"
            "array_fill('text'::TEXT,ARRAY[257]),array_fill(1::BIGINT,ARRAY[257]),"
            "array_fill(1::BIGINT,ARRAY[257]));"
        ), "bounded lexical segment group")

    def test_migration_replay_preserves_existing_resume_writers_and_definer(self):
        signature = "storage_v2_put_lexical_segments_at(bigint,bigint,bigint[],text[],text[],text[],bigint[])"
        original = self.sql(f"SELECT pg_get_functiondef('{signature}'::REGPROCEDURE)")
        migration = next((schema.ROOT / "migrations").glob("097_*.sql"))
        self.file(migration)
        self.file(migration)
        self.assertEqual(original, self.sql(
            f"SELECT pg_get_functiondef('{signature}'::REGPROCEDURE)"
        ))
        self.assertEqual(self.sql(
            "SELECT proowner::REGROLE::TEXT||':'||prosecdef::TEXT||':'||"
            "has_function_privilege('mainrag',oid,'EXECUTE')::TEXT "
            f"FROM pg_proc WHERE oid='{SIGNATURE}'::REGPROCEDURE;"
        ), "mainrag_v2_frontier_owner:true:true")


if __name__ == "__main__":
    unittest.main()
