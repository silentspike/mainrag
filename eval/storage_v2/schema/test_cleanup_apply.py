"""Actual PostgreSQL atomic legacy retirement, drift refusal and commit recovery."""
from __future__ import annotations
import copy
import importlib.util
import json
import os
import subprocess
from unittest.mock import patch

from eval.storage_v2.schema import test_active_set_search as base

SPEC=importlib.util.spec_from_file_location('cleanup_apply_sql',base.ROOT/'ops/storage-v2/cleanup-apply.py')
A=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(A)


class CleanupApplyTests(base.ActiveSetSearchTests):
    test_active_set_requires_receipt_and_reads_authorized_sources=None

    @classmethod
    def command(cls,database,sql=None,*,file=None,check=True):
        if sql is None:
            return super().command(database,file=file,check=check)
        result=subprocess.run(['psql','-X','--no-psqlrc','-qAt','--set=ON_ERROR_STOP=1',
            '--host',str(cls.socket),'--dbname',database],input=sql,cwd=base.ROOT,
            capture_output=True,text=True)
        if check and result.returncode:
            raise AssertionError(result.stderr)
        return result

    def test_atomic_drop_retains_native_state_and_refuses_drift(self):
        # Reuse the genuine activation + ordinary-ingest fixture, then remove
        # its deliberately registered late source that tests stale activation.
        base.ActiveSetSearchTests.test_active_set_requires_receipt_and_reads_authorized_sources(self)
        self.sql('DELETE FROM logical_source WHERE id=4; DELETE FROM sources WHERE id=4;')
        # Match deployed ordinary ownership in one block, preserving existing
        # dedicated native definers. This is the same boundary as the complete
        # schema fixture used by the supported native writer test.
        self.sql("""DO $fixture_owner$ DECLARE relation record; routine regprocedure;
BEGIN
 FOR relation IN SELECT relname FROM pg_class WHERE relnamespace='public'::regnamespace
   AND relkind IN ('r','p') AND relowner=current_user::regrole LOOP
  EXECUTE format('ALTER TABLE public.%I OWNER TO mainrag',relation.relname);
 END LOOP;
 FOR routine IN SELECT oid::regprocedure FROM pg_proc WHERE pronamespace='public'::regnamespace
   AND proowner=current_user::regrole AND (proname LIKE 'storage_v2_%' OR proname='user_can_access_source') LOOP
  EXECUTE format('ALTER FUNCTION %s OWNER TO mainrag',routine);
 END LOOP;
END $fixture_owner$;""")
        for number in range(100,135):
            self.command(self.database,file=next((base.ROOT/'migrations').glob(f'{number:03}_*.sql')))
        active=self.sql("SELECT manifest_sha256 FROM storage_v2_activation_set_evidence ORDER BY created_at DESC,id DESC LIMIT 1")
        self.assert_sql_fails(self.admin(f"SELECT storage_v2_require_complete_active_set('{active}')"),
                              'row-level security policy for table "storage_v2_active_ingest_receipt"')
        self.command(self.database,file=next((base.ROOT/'migrations').glob('135_*.sql')))
        self.assertEqual(self.sql("SELECT pg_get_userbyid(proowner) FROM pg_proc WHERE oid="
                                  "'storage_v2_require_complete_active_set(text)'::regprocedure"),'mainrag_v2_frontier_owner')
        self.assert_sql_fails('SET ROLE mainrag; SET row_security=off; SELECT count(*) FROM storage_v2_active_ingest_receipt',
                              'row-level security policy')
        self.sql("""
CREATE TABLE cleanup_parent(id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,value TEXT);
CREATE TABLE cleanup_child(id BIGINT PRIMARY KEY,parent_id BIGINT REFERENCES cleanup_parent(id));
CREATE INDEX cleanup_parent_value ON cleanup_parent(value);
INSERT INTO cleanup_parent(value) VALUES ('public fixture');
INSERT INTO cleanup_child VALUES (1,1);
CREATE FUNCTION cleanup_retired() RETURNS BIGINT LANGUAGE plpgsql AS $$
BEGIN RETURN (SELECT count(*) FROM cleanup_parent); END $$;
""")
        active=self.sql("SELECT manifest_sha256 FROM storage_v2_activation_set_evidence ORDER BY created_at DESC,id DESC LIMIT 1")
        search_before=self.search(base.ADMIN,active)
        reader_before=self.search(base.READER,active)
        def intelligence_set():
            return {command:json.loads(self.sql(self.admin(
                f"SELECT storage_v2_active_intelligence_command('{active}','{command}',"
                "'{\"name\":\"alpha\",\"direction\":\"callees\",\"limit\":50}'::jsonb)::text")))
                for command in ('card','explain','layers','ownership')}
        intelligence_before=intelligence_set()
        self.assert_sql_fails(self.actor(base.READER,f"SELECT storage_v2_search_active('{active}',"
            "'{\"type\":\"term\",\"value\":\"alpha\"}'::jsonb,'{}'::jsonb,10,2)"),'source access denied')
        protected=self.sql('SELECT jsonb_build_array((SELECT count(*) FROM content_body),(SELECT count(*) FROM occurrence),'
                          '(SELECT count(*) FROM generation_item_version),(SELECT count(*) FROM storage_v2_active_ingest_receipt))::text')

        legacy_names=('cleanup_parent','cleanup_child','files','chunks','symbols','embeddings',
                      'chunk_embeddings','call_graph','entities','entity_relations','indexing_outbox')
        def plan_for():
            with patch.dict(os.environ,{'PGHOST':str(self.socket)}):
                catalog=A.C.catalog(self.database,False,legacy_names,retain_all=True)
            inventory={'schema_version':'mainrag.storage-v2.cleanup-catalog.v1','status':'OBSERVED_ONLY',
                       'catalog':catalog,'qdrant':None,'runtime_search':None,'exports':None,
                       'before_state_sha256':A.digest(catalog),'operator_sha256':'b'*64}
            parents={row['oid']:row['relation_oid'] for row in catalog['indexes']}
            parents.update({row['oid']:row['owned_by_relation_oid'] for row in catalog['relations'] if row.get('owned_by_relation_oid')})
            deleted={row['oid'] for row in catalog['relations'] if row['name'] in legacy_names}
            owned={key for key,value in parents.items() if value in deleted}
            decisions={}
            for item in A.M.observed_objects(inventory):
                kind,row=item['kind'],item['observed']
                parent=row.get('relation_oid')
                delete=(kind=='relation' and row['oid'] in deleted|owned or
                        kind in ('column','constraint','policy','trigger','index') and parent in deleted|owned or
                        kind=='function' and row['name'] in A.M.RETIRED_BOOTSTRAP_FUNCTIONS|{'cleanup_retired'} or
                        kind=='outbox_class')
                decisions[item['key']]={'key':item['key'],'disposition':'DELETE' if delete else 'KEEP',
                                        'reason':'public destructive fixture only','authority':'fixture'}
            draft=A.M.draft(inventory,'c'*64,decisions)
            return A.build_plan(inventory,'c'*64,draft,'d'*64,'e'*64)

        plan=plan_for()
        sql=A.sql_for(plan,'1'*64,'2'*64,active)
        self.sql("INSERT INTO cleanup_parent(value) VALUES ('unplanned')")
        self.assert_sql_fails(sql,'catalog drifted')
        self.assertEqual(self.sql('SELECT count(*) FROM storage_v2_legacy_cleanup_receipt'),'0')
        self.assertEqual(self.sql('SELECT count(*) FROM cleanup_child'),'1')
        self.sql("DELETE FROM cleanup_parent WHERE value='unplanned'")
        plan=plan_for()
        self.sql('CREATE VIEW cleanup_unapproved AS SELECT * FROM cleanup_parent')
        self.assert_sql_fails(A.sql_for(plan,'1'*64,'2'*64,active),'catalog drifted')
        # A refreshed manifest still rejects an unapproved normal dependency,
        # and rolls back the function drop executed earlier in the transaction.
        with_dependency=plan_for()
        self.assert_sql_fails(A.sql_for(with_dependency,'1'*64,'2'*64,active),'because other objects depend')
        self.assertEqual(self.sql("SELECT to_regprocedure('cleanup_retired()') IS NOT NULL"),'t')
        self.sql('DROP VIEW cleanup_unapproved')
        plan=plan_for()
        epoch=self.sql(self.admin('SELECT storage_v2_begin_reader_epoch()'))
        self.assert_sql_fails(A.sql_for(plan,'1'*64,'2'*64,active),'catalog drifted')
        self.sql(self.admin(f"SELECT storage_v2_end_reader_epoch('{epoch}')"))
        plan=plan_for()
        wrong_pointer=copy.deepcopy(plan)
        wrong_pointer['catalog']['pointer_set_sha256']='0'*64
        self.assert_sql_fails(A.sql_for(wrong_pointer,'1'*64,'2'*64,active),'catalog drifted')
        self.sql(A.sql_for(plan,'1'*64,'2'*64,active))
        self.assertEqual(self.sql("SELECT to_regclass('cleanup_parent') IS NULL AND to_regclass('cleanup_child') IS NULL "
                                  "AND to_regprocedure('cleanup_retired()') IS NULL"),'t')
        self.assertEqual(self.sql("SELECT to_regclass('files') IS NULL AND to_regclass('chunks') IS NULL "
                                  "AND to_regclass('indexing_outbox') IS NULL"),'t')
        self.assertEqual(self.sql('SELECT count(*) FROM storage_v2_legacy_cleanup_receipt'),'1')
        with patch.dict(os.environ,{'PGHOST':str(self.socket)}):
            receipt=A.receipt(self.database,False,'1'*64)
            after=A.C.catalog(self.database,False,retain_all=True)
            postcheck=A.post_readback(plan,self.database,False,receipt)
            self.assertFalse(A.transaction_running(self.database,False))
        self.assertTrue(postcheck['targets_absent'])
        self.assertTrue(postcheck['native_root_set_unchanged'])
        self.assertIsInstance(postcheck['measured_database_bytes_released'],int)
        self.assertEqual(receipt['result']['status'],'DB_COMMITTED_POSTCHECK_PENDING')
        self.assertGreater(receipt['result']['deleted_relation_bytes'],0)
        self.assertEqual(after['pointer_set_sha256'],plan['pointer_set_sha256'])
        self.assertEqual(self.search(base.ADMIN,active),search_before)
        self.assertEqual(self.search(base.READER,active),reader_before)
        self.assertEqual(intelligence_set(),intelligence_before)
        self.assertTrue(all(value['source_count']==2 for value in intelligence_before.values()))
        self.assertEqual(self.sql('SELECT jsonb_build_array((SELECT count(*) FROM content_body),(SELECT count(*) FROM occurrence),'
                                 '(SELECT count(*) FROM generation_item_version),(SELECT count(*) FROM storage_v2_active_ingest_receipt))::text'),protected)
        self.assertEqual(self.sql("SELECT has_table_privilege('mainrag','storage_v2_legacy_cleanup_receipt','INSERT')"),'f')
        # The anti-replay decision observes a currently held backend lease,
        # rather than treating a state file or old RUNNING label as liveness.
        child=subprocess.Popen(['psql','-X','--no-psqlrc','-qAt','--set=ON_ERROR_STOP=1',
            '--host',str(self.socket),'--dbname',self.database],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,text=True)
        backend=None
        try:
            child.stdin.write("BEGIN; SELECT pg_advisory_xact_lock(hashtextextended('mainrag.legacy-cleanup',0)); "
                              "SELECT 'LEASE:'||pg_backend_pid(); SELECT pg_sleep(30); COMMIT;\n")
            child.stdin.flush()
            while backend is None:
                line=child.stdout.readline()
                if not line:raise AssertionError('owned lease holder stopped before observation')
                if line.startswith('LEASE:'):backend=int(line.split(':')[1])
            with patch.dict(os.environ,{'PGHOST':str(self.socket)}):
                self.assertTrue(A.transaction_running(self.database,False))
        finally:
            if backend:self.sql(f'SELECT pg_terminate_backend({backend})')
            child.terminate();child.wait(timeout=5)
            child.stdin.close();child.stdout.close()
        self.sql('CREATE TABLE cleanup_parent(id BIGINT)')
        with patch.dict(os.environ,{'PGHOST':str(self.socket)}),self.assertRaisesRegex(RuntimeError,'recreated'):
            A.post_readback(plan,self.database,False,receipt)
        self.sql('DROP TABLE cleanup_parent')
        print('PASS: atomic restricted drop, count/pointer/dependency drift, rollback, native search and durable commit readback',flush=True)
