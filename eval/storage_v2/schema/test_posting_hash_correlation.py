"""Exact reader equivalence and sparse/dense posting scope references."""
import hashlib
import json
from unittest.mock import patch

from eval.storage_v2.schema import test_bound_native_rank_work as parent

MIGRATION = parent.schema.ROOT / "migrations/108_storage_v2_posting_hash_correlation.sql"


class PostingHashCorrelationTests(parent.BoundNativeRankTests):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.file(parent.schema.ROOT / "migrations/106_storage_v2_bound_native_ranks_and_fragment_groups.sql")
        cls.file(parent.schema.ROOT / "migrations/107_storage_v2_correlated_rank_identity_and_scoped_postings.sql")

    def test_complete_results_authorization_ties_and_replay(self):
        with patch.object(parent, "MIGRATION", MIGRATION):
            super().test_complete_results_authorization_ties_and_replay()

    def test_sparse_dense_duplicate_null_and_collision_scopes(self):
        # Independent flat and compact row references; no candidate helper in
        # the reference. The simplified namespace isolates physical scale from
        # unrelated content constructors and does not represent production RLS.
        seen = {}
        for i in range(20000):
            term = f"posting_collision_{i}"
            fingerprint = hashlib.sha256(term.encode()).digest()[:2]
            if fingerprint in seen:
                collision = seen[fingerprint], term
                break
            seen[fingerprint] = term
        else:
            self.fail("collision fixture unavailable")
        body = MIGRATION.read_text().split(
            "CREATE OR REPLACE FUNCTION public.storage_v2_scoped_term_posting", 1
        )[1].split("CREATE OR REPLACE FUNCTION public.storage_v2_source_segment_rank_candidates", 1)[0]
        body = "CREATE OR REPLACE FUNCTION public.storage_v2_scoped_term_posting" + body
        body = body.replace("public.storage_v2_scoped_term_posting", "posting_fixture.scoped")
        body = body.replace("public.storage_v2_search_document", "posting_fixture.document")
        body = body.replace("public.storage_v2_search_posting", "posting_fixture.flat")
        body = body.replace("public.storage_v2_compact_posting_block", "posting_fixture.compact")
        body = body.replace("public.storage_v2_document_posting", "posting_fixture.document_posting")
        point = self.sql("SELECT pg_get_functiondef(\'storage_v2_document_posting(bigint,text)\'::regprocedure)")
        point = point.replace("public.storage_v2_document_posting", "posting_fixture.document_posting")
        point = point.replace("public.storage_v2_search_posting", "posting_fixture.flat")
        point = point.replace("public.storage_v2_compact_posting_block", "posting_fixture.compact")
        setup = """
CREATE SCHEMA posting_fixture;
CREATE TABLE posting_fixture.document(id bigint PRIMARY KEY);
CREATE TABLE posting_fixture.flat(document_id bigint,term text,term_frequency bigint,
 term_sha256 bytea GENERATED ALWAYS AS (public.digest(term,'sha256')) STORED,
 PRIMARY KEY(document_id,term_sha256));
CREATE INDEX posting_fixture_term ON posting_fixture.flat USING hash(term);
CREATE TABLE posting_fixture.compact(document_id bigint,block_order bigint,
 terms text[],term_frequencies bigint[],fingerprints integer[],
 PRIMARY KEY(document_id,block_order));
CREATE INDEX posting_fixture_fingerprints ON posting_fixture.compact USING gin(fingerprints);
INSERT INTO posting_fixture.document SELECT i FROM generate_series(1,50000) i;
INSERT INTO posting_fixture.flat SELECT i,'needle_common',1 FROM generate_series(1,50000) i WHERE i%5<>0;
""" + f"""
INSERT INTO posting_fixture.compact SELECT i,0,
 ARRAY['needle_common',{self.quote(collision[0])},'noise'],ARRAY[2::bigint,3::bigint,4::bigint],
 public.storage_v2_posting_fingerprints(ARRAY['needle_common',{self.quote(collision[0])},'noise'])
 FROM generate_series(1,50000) i WHERE i%5=0;
ANALYZE posting_fixture.document; ANALYZE posting_fixture.flat; ANALYZE posting_fixture.compact;
""" + point + ";\n" + body
        self.sql(setup)
        scopes = ["ARRAY[]::bigint[]", "NULL::bigint[]", "ARRAY[NULL]::bigint[]",
                  "ARRAY[1,1,5,NULL]::bigint[]",
                  "ARRAY(SELECT i::bigint FROM generate_series(1,4500) i)",
                  "ARRAY(SELECT i::bigint FROM generate_series(1,6000) i)",
                  "ARRAY(SELECT i::bigint FROM generate_series(1,6000) i) || ARRAY[1,5,NULL]::bigint[]"]
        cases = [(scope, "needle_common") for scope in scopes]
        cases += [(scopes[-1], collision[0]), (scopes[-1], collision[1]),
                  (scopes[-1], "missing_term")]
        for scope, term in cases:
            value = json.loads(self.sql(f"""
WITH actual AS MATERIALIZED (SELECT * FROM posting_fixture.scoped({scope},{self.quote(term)})),
reference AS MATERIALIZED (
 SELECT document_id,term,term_frequency FROM posting_fixture.flat
 WHERE document_id=ANY({scope}) AND term={self.quote(term)}
 UNION ALL
 SELECT block.document_id,item.term,item.frequency
 FROM posting_fixture.compact block
 CROSS JOIN LATERAL unnest(block.terms,block.term_frequencies) item(term,frequency)
 WHERE block.document_id=ANY({scope}) AND item.term={self.quote(term)}
), a AS (SELECT coalesce(jsonb_agg(to_jsonb(actual) ORDER BY document_id,term,term_frequency),'[]'::jsonb) v FROM actual),
r AS (SELECT coalesce(jsonb_agg(to_jsonb(reference) ORDER BY document_id,term,term_frequency),'[]'::jsonb) v FROM reference)
SELECT jsonb_build_object('equal',a.v=r.v,'rows',jsonb_array_length(a.v)) FROM a,r
"""))
            self.assertTrue(value["equal"], (scope, term, value))
        self.assertEqual(self.sql("SELECT count(*) FROM posting_fixture.scoped(ARRAY[1,1,5,NULL]::bigint[],'needle_common')"), "2")
