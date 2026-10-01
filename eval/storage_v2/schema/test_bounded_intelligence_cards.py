"""Bounded active card reads, complete exports, and authorization fences."""
import json
import re
import unittest
import uuid

from eval.storage_v2.schema import test_active_set_search as parent


MIGRATION = parent.ROOT / 'migrations/110_storage_v2_bounded_intelligence_cards.sql'
SIGNATURES = ('storage_v2_intelligence_command(bigint,text,text,jsonb)',
              'storage_v2_active_intelligence_command(text,text,jsonb,bigint,boolean)')


class BoundedIntelligenceCardsTests(unittest.TestCase):
    setUpClass = parent.ActiveSetSearchTests.__dict__['setUpClass']
    tearDownClass = parent.ActiveSetSearchTests.__dict__['tearDownClass']
    command = parent.ActiveSetSearchTests.__dict__['command']
    sql = parent.ActiveSetSearchTests.__dict__['sql']
    actor = parent.ActiveSetSearchTests.__dict__['actor']
    admin = parent.ActiveSetSearchTests.__dict__['admin']
    assert_sql_fails = parent.ActiveSetSearchTests.assert_sql_fails
    make_projection = parent.ActiveSetSearchTests.make_projection
    begin = parent.ActiveSetSearchTests.begin
    stage = parent.ActiveSetSearchTests.stage
    complete_analysis = parent.ActiveSetSearchTests.complete_analysis
    commit = parent.ActiveSetSearchTests.commit

    def named(self, source, query, command='card'):
        literal = json.dumps(query).replace("'", "''")
        return json.loads(self.sql(self.admin(
            f"SELECT storage_v2_intelligence_command({source},'1','{command}','{literal}')")))

    def active(self, digest, query, *, actor=parent.ADMIN, source=None, include_test=False, command='card'):
        literal = json.dumps(query).replace("'", "''")
        return json.loads(self.sql(self.actor(actor,
            f"SELECT storage_v2_active_intelligence_command('{digest}','{command}',"
            f"'{literal}',{'NULL' if source is None else source},{str(include_test).lower()})")))

    def extra_intelligence_tests(self,digest):
        pass

    def test_request_budget_membership_exports_and_authority(self):
        self.sql(f"""
CREATE TABLE users(id UUID PRIMARY KEY,is_admin BOOLEAN NOT NULL);
INSERT INTO users VALUES ('{parent.ADMIN}',true),('{parent.READER}',false);
CREATE FUNCTION user_can_access_source(p_user UUID,p_source BIGINT,p_action TEXT DEFAULT 'read')
RETURNS BOOLEAN LANGUAGE SQL STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
 SELECT p_user='{parent.ADMIN}'::uuid OR (p_user='{parent.READER}'::uuid AND p_source=1 AND p_action='read')
$$;
INSERT INTO sources(id,name,type,path) VALUES
 (1,'cards-one','fixture','synthetic/one'),
 (2,'cards-two','fixture','synthetic/two'),
 (3,'cards-test','fixture','synthetic/test');
UPDATE sources SET is_test=true WHERE id=3;
""")
        entries = []
        for source, count in [(1,137),(2,5),(3,3)]:
            content=f'fn fixture_source_{source}() {{}}'
            node,view,body_hash=self.make_projection(content)
            run=self.begin(source,f'{source:02x}'*32,f'{source+3:02x}'*32,user_id=parent.ADMIN)
            self.stage(run,f'source-{source}.rs',content,node,view,body_hash,user_id=parent.ADMIN)
            artifact,occurrence=map(int,self.sql(
                f"SELECT artifact_version_id||':'||occurrence_id FROM storage_v2_ingest_run_item WHERE run_id={run}").split(':'))
            self.sql(self.admin(f"""
SELECT count(*) FROM generate_series(1,{count}) n
CROSS JOIN LATERAL storage_v2_put_structural_card_bundle(
 {source},{artifact},{occurrence},'fixture-'||lpad(n::text,3,'0'),'rust','function',
 'crate::fixture_'||n,'fn fixture()',NULL,'public','{{"kind":"function"}}',
 '{{"line_start":1,"line_end":1}}','fixture-cards',decode(repeat('aa',32),'hex'),
 jsonb_build_object('name','fixture_'||n),'{{}}','{{}}') card;
"""))
            first=self.sql(self.admin(f"SELECT visible.id||':'||stable.id FROM storage_v2_symbol_occurrence visible JOIN storage_v2_symbol stable ON stable.id=visible.symbol_id WHERE visible.source_id={source} AND stable.symbol_key='fixture-001'"))
            second=self.sql(self.admin(f"SELECT id FROM storage_v2_symbol WHERE source_id={source} AND symbol_key='fixture-002'"))
            caller,_=first.split(':')
            self.sql(self.admin(f"SELECT storage_v2_record_call({caller},{second},'fixture_2','call','{{\"resolution_kind\":\"parser_symbol_id\",\"line\":1}}'); SELECT storage_v2_record_call({caller},NULL,'unknown_fixture','call','{{\"resolution_kind\":\"unresolved\",\"line\":2}}','[\"candidate-key\"]')"))
            self.complete_analysis(body_hash)
            self.commit(run,1,user_id=parent.ADMIN)
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
        old_cards=self.named(1,{})
        old_active=self.active(digest,{})
        exports=[self.sql(self.admin(f"SELECT storage_v2_export_intelligence({s},'1','protected')")) for s in (1,2,3)]
        def metadata():
            return [self.sql(f"SELECT to_jsonb(p)-'prosrc' FROM pg_proc p WHERE oid='{sig}'::regprocedure") for sig in SIGNATURES]
        before=metadata()
        self.command(self.database,file=MIGRATION)
        self.assertEqual(metadata(),before)
        self.assertEqual(self.named(1,{}),old_cards)
        self.assertEqual(self.active(digest,{}),old_active)
        for source,payload in zip((1,2,3),exports):
            self.assertEqual(self.sql(self.admin(f"SELECT storage_v2_export_intelligence({source},'1','protected')")),payload)
        for limit in (1,10,100,137,138,200):
            for command in ('card','layers'):
                with self.subTest(limit=limit,command=command):
                    expected=old_cards[:limit]
                    self.assertEqual(self.named(1,dict(limit=limit),command),expected)
                    response=self.active(digest,dict(limit=limit),command=command)
                    self.assertEqual(response['source_count'],2)
                    self.assertEqual([r['source_id'] for r in response['results']],[1,2])
                    self.assertEqual(response['results'][0]['value'],expected)
                    self.assertEqual(len(response['results'][1]['value']),min(5,max(0,limit-137)))
                    self.assertEqual(response,self.active(digest,dict(limit=limit),command=command))
        self.assertEqual(self.active(digest,dict(limit=10),actor=parent.READER)['source_count'],1)
        self.assertEqual(self.active(digest,dict(limit=10),source=2)['results'][0]['value'],self.named(2,{})[:10])
        self.assertEqual(self.active(digest,dict(limit=200),include_test=True)['source_count'],3)
        self.assertEqual(self.active(digest,dict(limit=2,name='fixture_137'))['results'][0]['value'],old_cards[-1:])
        self.assertTrue(all(not r['value'] for r in self.active(digest,dict(limit=10,name='missing'))['results']))
        # A later unsealed artifact with cards must not enter current membership.
        content='unsealed private fixture'; node,view,body_hash=self.make_projection(content)
        run=self.begin(1,'ab'*32,'cd'*32,user_id=parent.ADMIN)
        self.stage(run,'unsealed.rs',content,node,view,body_hash,user_id=parent.ADMIN)
        artifact,occurrence=self.sql(f"SELECT artifact_version_id||':'||occurrence_id FROM storage_v2_ingest_run_item WHERE run_id={run}").split(':')
        self.sql(self.admin(f"SELECT storage_v2_put_structural_card_bundle(1,{artifact},{occurrence},'000-hidden','rust','function','crate::hidden',NULL,NULL,NULL,'{{}}','{{}}','fixture-cards',decode(repeat('aa',32),'hex'),'{{\"name\":\"hidden\"}}','{{}}','{{}}')"))
        self.assertEqual(self.named(1,dict(limit=1)),old_cards[:1])
        for limit in [0,-1,201,1.5,'20',True,{},[],10**30]:
            query=json.dumps(dict(limit=limit))
            for function,args in [('storage_v2_intelligence_command',"1,'1'"),
                                  ('storage_v2_active_intelligence_command',f"'{digest}'")]:
                self.assert_sql_fails(self.admin(f"SELECT {function}({args},'card','{query}')"),'limit must be an integer')
        self.assert_sql_fails(self.actor(parent.READER,f"SELECT storage_v2_active_intelligence_command('{digest}','card','{{\"limit\":1}}',2)"),'source access denied')
        self.assert_sql_fails(self.actor(parent.READER,f"SELECT storage_v2_active_intelligence_command('{digest}','card','{{\"limit\":1}}',NULL,true)"),'administrator authority')
        self.assert_sql_fails(self.admin(f"SELECT storage_v2_active_intelligence_command('{digest}','card','{{\"limit\":1}}',3)"),'explicit test scope')
        # Guard the real per-source command: exhausted budgets must not invoke
        # another collection. Merely truncating an already aggregated result fails.
        definition=self.sql(f"SELECT pg_get_functiondef('{SIGNATURES[0]}'::regprocedure)")
        try:
            self.sql(definition.replace('BEGIN\n',"BEGIN\n    IF p_source_id<>1 THEN RAISE EXCEPTION 'exhausted budget read another source'; END IF;\n",1))
            self.assertEqual(self.active(digest,dict(limit=1))['results'][1]['value'],[])
        finally:
            self.sql(definition)
        # Inspect the executed card SELECT: LIMIT feeds JSON aggregation with
        # only the requested rows rather than trimming the resulting JSON array.
        query=re.search(r'SELECT COALESCE\(jsonb_agg\(value ORDER BY.*?\) cards;',definition,re.S).group()
        query=query.replace('INTO v_result','').replace('p_source_id','1').replace('v_generation.generation_seq','1')
        query=query.replace('p_query',"'{\"limit\":10}'::jsonb").replace('v_limit','10')
        plan=json.loads(self.sql(self.admin('EXPLAIN (ANALYZE,FORMAT JSON) '+query)))[0]['Plan']
        def nodes(node):
            yield node
            for child in node.get('Plans',[]): yield from nodes(child)
        limited=[n for n in nodes(plan) if n['Node Type']=='Limit']
        self.assertEqual([n['Actual Rows'] for n in limited],[10])
        self.command(self.database,file=MIGRATION)
        self.assertEqual(metadata(),before)
        # Renaming legacy runtime tables cannot affect active card reads.
        for table in ('files','chunks','symbols','call_graph'):
            self.sql(f'ALTER TABLE {table} RENAME TO fixture_retained_{table}')
        self.assertEqual(self.active(digest,dict(limit=1))['results'][0]['value'],old_cards[:1])
        symbols_migration=parent.ROOT/'migrations/111_storage_v2_symbol_and_call_commands.sql'
        self.command(self.database,file=symbols_migration)
        symbols=self.active(digest,dict(limit=138),command='symbols')
        self.assertEqual([len(r['value']) for r in symbols['results']],[137,1])
        self.assertTrue(all(row['id']<0 and row['file_id']<0 for r in symbols['results'] for row in r['value']))
        self.assertEqual(self.active(digest,dict(limit=10,name='hidden'),command='symbols')['results'][0]['value'],[])
        self.assertEqual([len(r['value']) for r in self.active(digest,{},command='symbols')['results']],[50,0])
        self.assertEqual(self.active(digest,dict(limit=10,language='python'),command='symbols')['results'][0]['value'],[])
        calls=self.active(digest,dict(limit=3,name='fixture_1'),command='callees')
        self.assertEqual([len(r['value']) for r in calls['results']],[2,1])
        one=calls['results'][0]['value']
        self.assertEqual([c['proven'] for c in one],[True,False])
        self.assertEqual(one[1]['candidate_symbol_keys'],['candidate-key'])
        self.assertEqual(one[0]['call_line'],1)
        self.assertEqual(one[0]['generation_seq'],1)
        self.assertEqual(one[0]['caller_id'],symbols['results'][0]['value'][0]['id'])
        callers=self.active(digest,dict(limit=3,name='fixture_2'),command='callers')
        self.assertEqual([len(r['value']) for r in callers['results']],[1,1])
        self.assertEqual(self.active(digest,dict(limit=3),actor=parent.READER,command='callees')['source_count'],1)
        self.command(self.database,file=symbols_migration)
        self.assertEqual(self.active(digest,dict(limit=3,name='fixture_1'),command='callees'),calls)
        self.assert_sql_fails(self.actor(parent.READER,"SELECT storage_v2_symbol_command(2,'1','symbols','{}')"),'authorized generation')
        self.extra_intelligence_tests(digest)
        self.sql("INSERT INTO sources(id,name,type,path) VALUES (4,'late-source','fixture','synthetic/late')")
        self.assert_sql_fails(self.admin(f"SELECT storage_v2_active_intelligence_command('{digest}','card','{{\"limit\":1}}')"),'complete activated source set')


if __name__ == '__main__':
    unittest.main()
