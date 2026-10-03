"""Complete native reader equivalence and sparse compact/canonical work bounds."""
import json
import unittest

from eval.storage_v2.schema import test_cached_legacy_rank_snapshots as previous

ROOT = previous.ROOT
INPUTS = ROOT / 'migrations/131_storage_v2_filter_native_reader_inputs.sql'
STATS = ROOT / 'migrations/132_storage_v2_cover_reader_statistics.sql'


class NativeReaderInputTests(unittest.TestCase):
    schema = previous.CachedLegacyRankSnapshotTests.schema
    command = classmethod(previous.CachedLegacyRankSnapshotTests.command.__func__)
    sql = classmethod(previous.CachedLegacyRankSnapshotTests.sql.__func__)
    file = classmethod(previous.CachedLegacyRankSnapshotTests.file.__func__)
    admin = classmethod(previous.CachedLegacyRankSnapshotTests.admin.__func__)
    actor = staticmethod(previous.CachedLegacyRankSnapshotTests.actor)
    quote = staticmethod(previous.CachedLegacyRankSnapshotTests.quote)
    make_projection = previous.CachedLegacyRankSnapshotTests.make_projection
    begin = previous.CachedLegacyRankSnapshotTests.begin
    stage = previous.CachedLegacyRankSnapshotTests.stage
    complete_analysis = previous.CachedLegacyRankSnapshotTests.complete_analysis
    commit = previous.CachedLegacyRankSnapshotTests.commit
    put = previous.CachedLegacyRankSnapshotTests.put
    assert_sql_fails = previous.CachedLegacyRankSnapshotTests.assert_sql_fails

    @classmethod
    def setUpClass(cls):
        try:
            previous.CachedLegacyRankSnapshotTests.setUpClass.__func__(cls)
            cls.file(previous.CACHE)
            cls.file(previous.INDEX)
        except BaseException:
            if hasattr(cls, 'stack'):
                cls.stack.close()
            raise

    @classmethod
    def tearDownClass(cls):
        previous.CachedLegacyRankSnapshotTests.tearDownClass.__func__(cls)

    def profile(self, statement):
        return self.command('--command', "LOAD 'auto_explain'; SET auto_explain.log_min_duration=0; "
            "SET auto_explain.log_nested_statements=on; SET auto_explain.log_analyze=on; "
            "SET auto_explain.log_format=json; SET auto_explain.log_level=notice; "+statement)

    def test_a_complete_envelopes_independent_candidates_and_authority(self):
        signatures = [
            ('storage_v2_authorized_lexical_candidates(bigint[],bigint[],text)', 'fixture_reference_candidates', 'mainrag_v2_lexical_rank_owner'),
            ('storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])', 'fixture_reference_ranks', 'mainrag_v2_frontier_owner'),
            ('storage_v2_source_segment_presence(bigint[])', 'fixture_reference_presence', 'mainrag_v2_frontier_owner'),
        ]
        original = {}
        for signature, name, owner in signatures:
            definition = self.sql(f"SELECT pg_get_functiondef('{signature}'::REGPROCEDURE)")
            original[signature] = definition
            reference = definition.replace('public.'+signature.split('(')[0], 'public.'+name, 1)
            reference = reference.replace('public.storage_v2_authorized_lexical_candidates(', 'public.fixture_reference_candidates(')
            arguments = signature[signature.index('('):]
            self.sql(reference+f'; ALTER FUNCTION {name}{arguments} OWNER TO {owner}; '
                f'REVOKE ALL ON FUNCTION {name}{arguments} FROM PUBLIC; '
                f'GRANT EXECUTE ON FUNCTION {name}{arguments} TO mainrag_v2_frontier_owner;')

        for source in (6,9):
            run = self.begin(source, f'{source+10:02x}'*32, f'{source+30:02x}'*32)
            texts = ['alpha beta common key_identifier', 'alpha common beta', 'common gamma',
                     'alpha common', 'common beta', '日本語 common', '', 'common alpha beta']
            for number, text in enumerate(texts):
                node, view, digest = self.make_projection(text)
                doc = self.put(node, text)
                self.sql(self.admin(f'SELECT storage_v2_bind_search_document({view},0,{doc},1.0)'))
                self.stage(run, f'input-{source}-{number}.txt', text, node, view, digest)
                self.complete_analysis(digest)
            self.commit(run, len(texts))
            records = json.loads(self.sql(f"""SELECT jsonb_agg(jsonb_build_object('id',o.id,
              'artifact',o.artifact_version_id,'text',d.search_text) ORDER BY o.id)
              FROM occurrence o JOIN storage_v2_search_view_document b ON b.view_id=o.view_id AND b.ordinal=0
              JOIN storage_v2_search_document d ON d.id=b.document_id WHERE o.source_id={source}"""))
            for number, item in enumerate(records):
                text = self.quote(item['text'])
                if item['text']:
                    order = 0 if number%2 else 9
                    # Small batches use real compact storage, not a loose stand-in.
                    self.sql(self.actor(self.schema.ADMIN_ID,
                        'SELECT storage_v2_put_lexical_segments_located('
                        f"{item['id']},{item['artifact']},ARRAY[{order},{order+1}]::BIGINT[],"
                        f"ARRAY[{text},{text}],ARRAY['',''],ARRAY['text','text'],"
                        'ARRAY[1,1]::BIGINT[],ARRAY[1,1]::BIGINT[])'))
                if number<2:
                    self.sql(f"""WITH payload AS (
                      INSERT INTO storage_v2_legacy_rank_payload(source_id,payload_sha256,fts_vector)
                      VALUES ({source},sha256(tsvectorsend(to_tsvector('simple',{text}))),to_tsvector('simple',{text}))
                      ON CONFLICT(source_id,payload_sha256) DO NOTHING
                      RETURNING id)
                      INSERT INTO storage_v2_legacy_rank_binding(occurrence_id,source_id,artifact_version_id,
                        legacy_chunk_id,legacy_file_hash,payload_id)
                      SELECT {item['id']},{source},{item['artifact']},{10000+item['id']},sha256(convert_to({text},'UTF8')),id FROM payload;""")
                for stage, status, score in [('graph','available',str((0,3,-2,1000001)[number%4])),
                                             ('semantic','unavailable','NULL'),('rerank','failed','NULL')]:
                    self.sql(self.admin('SELECT storage_v2_put_occurrence_score_component('
                        f"{item['id']},'{stage}','input-fixture','{status}',{score})"))

        asts = [{'type':'term','value':v} for v in ('alpha','common','日本語','missing')]
        asts += [{'type':'and','children':[{'type':'term','value':'alpha'},{'type':'term','value':'beta'}]},
                 {'type':'or','children':[{'type':'term','value':'alpha'},{'type':'term','value':'gamma'}]},
                 {'type':'and','children':[{'type':'term','value':'common'},{'type':'not','children':[{'type':'term','value':'beta'}]}]},
                 {'type':'phrase','value':'alpha beta'}, {'type':'exact','value':'key_identifier'}]

        def envelopes():
            filters = self.quote(json.dumps({stage+'_profile':'input-fixture' for stage in ('graph','semantic','rerank')}))
            statements = [f"SELECT storage_v2_search_exact({source},'1',{self.quote(json.dumps(ast))}::JSONB,{filters}::JSONB,{limit});"
                          for source in (6,9) for ast in asts for limit in (1,3,10)]
            return [json.loads(v) for v in self.sql(self.actor(self.schema.ADMIN_ID,'\n'.join(statements))).splitlines()]

        before = envelopes()
        # Function/authority drift must abort before any reader is replaced.
        for signature, name, owner in signatures:
            definition = original[signature]
            for change, error in (
                (definition.replace('RETURN QUERY','/* drift */ RETURN QUERY',1)+';', 'definition'),
                (f'GRANT EXECUTE ON FUNCTION {signature} TO storage_v2_shadow_worker;', 'authority'),
                (f'REVOKE EXECUTE ON FUNCTION {signature} FROM {owner};', 'authority'),
            ):
                body = INPUTS.read_text().replace('BEGIN;','',1).rsplit('COMMIT;',1)[0]
                self.assert_sql_fails('BEGIN;'+change+body+'ROLLBACK;', 'native reader input '+error+' differs')
        self.file(INPUTS)
        self.file(STATS)
        self.assertEqual(before, envelopes())
        comparisons = 0
        scope = 'ARRAY(SELECT id FROM occurrence WHERE source_id IN (6,9))'
        for user in (self.schema.ADMIN_ID,self.schema.WRITER_ID,self.schema.OTHER_ID):
            for sources in ('ARRAY[6,9]::BIGINT[]','ARRAY[6,6,NULL]::BIGINT[]','ARRAY[1,2]::BIGINT[]','ARRAY[]::BIGINT[]'):
                for query in ('alpha','alpha beta','"alpha beta"','alpha OR gamma','日本語','missing'):
                    statement = f"""WITH actual AS (SELECT * FROM storage_v2_authorized_lexical_candidates({scope},{sources},{self.quote(query)})),
                      reference AS (SELECT * FROM fixture_reference_candidates({scope},{sources},{self.quote(query)}))
                      SELECT NOT EXISTS(SELECT * FROM actual EXCEPT ALL SELECT * FROM reference)
                       AND NOT EXISTS(SELECT * FROM reference EXCEPT ALL SELECT * FROM actual)"""
                    self.assertEqual(self.sql(f"SET ROLE mainrag_v2_frontier_owner; SET app.user_id='{user}'; "+statement),'t')
                    comparisons += 1
            for actual, reference, arguments in (
                ('storage_v2_source_segment_rank_candidates','fixture_reference_ranks',f"{scope},'alpha beta',ARRAY[6,9]::BIGINT[]"),
                ('storage_v2_source_segment_presence','fixture_reference_presence',scope),
            ):
                statement = f"""WITH a AS (SELECT * FROM {actual}({arguments})),r AS (SELECT * FROM {reference}({arguments}))
                  SELECT NOT EXISTS(SELECT * FROM a EXCEPT ALL SELECT * FROM r) AND NOT EXISTS(SELECT * FROM r EXCEPT ALL SELECT * FROM a)"""
                self.assertEqual(self.sql(f"SET ROLE mainrag_v2_frontier_owner; SET app.user_id='{user}'; "+statement),'t')
        print(f'complete native envelopes: {len(before)}; independent lexical comparisons: {comparisons}; rank/presence authorization unchanged', flush=True)

    def test_b_sparse_compact_blocks_before_broad_requested_scope(self):
        definition = self.sql("SELECT pg_get_functiondef('storage_v2_authorized_lexical_candidates(bigint[],bigint[],text)'::REGPROCEDURE)")
        for old, new in (
            ('public.storage_v2_authorized_lexical_candidates', 'reader_input_fixture.candidates'),
            ('public.storage_v2_compact_lexical_block', 'reader_input_fixture.compact'),
            ('public.storage_v2_lexical_segment', 'reader_input_fixture.ordinary'),
        ):
            definition = definition.replace(old,new)
        self.sql("""CREATE SCHEMA reader_input_fixture;
          CREATE TABLE reader_input_fixture.ordinary(occurrence_id BIGINT,source_id BIGINT,
            artifact_version_id BIGINT,segment_order BIGINT,fts_vector TSVECTOR);
          CREATE TABLE reader_input_fixture.compact(occurrence_id BIGINT PRIMARY KEY,source_id BIGINT,
            artifact_version_id BIGINT,segment_orders BIGINT[],fts_vectors TSVECTOR[],fingerprints INTEGER[]);
          CREATE INDEX fixture_compact_source ON reader_input_fixture.compact(source_id,occurrence_id);
          CREATE INDEX fixture_compact_fingerprint ON reader_input_fixture.compact USING GIN(fingerprints);
          INSERT INTO reader_input_fixture.compact SELECT n,6,n,ARRAY[0::BIGINT],
            ARRAY[to_tsvector('simple',CASE WHEN n=777 THEN 'needle' ELSE 'common' END)],
            storage_v2_posting_fingerprints(ARRAY[CASE WHEN n=777 THEN 'needle' ELSE 'common' END])
            FROM generate_series(1,130908) n;
          -- A fingerprint false positive still needs a full vector match.
          UPDATE reader_input_fixture.compact SET fingerprints=storage_v2_posting_fingerprints(ARRAY['needle']) WHERE occurrence_id=888;
          -- Union fingerprints do not establish term co-location in one segment.
          INSERT INTO reader_input_fixture.compact VALUES(200000,6,200000,ARRAY[0,1]::BIGINT[],
            ARRAY[to_tsvector('simple','alpha'),to_tsvector('simple','beta')],storage_v2_posting_fingerprints(ARRAY['alpha','beta']));
          GRANT USAGE ON SCHEMA reader_input_fixture TO mainrag_v2_lexical_rank_owner;
          GRANT USAGE ON SCHEMA reader_input_fixture TO mainrag_v2_frontier_owner;
          GRANT SELECT ON ALL TABLES IN SCHEMA reader_input_fixture TO mainrag_v2_lexical_rank_owner;
          ANALYZE reader_input_fixture.compact;
        """+definition+"; ALTER FUNCTION reader_input_fixture.candidates(BIGINT[],BIGINT[],TEXT) OWNER TO mainrag_v2_lexical_rank_owner;")
        actor = f"SET ROLE mainrag_v2_frontier_owner; SET app.user_id='{self.schema.ADMIN_ID}'; "
        result = self.profile(actor+"SELECT count(*) FROM reader_input_fixture.candidates("
            "ARRAY(SELECT n::BIGINT FROM generate_series(1,130908) n),ARRAY[6]::BIGINT[],'needle')")
        self.assertEqual(result.stdout.strip(),'1')
        plans = list(previous.plans(result.stderr))
        matching = [n for p in plans for n in previous.nodes(p['Plan']) if n.get('Subplan Name')=='CTE matching_blocks']
        self.assertEqual(len(matching),1)
        self.assertEqual(matching[0]['Actual Loops'],1)
        self.assertEqual(matching[0]['Actual Rows'],2)
        accesses = [n for p in plans for n in previous.nodes(p['Plan']) if n.get('Relation Name')=='compact']
        self.assertTrue(any(n.get('Node Type')=='Bitmap Heap Scan' for n in accesses))
        self.assertTrue(all(n['Actual Loops']==1 for n in accesses))
        for query, expected in (('alpha beta','0'),('alpha OR beta','2')):
            self.assertEqual(self.sql(actor+"SELECT count(*) FROM reader_input_fixture.candidates("
                f"ARRAY[200000]::BIGINT[],ARRAY[6]::BIGINT[],{self.quote(query)})"),expected)
        print('130,908 requested occurrences: two fingerprint candidates, one exact match, zero per-occurrence compact probes', flush=True)

    def test_c_broad_canonical_scope_and_covering_statistics(self):
        scope = 'ARRAY(SELECT n::BIGINT FROM generate_series(1,130908) n)'
        actor = f"SET ROLE mainrag_v2_frontier_owner; SET app.user_id='{self.schema.ADMIN_ID}'; "
        result = self.profile('SET enable_seqscan=off; '+actor+'SELECT count(*) FROM storage_v2_source_segment_rank_candidates('
            f"{scope},'alpha beta',ARRAY[6,9]::BIGINT[])")
        self.assertGreater(int(result.stdout.strip()),0)
        plans = list(previous.plans(result.stderr))
        canonical = [p for p in plans if 'matching_document AS MATERIALIZED' in p['Query Text']]
        self.assertEqual(len(canonical),1)
        dead_branches = [n for n in previous.nodes(canonical[0]['Plan']) if n.get('One-Time Filter')=='false']
        # Custom planning may remove the false branch altogether; if kept,
        # none of its underlying scans may execute.
        for branch in dead_branches:
            self.assertTrue(all(n.get('Actual Loops',0)==0 for n in previous.nodes(branch)
                if n.get('Relation Name')), 'the irrelevant broad branch must not scan documents')
        document_scans = [n for n in previous.nodes(canonical[0]['Plan'])
                          if n.get('Relation Name')=='storage_v2_search_document' and n.get('Actual Loops',0)>0]
        self.assertTrue(any('id = ANY' in n.get('Index Cond','') for n in document_scans),
                        'the sparse canonical set must support an indexed ID restriction')
        for user in (self.schema.ADMIN_ID,self.schema.WRITER_ID,self.schema.OTHER_ID):
            for query in ('alpha','alpha beta','"alpha beta"','alpha OR gamma','missing'):
                arguments = f"{scope},{self.quote(query)},ARRAY[6,9]::BIGINT[]"
                comparison = f"""WITH a AS (SELECT * FROM storage_v2_source_segment_rank_candidates({arguments})),
                  r AS (SELECT * FROM fixture_reference_ranks({arguments}))
                  SELECT NOT EXISTS(SELECT * FROM a EXCEPT ALL SELECT * FROM r)
                   AND NOT EXISTS(SELECT * FROM r EXCEPT ALL SELECT * FROM a)"""
                self.assertEqual(self.sql(f"SET ROLE mainrag_v2_frontier_owner; SET app.user_id='{user}'; "+comparison),'t')
        for relation in ('storage_v2_search_document','storage_v2_search_view_document'):
            self.sql(f'VACUUM {relation}')
        queries = ["SELECT id,token_count FROM storage_v2_search_document WHERE id>0",
                   "SELECT view_id,ordinal,document_id,role_weight FROM storage_v2_search_view_document WHERE view_id>0"]
        names = ['idx_storage_v2_search_document_token_stats','idx_storage_v2_search_view_document_stats']
        for query, name in zip(queries,names):
            plan = json.loads(self.sql('SET enable_seqscan=off; EXPLAIN(ANALYZE,FORMAT JSON) '+query))
            scans = [n for n in previous.nodes(plan[0]['Plan']) if n.get('Index Name')==name]
            self.assertEqual(len(scans),1)
            self.assertEqual(scans[0]['Node Type'],'Index Only Scan')
            self.assertEqual(scans[0]['Heap Fetches'],0)
        print('broad canonical scope retains matches and skips the unused branch; both token-statistics scans are index-only', flush=True)


if __name__ == '__main__':
    unittest.main()
