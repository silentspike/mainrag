"""Synthetic search regression: full result identity, ties, ACLs and replay."""
import json
import unittest

from eval.storage_v2.schema import test_compact_exact_postings as compact
from eval.storage_v2.schema import test_shadow_ingest_schema as schema

MIGRATION = schema.ROOT / 'migrations/106_storage_v2_bound_native_ranks_and_fragment_groups.sql'


class BoundNativeRankTests(unittest.TestCase):
    schema = schema
    command = classmethod(compact.CompactExactPostingTests.command.__func__)
    sql = classmethod(schema.ShadowIngestSchemaTests.sql.__func__)
    file = classmethod(schema.ShadowIngestSchemaTests.file.__func__)
    admin = classmethod(schema.ShadowIngestSchemaTests.admin.__func__)
    actor = staticmethod(schema.ShadowIngestSchemaTests.actor)
    make_projection = compact.CompactExactPostingTests.make_projection
    begin = compact.CompactExactPostingTests.begin
    stage = compact.CompactExactPostingTests.stage
    complete_analysis = compact.CompactExactPostingTests.complete_analysis
    commit = compact.CompactExactPostingTests.commit
    put = compact.CompactExactPostingTests.put
    quote = staticmethod(compact.CompactExactPostingTests.quote)
    assert_sql_fails = schema.ShadowIngestSchemaTests.assert_sql_fails

    @classmethod
    def setUpClass(cls):
        compact.CompactExactPostingTests.setUpClass.__func__(cls)
        for number in range(99, 106):
            cls.file(next((schema.ROOT / 'migrations').glob(f'{number:03}_*.sql')))

    @classmethod
    def tearDownClass(cls):
        compact.CompactExactPostingTests.tearDownClass.__func__(cls)

    def test_complete_results_authorization_ties_and_replay(self):
        texts = ['alpha beta alpha common key_identifier', 'alpha beta common',
                 'common gamma', 'alpha common', 'common alpha',
                 'common beta', 'alpha beta common', '',
                 'common ' + ' '.join(f'token_{i}' for i in range(700))]
        run = self.begin(6, 'a1' * 32, 'a2' * 32)
        for i, text in enumerate(texts):
            node, view, digest = self.make_projection(text)
            self.stage(run, f'bounded-{i}.txt', text, node, view, digest)
            self.complete_analysis(digest)
            document = self.put(node, text)
            self.sql(self.admin(f'SELECT storage_v2_bind_search_document({view},0,{document},1.0)'))
        self.commit(run, len(texts))
        rows = json.loads(self.sql("SELECT jsonb_agg(jsonb_build_object('id',o.id,"
            "'artifact',o.artifact_version_id,'text',d.search_text) ORDER BY o.id) "
            "FROM occurrence o JOIN storage_v2_search_view_document b ON b.view_id=o.view_id "
            "AND b.ordinal=0 JOIN storage_v2_search_document d ON d.id=b.document_id "
            "WHERE o.source_id=6"))
        writer = f"SET ROLE mainrag; SET app.user_id='{schema.ADMIN_ID}'; "
        for i, row in enumerate(rows):
            if row['text']:
                # Segment zero marks generated provenance. Multiple segments
                # deliberately give exact segment ranks different tie orders.
                content = self.quote(row['text'])
                if i % 2:
                    self.sql(writer + 'SELECT storage_v2_put_lexical_segments_located('
                        f"{row['id']},{row['artifact']},ARRAY[0,{i+1}]::BIGINT[],"
                        f"ARRAY[{content},{content}],ARRAY['',''],ARRAY['text','text'],"
                        'ARRAY[1,1]::BIGINT[],ARRAY[1,1]::BIGINT[])')
                else:
                    self.sql(writer + 'SELECT storage_v2_put_lexical_segment('
                        f"{row['id']},{row['artifact']},0,{content},'','text')")
            if i < 2:
                self.sql('INSERT INTO storage_v2_legacy_lexical_segment '
                    '(occurrence_id,source_id,artifact_version_id,legacy_chunk_id,legacy_file_hash,fts_vector) '
                    f"VALUES ({row['id']},6,{row['artifact']},{10000+row['id']},"
                    f"digest({self.quote(row['text'])},'sha256'),to_tsvector('simple',{self.quote(row['text'])}))")
            for stage, status, score in [('graph', 'available', str((0, 3, -2, 1000001)[i % 4])),
                                         ('semantic', 'unavailable', 'NULL'),
                                         ('rerank', 'failed', 'NULL')]:
                self.sql(self.admin('SELECT storage_v2_put_occurrence_score_component('
                    f"{row['id']},'{stage}','bounded-fixture','{status}',{score})"))

        # Preserve all reader guards. Build empty active generations for the
        # remaining synthetic sources and load an internally consistent receipt.
        # This tests the active reader, not the separately tested activation API.
        fragment_policy = True
        if fragment_policy:
            fragmented_run = self.begin(9, 'f1'*32, 'f2'*32)
            for i in range(15):
                fragmented = i < 12
                text = 'alpha beta alpha' if fragmented else 'alpha beta'
                node, view, digest = self.make_projection(text)
                document = self.put(node, text)
                self.sql(self.admin(f'SELECT storage_v2_bind_search_document({view},0,{document},1.0)'))
                path = ('/synthetic/shared.txt' if i < 6 or i in (12,13) else
                        '/synthetic/second.txt' if i < 12 else '/synthetic/third.txt')
                locator = self.quote(json.dumps({'byte_start':i*32,'fragmented':fragmented}))
                self.sql(self.admin('SELECT (storage_v2_stage_shadow_item('
                    f"{fragmented_run},'fragment-fixture-{i}','document','synthetic-item',"
                    f"'{{}}'::jsonb,'fixture-adapter-v1',{node},NULL,'{digest}',"
                    f"octet_length({self.quote(text)}),decode('{digest}','hex'),"
                    f"'fixture-analysis-v1',{view},{self.quote(path)},{locator}::jsonb)).source_item_id"))
                self.complete_analysis(digest)
            self.commit(fragmented_run, 15)
            fragment_rows = json.loads(self.sql("SELECT jsonb_agg(jsonb_build_object('id',o.id,"
                "'artifact',o.artifact_version_id,'text',d.search_text) ORDER BY o.id) "
                "FROM occurrence o JOIN storage_v2_search_view_document b ON b.view_id=o.view_id "
                "AND b.ordinal=0 JOIN storage_v2_search_document d ON d.id=b.document_id WHERE o.source_id=9"))
            for row in fragment_rows:
                self.sql(writer + 'SELECT storage_v2_put_lexical_segments_located('
                    f"{row['id']},{row['artifact']},ARRAY[0]::BIGINT[],ARRAY[{self.quote(row['text'])}],"
                    "ARRAY[''],ARRAY['text'],ARRAY[1]::BIGINT[],ARRAY[1]::BIGINT[])")
            # Independent logical records sharing a path remain separate even
            # with the same locator flag; grouping is specific to artifacts.
            self.sql("INSERT INTO occurrence(source_id,artifact_version_id,view_id,role,ordinal,source_path,locator) "
                "SELECT source_id,artifact_version_id,view_id,'conversation',1,source_path,locator "
                "FROM occurrence WHERE source_id=9 ORDER BY id LIMIT 2")
        source_ids = json.loads(self.sql('SELECT jsonb_agg(id ORDER BY id) FROM sources'))
        for source in source_ids:
            if source not in ((6,9) if fragment_policy else (6,)):
                empty = self.begin(source, f'{source+100:064x}', f'{source+200:064x}')
                self.commit(empty, 0)
        self.sql("""
ALTER TABLE source_generation DISABLE TRIGGER USER;
ALTER TABLE logical_source DISABLE TRIGGER USER;
UPDATE source_generation SET status='active',activated_at=now(),verified_at=now(),
 verification_manifest_sha256=repeat('c',64);
UPDATE logical_source pointer SET active_generation_id=g.id
 FROM source_generation g WHERE g.source_id=pointer.id;
ALTER TABLE source_generation ENABLE TRIGGER USER;
ALTER TABLE logical_source ENABLE TRIGGER USER;
ALTER TABLE storage_v2_activation_set_evidence DISABLE TRIGGER USER;
INSERT INTO storage_v2_activation_set_evidence
 (id,manifest_sha256,source_count,pointer_set_sha256,source_classification_sha256)
 SELECT '00000000-0000-0000-0000-000000000106',repeat('a',64),count(*),repeat('b',64),
 encode(digest(convert_to(jsonb_agg(jsonb_build_object('source_id',id,'is_test',is_test)
 ORDER BY id)::text,'UTF8'),'sha256'),'hex') FROM sources;
ALTER TABLE storage_v2_activation_set_evidence ENABLE TRIGGER USER;
""")
        term = lambda value: {'type': 'term', 'value': value}
        asts = [term('alpha'), term('common'), term('absent'),
                {'type': 'and', 'children': [term('alpha'), term('beta')]},
                {'type': 'or', 'children': [term('alpha'), term('gamma')]},
                {'type': 'and', 'children': [term('common'),
                    {'type': 'not', 'children': [term('gamma')]}]},
                {'type': 'phrase', 'value': 'alpha beta'},
                {'type': 'exact', 'value': 'key_identifier'},
                {'type': 'and', 'children': [term('common'),
                    {'type': 'or', 'children': [term('gamma'), term('alpha')]}]}]
        filters = [{}, {'path_prefix': '/synthetic/bounded-0'}, {'role': 'artifact'},
                   {f'{stage}_profile': 'bounded-fixture'
                    for stage in ('graph','semantic','rerank')}]
        def searches():
            found=[]
            # Several return limits exercise full boundary ties. JSON includes
            # every ID, content, score, stage, explanation and successor mapping.
            for user in (schema.ADMIN_ID, schema.WRITER_ID):
                statements=[]
                for ast in asts:
                    for filter_value in filters:
                        for limit in (1,3,10):
                            a, f = self.quote(json.dumps(ast)), self.quote(json.dumps(filter_value))
                            for func, args in [
                                ('storage_v2_search_exact', f"6,'1',{a}::jsonb,{f}::jsonb,{limit}"),
                                ('storage_v2_search_active_unchecked', f"'{ 'a'*64 }',{a}::jsonb,{f}::jsonb,{limit},6,false")]:
                                statements.append(f'SELECT {func}({args});')
                found.extend(json.loads(line) for line in self.sql(
                    self.actor(user, '\n'.join(statements))).splitlines())
            return found
        before = searches()
        fragment_references=[]
        if fragment_policy:
            for ast in (term('alpha'), {'type':'and','children':[term('alpha'),term('beta')]}):
                for active in (False,True):
                    a=self.quote(json.dumps(ast))
                    call = (f"storage_v2_search_active_unchecked('{'a'*64}',{a}::jsonb,'{{}}',1000,9,false)"
                            if active else f"storage_v2_search_exact(9,'1',{a}::jsonb,'{{}}',1000)")
                    exhaustive=json.loads(self.sql(self.admin('SELECT '+call)))
                    grouped=[];seen=set()
                    for row in exhaustive['results']:
                        if row['role']=='artifact' and row['locator'].get('fragmented') is True:
                            key=(row['source_id'],row['source_path'])
                            if key in seen:continue
                            seen.add(key)
                        grouped.append(row)
                    self.assertEqual(len(grouped),7)
                    for limit in (1,3,10):
                        expected={**exhaustive,'results':grouped[:limit]}
                        fragment_references.append((call.replace(',1000',f',{limit}'),expected))
        identity_sql = ('SELECT md5(jsonb_agg(to_jsonb(o) ORDER BY id)::text) FROM occurrence o; '
            'SELECT md5(jsonb_agg(to_jsonb(g) ORDER BY id)::text) FROM source_generation g; '
            'SELECT md5(jsonb_agg(to_jsonb(p) ORDER BY id)::text) FROM logical_source p;')
        identities = self.sql(identity_sql)
        self.file(MIGRATION)
        self.file(MIGRATION)
        self.assertEqual(identities, self.sql(identity_sql))
        self.assertEqual(before, searches())
        for call, expected in fragment_references:
            self.assertEqual(expected,json.loads(self.sql(self.admin('SELECT '+call))))
        # Restricted helper execution and per-source authorization stay intact.
        helper = 'storage_v2_authorized_lexical_candidates(bigint[],bigint[],text)'
        self.assertEqual(self.sql(f"SELECT has_function_privilege('mainrag','{helper}','EXECUTE')"), 'f')
        helper = 'storage_v2_source_segment_rank_candidates(bigint[],text)'
        self.assertEqual(self.sql(f"SELECT has_function_privilege('mainrag','{helper}','EXECUTE')"), 't')
        occurrence_ids = 'ARRAY[' + ','.join(str(row['id']) for row in rows) + ']::BIGINT[]'
        self.assertEqual(self.sql(f"SET ROLE mainrag; SET app.user_id='{schema.OTHER_ID}'; "
            "SELECT count(*) FROM storage_v2_source_segment_rank_candidates("
            f"{occurrence_ids},'alpha')"), '0')
        # Broad arrays use the canonical document verification branch and must
        # not admit duplicate, null or unauthorized occurrence identities.
        prefix = f"SET ROLE mainrag_v2_frontier_owner; SET app.user_id='{schema.ADMIN_ID}'; "
        self.assertEqual(self.sql(prefix + 'SELECT count(*) FROM storage_v2_source_segment_rank_candidates('
            "ARRAY(SELECT o.id FROM occurrence o CROSS JOIN generate_series(1,1200) WHERE source_id=6),"
            "'alpha')"), self.sql(prefix + 'SELECT count(*) FROM storage_v2_source_segment_rank_candidates('
            "ARRAY(SELECT id FROM occurrence WHERE source_id=6),'alpha')"))
        # Mutation of the successor must fail replay; a marker is not sufficient.
        candidate = self.sql("SELECT pg_get_functiondef('storage_v2_source_segment_rank_candidates(bigint[],text)'::regprocedure)")
        altered = candidate.replace('RETURN QUERY', '/* fixture drift */ RETURN QUERY', 1)
        replay = self.command('--command', 'BEGIN; ' + altered + '; ' +
            MIGRATION.read_text().replace('BEGIN;', '', 1).rsplit('COMMIT;',1)[0] + 'ROLLBACK;', check=False)
        self.assertNotEqual(replay.returncode, 0)
        self.assertIn('candidate helper identity differs', replay.stderr)
        # Replaying a body-identical helper with an unexpected executor or
        # owner must fail before resetting grants or changing any reader.
        migration_body = MIGRATION.read_text().replace('BEGIN;', '', 1).rsplit('COMMIT;',1)[0]
        for change in (
            'GRANT EXECUTE ON FUNCTION storage_v2_source_segment_rank_candidates(bigint[],text) TO storage_v2_shadow_worker;',
            'ALTER FUNCTION storage_v2_source_segment_rank_candidates(bigint[],text) OWNER TO mainrag;'):
            drift = self.command('--command','BEGIN; '+change+migration_body+'ROLLBACK;',check=False)
            self.assertNotEqual(drift.returncode,0)
            self.assertIn('candidate helper authority differs',drift.stderr)
        if fragment_policy:
            drift = self.command('--command', 'BEGIN; DROP INDEX idx_storage_v2_fragmented_artifact_occurrence; '
                "CREATE INDEX idx_storage_v2_fragmented_artifact_occurrence ON occurrence(id) WHERE role='artifact'; "
                +migration_body+'ROLLBACK;',check=False)
            self.assertNotEqual(drift.returncode,0)
            self.assertIn('fragmented artifact occurrence index identity differs',drift.stderr)
        print('complete reader results compared:', len(before), flush=True)
        print('exhaustive grouped fragment results compared:',len(fragment_references),flush=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
