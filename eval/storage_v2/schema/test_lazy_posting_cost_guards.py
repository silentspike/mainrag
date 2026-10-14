"""One real167/173 parity gate and rolled-back observable classifier sentinel.

Uses the existing owned Unix PostgreSQL fixture. Synthetic public documents
exercise all decision predicates; this is correctness, not a speed benchmark.
"""
from __future__ import annotations

import hashlib
import json
import unittest

from eval.storage_v2.schema import test_fresh_document_no_fingerprints as previous

ROOT = previous.ROOT
MIGRATION = ROOT / 'migrations/173_storage_v2_lazy_posting_cost_guards.sql'
PRODUCER = 'storage_v2_put_search_document(text,text,bigint,text,text[])'
PREDECESSOR = '2c720c52064f44b8f3f47472a0d58a9fb5c531a21dd8b8cb94b32882ce49eadd'
REFERENCE = 'fixture173_original_put_search_document'


class LazyPostingCostGuardsTests(previous.FreshDocumentNoFingerprintTests):
    test_fresh_insert_formats_helper_skip_logical_postings_readers_and_authority = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls.old_producer = cls.sql(f"SELECT pg_get_functiondef('{PRODUCER}'::REGPROCEDURE)")
            if cls.function_hashes()[PRODUCER] != PREDECESSOR:
                raise AssertionError('actual167 producer predecessor required')
            definition = cls.old_producer.replace(
                'FUNCTION public.storage_v2_put_search_document(',
                'FUNCTION public.' + REFERENCE + '(', 1)
            if definition == cls.old_producer:
                raise AssertionError('one exact original producer reference required')
            cls.sql(definition)
            cls.sql(f'ALTER FUNCTION {REFERENCE}(TEXT,TEXT,BIGINT,TEXT,TEXT[]) OWNER TO mainrag;'
                    f'REVOKE ALL ON FUNCTION {REFERENCE}(TEXT,TEXT,BIGINT,TEXT,TEXT[]) FROM PUBLIC;')
            cls.producer_authority_before = cls.producer_authority()
            cls.functions_before173 = cls.function_hashes()
            cls.contracts_before173 = json.loads(cls.contracts())
            cls.sql(f"SET app.user_id='{cls.schema.ADMIN_ID}';" + MIGRATION.read_text())
            cls.new_producer = cls.sql(f"SELECT pg_get_functiondef('{PRODUCER}'::REGPROCEDURE)")
            cls.contracts_after173 = json.loads(cls.contracts())
        except BaseException:
            cls.base.tearDownClass.__func__(cls)
            raise

    @classmethod
    def producer_authority(cls):
        return cls.sql("SELECT jsonb_build_object('oid',oid,'owner',proowner,'acl',proacl::TEXT,"
                       "'configuration',proconfig,'security_definer',prosecdef,'arguments',proargnames,"
                       "'result',prorettype,'volatility',provolatile,'parallel',proparallel) "
                       f"FROM pg_proc WHERE oid='{PRODUCER}'::REGPROCEDURE")

    def construct173(self, graph, text, *, candidate):
        producer = 'storage_v2_put_search_document' if candidate else REFERENCE
        ranks = []
        for reader in self.rank_readers:
            actual = 'storage_v2_source_segment_rank_candidates' if reader.endswith('_scoped') else reader
            extra = f",ARRAY[{graph['source']}]::BIGINT[]" if reader.endswith('_scoped') else ''
            for query in ('alpha', '"alpha beta"', 'alpha -missing'):
                ranks.append(f"({self.quote(reader + ':' + query)},(SELECT coalesce(jsonb_agg(to_jsonb(r) "
                             "ORDER BY occurrence_id,score,segment_order),'[]'::JSONB) "
                             f"FROM {actual}(ARRAY[{graph['occurrence']}]::BIGINT[],{self.quote(query)}{extra}) r))")
        statement = f"""BEGIN;SET LOCAL app.user_id='{self.schema.ADMIN_ID}';SET LOCAL statement_timeout='30s';
DO $construct$
DECLARE document BIGINT;
BEGIN
 SELECT id INTO STRICT document FROM {producer}(
  'mainrag.lexical-simple.v1','node',{graph['node']},{self.quote(text)},{self.identifiers_sql(text)});
 PERFORM storage_v2_bind_search_document({graph['view']},0,document,1.0);
 PERFORM storage_v2_commit_shadow_ingest({graph['run']},1,storage_v2_shadow_generation_root({graph['run']}));
 PERFORM storage_v2_put_lexical_segments_located({graph['occurrence']},{graph['artifact']},
  ARRAY[0]::BIGINT[],ARRAY[{self.quote(text[:512])}],ARRAY['heading'],ARRAY['text'],
  ARRAY[1]::BIGINT[],ARRAY[1]::BIGINT[]);
 PERFORM storage_v2_verify_generation({graph['generation']},'{ 'a3' * 32}');
END $construct$;
WITH pairs AS MATERIALIZED (
 SELECT term,frequency,row_number() OVER(ORDER BY term COLLATE "C") AS ordinal,octet_length(term) AS byte_length
 FROM storage_v2_search_document d CROSS JOIN LATERAL storage_v2_complete_document_posting_blocks(d.id) b
 CROSS JOIN LATERAL unnest(b.terms,b.term_frequencies) p(term,frequency) WHERE d.node_id={graph['node']}
), arrays AS (
 SELECT (ordinal-1)/256 AS block_order,count(*)::BIGINT AS term_count,
  array_agg(term ORDER BY ordinal) AS terms,
  array_agg(CASE WHEN byte_length<=16 THEN term END ORDER BY ordinal) AS cached,
  array_agg(frequency ORDER BY ordinal) AS frequencies,
  array_agg(1::INTEGER ORDER BY ordinal) AS starts,array_agg(byte_length ORDER BY ordinal) AS lengths
 FROM pairs GROUP BY (ordinal-1)/256
), model AS (
 SELECT coalesce(sum(48+pg_column_size(terms)+pg_column_size(frequencies)+24+4*term_count),0)::BIGINT AS compact,
  coalesce(sum(56+pg_column_size(cached)+pg_column_size(frequencies)+pg_column_size(starts)+pg_column_size(lengths)
   +24+4*term_count),0)::BIGINT AS bytes,count(*)::BIGINT AS blocks FROM arrays
)
SELECT jsonb_build_object('id',d.id,
 'logical',jsonb_build_object('profile',d.profile_id,'component',d.component_kind,'body',d.body_id,'node',d.node_id,
  'text',encode(sha256(convert_to(d.search_text,'UTF8')),'hex'),'tokens',d.token_count,
  'materialization',encode(d.materialization_sha256,'hex'),
  'full_vector',encode(sha256(tsvectorsend(storage_v2_logical_document_fts(d.fts_simple,d.fts_simple_derived,d.search_text))),'hex'),
  'identifiers',to_jsonb(storage_v2_document_exact_identifiers(d.id)),
  'stored_identifiers',d.exact_identifiers,'identifiers_derived',d.exact_identifiers_derived,
  'vector_derived',d.fts_simple_derived,'vector_fingerprints',d.fts_simple_fingerprints,
  'occurrence',(SELECT to_jsonb(o) FROM occurrence o WHERE o.id={graph['occurrence']}),
  'artifact',(SELECT to_jsonb(a) FROM artifact_version a WHERE a.id={graph['artifact']}),
  'blocks',(SELECT jsonb_agg(to_jsonb(b) ORDER BY block_order) FROM storage_v2_complete_document_posting_blocks(d.id) b),
  'pairs',(SELECT jsonb_agg(jsonb_build_array(term,frequency) ORDER BY ordinal) FROM pairs),
  'seal',EXISTS(SELECT 1 FROM storage_v2_document_postings_seal s WHERE s.document_id=d.id
   AND s.materialization_sha256=d.materialization_sha256)),
 'compact_blocks',(SELECT count(*) FROM storage_v2_compact_posting_block WHERE document_id=d.id),
 'byte_blocks',(SELECT count(*) FROM storage_v2_byte_posting_block WHERE document_id=d.id),
 'model',jsonb_build_object('compact',m.compact,'byte',m.bytes,'blocks',m.blocks,
  'long',(SELECT count(*) FROM pairs WHERE byte_length>16),
  'prefixes',(SELECT count(DISTINCT substring(convert_to(term,'UTF8') FROM 1 FOR 32)) FROM pairs WHERE byte_length>16),
  'repetitive',coalesce((SELECT bool_or(length(regexp_replace(left(term,128)||right(term,128),
   '(.)\\1{{3,}}','\\1','g'))*2<length(left(term,128)||right(term,128))) FROM pairs WHERE byte_length>16),FALSE)),
 'ranks',(SELECT jsonb_object_agg(label,result) FROM (VALUES {','.join(ranks)}) ranked(label,result)))
FROM storage_v2_search_document d CROSS JOIN model m WHERE d.node_id={graph['node']};
{'COMMIT' if candidate else 'ROLLBACK'};"""
        return json.loads(self.sql(statement))

    def classifier_sentinel(self, cheap, admitted):
        prefix = "convert_to(term,'UTF8')"
        run = "regexp_replace(left(term,128)||right(term,128),\n                    '(.)\\1{3,}', '\\1', 'g')"
        # Only the expensive query has these exact expressions in173. This
        # test DDL, event table, new documents and helpers all roll back.
        self.assertEqual(self.new_producer.count(prefix), 1)
        self.assertEqual(self.new_producer.count(run), 1)
        instrumented = self.new_producer.replace(prefix, 'fixture173_prefix(term)').replace(
            run, 'fixture173_run(left(term,128)||right(term,128))')
        calls = []
        for graph, text in (cheap, admitted):
            calls.append(f"SELECT id FROM storage_v2_put_search_document('mainrag.lexical-simple.v1','node',"
                         f"{graph['node']},{self.quote(text)},{self.identifiers_sql(text)})")
        statement = f"""BEGIN;SET LOCAL app.user_id='{self.schema.ADMIN_ID}';SET LOCAL statement_timeout='30s';
CREATE TABLE fixture173_classifier_events(kind TEXT NOT NULL);
GRANT INSERT ON fixture173_classifier_events TO mainrag;
CREATE FUNCTION fixture173_prefix(term TEXT) RETURNS BYTEA LANGUAGE plpgsql VOLATILE AS $prefix$
BEGIN INSERT INTO fixture173_classifier_events VALUES('prefix');RETURN convert_to(term,'UTF8');END $prefix$;
CREATE FUNCTION fixture173_run(sample TEXT) RETURNS TEXT LANGUAGE plpgsql VOLATILE AS $run$
BEGIN INSERT INTO fixture173_classifier_events VALUES('run');
 RETURN regexp_replace(sample,'(.)\\1{{3,}}','\\1','g');END $run$;
{instrumented};
DO $prove$
BEGIN
 PERFORM fixture173_prefix('armed');PERFORM fixture173_run('aaaa');
 IF (SELECT count(*) FROM fixture173_classifier_events)<>2 THEN RAISE EXCEPTION 'sentinel not armed';END IF;
 TRUNCATE fixture173_classifier_events;
 PERFORM id FROM ({calls[0]}) created;
 IF EXISTS(SELECT 1 FROM fixture173_classifier_events) THEN RAISE EXCEPTION 'cheap rejection invoked classifier';END IF;
 PERFORM id FROM ({calls[1]}) created;
 IF NOT EXISTS(SELECT 1 FROM fixture173_classifier_events WHERE kind='prefix')
 OR NOT EXISTS(SELECT 1 FROM fixture173_classifier_events WHERE kind='run') THEN
  RAISE EXCEPTION 'BYTE candidate did not invoke both actual classifier sentinels';
 END IF;
END $prove$;
SELECT jsonb_build_object('prefix_calls',count(*) FILTER(WHERE kind='prefix'),
                         'run_calls',count(*) FILTER(WHERE kind='run')) FROM fixture173_classifier_events;
ROLLBACK;"""
        return json.loads(self.sql(statement))

    def test_exact_choices_logical_readers_authority_and_observable_lazy_classifier(self):
        self.assertEqual(self.producer_authority_before, self.producer_authority())
        self.assertEqual({s for s,h in self.function_hashes().items() if self.functions_before173.get(s)!=h}, {PRODUCER})
        before = self.contracts_before173;after = self.contracts_after173
        for key in ('posting_relations', 'lexical_relations'):
            self.assertEqual(before[key],after[key])
        for key in ('posting_functions', 'lexical_functions'):
            old = {r['signature']:r for r in before[key]};new = {r['signature']:r for r in after[key]}
            self.assertEqual(old.keys(),new.keys())
            for signature in old:
                if signature != PRODUCER:
                    self.assertEqual(old[signature],new[signature])
                else:
                    self.assertEqual({k:v for k,v in old[signature].items() if k!='definition_sha256'},
                                     {k:v for k,v in new[signature].items() if k!='definition_sha256'})
        header = 'alpha beta gamma 日本語 key_173 co-op a.b\n'
        words = [hashlib.sha256(str(n).encode()).hexdigest() for n in range(128)]
        fixtures = (
            ('cheap',header+'ordinarylongwordabcdefghi',False),
            ('prefix',header+' '.join('commonprefixabcdefghijklmnoqrstuvwxyz'+w*3 for w in words),False),
            ('repeat',header+' '.join(w[:32]+'a'*192+w[32:] for w in words),False),
            ('admit',header+' '.join(w+hashlib.sha256(('middle'+w).encode()).hexdigest()+
                                      hashlib.sha256(('tail'+w).encode()).hexdigest() for w in words),True),
        )
        for label,text,byte in fixtures:
            with self.subTest(label=label):
                self.assertLess(len(text.encode()),65536)
                graph = self.seed(text)
                original = self.construct173(graph,text,candidate=False)
                self.assertEqual(self.sql(f"SELECT count(*) FROM storage_v2_search_document WHERE node_id={graph['node']}"),'0')
                candidate = self.construct173(graph,text,candidate=True)
                self.assertEqual(original['logical'],candidate['logical'])
                self.assertTrue(candidate['logical']['seal'])
                self.assertEqual(original['model'],candidate['model'])
                self.assertEqual(original['ranks'],candidate['ranks'])
                self.assertEqual(original['compact_blocks'],candidate['compact_blocks'])
                self.assertEqual(original['byte_blocks'],candidate['byte_blocks'])
                self.assertEqual(candidate['byte_blocks']>0,byte)
                self.assertEqual(candidate['compact_blocks']>0,not byte)
                model = candidate['model']
                cheap_pass = (model['long']>0 and model['byte']*2<=model['compact'] and
                              model['compact']-model['byte']>=max(4096,model['blocks']*256))
                self.assertEqual(cheap_pass,label!='cheap')
                if label=='prefix':self.assertLess(model['prefixes']*4,model['long']*3)
                if label=='repeat':
                    self.assertGreaterEqual(model['prefixes']*4,model['long']*3)
                    self.assertTrue(model['repetitive'])
                if label=='admit':self.assertFalse(model['repetitive'])
                for reader in self.rank_readers:
                    self.assertTrue(json.loads(self.rank(graph['occurrence'],'alpha',reader=reader)))
                    self.assertEqual(self.rank(graph['occurrence'],'alpha',actor=self.schema.OTHER_ID,reader=reader),'[]')
                self.assertTrue(candidate['ranks']['storage_v2_source_segment_ranks_precise:"alpha beta"'])
                self.assertIn('key_173',self.envelope(graph['source'],{'type':'exact','value':'key_173'}))
                doc = candidate['id']
                self.assertEqual(int(self.sql(self.admin('SELECT id FROM storage_v2_put_search_document('
                    f"'mainrag.lexical-simple.v1','node',{graph['node']},{self.quote(text)},{self.identifiers_sql(text)})"))),doc)
                self.assert_sql_fails(self.actor(self.schema.OTHER_ID,
                    f"SELECT storage_v2_put_search_document('denied173','node',{graph['node']},'denied',ARRAY[]::TEXT[])"),
                    'administrator authority')
        # Fresh immutable components prevent existing-document reuse from skipping
        # the classifier positive control. These suffixes do not change its branch.
        cheap_text=fixtures[0][1]+' sentinel173cheap'
        admit_text=fixtures[-1][1]+' sentinel173admit'
        events=self.classifier_sentinel((self.seed(cheap_text),cheap_text),(self.seed(admit_text),admit_text))
        self.assertGreater(events['prefix_calls'],0);self.assertGreater(events['run_calls'],0)
        self.assertEqual(self.producer_authority_before,self.producer_authority())
        self.assertEqual(self.contracts_after173,json.loads(self.contracts()))
        self.assertEqual({s for s,h in self.function_hashes().items() if self.functions_before173.get(s)!=h}, {PRODUCER})
        self.sql(f"SET app.user_id='{self.schema.ADMIN_ID}';SELECT storage_v2_posting_conversion_require_operator();"
                 'SELECT storage_v2_require_lexical_conversion_operator();')


if __name__ == '__main__':
    unittest.main()
