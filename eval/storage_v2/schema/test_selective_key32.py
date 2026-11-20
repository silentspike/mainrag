"""One disposable184 cohort: selective key32 probes keep complete exact semantics."""
from __future__ import annotations
import hashlib
import importlib.util
import json
import unittest
import uuid
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

    def test_inverted_ingest_opt_in_shared_layout_and_drift(self):
        token = uuid.uuid4().hex
        source = int(self.sql(
            f"INSERT INTO sources(id,name,type,path) SELECT max(id)+1,'inverted-{token}',"
            "'fixture','synthetic-inverted' FROM sources RETURNING id"
        ))
        run = self.begin(source, token * 2, uuid.uuid4().hex * 2)
        generation = int(self.sql(f"SELECT generation_id FROM storage_v2_ingest_run WHERE id={run}"))
        words = [['shared', f'ordinal{index}'] for index in range(64)]
        words[19].append(COLLISION[0])
        words[56].append(COLLISION[1])
        segments = [' '.join(group) for group in words]
        context_prefixes = ['headingproof' if index == 45 else '' for index in range(64)]
        start = 1
        starts = []
        for segment in segments:
            starts.append(start)
            start += len(segment) + 1
        # Use genuine small inline leaves and let PostgreSQL construct the
        # large document. The lexical writer receives only 64 short segments.
        body = f"{self.quote(' '.join(segments))} || repeat(' ', 262144)"
        graph = json.loads(self.sql(self.admin(f"""
            WITH text_body AS MATERIALIZED (SELECT {body} AS value),
            full_body AS MATERIALIZED (
                SELECT convert_to(value,'UTF8') AS bytes, value FROM text_body
            ), leaves AS MATERIALIZED (
                SELECT offset_bytes, node.id
                FROM full_body
                CROSS JOIN LATERAL generate_series(1,octet_length(bytes),60000) offsets(offset_bytes)
                CROSS JOIN LATERAL storage_v2_put_inline_body(
                    substring(bytes FROM offset_bytes FOR 60000)
                ) part
                CROSS JOIN LATERAL storage_v2_put_leaf_node('shadow-fixture','text',part.id) node
            ), children AS (
                SELECT array_agg(id ORDER BY offset_bytes) AS ids FROM leaves
            ), root AS MATERIALIZED (
                SELECT node.id, octet_length(bytes) AS byte_count, sha256(bytes) AS digest, value
                FROM full_body CROSS JOIN children
                CROSS JOIN LATERAL storage_v2_put_internal_node('shadow-fixture','document',
                    octet_length(bytes), array_fill('content'::TEXT,ARRAY[cardinality(ids)]), ids
                ) node
            ), components AS MATERIALIZED (
                SELECT root.*, retrieval.id AS view_id
                FROM root CROSS JOIN LATERAL storage_v2_put_retrieval_view(
                    'chunk','fixture-view-v1','text','fixture-tokenizer-v1',0,
                    ARRAY['content'],ARRAY['node'],ARRAY[root.id],ARRAY[0::BIGINT],ARRAY[byte_count::BIGINT]
                ) retrieval
            ), documents AS MATERIALIZED (
                SELECT components.*, document.id AS document_id
                FROM components CROSS JOIN LATERAL storage_v2_put_search_document(
                    'mainrag.lexical-simple.v1','node',components.id,value,ARRAY[]::TEXT[]
                ) document
            )
            SELECT jsonb_build_object('node',id,'view',view_id,'document',document_id,
                'bytes',byte_count,'digest',encode(digest,'hex'))
            FROM documents;
        """)))
        self.sql(self.admin(f"SELECT storage_v2_bind_search_document({graph['view']},0,{graph['document']},1.0)"))
        self.complete_analysis(graph['digest'])
        items = json.loads(self.sql(self.admin(f"""
            SELECT jsonb_object_agg(inputs.item,jsonb_build_array(staged.occurrence_id,staged.artifact_version_id))
            FROM (VALUES ('disabled'),('first'),('second')) inputs(item)
            CROSS JOIN LATERAL storage_v2_stage_shadow_item(
                {run},inputs.item,'document','synthetic-item',jsonb_build_object('item',inputs.item),
                'fixture-adapter-v1',{graph['node']},NULL,'{graph['digest']}',{graph['bytes']},
                decode('{graph['digest']}','hex'),'fixture-analysis-v1',{graph['view']},
                '/synthetic/'||inputs.item,'{{"byte_start":0}}'::JSONB
            ) staged;
        """)))
        # The context writer requires a building run during the test. Seal
        # these staged items afterwards so later retention cases see no writer.
        self.addCleanup(self.commit, run, 3)
        self.assertEqual(len({row[0] for row in items.values()}), 3)
        self.assertEqual(len({row[1] for row in items.values()}), 3)

        def write(item, function='storage_v2_put_lexical_segments_located', start_order=0):
            occurrence, artifact = items[item]
            return (
                f'{function}({occurrence},{artifact},'
                f'{self.array(list(range(start_order, start_order + 64)), "BIGINT")},{self.array(segments, "TEXT")},'
                f'{self.array(context_prefixes, "TEXT")},array_fill(\'textkind\'::TEXT,ARRAY[64]),'
                f'{self.array(starts, "BIGINT")},{self.array(starts, "BIGINT")})'
            )

        def app_write(item, *, with_context=False):
            if not with_context:
                return self.admin(f"SELECT {write(item)};RESET ROLE;")
            occurrence, artifact = items[item]
            return self.admin(
                f'SELECT storage_v2_begin_lexical_document_context({run},{occurrence},{artifact},64);'
                f"SELECT {write(item, 'storage_v2_stage_lexical_document_context')};"
                f'SELECT storage_v2_finish_lexical_document_context({occurrence},{artifact});'
                'RESET ROLE;'
            )

        def projection_state(stage):
            return f"""
                SELECT jsonb_build_object('stage','{stage}','projections',(SELECT jsonb_agg(
                    jsonb_build_array(document_id,representative_occurrence_id,representative_block_order,
                                      encode(layout_sha256,'hex'),encode(payload,'hex'))
                    ORDER BY document_id,layout_sha256)
                    FROM storage_v2_inverted_projection WHERE source_id={source}),
                    'bindings',(SELECT jsonb_agg(jsonb_build_array(
                        occurrence_id,artifact_version_id,document_id,block_order,encode(layout_sha256,'hex'))
                        ORDER BY occurrence_id)
                        FROM storage_v2_inverted_binding WHERE source_id={source}),
                    'derived',(SELECT jsonb_object_agg(occurrence_id,fingerprints)
                        FROM storage_v2_derived_lexical_block WHERE source_id={source}));
            """

        def transaction_body(number):
            path, = (ROOT / 'migrations').glob(f'{number}_*.sql')
            return path.read_text().replace('\nBEGIN;', '', 1).replace('\nCOMMIT;', '', 1)

        activation = f'SELECT storage_v2_set_inverted_source_enabled({source},TRUE);'
        denied_operator = f"""
            DO $fixture_inverted_denied$
            BEGIN
                BEGIN
                    PERFORM set_config('app.user_id','{self.schema.OTHER_ID}',TRUE);
                    PERFORM storage_v2_set_inverted_source_enabled({source},TRUE);
                    RAISE EXCEPTION 'fixture accepted unauthorized inverted source enable';
                EXCEPTION WHEN insufficient_privilege THEN
                    IF SQLERRM NOT LIKE '%inverted projection requires local administrative operator%' THEN
                        RAISE;
                    END IF;
                END;
            END $fixture_inverted_denied$;
        """
        drift = f"""
            DO $fixture_inverted_drift$
            BEGIN
                BEGIN
                    ALTER TABLE storage_v2_inverted_projection
                        DISABLE TRIGGER storage_v2_inverted_projection_immutable;
                    UPDATE storage_v2_inverted_projection
                       SET payload=set_byte(payload,2,get_byte(payload,2) # 1)
                     WHERE source_id={source};
                    IF NOT FOUND THEN RAISE EXCEPTION 'fixture did not simulate stored payload drift'; END IF;
                    ALTER TABLE storage_v2_inverted_projection
                        ENABLE TRIGGER storage_v2_inverted_projection_immutable;
                    PERFORM {write('second')};
                    RAISE EXCEPTION 'fixture ingest accepted the changed same-layout payload';
                EXCEPTION WHEN OTHERS THEN
                    IF SQLERRM NOT LIKE '%inverted projection payload proof differs%' THEN
                        RAISE;
                    END IF;
                END;
            END $fixture_inverted_drift$;
        """
        seal_generation = self.admin(
            f'SELECT (storage_v2_commit_shadow_ingest({run},3,'
            f'storage_v2_shadow_generation_root({run}))).status;'
            f'SELECT view_count FROM storage_v2_materialize_reader_metadata({generation});'
            'RESET ROLE;'
        )

        def require_publication_error(call, message):
            return f"""
                DO $fixture_inverted_incomplete$
                BEGIN
                    BEGIN
                        PERFORM {call};
                        RAISE EXCEPTION 'fixture attempted an incomplete inverted publication';
                    EXCEPTION WHEN OTHERS THEN
                        IF SQLERRM NOT LIKE {self.quote('%' + message + '%')} THEN RAISE; END IF;
                    END;
                END $fixture_inverted_incomplete$;
            """

        def append_batch(stage, batch):
            return (
                f"SELECT jsonb_build_object('stage','{stage}','batch',to_jsonb(receipt)) "
                f"FROM storage_v2_append_inverted_batch({generation},{batch},1) receipt;"
            )

        def ready_state(stage):
            return (
                f"SELECT jsonb_build_object('stage','{stage}','ready',"
                f"storage_v2_inverted_generation_ready({generation}));"
            )

        def backfill_state(stage):
            return (
                f"SELECT jsonb_build_object('stage','{stage}','backfill',to_jsonb(receipt)) "
                f"FROM storage_v2_backfill_inverted_projections({generation},0,-1,1,1048576) receipt;"
            )

        occurrence_scope = self.array(sorted(occurrence for occurrence, _ in items.values()), 'BIGINT')
        queries = ','.join(f'({self.quote(query)})' for query in (
            'headingproof', *COLLISION, 'invertednoprobe', '"shared ordinal19"',
        ))

        def reader_state(stage):
            return f"""
                SELECT jsonb_build_object('stage','{stage}','queries',jsonb_object_agg(query,
                    (SELECT coalesce(jsonb_agg(to_jsonb(hit)
                        ORDER BY hit.occurrence_id,hit.segment_order),'[]'::JSONB)
                     FROM storage_v2_derived_lexical_first_candidates(
                        {occurrence_scope},ARRAY[{source}]::BIGINT[],query
                     ) hit)))
                FROM (VALUES {queries}) inputs(query);
            """

        def reader_choice(stage):
            return (
                f"SELECT jsonb_build_object('stage','{stage}','generation',"
                f"storage_v2_inverted_matching_generation({source},{occurrence_scope}));"
            )

        # Independently reverse the prefix transpose into the original
        # five-byte slots and order each block by its complete coarse key.
        published_payloads = f"""
            WITH entries AS (
                SELECT p.generation_id,p.batch_no,p.prefix,
                    get_byte(p.postings,i) AS low_byte,
                    get_byte(p.postings,i+1)*256+get_byte(p.postings,i+2) AS block_id,
                    decode(lpad(to_hex(p.prefix::INTEGER),2,'0'),'hex')
                    ||substring(p.postings FROM i+1 FOR 1)
                    ||substring(p.postings FROM i+4 FOR 3) AS term_slot
                FROM storage_v2_inverted_prefix p
                CROSS JOIN LATERAL generate_series(0,octet_length(p.postings)-6,6) offsets(i)
                WHERE p.generation_id={generation}
            ), blocks AS (
                SELECT b.occurrence_id,b.block_order,
                    encode(string_agg(term_slot, ''::BYTEA ORDER BY e.prefix,e.low_byte),'hex') AS payload
                FROM entries e JOIN storage_v2_inverted_block b
                  USING(generation_id,batch_no,block_id)
                GROUP BY b.occurrence_id,b.block_order
            )
            SELECT jsonb_build_object('stage','published_payloads','blocks',
                jsonb_agg(jsonb_build_array(occurrence_id,block_order,payload) ORDER BY occurrence_id,block_order))
            FROM blocks;
        """
        output = self.sql(
            f"BEGIN;SET LOCAL app.user_id='{self.schema.ADMIN_ID}';"
            + transaction_body(188) + transaction_body(189) + denied_operator
            + app_write('disabled') + projection_state('disabled')
            + activation + app_write('first') + app_write('second', with_context=True) + projection_state('enabled')
            + app_write('second', with_context=True) + projection_state('retry') + drift + projection_state('restored')
            + seal_generation + reader_state('existing_reader')
            + transaction_body(190) + transaction_body(191) + transaction_body(192) + transaction_body(195)
            + ready_state('unpublished') + reader_choice('unpublished_choice')
            + require_publication_error(f'storage_v2_append_inverted_batch({generation},0,3)',
                                        'inverted batch requires staged coverage for every selected derived block')
            + backfill_state('backfilled') + backfill_state('backfill_replay') + projection_state('filled')
            + append_batch('first_batch', 0) + append_batch('first_replay', 0) + ready_state('partially_appended')
            + require_publication_error(f'storage_v2_publish_inverted_generation({generation},1)',
                                        'inverted publication derived coverage is incomplete')
            + append_batch('second_batch', 1) + append_batch('third_batch', 2)
            + f'SELECT storage_v2_publish_inverted_generation({generation},3);'
            + ready_state('published') + published_payloads
            + reader_choice('published_choice') + reader_state('published_reader')
            + 'SAVEPOINT fixture_new_derived_block;'
            + self.admin(f"SELECT {write('first', start_order=64)};RESET ROLE;")
            + reader_choice('new_block_choice') + reader_state('new_block_reader')
            + 'ROLLBACK TO fixture_new_derived_block; RELEASE fixture_new_derived_block;'
            + f"SET LOCAL app.user_id='{self.schema.OTHER_ID}';"
            + reader_choice('denied_choice') + reader_state('denied_reader')
            + f"SET LOCAL app.user_id='{self.schema.ADMIN_ID}';"
            + f'SELECT storage_v2_set_inverted_source_enabled({source},FALSE);'
            + reader_choice('disabled_choice') + reader_state('disabled_reader')
            + "ROLLBACK; SELECT to_regclass('public.storage_v2_inverted_projection') IS NULL"
            " AND to_regclass('public.storage_v2_inverted_generation') IS NULL;"
        )
        states = {row['stage']: row for row in map(json.loads, (
            line for line in output.splitlines() if line.startswith('{')
        ))}
        self.assertIsNone(states['disabled']['projections'])
        self.assertIsNone(states['disabled']['bindings'])
        self.assertEqual(set(states['disabled']['derived']), {str(items['disabled'][0])})

        enabled = states['enabled']
        self.assertEqual(len(enabled['projections']), 1)
        document, representative, block_order, layout_hash, payload = enabled['projections'][0]
        self.assertEqual((document, representative, block_order), (graph['document'], items['first'][0], 0))
        self.assertEqual(enabled['bindings'], sorted(
            [occurrence, artifact, graph['document'], 0, layout_hash]
            for occurrence, artifact in (items['first'], items['second'])
        ))
        first = {}
        for ordinal, terms in enumerate(words):
            for term in [*terms, 'textkind', context_prefixes[ordinal]]:
                if term:
                    first.setdefault(term, ordinal)
        groups = {}
        for term, ordinal in first.items():
            key = hashlib.sha256(term.encode('UTF8')).digest()[:4]
            groups.setdefault(key[:2], []).append((key[2:], ordinal))
        expected = b''.join(
            coarse + (suffixes[0][0] + bytes([suffixes[0][1]])
                      if len(suffixes) == 1 else b'\x00\x00\xff')
            for coarse, suffixes in sorted(groups.items())
        )
        self.assertTrue(any(len(group) > 1 for group in groups.values()))
        self.assertEqual(bytes.fromhex(payload), expected)
        fingerprints = [int.from_bytes(coarse, 'big') for coarse in sorted(groups)]
        for occurrence, _ in items.values():
            self.assertEqual(enabled['derived'][str(occurrence)], fingerprints)
        for replay in ('retry', 'restored'):
            self.assertEqual({k: v for k, v in states[replay].items() if k != 'stage'},
                             {k: v for k, v in enabled.items() if k != 'stage'})
        self.assertEqual(states['filled']['projections'], enabled['projections'])
        self.assertEqual(len(states['filled']['bindings']), 3)
        for number, stage in enumerate(('first_batch', 'second_batch', 'third_batch')):
            self.assertEqual(states[stage]['batch'], dict(
                disposition='appended', batch_no=number, block_count=1, total_blocks=number + 1,
            ))
        self.assertEqual(states['first_replay']['batch'],
                         dict(states['first_batch']['batch'], disposition='replayed'))
        self.assertFalse(states['unpublished']['ready'])
        self.assertFalse(states['partially_appended']['ready'])
        self.assertTrue(states['published']['ready'])
        self.assertEqual(states['published_payloads']['blocks'],
                         sorted([occurrence, 0, payload] for occurrence, _ in items.values()))
        self.assertEqual(states['backfilled']['backfill']['staged_blocks'], 1)
        self.assertEqual(states['backfilled']['backfill']['decoded_occurrences'], 1)
        self.assertEqual(states['backfill_replay']['backfill']['staged_blocks'], 0)
        self.assertEqual(states['backfill_replay']['backfill']['decoded_occurrences'], 0)
        for stage in ('unpublished_choice', 'new_block_choice', 'denied_choice', 'disabled_choice'):
            self.assertIsNone(states[stage]['generation'])
        self.assertEqual(states['published_choice']['generation'], generation)
        old_reader = states['existing_reader']['queries']
        self.assertEqual(states['published_reader']['queries'], old_reader)
        self.assertEqual(states['new_block_reader']['queries'], old_reader)
        self.assertEqual(states['disabled_reader']['queries'], old_reader)
        self.assertTrue(all(not hits for hits in states['denied_reader']['queries'].values()))
        for query, ordinal in (('headingproof',45),(COLLISION[0],19),(COLLISION[1],56)):
            self.assertEqual([hit['segment_order'] for hit in old_reader[query]], [ordinal] * 3)
        self.assertEqual(old_reader['invertednoprobe'], [])
        self.assertEqual(output.splitlines()[-1], 't')


# Reuse setup and fixture methods, never rerun unrelated inherited campaigns.
for _name in dir(SelectiveKey32Tests):
    if _name.startswith('test_') and _name not in SelectiveKey32Tests.__dict__:
        setattr(SelectiveKey32Tests,_name,None)


if __name__=='__main__':unittest.main()
