"""Actual173/174 distinct-whitespace tokenizer and immutable posting payload gate.

One disposable cluster and one test method. Public deterministic fixtures cover
UTF8/locale/boundaries and both physical choices. Whole-document scanning is
capture-free; DISTINCT mixed tokens retain the exact captured local grammar.
Reference INSERTs roll back;
no reference result is obtained by reusing an already materialized document.
"""
from __future__ import annotations

import hashlib
import json
import unittest

from eval.storage_v2.schema import test_lazy_posting_cost_guards as previous

ROOT = previous.ROOT
MIGRATION = ROOT / 'migrations/174_storage_v2_capture_free_posting_tokenizer.sql'
PRODUCER = previous.PRODUCER
PREDECESSOR = '9f2ba80adc1283fb49bc6434e710cc048af34a4df62977a1f1eae617f916023b'
REFERENCE = 'fixture174_original_put_search_document'
WRITE_PROFILE = ("SET LOCAL work_mem='32MB';SET LOCAL maintenance_work_mem='256MB';"
                 "SET LOCAL gin_pending_list_limit='64MB';")


class CaptureFreePostingTokenizerTests(previous.LazyPostingCostGuardsTests):
    test_exact_choices_logical_readers_authority_and_observable_lazy_classifier = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls.original174 = cls.sql(f"SELECT pg_get_functiondef('{PRODUCER}'::REGPROCEDURE)")
            if cls.function_hashes()[PRODUCER] != PREDECESSOR:
                raise AssertionError('actual173 predecessor required')
            definition = cls.original174.replace('FUNCTION public.storage_v2_put_search_document(',
                                                 'FUNCTION public.' + REFERENCE + '(', 1)
            if definition == cls.original174:
                raise AssertionError('one frozen173 reference required')
            cls.sql(definition)
            cls.sql(f'ALTER FUNCTION {REFERENCE}(TEXT,TEXT,BIGINT,TEXT,TEXT[]) OWNER TO mainrag;'
                    f'REVOKE ALL ON FUNCTION {REFERENCE}(TEXT,TEXT,BIGINT,TEXT,TEXT[]) FROM PUBLIC;')
            cls.authority174_before = cls.producer_authority()
            cls.functions174_before = cls.function_hashes()
            cls.contracts174_before = json.loads(cls.contracts())
            cls.sql(f"SET app.user_id='{cls.schema.ADMIN_ID}';" + MIGRATION.read_text())
            cls.candidate174 = cls.sql(f"SELECT pg_get_functiondef('{PRODUCER}'::REGPROCEDURE)")
            cls.functions174_after = cls.function_hashes()
            cls.contracts174_after = json.loads(cls.contracts())
            for name, body in (('fixture174_old_tokens', cls.original174),
                               ('fixture174_new_tokens', cls.candidate174)):
                cls.freeze_tokenizer(name, body)
        except BaseException:
            cls.base.tearDownClass.__func__(cls)
            raise

    @classmethod
    def freeze_tokenizer(cls, name, definition):
        start = definition.index('    v_normalized_text := lower(p_search_text);')
        end = definition.index('      FROM frequency;', start) + len('      FROM frequency;')
        # Exact original/candidate SQL, not a Python/ASCII implementation or gold.
        statement = f"""CREATE FUNCTION {name}(p_search_text TEXT) RETURNS JSONB
LANGUAGE plpgsql SET search_path=pg_catalog,public AS $tokens$
DECLARE v_normalized_text TEXT;v_token_count BIGINT;v_terms TEXT[];
 v_frequencies BIGINT[];v_word_exact TEXT[];v_byte_starts INTEGER[];v_byte_lengths INTEGER[];
BEGIN
{definition[start:end]}
 RETURN jsonb_build_array(v_token_count,v_terms,v_frequencies,v_word_exact,v_byte_starts,v_byte_lengths);
END $tokens$;"""
        cls.sql(statement)
        cls.sql(f'ALTER FUNCTION {name}(TEXT) OWNER TO mainrag;REVOKE ALL ON FUNCTION {name}(TEXT) FROM PUBLIC;')

    def arrays(self, text):
        return json.loads(self.sql(f"SELECT jsonb_build_array(fixture174_old_tokens({self.quote(text)}),"
                                  f"fixture174_new_tokens({self.quote(text)}))"))

    def construct174(self, graph, text, *, candidate):
        producer = 'storage_v2_put_search_document' if candidate else REFERENCE
        # Same immutable graph and exact same arguments. Original document,
        # postings and timing table all roll back before the candidate INSERT.
        # Only surrogate document IDs and insertion audit timestamps differ;
        # every logical/physical semantic column is compared unchanged.
        statement = f"""BEGIN;{WRITE_PROFILE}SET LOCAL app.user_id='{self.schema.ADMIN_ID}';SET LOCAL statement_timeout='60s';
CREATE TEMP TABLE fixture174_result(document BIGINT,milliseconds DOUBLE PRECISION) ON COMMIT DROP;
DO $materialize$
DECLARE doc BIGINT;began TIMESTAMPTZ;identifiers TEXT[];
BEGIN
 identifiers:={self.identifiers_sql(text)};
 began:=clock_timestamp();
 SELECT id INTO STRICT doc FROM {producer}('mainrag.lexical-simple.v1','node',
  {graph['node']},{self.quote(text)},identifiers);
 INSERT INTO fixture174_result VALUES(doc,extract(epoch FROM clock_timestamp()-began)*1000);
END $materialize$;
SELECT jsonb_build_object('id',d.id,'milliseconds',timing.milliseconds,
 'document',to_jsonb(d)-ARRAY['id','created_at'],
 'identifiers',storage_v2_document_exact_identifiers(d.id),
 'logical_fts',encode(sha256(tsvectorsend(storage_v2_logical_document_fts(
     d.fts_simple,d.fts_simple_derived,d.search_text))),'hex'),
 'logical_blocks',(SELECT coalesce(jsonb_agg(to_jsonb(p)-'document_id' ORDER BY p.block_order),'[]'::JSONB)
     FROM storage_v2_complete_document_posting_blocks(d.id) p),
 'compact',(SELECT coalesce(jsonb_agg(to_jsonb(p)-'document_id' ORDER BY p.block_order),'[]'::JSONB)
     FROM storage_v2_compact_posting_block p WHERE p.document_id=d.id),
 'byte',(SELECT coalesce(jsonb_agg(to_jsonb(p)-'document_id' ORDER BY p.block_order),'[]'::JSONB)
     FROM storage_v2_byte_posting_block p WHERE p.document_id=d.id),
 'column_sizes',(SELECT coalesce(jsonb_agg(jsonb_build_array(p.block_order,
       pg_column_size(p.terms),pg_column_size(p.term_frequencies)) ORDER BY p.block_order),'[]'::JSONB)
     FROM storage_v2_compact_posting_block p WHERE p.document_id=d.id),
 'byte_column_sizes',(SELECT coalesce(jsonb_agg(jsonb_build_array(p.block_order,
       pg_column_size(p.cached_terms),pg_column_size(p.text_byte_starts),pg_column_size(p.text_byte_lengths),
       pg_column_size(p.term_frequencies),pg_column_size(p.fingerprints)) ORDER BY p.block_order),'[]'::JSONB)
     FROM storage_v2_byte_posting_block p WHERE p.document_id=d.id),
 'seal',(SELECT to_jsonb(s)-'document_id' FROM storage_v2_document_postings_seal s WHERE s.document_id=d.id))
FROM storage_v2_search_document d JOIN fixture174_result timing ON timing.document=d.id;
{'COMMIT' if candidate else 'ROLLBACK'};"""
        return json.loads(self.sql(statement))

    def measured_arrays(self, text):
        # Each complete tokenizer executes once; timings are diagnostics. CI
        # correctness does not assert a noisy universal performance threshold.
        statement = f"""BEGIN;{WRITE_PROFILE}DO $time$
DECLARE a JSONB;b JSONB;began TIMESTAMPTZ;old_ms DOUBLE PRECISION;new_ms DOUBLE PRECISION;
BEGIN
 began:=clock_timestamp();a:=fixture174_old_tokens({self.quote(text)});
 old_ms:=extract(epoch FROM clock_timestamp()-began)*1000;
 began:=clock_timestamp();b:=fixture174_new_tokens({self.quote(text)});
 new_ms:=extract(epoch FROM clock_timestamp()-began)*1000;
 IF a IS DISTINCT FROM b THEN RAISE EXCEPTION 'large native tokenizer parity differs';END IF;
 PERFORM set_config('fixture174.timings',jsonb_build_object('original_ms',old_ms,'candidate_ms',new_ms)::TEXT,false);
END $time$;SELECT current_setting('fixture174.timings')::JSONB || jsonb_build_object(
 'work_mem_bytes',pg_size_bytes(current_setting('work_mem')),
 'maintenance_work_mem_bytes',pg_size_bytes(current_setting('maintenance_work_mem')),
 'gin_pending_list_limit_bytes',pg_size_bytes(current_setting('gin_pending_list_limit')));ROLLBACK;"""
        return json.loads(self.sql(statement))

    def test_exact_token_arrays_postings_replay_authority_and_one_large_phase(self):
        self.assertEqual(self.authority174_before, self.producer_authority())
        self.assertEqual({s for s,h in self.functions174_after.items()
                          if self.functions174_before.get(s) != h}, {PRODUCER})
        self.assertEqual(self.functions174_before.keys(), self.functions174_after.keys())
        for key in ('posting_relations', 'lexical_relations'):
            self.assertEqual(self.contracts174_before[key], self.contracts174_after[key])
        for key in ('posting_functions', 'lexical_functions'):
            old = {r['signature']:r for r in self.contracts174_before[key]}
            new = {r['signature']:r for r in self.contracts174_after[key]}
            self.assertEqual(old.keys(), new.keys())
            for signature in old:
                if signature != PRODUCER:
                    self.assertEqual(old[signature], new[signature])
                else:
                    self.assertEqual({k:v for k,v in old[signature].items() if k != 'definition_sha256'},
                                     {k:v for k,v in new[signature].items() if k != 'definition_sha256'})
        # POSIX class membership and lower() are evaluated by this same actual
        # PostgreSQL/default locale, never approximated with Python predicates.
        words = [hashlib.sha256(str(n).encode()).hexdigest() for n in range(128)]
        header = 'alpha beta gamma key_174 co-op a.b 日本語\n'
        fixtures = [
            ('empty', ''), ('spaces', ' \t\r\n\v\f\u2003\u00a0'),
            ('punctuation', '!@#$%^&*()---...'),
            ('multiplicity', 'ALPHA alpha\talpha a-b a-b beta.beta _9 x9 a-b-a\n\ta-b-a'),
            ('unicode', 'İ I ı ß ẞ Ää Σσς Кириллица 中文 日本語 e\u0301 a\u200db a\u2028b a\u2003b'),
            ('line_ends', ' a-b\r\n c.d\t\v\f x/y\n\n z_7 '),
            ('positions', '..alpha alpha.. alpha\u2003alpha x_y ..x_y! x_y'),
            ('cache_edges', 'a'*16+' '+ 'b'*17+' '+ 'é'*8+' '+ 'é'*9+' '+'c'*8193),
            ('compact', header+' '.join('commonprefixabcdefghijklmnoqrstuvwxyz'+w*3 for w in words)),
            ('byte', header+' '.join(w+hashlib.sha256(('middle'+w).encode()).hexdigest()+
                                    hashlib.sha256(('tail'+w).encode()).hexdigest() for w in words)),
        ]
        for count in (255,256,257):
            fixtures.append((f'block_{count}',' '.join(f'word{n:04d}' for n in range(count))))
        unit = 'alpha beta a-b key_174 x/y alpha\n'
        large = (unit*((1048576+len(unit)-1)//len(unit)))[:1048576]
        self.assertEqual(len(large.encode()),1048576)
        timing = self.measured_arrays(large)
        self.assertEqual(timing['work_mem_bytes'],33554432)
        self.assertEqual(timing['maintenance_work_mem_bytes'],268435456)
        self.assertEqual(timing['gin_pending_list_limit_bytes'],67108864)
        fixtures.append(('one_mib',large))
        for label,text in fixtures:
            with self.subTest(label=label):
                if label != 'one_mib':
                    original_arrays,candidate_arrays = self.arrays(text)
                    self.assertEqual(original_arrays,candidate_arrays)
                # Empty search text can be materialized against a valid fixture
                # component; both implementations get identical arguments.
                graph = self.seed(text if text else 'fixture174 empty search component')
                before = self.construct174(graph,text,candidate=False)
                self.assertEqual(self.sql(f"SELECT count(*) FROM storage_v2_search_document WHERE node_id={graph['node']}"),'0')
                after = self.construct174(graph,text,candidate=True)
                self.assertNotEqual(before['id'],after['id'])
                for key in ('document','identifiers','logical_fts','logical_blocks','compact','byte','column_sizes','byte_column_sizes','seal'):
                    self.assertEqual(before[key],after[key],key)
                self.assertIsNotNone(after['seal'])
                if label == 'compact':
                    self.assertTrue(after['compact']);self.assertFalse(after['byte'])
                if label == 'byte':
                    self.assertTrue(after['byte']);self.assertFalse(after['compact'])
                replay = self.admin('SELECT id FROM storage_v2_put_search_document('
                    f"'mainrag.lexical-simple.v1','node',{graph['node']},{self.quote(text)},{self.identifiers_sql(text)})")
                self.assertEqual(int(self.sql(replay)),after['id'])
                self.assert_sql_fails(self.admin('SELECT id FROM storage_v2_put_search_document('
                    f"'mainrag.lexical-simple.v1','node',{graph['node']},{self.quote(text+' collision')},{self.identifiers_sql(text)})"),
                    'profile collision')
                self.assert_sql_fails(self.actor(self.schema.OTHER_ID,
                    f"SELECT storage_v2_put_search_document('denied174','node',{graph['node']},'denied',ARRAY[]::TEXT[])"),
                    'administrator authority')
                if label == 'one_mib':
                    timing.update(original_producer_ms=before['milliseconds'],candidate_producer_ms=after['milliseconds'])
        self.assertEqual(self.authority174_before,self.producer_authority())
        self.assertEqual(self.contracts174_after,json.loads(self.contracts()))
        self.sql(f"SET app.user_id='{self.schema.ADMIN_ID}';SELECT storage_v2_posting_conversion_require_operator();"
                 'SELECT storage_v2_require_lexical_conversion_operator();')
        print('\nCAPTURE_FREE_TOKENIZER_NATIVE_PARITY '+json.dumps(dict(
            cases=len(fixtures),bytes=1048576,tokenizer_arrays_exact=True,physical_postings_exact=True,
            authority_unchanged=True,reader_functions_unchanged=True,producer_semantics_unchanged=True,
            migration_sha256=hashlib.sha256(MIGRATION.read_bytes()).hexdigest(),
            predecessor_function_sha256=PREDECESSOR,resulting_function_sha256=self.functions174_after[PRODUCER],
            timing=timing),sort_keys=True),flush=True)


if __name__ == '__main__':
    unittest.main()
