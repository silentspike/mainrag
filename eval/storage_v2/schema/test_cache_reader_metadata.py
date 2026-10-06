"""Exact cache population preserves metadata; relevant mutations invalidate it."""
import unittest

from eval.storage_v2.schema import test_document_conjunction_proof as previous


class CacheReaderMetadataTests(unittest.TestCase):
    base = previous.DocumentConjunctionProofTests.base
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
        previous.DocumentConjunctionProofTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        previous.DocumentConjunctionProofTests.tearDownClass.__func__(cls)

    def test_exact_cache_keeps_ready_metadata_and_authorization(self):
        source = int(self.sql("INSERT INTO sources(id,name,type,path) SELECT max(id)+1,"
            "'cache-metadata-fixture','fixture','synthetic-cache' FROM sources RETURNING id"))
        text = "alpha beta\n" * 30000
        node, view, digest = self.make_projection(text)
        run = self.begin(source, "c1" * 32, "c2" * 32, commit_sha="a" * 40)
        self.stage(run, "cache.txt", text, node, view, digest)
        self.complete_analysis(digest)
        document = int(self.sql(self.admin("SELECT id FROM storage_v2_put_search_document("
            f"'mainrag.lexical-simple.v1','node',{node},{self.quote(text)},ARRAY[]::TEXT[])")))
        self.sql(self.admin(f"SELECT storage_v2_bind_search_document({view},0,{document},1.0)"))
        self.commit(run, 1)
        generation = int(self.sql(f"SELECT generation_id FROM storage_v2_ingest_run WHERE id={run}"))
        self.sql(self.admin(f"SELECT storage_v2_verify_generation({generation},'{ 'c3' * 32}')"))
        self.sql(self.admin(f"SELECT * FROM storage_v2_materialize_reader_metadata({generation})"))
        ready = f"SELECT storage_v2_reader_metadata_ready(ARRAY[{generation}]::BIGINT[])"
        before = self.sql(f"SELECT to_jsonb(h)::TEXT FROM storage_v2_reader_metadata_header h WHERE generation_id={generation}")
        self.assertEqual(self.sql(self.admin(ready)), "t")

        self.assertEqual(self.sql(f"SELECT fts_simple IS NULL AND fts_simple_derived FROM storage_v2_search_document WHERE id={document}"), "t")
        self.file(next(self.schema.ROOT.glob("migrations/155_*.sql")))
        self.file(next(self.schema.ROOT.glob("migrations/156_*.sql")))
        self.sql(f"UPDATE storage_v2_search_document SET fts_simple=strip(storage_v2_safe_tsvector(search_text)) WHERE id={document}")
        self.assertEqual(self.sql(self.admin(ready)), "t")
        self.assertEqual(before, self.sql(f"SELECT to_jsonb(h)::TEXT FROM storage_v2_reader_metadata_header h WHERE generation_id={generation}"))
        self.assertEqual(self.sql(self.actor(self.schema.OTHER_ID, ready)), "f")
        self.assert_sql_fails(f"UPDATE storage_v2_search_document SET fts_simple=to_tsvector('simple','forged') WHERE id={document}", "immutable")
        # Exercise the invalidation trigger independently of the immutable row
        # guard. A changed cached length must still force the complete fallback.
        self.assertEqual(self.sql("BEGIN; ALTER TABLE storage_v2_search_document DISABLE TRIGGER USER;"
            "ALTER TABLE storage_v2_search_document ENABLE TRIGGER storage_v2_reader_metadata_update;"
            f"UPDATE storage_v2_search_document SET token_count=token_count+1 WHERE id={document};"
            + self.admin(ready) + "ROLLBACK;"), "f")
        self.assertEqual(self.sql("BEGIN; TRUNCATE storage_v2_search_document CASCADE;"
            + self.admin(ready) + "ROLLBACK;"), "f")
        self.assertEqual(self.sql(self.admin(ready)), "t")
        self.assert_compact_first_match_preserves_boundary_and_metadata_terms()
        self.assert_derived_candidate_scope_and_exact_matches()
        self.assert_scoped_posting_collisions_and_frequencies()

    def assert_compact_first_match_preserves_boundary_and_metadata_terms(self):
        native, artifact = self.fixture("alphabeta\n" * 256)
        source = int(self.sql(f"SELECT source_id FROM occurrence WHERE id={native}"))
        # A valid native chunk may split a whole-document lexeme. Metadata can
        # also introduce terms absent from the canonical document vector.
        self.sql(self.admin(f"SELECT storage_v2_put_lexical_segments_located({native},{artifact},"
            "ARRAY(SELECT n::BIGINT FROM generate_series(0,255) n),"
            "array_fill('alpha'::TEXT,ARRAY[256]),array_fill('titleproof'::TEXT,ARRAY[256]),"
            "array_fill('text'::TEXT,ARRAY[256]),"
            "ARRAY(SELECT (n*10+1)::BIGINT FROM generate_series(0,255) n),"
            "ARRAY(SELECT (n*10+1)::BIGINT FROM generate_series(0,255) n))"))
        self.assertEqual(self.sql(f"SELECT count(*) FROM storage_v2_compact_lexical_block WHERE occurrence_id={native}"), "4")
        for query in ("alpha", "titleproof", "alpha titleproof", "unseencompactnonce", "alpha OR unseencompactnonce"):
            q = self.quote(query)
            expected = self.sql(self.admin("SELECT min(segment_order) FROM storage_v2_lexical_segment_all "
                f"WHERE occurrence_id={native} AND fts_vector@@websearch_to_tsquery('simple',{q})"))
            call = "SELECT min(segment_order) FROM storage_v2_authorized_lexical_first_candidates("
            call += f"ARRAY[{native},{native}]::BIGINT[],ARRAY[{source}]::BIGINT[],{q})"
            self.assertEqual(self.sql(f"SET app.user_id='{self.schema.ADMIN_ID}';" + call), expected)
            self.assertEqual(self.sql(f"SET app.user_id='{self.schema.OTHER_ID}';" + call), "")
            self.assert_sql_fails(self.admin(call), "permission denied")
        self.assertEqual(self.sql("BEGIN; SET LOCAL plan_cache_mode='force_custom_plan';"
            f"SET LOCAL app.user_id='{self.schema.ADMIN_ID}';"
            "SELECT min(segment_order) FROM storage_v2_authorized_lexical_first_candidates("
            f"ARRAY[{native}]::BIGINT[],ARRAY[{source}]::BIGINT[],'alpha');"
            "SELECT current_setting('plan_cache_mode');ROLLBACK;"), "0\nforce_custom_plan")

    def assert_derived_candidate_scope_and_exact_matches(self):
        _, native = self.base.projected(
            self, "alphabeta\n" * 40000, list(range(0, 2560, 10)),
            "derived", length=5,
        )
        source = int(self.sql(f"SELECT source_id FROM occurrence WHERE id={native}"))
        self.assertGreater(int(self.sql("SELECT count(*) FROM storage_v2_derived_lexical_block "
            f"WHERE occurrence_id={native}")), 1)
        for query in ("alpha", "ALPHA", "alph", "alphabeta", "β", "context", "alpha context", "text", "unseencompactnonce",
                      "alpha OR unseencompactnonce", '"alpha context"', "alpha -context"):
            q = self.quote(query)
            expected = self.sql(self.admin("SELECT min(segment_order) FROM storage_v2_lexical_segment_all "
                f"WHERE occurrence_id={native} AND fts_vector@@websearch_to_tsquery('simple',{q})"))
            scopes = [(f"ARRAY[{native},{native},NULL]::BIGINT[]", expected)]
            if query == "alpha":
                scopes += [("ARRAY[]::BIGINT[]", ""), ("ARRAY[NULL]::BIGINT[]", "")]
            for ids, wanted in scopes:
                call = "SELECT min(segment_order) FROM storage_v2_authorized_lexical_first_candidates("
                call += f"{ids},ARRAY[{source}]::BIGINT[],{q})"
                self.assertEqual(self.sql(f"SET app.user_id='{self.schema.ADMIN_ID}';" + call), wanted)
                self.assertEqual(self.sql(f"SET app.user_id='{self.schema.OTHER_ID}';" + call), "")
        private = "SELECT count(*) FROM storage_v2_derived_lexical_candidate_occurrences("
        private += f"ARRAY[{native}]::BIGINT[],ARRAY[{source}]::BIGINT[],NULL)"
        self.assert_sql_fails(self.admin(private), "permission denied")
        # Changing an unused mapping disables the guard without changing the
        # fixture's exact alpha predicate. Exercise the ordinary fallback.
        self.assertEqual(self.sql("BEGIN; ALTER TEXT SEARCH CONFIGURATION pg_catalog.simple "
            "DROP MAPPING FOR url;"
            f"SET LOCAL app.user_id='{self.schema.ADMIN_ID}';"
            "SELECT min(segment_order) FROM storage_v2_authorized_lexical_first_candidates("
            f"ARRAY[{native}]::BIGINT[],ARRAY[{source}]::BIGINT[],'alpha');ROLLBACK;"), "0")

    def assert_scoped_posting_collisions_and_frequencies(self):
        text = self.collision[0] + " " + " ".join(f"postingword{i}" for i in range(10000))
        node, _, _ = self.make_projection(text)
        document = int(self.sql(self.admin("SELECT id FROM storage_v2_put_search_document("
            f"'scoped-posting-filter-fixture','node',{node},{self.quote(text)},ARRAY[]::TEXT[])")))
        self.assertGreater(int(self.sql("SELECT count(*) FROM storage_v2_compact_posting_block "
            f"WHERE document_id={document}")), 1)
        values = [self.collision[1], "postingword42", "postingword43"]
        terms = "ARRAY[" + ",".join(self.quote(v) for v in values + values[:1]) + "]"
        scoped = ("SELECT coalesce(jsonb_agg(to_jsonb(p) ORDER BY p.term),'[]'::JSONB) FROM "
            "storage_v2_scoped_query_posting("
            f"array_fill({document}::BIGINT,ARRAY[1025]),{terms}) p")
        expected = ("SELECT coalesce(jsonb_agg(to_jsonb(p) ORDER BY p.term),'[]'::JSONB) FROM "
            f"(SELECT {document}::BIGINT AS document_id,posting.* FROM "
            f"unnest(ARRAY[{','.join(self.quote(v) for v in values)}]) term(value) "
            f"CROSS JOIN LATERAL storage_v2_document_posting({document},term.value) posting) p")
        self.assertEqual(self.sql(self.admin(scoped)), self.sql(self.admin(expected)))


if __name__ == "__main__":
    unittest.main()
