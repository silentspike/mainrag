"""Native snapshot reuse, mutation invalidation and payload-driven rank expansion."""
import json
import re
import unittest

from eval.storage_v2.schema import test_factored_legacy_rank_payloads as previous

ROOT = previous.MIGRATION.parent.parent
CACHE = ROOT / 'migrations/129_storage_v2_cache_legacy_rank_snapshots.sql'
INDEX = ROOT / 'migrations/130_storage_v2_index_rank_payload_bindings.sql'


def nodes(node):
    yield node
    for child in node.get('Plans', []):
        yield from nodes(child)


def plans(log):
    decoder = json.JSONDecoder()
    for match in re.finditer(r'\{\s*"Query Text"', log):
        yield decoder.raw_decode(log[match.start():])[0]


class CachedLegacyRankSnapshotTests(unittest.TestCase):
    schema = previous.FactoredLegacyRankPayloadTests.schema
    command = classmethod(previous.FactoredLegacyRankPayloadTests.command.__func__)
    sql = classmethod(previous.FactoredLegacyRankPayloadTests.sql.__func__)
    file = classmethod(previous.FactoredLegacyRankPayloadTests.file.__func__)
    admin = classmethod(previous.FactoredLegacyRankPayloadTests.admin.__func__)
    actor = staticmethod(previous.FactoredLegacyRankPayloadTests.actor)
    quote = staticmethod(previous.FactoredLegacyRankPayloadTests.quote)
    make_projection = previous.FactoredLegacyRankPayloadTests.make_projection
    begin = previous.FactoredLegacyRankPayloadTests.begin
    stage = previous.FactoredLegacyRankPayloadTests.stage
    complete_analysis = previous.FactoredLegacyRankPayloadTests.complete_analysis
    commit = previous.FactoredLegacyRankPayloadTests.commit
    put = previous.FactoredLegacyRankPayloadTests.put
    assert_sql_fails = previous.FactoredLegacyRankPayloadTests.assert_sql_fails

    @classmethod
    def setUpClass(cls):
        try:
            previous.FactoredLegacyRankPayloadTests.setUpClass.__func__(cls)
            cls.file(previous.MIGRATION)
        except BaseException:
            if hasattr(cls, 'stack'):
                cls.stack.close()
            raise

    @classmethod
    def tearDownClass(cls):
        previous.FactoredLegacyRankPayloadTests.tearDownClass.__func__(cls)

    def install(self):
        if self.sql("SELECT to_regclass('storage_v2_legacy_rank_snapshot') IS NOT NULL") == 'f':
            self.file(CACHE)

    def test_a_complete_reuse_and_all_legacy_mutations(self):
        self.install()
        node, view, digest = self.make_projection('alpha')
        document = self.put(node, 'alpha')
        self.sql(self.admin(f'SELECT storage_v2_bind_search_document({view},0,{document},1.0)'))
        self.sql("""INSERT INTO files(source_id,path,hash,content,content_text,size_original,
          size_compressed,last_modified) VALUES (1,'/synthetic/cache-origin.txt',
          sha256(convert_to('alpha beta gamma','UTF8')),decode('00','hex'),'alpha beta gamma',16,1,now());
          INSERT INTO chunks(file_id,chunk_type,content_hash,content_compressed,content_text,start_line,end_line)
          SELECT f.id,'text',sha256(convert_to(value,'UTF8')),decode('00','hex'),value,1,1
          FROM files f CROSS JOIN unnest(ARRAY['alpha','beta','gamma']) value
          WHERE f.source_id=1 AND f.path='/synthetic/cache-origin.txt';""")
        file_id = int(self.sql("SELECT id FROM files WHERE path='/synthetic/cache-origin.txt'"))
        file_hash = self.sql(f"SELECT encode(hash,'hex') FROM files WHERE id={file_id}")
        run = self.begin(1, 'e1'*32, 'e2'*32)

        def fragments(first, last, count):
            return self.admin(f"""DO $$ DECLARE item RECORD; n INTEGER; BEGIN
              FOR n IN {first}..{last} LOOP
                SELECT * INTO item FROM storage_v2_stage_shadow_item({run},'cache-fragment-'||n,
                  'document','synthetic-item','{{"fixture":true}}'::JSONB,'fixture-adapter-v1',
                  {node},NULL,'{digest}',5,decode('{digest}','hex'),'fixture-analysis-v1',{view},
                  '/synthetic/cache-origin.txt',jsonb_build_object('fragment',n));
                IF storage_v2_materialize_legacy_chunk_ranks(item.occurrence_id,item.artifact_version_id)<>{count}
                  OR storage_v2_materialize_legacy_chunk_ranks(item.occurrence_id,item.artifact_version_id)<>{count}
                  THEN RAISE EXCEPTION 'incomplete fragment snapshot'; END IF;
              END LOOP;
            END $$;""")

        self.sql(fragments(1, 1, 3))
        old_digest = self.sql("SELECT md5(jsonb_agg(to_jsonb(p) ORDER BY legacy_chunk_id)::TEXT) "
                             "FROM storage_v2_legacy_lexical_segment p WHERE occurrence_id="
                             "(SELECT min(id) FROM occurrence WHERE source_id=1)")
        profiled = self.command('--command', "LOAD 'auto_explain'; SET auto_explain.log_min_duration=0; "
            "SET auto_explain.log_analyze=on; SET auto_explain.log_nested_statements=on; "
            "SET auto_explain.log_format=json; SET auto_explain.log_level=notice; "
            + fragments(2, 101, 3))
        executed = list(plans(profiled.stderr))
        self.assertTrue(executed, 'nested execution plans must actually be captured')
        chunk_scans = [n for p in executed for n in nodes(p['Plan'])
                       if n.get('Relation Name') == 'chunks' and n.get('Actual Loops', 0)>0]
        self.assertEqual(chunk_scans, [], 'warm fragments must not reread or rehash legacy vectors')
        self.assertEqual(self.sql('SELECT count(*) FROM storage_v2_legacy_rank_snapshot'), '1')
        self.assertEqual(self.sql('SELECT count(*) FROM storage_v2_legacy_rank_snapshot_chunk'), '3')
        self.assertEqual(self.sql('SELECT count(*) FROM storage_v2_legacy_rank_binding WHERE source_id=1'), '303')

        # All three chunks in one UPDATE increment the file once. Keep the
        # whole-file hash unchanged to expose otherwise invisible invalidation.
        before = int(self.sql(f'SELECT revision FROM storage_v2_legacy_rank_revision WHERE file_id={file_id}'))
        self.sql(f"UPDATE chunks SET content_text='changed' WHERE file_id={file_id}")
        after = int(self.sql(f'SELECT revision FROM storage_v2_legacy_rank_revision WHERE file_id={file_id}'))
        self.assertEqual(after, before+1)
        self.sql(fragments(102, 102, 3))
        self.assertEqual(self.sql('SELECT count(*) FROM storage_v2_legacy_rank_snapshot'), '2')
        self.assertEqual(old_digest, self.sql("SELECT md5(jsonb_agg(to_jsonb(p) ORDER BY legacy_chunk_id)::TEXT) "
            "FROM storage_v2_legacy_lexical_segment p WHERE occurrence_id="
            "(SELECT min(id) FROM occurrence WHERE source_id=1)"))
        self.sql(f"""INSERT INTO chunks(file_id,chunk_type,content_hash,content_compressed,
          content_text,start_line,end_line) VALUES ({file_id},'text',sha256(convert_to('delta','UTF8')),
          decode('00','hex'),'delta',1,1);""")
        self.sql(fragments(103, 103, 4))
        self.sql(f"DELETE FROM chunks WHERE file_id={file_id} AND content_text='delta'")
        self.sql(fragments(104, 104, 3))
        self.sql(f"UPDATE files SET hash=sha256(convert_to('new-file-hash','UTF8')) WHERE id={file_id}")
        self.sql(fragments(105, 105, 3))
        self.assertEqual(self.sql('SELECT count(*) FROM storage_v2_legacy_rank_snapshot'), '5')
        # The old hash cannot retrieve a snapshot even when its cache exists.
        self.assert_sql_fails(self.admin('SET ROLE mainrag_v2_frontier_owner; '
            f"SELECT storage_v2_get_legacy_rank_snapshot(1,{file_id},decode('{file_hash}','hex'))"),
            'identity changed')

        current_hash = self.sql(f"SELECT encode(hash,'hex') FROM files WHERE id={file_id}")
        # TRUNCATE invalidates even when no old rows remain to bump a file ID.
        result = self.sql('BEGIN; TRUNCATE chunks CASCADE; '+self.admin(
            'SET LOCAL ROLE mainrag_v2_frontier_owner; '
            f"DO $$ DECLARE snapshot_id BIGINT; BEGIN snapshot_id:="
            f"storage_v2_get_legacy_rank_snapshot(1,{file_id},decode('{current_hash}','hex'));"
            "IF (SELECT chunk_count FROM storage_v2_legacy_rank_snapshot WHERE id=snapshot_id)<>0 "
            "THEN RAISE EXCEPTION 'truncated legacy snapshot must be empty'; END IF; END $$; "
            "SELECT 0;")+'ROLLBACK;')
        self.assertEqual(result, '0')
        self.assertEqual(self.sql('SELECT count(*) FROM storage_v2_legacy_rank_snapshot'), '5')
        # Revision rows cover both the old and new file when a chunk moves.
        self.sql("INSERT INTO files(source_id,path,hash,content,content_text,size_original,"
                 "size_compressed,last_modified) VALUES (1,'/synthetic/cache-target.txt',"
                 "sha256(convert_to('alpha','UTF8')),decode('00','hex'),'alpha',5,1,now())")
        target = int(self.sql("SELECT id FROM files WHERE path='/synthetic/cache-target.txt'"))
        before = int(self.sql(f'SELECT revision FROM storage_v2_legacy_rank_revision WHERE file_id={file_id}'))
        self.sql(f'UPDATE chunks SET file_id={target} WHERE id=(SELECT min(id) FROM chunks WHERE file_id={file_id})')
        self.assertEqual(self.sql(f'SELECT revision FROM storage_v2_legacy_rank_revision WHERE file_id={file_id}'), str(before+1))
        self.assertEqual(self.sql(f'SELECT revision FROM storage_v2_legacy_rank_revision WHERE file_id={target}'), '2')
        for relation in ('storage_v2_legacy_rank_snapshot', 'storage_v2_legacy_rank_snapshot_chunk'):
            self.assert_sql_fails(self.admin(f'DELETE FROM {relation}'), 'permission denied')
            self.assert_sql_fails(f'UPDATE {relation} SET source_id=source_id', 'immutable')
        self.assert_sql_fails(self.admin(f"SELECT storage_v2_get_legacy_rank_snapshot(1,{file_id},decode('{current_hash}','hex'))"), 'permission denied')
        self.assertEqual(self.sql(self.actor(self.schema.OTHER_ID,
            'SELECT count(*) FROM storage_v2_legacy_rank_snapshot WHERE source_id=1')), '0')
        print('100 warm native fragments, 303 complete bindings; zero executed legacy chunk scans; mutation and replay proofs passed', flush=True)

    def test_b_complete_reader_envelopes_and_sparse_payload_work(self):
        self.install()
        for source in (6, 9):
            run = self.begin(source, f'{source+60:02x}'*32, f'{source+80:02x}'*32)
            for number, text in enumerate(('alpha beta common', 'alpha common', '日本語 common', 'absent')):
                node, view, digest = self.make_projection(text)
                self.stage(run, f'cache-reader-{source}-{number}.txt', text, node, view, digest)
                self.complete_analysis(digest)
                doc = self.put(node, text)
                self.sql(self.admin(f'SELECT storage_v2_bind_search_document({view},0,{doc},1.0)'))
            self.commit(run, 4)
        self.sql("""INSERT INTO storage_v2_legacy_rank_payload(source_id,payload_sha256,fts_vector)
          SELECT source,sha256(tsvectorsend(vector)),vector FROM (SELECT source,
            to_tsvector('simple',value) vector FROM unnest(ARRAY[6,9]) source
            CROSS JOIN unnest(ARRAY['alpha beta common','日本語 common','absent']) value) v;
          INSERT INTO storage_v2_legacy_rank_binding(occurrence_id,source_id,artifact_version_id,
            legacy_chunk_id,legacy_file_hash,payload_id)
          SELECT o.id,o.source_id,o.artifact_version_id,repeat.number,
            sha256(convert_to(o.source_path,'UTF8')),p.id
          FROM occurrence o JOIN storage_v2_legacy_rank_payload p ON p.source_id=o.source_id
          CROSS JOIN generate_series(1,600) repeat(number)
          WHERE o.source_id IN (6,9) AND p.id=(SELECT min(id) FROM storage_v2_legacy_rank_payload WHERE source_id=o.source_id);
          ANALYZE storage_v2_legacy_rank_binding;""")
        asts = [{'type':'term','value':v} for v in ('alpha','日本語','absent','missing')]
        asts += [{'type':'phrase','value':'alpha beta'},
                 {'type':'and','children':[{'type':'term','value':'alpha'},{'type':'term','value':'common'}]}]

        def envelopes():
            queries = [f"SELECT storage_v2_search_exact({source},'1',{self.quote(json.dumps(ast))}::JSONB,'{{}}',10);"
                       for source in (6,9) for ast in asts]
            return [json.loads(v) for v in self.sql(self.actor(self.schema.ADMIN_ID,'\n'.join(queries))).splitlines()]

        before = envelopes()
        self.file(INDEX)
        self.assertEqual(before, envelopes())
        definition = self.sql("SELECT pg_get_indexdef('idx_storage_v2_legacy_rank_binding_payload'::REGCLASS)")
        self.assertIn('(payload_id, source_id, occurrence_id) INCLUDE (artifact_version_id, legacy_chunk_id)', definition)
        # A separate physical-scale fixture preserves the exact production
        # join but isolates the matching-payload access path from tiny metadata.
        self.sql("""CREATE SCHEMA rank_scope_fixture;
          CREATE TABLE rank_scope_fixture.binding AS SELECT n::BIGINT occurrence_id,6::BIGINT source_id,
            n::BIGINT artifact_version_id,n::BIGINT legacy_chunk_id,n::BIGINT payload_id
            FROM generate_series(1,100000) n;
          CREATE INDEX fixture_payload ON rank_scope_fixture.binding(payload_id,source_id,occurrence_id)
            INCLUDE(artifact_version_id,legacy_chunk_id);
          ANALYZE rank_scope_fixture.binding;""")
        plan = json.loads(self.sql("""EXPLAIN (ANALYZE,FORMAT JSON)
          WITH requested AS MATERIALIZED (SELECT n::BIGINT id FROM generate_series(1,100000) n),
          ranked_payload AS MATERIALIZED (SELECT 777::BIGINT id,6::BIGINT source_id)
          SELECT binding.* FROM ranked_payload payload JOIN rank_scope_fixture.binding binding
            ON binding.payload_id=payload.id AND binding.source_id=payload.source_id
          WHERE EXISTS(SELECT 1 FROM requested WHERE requested.id=binding.occurrence_id)"""))
        scanned = [n for n in nodes(plan[0]['Plan']) if n.get('Relation Name')=='binding']
        self.assertEqual(len(scanned),1)
        self.assertEqual(scanned[0].get('Index Name'),'fixture_payload')
        self.assertEqual(scanned[0]['Actual Loops'],1)
        self.assertEqual(scanned[0]['Actual Rows'],1)
        print('12 complete native reader envelopes unchanged; one binding probe across 100,000 requested occurrences', flush=True)

    def test_c_authority_and_materializer_drift_abort_atomically(self):
        self.install()
        self.assert_sql_fails(CACHE.read_text(), 'unexpected preceding')
        self.assert_sql_fails('BEGIN; SET ROLE mainrag; '+CACHE.read_text().replace('BEGIN;','',1), 'administrator')
        self.assert_sql_fails(self.admin('SELECT storage_v2_invalidate_legacy_rank_snapshot()'), 'permission denied')
        self.assertEqual(self.sql("SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'rank_snapshot_%' AND NOT tgisinternal"), '8')
        self.assertEqual(self.sql("SELECT count(*) FROM pg_constraint WHERE conrelid IN "
            "('storage_v2_legacy_rank_snapshot'::REGCLASS,'storage_v2_legacy_rank_snapshot_chunk'::REGCLASS) "
            "AND confrelid IN ('files'::REGCLASS,'chunks'::REGCLASS)"), '0')


if __name__ == '__main__':
    unittest.main()
