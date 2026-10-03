"""Complete rank-vector equivalence and bounded physical fragment storage."""
import json
import re
import unittest

from eval.storage_v2.schema import test_scoped_posting_sets as base
from eval.storage_v2.schema import test_shared_query_postings as reader

MIGRATION = base.ScopedPostingSetsTests.schema.ROOT / 'migrations/128_storage_v2_factor_legacy_rank_payloads.sql'


class FactoredLegacyRankPayloadTests(unittest.TestCase):
    schema = base.ScopedPostingSetsTests.schema
    command = classmethod(base.ScopedPostingSetsTests.command.__func__)
    sql = classmethod(base.ScopedPostingSetsTests.sql.__func__)
    file = classmethod(base.ScopedPostingSetsTests.file.__func__)
    admin = classmethod(base.ScopedPostingSetsTests.admin.__func__)
    actor = staticmethod(reader.SharedQueryPostingTests.actor)
    quote = staticmethod(base.ScopedPostingSetsTests.quote)
    make_projection = base.ScopedPostingSetsTests.make_projection
    begin = base.ScopedPostingSetsTests.begin
    stage = base.ScopedPostingSetsTests.stage
    complete_analysis = base.ScopedPostingSetsTests.complete_analysis
    commit = base.ScopedPostingSetsTests.commit
    put = base.ScopedPostingSetsTests.put
    assert_sql_fails = base.ScopedPostingSetsTests.assert_sql_fails

    @classmethod
    def setUpClass(cls):
        try:
            reader.SharedQueryPostingTests.setUpClass.__func__(cls)
            for number in range(122, 128):
                cls.file(next((cls.schema.ROOT/'migrations').glob(f'{number:03}_*.sql')))
        except BaseException:
            if hasattr(cls, 'stack'):
                cls.stack.close()
            raise

    @classmethod
    def tearDownClass(cls):
        base.ScopedPostingSetsTests.tearDownClass.__func__(cls)

    def relation_digest(self):
        return self.sql("""SELECT encode(sha256(convert_to(coalesce(string_agg(
          occurrence_id::TEXT||':'||source_id::TEXT||':'||artifact_version_id::TEXT||':'||
          legacy_chunk_id::TEXT||':'||encode(legacy_file_hash,'hex')||':'||
          encode(sha256(tsvectorsend(fts_vector)),'hex'),',' ORDER BY occurrence_id,legacy_chunk_id),
          ''),'UTF8')),'hex') FROM storage_v2_legacy_lexical_segment""")

    def test_a_complete_envelopes_vectors_and_physical_factorization(self):
        for source in (6, 9):
            run = self.begin(source, f'{source:02x}'*32, f'{source+20:02x}'*32)
            texts = ['alpha beta common key_identifier', 'alpha common beta',
                     'common beta', 'alpha beta', 'ÄÖÜ 日本語 common', '']
            for number, text in enumerate(texts):
                node, view, digest = self.make_projection(text)
                self.stage(run, f'factor-{source}-{number}.txt', text, node, view, digest)
                self.complete_analysis(digest)
                document = self.put(node, text)
                self.sql(self.admin(f'SELECT storage_v2_bind_search_document({view},0,{document},1.0)'))
            self.commit(run, len(texts))
        # Many associations share one large weighted vector in each source.
        # Source separation remains explicit even when the vector is identical.
        self.sql("""WITH vector AS MATERIALIZED (
          SELECT setweight(to_tsvector('simple','alpha beta common ÄÖÜ 日本語'), 'A') ||
            setweight(to_tsvector('simple',(SELECT string_agg('factor_token_'||n,' ')
              FROM generate_series(1,1600) n)), 'B') AS value)
          INSERT INTO storage_v2_legacy_lexical_segment
          (occurrence_id,source_id,artifact_version_id,legacy_chunk_id,legacy_file_hash,fts_vector)
          SELECT o.id,o.source_id,o.artifact_version_id,100000+repeat.number,
            sha256(convert_to(o.source_path,'UTF8')),
            vector.value
          FROM occurrence o CROSS JOIN generate_series(1,512) repeat(number) CROSS JOIN vector
          WHERE o.source_id IN (6,9)""")

        asts = [{'type':'term','value':value} for value in ['common','alpha','日本語','never_present']]
        asts += [{'type':'phrase','value':'alpha beta'},
                 {'type':'and','children':[{'type':'term','value':'common'},{'type':'term','value':'beta'}]},
                 {'type':'or','children':[{'type':'term','value':'alpha'},{'type':'term','value':'日本語'}]}]

        def searches():
            queries = [f"SELECT storage_v2_search_exact({source},'1',"
                       f"{self.quote(json.dumps(ast))}::JSONB,'{{}}'::JSONB,{limit});"
                       for source in (6,9) for ast in asts for limit in (1,3,10)]
            return [json.loads(line) for line in self.sql(
                self.actor(self.schema.ADMIN_ID,'\n'.join(queries))).splitlines()]

        before = searches()
        digest = self.relation_digest()
        old_bytes = int(self.sql("SELECT pg_total_relation_size('storage_v2_legacy_lexical_segment')"))
        definition = self.sql("SELECT pg_get_functiondef('storage_v2_materialize_legacy_chunk_ranks(bigint,bigint)'::REGPROCEDURE)")
        self.sql(definition.replace('DECLARE','/* unexpected drift */ DECLARE',1)+';')
        self.assert_sql_fails(MIGRATION.read_text(), 'unexpected preceding')
        self.sql(definition+';')
        self.assertEqual(digest,self.relation_digest())
        body = MIGRATION.read_text().replace('BEGIN;','',1).rsplit('COMMIT;',1)[0]
        self.assert_sql_fails('BEGIN; SET ROLE mainrag; '+body+' ROLLBACK;', 'administrator')
        # Fail after the physical conversion has started. All DDL and writes
        # must roll back to the original relation, including its full vectors.
        corrupt = MIGRATION.read_text().replace(
            '-- Equality covers',
            'ALTER TABLE storage_v2_legacy_rank_binding DISABLE TRIGGER USER;\n'
            'DELETE FROM storage_v2_legacy_rank_binding WHERE occurrence_id='
            '(SELECT min(occurrence_id) FROM storage_v2_legacy_rank_binding);\n'
            '-- Equality covers', 1)
        self.assert_sql_fails(corrupt, 'not the complete original relation')
        self.assertEqual(self.sql("SELECT relkind FROM pg_class WHERE "
                                 "oid='storage_v2_legacy_lexical_segment'::REGCLASS"), 'r')
        self.assertEqual(digest,self.relation_digest())
        self.assertEqual(self.sql("SELECT to_regclass('storage_v2_legacy_rank_binding') IS NULL"), 't')
        self.file(MIGRATION)
        self.assertEqual(digest,self.relation_digest())
        self.assertEqual(before,searches())
        result = self.command('--command', "LOAD 'auto_explain'; "
            "SET auto_explain.log_min_duration=0; SET auto_explain.log_nested_statements=on; "
            "SET auto_explain.log_analyze=on; SET auto_explain.log_format=json; "
            "SET auto_explain.log_level=notice; SET enable_seqscan=off; "
            + self.actor(self.schema.ADMIN_ID,
                "SELECT count(*) FROM storage_v2_source_segment_rank_candidates("
                "ARRAY(SELECT id FROM occurrence WHERE source_id=6),'common',ARRAY[6]::BIGINT[]);"))
        self.assertEqual(result.stdout.strip(), '4')
        decoder = json.JSONDecoder()
        plans = [decoder.raw_decode(result.stderr[match.start():])[0]
                 for match in re.finditer(r'\{\s*"Query Text"', result.stderr)]
        def nodes(node):
            yield node
            for child in node.get('Plans', []):
                yield from nodes(child)
        payload_nodes = [node for plan in plans for node in nodes(plan['Plan'])
                         if node.get('Subplan Name') == 'CTE ranked_payload']
        self.assertEqual(len(payload_nodes), 1)
        self.assertEqual(payload_nodes[0]['Actual Rows'], 1)
        self.assertEqual(payload_nodes[0]['Actual Loops'], 1)
        gin_nodes = [node for plan in plans for node in nodes(plan['Plan'])
                     if node.get('Index Name') == 'idx_storage_v2_legacy_lexical_segment_fts']
        # With only two dictionary rows a source-key B-tree probe can be
        # cheaper than GIN. Do not require a worse tiny-fixture plan.
        for node in gin_nodes:
            self.assertIn('source_id', node.get('Index Cond',''))
            self.assertIn('fts_vector', node.get('Index Cond',''))
        index_definition = self.sql("SELECT pg_get_indexdef("
            "'idx_storage_v2_legacy_lexical_segment_fts'::REGCLASS)")
        self.assertIn('storage_v2_legacy_rank_payload USING gin (source_id, fts_vector)',
                      index_definition)
        self.assertEqual(self.sql('SELECT count(*) FROM storage_v2_legacy_rank_binding'),'6144')
        self.assertEqual(self.sql('SELECT count(*) FROM storage_v2_legacy_rank_payload'),'2')
        new_bytes = int(self.sql("SELECT pg_total_relation_size('storage_v2_legacy_rank_binding')"
                                "+pg_total_relation_size('storage_v2_legacy_rank_payload')"))
        self.assertLess(new_bytes,old_bytes//3)
        self.assertEqual(self.sql(self.actor(self.schema.WRITER_ID,
            'SELECT string_agg(DISTINCT source_id::TEXT,\',\') FROM storage_v2_legacy_lexical_segment')), '6')
        for relation in ('storage_v2_legacy_rank_payload','storage_v2_legacy_rank_binding'):
            self.assert_sql_fails(self.admin(f'DELETE FROM {relation}'), 'permission denied')
            self.assert_sql_fails(f'UPDATE {relation} SET source_id=source_id', 'immutable')
        self.assert_sql_fails(self.admin('SELECT storage_v2_lock_legacy_rank_snapshot(6,1,decode(\'aa\',\'hex\'))'),
                              'permission denied')
        self.assert_sql_fails(MIGRATION.read_text(),'already installed')
        self.assertEqual(digest,self.relation_digest())
        print(f'complete envelopes: {len(before)}; associations: 6144; payloads: 2; '
              f'physical bytes before={old_bytes} after={new_bytes}',flush=True)

    def test_b_fragment_materializer_replay_uses_one_payload_per_vector(self):
        if self.sql("SELECT relkind FROM pg_class WHERE oid='storage_v2_legacy_lexical_segment'::REGCLASS") == 'r':
            self.file(MIGRATION)
        content = 'alpha beta gamma'
        node, view, digest = self.make_projection('alpha')
        document = self.put(node, 'alpha')
        self.sql(self.admin(f'SELECT storage_v2_bind_search_document({view},0,{document},1.0)'))
        self.sql("""INSERT INTO files(source_id,path,hash,content,content_text,size_original,
          size_compressed,last_modified) VALUES (1,'/synthetic/factor-origin.txt',
          sha256(convert_to('alpha beta gamma','UTF8')),decode('00','hex'),'alpha beta gamma',16,1,now());
          INSERT INTO chunks(file_id,chunk_type,content_hash,content_compressed,content_text,start_line,end_line)
          SELECT f.id,'text',sha256(convert_to(value,'UTF8')),decode('00','hex'),value,1,1
          FROM files f CROSS JOIN unnest(ARRAY['alpha','beta','gamma']) value
          WHERE f.source_id=1 AND f.path='/synthetic/factor-origin.txt';""")
        run = self.begin(1,'f3'*32,'f4'*32)
        self.sql(self.admin(f"""DO $$ DECLARE item RECORD; n INTEGER; BEGIN
          FOR n IN 1..100 LOOP
            SELECT * INTO item FROM storage_v2_stage_shadow_item({run},'factor-fragment-'||n,
              'document','synthetic-item','{{"fixture":true}}'::JSONB,'fixture-adapter-v1',
              {node},NULL,'{digest}',5,decode('{digest}','hex'),'fixture-analysis-v1',{view},
              '/synthetic/factor-origin.txt',jsonb_build_object('fragment',n));
            IF storage_v2_materialize_legacy_chunk_ranks(item.occurrence_id,item.artifact_version_id)<>3
              OR storage_v2_materialize_legacy_chunk_ranks(item.occurrence_id,item.artifact_version_id)<>3 THEN
              RAISE EXCEPTION 'complete stable fragment projection required';
            END IF;
          END LOOP;
          END $$;"""))
        self.assertEqual(self.sql('SELECT count(*) FROM storage_v2_legacy_rank_binding WHERE source_id=1'),'300')
        self.assertEqual(self.sql('SELECT count(*) FROM storage_v2_legacy_rank_payload WHERE source_id=1'),'3')
        self.assertEqual(self.sql("""SELECT count(*) FROM storage_v2_legacy_lexical_segment p
          JOIN chunks c ON c.id=p.legacy_chunk_id WHERE p.source_id=1 AND p.fts_vector=c.fts_vector"""),'300')
        print('100 native fragments: 300 complete associations, 3 stored vectors; replay preserved',flush=True)


if __name__ == '__main__':
    unittest.main()
