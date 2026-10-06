"""Whole-document conjunction proof, indexed parity, and hidden-source guards."""
import json
import unittest

from eval.storage_v2.schema import test_bounded_lexical_verification as previous

MIGRATION = previous.base.schema.ROOT / "migrations/155_storage_v2_document_conjunction_proof.sql"


class DocumentConjunctionProofTests(unittest.TestCase):
    base = previous.BoundedLexicalVerificationTests
    schema = base.schema
    command = classmethod(base.command.__func__)
    sql = classmethod(base.sql.__func__)
    file = classmethod(base.file.__func__)
    actor = staticmethod(base.actor)
    admin = classmethod(base.admin.__func__)
    quote = staticmethod(base.quote)
    make_projection = base.make_projection
    begin = base.begin
    stage = base.stage
    complete_analysis = base.complete_analysis
    commit = base.commit
    assert_sql_fails = base.assert_sql_fails
    snapshots = base.snapshots
    fixture = base.fixture

    @classmethod
    def setUpClass(cls):
        cls.base.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        cls.base.tearDownClass.__func__(cls)

    def test_conjunction_across_segments_has_independent_body_proof(self):
        source = int(self.sql("INSERT INTO sources(id,name,type,path) SELECT max(id)+1,"
            "'conjunction-fixture','fixture','synthetic-conjunction' FROM sources RETURNING id"))
        content = "alpha / beta"
        node, view, digest = self.make_projection(content)
        run = self.begin(source, "b1" * 32, "b2" * 32, commit_sha="a" * 40)
        self.stage(run, "conjunction.txt", content, node, view, digest)
        self.complete_analysis(digest)
        document = int(self.sql(self.admin("SELECT id FROM storage_v2_put_search_document("
            f"'mainrag.lexical-simple.v1','node',{node},{self.quote(content)},ARRAY[]::TEXT[])")))
        self.sql(self.admin(f"SELECT storage_v2_bind_search_document({view},0,{document},1.0)"))
        self.commit(run, 1)
        generation, occurrence, artifact = map(int, self.sql(
            "SELECT r.generation_id||':'||o.id||':'||o.artifact_version_id FROM storage_v2_ingest_run r "
            f"JOIN occurrence o ON o.source_id=r.source_id WHERE r.id={run}").split(":"))
        self.sql(self.admin(f"SELECT storage_v2_verify_generation({generation},'{ 'c1' * 32}')"))
        self.sql(self.admin(f"SELECT storage_v2_put_lexical_segments_located({occurrence},{artifact},"
            "ARRAY[0,1]::BIGINT[],ARRAY['alpha','beta'],ARRAY['',''],ARRAY['text','text'],"
            "ARRAY[1,9]::BIGINT[],ARRAY[1,9]::BIGINT[])"))
        self.assertEqual(self.sql(self.admin(
            f"SELECT storage_v2_source_segment_body_matches({occurrence},'alpha beta')")), "f")
        # Retain a real document created with the predecessor's empty vector.
        large_text = "alpha beta\n" * 30000 + "foo_bar 日本語 trailing_term"
        large_node, _, _ = self.make_projection(large_text)
        old_document = int(self.sql(self.admin("SELECT id FROM storage_v2_put_search_document("
            f"'mainrag.lexical-simple.v1','node',{large_node},{self.quote(large_text)},ARRAY[]::TEXT[])")))
        original = self.sql(f"SELECT (to_jsonb(d)-'fts_simple')::TEXT FROM storage_v2_search_document d WHERE id={old_document}")
        self.assertEqual(self.sql(f"SELECT fts_simple IS NULL AND fts_simple_derived FROM storage_v2_search_document WHERE id={old_document}"), "t")
        self.file(MIGRATION)
        self.sql(f"UPDATE storage_v2_search_document SET fts_simple=strip(storage_v2_safe_tsvector(search_text)) WHERE id={old_document}")
        self.assertEqual(original, self.sql(f"SELECT (to_jsonb(d)-'fts_simple')::TEXT FROM storage_v2_search_document d WHERE id={old_document}"))
        self.assertEqual(self.sql(f"SELECT fts_simple=strip(storage_v2_safe_tsvector(search_text)) AND pg_column_size(fts_simple)<256 FROM storage_v2_search_document WHERE id={old_document}"), "t")
        self.assert_sql_fails(f"UPDATE storage_v2_search_document SET fts_simple=to_tsvector('simple','forged') WHERE id={old_document}", "immutable")
        new_document = int(self.sql(self.admin("SELECT id FROM storage_v2_put_search_document("
            f"'conjunction-new-cache-fixture','node',{large_node},{self.quote(large_text)},ARRAY[]::TEXT[])")))
        self.assertEqual(self.sql(f"SELECT fts_simple IS NOT NULL AND fts_simple_derived AND fts_simple=strip(storage_v2_safe_tsvector(search_text)) FROM storage_v2_search_document WHERE id={new_document}"), "t")
        for query in ("alpha beta", "foo_bar", "日本語 trailing_term", '"alpha beta"',
                      "alpha OR missing", "alpha -beta"):
            q = self.quote(query)
            # Phrase reconstruction retains the original positions; the stored
            # cache is exact only for predicates without positions.
            self.assertEqual(self.sql(f"SELECT storage_v2_logical_document_fts(fts_simple,fts_simple_derived,search_text) "
                f"@@ websearch_to_tsquery('simple',{q}) IS NOT DISTINCT FROM "
                f"(storage_v2_safe_tsvector(search_text)@@websearch_to_tsquery('simple',{q})) "
                f"FROM storage_v2_search_document WHERE id={new_document}"), "t")
        self.assertEqual(self.sql(self.admin(
            f"SELECT storage_v2_source_segment_body_matches({occurrence},'alpha beta')")), "t")
        self.assertEqual(self.sql(self.actor(self.schema.OTHER_ID,
            f"SELECT storage_v2_source_segment_body_matches({occurrence},'alpha beta')")), "f")
        self.assertEqual(self.sql(self.admin(
            f"SELECT storage_v2_source_segment_body_matches({occurrence},'alpha missing')")), "f")
        # A copied ranking projection can match the complete document while no
        # individual native segment contains both terms.
        self.sql(f"""WITH payload AS (
          INSERT INTO storage_v2_legacy_rank_payload(source_id,payload_sha256,fts_vector)
          VALUES({source},sha256(tsvectorsend(to_tsvector('simple','alpha beta'))),
                 to_tsvector('simple','alpha beta')) RETURNING id)
          INSERT INTO storage_v2_legacy_rank_binding(occurrence_id,source_id,artifact_version_id,
             legacy_chunk_id,legacy_file_hash,payload_id)
          SELECT {occurrence},{source},{artifact},10001,sha256(convert_to('alpha beta','UTF8')),id FROM payload""")
        result = json.loads(self.sql(self.admin(
            f"SELECT storage_v2_candidate_query_evidence({source},{generation},'{ 'a' * 40}',"
            f"'alpha beta',ARRAY[{occurrence}]::BIGINT[],ARRAY[]::BIGINT[])")))
        self.assertTrue(result["candidate"][0]["fts_body_matches"])
        self.assertTrue(result["candidate"][0]["body_text_matches"])
        self.assertTrue(result["candidate"][0]["segment_matches"])
        self.assertEqual(result["candidate"][0]["reference_frequency"], 0)
        # A cache proves a conjunction only when every exact lexeme first
        # occurs in the same segment; missing/different ordinals fall back.
        for terms, ordinals, expected in (("ARRAY['alpha','beta']", "ARRAY[1,1]", "7"),
                                          ("ARRAY['alpha','beta']", "ARRAY[1,2]", ""),
                                          ("ARRAY['alpha','missing']", "ARRAY[1,1]", "")):
            self.assertEqual(self.sql(self.admin(
                "SELECT storage_v2_cached_first_conjunction_order(ARRAY['alpha','beta'],"
                f"{ordinals}::SMALLINT[],ARRAY[7,9]::BIGINT[],{terms})")), expected)
        # Exercise the changed compact reader with four real immutable blocks.
        # Its result must equal the complete exact-vector minimum, with no
        # duplicate identities or hidden-source result.
        native, version = self.fixture("alpha beta\n" * 256)
        source = int(self.sql(f"SELECT source_id FROM occurrence WHERE id={native}"))
        self.sql(self.admin(f"SELECT storage_v2_put_lexical_segments_located({native},{version},"
            "ARRAY(SELECT n::BIGINT FROM generate_series(0,255) n),"
            "array_fill('alpha beta'::TEXT,ARRAY[256]),array_fill(''::TEXT,ARRAY[256]),"
            "array_fill('text'::TEXT,ARRAY[256]),"
            "ARRAY(SELECT (n*11+1)::BIGINT FROM generate_series(0,255) n),"
            "ARRAY(SELECT (n*11+1)::BIGINT FROM generate_series(0,255) n))"))
        for query in ("alpha beta", "ALPHA alpha beta", "alpha missing"):
            q = self.quote(query)
            statement = (f"SELECT min(segment_order) FROM storage_v2_authorized_lexical_first_candidates("
                         f"ARRAY[{native},{native}]::BIGINT[],ARRAY[{source}]::BIGINT[],{q})")
            expected = self.sql(self.admin(f"SELECT min(segment_order) FROM storage_v2_lexical_segment_all "
                f"WHERE occurrence_id={native} AND fts_vector@@websearch_to_tsquery('simple',{q})"))
            self.assertEqual(self.sql(f"SET app.user_id='{self.schema.ADMIN_ID}';"+statement), expected)
            self.assertEqual(self.sql(f"SET app.user_id='{self.schema.OTHER_ID}';"+statement), "")
