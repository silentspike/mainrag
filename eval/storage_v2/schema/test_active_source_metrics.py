"""Active metadata isolation, fragment counts and legacy-table independence."""
import json
import re
import unittest
import uuid

from eval.storage_v2.schema import test_active_set_search as parent


class ActiveSourceMetricsTests(unittest.TestCase):
    setUpClass = parent.ActiveSetSearchTests.__dict__["setUpClass"]
    tearDownClass = parent.ActiveSetSearchTests.__dict__["tearDownClass"]
    command = parent.ActiveSetSearchTests.__dict__["command"]
    sql = parent.ActiveSetSearchTests.__dict__["sql"]
    actor = parent.ActiveSetSearchTests.__dict__["actor"]
    admin = parent.ActiveSetSearchTests.__dict__["admin"]
    assert_sql_fails = parent.ActiveSetSearchTests.assert_sql_fails
    make_projection = parent.ActiveSetSearchTests.make_projection
    begin = parent.ActiveSetSearchTests.begin
    complete_analysis = parent.ActiveSetSearchTests.complete_analysis
    commit = parent.ActiveSetSearchTests.commit

    def metrics(self, digest, source=1, actor=parent.ADMIN, include_test=False):
        return json.loads(self.sql(self.actor(actor,
            f"SELECT storage_v2_active_source_metrics('{digest}',{source},{str(include_test).lower()})")))

    def test_fragment_membership_isolation_and_no_legacy_tables(self):
        self.sql(f"""
CREATE TABLE users(id UUID PRIMARY KEY,is_admin BOOLEAN NOT NULL);
INSERT INTO users VALUES ('{parent.ADMIN}',true),('{parent.READER}',false);
CREATE FUNCTION user_can_access_source(p_user UUID,p_source BIGINT,p_action TEXT DEFAULT 'read')
RETURNS BOOLEAN LANGUAGE SQL STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
 SELECT p_user='{parent.ADMIN}'::uuid OR (p_user='{parent.READER}'::uuid AND p_source=1 AND p_action='read')
$$;
INSERT INTO sources(id,name,type,path) VALUES
 (1,'metrics-fragments','fixture','synthetic/fragments'),
 (2,'metrics-other','fixture','synthetic/other'),
 (3,'metrics-empty','fixture','synthetic/empty');
UPDATE sources SET is_test=true WHERE id=3;
""")
        migration = parent.ROOT / 'migrations/109_storage_v2_active_source_metrics.sql'
        self.command(self.database,file=migration)
        entries=[]
        for source, count in [(1,2),(2,1),(3,0)]:
            run=self.begin(source,f'{source:02x}'*32,f'{source+3:02x}'*32,user_id=parent.ADMIN)
            if count:
                content='alpha shared fragment';node,view,body_hash=self.make_projection(content)
                for number in range(count):
                    self.sql(self.admin(f"""
SELECT storage_v2_stage_shadow_item({run},'fragment-{number}','document','fixture-item',
 '{{"path":"synthetic/shared.txt","byte_start":{number*len(content)}}}'::jsonb,
 'fixture-adapter-v1',{node},NULL,'{body_hash}',{len(content)},decode('{body_hash}','hex'),
 'fixture-analysis-v1',{view},'synthetic/shared.txt','{{"fragmented":true}}'::jsonb);
"""))
                self.complete_analysis(body_hash)
            if count:
                symbol = self.sql(self.admin(f"""
WITH item AS (SELECT artifact_version_id,occurrence_id FROM storage_v2_ingest_run_item
 WHERE run_id={run} ORDER BY source_item_id LIMIT 1), added AS (
 SELECT storage_v2_put_symbol_occurrence({source},artifact_version_id,occurrence_id,
 'fixture-symbol','text','function','fixture_function',NULL,NULL,NULL,
 '{{"name":"fixture_function"}}'::jsonb,'{{"byte_start":0,"byte_end":1}}'::jsonb) value
 FROM item)
SELECT (value).id||':'||(value).symbol_id FROM added;
"""))
                occurrence,symbol_id=map(int,symbol.split(':'))
                self.sql(self.admin(f"""
SELECT storage_v2_record_call({occurrence},{symbol_id},'fixture_function','call',
 '{{"resolution_kind":"parser_symbol_id"}}'::jsonb);
SELECT storage_v2_record_call({occurrence},NULL,'unknown_function','call',
 '{{"resolution_kind":"unresolved"}}'::jsonb);
"""))
            self.commit(run,count,user_id=parent.ADMIN)
            generation=int(self.sql(f'SELECT generation_id FROM storage_v2_ingest_run WHERE id={run}'))
            self.sql(self.admin(f"SELECT storage_v2_verify_generation({generation},'{'a'*64}'); SELECT storage_v2_mark_release_candidate({generation});"))
            evidence=str(uuid.uuid4())
            self.sql(f"""INSERT INTO storage_v2_release_candidate_evidence(
 id,source_id,generation_id,commit_sha,source_watermark_sha256,
 adapter_profile_id,analysis_profile_id,search_profile_id,manifest,manifest_sha256)
 VALUES ('{evidence}',{source},{generation},'{'c'*40}','{'b'*64}',
 'fixture-adapter','fixture-analysis','fixture-search','{{"status":"PASS"}}',
 digest(convert_to('{{"status":"PASS"}}'::jsonb::text,'UTF8'),'sha256'));""")
            evidence_hash=self.sql(f"SELECT encode(manifest_sha256,'hex') FROM storage_v2_release_candidate_evidence WHERE id='{evidence}'")
            entries.append(dict(source_id=source,candidate_generation_id=generation,
                expected_active_generation_id=None,candidate_commit_sha='c'*40,evidence_id=evidence,
                evidence_manifest_sha256=evidence_hash,source_watermark_sha256='b'*64))
        manifest=dict(schema_version='mainrag.storage-v2.activation-set.v1',activation_id=str(uuid.uuid4()),
            code_commit_sha='c'*40,schema_sha256='d'*64,backend_package_sha256='e'*64,
            aggregate_evidence_sha256='f'*64,sources=entries)
        literal=json.dumps(manifest,sort_keys=True)
        digest=self.sql(f"SELECT encode(digest(convert_to('{literal}'::jsonb::text,'UTF8'),'sha256'),'hex')")
        self.sql(self.admin(f"SELECT storage_v2_activate_candidate_set('{literal}'::jsonb,'{digest}')"))
        before=self.metrics(digest)
        self.assertEqual(before['file_count'],1)
        self.assertEqual(before['item_count'],2)
        self.assertEqual(before['total_size'],2*len(content))
        self.assertEqual(before['view_count'],1)
        self.assertEqual(before['symbol_count'],1)
        self.assertEqual(before['call_count'],2)
        self.assertEqual(self.metrics(digest,actor=parent.READER),before)
        self.assert_sql_fails(self.actor(parent.READER,f"SELECT storage_v2_active_source_metrics('{digest}',2)"),'source access denied')
        self.assert_sql_fails(self.admin(f"SELECT storage_v2_active_source_metrics('{digest}',3)"),'test source requires explicit test scope')
        empty=self.metrics(digest,source=3,include_test=True)
        self.assertEqual([empty[k] for k in ['file_count','item_count','total_size','view_count','symbol_count','call_count']],[0]*6)
        self.assert_sql_fails(self.actor(parent.READER,f"SELECT storage_v2_active_source_metrics('{digest}',1,true)"),'test scope requires administrator authority')
        # Unsealed work and legacy cached counters must not change active metrics.
        self.begin(1,'91'*32,'92'*32,user_id=parent.ADMIN)
        self.sql('UPDATE sources SET file_count=999,total_size=999; ALTER TABLE files RENAME TO retired_files; ALTER TABLE chunks RENAME TO retired_chunks; ALTER TABLE symbols RENAME TO retired_symbols; ALTER TABLE call_graph RENAME TO retired_call_graph;')
        self.assertEqual(self.metrics(digest),before)
        # Execute the actual handler query against renamed legacy tables and
        # deliberately wrong cached counts. Test sources cannot inflate it.
        self.sql('ALTER TABLE sources ADD COLUMN IF NOT EXISTS watch_enabled BOOLEAN; '
                 'UPDATE sources SET watch_enabled=(id IN (1,3));')
        handler = (parent.ROOT / 'api/src/api/handlers/watch.rs').read_text()
        watch_sql = re.search(r'const ACTIVE_WATCH_STATS_SQL: &str = r#"(.*?)"#;',
                              handler, re.S).group(1).replace('$1', f"'{digest}'")
        watch = json.loads(self.sql(self.admin(
            f'SELECT row_to_json(watched) FROM ({watch_sql}) watched')))
        self.assertEqual(watch['watched_sources'],1)
        self.assertEqual(watch['monitored_files'],before['file_count'])
        self.assertIsNotNone(watch['last_scan'])
        self.command(self.database,file=migration)
        self.assertEqual(self.metrics(digest),before)
        self.assertEqual(self.sql("SELECT has_function_privilege('public','storage_v2_active_source_metrics(text,bigint,boolean)','execute')"),'f')
        self.sql("INSERT INTO sources(id,name,type,path) VALUES(4,'late-metrics-source','fixture','synthetic/late')")
        self.assert_sql_fails(self.admin(f"SELECT storage_v2_active_source_metrics('{digest}',1)"),'complete activated source set and exact receipt are required')


if __name__ == '__main__':
    unittest.main()
