"""Exact174/175 token keys, immutable postings and bounded context PK gate.

One disposable cluster and one combined method. Runtime diagnostics have no SLA
threshold; static and JSON plans prove the changed internal Window key.
"""
from __future__ import annotations

import hashlib
import json
import re
import unittest

from eval.storage_v2.schema import test_capture_free_posting_tokenizer as previous

ROOT = previous.ROOT
MIGRATION = ROOT / 'migrations/175_storage_v2_bounded_token_keys_and_context_lookup.sql'
PRODUCER = previous.PRODUCER
PREDECESSOR = '9d4f6a28b1e681df79954e2e88a677fe30f546b1c8654f77fef09a943b0543d5'
BEGIN = 'storage_v2_begin_lexical_document_context(bigint,bigint,bigint,integer)'
REQUIRE = 'storage_v2_require_lexical_document_context(bigint,bigint)'
CHANGED = {PRODUCER, BEGIN, REQUIRE}


class BoundedPostingTokenKeysTests(previous.CaptureFreePostingTokenizerTests):
    test_exact_token_arrays_postings_replay_authority_and_one_large_phase = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls.original175 = cls.sql(f"SELECT pg_get_functiondef('{PRODUCER}'::REGPROCEDURE)")
            if cls.function_hashes()[PRODUCER] != PREDECESSOR:
                raise AssertionError('actual174 producer predecessor required')
            cls.contexts175 = {sig: cls.sql(f"SELECT pg_get_functiondef('{sig}'::REGPROCEDURE)")
                               for sig in (BEGIN, REQUIRE)}
            # Reuse the174 observation/constructor methods against a frozen174
            # producer reference, not the older173 implementation.
            reference = cls.original175.replace('FUNCTION public.storage_v2_put_search_document(',
                                                 'FUNCTION public.' + previous.REFERENCE + '(', 1)
            if reference == cls.original175:
                raise AssertionError('exact174 reference boundary required')
            cls.sql(reference)
            cls.sql(f'ALTER FUNCTION {previous.REFERENCE}(TEXT,TEXT,BIGINT,TEXT,TEXT[]) OWNER TO mainrag;'
                    f'REVOKE ALL ON FUNCTION {previous.REFERENCE}(TEXT,TEXT,BIGINT,TEXT,TEXT[]) FROM PUBLIC;')
            cls.authorities175 = cls.authority_catalog()
            cls.before175 = cls.function_hashes()
            cls.contracts_before175 = json.loads(cls.contracts())
            cls.sql(f"SET app.user_id='{cls.schema.ADMIN_ID}';" + MIGRATION.read_text())
            cls.candidate175 = cls.sql(f"SELECT pg_get_functiondef('{PRODUCER}'::REGPROCEDURE)")
            cls.after175 = cls.function_hashes()
            cls.contracts_after175 = json.loads(cls.contracts())
            for name, body in (('fixture174_old_tokens', cls.original175),
                               ('fixture174_new_tokens', cls.candidate175)):
                cls.freeze_tokenizer(name, body)
            for sig, old in cls.contexts175.items():
                name = sig.split('(')[0]
                copy = old.replace('FUNCTION public.' + name + '(',
                                   'FUNCTION public.fixture175_old_' + name + '(', 1)
                if copy == old:
                    raise AssertionError('exact context reference boundary required')
                cls.sql(copy)
                arguments = sig.split('(', 1)[1]
                cls.sql(f'ALTER FUNCTION fixture175_old_{name}({arguments} OWNER TO mainrag_v2_frontier_owner;'
                        f'REVOKE ALL ON FUNCTION fixture175_old_{name}({arguments} FROM PUBLIC;')
                if sig == BEGIN:
                    cls.sql(f'GRANT EXECUTE ON FUNCTION fixture175_old_{name}({arguments} TO mainrag;')
        except BaseException:
            cls.base.tearDownClass.__func__(cls)
            raise

    @classmethod
    def freeze_tokenizer(cls, name, definition):
        start = definition.index('    v_normalized_text := lower(p_search_text);')
        end = definition.index('      FROM frequency;', start) + len('      FROM frequency;')
        # Exact original/candidate SQL, not a Python/ASCII implementation or gold.
        statement = f"""CREATE OR REPLACE FUNCTION {name}(p_search_text TEXT) RETURNS JSONB
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
        if text is None:
            return json.loads(self.sql('SELECT jsonb_build_array(fixture174_old_tokens(NULL),fixture174_new_tokens(NULL))'))
        return super().arrays(text)

    @classmethod
    def authority_catalog(cls):
        return json.loads(cls.sql("SELECT jsonb_object_agg(p.oid::REGPROCEDURE::TEXT,to_jsonb(p)-'prosrc') "
                                 "FROM pg_proc p WHERE pronamespace='public'::REGNAMESPACE AND prokind='f'"))

    @staticmethod
    def plan_keys(plan):
        keys = []
        if isinstance(plan, dict):
            keys.extend(plan.get('Sort Key', []))
            keys.extend(plan.get('Presorted Key', []))
            for value in plan.values():
                keys.extend(BoundedPostingTokenKeysTests.plan_keys(value))
        elif isinstance(plan, list):
            for value in plan:
                keys.extend(BoundedPostingTokenKeysTests.plan_keys(value))
        return keys

    def token_plan(self, definition, text):
        start = definition.index('    WITH raw_runs AS MATERIALIZED (')
        end = definition.index('      FROM frequency;', start) + len('      FROM frequency;')
        query = definition[start:end].replace('v_normalized_text', 'lower(' + self.quote(text) + ')')
        query, count = re.subn(r'\s+INTO v_token_count,v_terms,v_frequencies,v_word_exact,v_byte_starts,v_byte_lengths',
                              '', query, count=1)
        self.assertEqual(count, 1)
        return json.loads(self.sql("BEGIN;SET LOCAL work_mem='32MB';EXPLAIN(FORMAT JSON,VERBOSE,COSTS) "
                                   + query + 'ROLLBACK;'))

    def context_query(self, definition, graph):
        start = definition.index(' SELECT run.id AS run_id')
        end = definition.index(';', start)
        query = definition[start:end].replace('   INTO identity FROM', '   FROM')
        for name, value in (('p_run_id', graph['run']), ('p_occurrence_id', graph['occurrence']),
                            ('p_artifact_version_id', graph['artifact'])):
            query = query.replace(name, str(value))
        return query

    def context_parity(self, graph, text):
        # Exact same bound graph; reference scratch and all mutations roll back.
        snapshots = []
        for old in (True, False):
            begin = ('fixture175_old_' if old else '') + 'storage_v2_begin_lexical_document_context'
            required = ('fixture175_old_' if old else '') + 'storage_v2_require_lexical_document_context'
            call = f'{begin}({graph["run"]},{graph["occurrence"]},{graph["artifact"]},1)'
            snapshots.append(json.loads(self.sql(self.admin('BEGIN;SELECT ' + call + ';'
                # The require helper is owner-private in both versions.
                # Begin remains called through the ordinary worker surface.
                f'RESET ROLE;SELECT {required}({graph["occurrence"]},{graph["artifact"]});'
                # Inspect owner-private scratch as the fixture administrator
                # after both context functions ran as the ordinary caller.
                "RESET ROLE;SELECT to_jsonb(c)-ARRAY['transaction_id','backend_id'] FROM pg_temp.mainrag_v2_lexical_context c;ROLLBACK;")).splitlines()[-1]))
            self.assert_sql_fails(self.admin('BEGIN;SELECT ' + call + ';'
                f'RESET ROLE;SELECT {required}({graph["occurrence"]},{graph["artifact"]+100000});ROLLBACK;'),
                'bound lexical document context')
            self.assert_sql_fails(self.actor(self.schema.OTHER_ID, 'BEGIN;SELECT ' + call + ';ROLLBACK;'),
                                  'authorized bound lexical document')
        self.assertEqual(*snapshots)
        payloads = []
        for old in (True, False):
            begin = ('fixture175_old_' if old else '') + 'storage_v2_begin_lexical_document_context'
            script = self.admin('BEGIN;SELECT '+begin+
                f'({graph["run"]},{graph["occurrence"]},{graph["artifact"]},1);'
                'SELECT storage_v2_stage_lexical_document_context('
                f'{graph["occurrence"]},{graph["artifact"]},ARRAY[0]::BIGINT[],ARRAY[{self.quote(text)}],'
                "ARRAY['heading'],ARRAY['text'],ARRAY[1]::BIGINT[],ARRAY[1]::BIGINT[]);"
                f'SELECT storage_v2_finish_lexical_document_context({graph["occurrence"]},{graph["artifact"]});'
                "SELECT coalesce(jsonb_agg(to_jsonb(l)-'created_at' ORDER BY segment_order),'[]'::JSONB) "
                f'FROM storage_v2_lexical_segment_all l WHERE occurrence_id={graph["occurrence"]};ROLLBACK;')
            payloads.append(json.loads(self.sql(script).splitlines()[-1]))
        self.assertEqual(*payloads)
        self.assertTrue(payloads[0])
        original = self.context_query(self.contexts175[BEGIN], graph)
        current_def = self.sql(f"SELECT pg_get_functiondef('{BEGIN}'::REGPROCEDURE)")
        current = self.context_query(current_def, graph)
        for query in (original, current):
            plan = json.loads(self.sql(self.admin("BEGIN;SET LOCAL enable_seqscan=off;SET LOCAL enable_hashjoin=off;SET LOCAL enable_mergejoin=off;"
                "EXPLAIN(FORMAT JSON,VERBOSE,COSTS) " + query + ';ROLLBACK;')))
            self.assertTrue(plan)
            if query == current:
                self.assertRegex(json.dumps(plan), r'Index Cond[^\n]*source_item_id')
        self.assertIn('item.source_item_id=artifact.item_id', current)
        print('BOUNDED_CONTEXT_PK_QUERY_PROOF ' + json.dumps(dict(
            original_predicate_present='item.source_item_id=artifact.item_id' in original,
            candidate_predicate_present=True, runtime_hotspot_claimed=False)), flush=True)

    def test_exact174_arrays_postings_bounded_keys_context_and_authority(self):
        self.assertEqual(self.before175.keys(), self.after175.keys())
        self.assertEqual({s for s,h in self.before175.items() if self.after175[s] != h}, CHANGED)
        authority_after = self.authority_catalog()
        for sig in self.authorities175:
            self.assertEqual(self.authorities175[sig], authority_after[sig], sig)
        for sig, old in self.contexts175.items():
            actual = self.sql(f"SELECT pg_get_functiondef('{sig}'::REGPROCEDURE)")
            marker = 'JOIN public.artifact_version artifact ON artifact.id=occurrence_row.artifact_version_id'
            self.assertEqual(old.count(marker), 1)
            self.assertEqual(actual, old.replace(marker, marker+' AND item.source_item_id=artifact.item_id'))
        for key in ('posting_relations', 'lexical_relations'):
            self.assertEqual(self.contracts_before175[key], self.contracts_after175[key])
        for key in ('posting_functions', 'lexical_functions'):
            old = {r['signature']:r for r in self.contracts_before175[key]}
            new = {r['signature']:r for r in self.contracts_after175[key]}
            self.assertEqual(old.keys(), new.keys())
            for sig in old:
                self.assertEqual({k:v for k,v in old[sig].items() if k!='definition_sha256'},
                                 {k:v for k,v in new[sig].items() if k!='definition_sha256'})
                if sig not in CHANGED:
                    self.assertEqual(old[sig], new[sig])
                else:
                    self.assertEqual(new[sig]['definition_sha256'], '\\x'+self.after175[sig])
        # Retain all fourteen174 cases unchanged, including the1MiB corpus.
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
        fixtures.append(('one_mib',large))

        minified_prefix = '{'+','.join(f'"k_{n:05d}":{n%10}' for n in range(4096))+',"pad":"'
        minified = minified_prefix + 'z'*(65536-len(minified_prefix)-2) + '"}'
        self.assertEqual(len(minified.encode()),65536)
        self.assertFalse(any(c.isspace() for c in minified))
        fixtures.append(('minified_64k', minified))
        timings = {}
        self.assertEqual(*self.arrays(None))
        for label,text in fixtures:
            with self.subTest(label=label):
                if label=='minified_64k':
                    temp_query = "SELECT jsonb_build_object('bytes',temp_bytes,'files',temp_files) FROM pg_stat_database WHERE datname=current_database()"
                    temp_before = json.loads(self.sql(temp_query))
                    timings = self.measured_arrays(text)
                    temp_after = json.loads(self.sql(temp_query))
                    timings['fixture_database_temp_observation'] = dict(before=temp_before,after=temp_after,
                        combined_old_and_new=True,asynchronous_statistics=True,production_claimed=False)
                else:
                    self.assertEqual(*self.arrays(text))
                graph = self.seed(text if text else 'fixture175 empty search component')
                before = self.construct174(graph,text,candidate=False)
                after = self.construct174(graph,text,candidate=True)
                for key in ('document','identifiers','logical_fts','logical_blocks','compact','byte','column_sizes','byte_column_sizes','seal'):
                    self.assertEqual(before[key],after[key],key)
                if label=='unicode':
                    self.sql(self.admin(f'SELECT storage_v2_bind_search_document({graph["view"]},0,{after["id"]},1.0)'))
                    self.context_parity(graph,text)
                if label=='minified_64k':
                    old_keys = self.plan_keys(self.token_plan(self.original175,text))
                    new_keys = self.plan_keys(self.token_plan(self.candidate175,text))
                    self.assertTrue(any('mixed.token' in k for k in old_keys), old_keys)
                    self.assertFalse(any('mixed.token' in k for k in new_keys), new_keys)
                    self.assertTrue(any('mixed.mixed_id' in k for k in new_keys), new_keys)
                    print('BOUNDED_TOKEN_WINDOW_PLAN '+json.dumps(dict(
                        old_keys=old_keys,new_keys=new_keys,analyze_executed=False,physical_temp_claimed=False)),flush=True)
                replay = self.sql(self.admin('SELECT id FROM storage_v2_put_search_document('
                    f"'mainrag.lexical-simple.v1','node',{graph['node']},{self.quote(text)},{self.identifiers_sql(text)})"))
                self.assertEqual(int(replay),after['id'])
        graph = self.seed('fixture175 null input')
        for producer in (previous.REFERENCE, 'storage_v2_put_search_document'):
            self.assert_sql_fails(self.admin(f"SELECT {producer}('null175','node',{graph['node']},NULL,ARRAY[]::TEXT[])"),
                                  'search')
        self.assertEqual(self.contracts_after175,json.loads(self.contracts()))
        self.sql(f"SET app.user_id='{self.schema.ADMIN_ID}';SELECT storage_v2_posting_conversion_require_operator();"
                 'SELECT storage_v2_require_lexical_conversion_operator();')
        print('BOUNDED_POSTING_TOKEN_KEYS_NATIVE_PARITY '+json.dumps(dict(
            cases=len(fixtures),bytes=65536,tokenizer_arrays_exact=True,physical_postings_exact=True,
            context_identity_exact=True,authority_unchanged=True,reader_functions_unchanged=True,
            producer_semantics_unchanged=True,posting_window_key_bounded=True,
            migration_sha256=hashlib.sha256(MIGRATION.read_bytes()).hexdigest(),
            predecessor_function_sha256=PREDECESSOR,resulting_function_sha256=self.after175[PRODUCER],
            resulting_context_sha256={sig:self.after175[sig] for sig in (BEGIN, REQUIRE)},
            timing=timings,performance_sla_claimed=False),sort_keys=True),flush=True)


if __name__ == '__main__':
    unittest.main()
