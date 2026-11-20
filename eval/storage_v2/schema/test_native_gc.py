"""Real native graph sweep after the genuine atomic legacy cleanup rehearsal."""
from __future__ import annotations
import importlib.util
import json
import os
from unittest.mock import patch
from eval.storage_v2.schema import test_cleanup_apply as fixture
from eval.storage_v2.schema.presence_reader_fixture import register_presence_role_cleanup

SPEC = importlib.util.spec_from_file_location('native_gc', fixture.base.ROOT/'ops/storage-v2/native-gc.py')
G = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(G)


class NativeGcTests(fixture.CleanupApplyTests):
    test_atomic_drop_retains_native_state_and_refuses_drift = None

    def make_inverted_retention_fixture(self):
        # This cohort rehearses GC after an older legacy cutover. Import only
        # the actual relation definitions, whose reference graph is the scope
        # of this test. The named writer and publisher have their own fixtures.
        migrations = fixture.base.ROOT / 'migrations'
        for revision, relation in ((188, 'source'), (190, 'generation')):
            migration = next(migrations.glob(f'{revision}_*.sql')).read_text()
            marker = f'CREATE TABLE public.storage_v2_inverted_{relation} ('
            self.sql(marker + migration.split(marker, 1)[1].split('ALTER TABLE', 1)[0])

        node, view, digest = self.make_projection('Inverted GC fixture')
        document_id = int(self.sql(self.admin(
            f"SELECT id FROM storage_v2_put_search_document('gc-inverted-fixture','node',{node},"
            "'Inverted GC fixture',ARRAY[]::TEXT[])")))
        self.sql(self.admin(f'SELECT storage_v2_bind_search_document({view},0,{document_id},1.0)'))
        return json.loads(self.sql(f"""
CREATE TEMP TABLE fixture_inverted_claim AS
WITH sealed AS (
    SELECT DISTINCT ON (source_id) id,source_id FROM source_generation
     WHERE sealed_at IS NOT NULL ORDER BY source_id,generation_seq
), ranked AS (
    SELECT sealed.*,row_number() OVER(ORDER BY source_id) AS ordinal FROM sealed
), requests AS (
    SELECT cases.kind,ranked.id AS generation_id,ranked.source_id
      FROM (VALUES ('staged',1),('published',1),('unfinished',2)) cases(kind,ordinal)
      JOIN ranked USING(ordinal)
), artifacts AS (
    INSERT INTO artifact_version(item_id,source_id,witness_type,witness,adapter_profile_id,
                                 content_root_node_id,expected_content_hash,byte_length)
    SELECT template.item_id,request.source_id,'public-fixture-item','{{}}','gc-inverted-'||request.kind,
           {node},'{digest}',19 FROM requests request
     CROSS JOIN LATERAL (
         SELECT original.item_id FROM artifact_version original
          WHERE original.source_id=request.source_id ORDER BY original.id LIMIT 1
     ) template
    RETURNING id,source_id,adapter_profile_id
), items AS (
    INSERT INTO occurrence(source_id,artifact_version_id,view_id,role,ordinal,source_path,locator)
    SELECT source_id,id,{view},'gc-inverted-fixture',0,adapter_profile_id,'{{}}' FROM artifacts
    RETURNING id,source_id,artifact_version_id,source_path
)
SELECT request.kind,request.generation_id,item.id AS occurrence_id,item.source_id,item.artifact_version_id,
       {document_id}::BIGINT AS document_id,sha256('fixture layout'::BYTEA) AS layout_sha256,
       decode('1234567800','hex') AS payload,sha256(decode('1234567800','hex')) AS payload_sha256
  FROM items item JOIN requests request ON item.source_path='gc-inverted-'||request.kind;

-- Stage remains a root with the source now disabled. Pack-only witnesses
-- exercise the independent reference from each publication state.
INSERT INTO storage_v2_inverted_projection
SELECT source_id,document_id,layout_sha256,occurrence_id,0,payload,payload_sha256
  FROM fixture_inverted_claim WHERE kind='staged';
INSERT INTO storage_v2_inverted_binding
SELECT occurrence_id,0,source_id,artifact_version_id,document_id,layout_sha256,payload_sha256
  FROM fixture_inverted_claim WHERE kind='staged';
INSERT INTO storage_v2_inverted_generation (
    generation_id,source_id,generation_seq,generation_item_count,generation_sealed_at,
    metadata_revision,metadata_binding_count,metadata_view_count,next_batch,total_blocks,
    cursor_occurrence_id,cursor_block_order,published_at,published_by
)
SELECT g.id,g.source_id,g.generation_seq,g.item_count,g.sealed_at,0,1,1,1,1,claim.occurrence_id,0,
       CASE WHEN claim.kind='published' THEN clock_timestamp() END,
       CASE WHEN claim.kind='published' THEN '{fixture.base.ADMIN}'::UUID END
  FROM fixture_inverted_claim claim JOIN source_generation g ON g.id=claim.generation_id
 WHERE claim.kind<>'staged';
INSERT INTO storage_v2_inverted_batch
SELECT generation_id,0,1,1,occurrence_id,0,1,6 FROM fixture_inverted_claim WHERE kind<>'staged';
INSERT INTO storage_v2_inverted_block
SELECT generation_id,0,0,occurrence_id,0,source_id,artifact_version_id,document_id,layout_sha256,payload_sha256
  FROM fixture_inverted_claim WHERE kind<>'staged';
INSERT INTO storage_v2_inverted_prefix
SELECT generation_id,0,prefix,CASE WHEN prefix=18 THEN decode('340000567800','hex') ELSE decode('','hex') END
  FROM fixture_inverted_claim CROSS JOIN generate_series(0,255) AS prefix WHERE kind<>'staged';
SELECT jsonb_object_agg(kind,occurrence_id) FROM fixture_inverted_claim;
"""))

    def test_sweep_retains_history_exports_external_references_and_generations(self):
        # Only synthetic fixture SQL errors may appear in this test log. The
        # live operator keeps raw database diagnostics in private evidence.
        diagnostics = patch.object(G, 'sql', lambda database, privileged, statement, error_path=None:
                                   self.command(database, statement).stdout.strip())
        diagnostics.start()
        self.addCleanup(diagnostics.stop)
        fixture.CleanupApplyTests.test_atomic_drop_retains_native_state_and_refuses_drift(self)
        self.command(self.database, file=next((fixture.base.ROOT/'migrations').glob('136_*.sql')))
        self.command(self.database, file=next((fixture.base.ROOT/'migrations').glob('137_*.sql')))
        self.command(self.database, file=next((fixture.base.ROOT/'migrations').glob('138_*.sql')))
        self.command(self.database, file=next((fixture.base.ROOT/'migrations').glob('139_*.sql')))
        self.command(self.database, file=next((fixture.base.ROOT/'migrations').glob('140_*.sql')))
        self.command(self.database, file=next((fixture.base.ROOT/'migrations').glob('141_*.sql')))
        self.command(self.database, file=next((fixture.base.ROOT/'migrations').glob('142_*.sql')))
        register_presence_role_cleanup(self.stack, self.socket)
        self.sql(self.admin('SELECT * FROM storage_v2_derive_document_identifiers(0,256,TRUE)'))
        self.sql(self.admin("""SELECT storage_v2_put_lexical_segment(
            o.id,o.artifact_version_id,65537,d.search_text,'','text')
            FROM occurrence o JOIN storage_v2_search_view_document b ON b.view_id=o.view_id
            JOIN storage_v2_search_document d ON d.id=b.document_id
            WHERE d.search_text<>'' ORDER BY o.id LIMIT 1"""))
        for source in json.loads(self.sql('SELECT json_agg(id ORDER BY id) FROM sources')):
            self.sql(self.admin(f'SELECT * FROM storage_v2_materialize_ordinary_first_terms({source},0,128)'))
        retained_first_coverage = self.sql('SELECT jsonb_agg(to_jsonb(c) ORDER BY occurrence_id)::text '
                                           'FROM storage_v2_ordinary_first_coverage c')
        retained_first_terms = self.sql('SELECT jsonb_agg(to_jsonb(t) ORDER BY occurrence_id,lexeme)::text '
                                        'FROM storage_v2_ordinary_first_term t')
        self.assertGreater(int(self.sql('SELECT count(*) FROM storage_v2_ordinary_first_term')), 0)
        active = self.sql('SELECT manifest_sha256 FROM storage_v2_activation_set_evidence ORDER BY created_at DESC,id DESC LIMIT 1')
        cleanup = self.sql('SELECT manifest_sha256 FROM storage_v2_legacy_cleanup_receipt LIMIT 1')
        before_search = self.search(fixture.base.ADMIN, active)
        generation_before = self.sql('SELECT jsonb_agg(to_jsonb(g) ORDER BY id)::text FROM source_generation g')
        dead_node, dead_view, dead_digest = self.make_projection('orphan graph fixture')
        retained_document = self.sql('SELECT id FROM storage_v2_search_document ORDER BY id LIMIT 1')
        self.sql(self.admin(f'SELECT storage_v2_seal_document_postings({retained_document})'))
        retained_seals = self.sql('SELECT jsonb_agg(to_jsonb(s) ORDER BY document_id)::text '
                                  'FROM storage_v2_document_postings_seal s')
        dead_document = self.sql(self.admin(
            "SELECT id FROM storage_v2_put_search_document('gc-identifiers-fixture-v1','node',"
            f"{dead_node},'orphan graph fixture',ARRAY[]::TEXT[])"))
        self.assertEqual(self.sql('SELECT count(*) FROM storage_v2_document_postings_seal '
                                  f'WHERE document_id={dead_document}'), '1')
        self.sql(self.admin(f'SELECT storage_v2_bind_search_document({dead_view},0,{dead_document},1.0)'))
        dead_occurrence = json.loads(self.sql(f"""WITH artifact AS (
            INSERT INTO artifact_version(item_id,source_id,witness_type,witness,adapter_profile_id,
                content_root_node_id,expected_content_hash,byte_length)
            SELECT item_id,source_id,'public-fixture-item','{{}}','gc-ordinary-first-fixture-v1',
                {dead_node},'{dead_digest}',19 FROM artifact_version ORDER BY id LIMIT 1
            RETURNING id,source_id
        ), inserted AS (
            INSERT INTO occurrence(source_id,artifact_version_id,view_id,role,ordinal,source_path,locator)
            SELECT source_id,id,{dead_view},'gc-fixture',0,'gc-orphan-fixture.txt','{{}}' FROM artifact
            RETURNING id,source_id
        ) SELECT row_to_json(inserted) FROM inserted"""))
        self.sql(self.admin('SELECT storage_v2_put_lexical_segment('
            f"{dead_occurrence['id']},(SELECT artifact_version_id FROM occurrence WHERE id={dead_occurrence['id']}),"
            "12001,'orphan graph fixture','','text')"))
        self.sql(self.admin('SELECT * FROM storage_v2_materialize_ordinary_first_terms('
            f"{dead_occurrence['source_id']},0,128)"))
        self.assertGreater(int(self.sql('SELECT count(*) FROM storage_v2_ordinary_first_term '
                                        f"WHERE occurrence_id={dead_occurrence['id']}")), 0)
        pack_id = '00000000-0000-4000-8000-000000000081'
        self.sql(f"""INSERT INTO content_pack(id,storage_key,build_nonce,status,manifest_sha256,
            stored_bytes,verified_at,published_at) VALUES('{pack_id}','{pack_id}.pack',
            '00000000-0000-4000-8000-000000000082','published',sha256('public pack identity fixture'::bytea),
            18,clock_timestamp(),clock_timestamp())""")
        self.sql(self.admin("""
CREATE TABLE fixture_gc_external(body_id BIGINT REFERENCES content_body(id));
CREATE TEMP TABLE fixture_gc_ids(name TEXT PRIMARY KEY,id BIGINT);
INSERT INTO fixture_gc_ids SELECT 'dead',id FROM storage_v2_put_inline_body('unreachable fixture'::bytea);
INSERT INTO fixture_gc_ids SELECT 'external',id FROM storage_v2_put_inline_body('foreign key retained fixture'::bytea);
INSERT INTO fixture_gc_ids SELECT 'export',id FROM storage_v2_put_inline_body('export retained fixture'::bytea);
INSERT INTO fixture_gc_ids SELECT 'history',id FROM storage_v2_put_inline_body('historic body without current mapping'::bytea);
INSERT INTO fixture_gc_external SELECT id FROM fixture_gc_ids WHERE name='external';
INSERT INTO storage_v2_legacy_hit_history(old_hit_id,source_id,occurrence_id,body_id,proof)
 SELECT 'gc-history-fixture',occ.source_id,occ.id,body.id,'{}'::jsonb
 FROM occurrence occ CROSS JOIN fixture_gc_ids body WHERE body.name='history' ORDER BY occ.id LIMIT 1;
"""))
        bodies = {name:int(self.sql(f"SELECT id FROM content_body WHERE digest=sha256('{value}'::bytea)"))
                  for name,value in [('dead','unreachable fixture'),('external','foreign key retained fixture'),
                                     ('export','export retained fixture'),('history','historic body without current mapping')]}
        retained = {'roots':[{'table':'content_body','id':bodies['export']}], 'preserve_all_generations':True}
        def make_plan(observed):
            return G.build_plan(observed,retained,'a'*64,active,cleanup,'b'*64,'c'*40,
                                resource_policy={'pack_root':'/public/fixture/pack-root'},
                                maintenance_binary_sha='e'*64)
        with patch.dict(os.environ, {'PGHOST':str(self.socket)}):
            observed = G.observation(self.database, False, retained['roots'])
        self.assertNotIn('inverted_retention', observed)
        plan = make_plan(observed)
        statement = G.apply_sql(plan, G.digest(plan), 'd'*64, fixture.base.ADMIN)
        self.sql(self.admin("SELECT storage_v2_put_inline_body('unexpected fixture row'::bytea)"))
        self.assert_sql_fails(statement, 'native GC before state drifted')
        self.assertEqual(self.sql('SELECT count(*) FROM storage_v2_gc_receipt'), '0')
        inverted = self.make_inverted_retention_fixture()
        self.assertEqual(set(inverted), {'staged', 'published', 'unfinished'})
        old_seal_ids = ','.join(str(row['document_id']) for row in json.loads(retained_seals))
        inverted_occurrences = ','.join(map(str, inverted.values()))
        retained_seals = self.sql(
            'SELECT jsonb_agg(to_jsonb(s) ORDER BY s.document_id)::text '
            'FROM storage_v2_document_postings_seal s '
            f'WHERE s.document_id IN ({old_seal_ids}) OR s.document_id IN ('
            'SELECT binding.document_id FROM occurrence o JOIN storage_v2_search_view_document binding '
            'ON binding.view_id=o.view_id AND binding.ordinal=0 '
            f'WHERE o.id IN ({inverted_occurrences}))'
        )
        self.assert_sql_fails(
            'BEGIN;UPDATE storage_v2_inverted_block SET document_id=-1;'
            + G.graph_sql(retained['roots']) + 'ROLLBACK;',
            'native GC inverted canonical identity differs')
        with patch.dict(os.environ, {'PGHOST':str(self.socket)}):
            observed = G.observation(self.database, False, retained['roots'])
        self.assertEqual(
            {kind: state['reference_count'] for kind, state in observed['inverted_retention'].items()},
            {'staged': 1, 'representative': 1, 'published': 1, 'unfinished': 1})
        plan = make_plan(observed)
        manifest = G.digest(plan)
        statement = G.apply_sql(plan, manifest, 'd'*64, fixture.base.ADMIN)
        self.sql(f"INSERT INTO content_reader_epoch(principal_id) VALUES('{fixture.base.ADMIN}')")
        self.assert_sql_fails(statement, 'drained readers and writers')
        self.sql('UPDATE content_reader_epoch SET finished_at=clock_timestamp() WHERE finished_at IS NULL')
        self.sql(statement)
        self.assertEqual(self.sql(f"SELECT count(*) FROM content_body WHERE id={bodies['dead']}"),'0')
        for occurrence_id in inverted.values():
            self.assertEqual(self.sql(f"SELECT count(*) FROM occurrence WHERE id={occurrence_id}"), '1')
        self.assertEqual(self.sql(f"SELECT count(*) FROM content_node WHERE id={dead_node}"),'0')
        self.assertEqual(self.sql(f"SELECT count(*) FROM retrieval_view WHERE id={dead_view}"),'0')
        self.assertEqual(self.sql(f"SELECT count(*) FROM occurrence WHERE id={dead_occurrence['id']}"), '0')
        for table in ('storage_v2_ordinary_first_term','storage_v2_ordinary_first_coverage'):
            self.assertEqual(self.sql(f"SELECT count(*) FROM {table} WHERE occurrence_id={dead_occurrence['id']}"), '0')
        self.assertEqual(self.sql(f'SELECT count(*) FROM storage_v2_search_document WHERE id={dead_document}'), '0')
        self.assertEqual(self.sql('SELECT count(*) FROM storage_v2_document_postings_seal '
                                  f'WHERE document_id={dead_document}'), '0')
        self.assertEqual(self.sql('SELECT jsonb_agg(to_jsonb(s) ORDER BY document_id)::text '
                                  'FROM storage_v2_document_postings_seal s'), retained_seals)
        for name in ('external','export','history'):
            self.assertEqual(self.sql(f"SELECT count(*) FROM content_body WHERE id={bodies[name]}"),'1')
        self.assertEqual(self.sql(f"SELECT count(*) FROM storage_v2_body_identity WHERE id={bodies['dead']}"),'1')
        self.assertEqual(self.search(fixture.base.ADMIN,active),before_search)
        self.assertEqual(self.sql('SELECT jsonb_agg(to_jsonb(c) ORDER BY occurrence_id)::text '
                                  'FROM storage_v2_ordinary_first_coverage c'), retained_first_coverage)
        self.assertEqual(self.sql('SELECT jsonb_agg(to_jsonb(t) ORDER BY occurrence_id,lexeme)::text '
                                  'FROM storage_v2_ordinary_first_term t'), retained_first_terms)
        self.assertEqual(self.sql('SELECT jsonb_agg(to_jsonb(g) ORDER BY id)::text FROM source_generation g'),generation_before)
        with patch.dict(os.environ, {'PGHOST':str(self.socket)}):
            receipt = G.receipt(self.database,False,manifest)
        self.assertEqual(receipt['result']['phase'],'DB_COMMITTED_PACK_RECLAIM_PENDING')
        self.assertEqual(receipt['result']['inverted_retention'], observed['inverted_retention'])
        self.assertFalse(receipt['result']['physical_reclaim_proven'])
        self.assertEqual(self.sql(f"SELECT status FROM storage_v2_gc_epoch WHERE id={receipt['gc_epoch_id']}"),'sweeping')
        self.assert_sql_fails('SET ROLE mainrag; SELECT * FROM storage_v2_gc_receipt','permission denied')
        accepted = self.sql(self.admin(f"SET ROLE mainrag; SELECT gc_epoch_id||':'||pack_root||':'||maintenance_binary_sha256 "
                                      f"FROM storage_v2_gc_pack_authority('{manifest}','{pack_id}')"))
        self.assertEqual(accepted,f"{receipt['gc_epoch_id']}:/public/fixture/pack-root:"+'e'*64)
        self.assertEqual(self.sql(self.admin(f"SET ROLE mainrag; SELECT count(*) FROM storage_v2_gc_pack_authority('"+'f'*64+f"','{pack_id}')")),'0')
        self.assert_sql_fails(self.actor(fixture.base.READER,f"SET ROLE mainrag; SELECT * FROM storage_v2_gc_pack_authority('{manifest}','{pack_id}')"),
                              'administrator authority')
        self.assert_sql_fails(self.admin(f"DELETE FROM content_body WHERE id={bodies['export']}"),'immutable')
        self.assert_sql_fails(self.admin(f'DELETE FROM storage_v2_search_document WHERE id={retained_document}'), 'immutable')
        self.assert_sql_fails(self.admin('DELETE FROM storage_v2_document_postings_seal'), 'immutable')
        self.assert_sql_fails(self.admin('DELETE FROM artifact_version '
            'WHERE id=(SELECT min(id) FROM artifact_version)'), 'immutable')
        print('PASS: real native mark/sweep, full retained generations, historical body, external FK and export roots, drift and reader rejection, immutable guard restoration',flush=True)
