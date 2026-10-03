"""Full native envelopes, request parsing and key-only presence invariants."""
import json
import unittest

from eval.storage_v2.schema import test_native_reader_inputs as previous

MIGRATION = previous.ROOT / 'migrations/133_storage_v2_bound_query_and_presence_work.sql'


class BoundQueryAndPresenceTests(unittest.TestCase):
    schema = previous.NativeReaderInputTests.schema
    command = classmethod(previous.NativeReaderInputTests.command.__func__)
    sql = classmethod(previous.NativeReaderInputTests.sql.__func__)
    file = classmethod(previous.NativeReaderInputTests.file.__func__)
    admin = classmethod(previous.NativeReaderInputTests.admin.__func__)
    actor = staticmethod(previous.NativeReaderInputTests.actor)
    quote = staticmethod(previous.NativeReaderInputTests.quote)
    make_projection = previous.NativeReaderInputTests.make_projection
    begin = previous.NativeReaderInputTests.begin
    stage = previous.NativeReaderInputTests.stage
    complete_analysis = previous.NativeReaderInputTests.complete_analysis
    commit = previous.NativeReaderInputTests.commit
    put = previous.NativeReaderInputTests.put
    assert_sql_fails = previous.NativeReaderInputTests.assert_sql_fails
    profile = previous.NativeReaderInputTests.profile

    @classmethod
    def setUpClass(cls):
        try:
            previous.NativeReaderInputTests.setUpClass.__func__(cls)
            cls.file(previous.INPUTS)
            cls.file(previous.STATS)
        except BaseException:
            if hasattr(cls, 'stack'):
                cls.stack.close()
            raise

    @classmethod
    def tearDownClass(cls):
        previous.NativeReaderInputTests.tearDownClass.__func__(cls)

    def test_b_complete_named_active_and_authorization_envelopes(self):
        for source in (6, 9):
            run = self.begin(source, f'{source+10:02x}'*32, f'{source+30:02x}'*32)
            texts = ['alpha beta common key_identifier', 'alpha common beta',
                     'common beta', 'alpha beta gamma', 'alpha beta 日本語', 'common', '']
            for number, text in enumerate(texts):
                node, view, digest = self.make_projection(text)
                document = self.put(node, text)
                self.sql(self.admin(f'SELECT storage_v2_bind_search_document({view},0,{document},1.0)'))
                self.stage(run, f'bounded-request-{source}-{number}.txt', text, node, view, digest)
                self.complete_analysis(digest)
            self.commit(run, len(texts))
            rows = json.loads(self.sql(f"""SELECT jsonb_agg(jsonb_build_object('id',o.id,
                'artifact',o.artifact_version_id,'text',d.search_text) ORDER BY o.id)
                FROM occurrence o JOIN storage_v2_search_view_document b ON b.view_id=o.view_id AND b.ordinal=0
                JOIN storage_v2_search_document d ON d.id=b.document_id WHERE o.source_id={source}"""))
            for number, row in enumerate(rows):
                content = self.quote(row['text'])
                if row['text']:
                    # Include generated segments, nonzero orders, split AND
                    # terms and documents with no native lexical store.
                    if number == 4:
                        segments = "ARRAY['alpha','beta 日本語']"
                    else:
                        segments = f'ARRAY[{content},{content}]'
                    starts = 'ARRAY[1,7]::BIGINT[]' if number == 4 else 'ARRAY[1,1]::BIGINT[]'
                    if number < 5:
                        order = 0 if number % 2 else 7
                        self.sql(self.actor(self.schema.ADMIN_ID,
                            'SELECT storage_v2_put_lexical_segments_located('
                            f"{row['id']},{row['artifact']},ARRAY[{order},{order+1}]::BIGINT[],{segments},"
                            f"ARRAY['',''],ARRAY['text','text'],{starts},{starts})"))
                if number < 2:
                    self.sql(f"""WITH payload AS (
                        INSERT INTO storage_v2_legacy_rank_payload(source_id,payload_sha256,fts_vector)
                        VALUES({source},sha256(tsvectorsend(to_tsvector('simple',{content}))),to_tsvector('simple',{content}))
                        ON CONFLICT(source_id,payload_sha256) DO NOTHING RETURNING id)
                        INSERT INTO storage_v2_legacy_rank_binding(occurrence_id,source_id,artifact_version_id,
                            legacy_chunk_id,legacy_file_hash,payload_id)
                        SELECT {row['id']},{source},{row['artifact']},{10000+row['id']},sha256(convert_to({content},'UTF8')),id FROM payload""")
                for stage, status, score in [('graph','available',str((0,3,-2,1000001)[number%4])),
                                             ('semantic','unavailable','NULL'),('rerank','failed','NULL')]:
                    self.sql(self.admin('SELECT storage_v2_put_occurrence_score_component('
                        f"{row['id']},'{stage}','request-fixture','{status}',{score})"))

        source_ids = json.loads(self.sql('SELECT jsonb_agg(id ORDER BY id) FROM sources'))
        for source in source_ids:
            if source not in (6, 9):
                self.commit(self.begin(source, f'{source+100:064x}', f'{source+200:064x}'), 0)
        # Disposable active-reader fixture; no activation acceptance is claimed.
        self.sql("""ALTER TABLE source_generation DISABLE TRIGGER USER;
            ALTER TABLE logical_source DISABLE TRIGGER USER;
            UPDATE source_generation SET status='active',activated_at=now(),verified_at=now(),
                verification_manifest_sha256=repeat('c',64);
            UPDATE logical_source p SET active_generation_id=g.id FROM source_generation g WHERE g.source_id=p.id;
            ALTER TABLE source_generation ENABLE TRIGGER USER;
            ALTER TABLE logical_source ENABLE TRIGGER USER;
            ALTER TABLE storage_v2_activation_set_evidence DISABLE TRIGGER USER;
            INSERT INTO storage_v2_activation_set_evidence
                (id,manifest_sha256,source_count,pointer_set_sha256,source_classification_sha256)
                SELECT '00000000-0000-0000-0000-000000000133',repeat('a',64),count(*),repeat('b',64),
                    encode(digest(convert_to(jsonb_agg(jsonb_build_object('source_id',id,'is_test',is_test)
                        ORDER BY id)::text,'UTF8'),'sha256'),'hex') FROM sources;
            ALTER TABLE storage_v2_activation_set_evidence ENABLE TRIGGER USER;""")
        term = lambda v: {'type':'term','value':v}
        asts = [term('alpha'),term('common'),term('missing'),term('日本語'),
                {'type':'and','children':[term('alpha'),term('beta')]},
                {'type':'and','children':[term('alpha'),{'type':'and','children':[term('beta'),term('common')]}]},
                {'type':'or','children':[term('alpha'),term('gamma')]},
                {'type':'and','children':[term('common'),{'type':'not','children':[term('beta')]}]},
                {'type':'phrase','value':'alpha beta'}, {'type':'exact','value':'key_identifier'}]
        filters = [{},{stage+'_profile':'request-fixture' for stage in ('graph','semantic','rerank')}]

        def envelopes():
            statements = []
            for ast in asts:
                for values in filters:
                    for limit in (1, 3, 10):
                        a, f = self.quote(json.dumps(ast)), self.quote(json.dumps(values))
                        statements += [f"SELECT storage_v2_search_exact(6,'1',{a}::JSONB,{f}::JSONB,{limit});",
                            f"SELECT storage_v2_search_active_unchecked('{'a'*64}',{a}::JSONB,{f}::JSONB,{limit},6,FALSE);"]
            return [json.loads(line) for user in (self.schema.ADMIN_ID,self.schema.WRITER_ID)
                for line in self.sql(self.actor(user,'\n'.join(statements))).splitlines()]

        before = envelopes()
        self.file(MIGRATION)
        self.assertEqual(before, envelopes())
        for signature in ('storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)',
                          'storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'):
            definition = self.sql(f"SELECT pg_get_functiondef('{signature}'::REGPROCEDURE)")
            self.assertEqual(definition.count('storage_v2_simple_and_query(p_ast)'),1)
        self.assert_sql_fails(self.actor(self.schema.OTHER_ID,
            "SELECT storage_v2_search_exact(6,'1','{\"type\":\"term\",\"value\":\"alpha\"}','{}',10)"),
            'authorized generation selector required')
        print(f'complete named/active result envelopes: {len(before)}; nested AND, fallback, stages and authorization unchanged',flush=True)

        # Use the real fixture rows created by the first test, including both
        # source permissions, absent lexical data and duplicate/null IDs.
        scope = 'ARRAY(SELECT id FROM occurrence WHERE source_id IN (6,9))'
        for user in (self.schema.ADMIN_ID,self.schema.WRITER_ID,self.schema.OTHER_ID):
            for requested in (scope,scope+'||'+scope+'||ARRAY[NULL]::BIGINT[]','ARRAY[]::BIGINT[]','NULL::BIGINT[]'):
                statement = f"""WITH a AS (SELECT * FROM storage_v2_source_segment_presence({requested})),
                    r AS (SELECT * FROM fixture_presence({requested}))
                    SELECT NOT EXISTS(SELECT * FROM a EXCEPT ALL SELECT * FROM r)
                        AND NOT EXISTS(SELECT * FROM r EXCEPT ALL SELECT * FROM a)"""
                self.assertEqual(self.sql(self.actor(user,statement)),'t')
        for relation in ('storage_v2_compact_lexical_block','storage_v2_legacy_rank_binding','occurrence'):
            self.sql(f'VACUUM {relation}')
        result = self.profile('SET enable_seqscan=off; '+self.actor(self.schema.ADMIN_ID,
            f'SELECT count(*) FROM storage_v2_source_segment_presence({scope})'))
        plans = list(previous.previous.plans(result.stderr))
        compact = [n for p in plans for n in previous.previous.nodes(p['Plan'])
                   if n.get('Relation Name')=='storage_v2_compact_lexical_block' and n.get('Actual Loops',0)>0]
        self.assertTrue(compact)
        self.assertTrue(all(n['Node Type']=='Index Only Scan' and n['Heap Fetches']==0 for n in compact))
        print('12 independent presence comparisons; actual compact presence probes are index-only with zero heap fetches',flush=True)

    def test_a_constraint_and_authority_drift(self):
        # The old presence evaluator is an independent result reference.
        signature = 'storage_v2_source_segment_presence(bigint[])'
        original = self.sql(f"SELECT pg_get_functiondef('{signature}'::REGPROCEDURE)")
        reference = original.replace('public.storage_v2_source_segment_presence','public.fixture_presence',1)
        self.sql(reference+'; ALTER FUNCTION fixture_presence(BIGINT[]) OWNER TO mainrag_v2_frontier_owner; '
            'REVOKE ALL ON FUNCTION fixture_presence(BIGINT[]) FROM PUBLIC; GRANT EXECUTE ON FUNCTION fixture_presence(BIGINT[]) TO mainrag;')
        body = MIGRATION.read_text().replace('BEGIN;','',1).rsplit('COMMIT;',1)[0]
        for change, error in (
            ('ALTER TABLE storage_v2_compact_lexical_block ALTER COLUMN segment_orders DROP NOT NULL;', 'compact identity constraints'),
            ('ALTER TABLE storage_v2_compact_lexical_block DROP CONSTRAINT storage_v2_compact_lexical_bl_occurrence_id_source_id_arti_fkey;', 'compact identity constraints'),
            ('ALTER TABLE storage_v2_compact_lexical_block DROP CONSTRAINT storage_v2_compact_lexical_block_check;', 'compact identity constraints'),
            ("ALTER TABLE storage_v2_compact_lexical_block DROP CONSTRAINT storage_v2_compact_lexical_block_check; "
             "ALTER TABLE storage_v2_compact_lexical_block ADD CHECK(storage_v2_lexical_block_orders_valid(block_order,segment_orders)) NOT VALID;", 'compact identity constraints'),
            ('GRANT EXECUTE ON FUNCTION storage_v2_source_segment_presence(BIGINT[]) TO storage_v2_shadow_worker;', 'authority'),
        ):
            self.assert_sql_fails('BEGIN;'+change+body+'ROLLBACK;','bounded query '+error)
