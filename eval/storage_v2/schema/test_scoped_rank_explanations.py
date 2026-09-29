"""Exact mixed ranking and source-backed offset guards after deferred scoring."""

from __future__ import annotations

from eval.storage_v2.schema import test_shadow_ingest_schema as schema
from eval.storage_v2.schema import test_compact_exact_postings as compact
from eval.storage_v2.schema import test_filter_before_ranking as filtered
from eval.storage_v2.schema import test_compact_lexical_vectors as vectors


MIGRATION = schema.ROOT / "migrations/103_storage_v2_scoped_rank_explanations.sql"
DIRECT_POSTING_MIGRATION = schema.ROOT / "migrations/104_storage_v2_direct_scoped_postings.sql"


class ScopedRankExplanationTests(filtered.FilterBeforeRankingTests):
    reader_installed = False

    @classmethod
    def file(cls, path):
        if path == compact.MIGRATION and cls.reader_installed:
            # Replaying the old posting constructor must not rewind the later
            # reader migrations that this fixture already installed in order.
            compact.CompactExactPostingTests.file.__func__(cls, path)
            super().file(MIGRATION)
            return
        super().file(path)
        if path == compact.MIGRATION:
            super().file(vectors.MIGRATION)
            super().file(MIGRATION)
            cls.reader_installed = True

    def test_complete_mixed_search_and_new_constructor_contract(self):
        # Keep the independent pre-migration envelopes, copied/generated ranks,
        # optional stages, collision checks and constructor races. The parent's
        # final migration-101 drift probe belongs to its earlier reader layout.
        compact.CompactExactPostingTests.test_complete_mixed_search_and_new_constructor_contract(self)
        small = "ARRAY(SELECT id FROM occurrence WHERE source_id=6 ORDER BY id)"
        broad = ("ARRAY(SELECT occurrence.id FROM occurrence CROSS JOIN generate_series(1,11000) "
                 "WHERE source_id=6 UNION ALL SELECT NULL::BIGINT)")
        admin = f"SET ROLE mainrag_v2_frontier_owner; SET app.user_id='{schema.ADMIN_ID}'; "
        denied = f"SET ROLE mainrag_v2_frontier_owner; SET app.user_id='{schema.OTHER_ID}'; "
        for helper in ("storage_v2_source_segment_ranks", "storage_v2_source_segment_ranks_precise"):
            query = "SELECT coalesce(jsonb_agg(to_jsonb(rank) ORDER BY occurrence_id),'[]'::JSONB) "
            expected = self.sql(admin + query + f"FROM {helper}({small},'alpha') rank")
            self.assertEqual(self.sql(admin + query + f"FROM {helper}({broad},'alpha') rank"), expected)
            self.assertEqual(self.sql(denied + f"SELECT count(*) FROM {helper}({broad},'alpha')"), "0")
        self.file(MIGRATION)
        queries = (
            {"type": "term", "value": "alpha"},
            {"type": "term", "value": "common"},
            {"type": "and", "children": [
                {"type": "term", "value": "alpha"}, {"type": "term", "value": "beta"}]},
            {"type": "term", "value": "absent_fixture"},
        )
        filters = ({}, {"path_prefix": "/synthetic/compact-0"})
        before = [self.exact_search(ast, selected, user_id=user)
                  for user in (schema.ADMIN_ID, schema.WRITER_ID)
                  for ast in queries for selected in filters]
        identities = self.sql("SELECT count(*) FROM storage_v2_search_document; "
                              "SELECT count(*) FROM storage_v2_search_posting; "
                              "SELECT count(*) FROM storage_v2_compact_posting_block; "
                              "SELECT count(*) FROM occurrence")
        self.file(DIRECT_POSTING_MIGRATION)
        self.file(DIRECT_POSTING_MIGRATION)
        self.assertEqual(identities, self.sql(
            "SELECT count(*) FROM storage_v2_search_document; "
            "SELECT count(*) FROM storage_v2_search_posting; "
            "SELECT count(*) FROM storage_v2_compact_posting_block; "
            "SELECT count(*) FROM occurrence"))
        self.assertEqual(before, [self.exact_search(ast, selected, user_id=user)
                                  for user in (schema.ADMIN_ID, schema.WRITER_ID)
                                  for ast in queries for selected in filters])

    def test_canonical_layouts_and_offset_only_source_guards(self):
        self.file(compact.MIGRATION)
        content = "alpha βeta gamma"
        node, view, digest = self.make_projection(content)
        document = self.put(node, content)
        self.sql(self.admin(f"SELECT storage_v2_bind_search_document({view},0,{document},1.0)"))
        run = self.begin(6, "d1" * 32, "d2" * 32, commit_sha=compact.COMMIT)
        for path in ("flat.txt", "compact.txt", "offset.txt"):
            self.stage(run, path, content, node, view, digest)
        self.complete_analysis(digest)
        schema.ShadowIngestSchemaTests.commit(self, run, 3)
        occurrences = dict(line.split(":") for line in self.sql(
            "SELECT source_path||':'||id FROM occurrence WHERE source_id=6 ORDER BY id"
        ).splitlines())
        writer = f"SET ROLE mainrag; SET app.user_id='{schema.ADMIN_ID}'; "
        for path, order in (("flat.txt", 0), ("compact.txt", 0), ("offset.txt", 1)):
            occurrence = int(occurrences["/synthetic/" + path])
            artifact = int(self.sql(f"SELECT artifact_version_id FROM occurrence WHERE id={occurrence}"))
            if path == "flat.txt":
                self.sql(writer + "SELECT storage_v2_put_lexical_segment("
                    f"{occurrence},{artifact},0,{self.quote(content)},'','text')")
            else:
                self.sql(writer + "SELECT storage_v2_put_lexical_segments_located("
                    f"{occurrence},{artifact},ARRAY[{order}]::BIGINT[],ARRAY[{self.quote(content)}],"
                    "ARRAY[''],ARRAY['text'],ARRAY[1]::BIGINT[],ARRAY[1]::BIGINT[])")
            call = f"SELECT storage_v2_source_legacy_segment_matches({occurrence},'alpha')"
            self.assertEqual(self.sql(writer + call), "t" if path == "offset.txt" else "f")
            self.assertEqual(self.sql(writer + call.replace("'alpha'", "'absent_fixture'")), "f")
            denied = f"SET ROLE mainrag; SET app.user_id='{schema.OTHER_ID}'; "
            self.assertEqual(self.sql(denied + call), "f")
        compact_occurrence = int(occurrences["/synthetic/compact.txt"])
        self.assertEqual(self.sql("SELECT count(*) FROM storage_v2_compact_lexical_block "
            f"WHERE occurrence_id={compact_occurrence} AND block_order=0"), "1")
        self.assertEqual(self.sql(writer +
            "SELECT storage_v2_source_legacy_segment_matches(NULL,'alpha') IS NULL"), "t")
        # A restored offset still needs the independently bound source body.
        # Only this disposable fixture is altered; migration changes no rows.
        offset = int(occurrences["/synthetic/offset.txt"])
        self.sql("ALTER TABLE storage_v2_lexical_segment DISABLE TRIGGER ALL; "
            f"UPDATE storage_v2_lexical_segment SET text_sha256=decode(repeat('00',32),'hex') "
            f"WHERE occurrence_id={offset}; "
            "ALTER TABLE storage_v2_lexical_segment ENABLE TRIGGER ALL;")
        self.assertEqual(self.sql(writer +
            f"SELECT storage_v2_source_legacy_segment_matches({offset},'alpha')"), "f")
        self.file(MIGRATION)
