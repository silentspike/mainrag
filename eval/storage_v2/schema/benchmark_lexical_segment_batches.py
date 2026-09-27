"""Disposable PostgreSQL measurement of old and bounded lexical writers."""

from __future__ import annotations

import json
import uuid

from eval.storage_v2.schema import test_shadow_ingest_schema as schema


class LexicalBatchBenchmark(schema.ShadowIngestSchemaTests):
    def test_old_and_new_writers_at_two_sizes(self) -> None:
        self.sql("INSERT INTO sources(id,name,type,path) VALUES "
                 "(29,'lexical-batch-benchmark','fixture','lexical-batch-benchmark')")
        content = "alpha visible"
        node, view, digest = self.make_projection(content)
        run = self.begin(29, uuid.uuid4().hex * 2, uuid.uuid4().hex * 2)
        self.stage(run, "benchmark.txt", content, node, view, digest)
        self.complete_analysis(digest)
        document = self.sql(self.admin(
            "SELECT id FROM storage_v2_put_search_document("
            f"'mainrag.lexical-simple.v1','node',{node},'{content}',ARRAY[]::TEXT[])"))
        self.sql(self.admin(f"SELECT storage_v2_bind_search_document({view},0,{document},1.0)"))
        self.commit(run, 1)
        occurrence, artifact = map(int, self.sql(
            "SELECT id::TEXT||':'||artifact_version_id::TEXT FROM occurrence "
            "WHERE source_id=29").split(":"))
        for number in (66, 67, 68, 77):
            filename = {
                66: "066_storage_v2_controlled_frontier_owner.sql",
                67: "067_storage_v2_active_ingest_receipt_owner.sql",
                68: "068_storage_v2_lexical_segments.sql",
                77: "077_storage_v2_batched_lexical_segments.sql",
            }[number]
            self.file(schema.ROOT / "migrations" / filename)
        self.sql("GRANT SELECT ON occurrence, artifact_version, "
                 "storage_v2_search_view_document, storage_v2_search_document "
                 "TO mainrag")
        self.sql("GRANT EXECUTE ON FUNCTION "
                 "storage_v2_put_lexical_segment(BIGINT,BIGINT,BIGINT,TEXT,TEXT,TEXT),"
                 "storage_v2_put_lexical_segments(BIGINT,BIGINT,BIGINT[],TEXT[],TEXT[],TEXT[]) "
                 "TO storage_v2_shadow_worker")
        measurements = []
        for count in (128, 1024):
            old_start = 10_000 + count * 10
            old = (
                "EXPLAIN (ANALYZE,FORMAT JSON) "
                f"SELECT storage_v2_put_lexical_segment({occurrence},{artifact},"
                f"n,'alpha','','text') FROM generate_series({old_start},"
                f"{old_start + count - 1}) n"
            )
            old_ms = json.loads(self.sql(self.admin(old)))[0]["Execution Time"]
            batch_count = (count + 255) // 256
            new_start = 40_000 + count * 10
            new = (
                "EXPLAIN (ANALYZE,FORMAT JSON) "
                f"SELECT storage_v2_put_lexical_segments({occurrence},{artifact},"
                f"ARRAY(SELECT n::BIGINT FROM generate_series({new_start}+b*256,"
                f"LEAST({new_start}+b*256+255,{new_start + count - 1})) n),"
                "array_fill('alpha'::TEXT,ARRAY[LEAST(256,"
                f"{count}-b*256)]),"
                f"array_fill(''::TEXT,ARRAY[LEAST(256,{count}-b*256)]),"
                f"array_fill('text'::TEXT,ARRAY[LEAST(256,{count}-b*256)])) "
                f"FROM generate_series(0,{batch_count - 1}) b"
            )
            new_ms = json.loads(self.sql(self.admin(new)))[0]["Execution Time"]
            measurements.append({"segments": count, "old_calls": count,
                                 "new_calls": batch_count,
                                 "old_execution_ms": old_ms,
                                 "new_execution_ms": new_ms})
        self.assertEqual(self.sql("SELECT count(*) FROM storage_v2_lexical_segment "
                                  "WHERE occurrence_id=" + str(occurrence)),
                         str(2 * sum((128, 1024))))
        print(json.dumps({"schema_version": "mainrag.storage-v2.lexical-batch-benchmark.v1",
                          "measurements": measurements}, sort_keys=True))


if __name__ == "__main__":
    import unittest
    unittest.main()
