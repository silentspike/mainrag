"""Lossless mixed lexical storage, constructor replay and source isolation."""

from __future__ import annotations

import json
import hashlib
import uuid

from eval.storage_v2.schema import test_compact_exact_postings as compact
from eval.storage_v2.schema import test_shadow_ingest_schema as schema
from eval.storage_v2.schema import test_filter_before_ranking as filter_schema
from eval.storage_v2 import test_candidate_projection_restore as restore


MIGRATION = schema.ROOT / "migrations/102_storage_v2_compact_lexical_vectors.sql"


class CompactLexicalVectorTests(filter_schema.FilterBeforeRankingTests):
    @classmethod
    def file(cls, path):
        super().file(path)
        if path == compact.MIGRATION:
            super().file(MIGRATION)

    def test_new_native_blocks_keep_exact_rows_results_and_mixed_replay(self):
        self.file(compact.MIGRATION)
        source = int(self.sql("INSERT INTO sources(id,name,type,path) "
            "SELECT max(id)+1,'compact-lexical-native','fixture','synthetic-compact-lexical' "
            "FROM sources RETURNING id"))
        fingerprints = {}
        for number in range(20000):
            term = f"lexicalcollision{number}"
            fingerprint = hashlib.sha256(term.encode()).digest()[:2]
            if fingerprint in fingerprints:
                collision = fingerprints[fingerprint], term
                break
            fingerprints[fingerprint] = term
        else:
            self.fail("deterministic lexical fingerprint collision not found")
        content = collision[0] + "\n" + "alpha βeta é🙂 gamma delta\n" * 400
        node, view, digest = self.make_projection(content)
        run = self.begin(source, uuid.uuid4().hex * 2, uuid.uuid4().hex * 2)
        self.stage(run, "native.txt", content, node, view, digest)
        self.complete_analysis(digest)
        document = self.put(node, content)
        self.sql(self.admin(f"SELECT storage_v2_bind_search_document({view},0,{document},1.0)"))
        schema.ShadowIngestSchemaTests.commit(self, run, 1)
        generation, occurrence, artifact = map(int, self.sql(
            "SELECT r.generation_id||':'||o.id||':'||o.artifact_version_id "
            f"FROM storage_v2_ingest_run r JOIN occurrence o ON o.source_id=r.source_id WHERE r.id={run}"
        ).split(":"))
        self.sql(self.admin(f"SELECT storage_v2_verify_generation({generation},'{'f3' * 32}')"))
        asts = [{"type": "term", "value": "alpha"},
                {"type": "and", "children": [{"type": "term", "value": "alpha"},
                                                {"type": "term", "value": "gamma"}]},
                {"type": "phrase", "value": "alpha βeta"},
                {"type": "or", "children": [{"type": "term", "value": "alpha"},
                                               {"type": "term", "value": "missing"}]}]
        def searches():
            return [self.exact_search(ast, source_id=source) for ast in asts]
        before = searches()
        texts = []
        start = 0
        while start < len(content):
            end = min(start + 100, len(content))
            texts.append(content[start:end])
            if end == len(content):
                break
            start = end - 10
        positions = [content.find(text) for text in texts]
        def array(values, kind):
            return "ARRAY[" + ",".join(str(v) if isinstance(v, int) else self.quote(v)
                                     for v in values) + f"]::{kind}[]"
        orders = array(list(range(len(texts))), "BIGINT")
        arguments = (f"{occurrence},{artifact},{orders},{array(texts, 'TEXT')},"
                     f"{array([''] * len(texts), 'TEXT')},{array(['text'] * len(texts), 'TEXT')}")
        located = ("SELECT storage_v2_put_lexical_segments_located(" + arguments + ","
                   + array([p + 1 for p in positions], "BIGINT") + ","
                   + array([len(content[:p].encode()) + 1 for p in positions], "BIGINT") + ")")
        writer = f"SET ROLE mainrag; SET app.user_id='{schema.ADMIN_ID}'; "
        self.assertEqual(self.sql(writer + located), str(len(texts)))
        self.assertEqual(self.sql(writer + located), str(len(texts)))
        self.assertEqual(self.sql(f"SELECT count(*) FROM storage_v2_lexical_segment "
                                 f"WHERE occurrence_id={occurrence}"), "0")
        self.assertEqual(int(self.sql("SELECT count(*) FROM storage_v2_compact_lexical_block "
                                     f"WHERE occurrence_id={occurrence}")), (len(texts) + 63) // 64)
        self.assertEqual(self.sql("SELECT count(*) FROM storage_v2_lexical_segment_all "
                                 f"WHERE occurrence_id={occurrence}"), str(len(texts)))
        db = restore.M["Database"](self.database, False)
        db.command += ["--host", str(self.socket)]
        frozen = db.query(restore.M["snapshot_statement"](source, generation))[0]
        self.assertEqual(frozen["lexical_relation"], "storage_v2_lexical_segment_all")
        projection_plan = {"original": frozen}
        self.assertEqual(db.query(restore.M["member_query"](projection_plan, 0))[0]["segment_count"], len(texts))
        reference = "SELECT count(*) FROM unnest(" + orders + "," + array(texts, "TEXT") + "," + \
            array([p + 1 for p in positions], "BIGINT") + ") expected(segment_order,text,text_start) " + \
            "FULL JOIN storage_v2_lexical_segment_all actual ON actual.occurrence_id=" + \
            str(occurrence) + " AND actual.segment_order=expected.segment_order " + \
            f"WHERE actual.occurrence_id={occurrence} AND (actual.text_start,actual.text_length," + \
            "actual.text_sha256,actual.context_prefix,actual.chunk_type,actual.fts_vector) IS DISTINCT FROM " + \
            "(expected.text_start,char_length(expected.text),sha256(convert_to(expected.text,'UTF8'))," + \
            "'','text',setweight(to_tsvector('simple',expected.text),'A')" + \
            "||setweight(to_tsvector('simple','text'),'C'))"
        self.assertEqual(self.sql(reference), "0")
        self.assertEqual(before, searches(), "native compact search envelopes differ")
        self.assertEqual(self.sql(writer + "SELECT storage_v2_put_lexical_segments(" + arguments + ")"),
                         str(len(texts)))
        self.assertEqual(self.sql(writer + "SELECT storage_v2_put_lexical_segment("
                         f"{occurrence},{artifact},0,{self.quote(texts[0])},'','text')"), "")
        self.assert_sql_fails(writer + "SELECT storage_v2_put_lexical_segment("
            f"{occurrence},{artifact},0,{self.quote(texts[0])},'changed','text')", "identity collision")
        self.assert_sql_fails(writer + located.replace(array([''] * len(texts), "TEXT"),
            array(['changed'] * len(texts), "TEXT")), "identity collision")
        self.assert_sql_fails("UPDATE storage_v2_compact_lexical_block SET fts_vectors=fts_vectors "
                              f"WHERE occurrence_id={occurrence}", "immutable")
        denied = f"SET ROLE mainrag; SET app.user_id='{schema.OTHER_ID}'; "
        self.assertEqual(self.sql(denied + "SELECT count(*) FROM storage_v2_compact_lexical_block"), "0")
        self.assertEqual(self.sql(denied + "SELECT count(*) FROM storage_v2_lexical_segment_all"), "0")
        self.assert_sql_fails(denied + located, "authorized source-backed")
        helper = ("storage_v2_authorized_lexical_matches("
                  f"ARRAY[{occurrence},{occurrence},NULL]::BIGINT[],ARRAY[{source}]::BIGINT[],")
        def matches(query, actor=schema.ADMIN_ID):
            return int(self.sql("SET ROLE mainrag_v2_frontier_owner; "
                f"SET app.user_id='{actor}'; SELECT count(*) FROM {helper}{self.quote(query)})"))
        for query in ("alpha gamma", '"alpha βeta"', "alpha OR missing", "alpha -missing"):
            expected = int(self.sql("SELECT count(*) FROM unnest(" + array(texts, "TEXT") + ") text "
                "WHERE (setweight(to_tsvector('simple',text),'A') "
                "||setweight(to_tsvector('simple','text'),'C'))@@websearch_to_tsquery('simple',"
                + self.quote(query) + ")"))
            self.assertEqual(matches(query), expected)
        self.assertEqual(matches(collision[0]), 1)
        self.assertEqual(matches(collision[1]), 0, "fingerprints must not admit a colliding full term")
        self.assertEqual(matches("alpha", schema.OTHER_ID), 0)
        proof = json.loads(self.sql(writer + f"SELECT storage_v2_verify_lexical_segments({generation})"))
        self.assertEqual(proof["segment_count"], len(texts))
        self.assertEqual(proof["invalid_count"], 0)
        self.sql("CREATE TABLE fixture_lexical_barrier(released BOOLEAN NOT NULL); "
                 "INSERT INTO fixture_lexical_barrier VALUES(FALSE); "
                 "GRANT SELECT ON fixture_lexical_barrier TO mainrag")
        for order, first_format in ((256, "compact"), (320, "flat")):
            self.sql("UPDATE fixture_lexical_barrier SET released=FALSE")
            native = ("SELECT storage_v2_put_lexical_segments_located("
                f"{occurrence},{artifact},ARRAY[{order}]::BIGINT[],ARRAY[{self.quote(texts[0])}],"
                "ARRAY[''],ARRAY['text'],ARRAY[1]::BIGINT[],ARRAY[1]::BIGINT[])")
            flat = ("SELECT storage_v2_put_lexical_segment("
                f"{occurrence},{artifact},{order},{self.quote(texts[0])},'','text')")
            first, second = (native, flat) if first_format == "compact" else (flat, native)
            winner_name = "lexical-winner-" + uuid.uuid4().hex
            loser_name = "lexical-loser-" + uuid.uuid4().hex
            winner = self.start_client(winner_name, "BEGIN; " + writer + first + "; "
                "DO $$ BEGIN LOOP EXIT WHEN (SELECT released FROM fixture_lexical_barrier); "
                "PERFORM pg_sleep(0.05); END LOOP; END $$; COMMIT;")
            loser = None
            try:
                self.wait_for_client(winner_name, "PgSleep", winner)
                loser = self.start_client(loser_name, writer + second)
                self.wait_for_client(loser_name, "advisory", loser)
            finally:
                self.sql("UPDATE fixture_lexical_barrier SET released=TRUE")
                _, error = winner.communicate(timeout=20)
                self.assertEqual(winner.returncode, 0, error)
                if loser is not None:
                    _, error = loser.communicate(timeout=20)
                    self.assertEqual(loser.returncode, 0, error)
            self.assertEqual(self.sql("SELECT count(*) FROM storage_v2_lexical_segment_all "
                f"WHERE occurrence_id={occurrence} AND segment_order={order}"), "1")
        self.assertEqual(before, searches(), "mixed constructor races changed search results")
        proof = json.loads(self.sql(writer + f"SELECT storage_v2_verify_lexical_segments({generation})"))
        self.assertEqual(proof["segment_count"], len(texts) + 2)
        self.assertEqual(proof["invalid_count"], 0)
        self.assertEqual(db.query(restore.M["member_query"](projection_plan, 0))[0]["segment_count"], len(texts) + 2)
        self.file(MIGRATION)
        self.assertEqual(before, searches())
        weakened = self.command("--command", "BEGIN; ALTER POLICY storage_v2_compact_lexical_source "
            "ON storage_v2_compact_lexical_block USING(TRUE); " +
            MIGRATION.read_text().replace("\nBEGIN;", "\n", 1).rsplit("COMMIT;", 1)[0] +
            "ROLLBACK;", check=False)
        self.assertNotEqual(weakened.returncode, 0)
        self.assertIn("compact lexical source policy differs", weakened.stderr)
        for mutation, diagnostic in (
            ("ALTER TABLE storage_v2_compact_lexical_block DISABLE TRIGGER "
             "storage_v2_compact_lexical_immutable", "immutable trigger differs"),
            ("DROP INDEX idx_storage_v2_compact_lexical_fingerprint; "
             "CREATE INDEX idx_storage_v2_compact_lexical_fingerprint "
             "ON storage_v2_compact_lexical_block(source_id)", "index identity differs"),
            ("DO $$ DECLARE name TEXT; BEGIN SELECT conname INTO STRICT name "
             "FROM pg_constraint WHERE conrelid='storage_v2_compact_lexical_block'::REGCLASS "
             "AND contype='c' AND pg_get_constraintdef(oid) LIKE '%text_hashes%'; "
             "EXECUTE format('ALTER TABLE storage_v2_compact_lexical_block DROP CONSTRAINT %I',name); "
             "END $$", "payload constraints differ"),
        ):
            drift = self.command("--command", "BEGIN; " + mutation + "; " +
                MIGRATION.read_text().replace("\nBEGIN;", "\n", 1).rsplit("COMMIT;", 1)[0] +
                "ROLLBACK;", check=False)
            self.assertNotEqual(drift.returncode, 0)
            self.assertIn(diagnostic, drift.stderr)
        self.assertIn("compact lexical source policy differs", weakened.stderr)
