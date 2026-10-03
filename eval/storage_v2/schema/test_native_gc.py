"""Real native graph sweep after the genuine atomic legacy cleanup rehearsal."""
from __future__ import annotations
import importlib.util
import os
from unittest.mock import patch
from eval.storage_v2.schema import test_cleanup_apply as fixture

SPEC = importlib.util.spec_from_file_location('native_gc', fixture.base.ROOT/'ops/storage-v2/native-gc.py')
G = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(G)


class NativeGcTests(fixture.CleanupApplyTests):
    test_atomic_drop_retains_native_state_and_refuses_drift = None

    def test_sweep_retains_history_exports_external_references_and_generations(self):
        # Only synthetic fixture SQL errors may appear in this test log. The
        # live operator keeps raw database diagnostics in private evidence.
        diagnostics = patch.object(G, 'sql', lambda database, privileged, statement, error_path=None:
                                   self.command(database, statement).stdout.strip())
        diagnostics.start()
        self.addCleanup(diagnostics.stop)
        fixture.CleanupApplyTests.test_atomic_drop_retains_native_state_and_refuses_drift(self)
        self.command(self.database, file=next((fixture.base.ROOT/'migrations').glob('136_*.sql')))
        active = self.sql('SELECT manifest_sha256 FROM storage_v2_activation_set_evidence ORDER BY created_at DESC,id DESC LIMIT 1')
        cleanup = self.sql('SELECT manifest_sha256 FROM storage_v2_legacy_cleanup_receipt LIMIT 1')
        before_search = self.search(fixture.base.ADMIN, active)
        generation_before = self.sql('SELECT jsonb_agg(to_jsonb(g) ORDER BY id)::text FROM source_generation g')
        dead_node, dead_view, _ = self.make_projection('orphan graph fixture')
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
        plan = make_plan(observed)
        statement = G.apply_sql(plan, G.digest(plan), 'd'*64, fixture.base.ADMIN)
        self.sql(self.admin("SELECT storage_v2_put_inline_body('unexpected fixture row'::bytea)"))
        self.assert_sql_fails(statement, 'native GC before state drifted')
        self.assertEqual(self.sql('SELECT count(*) FROM storage_v2_gc_receipt'), '0')
        with patch.dict(os.environ, {'PGHOST':str(self.socket)}):
            observed = G.observation(self.database, False, retained['roots'])
        plan = make_plan(observed)
        manifest = G.digest(plan)
        statement = G.apply_sql(plan, manifest, 'd'*64, fixture.base.ADMIN)
        self.sql(f"INSERT INTO content_reader_epoch(principal_id) VALUES('{fixture.base.ADMIN}')")
        self.assert_sql_fails(statement, 'drained readers and writers')
        self.sql('UPDATE content_reader_epoch SET finished_at=clock_timestamp() WHERE finished_at IS NULL')
        self.sql(statement)
        self.assertEqual(self.sql(f"SELECT count(*) FROM content_body WHERE id={bodies['dead']}"),'0')
        self.assertEqual(self.sql(f"SELECT count(*) FROM content_node WHERE id={dead_node}"),'0')
        self.assertEqual(self.sql(f"SELECT count(*) FROM retrieval_view WHERE id={dead_view}"),'0')
        for name in ('external','export','history'):
            self.assertEqual(self.sql(f"SELECT count(*) FROM content_body WHERE id={bodies[name]}"),'1')
        self.assertEqual(self.sql(f"SELECT count(*) FROM storage_v2_body_identity WHERE id={bodies['dead']}"),'1')
        self.assertEqual(self.search(fixture.base.ADMIN,active),before_search)
        self.assertEqual(self.sql('SELECT jsonb_agg(to_jsonb(g) ORDER BY id)::text FROM source_generation g'),generation_before)
        with patch.dict(os.environ, {'PGHOST':str(self.socket)}):
            receipt = G.receipt(self.database,False,manifest)
        self.assertEqual(receipt['result']['phase'],'DB_COMMITTED_PACK_RECLAIM_PENDING')
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
        print('PASS: real native mark/sweep, full retained generations, historical body, external FK and export roots, drift and reader rejection, immutable guard restoration',flush=True)
