"""Complete mixed-storage parity, corruption rejection and bounded UTF-8 reads."""
from __future__ import annotations

import json
import time
import unittest
import hashlib
import uuid

from eval.storage_v2.schema.test_derived_retrieval_codec import DerivedRetrievalCodecTests as base
from eval.storage_v2.schema.test_byte_located_lexical_segments import ByteLocatedLexicalTests


class BoundedLexicalVerificationTests(unittest.TestCase):
    schema = base.schema
    command = classmethod(base.command.__func__)
    sql = classmethod(base.sql.__func__)
    file = classmethod(base.file.__func__)
    quote = staticmethod(base.quote)
    actor = staticmethod(base.actor)
    admin = classmethod(base.admin.__func__)
    begin = base.begin
    stage = base.stage
    complete_analysis = base.complete_analysis
    commit = base.commit
    assert_sql_fails = base.assert_sql_fails
    snapshots = base.snapshots
    fixture = ByteLocatedLexicalTests.fixture

    def make_projection(self, content, language="text"):
        if len(content.encode()) <= 65536:
            return base.make_projection(self, content, language)
        # Real immutable concatenation, not a small component standing in for
        # the large document. Every leaf respects the public inline-body bound.
        result = self.sql(self.admin(f"""
WITH input AS (SELECT {self.quote(content)}::TEXT AS text),
pieces AS MATERIALIZED (
 SELECT n,substring(text FROM n FOR 16384) AS text FROM input
 CROSS JOIN LATERAL generate_series(1,char_length(text),16384) n
), leaves AS MATERIALIZED (
 SELECT n,leaf.id FROM pieces
 CROSS JOIN LATERAL storage_v2_put_inline_body(convert_to(text,'UTF8')) body
 CROSS JOIN LATERAL storage_v2_put_leaf_node('bounded-lexical-fixture','text',body.id) leaf
), root AS (
 SELECT value.id FROM storage_v2_put_internal_node('bounded-lexical-fixture','artifact-root',
   {len(content.encode())},ARRAY(SELECT 'part-'||n FROM leaves ORDER BY n),
   ARRAY(SELECT id FROM leaves ORDER BY n)) value
), view_row AS (
 SELECT root.id AS node_id,value.id AS view_id FROM root CROSS JOIN LATERAL
 storage_v2_put_retrieval_view('chunk','fixture-view-v1','text','fixture-tokenizer-v1',0,
  ARRAY['content'],ARRAY['node'],ARRAY[root.id],ARRAY[0::BIGINT],
  ARRAY[{len(content.encode())}::BIGINT]) value
)
SELECT node_id||':'||view_id FROM view_row;
"""))
        node, view = map(int, result.split(":"))
        return node, view, hashlib.sha256(content.encode()).hexdigest()

    @classmethod
    def setUpClass(cls):
        base.setUpClass.__func__(cls)
        try:
            old = cls.sql("SELECT pg_get_functiondef('storage_v2_verify_lexical_segments(bigint)'::REGPROCEDURE)")
            cls.sql(old.replace("FUNCTION public.storage_v2_verify_lexical_segments(",
                                "FUNCTION public.fixture_old_lexical_verifier("))
            cls.sql("ALTER FUNCTION fixture_old_lexical_verifier(BIGINT) OWNER TO mainrag_v2_frontier_owner; "
                    "REVOKE ALL ON FUNCTION fixture_old_lexical_verifier(BIGINT) FROM PUBLIC; "
                    "GRANT EXECUTE ON FUNCTION fixture_old_lexical_verifier(BIGINT) TO mainrag;")
            cls.file(next((cls.schema.ROOT / "migrations").glob("153_*.sql")))
            predecessor = cls.sql("SELECT pg_get_functiondef('storage_v2_verify_lexical_segments(bigint)'::REGPROCEDURE)")
            cls.sql(predecessor.replace("FUNCTION public.storage_v2_verify_lexical_segments(",
                                        "FUNCTION public.fixture_megabyte_lexical_verifier("))
            cls.sql("ALTER FUNCTION fixture_megabyte_lexical_verifier(BIGINT) OWNER TO mainrag_v2_frontier_owner; "
                    "REVOKE ALL ON FUNCTION fixture_megabyte_lexical_verifier(BIGINT) FROM PUBLIC; "
                    "GRANT EXECUTE ON FUNCTION fixture_megabyte_lexical_verifier(BIGINT) TO mainrag;")
            cls.file(next((cls.schema.ROOT / "migrations").glob("154_*.sql")))
            # Exercise large retained compact rows through the historical layout
            # branch of the source-backed constructor. The fixture changes only
            # its physical-format selector, preserving all canonical validation.
            constructor = cls.sql("SELECT pg_get_functiondef('storage_v2_put_lexical_segments_located"
                "(bigint,bigint,bigint[],text[],text[],text[],bigint[],bigint[])'::REGPROCEDURE)")
            selector = "IF octet_length(v_search_text)>=262144"
            if constructor.count(selector) != 1:
                raise AssertionError("one derived-format selector required")
            constructor = constructor.replace("FUNCTION public.storage_v2_put_lexical_segments_located(",
                "FUNCTION public.fixture_retained_compact_constructor(").replace(selector, "IF FALSE")
            cls.sql(constructor)
            cls.sql("ALTER FUNCTION fixture_retained_compact_constructor"
                "(BIGINT,BIGINT,BIGINT[],TEXT[],TEXT[],TEXT[],BIGINT[],BIGINT[]) OWNER TO mainrag_v2_frontier_owner; "
                "REVOKE ALL ON FUNCTION fixture_retained_compact_constructor"
                "(BIGINT,BIGINT,BIGINT[],TEXT[],TEXT[],TEXT[],BIGINT[],BIGINT[]) FROM PUBLIC; "
                "GRANT EXECUTE ON FUNCTION fixture_retained_compact_constructor"
                "(BIGINT,BIGINT,BIGINT[],TEXT[],TEXT[],TEXT[],BIGINT[],BIGINT[]) TO mainrag;")
        except BaseException:
            base.tearDownClass.__func__(cls)
            raise

    @classmethod
    def tearDownClass(cls):
        base.tearDownClass.__func__(cls)

    def projected(self, text, positions, storage, *, length=48, retained_compact=False):
        occurrence, artifact = self.fixture(text)
        generation = int(self.sql(
            "SELECT r.generation_id FROM storage_v2_ingest_run r JOIN occurrence o "
            f"ON o.source_id=r.source_id WHERE o.id={occurrence}"
        ))
        self.sql(self.admin(f"SELECT storage_v2_verify_generation({generation},'{'f' * 64}')"))
        # Construct through the supported source-backed writer, retaining its
        # character/byte validation. The flat writer explicitly tests old rows.
        for offset in range(0, len(positions), 256):
            selected = positions[offset:offset + 256]
            def array(values, kind):
                return "ARRAY[" + ",".join(str(v) if isinstance(v, int) else self.quote(v)
                                           for v in values) + f"]::{kind}[]"
            starts = [p + 1 for p in selected]
            texts = [text[p:p + length] for p in selected]
            common = (f"{occurrence},{artifact},"
                      f"{array(list(range(offset, offset + len(selected))), 'BIGINT')},"
                      f"{array(texts, 'TEXT')},{array(['context β'] * len(selected), 'TEXT')},"
                      f"{array(['text'] * len(selected), 'TEXT')},{array(starts, 'BIGINT')}")
            if storage == "flat":
                call = f"SELECT storage_v2_put_lexical_segments_at({common})"
            else:
                byte_starts = [len(text[:p].encode()) + 1 for p in selected]
                constructor = ("fixture_retained_compact_constructor" if retained_compact
                               else "storage_v2_put_lexical_segments_located")
                call = (f"SELECT {constructor}({common},"
                        f"{array(byte_starts, 'BIGINT')})")
                if storage == "compact" and not retained_compact:
                    # This public fixture is ASCII and short: the supported
                    # writer retains persisted compact vectors automatically.
                    self.assertLess(len(text.encode()), 262144)
            self.sql(self.admin(call))
        return generation, occurrence

    def verify(self, generation, old=False, *, bounded=False):
        function = "fixture_old_lexical_verifier" if old else "storage_v2_verify_lexical_segments"
        limit = "SET temp_file_limit='16MB'; SET statement_timeout='30s'; " if bounded else ""
        return json.loads(self.sql(limit + self.admin(f"SELECT {function}({generation})")))

    def test_unicode_windows_unordered_duplicates_and_all_representations_match(self):
        stride = 65536 - 4096
        for storage, text, positions in (
            ("flat", "alpha β 🙂 日本語\n" * 190000,
             [stride + 3, 0, stride - 9, 2 * stride + 1, 0, stride + 3]),
            ("compact", "alpha beta gamma delta\n" * 2000, [1000, 0, 998, 0, 1000]),
            ("derived", "alpha β 🙂 日本語\n" * 190000,
             [2 * stride + 5, 0, stride - 8, 2 * stride + 5]),
        ):
            with self.subTest(storage=storage):
                generation, occurrence = self.projected(text, positions, storage)
                relation = {"flat": "storage_v2_lexical_segment", "compact": "storage_v2_compact_lexical_block",
                            "derived": "storage_v2_derived_lexical_block"}[storage]
                self.assertGreater(int(self.sql(f"SELECT count(*) FROM {relation} WHERE occurrence_id={occurrence}")), 0)
                current = self.verify(generation, bounded=True)
                self.assertEqual(current, self.verify(generation, old=True))
                self.assertEqual(current["segment_count"], len(positions))
                self.assertEqual(current["occurrence_count"], 1)

    def test_digest_vector_and_derived_byte_corruption_are_rejected(self):
        for storage, text, changes in (
            ("flat", "alpha beta gamma\n" * 80,
             ["text_sha256=sha256(convert_to('corrupt','UTF8'))", "fts_vector=to_tsvector('simple','corrupt')",
              "text_start=100000"]),
            ("compact", "alpha beta gamma\n" * 80,
             ["text_hashes=ARRAY[sha256(convert_to('corrupt','UTF8'))]", "fts_vectors=ARRAY[to_tsvector('simple','corrupt')]"]),
            ("derived", "alpha β 🙂 gamma\n" * 25000, ["text_byte_starts=ARRAY[2]"]),
        ):
            generation, occurrence = self.projected(text, [0], storage)
            relation = {"flat": "storage_v2_lexical_segment", "compact": "storage_v2_compact_lexical_block",
                        "derived": "storage_v2_derived_lexical_block"}[storage]
            for change in changes:
                with self.subTest(storage=storage, change=change):
                    self.assert_sql_fails(f"BEGIN; ALTER TABLE {relation} DISABLE TRIGGER USER; "
                        f"UPDATE {relation} SET {change} WHERE occurrence_id={occurrence}; "
                        + self.admin(f"SELECT storage_v2_verify_lexical_segments({generation});"),
                        "lexical segment projection is incomplete")
            self.verify(generation)

    def test_missing_document_segments_and_unauthorized_generation_fail(self):
        generation, occurrence = self.projected("alpha beta\n" * 30, [0], "flat")
        for statement in (
            "ALTER TABLE storage_v2_search_view_document DISABLE TRIGGER USER; "
            f"DELETE FROM storage_v2_search_view_document WHERE view_id=(SELECT view_id FROM occurrence WHERE id={occurrence});",
            "ALTER TABLE storage_v2_lexical_segment DISABLE TRIGGER USER; "
            f"DELETE FROM storage_v2_lexical_segment WHERE occurrence_id={occurrence};",
        ):
            self.assert_sql_fails("BEGIN; " + statement + self.admin(
                f"SELECT storage_v2_verify_lexical_segments({generation});"), "lexical segment projection is incomplete")
        self.assert_sql_fails(self.actor(self.schema.OTHER_ID,
            f"SELECT storage_v2_verify_lexical_segments({generation})"), "verified authorized generation required")
        self.assert_sql_fails(self.admin("SELECT storage_v2_verify_lexical_segments(-1)"),
                              "verified authorized generation required")

    def test_empty_document_and_shared_calculation_inputs_preserve_individual_checks(self):
        empty, _ = self.projected("", [], "flat")
        self.assertEqual(self.verify(empty), self.verify(empty, old=True))
        self.assertEqual(self.verify(empty)["segment_count"], 0)
        generation, occurrence = self.projected("alpha beta gamma\n" * 30, [0, 0, 0], "flat")
        self.assertEqual(self.verify(generation)["segment_count"], 3)
        self.assert_sql_fails("BEGIN; ALTER TABLE storage_v2_lexical_segment DISABLE TRIGGER USER; "
            "UPDATE storage_v2_lexical_segment SET fts_vector=to_tsvector('simple','corrupt') "
            f"WHERE occurrence_id={occurrence} AND segment_order=1; " + self.admin(
            f"SELECT storage_v2_verify_lexical_segments({generation});"), "lexical segment projection is incomplete")
        self.assertEqual(self.verify(generation), self.verify(generation, old=True))

    def test_large_document_and_segment_count_stay_bounded_and_preserve_every_row(self):
        text = "alpha beta gamma delta\n" * 400000
        # More than one 256-row batch in the same window, plus late sparse windows.
        positions = [n * 22 for n in range(1024)] + [len(text) - 100, 1044500, 2088960]
        generation, _ = self.projected(text, positions, "flat")
        timings = {}
        for old in (True, False):
            start = time.monotonic()
            result = self.verify(generation, old=old, bounded=not old)
            timings["old" if old else "bounded"] = time.monotonic() - start
            self.assertEqual(result["segment_count"], len(positions))
            if old:
                reference = result
            else:
                self.assertEqual(result, reference)
        print("public lexical verifier seconds:", json.dumps(timings), flush=True)

    def test_dense_compact_segment_work_is_measured_independently_of_input_bytes(self):
        text = "alpha beta gamma delta\n" * 2900
        positions = [(n * 47) % 50000 for n in range(8192)]
        generation, occurrence = self.projected(text, positions, "compact", length=1500)
        self.assertEqual(self.sql("SELECT sum(cardinality(segment_orders)) "
            f"FROM storage_v2_compact_lexical_block WHERE occurrence_id={occurrence}"), "8192")
        timings = {}
        for old in (True, False):
            started = time.monotonic()
            result = self.verify(generation, old=old, bounded=not old)
            timings["old" if old else "bounded"] = time.monotonic() - started
            self.assertEqual(result["segment_count"], 8192)
            if old:
                reference = result
            else:
                self.assertEqual(result, reference)
        print("public dense segment verifier:", json.dumps({"segments":8192,
              "input_bytes":len(text.encode()), "segment_length":1500, "seconds":timings}), flush=True)

    def test_dense_unique_segments_in_large_retained_compact_documents(self):
        records = [" ".join(f"record{n:04d}term{k:03d}" for k in range(60))
                   .ljust(1000)[:1000] + "\n" for n in range(1024)]
        ascii_text = "".join(records)
        # A non-ASCII character near the end invalidates the ASCII path for the
        # entire document, even though every preceding segment begins in ASCII.
        variants = (ascii_text, ascii_text[:-2] + "β\n",
                    "".join(record[:500] + "🙂日本語" + record[504:] for record in records))
        positions = [n * 1001 for n in range(1024)]
        for variant, text in enumerate(variants):
            with self.subTest(encoding_variant=variant):
                generation, occurrence = self.projected(text, positions, "compact",
                    length=1000, retained_compact=True)
                self.assertEqual(self.sql("SELECT sum(cardinality(segment_orders)) FROM "
                    f"storage_v2_compact_lexical_block WHERE occurrence_id={occurrence}"), "1024")
                self.assertEqual(self.sql("SELECT count(*) FROM storage_v2_derived_lexical_block "
                    f"WHERE occurrence_id={occurrence}"), "0")
                started = time.monotonic()
                predecessor = json.loads(self.sql(self.admin(
                    f"SELECT fixture_megabyte_lexical_verifier({generation})")))
                predecessor_seconds = time.monotonic() - started
                started = time.monotonic()
                current = self.verify(generation, bounded=True)
                current_seconds = time.monotonic() - started
                self.assertEqual(current, predecessor)
                self.assertEqual(current["segment_count"], 1024)
                self.assert_sql_fails("BEGIN; ALTER TABLE storage_v2_compact_lexical_block "
                    "DISABLE TRIGGER USER; UPDATE storage_v2_compact_lexical_block "
                    "SET fts_vectors[cardinality(fts_vectors)]=to_tsvector('simple','corrupt') "
                    f"WHERE occurrence_id={occurrence} AND block_order=(SELECT max(block_order) "
                    f"FROM storage_v2_compact_lexical_block WHERE occurrence_id={occurrence}); "
                    + self.admin(f"SELECT storage_v2_verify_lexical_segments({generation});"),
                    "lexical segment projection is incomplete")
                print("public large retained compact verifier:", json.dumps(dict(
                    encoding_variant=variant, input_bytes=len(text.encode()), segments=1024,
                    segment_length=1000, predecessor_seconds=predecessor_seconds,
                    byte_sliced_seconds=current_seconds)), flush=True)

    def test_definer_rls_and_small_workspace_are_preserved(self):
        self.assertEqual(self.sql("SELECT proowner::REGROLE::TEXT||':'||prosecdef::TEXT||':'||"
            "has_function_privilege('mainrag',oid,'EXECUTE')::TEXT FROM pg_proc "
            "WHERE oid='storage_v2_verify_lexical_segments(bigint)'::REGPROCEDURE"),
            "mainrag_v2_frontier_owner:true:true")
        settings = self.sql("SELECT array_to_string(proconfig,',') FROM pg_proc "
            "WHERE oid='storage_v2_verify_lexical_segments(bigint)'::REGPROCEDURE")
        self.assertIn("row_security=on", settings)
        self.assertIn("work_mem=8MB", settings)

    def test_many_items_do_not_materialize_the_whole_visible_source(self):
        text = "alpha beta gamma delta\n" * 36000
        node, view, digest = self.make_projection(text)
        source = int(self.sql("INSERT INTO sources(id,name,type,path) "
            "SELECT max(id)+1,'bounded-many-items','fixture','synthetic-bounded-items' "
            "FROM sources RETURNING id"))
        run = self.begin(source, uuid.uuid4().hex * 2, uuid.uuid4().hex * 2)
        self.complete_analysis(digest)
        document = self.sql(self.admin("SELECT id FROM storage_v2_put_search_document("
            f"'mainrag.lexical-simple.v1','node',{node},{self.quote(text)},ARRAY[]::TEXT[])"))
        self.sql(self.admin(f"SELECT storage_v2_bind_search_document({view},0,{document},1.0)"))
        self.sql(self.admin(f"""DO $fixture$ DECLARE n INTEGER; BEGIN
 FOR n IN 1..384 LOOP
  PERFORM storage_v2_stage_shadow_item({run},'many-'||n,'document','synthetic-item',
   jsonb_build_object('item','many-'||n),'fixture-adapter-v1',{node},NULL,'{digest}',
   {len(text.encode())},decode('{digest}','hex'),'fixture-analysis-v1',{view},
   '/synthetic/many-'||n,'{{"byte_start":0}}'::JSONB);
 END LOOP;
END $fixture$;"""))
        self.commit(run, 384)
        generation = int(self.sql(f"SELECT generation_id FROM storage_v2_ingest_run WHERE id={run}"))
        self.sql(self.admin(f"SELECT storage_v2_verify_generation({generation},'{'f' * 64}')"))
        self.sql(self.admin(f"""DO $fixture$ DECLARE item RECORD; BEGIN
 FOR item IN SELECT id,artifact_version_id FROM occurrence WHERE source_id={source} LOOP
  PERFORM storage_v2_put_lexical_segments_at(item.id,item.artifact_version_id,
    ARRAY[0]::BIGINT[],ARRAY['alpha beta gamma delta'],ARRAY[''],ARRAY['text'],ARRAY[1]::BIGINT[]);
 END LOOP;
END $fixture$;"""))
        self.sql("SELECT pg_stat_reset();")
        started = time.monotonic()
        current = self.verify(generation, bounded=True)
        seconds = time.monotonic() - started
        self.sql("SELECT pg_stat_force_next_flush();")
        temporary = int(self.sql("SELECT temp_bytes FROM pg_stat_database WHERE datname=current_database()"))
        self.assertEqual(current["occurrence_count"], 384)
        self.assertEqual(current["segment_count"], 384)
        self.assertLess(temporary, 16 * 1024**2)
        self.assertEqual(current, self.verify(generation, old=True))
        print("public many-item verifier:", json.dumps({"items":384,
              "visible_input_bytes":384 * len(text.encode()), "bounded_seconds":seconds,
              "bounded_temp_bytes":temporary}), flush=True)


if __name__ == "__main__":
    unittest.main()
