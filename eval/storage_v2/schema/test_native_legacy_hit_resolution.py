"""Native old-ID resolution, retained roots and atomic source-scoped mapping batches."""
import json
import hashlib
import unittest
from eval.storage_v2.schema import test_bound_query_and_presence_work as previous

MIGRATION=previous.previous.ROOT/'migrations/134_storage_v2_native_legacy_hit_resolution.sql'

class NativeLegacyHitResolutionTests(unittest.TestCase):
    schema=previous.BoundQueryAndPresenceTests.schema
    command=classmethod(previous.BoundQueryAndPresenceTests.command.__func__)
    sql=classmethod(previous.BoundQueryAndPresenceTests.sql.__func__)
    file=classmethod(previous.BoundQueryAndPresenceTests.file.__func__)
    admin=classmethod(previous.BoundQueryAndPresenceTests.admin.__func__)
    actor=staticmethod(previous.BoundQueryAndPresenceTests.actor)
    quote=staticmethod(previous.BoundQueryAndPresenceTests.quote)
    make_projection=previous.BoundQueryAndPresenceTests.make_projection
    begin=previous.BoundQueryAndPresenceTests.begin
    stage=previous.BoundQueryAndPresenceTests.stage
    complete_analysis=previous.BoundQueryAndPresenceTests.complete_analysis
    assert_sql_fails=previous.BoundQueryAndPresenceTests.assert_sql_fails

    @classmethod
    def setUpClass(cls):
        try:
            previous.BoundQueryAndPresenceTests.setUpClass.__func__(cls)
            cls.file(previous.MIGRATION)
            cls.file(MIGRATION)
        except BaseException:
            if hasattr(cls,'stack'):cls.stack.close()
            raise

    @classmethod
    def tearDownClass(cls):
        previous.BoundQueryAndPresenceTests.tearDownClass.__func__(cls)

    def projection(self,run,path,text):
        node,view,digest=self.make_projection(text)
        # Produce the complete locator emitted by the native source builder;
        # the older minimal staging fixture supplies only byte_start.
        locator=json.dumps(dict(byte_start=0,byte_end=len(text.encode()),fragmented=False))
        self.sql(self.admin(f"SELECT (storage_v2_stage_shadow_item({run},{self.quote(path)},'document','synthetic-item',"
            f"'{json.dumps({'item':path})}'::JSONB,'fixture-adapter-v1',{node},NULL,'{digest}',{len(text.encode())},"
            f"decode('{digest}','hex'),'fixture-analysis-v1',{view},{self.quote('/synthetic/'+path)},{self.quote(locator)}::JSONB)).source_item_id"))
        self.complete_analysis(digest)
        return int(self.sql(f"SELECT id FROM occurrence WHERE view_id={view} AND source_path='/synthetic/{path}' ORDER BY id DESC LIMIT 1"))

    def commit(self,run,count):
        previous.BoundQueryAndPresenceTests.commit(self,run,count)
        # Fixture roots are sealed through the real ingest function, then put in
        # the verified state required by retained-history resolution.
        self.sql(self.admin(f"SELECT storage_v2_verify_generation((SELECT generation_id FROM storage_v2_ingest_run WHERE id={run}),repeat('c',64))"))

    def batch(self,source,records,user=None):
        statement=f'SELECT storage_v2_replace_legacy_hit_mappings({source},{self.quote(json.dumps(records))}::JSONB)'
        return json.loads(self.sql(self.actor(user or self.schema.ADMIN_ID,statement)))

    def record(self,source,hit,ids,overlaps=None,offsets=None,kind='exact'):
        state=json.loads(self.sql(self.admin(f"SELECT storage_v2_legacy_hit_mapping_states({source},ARRAY[{self.quote(hit)}])")))
        return dict(old_hit_id=hit,occurrence_ids=ids,relation_kind=kind,
            byte_overlaps=overlaps or [1]*len(ids),source_offsets=offsets or [0]*len(ids),
            expected_mapping_sha256=state['mappings'][0]['expected_mapping_sha256'])

    def resolve(self,hit,sequence='2',source=6,user=None):
        return json.loads(self.sql(self.actor(user or self.schema.ADMIN_ID,
            f"SELECT storage_v2_resolve_legacy_hit({source},{self.quote(sequence)},{self.quote(hit)})")))

    def preserve(self,hit,body,proof,source=6,expected=None,user=None):
        if expected is None:
            expected=json.loads(self.sql(self.admin(f"SELECT storage_v2_legacy_hit_mapping_states({source},ARRAY[{self.quote(hit)}])")))['mappings'][0]['expected_mapping_sha256']
        statement=f"SELECT storage_v2_preserve_legacy_hit({source},{self.quote(hit)},{body},{self.quote(json.dumps(proof))}::JSONB,{self.quote(expected)})"
        return int(self.sql(self.actor(user or self.schema.ADMIN_ID,statement)))

    def test_a_complete_resolution_after_legacy_removal(self):
        run=self.begin(6,'a1'*32,'b1'*32)
        old=self.projection(run,'legacy-alpha.txt','alpha Über🙂')
        stable=self.projection(run,'legacy-beta.txt','beta 東京')
        self.commit(run,2)
        records=[self.record(6,'legacy-split',[old,stable],[10,5],[0,10],'split'),
            self.record(6,'legacy-old-only',[old]),self.record(6,'legacy-merged-a',[stable],kind='merged'),
            self.record(6,'legacy-merged-b',[stable],kind='merged')]
        applied=self.batch(6,records)
        self.assertEqual((applied['hit_count'],applied['target_count']),(4,5))
        run=self.begin(6,'a2'*32,'b2'*32)
        self.projection(run,'legacy-alpha.txt','changed alpha')
        self.projection(run,'legacy-beta.txt','beta 東京')
        self.commit(run,2)
        current=self.resolve('legacy-split')
        self.assertEqual(current['primary_ordinal'],1)
        self.assertEqual(current['resolution_scope'],'selected_generation')
        self.assertEqual([t['occurrence_id'] for t in current['targets']],[stable])
        historical=self.resolve('legacy-old-only')
        self.assertEqual(historical['resolution_scope'],'retained_history')
        self.assertEqual(historical['targets'][0]['occurrence_id'],old)
        self.assertEqual(historical['targets'][0]['target_generation_seq'],1)
        self.assertEqual(historical['primary_ordinal'],0)
        original=self.resolve('legacy-split','1')
        self.assertEqual([t['occurrence_id'] for t in original['targets']],[old,stable])
        self.assertEqual(original['primary_ordinal'],0)
        merged=[self.resolve(hit) for hit in ['legacy-merged-a','legacy-merged-b']]
        self.assertEqual(merged[0]['targets'][0]['external_hit_id'],merged[1]['targets'][0]['external_hit_id'])
        self.assertEqual(merged[0]['targets'][0]['relation_kind'],'merged')
        missing=self.resolve('unknown-id')
        self.assertEqual((missing['resolution_scope'],missing['primary_ordinal'],missing['targets']),('unresolved',None,[]))
        run=self.begin(6,'a3'*32,'b3'*32)
        unpublished=self.projection(run,'unpublished.txt','pending content')
        self.batch(6,[self.record(6,'not-retained',[unpublished])])
        self.assertEqual(self.resolve('not-retained')['targets'],[])
        for user in [self.schema.OTHER_ID]:
            self.assert_sql_fails(self.actor(user,"SELECT storage_v2_resolve_legacy_hit(6,'2','legacy-old-only')"),'authorized generation selector')
        self.assertEqual(self.resolve('legacy-old-only',user=self.schema.WRITER_ID),historical)
        before={hit:self.resolve(hit) for hit in ['legacy-split','legacy-old-only','legacy-merged-a','legacy-merged-b','unknown-id','not-retained']}
        self.sql('DROP TABLE chunks CASCADE; DROP TABLE files CASCADE;')
        self.assertEqual(before,{hit:self.resolve(hit) for hit in before})
        self.assertEqual(self.sql('SELECT count(*) FROM legacy_hit_mapping'),'6')

    def test_c_native_history_is_idempotent_and_independent_of_removed_legacy(self):
        # Test a already removed the actual legacy tables. Preservation and
        # resolution retain no dependency on those tables, even on retries.
        self.assertEqual(self.sql("SELECT to_regclass('chunks') IS NULL AND to_regclass('files') IS NULL"),'t')
        text='original Über🙂 東京'
        body=int(self.sql(self.admin(f"SELECT id FROM storage_v2_put_inline_body(convert_to({self.quote(text)},'UTF8'))")))
        proof=dict(chunk_sha256=hashlib.sha256(text.encode()).hexdigest(),file_sha256='f'*64,
            logical_bytes=len(text.encode()),source_path='/synthetic/deleted.txt',start_line=4,end_line=5)
        before=self.sql("SELECT jsonb_build_array((SELECT count(*) FROM source_generation),"
            "(SELECT count(*) FROM generation_item_version),(SELECT count(*) FROM logical_source WHERE active_generation_id IS NOT NULL))")
        target=self.preserve('preserved-history',body,proof)
        envelope=self.resolve('preserved-history')
        self.assertEqual(envelope['resolution_scope'],'retained_legacy_hit')
        self.assertEqual(envelope['targets'][0]['occurrence_id'],target)
        self.assertIsNone(envelope['targets'][0]['target_generation_id'])
        self.assertEqual(envelope['targets'][0]['locator']['byte_scope'],'legacy_chunk')
        self.assertEqual(envelope['targets'][0]['byte_overlap'],len(text.encode()))
        mapping_tuple=self.sql("SELECT ctid FROM legacy_hit_mapping WHERE old_hit_id='preserved-history'")
        self.assertEqual(self.preserve('preserved-history',body,proof),target)
        self.assertEqual(mapping_tuple,self.sql("SELECT ctid FROM legacy_hit_mapping WHERE old_hit_id='preserved-history'"))
        self.assertEqual(envelope,self.resolve('preserved-history',user=self.schema.WRITER_ID))
        self.assertEqual(before,self.sql("SELECT jsonb_build_array((SELECT count(*) FROM source_generation),"
            "(SELECT count(*) FROM generation_item_version),(SELECT count(*) FROM logical_source WHERE active_generation_id IS NOT NULL))"))
        self.assertEqual(self.sql("SELECT count(*) FROM source_item WHERE item_kind='legacy-hit-history'"),'1')
        wrong=dict(proof,chunk_sha256='a'*64)
        for change,message in [(wrong,'native body differs'),(dict(proof,end_line=3),'proof shape differs'),
            (dict(proof,logical_bytes=None),'proof shape differs'),(dict(proof,start_line=-1),'proof shape differs')]:
            statement=f"SELECT storage_v2_preserve_legacy_hit(6,'invalid-history',{body},{self.quote(json.dumps(change))}::JSONB,repeat('a',64))"
            self.assert_sql_fails(self.admin(statement),message)
        current=self.record(6,'preserved-history',[target])['expected_mapping_sha256']
        wrong_path=dict(proof,source_path='/synthetic/forged.txt')
        self.assert_sql_fails(self.admin(f"SELECT storage_v2_preserve_legacy_hit(6,'preserved-history',{body},{self.quote(json.dumps(wrong_path))}::JSONB,{self.quote(current)})"),'immutable identity drifted')
        self.assert_sql_fails(self.admin(f"SELECT storage_v2_preserve_legacy_hit(6,'preserved-history',{body},{self.quote(json.dumps(proof))}::JSONB,repeat('0',64))"),'mapping state drifted')
        self.assert_sql_fails(self.actor(self.schema.WRITER_ID,f"SELECT storage_v2_preserve_legacy_hit(6,'unauthorized',{body},{self.quote(json.dumps(proof))}::JSONB,repeat('0',64))"),'authorized administrator')
        self.assert_sql_fails(self.admin("UPDATE storage_v2_legacy_hit_history SET source_id=9 WHERE old_hit_id='preserved-history'"),'permission denied')
        self.assert_sql_fails("UPDATE storage_v2_legacy_hit_history SET source_id=9 WHERE old_hit_id='preserved-history'",'immutable')
        self.assert_sql_fails(self.actor(self.schema.OTHER_ID,"SELECT storage_v2_resolve_legacy_hit(6,'2','preserved-history')"),'authorized generation selector')
        self.assertEqual(self.sql(self.actor(self.schema.OTHER_ID,'SELECT count(*) FROM storage_v2_legacy_hit_history')),'0')
        self.assertEqual(envelope,self.resolve('preserved-history'))

    def test_b_atomic_validation_source_isolation_and_drift(self):
        run=self.begin(9,'a9'*32,'b9'*32)
        foreign=self.projection(run,'foreign.txt','foreign')
        self.commit(run,1)
        target=int(self.sql("SELECT min(id) FROM occurrence WHERE source_id=6"))
        initial=self.record(6,'batch-preserved',[target])
        self.batch(6,[initial])
        digest=self.sql("SELECT encode(sha256(convert_to(jsonb_agg(to_jsonb(mapping) ORDER BY old_hit_id,ordinal)::TEXT,'UTF8')),'hex') FROM legacy_hit_mapping mapping")
        good=self.record(6,'batch-new',[target])
        bad=self.record(6,'batch-bad',[target])
        for mutation,error in [
            ({'occurrence_ids':[foreign]},'target source differs'),
            ({'byte_overlaps':[None]},'integer coordinates'),
            ({'source_offsets':[-1]},'integer coordinates'),
            ({'source_offsets':[]},'integer coordinates'),
            ({'occurrence_ids':[target,target],'byte_overlaps':[1,1],'source_offsets':[0,0],'relation_kind':'split'},'targets must be unique'),
            ({'expected_mapping_sha256':'0'*64},'state drifted'),
            ({'relation_kind':None},'record shape differs'),
        ]:
            records=[good,{**bad,**mutation}]
            self.assert_sql_fails(self.admin(f'SELECT storage_v2_replace_legacy_hit_mappings(6,{self.quote(json.dumps(records))}::JSONB)'),error)
            self.assertEqual(self.sql("SELECT encode(sha256(convert_to(jsonb_agg(to_jsonb(mapping) ORDER BY old_hit_id,ordinal)::TEXT,'UTF8')),'hex') FROM legacy_hit_mapping mapping"),digest)
        self.assert_sql_fails(self.admin(f"SELECT storage_v2_replace_legacy_hit_mapping('batch-preserved',ARRAY[{foreign}], 'exact',ARRAY[1]::BIGINT[],ARRAY[0]::BIGINT[])"),'existing source differs')
        for coordinates in ['ARRAY[NULL]::BIGINT[],ARRAY[0]::BIGINT[]','ARRAY[1]::BIGINT[],ARRAY[NULL]::BIGINT[]']:
            self.assert_sql_fails(self.admin(f"SELECT storage_v2_replace_legacy_hit_mapping('batch-null',ARRAY[{target}], 'exact',{coordinates})"),'invalid legacy mapping input')
        self.assert_sql_fails(self.actor(self.schema.WRITER_ID,f"SELECT storage_v2_replace_legacy_hit_mappings(6,{self.quote(json.dumps([good]))}::JSONB)"),'authorized administrator')
        self.assert_sql_fails(self.admin(f"SELECT storage_v2_legacy_hit_mapping_states(9,ARRAY['batch-preserved'])"),'state source differs')
        self.assert_sql_fails(self.admin(f'SELECT storage_v2_replace_legacy_hit_mappings(6,{self.quote(json.dumps([good,good]))}::JSONB)'),'identities must be unique')
        self.assert_sql_fails(self.admin("SELECT storage_v2_replace_legacy_hit_mappings(6,'[]'::JSONB)"),'bounded nonempty')
        applied=self.batch(6,[good])
        self.assertEqual(applied['hit_count'],1)
        self.assert_sql_fails(self.admin(f'SELECT storage_v2_replace_legacy_hit_mappings(6,{self.quote(json.dumps([good]))}::JSONB)'),'state drifted')
        self.assertEqual(self.resolve('batch-new')['resolution_scope'],'retained_history')

    def test_d_atomic_proofs_resume_invalidate_and_survive_cleanup(self):
        run=self.begin(3,'d1'*32,'d2'*32)
        text='alpha Über🙂 beta'
        target=self.projection(run,'proof.txt',text)
        self.commit(run,1)
        generation=int(self.sql(f'SELECT generation_id FROM storage_v2_ingest_run WHERE id={run}'))
        digest=hashlib.sha256(text.encode()).hexdigest()
        path='/synthetic/proof.txt'
        native_proof=dict(chunk_sha256=hashlib.sha256(b'alpha').hexdigest(),file_sha256=digest,file_id=700,file_revision=0,
            source_path=path,logical_bytes=5,start_line=1,end_line=1,native_file_sha256=digest,byte_start=0,byte_end=5)
        old='retained original 東京'
        body=int(self.sql(self.admin(f"SELECT id FROM storage_v2_put_inline_body(convert_to({self.quote(old)},'UTF8'))")))
        history_proof=dict(chunk_sha256=hashlib.sha256(old.encode()).hexdigest(),file_sha256=digest,file_id=700,file_revision=0,
            source_path=path,logical_bytes=len(old.encode()),start_line=2,end_line=2)
        native=dict(mapping=self.record(3,'70001',[target],[5],[0]),proof=native_proof,history_body_id=None)
        history=dict(mapping=self.record(3,'70002',[target]),proof=history_proof,history_body_id=body)
        def apply(records):
            return json.loads(self.sql(self.admin(f'SELECT storage_v2_complete_legacy_hit_batch(3,{generation},{self.quote(json.dumps(records))}::JSONB)')))
        result=apply([native,history])
        self.assertEqual((result['hit_count'],result['historical_hits'],result['native_targets']),(2,1,1))
        proofs=self.sql('SELECT jsonb_agg(jsonb_build_array(old_hit_id,ctid,mapping_sha256) ORDER BY old_hit_id) FROM storage_v2_legacy_hit_proof')
        native['mapping']=self.record(3,'70001',[target],[5],[0])
        history['mapping']=self.record(3,'70002',[target])
        replay=apply([native,history])
        self.assertEqual((replay['historical_hits'],replay['native_targets']),(0,0))
        self.assertEqual(proofs,self.sql('SELECT jsonb_agg(jsonb_build_array(old_hit_id,ctid,mapping_sha256) ORDER BY old_hit_id) FROM storage_v2_legacy_hit_proof'))
        self.sql('CREATE TABLE files(id BIGINT PRIMARY KEY,source_id BIGINT,path TEXT,hash BYTEA); '
            'CREATE TABLE chunks(id BIGINT PRIMARY KEY,file_id BIGINT,content_hash BYTEA,start_line INT,end_line INT); '
            'ALTER TABLE files OWNER TO mainrag; ALTER TABLE chunks OWNER TO mainrag;')
        self.sql(f"INSERT INTO files VALUES(700,3,{self.quote(path)},decode('{digest}','hex')); "
            f"INSERT INTO chunks VALUES(70001,700,decode('{native_proof['chunk_sha256']}','hex'),1,1),"
            f"(70002,700,decode('{history_proof['chunk_sha256']}','hex'),2,2)")
        inventory=lambda: json.loads(self.sql(self.admin(f'SELECT storage_v2_legacy_hit_inventory(3,{generation},0)')))
        self.assertEqual(inventory()['files'][0]['completed_hits'],2)
        self.batch(3,[self.record(3,'70001',[target],[1],[0])])
        self.assertEqual(self.sql("SELECT count(*) FROM storage_v2_legacy_hit_proof WHERE old_hit_id='70001'"),'0')
        self.assertEqual(inventory()['files'][0]['completed_hits'],1)
        native['mapping']=self.record(3,'70001',[target],[5],[0])
        apply([native,history])
        self.assertEqual(inventory()['files'][0]['completed_hits'],2)
        # Any old-file/chunk mutation invalidates coverage, even if the stored
        # hash/path/line columns remain unchanged. A new inventory cannot skip
        # the byte verification based on an earlier completion proof.
        self.sql('INSERT INTO storage_v2_legacy_rank_revision VALUES(700,1)')
        self.assertEqual(inventory()['files'][0]['completed_hits'],0)
        native_proof['file_revision']=history_proof['file_revision']=1
        native['mapping']=self.record(3,'70001',[target],[5],[0])
        history['mapping']=self.record(3,'70002',[target])
        apply([native,history])
        self.assertEqual(inventory()['files'][0]['completed_hits'],2)
        bad=dict(mapping=self.record(3,'70004',[target],[5],[1]),proof=native_proof,history_body_id=None)
        new_history=dict(mapping=self.record(3,'70003',[target]),proof=history_proof,history_body_id=body)
        self.assert_sql_fails(self.admin(f'SELECT storage_v2_complete_legacy_hit_batch(3,{generation},{self.quote(json.dumps([new_history,bad]))}::JSONB)'), 'coverage or generation differs')
        self.assertEqual(self.sql("SELECT count(*) FROM storage_v2_legacy_hit_history WHERE old_hit_id='70003'"),'0')
        self.assertEqual(self.sql("SELECT count(*) FROM legacy_hit_mapping WHERE old_hit_id='70003'"),'0')
        envelopes={hit:self.resolve(hit,'1',source=3) for hit in ['70001','70002']}
        self.assertEqual(envelopes['70001']['resolution_scope'],'selected_generation')
        self.assertEqual(envelopes['70002']['resolution_scope'],'retained_legacy_hit')
        self.sql('DROP TABLE chunks CASCADE; DROP TABLE files CASCADE;')
        self.assertEqual(envelopes,{hit:self.resolve(hit,'1',source=3) for hit in envelopes})
        # Replacing the live mapping does not authorize collection of the
        # immutable original history anchor. Exercise the real native GC SQL.
        self.batch(3,[self.record(3,'70002',[target],[5],[0])])
        from eval.storage_v2.test_cleanup_plan import cleanup
        without=json.loads(self.sql(cleanup.reachability_sql((generation,),False)))
        retained=json.loads(self.sql(cleanup.reachability_sql((generation,),False,True)))
        self.assertEqual(retained['body_count'],without['body_count']+1)

if __name__=='__main__':unittest.main()
