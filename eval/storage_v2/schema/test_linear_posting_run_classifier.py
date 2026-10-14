"""One native175/176 run-classifier differential and realistic complete write.

Reuses the isolated175 schema harness, not production data. Original/candidate
arrays, storage choice, immutable rows and authority are checked in one cohort.
Timings describe this public fixture only and do not establish a source ETA.
"""
from __future__ import annotations

import hashlib
import json
import unittest

from eval.storage_v2.schema import test_bounded_posting_token_keys as previous

ROOT = previous.ROOT
MIGRATION = ROOT / 'migrations/176_storage_v2_linear_posting_run_classifier.sql'
PRODUCER = previous.PRODUCER
PREDECESSOR = 'e64938762b3e2bb180c46abc906ca2e2e783352f991149e3bab2f405ec4d3d8a'
REFERENCE = previous.previous.REFERENCE


class LinearPostingRunClassifierTests(previous.BoundedPostingTokenKeysTests):
    test_exact174_arrays_postings_bounded_keys_context_and_authority = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls.original176 = cls.candidate175
            if cls.function_hashes()[PRODUCER] != PREDECESSOR:
                raise AssertionError('actual175 producer predecessor required')
            reference = cls.original176.replace('FUNCTION public.storage_v2_put_search_document(',
                                                 'FUNCTION public.' + REFERENCE + '(', 1)
            if reference == cls.original176:
                raise AssertionError('one exact175 producer reference required')
            cls.sql(reference)
            cls.sql(f'ALTER FUNCTION {REFERENCE}(TEXT,TEXT,BIGINT,TEXT,TEXT[]) OWNER TO mainrag;'
                    f'REVOKE ALL ON FUNCTION {REFERENCE}(TEXT,TEXT,BIGINT,TEXT,TEXT[]) FROM PUBLIC;')
            cls.authority_before176 = cls.authority_catalog()
            cls.functions_before176 = cls.function_hashes()
            cls.contracts_before176 = json.loads(cls.contracts())
            cls.sql(f"SET app.user_id='{cls.schema.ADMIN_ID}';" + MIGRATION.read_text())
            cls.candidate176 = cls.sql(f"SELECT pg_get_functiondef('{PRODUCER}'::REGPROCEDURE)")
            cls.functions_after176 = cls.function_hashes()
            cls.contracts_after176 = json.loads(cls.contracts())
            for name, definition in (('fixture174_old_tokens', cls.original176),
                                      ('fixture174_new_tokens', cls.candidate176)):
                cls.freeze_tokenizer(name, definition)
        except BaseException:
            cls.base.tearDownClass.__func__(cls)
            raise

    @staticmethod
    def classifier(definition):
        start = definition.index("        SELECT count(DISTINCT substring(convert_to(term,'UTF8') FROM 1 FOR 32))")
        if 'FOREACH v_run_character' in definition:
            marker = '\n        END LOOP;'
        else:
            marker = '\n         WHERE byte_length>16;'
        end = definition.index(marker, start) + len(marker)
        return definition[start:end]

    def sample_differential(self, terms):
        old = self.classifier(self.original176)
        new = self.classifier(self.candidate176)
        # Execute the two ACTUAL producer fragments. Artificial byte-length17
        # enables the isolated classifier on short boundary terms; it is never
        # an inserted document or a claim about source term lengths.
        statement = f"""BEGIN;SET LOCAL statement_timeout='60s';
DO $samples$
DECLARE v_terms TEXT[];v_byte_lengths INTEGER[];v_long_prefix_count BIGINT;
 v_repetitive_dictionary BOOLEAN;v_run_sample TEXT;v_run_character TEXT;v_run_previous TEXT;
 v_run_length INTEGER;v_run_reduced_length INTEGER;v_run_sample_length INTEGER;
 input_term TEXT;original_choice BOOLEAN;candidate_choice BOOLEAN;original_prefix BIGINT;cases INTEGER:=0;
BEGIN
 FOREACH input_term IN ARRAY ARRAY[{','.join(self.quote(v) for v in terms)}]::TEXT[] LOOP
  v_terms:=ARRAY[input_term];v_byte_lengths:=ARRAY[17];
{old}
  original_choice:=v_repetitive_dictionary;original_prefix:=v_long_prefix_count;
{new}
  candidate_choice:=v_repetitive_dictionary;
  IF original_choice IS DISTINCT FROM candidate_choice OR original_prefix IS DISTINCT FROM v_long_prefix_count THEN
   RAISE EXCEPTION 'exact175/176 bounded codepoint run classifier differs';END IF;
  cases:=cases+1;
 END LOOP;
 -- Empty/all-short dictionary retains the original coalesce(false) result.
 v_terms:=ARRAY[]::TEXT[];v_byte_lengths:=ARRAY[]::INTEGER[];
{old}
 original_choice:=v_repetitive_dictionary;
{new}
 IF original_choice IS DISTINCT FROM v_repetitive_dictionary THEN RAISE EXCEPTION 'empty run classifier differs';END IF;
 PERFORM set_config('fixture176.sample_cases',cases::TEXT,false);
END $samples$;
SELECT current_setting('fixture176.sample_cases');ROLLBACK;"""
        return int(self.sql(statement))

    def test_native_run_predicate_and_one_realistic_byte_write(self):
        self.assertEqual(self.authority_before176, self.authority_catalog())
        self.assertEqual(set(self.functions_before176), set(self.functions_after176))
        self.assertEqual({PRODUCER}, {s for s in self.functions_before176
                                     if self.functions_before176[s] != self.functions_after176[s]})
        # The tokenizer must remain byte-identical; no new captured grammar,
        # profile, frequency, offsets or identifier construction appears.
        def tokenizer(body):
            start = body.index('    v_normalized_text := lower(p_search_text);')
            end = body.index('      FROM frequency;', start) + len('      FROM frequency;')
            return body[start:end]
        self.assertEqual(tokenizer(self.original176), tokenizer(self.candidate176))
        for name in ('posting_relations', 'lexical_relations'):
            self.assertEqual(self.contracts_before176[name], self.contracts_after176[name])
        for name in ('posting_functions', 'lexical_functions'):
            before = {r['signature']: r for r in self.contracts_before176[name]}
            after = {r['signature']: r for r in self.contracts_after176[name]}
            self.assertEqual(set(before), set(after))
            for signature, row in before.items():
                expected = dict(row)
                if signature == PRODUCER:
                    expected['definition_sha256'] = '\\x' + self.functions_after176[PRODUCER]
                self.assertEqual(expected, after[signature])
        samples = ['', 'a', 'aaa', 'aaaa', 'aaaaa', 'aaabbbbcc', 'AAAaaaAAAA',
                   'éééé', '😀😀😀😀', 'e\u0301'*20, 'éÉéÉ'*32, 'Σσς'*42,
                   '\n'*4, '\r\n'*16, '\\'*20, "''''", '\u2028'*5,
                   'a'*3+'b'*4+'c'*3, 'a'*128+'b'*128, 'a'*127+'b'+'a'*128]
        # Run3/4 boundary, merged left/right sample boundary, all native UTF8
        # codepoint widths and lengths on either side of128/256.
        for char in ('a', 'é', '中', '😀', '\n'):
            samples.extend(char*n+'x' for n in (3,4,5,16,63,64,127,128,129,255,256,257))
        for n in range(48):
            samples.append(''.join(char*((n//(4**i))%4+1) for i,char in enumerate(('a','é','😀','\n'))))
        sample_count = self.sample_differential(samples)
        self.assertEqual(sample_count, len(samples))

        words = ['opaque_' + ''.join(hashlib.sha256((part+str(n)).encode()).hexdigest()
                                    for part in ('first','middle','tail')) for n in range(5243)]
        text = ' '.join(words)[:1048576]
        self.assertEqual(len(text.encode()),1048576)
        # Unlike the repetitive6-term one-MiB fixture, this has thousands of
        # distinct >16-byte words, distinct prefixes and no large equal-char
        # runs. Both complete native writes must actually select BYTE.
        graph = self.seed(text)
        self.assertEqual(*self.arrays(text))
        before = self.construct174(graph,text,candidate=False)
        self.assertEqual(self.sql(f"SELECT count(*) FROM storage_v2_search_document WHERE node_id={graph['node']}"),'0')
        after = self.construct174(graph,text,candidate=True)
        for key in ('document','identifiers','logical_fts','logical_blocks','compact','byte',
                    'column_sizes','byte_column_sizes','seal'):
            self.assertEqual(before[key],after[key],key)
        self.assertTrue(after['byte']);self.assertFalse(after['compact'])
        replay = self.sql(self.admin('SELECT id FROM storage_v2_put_search_document('
            f"'mainrag.lexical-simple.v1','node',{graph['node']},{self.quote(text)},{self.identifiers_sql(text)})"))
        self.assertEqual(int(replay),after['id'])
        self.assert_sql_fails(self.actor(self.schema.OTHER_ID,
            f"SELECT storage_v2_put_search_document('denied176','node',{graph['node']},'denied',ARRAY[]::TEXT[])"),
            'administrator authority')
        self.assertEqual(self.authority_before176,self.authority_catalog())
        self.assertEqual(self.contracts_after176,json.loads(self.contracts()))
        self.sql(f"SET app.user_id='{self.schema.ADMIN_ID}';SELECT storage_v2_posting_conversion_require_operator();"
                 'SELECT storage_v2_require_lexical_conversion_operator();')
        print('LINEAR_POSTING_RUN_CLASSIFIER_NATIVE_PARITY '+json.dumps(dict(
            cases=1,run_sample_cases=sample_count,run_sample_parity=True,bytes=1048576,
            selected_large_case='public_deterministic_one_mib_opaque_long_identifiers',
            tokenizer_arrays_exact=True,physical_postings_exact=True,posting_choice_exact=True,
            authority_unchanged=True,reader_functions_unchanged=True,producer_semantics_unchanged=True,
            public_function_signature_set_unchanged=True,
            migration_sha256=hashlib.sha256(MIGRATION.read_bytes()).hexdigest(),
            predecessor_function_sha256=PREDECESSOR,resulting_function_sha256=self.functions_after176[PRODUCER],
            timing={'original_producer_ms':before['milliseconds'],'candidate_producer_ms':after['milliseconds']},
            performance_sla_claimed=False),sort_keys=True),flush=True)


if __name__ == '__main__':
    unittest.main()
