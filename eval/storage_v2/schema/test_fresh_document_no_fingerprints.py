"""Actual fresh INSERT routing with frozen prior trigger and current readers.

One disposable cluster and one changed-format method. Temporary reference and
sentinel DDL roll back; production has no extra cache table or UPDATE path.
Stored column costs are diagnostics, not whole-database physical savings.
"""
from __future__ import annotations

import hashlib
import json
import unittest
import uuid

from eval.storage_v2.schema import test_retained_cache_no_fingerprints as previous
from eval.storage_v2.schema import test_bounded_lexical_verification as bounded

ROOT = previous.ROOT
MIGRATION = ROOT / 'migrations/170_storage_v2_fresh_document_without_fingerprints.sql'
PREPARE = 'storage_v2_prepare_document_vector()'
FINGERPRINTS = 'storage_v2_lexical_block_fingerprints(tsvector[])'


class FreshDocumentNoFingerprintTests(previous.RetainedCacheNoFingerprintsTests):
    test_no_fingerprint_storage_all_readers_metadata_replay_and_insert_sentinel = None
    test_mismatch_rollback_and_unchanged_standard_161_and_168_modes = None
    make_projection = bounded.BoundedLexicalVerificationTests.make_projection

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls.sql(f"SET app.user_id='{cls.schema.ADMIN_ID}';" +
                    (MIGRATION.parent / '167_storage_v2_cost_aware_posting_selection.sql').read_text())
            cls.old_prepare = cls.sql(f"SELECT pg_get_functiondef('{PREPARE}'::REGPROCEDURE)")
            cls.helper_definition = cls.sql(f"SELECT pg_get_functiondef('{FINGERPRINTS}'::REGPROCEDURE)")
            for original, name in ((cls.old_prepare, 'fixture170_original_prepare'),
                                   (cls.helper_definition, 'fixture170_original_fingerprints')):
                old_name = 'storage_v2_prepare_document_vector' if 'prepare' in name else 'storage_v2_lexical_block_fingerprints'
                definition = original.replace('FUNCTION public.' + old_name + '(',
                                              'FUNCTION public.' + name + '(', 1)
                if definition == original:
                    raise AssertionError('one exact original reference definition required')
                cls.sql(definition)
                arguments = '' if 'prepare' in name else 'TSVECTOR[]'
                cls.sql(f'ALTER FUNCTION {name}({arguments}) OWNER TO mainrag;'
                        f'REVOKE ALL ON FUNCTION {name}({arguments}) FROM PUBLIC;')
            cls.authority_before = cls.prepare_authority()
            cls.functions_before = cls.function_hashes()
            cls.contracts_before = cls.contracts()
            cls.sql(f"SET app.user_id='{cls.schema.ADMIN_ID}';" + MIGRATION.read_text())
        except BaseException:
            cls.base.tearDownClass.__func__(cls)
            raise

    @classmethod
    def prepare_authority(cls):
        return cls.sql("SELECT jsonb_build_object('oid',oid,'owner',proowner,'acl',proacl::TEXT,"
                       "'configuration',proconfig,'security_definer',prosecdef,'arguments',proargnames,"
                       "'result',prorettype,'volatility',provolatile,'parallel',proparallel) "
                       f"FROM pg_proc WHERE oid='{PREPARE}'::REGPROCEDURE")

    @classmethod
    def function_hashes(cls):
        return json.loads(cls.sql("SELECT jsonb_object_agg(oid::REGPROCEDURE::TEXT,"
                                  "encode(sha256(convert_to(pg_get_functiondef(oid),'UTF8')),'hex')) "
                                  "FROM pg_proc WHERE pronamespace='public'::REGNAMESPACE AND prokind='f'"))

    @classmethod
    def contracts(cls):
        return cls.sql("SELECT jsonb_build_object("
                       "'posting_functions',(SELECT jsonb_agg(to_jsonb(c) ORDER BY signature) FROM storage_v2_posting_conversion_contract c),"
                       "'posting_relations',(SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM storage_v2_posting_conversion_catalog_contract c),"
                       "'lexical_functions',(SELECT jsonb_agg(to_jsonb(c) ORDER BY signature) FROM storage_v2_lexical_conversion_function_contract c),"
                       "'lexical_relations',(SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM storage_v2_lexical_conversion_catalog_contract c),"
                       "'lexical_seal',(SELECT to_jsonb(c) FROM storage_v2_lexical_conversion_contract_seal c WHERE singleton))")

    def seed(self, text):
        token = uuid.uuid4().hex
        source = int(self.sql('INSERT INTO sources(id,name,type,path) '
                             f"SELECT max(id)+1,'fresh170-{token}','fixture','synthetic-fresh170' FROM sources RETURNING id"))
        node, view, digest = self.make_projection(text)
        run = self.begin(source, token * 2, uuid.uuid4().hex * 2)
        self.stage(run, 'fresh170.txt', text, node, view, digest)
        self.complete_analysis(digest)
        row = json.loads(self.sql('SELECT jsonb_build_object(\'occurrence\',o.id,\'artifact\',o.artifact_version_id,'
                                 "'generation',r.generation_id) FROM occurrence o "
                                 'JOIN storage_v2_ingest_run_item i ON i.occurrence_id=o.id '
                                 f'JOIN storage_v2_ingest_run r ON r.id=i.run_id WHERE r.id={run}'))
        return dict(row, source=source, node=node, view=view, run=run)

    def identifiers_sql(self, text):
        return ("ARRAY(SELECT DISTINCT token FROM regexp_split_to_table(lower(" + self.quote(text) +
                "),'[^[:alnum:]_]+') token WHERE token<>'' AND token ~ '[_0-9]' ORDER BY token)")

    def construct(self, graph, text, *, candidate, fallback=False):
        # Same previously seeded immutable graph and same source/run/occurrence
        # before and after. Reference document/binding/postings/commit/segments
        # all roll back; only sequence gaps survive that reference transaction.
        # The candidate is a genuinely new INSERT, not an existing-doc reuse.
        prefix = '' if candidate else (
            'ALTER TABLE storage_v2_search_document DISABLE TRIGGER storage_v2_search_document_vector;'
            'CREATE TRIGGER fixture170_reference_vector BEFORE INSERT ON storage_v2_search_document '
            'FOR EACH ROW EXECUTE FUNCTION fixture170_original_prepare();')
        if candidate:
            prefix += '''CREATE OR REPLACE FUNCTION storage_v2_lexical_block_fingerprints(p_vectors TSVECTOR[])
RETURNS INTEGER[] LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE SET search_path=pg_catalog,public AS $sentinel$
BEGIN
 IF EXISTS(SELECT 1 FROM unnest(p_vectors) value WHERE value IS NOT NULL) THEN
  RAISE EXCEPTION 'fresh nonnull vector invoked redundant fingerprint helper' USING ERRCODE='P1701';
 END IF;
 RETURN public.fixture170_original_fingerprints(p_vectors);
END $sentinel$;'''
        fingerprint_probe = '' if not candidate else """
 BEGIN
  PERFORM storage_v2_lexical_block_fingerprints(ARRAY[to_tsvector('simple','active sentinel')]);
 EXCEPTION WHEN SQLSTATE 'P1701' THEN armed:=TRUE;
 END;
 IF NOT armed THEN RAISE EXCEPTION 'fresh fingerprint sentinel is inactive';END IF;
"""
        restore = '' if not candidate else f'EXECUTE {self.quote(self.helper_definition)};'
        lexical = '' if fallback else (
            f"PERFORM storage_v2_put_lexical_segments_located({graph['occurrence']},{graph['artifact']},"
            # One real bounded prefix segment supplies this targeted rank gate;
            # the complete document/body/posting checks still cover all bytes.
            f"ARRAY[0]::BIGINT[],ARRAY[{self.quote(text[:512])}],ARRAY['heading'],ARRAY['text'],"
            'ARRAY[1]::BIGINT[],ARRAY[1]::BIGINT[]);')
        ranks = []
        if not fallback:
            for reader in self.rank_readers:
                actual = 'storage_v2_source_segment_rank_candidates' if reader.endswith('_scoped') else reader
                extra = f",ARRAY[{graph['source']}]::BIGINT[]" if reader.endswith('_scoped') else ''
                for query in ('alpha', '"alpha beta"', 'alpha -missing'):
                    ranks.append(f"({self.quote(reader + ':' + query)},(SELECT coalesce(jsonb_agg(to_jsonb(r) "
                                 'ORDER BY occurrence_id,score,segment_order),\'[]\'::JSONB) '
                                 f"FROM {actual}(ARRAY[{graph['occurrence']}]::BIGINT[],{self.quote(query)}{extra}) r))")
        rank_expression = ('(SELECT jsonb_object_agg(label,result) FROM (VALUES ' + ','.join(ranks) +
                           ') rank_results(label,result))') if ranks else "'{}'::JSONB"
        statement = f"""BEGIN;SET LOCAL app.user_id='{self.schema.ADMIN_ID}';SET LOCAL statement_timeout='30s';
{prefix}
DO $construct$
DECLARE document BIGINT; armed BOOLEAN:=FALSE;
BEGIN
 {fingerprint_probe}
 SELECT id INTO STRICT document FROM storage_v2_put_search_document(
  'mainrag.lexical-simple.v1','node',{graph['node']},{self.quote(text)},{self.identifiers_sql(text)});
 {restore}
 PERFORM storage_v2_bind_search_document({graph['view']},0,document,1.0);
 PERFORM storage_v2_commit_shadow_ingest({graph['run']},1,storage_v2_shadow_generation_root({graph['run']}));
 {lexical}
 PERFORM storage_v2_verify_generation({graph['generation']},'{ 'a0' * 32}');
END $construct$;
SELECT jsonb_build_object(
 'id',d.id,'logical',jsonb_build_object('profile',d.profile_id,'component',d.component_kind,'node',d.node_id,
   'text',encode(sha256(convert_to(d.search_text,'UTF8')),'hex'),'tokens',d.token_count,
   'materialization',encode(d.materialization_sha256,'hex'),
   'full_vector',encode(sha256(tsvectorsend(storage_v2_logical_document_fts(d.fts_simple,d.fts_simple_derived,d.search_text))),'hex'),
   'identifiers',encode(sha256(convert_to(to_jsonb(storage_v2_document_exact_identifiers(d.id))::TEXT,'UTF8')),'hex'),
   'pairs',(SELECT encode(sha256(convert_to(jsonb_agg(jsonb_build_array(term,frequency) ORDER BY term COLLATE "C")::TEXT,'UTF8')),'hex')
       FROM storage_v2_complete_document_posting_blocks(d.id) b
       CROSS JOIN LATERAL unnest(b.terms,b.term_frequencies) p(term,frequency)),
   'seal',EXISTS(SELECT 1 FROM storage_v2_document_postings_seal s WHERE s.document_id=d.id
                       AND s.materialization_sha256=d.materialization_sha256)),
 'cache',jsonb_build_object('derived',d.fts_simple_derived,'nonnull',d.fts_simple IS NOT NULL,
   'vector',encode(sha256(tsvectorsend(d.fts_simple)),'hex'),'fingerprints',d.fts_simple_fingerprints,
   'stored_bytes',coalesce(pg_column_size(d.fts_simple),0)::BIGINT+coalesce(pg_column_size(d.fts_simple_fingerprints),0)),
 'ranks',{rank_expression})
FROM storage_v2_search_document d WHERE profile_id='mainrag.lexical-simple.v1' AND node_id={graph['node']};
{'COMMIT' if candidate else 'ROLLBACK'};"""
        return json.loads(self.sql(statement))

    def test_fresh_insert_formats_helper_skip_logical_postings_readers_and_authority(self):
        self.assertEqual(self.authority_before, self.prepare_authority())
        changed = {signature for signature, sha in self.function_hashes().items()
                   if self.functions_before.get(signature) != sha}
        self.assertEqual(changed, {PREPARE})
        self.assertEqual(self.contracts_before, self.contracts())
        self.sql(f"SET app.user_id='{self.schema.ADMIN_ID}';SELECT storage_v2_posting_conversion_require_operator();"
                 'SELECT storage_v2_require_lexical_conversion_operator();')
        small = 'alpha beta gamma 日本語 key_170 co-op a.b\n'
        large = small + '\n'.join('alpha beta gamma delta epsilon zeta eta theta iota kappa lambda' for _ in range(5000))
        # Real program_limit_exceeded, not a replacement safe-vector function.
        # Each lexeme remains below the2047-byte lexer limit, while the real
        # complete vector exceeds1MiB. Fewer long terms keep this exception
        # fixture bounded without a large posting/identifier fanout.
        oversized = 'key_170 ' + ' '.join(f'word{n:06d}' +
            (hashlib.sha256(str(n).encode()).hexdigest()*30)[:1890] for n in range(600))
        self.assertGreater(len(large.encode()),262144)
        for label, text, fallback in (('small',small,False),('large',large,False),('fallback',oversized,True)):
            with self.subTest(label=label):
                graph = self.seed(text)
                before = self.construct(graph,text,candidate=False,fallback=fallback)
                self.assertEqual(self.sql(f"SELECT count(*) FROM storage_v2_search_document WHERE node_id={graph['node']}"),'0')
                after = self.construct(graph,text,candidate=True,fallback=fallback)
                self.assertNotEqual(before['id'],after['id'])
                self.assertEqual(before['logical'],after['logical'])
                self.assertTrue(after['logical']['seal'])
                self.assertEqual(before['ranks'],after['ranks'])
                if label == 'large':
                    self.assertTrue(after['cache']['derived']);self.assertTrue(after['cache']['nonnull'])
                    self.assertTrue(before['cache']['fingerprints']);self.assertIsNone(after['cache']['fingerprints'])
                    self.assertEqual(before['cache']['vector'],after['cache']['vector'])
                    self.assertLess(after['cache']['stored_bytes'],before['cache']['stored_bytes'])
                else:
                    self.assertEqual(before['cache'],after['cache'])
                    self.assertEqual(after['cache']['nonnull'],not fallback)
                doc = after['id']
                reuse = self.admin('SELECT id FROM storage_v2_put_search_document('
                                   f"'mainrag.lexical-simple.v1','node',{graph['node']},{self.quote(text)},{self.identifiers_sql(text)})")
                self.assertEqual(int(self.sql(reuse)),doc)
                self.assert_sql_fails(self.actor(self.schema.OTHER_ID,
                    f"SELECT storage_v2_put_search_document('denied170','node',{graph['node']},'denied',ARRAY[]::TEXT[])"),
                    'administrator authority')
                if not fallback:
                    for reader in self.rank_readers:
                        self.assertTrue(json.loads(self.rank(graph['occurrence'],'alpha',reader=reader)))
                        self.assertEqual(self.rank(graph['occurrence'],'alpha',actor=self.schema.OTHER_ID,reader=reader),'[]')
                    self.assertTrue(after['ranks']['storage_v2_source_segment_ranks_precise:"alpha beta"'])
                    envelope = self.envelope(graph['source'],{'type':'exact','value':'key_170'})
                    self.assertIn('key_170',envelope)
                self.assertEqual(self.functions_before[FINGERPRINTS],self.function_hashes()[FINGERPRINTS])
                self.assertEqual(self.contracts_before,self.contracts())


if __name__ == '__main__':
    unittest.main()
