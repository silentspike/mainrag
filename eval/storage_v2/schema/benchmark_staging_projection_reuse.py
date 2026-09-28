"""Measure bounded segment writes on TOAST-sized public synthetic documents."""
from __future__ import annotations
from pathlib import Path
import json
import os
import tempfile
import uuid
from eval.storage_v2.schema import test_shadow_ingest_schema as schema


class StagingProjectionBenchmark(schema.ShadowIngestSchemaTests):
    @classmethod
    def sql(cls, statement):
        # Large synthetic source witnesses exceed the OS per-argument limit.
        # Use an owned private client input file, never a server-side file read.
        with tempfile.TemporaryDirectory(prefix="mainrag-projection-sql-") as directory:
            path=Path(directory)/"input.sql"
            path.write_text(statement)
            os.chmod(path,0o600)
            return cls.command("--file",str(path)).stdout.strip()

    def test_projection_reuse_at_two_document_sizes(self):
        for filename in ("066_storage_v2_controlled_frontier_owner.sql",
                         "067_storage_v2_active_ingest_receipt_owner.sql",
                         "068_storage_v2_lexical_segments.sql",
                         "077_storage_v2_batched_lexical_segments.sql"):
            self.file(schema.ROOT / "migrations" / filename)
        self.sql("GRANT EXECUTE ON FUNCTION storage_v2_put_lexical_segments(BIGINT,BIGINT,BIGINT[],TEXT[],TEXT[],TEXT[]) TO storage_v2_shadow_worker")
        old_writer=self.sql("SELECT pg_get_functiondef('storage_v2_put_lexical_segments(bigint,bigint,bigint[],text[],text[],text[])'::regprocedure)")
        old_copy=self.sql("SELECT pg_get_functiondef('storage_v2_copy_legacy_lexical_segments(bigint,bigint)'::regprocedure)")
        measurements=[]
        for index,count in enumerate((64,1024)):
            sid=30+index
            self.sql(f"INSERT INTO sources(id,name,type,path) VALUES ({sid},'projection-fixture-{index}','fixture','projection-fixture-{index}')")
            self.sql(f"CREATE TABLE projection_input_{index} AS SELECT n, lpad(n::TEXT,8,'0')||repeat(' é🙂fixture',100)||E'\n' AS segment FROM generate_series(1,{count}) n")
            self.sql(f"CREATE TABLE projection_body_{index} AS SELECT string_agg(segment,'' ORDER BY n) AS text FROM projection_input_{index}")
            self.sql(f"GRANT SELECT ON projection_input_{index},projection_body_{index} TO mainrag,storage_v2_shadow_worker")
            projection=self.sql(self.admin(f"""
WITH parts AS MATERIALIZED (
 SELECT substring(text FROM n FOR 32768) AS text,n
 FROM projection_body_{index} CROSS JOIN LATERAL generate_series(1,char_length(text),32768) n
), leaves AS MATERIALIZED (
 SELECT leaf.id,body.logical_length,parts.n FROM parts CROSS JOIN LATERAL
 storage_v2_put_inline_body(convert_to(parts.text,'UTF8')) body CROSS JOIN LATERAL
 storage_v2_put_leaf_node('shadow-fixture','text',body.id) leaf
), root AS MATERIALIZED (
 SELECT node.id,node.logical_length FROM
 (SELECT sum(logical_length)::BIGINT AS length,array_agg('content'::TEXT ORDER BY n) AS kinds,array_agg(id ORDER BY n) AS ids FROM leaves) input
 CROSS JOIN LATERAL storage_v2_put_internal_node('shadow-fixture','text',input.length,input.kinds,input.ids) node
), view_row AS MATERIALIZED (
 SELECT root.id AS node_id,view_value.id,root.logical_length FROM root CROSS JOIN LATERAL
 storage_v2_put_retrieval_view('chunk','fixture-view-v1','text','fixture-tokenizer-v1',0,ARRAY['content'],ARRAY['node'],ARRAY[root.id],ARRAY[0::BIGINT],ARRAY[root.logical_length]) view_value
)
SELECT node_id::TEXT||':'||id::TEXT||':'||encode(sha256(convert_to(text,'UTF8')),'hex') FROM view_row CROSS JOIN projection_body_{index}
"""))
            node,view,digest=projection.split(':');node=int(node);view=int(view)
            content=''.join(str(n).zfill(8)+' é🙂fixture'*100+'\n' for n in range(1,count+1))
            run=self.begin(sid,uuid.uuid4().hex*2,uuid.uuid4().hex*2)
            self.stage(run,'projection.txt',content,node,view,digest)
            self.complete_analysis(digest)
            document=self.sql(self.admin(f"SELECT document.id FROM projection_body_{index} CROSS JOIN LATERAL storage_v2_put_search_document('mainrag.lexical-simple.v1','node',{node},text,ARRAY[]::TEXT[]) document"))
            self.sql(self.admin(f"SELECT storage_v2_bind_search_document({view},0,{document},1.0)"))
            self.commit(run,1)
            occurrence,artifact=map(int,self.sql(f"SELECT id::TEXT||':'||artifact_version_id::TEXT FROM occurrence WHERE source_id={sid}").split(':'))
            self.sql("GRANT SELECT ON occurrence,artifact_version,storage_v2_search_view_document,storage_v2_search_document TO mainrag")
            def measure(offset,positioned=False):
                function="storage_v2_put_lexical_segments_at" if positioned else "storage_v2_put_lexical_segments"
                positions=(f",ARRAY(SELECT ((n-1)*char_length(segment)+1)::BIGINT FROM projection_input_{index} WHERE (n-1)/256=b ORDER BY n)" if positioned else "")
                statement=f"""EXPLAIN (ANALYZE,FORMAT JSON)
SELECT {function}({occurrence},{artifact},
 ARRAY(SELECT (n+{offset})::BIGINT FROM projection_input_{index} WHERE (n-1)/256=b ORDER BY n),
 ARRAY(SELECT segment FROM projection_input_{index} WHERE (n-1)/256=b ORDER BY n),
 array_fill('context Ω'::TEXT,ARRAY[LEAST(256,{count}-b*256)]),
 array_fill('text'::TEXT,ARRAY[LEAST(256,{count}-b*256)]){positions}) FROM generate_series(0,({count}-1)/256) b"""
                return json.loads(self.sql(self.admin(statement)))[0]['Execution Time']
            old_ms=measure(0)
            self.file(schema.ROOT / 'migrations/083_storage_v2_staging_projection_reuse.sql')
            new_ms=measure(10000)
            self.file(schema.ROOT / 'migrations/085_storage_v2_positioned_lexical_segments.sql')
            self.sql("GRANT EXECUTE ON FUNCTION storage_v2_put_lexical_segments_at(BIGINT,BIGINT,BIGINT[],TEXT[],TEXT[],TEXT[],BIGINT[]) TO storage_v2_shadow_worker")
            positioned_ms=measure(20000,True)
            equality=self.sql(f"SELECT bool_and((old.text_start,old.text_length,old.text_sha256,old.context_prefix,old.chunk_type,old.fts_vector) IS NOT DISTINCT FROM (new.text_start,new.text_length,new.text_sha256,new.context_prefix,new.chunk_type,new.fts_vector)) FROM storage_v2_lexical_segment old JOIN storage_v2_lexical_segment new ON new.occurrence_id=old.occurrence_id AND new.segment_order=old.segment_order+10000 WHERE old.occurrence_id={occurrence} AND old.segment_order<10000")
            self.assertEqual(equality,'t')
            fast_equality=self.sql(f"SELECT bool_and((old.text_start,old.text_length,old.text_sha256,old.context_prefix,old.chunk_type,old.fts_vector) IS NOT DISTINCT FROM (new.text_start,new.text_length,new.text_sha256,new.context_prefix,new.chunk_type,new.fts_vector)) FROM storage_v2_lexical_segment old JOIN storage_v2_lexical_segment new ON new.occurrence_id=old.occurrence_id AND new.segment_order=old.segment_order+20000 WHERE old.occurrence_id={occurrence} AND old.segment_order<10000")
            self.assertEqual(fast_equality,'t')

            measurements.append({'segments':count,'input_bytes':len(content.encode()),'batch_calls':(count+255)//256,'previous_ms':old_ms,'projection_reuse_ms':new_ms,'positioned_ms':positioned_ms,'identity_equal':True})
            self.sql(old_writer);self.sql(old_copy)
        print(json.dumps({'schema_version':'mainrag.storage-v2.staging-projection-benchmark.v1','measurements':measurements},sort_keys=True))
