"""Differential public fixtures for lossless byte locators and lazy FTS."""
import json
import hashlib
import unittest

from eval.storage_v2.schema import test_compact_exact_postings as base
from eval.storage_v2.schema.presence_reader_fixture import (
    register_metadata_role_cleanup, register_presence_role_cleanup,
)
from eval.storage_v2.schema.test_native_gc import G as native_gc


class DerivedRetrievalCodecTests(unittest.TestCase):
    schema = base.schema
    command = classmethod(base.CompactExactPostingTests.command.__func__)
    sql = classmethod(base.CompactExactPostingTests.sql.__func__)
    file = classmethod(base.CompactExactPostingTests.file.__func__)
    quote = staticmethod(base.CompactExactPostingTests.quote)
    make_projection = base.CompactExactPostingTests.make_projection
    begin = base.CompactExactPostingTests.begin
    stage = base.CompactExactPostingTests.stage
    complete_analysis = base.CompactExactPostingTests.complete_analysis
    commit = base.CompactExactPostingTests.commit
    assert_sql_fails = base.CompactExactPostingTests.assert_sql_fails
    exact_search = base.CompactExactPostingTests.exact_search

    @staticmethod
    def actor(user_id, statement):
        return f"SET ROLE mainrag; SET app.user_id='{user_id}'; {statement}"

    @classmethod
    def admin(cls, statement):
        return cls.actor(cls.schema.ADMIN_ID, statement)

    @classmethod
    def setUpClass(cls):
        try:
            base.CompactExactPostingTests.setUpClass.__func__(cls)
            for number in range(99, 150):
                if number == 123:
                    # The generic historical fixture grants its worker all
                    # functions; these production readers are private.
                    for signature in (
                        "storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)",
                        "storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)",
                    ):
                        cls.sql(f"REVOKE ALL ON FUNCTION {signature} FROM storage_v2_shadow_worker")
                if number == 135:
                    cls.sql("REVOKE ALL ON FUNCTION storage_v2_require_complete_active_set(TEXT) "
                            "FROM storage_v2_shadow_worker")
                if number == 145:
                    for signature in ("storage_v2_verify_generation(bigint,text)",
                                      "storage_v2_requalify_generation(bigint,text)"):
                        cls.sql(f"REVOKE ALL ON FUNCTION {signature} FROM storage_v2_shadow_worker")
                paths = list((cls.schema.ROOT / "migrations").glob(f"{number:03}_*.sql"))
                if len(paths) != 1:
                    raise AssertionError(f"one migration required for {number}")
                cls.file(paths[0])
                if number == 142:
                    register_presence_role_cleanup(cls.stack, cls.socket)
                if number == 145:
                    register_metadata_role_cleanup(cls.stack, cls.socket)
            instance = cls()
            fingerprints = {}
            for number in range(20000):
                suffix = ""
                value = number
                while True:
                    suffix += chr(97 + value % 26)
                    value //= 26
                    if not value:
                        break
                term = "codeccollision" + suffix
                fingerprint = hashlib.sha256(term.encode()).digest()[:2]
                if fingerprint in fingerprints:
                    cls.collision = fingerprints[fingerprint], term
                    break
                fingerprints[fingerprint] = term
            else:
                raise AssertionError("public fingerprint collision fixture missing")
            cls.cases = ["", "--- !!!", "ÄÖÜ Straße Καλημέρα 日本語 аБВ",
                         "alpha\u00a0beta\u2003gamma re\u0301sume\u0301",
                         "a__b 0123 -12.3 +42% api::call() a/b",
                         cls.collision[0] + "\n" + ("alpha βeta key_42 a/b api::call()\n" * 12000),
                         " ".join(f"term_{i}" for i in range(40000)),
                         "x" * 270000, "f" * 15 + " " + "g" * 16 + " " + "h" * 17
                         + " u" + "ä" * 7 + " uu" + "ä" * 7 + " u" + "ö" * 8 + " "
                         + "w" * 127 + " " + "y" * 128 + " " + "z" * 129
                         + "\n" + "alpha\n" * 45000]
            cls.nodes = [instance.make_projection(f"codec component {i}")[0]
                         for i in range(len(cls.cases))]
            cls.before = instance.snapshots(False)
            for number in range(150, 153):
                cls.file(next((cls.schema.ROOT / "migrations").glob(f"{number:03}_*.sql")))
        except BaseException:
            if hasattr(cls, "stack"):
                if hasattr(cls, "database"):
                    base.CompactExactPostingTests.tearDownClass.__func__(cls)
                else:
                    cls.stack.close()
            raise

    @classmethod
    def tearDownClass(cls):
        base.CompactExactPostingTests.tearDownClass.__func__(cls)

    def snapshots(self, derived):
        blocks = ("storage_v2_complete_document_posting_blocks(d.id)" if derived
                  else "storage_v2_compact_posting_block")
        block_scope = "" if derived else " WHERE b.document_id=d.id"
        vector = ("storage_v2_logical_document_fts(d.fts_simple,d.fts_simple_derived,d.search_text)"
                  if derived else "d.fts_simple")
        result = []
        for node, text in zip(self.nodes, self.cases):
            statement = self.admin("SELECT id FROM storage_v2_put_search_document("
                f"'derived-codec-differential','node',{node},{self.quote(text)},ARRAY['key_42']);")
            value = self.sql("BEGIN;" + statement + "RESET ROLE;" + f"""
SELECT jsonb_build_object('hash',encode(d.materialization_sha256,'hex'),
 'text',d.search_text,'tokens',d.token_count,'identifiers',storage_v2_document_exact_identifiers(d.id),
 'vector',({vector})::TEXT,
 'blocks',(SELECT coalesce(jsonb_agg(jsonb_build_object('order',b.block_order,
  'terms',b.terms,'frequency',b.term_frequencies,'fingerprints',b.fingerprints)
   ORDER BY b.block_order),'[]') FROM {blocks} b{block_scope}))
 FROM storage_v2_search_document d WHERE d.profile_id='derived-codec-differential' AND d.node_id={node};
ROLLBACK;""")
            result.append(json.loads(value.splitlines()[-1]))
        return result

    def test_complete_constructor_values_and_materialization_hashes(self):
        self.assertEqual(self.before, self.snapshots(True))
        print("complete old/new constructor comparisons:", len(self.cases), flush=True)

    def test_posting_readers_seals_collisions_and_mutation_rejection(self):
        node = self.nodes[5]
        text = self.cases[5]
        call = ("SELECT id FROM storage_v2_put_search_document("
                f"'derived-codec-readers','node',{node},{self.quote(text)},ARRAY['key_42'])")
        document = int(self.sql(self.admin(call)))
        self.assertEqual(str(document), self.sql(self.admin(call)))
        self.assertEqual(self.sql(f"SELECT count(*) FROM storage_v2_compact_posting_block "
                                  f"WHERE document_id={document}"), "0")
        self.assertNotEqual(self.sql(f"SELECT count(*) FROM storage_v2_byte_posting_block "
                                     f"WHERE document_id={document}"), "0")
        self.assertEqual(self.sql("BEGIN;" + native_gc.graph_sql([]) + f"""
SELECT NOT EXISTS(SELECT 1 FROM gc_mark WHERE kind='storage_v2_search_document' AND id={document})
 AND EXISTS(SELECT 1 FROM gc_dependents WHERE name='storage_v2_byte_posting_block' AND total>kept);
ROLLBACK;"""), "t")
        self.assertEqual(self.sql(f"""BEGIN;
CREATE TABLE codec_gc_unknown_reference(document_id BIGINT REFERENCES storage_v2_search_document(id));
INSERT INTO codec_gc_unknown_reference VALUES({document});
""" + native_gc.graph_sql([]) + f"""
SELECT EXISTS(SELECT 1 FROM gc_mark WHERE kind='storage_v2_search_document' AND id={document});
ROLLBACK;"""), "t")
        self.assert_sql_fails(self.admin(call.replace(self.quote(text), "'different text'")),
                              "profile collision")
        for term, expected in (("alpha", "12000"), ("a/b", "12000"), ("key_42", "12000"),
                               ("absent", "")):
            for function in (
                f"storage_v2_document_posting({document},{self.quote(term)})",
                f"storage_v2_posting_probe({self.quote(term)},4097)",
                f"storage_v2_scoped_query_posting(ARRAY[{document},{document},NULL]::BIGINT[],"
                f"ARRAY[{self.quote(term)}])",
                f"storage_v2_scoped_query_posting(ARRAY(SELECT {document}::BIGINT "
                f"FROM generate_series(1,2048)),ARRAY[{self.quote(term)}])",
            ):
                scope = f" WHERE document_id={document}" if function.startswith("storage_v2_posting_probe") else ""
                self.assertEqual(self.sql(self.admin(f"SELECT term_frequency FROM {function}{scope}")), expected)
        long_document = int(self.sql(self.admin("SELECT id FROM storage_v2_put_search_document("
            f"'derived-codec-long-readers','node',{self.nodes[7]},{self.quote(self.cases[7])},ARRAY[]::TEXT[])")))
        for function in (
            f"storage_v2_document_posting({long_document},{self.quote(self.cases[7])})",
            f"storage_v2_posting_probe({self.quote(self.cases[7])},1)",
            f"storage_v2_scoped_query_posting(ARRAY(SELECT {long_document}::BIGINT "
            f"FROM generate_series(1,2048)),ARRAY[{self.quote(self.cases[7])},NULL])",
        ):
            self.assertEqual(self.sql(self.admin(f"SELECT term_frequency FROM {function}")), "1")
        cutoff_document = int(self.sql(self.admin("SELECT id FROM storage_v2_put_search_document("
            f"'derived-codec-cutoff-readers','node',{self.nodes[8]},{self.quote(self.cases[8])},ARRAY[]::TEXT[])")))
        self.assertEqual(self.sql(f"SELECT bool_and(cache_max_term_bytes=16) "
                                  f"FROM storage_v2_byte_posting_block WHERE document_id={cutoff_document}"), "t")
        # Include ASCII in multibyte whitespace tokens so the established
        # classifier accepts them even with the fixture's C locale.
        terms = ["f" * 15, "g" * 16, "h" * 17, "u" + "ä" * 7,
                 "uu" + "ä" * 7, "u" + "ö" * 8, "w" * 127, "y" * 128, "z" * 129]
        for term in terms:
            value = self.quote(term)
            for function in (
                f"storage_v2_document_posting({cutoff_document},{value})",
                f"storage_v2_posting_probe({value},4097) WHERE document_id={cutoff_document}",
                f"storage_v2_scoped_query_posting(ARRAY[{cutoff_document},{cutoff_document},NULL]::BIGINT[],"
                f"ARRAY[{value},NULL,{value}])",
            ):
                self.assertEqual(self.sql(self.admin(f"SELECT term_frequency FROM {function}")), "1", function)
            self.assertEqual(self.sql(f"SELECT coalesce(bool_or({value}=ANY(cached_terms)),FALSE) "
                                      f"FROM storage_v2_byte_posting_block WHERE document_id={cutoff_document}"),
                             "t" if len(term.encode()) <= 16 else "f")
        self.assert_sql_fails("BEGIN;" + self.admin(f"""
WITH fresh AS (
 INSERT INTO storage_v2_search_document(profile_id,component_kind,node_id,search_text,
  token_count,exact_identifiers,materialization_sha256,exact_identifiers_derived)
 SELECT 'cache-negative-fixture',component_kind,node_id,search_text,token_count,
  exact_identifiers,materialization_sha256,exact_identifiers_derived
 FROM storage_v2_search_document WHERE id={document} RETURNING id
)
INSERT INTO storage_v2_byte_posting_block
 SELECT fresh.id,b.block_order,array_fill('corrupt-cache'::TEXT,ARRAY[cardinality(b.cached_terms)]),
  b.text_byte_starts,b.text_byte_lengths,b.term_frequencies,b.fingerprints
 FROM fresh CROSS JOIN storage_v2_byte_posting_block b WHERE b.document_id={document};
"""), "term cache differs")
        self.assertEqual(self.sql(self.admin(
            f"SELECT storage_v2_document_has_exact_identifier({document},'key_42')")), "t")
        self.assertEqual(self.sql(self.admin(
            f"SELECT count(*) FROM storage_v2_byte_posting_block WHERE document_id={document} "
            f"AND fingerprints @> ARRAY[storage_v2_posting_fingerprint({self.quote(self.collision[1])})]")), "1")
        self.assertEqual(self.sql(self.admin(f"SELECT count(*) FROM storage_v2_document_posting("
            f"{document},{self.quote(self.collision[1])})")), "0")
        for column in ("search_text", "fts_simple", "fts_simple_derived", "fts_simple_fingerprints"):
            self.assert_sql_fails(f"UPDATE storage_v2_search_document SET {column}=" +
                ("'changed'" if column == "search_text" else
                 "to_tsvector('simple','changed')" if column == "fts_simple" else
                 "FALSE" if column == "fts_simple_derived" else "ARRAY[1]")
                + f" WHERE id={document}", "immutable")
        self.assert_sql_fails(f"INSERT INTO storage_v2_byte_posting_block "
            f"SELECT document_id,block_order+100,cached_terms,text_byte_starts,text_byte_lengths,term_frequencies,fingerprints "
            f"FROM storage_v2_byte_posting_block WHERE document_id={document}", "sealed")

    def test_retained_128_byte_cache_and_new_cache_have_identical_readers(self):
        # Construct the retained layout with its real predecessor threshold;
        # the entire compatibility fixture and function replacement roll back.
        signature = "storage_v2_put_search_document(text,text,bigint,text,text[])"
        constructor = self.sql(f"SELECT pg_get_functiondef('{signature}'::REGPROCEDURE)")
        self.assertEqual(constructor.count("octet_length(posting.term)<=16"), 1)
        self.assertEqual(constructor.count(",16::SMALLINT"), 1)
        legacy = constructor.replace("octet_length(posting.term)<=16",
                                     "octet_length(posting.term)<=128").replace(",16::SMALLINT", ",128::SMALLINT")
        terms = ["f" * 15, "g" * 16, "h" * 17, "u" + "ä" * 7,
                 "uu" + "ä" * 7, "u" + "ö" * 8, "w" * 127, "y" * 128, "z" * 129]
        self.assertEqual(self.sql("BEGIN;" + legacy + ";" + self.admin(f"""
DO $compatibility$ DECLARE document BIGINT; value TEXT; cached BOOLEAN; BEGIN
 SELECT id INTO document FROM storage_v2_put_search_document(
  'retained-cache-compatibility','node',{self.nodes[8]},
  {self.quote(self.cases[8])},ARRAY[]::TEXT[]);
 IF NOT (SELECT bool_and(cache_max_term_bytes=128)
           FROM storage_v2_byte_posting_block WHERE document_id=document) THEN
  RAISE EXCEPTION 'retained cache bound differs';
 END IF;
 FOREACH value IN ARRAY ARRAY[{','.join(self.quote(t) for t in terms)}] LOOP
  IF (SELECT array_agg(term_frequency) FROM storage_v2_document_posting(document,value))
       IS DISTINCT FROM ARRAY[1::BIGINT]
    OR (SELECT array_agg(term_frequency) FROM storage_v2_posting_probe(value,4097) p
          WHERE p.document_id=document) IS DISTINCT FROM ARRAY[1::BIGINT]
    OR (SELECT array_agg(term_frequency) FROM storage_v2_scoped_query_posting(
          ARRAY[document,document,NULL],ARRAY[value,NULL,value])) IS DISTINCT FROM ARRAY[1::BIGINT] THEN
   RAISE EXCEPTION 'retained cache exact reader differs: %, document %, document reader %, probe %, scoped %',
    value,document,
    (SELECT array_agg(term_frequency) FROM storage_v2_document_posting(document,value)),
    (SELECT array_agg(term_frequency) FROM storage_v2_posting_probe(value,4097) p WHERE p.document_id=document),
    (SELECT array_agg(term_frequency) FROM storage_v2_scoped_query_posting(
          ARRAY[document,document,NULL],ARRAY[value,NULL,value]));
  END IF;
  SELECT coalesce(bool_or(value=ANY(cached_terms)),FALSE) INTO cached
    FROM storage_v2_byte_posting_block WHERE document_id=document;
  IF cached IS DISTINCT FROM (octet_length(value)<=128) THEN
   RAISE EXCEPTION 'retained cache byte boundary differs';
  END IF;
 END LOOP;
END $compatibility$;
SELECT 'retained-cache-compatible';
""") + "ROLLBACK;"), "retained-cache-compatible")

    def test_document_query_pruning_preserves_complex_and_negative_queries(self):
        text = self.cases[5]
        node = self.nodes[5]
        document = int(self.sql(self.admin("SELECT id FROM storage_v2_put_search_document("
            f"'derived-codec-query','node',{node},{self.quote(text)},ARRAY[]::TEXT[])")))
        for query in ("alpha", "alpha absent", "alpha OR absent", '"alpha βeta"',
                      "alpha -absent", "-alpha", "a/b", "key_42", *self.collision):
            value = self.sql(f"""WITH q AS(SELECT websearch_to_tsquery('simple',{self.quote(query)}) v,
 storage_v2_plain_document_fingerprints({self.quote(query)}) f)
 SELECT coalesce(storage_v2_safe_tsvector(d.search_text)@@q.v,FALSE)=coalesce(
  (q.f IS NULL OR d.fts_simple_fingerprints @> q.f)
  AND storage_v2_logical_document_fts(d.fts_simple,d.fts_simple_derived,d.search_text)@@q.v,FALSE)
 FROM storage_v2_search_document d CROSS JOIN q WHERE d.id={document}""")
            self.assertEqual(value, "t", query)

    def test_derived_segments_vectors_queries_replays_and_authorization(self):
        content = self.cases[5][:22000] + "\nlatesecondblock\n" + self.cases[5][22000:]
        content = content[:180000] + "\nlateutf8prefix\n" + content[180000:]
        parts = "ARRAY[" + ",".join(self.quote(content[start:start + 16000])
            for start in range(0, len(content), 16000)) + "]::TEXT[]"
        node, view = map(int, self.sql(self.admin(f"""
WITH leaves AS MATERIALIZED (
 SELECT leaf.id,p.n FROM unnest({parts}) WITH ORDINALITY p(text,n)
 CROSS JOIN LATERAL storage_v2_put_inline_body(convert_to(p.text,'UTF8')) body
 CROSS JOIN LATERAL storage_v2_put_leaf_node('codec-fixture','text',body.id) leaf
), root AS MATERIALIZED (
 SELECT * FROM storage_v2_put_internal_node('codec-fixture','artifact-root',{len(content.encode())},
  ARRAY(SELECT 'content'::TEXT FROM leaves ORDER BY n),ARRAY(SELECT id FROM leaves ORDER BY n))
), retrieval AS (
 SELECT view.id,root.id AS node_id FROM root
 CROSS JOIN LATERAL storage_v2_put_retrieval_view('chunk','fixture-view-v1','text','fixture-tokenizer-v1',0,
  ARRAY['content'],ARRAY['node'],ARRAY[root.id],ARRAY[0::BIGINT],ARRAY[{len(content.encode())}::BIGINT]) view
) SELECT node_id||':'||id FROM retrieval
""")).split(":"))
        digest = hashlib.sha256(content.encode()).hexdigest()
        source = int(self.sql("INSERT INTO sources(id,name,type,path) "
            "SELECT max(id)+1,'derived-codec-segments','fixture','synthetic-derived-codec' "
            "FROM sources RETURNING id"))
        run = self.begin(source, "d1" * 32, "d2" * 32)
        self.stage(run, "derived.txt", content, node, view, digest)
        self.complete_analysis(digest)
        document = int(self.sql(self.admin("SELECT id FROM storage_v2_put_search_document("
            f"'derived-codec-segment-document','node',{node},{self.quote(content)},ARRAY['key_42'])")))
        self.sql(self.admin(f"SELECT storage_v2_bind_search_document({view},0,{document},1.0)"))
        occurrence, artifact = map(int, self.sql(
            f"SELECT id||':'||artifact_version_id FROM occurrence WHERE view_id={view}"
            f" AND source_id={source}").split(":"))
        positions = list(range(0, 128 * 200, 200)) + [180000]
        texts = [content[start:start + 180] for start in positions]
        prefixes = ["public context α" for _ in texts]
        kinds = ["text" for _ in texts]

        def array(values, kind):
            return "ARRAY[" + ",".join(str(v) if isinstance(v, int) else self.quote(v)
                                       for v in values) + f"]::{kind}[]"

        args = (f"{occurrence},{artifact},{array(list(range(len(texts))), 'BIGINT')},"
                f"{array(texts, 'TEXT')},{array(prefixes, 'TEXT')},{array(kinds, 'TEXT')},"
                f"{array([p + 1 for p in positions], 'BIGINT')},"
                f"{array([len(content[:p].encode()) + 1 for p in positions], 'BIGINT')}")
        call = f"SELECT storage_v2_put_lexical_segments_located({args})"
        self.assertEqual(self.sql(self.admin(call)), str(len(texts)))
        self.assertEqual(self.sql(self.admin(call)), str(len(texts)))
        self.assertEqual(self.sql(f"SELECT count(*) FROM storage_v2_derived_lexical_block "
                                 f"WHERE occurrence_id={occurrence}"), "3")
        self.assertEqual(self.sql(f"SELECT count(*) FROM storage_v2_compact_lexical_block "
                                 f"WHERE occurrence_id={occurrence}"), "0")
        self.assertEqual(self.sql(self.admin(f"""
SELECT count(*) FROM unnest({array(texts, 'TEXT')},{array(prefixes, 'TEXT')},
 {array(kinds, 'TEXT')}) WITH ORDINALITY expected(text,prefix,kind,n)
 FULL JOIN storage_v2_lexical_segment_all actual
  ON actual.occurrence_id={occurrence} AND actual.segment_order=expected.n-1
 WHERE actual.occurrence_id={occurrence} AND (actual.text_sha256,actual.fts_vector)
 IS DISTINCT FROM (sha256(convert_to(expected.text,'UTF8')),
  setweight(to_tsvector('simple',expected.text),'A')
  ||setweight(to_tsvector('simple',expected.prefix),'B')
  ||setweight(to_tsvector('simple',expected.kind),'C'))
""")), "0")
        for query in ("alpha", "alpha absent", "alpha OR absent", '"alpha βeta"',
                      "alpha -absent", "-alpha", "key_42", "public", "text",
                      "latesecondblock", "lateutf8prefix", "lateutf8prefix OR absent",
                      *self.collision):
            expected = self.sql(f"SELECT count(*) FROM unnest({array(texts, 'TEXT')}) text "
                "WHERE (setweight(to_tsvector('simple',text),'A') "
                "||setweight(to_tsvector('simple','public context α'),'B') "
                "||setweight(to_tsvector('simple','text'),'C'))@@websearch_to_tsquery('simple',"
                + self.quote(query) + ")")
            function = (f"storage_v2_authorized_lexical_matches(ARRAY[{occurrence}]::BIGINT[],"
                        f"ARRAY[{source}]::BIGINT[],{self.quote(query)})")
            for user, count in ((self.schema.ADMIN_ID, expected), (self.schema.OTHER_ID, "0")):
                self.assertEqual(self.sql("SET ROLE mainrag_v2_frontier_owner; "
                    f"SET app.user_id='{user}'; SELECT count(*) FROM {function}"), count)
            first = self.sql(self.admin("SELECT min(segment_order) FROM "
                f"storage_v2_lexical_segment_all WHERE occurrence_id={occurrence} "
                f"AND fts_vector@@websearch_to_tsquery('simple',{self.quote(query)})"))
            if query == "latesecondblock":
                self.assertEqual(first, "110")
            if query.startswith("lateutf8prefix"):
                self.assertEqual(first, "128")
            for scope in (f"ARRAY[{occurrence},{occurrence},NULL]::BIGINT[]",
                          f"ARRAY(SELECT {occurrence}::BIGINT FROM generate_series(1,2048))"):
                candidate = (f"storage_v2_authorized_lexical_first_candidates({scope},"
                             f"ARRAY[{source},{source},NULL]::BIGINT[],{self.quote(query)})")
                prefix = "SET ROLE mainrag_v2_lexical_rank_owner; SET app.user_id="
                self.assertEqual(self.sql(prefix + self.quote(self.schema.ADMIN_ID)
                    + f"; SELECT segment_order FROM {candidate}"), first)
                self.assertEqual(self.sql(prefix + self.quote(self.schema.OTHER_ID)
                    + f"; SELECT count(*) FROM {candidate}"), "0")
        private_blocks = (f"storage_v2_derived_lexical_candidate_blocks({occurrence},{source},"
                          f"{artifact},NULL::INTEGER[])")
        self.assert_sql_fails(self.admin(f"SELECT count(*) FROM {private_blocks}"), "permission denied")
        for actor, expected in ((self.schema.ADMIN_ID, "3"), (self.schema.OTHER_ID, "0")):
            self.assertEqual(self.sql("SET ROLE mainrag_v2_frontier_owner;SET app.user_id="
                + self.quote(actor) + f";SELECT count(*) FROM {private_blocks}"), expected)
        self.assertEqual(self.sql("SET ROLE mainrag_v2_frontier_owner;SET app.user_id="
            + self.quote(self.schema.ADMIN_ID) + ";SELECT count(*) FROM "
            + private_blocks.replace(f",{artifact},", f",{artifact + 1000000},")), "0")
        # Reencode one owned fixture block transactionally to exercise a
        # retained cached block beside a derived block without changing bytes.
        self.assertEqual(self.sql(f"""BEGIN;
SET LOCAL app.user_id='{self.schema.ADMIN_ID}';
CREATE TEMP TABLE codec_mixed_block AS SELECT block.*,
 storage_v2_derived_lexical_vectors(occurrence_id,text_byte_starts,text_byte_lengths,
  context_prefixes,chunk_types) AS fts_vectors
 FROM storage_v2_derived_lexical_block block
 WHERE occurrence_id={occurrence} AND block_order=1;
ALTER TABLE storage_v2_derived_lexical_block DISABLE TRIGGER storage_v2_derived_lexical_immutable;
DELETE FROM storage_v2_derived_lexical_block WHERE occurrence_id={occurrence} AND block_order=1;
ALTER TABLE storage_v2_derived_lexical_block ENABLE TRIGGER storage_v2_derived_lexical_immutable;
INSERT INTO storage_v2_compact_lexical_block(occurrence_id,source_id,artifact_version_id,block_order,
 segment_orders,text_starts,text_lengths,text_hashes,context_prefixes,chunk_types,fts_vectors)
 SELECT occurrence_id,source_id,artifact_version_id,block_order,segment_orders,text_starts,text_lengths,
 text_hashes,context_prefixes,chunk_types,fts_vectors FROM codec_mixed_block;
SET LOCAL ROLE mainrag_v2_lexical_rank_owner;
SET LOCAL app.user_id='{self.schema.ADMIN_ID}';
SELECT (SELECT count(*)=1 AND min(segment_order)=0
 FROM storage_v2_authorized_lexical_first_candidates(ARRAY[{occurrence}]::BIGINT[],ARRAY[{source}]::BIGINT[],'alpha'))
 AND (SELECT count(*)=1 AND min(segment_order)=110
 FROM storage_v2_authorized_lexical_first_candidates(ARRAY[{occurrence}]::BIGINT[],ARRAY[{source}]::BIGINT[],'latesecondblock'))
 AND (SELECT count(*)=1 AND min(segment_order)=128
 FROM storage_v2_authorized_lexical_first_candidates(ARRAY[{occurrence}]::BIGINT[],ARRAY[{source}]::BIGINT[],'lateutf8prefix'));
ROLLBACK;"""), "t")
        self.assert_sql_fails(self.admin(call.replace(array(prefixes, "TEXT"),
            array(["changed"] * len(texts), "TEXT"))), "identity collision")
        self.assert_sql_fails(self.actor(self.schema.OTHER_ID, call), "authorized source-backed")
        self.assert_sql_fails(f"UPDATE storage_v2_derived_lexical_block SET fingerprints=ARRAY[1] "
                              f"WHERE occurrence_id={occurrence}", "immutable")
        self.commit(run, 1)
        self.sql(self.admin("SELECT storage_v2_verify_generation("
            f"(SELECT generation_id FROM storage_v2_ingest_run WHERE id={run}),repeat('f',64))"))
        envelope = self.exact_search({"type": "term", "value": "alpha"}, source_id=source)
        self.assertEqual(len(envelope["results"]), 1)
        self.assertEqual(self.sql("BEGIN;" + native_gc.graph_sql([]) + f"""
SELECT EXISTS(SELECT 1 FROM gc_mark WHERE kind='occurrence' AND id={occurrence})
 AND EXISTS(SELECT 1 FROM gc_mark WHERE kind='storage_v2_search_document' AND id={document})
 AND EXISTS(SELECT 1 FROM gc_dependents WHERE name='storage_v2_derived_lexical_block' AND total=kept AND total>=2);
ROLLBACK;"""), "t")
