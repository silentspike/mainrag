"""One disposable184 cohort: selective key32 probes keep complete exact semantics."""
from __future__ import annotations
import hashlib
import importlib.util
import json
import unittest
from eval.storage_v2.schema import test_copied_literal_existence as previous
from eval.storage_v2.schema import test_encoded_lexical_blocks as encoded
from eval.storage_v2.schema import test_retained_lexical_conversion as lexical
from eval.storage_v2.schema import test_exact_lexeme_probes as exact

ROOT=previous.fragment.ROOT
MIGRATION=ROOT/'migrations/185_storage_v2_selective_key32.sql'
CACHED='storage_v2_authorized_cached_lexical_first_candidates(bigint[],bigint[],text)'
POSTING='storage_v2_scoped_query_posting(bigint[],text[])'
LEXICAL_INDEX='idx_storage_v2_compact_lexical_key32'
POSTING_INDEX='idx_storage_v2_compact_posting_key32'
LEXICAL_DDL=f'CREATE INDEX {LEXICAL_INDEX} ON storage_v2_compact_lexical_block USING GIN(storage_v2_probe_keys32(storage_v2_compact_lexical_exact_lexemes(fts_vectors)))'
POSTING_DDL=f'CREATE INDEX {POSTING_INDEX} ON storage_v2_compact_posting_block USING GIN(storage_v2_probe_keys32(terms))'
COLLISION=('key32collision11821', 'key32collision71533')


class SelectiveKey32Tests(previous.CopiedLiteralExistenceTests, encoded.EncodedLexicalBlockTests):
    # Shared Fresh170 ancestor runs once. Encoded172 is inserted by the MRO
    # before Lazy173 resumes, preserving actual171->172->173->...->184 order.
    array=lexical.RetainedLexicalConversionTests.array
    block=encoded.EncodedLexicalBlockTests.block

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            if cls.sql("SELECT to_regclass('public.storage_v2_encoded_lexical_block') IS NOT NULL")!='t':
                raise AssertionError('genuine chronological172 encoded schema required before185')
            for signature,name in ((CACHED,'fixture185_old_lexical'),(POSTING,'fixture185_old_posting')):
                definition=cls.sql(f"SELECT pg_get_functiondef('{signature}'::REGPROCEDURE)")
                original=signature.split('(')[0];args=signature.split('(',1)[1]
                cls.sql(definition.replace('FUNCTION public.'+original+'(', 'FUNCTION public.'+name+'(',1))
                owner='mainrag_v2_lexical_rank_owner' if signature==CACHED else 'mainrag'
                cls.sql(f'ALTER FUNCTION {name}({args} OWNER TO {owner};REVOKE ALL ON FUNCTION {name}({args} FROM PUBLIC;')
            cls.before185=cls.function_hashes();cls.authorities185=cls.authority_catalog()
            cls.posting_members=cls.sql('SELECT count(*) FROM storage_v2_posting_conversion_contract')
            cls.sql(f"SET app.user_id='{cls.schema.ADMIN_ID}';"+MIGRATION.read_text())
        except BaseException:
            cls.base.tearDownClass.__func__(cls);raise

    def ready(self,kind=None):
        argument='' if kind is None else self.quote(kind)
        return self.sql(f'SELECT storage_v2_key32_probes_ready({argument})')

    def native185(self,row,query,*,old=False,actor=None):
        function='fixture185_old_lexical' if old else CACHED.split('(')[0]
        return self.sql(f"SET app.user_id='{actor or self.schema.ADMIN_ID}';"+
            "SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY occurrence_id,segment_order),'[]'::JSONB) FROM "+
            f"{function}(ARRAY[{row['occurrence']},{row['occurrence']},NULL]::BIGINT[],ARRAY[{row['source']}]::BIGINT[],{self.quote(query)}) r")

    def posting185(self,document,terms,*,old=False):
        function='fixture185_old_posting' if old else POSTING.split('(')[0]
        array='ARRAY['+','.join('NULL' if term is None else self.quote(term) for term in terms)+']::TEXT[]'
        return self.sql(self.admin("SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY term),'[]'::JSONB) FROM "+
            f'{function}(array_fill({document}::BIGINT,ARRAY[1025]),{array}) r'))

    def test_keys_are_signed_sorted_distinct_and_contracts_only_change_named_readers(self):
        terms=['alpha','βeta','日本語','',COLLISION[0],COLLISION[1],'alpha',None]
        expected=sorted({int.from_bytes(hashlib.sha256(t.encode()).digest()[:4],'big',signed=True) for t in terms if t is not None})
        actual=json.loads(self.sql(f'SELECT array_to_json(storage_v2_probe_keys32({self.array([t for t in terms if t is not None],"TEXT")}))'))
        self.assertEqual(actual,expected)
        self.assertEqual(self.sql('SELECT storage_v2_probe_keys32(ARRAY[]::TEXT[])'), '{}')
        self.assertEqual(self.sql('SELECT storage_v2_probe_keys32(NULL::TEXT[]) IS NULL'), 't')
        self.assertEqual(self.sql("SELECT storage_v2_probe_keys32(ARRAY[NULL]::TEXT[])"), '{}')
        self.assertEqual(self.sql(f'SELECT cardinality(storage_v2_probe_keys32({self.array(list(COLLISION),"TEXT")}))'),'1')
        after=self.function_hashes();added=set(after)-set(self.before185)
        self.assertEqual(added,{'storage_v2_probe_keys32(text[])','storage_v2_key32_probes_ready(text)','storage_v2_key32_probes_ready()'})
        self.assertEqual({key for key in self.before185 if after[key]!=self.before185[key]},{CACHED,POSTING})
        authority=self.authority_catalog()
        self.assertEqual({k:authority[k] for k in self.authorities185},self.authorities185)
        self.assertEqual(self.sql('SELECT count(*) FROM storage_v2_posting_conversion_contract'),self.posting_members)
        self.sql(self.operator('SELECT storage_v2_posting_conversion_require_operator();SELECT storage_v2_require_lexical_conversion_operator()'))

    def test_reader_collision_partial_rollout_encoded_fallback_drift_and_real_operator(self):
        a,b=COLLISION
        collision,_,_=self.block([b])
        conjunction,_,_=self.block([a,b])
        encoded_row,_,_=self.block(['alpha beta','alpha gamma'])
        # A fixture-only exact same-transaction guarded transition creates the
        # real encoded tuple shape without claiming codec cost admission.
        self.sql(self.operator(f"""CREATE TEMP TABLE fixture185_encoded ON COMMIT DROP AS
 SELECT ROW(c.occurrence_id,c.source_id,c.artifact_version_id,c.block_order,c.segment_orders,
 c.text_starts,c.text_lengths,c.text_hashes,c.context_prefixes,c.chunk_types,
 storage_v2_encode_lexical_vectors(c.fts_vectors),c.fingerprints)::storage_v2_encoded_lexical_block destination,
 sha256(convert_to(to_jsonb(c)::TEXT,'UTF8')) original_sha
 FROM storage_v2_compact_lexical_block c WHERE c.occurrence_id={encoded_row['occurrence']} AND c.block_order=0;
 INSERT INTO storage_v2_encoded_lexical_intent
 SELECT {encoded_row['occurrence']},0,pg_current_xact_id(),original_sha,
 sha256(convert_to(to_jsonb(destination)::TEXT,'UTF8')),FALSE FROM fixture185_encoded;
 INSERT INTO storage_v2_encoded_lexical_block SELECT (destination).* FROM fixture185_encoded;
 DELETE FROM storage_v2_compact_lexical_block WHERE occurrence_id={encoded_row['occurrence']} AND block_order=0;
 DELETE FROM storage_v2_encoded_lexical_intent WHERE occurrence_id={encoded_row['occurrence']} AND block_order=0;
 """,timeout='30s'))
        long='longkey'+''.join(hashlib.sha256(f'key185-{n}'.encode()).hexdigest() for n in range(160))
        text=b+' '+' '.join(f'postingword{n}' for n in range(1000))+' '+long+' '+long
        node,_,_=self.make_projection(text)
        document=int(self.sql(self.admin('SELECT id FROM storage_v2_put_search_document('
            f"'selective-key32-fixture','node',{node},{self.quote(text)},ARRAY[]::TEXT[])")))
        self.assertGreater(int(self.sql(f'SELECT count(*) FROM storage_v2_compact_posting_block WHERE document_id={document}')),0)
        cases=[(collision,a),(collision,b),(conjunction,a+' '+b),(conjunction,'headingproof'),
               (conjunction,'"'+a+' '+b+'"'),(encoded_row,'alpha beta'),(encoded_row,'alpha missing')]
        expected=[self.native185(row,q,old=True) for row,q in cases]
        self.assertEqual(expected[0],'[]');self.assertEqual(expected[2],'[]')
        self.assertTrue(json.loads(expected[1]));self.assertTrue(json.loads(expected[5]))
        terms=[a,b,long,'missing',b,None];old_post=self.posting185(document,terms,old=True)
        def parity():
            self.assertEqual([self.native185(row,q) for row,q in cases],expected)
            self.assertEqual(self.posting185(document,terms),old_post)
            self.assertEqual(self.native185(collision,b,actor=self.schema.OTHER_ID),'[]')
        self.assertEqual(self.ready(),'f');parity()
        # No key32 helper executes on block arrays (or query terms) while both
        # new indexes are absent. Counters are actual PG function statistics.
        self.sql("SELECT pg_stat_reset_single_function_counters('storage_v2_probe_keys32(text[])'::REGPROCEDURE)")
        self.sql(self.operator(
            "SET LOCAL track_functions='all';SET LOCAL ROLE mainrag_v2_lexical_rank_owner;"
            f"SELECT count(*) FROM {CACHED.split('(')[0]}(ARRAY[{collision['occurrence']}]::BIGINT[],ARRAY[{collision['source']}]::BIGINT[],{self.quote(b)});"
            "SET LOCAL ROLE mainrag;"
            f"SELECT count(*) FROM {POSTING.split('(')[0]}(array_fill({document}::BIGINT,ARRAY[1025]),ARRAY[{self.quote(b)}]::TEXT[])"))
        self.assertEqual(self.sql("SELECT coalesce(pg_stat_get_function_calls('storage_v2_probe_keys32(text[])'::REGPROCEDURE),0)"),'0')
        for definition,kind in ((LEXICAL_DDL,'lexical'),(POSTING_DDL,'posting')):
            self.sql(definition)
            self.assertEqual(self.ready(kind),'t');parity()
        self.assertEqual(self.ready(),'t')
        # Extract the installed reader's real conditional predicate. A custom
        # plan must fold the flag and expose the exact optional expression index;
        # the false branch must remove all block-array key hashing from the plan.
        def indexes(plan):
            return ([plan['Index Name']] if 'Index Name' in plan else [])+[
                name for child in plan.get('Plans',[]) for name in indexes(child)]
        for signature,flag,keys,old_flag,old_keys,table,index,argtype in (
            (CACHED,'lazy_key32_ready','lazy_keys32','lazy_legacy_exact_ready','lazy_terms',
             'storage_v2_compact_lexical_block',LEXICAL_INDEX,'TEXT[]'),
            (POSTING,'v_key32_ready','v_keys32','v_exact_lexeme_probes','v_hashes',
             'storage_v2_compact_posting_block',POSTING_INDEX,'BYTEA[]')):
            definition=self.sql(f"SELECT pg_get_functiondef('{signature}'::REGPROCEDURE)")
            predicate='CASE WHEN '+flag+' THEN'+definition.split('AND CASE WHEN '+flag+' THEN',1)[1].split(' END',1)[0]+' END'
            for old,new in ((flag,'$1'),(keys,'$2'),(old_flag,'$3'),(old_keys,'$4')):
                predicate=predicate.replace(old,new)
            identifier='occurrence_id' if signature==CACHED else 'document_id'
            for enabled in (True,False):
                arguments=('TRUE' if enabled else 'FALSE')+f",storage_v2_probe_keys32(ARRAY[{self.quote(b)}]::TEXT[]),FALSE,NULL::{argtype}"
                plan=json.loads(self.sql('SET enable_seqscan=off;SET plan_cache_mode=force_custom_plan;'
                    f'PREPARE fixture185_probe(BOOLEAN,INTEGER[],BOOLEAN,{argtype}) AS SELECT block.{identifier} FROM public.{table} block WHERE '+
                    predicate+';EXPLAIN(FORMAT JSON) EXECUTE fixture185_probe('+arguments+');DEALLOCATE fixture185_probe;'))[0]['Plan']
                if enabled:self.assertIn(index,indexes(plan))
                else:self.assertNotIn('storage_v2_probe_keys32',json.dumps(plan))
        for index in (LEXICAL_INDEX,POSTING_INDEX):
            self.assertEqual(self.sql('BEGIN;UPDATE pg_index SET indisvalid=FALSE '+
                f"WHERE indexrelid='{index}'::REGCLASS;SELECT storage_v2_key32_probes_ready();ROLLBACK;"),'f')
        # Existing exact indexes remain a valid fallback if a key32 index is
        # temporarily unavailable; the old wide definitions are never changed.
        self.sql(exact.NATIVE_DDL);self.sql(exact.POSTING_DDL)
        self.sql(f'DROP INDEX {LEXICAL_INDEX};DROP INDEX {POSTING_INDEX}')
        self.assertEqual(self.ready(),'f');parity()
        self.sql(f'DROP INDEX {exact.NATIVE_INDEX};DROP INDEX {exact.POSTING_INDEX}')
        for name,kind in ((LEXICAL_INDEX,'lexical'),(POSTING_INDEX,'posting')):
            self.sql(f'CREATE INDEX {name} ON occurrence(id)')
            self.assert_sql_fails(self.admin(f"SELECT storage_v2_key32_probes_ready('{kind}')"),'key32 index definition differs')
            self.assert_sql_fails(self.admin('SELECT storage_v2_key32_probes_ready()'),'key32 index definition differs')
            self.sql(f'DROP INDEX {name}')
        self.sql(self.operator('SELECT storage_v2_posting_conversion_require_operator();SELECT storage_v2_require_lexical_conversion_operator()'))
        # Reuse the genuine original persistent Session/CIC/catalog-CAS/inverse
        # path on this same database. No additional database or runner is made.
        path=ROOT/'ops/storage-v2/test_exact_lexeme_indexes.py'
        spec=importlib.util.spec_from_file_location('key185_real_operator',path)
        fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
        result=fixture.exercise_disposable_catalog_protocol(self.database,self.socket,self.schema.ADMIN_ID,
            config={'index_variant':'key32'})
        self.assertEqual(result['status'],'PASS');self.assertTrue(result['original_catalog_restored'])
        self.sql(self.operator('SELECT storage_v2_posting_conversion_require_operator();SELECT storage_v2_require_lexical_conversion_operator()'))

    def test_scope_join_preserves_candidate_identity_and_authority(self):
        words = ' '.join(f'scopeword{number}' for number in range(120))
        row, _, _ = self.block([words, words + ' afternote'])
        occurrence = row['occurrence']
        source = row['source']
        conversion = self.sql(self.operator(
            "SELECT disposition FROM storage_v2_convert_retained_lexical_block("
            f"'00000000-0000-0000-0000-000000000186',decode('{'ba' * 32}','hex'),{occurrence},0,"
            f"decode('{row['original']}','hex'),decode('{row['identity']}','hex'),8388608,10000)"))
        self.assertEqual(conversion, 'CONVERTED')

        signature = 'storage_v2_derived_lexical_candidate_occurrences(bigint[],bigint[],integer[])'
        candidate = signature.split('(')[0]
        observed = f"""
            SELECT jsonb_agg((SELECT count(*) FROM {candidate}(scopes.ids,ARRAY[{source}]::BIGINT[],scopes.keys))
                             ORDER BY scopes.sequence)
            FROM (VALUES
                (1,ARRAY[{occurrence},{occurrence},NULL]::BIGINT[],storage_v2_posting_fingerprints(ARRAY['afternote'])),
                (2,ARRAY[NULL]::BIGINT[],NULL::INTEGER[]),
                (3,ARRAY[]::BIGINT[],NULL::INTEGER[]),
                (4,NULL::BIGINT[],NULL::INTEGER[]),
                (5,ARRAY[{occurrence}]::BIGINT[],NULL::INTEGER[])
            ) AS scopes(sequence,ids,keys);"""
        migration = (ROOT / 'migrations/186_storage_v2_join_derived_candidate_scope.sql').read_text()
        migration = migration.replace('\nBEGIN;','',1).replace('\nCOMMIT;','',1)
        admin = f"SET LOCAL app.user_id='{self.schema.ADMIN_ID}';"
        other = f"SET LOCAL app.user_id='{self.schema.OTHER_ID}';"
        authority = f"SELECT to_jsonb(p)-'prosrc' FROM pg_proc p WHERE oid='{signature}'::REGPROCEDURE;"
        output = self.sql('BEGIN;' + admin + authority + observed + other + observed + admin
                          + migration + authority + observed + other + observed + 'ROLLBACK;')
        values = list(map(json.loads, filter(None, output.splitlines())))
        before_acl, before_hits, before_denied, after_acl, after_hits, after_denied = values
        self.assertEqual(before_acl, after_acl)
        self.assertEqual(before_hits, [1, 0, 0, 0, 1])
        self.assertEqual(after_hits, before_hits)
        self.assertEqual(before_denied, [0, 0, 0, 0, 0])
        self.assertEqual(after_denied, before_denied)


# Reuse setup and fixture methods, never rerun unrelated inherited campaigns.
for _name in dir(SelectiveKey32Tests):
    if _name.startswith('test_') and _name not in SelectiveKey32Tests.__dict__:
        setattr(SelectiveKey32Tests,_name,None)


if __name__=='__main__':unittest.main()
