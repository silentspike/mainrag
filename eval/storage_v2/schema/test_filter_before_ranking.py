"""Mixed full-result identity with generated/copied ranks and sparse stages."""

from __future__ import annotations

import json

from eval.storage_v2.schema import test_shadow_ingest_schema as schema
from eval.storage_v2.schema import test_compact_exact_postings as compact


MIGRATION = schema.ROOT / "migrations/101_storage_v2_filter_before_ranking.sql"


class FilterBeforeRankingTests(compact.CompactExactPostingTests):
    @classmethod
    def file(cls, path):
        super().file(path)
        if path == compact.MIGRATION:
            super().file(MIGRATION)

    def commit(self, run, count):
        super().commit(run, count)
        rows = json.loads(self.sql("""
SELECT jsonb_agg(jsonb_build_object('id',o.id,'artifact',o.artifact_version_id,
                                  'text',d.search_text) ORDER BY o.id)
 FROM occurrence o JOIN storage_v2_search_view_document b
   ON b.view_id=o.view_id AND b.ordinal=0
 JOIN storage_v2_search_document d ON d.id=b.document_id
 WHERE o.source_id=6
"""))
        for index, row in enumerate(rows):
            if row["text"]:
                self.sql("SET ROLE mainrag; SET app.user_id='" + schema.ADMIN_ID + "'; " +
                    "SELECT storage_v2_put_lexical_segments_located("
                    f"{row['id']},{row['artifact']},ARRAY[0]::BIGINT[],"
                    f"ARRAY[{self.quote(row['text'])}],ARRAY[''],ARRAY['text'],"
                    "ARRAY[1]::BIGINT[],ARRAY[1]::BIGINT[])")
            if index < 2:
                # Protected fixture insertion supplies a valid immutable
                # compatibility projection without a live legacy dependency.
                self.sql(
                    "INSERT INTO storage_v2_legacy_lexical_segment "
                    "(occurrence_id,source_id,artifact_version_id,legacy_chunk_id,"
                    "legacy_file_hash,fts_vector) VALUES ("
                    f"{row['id']},6,{row['artifact']},{10000+row['id']},"
                    f"digest({self.quote(row['text'])},'sha256'),"
                    f"to_tsvector('simple',{self.quote(row['text'])}))")
            score = (0, 3, -2)[index % 3]
            for stage, status, value in (
                ("graph", "available", str(score)),
                ("semantic", "unavailable", "NULL"),
                ("rerank", "failed", "NULL"),
            ):
                self.sql(self.admin(
                    "SELECT storage_v2_put_occurrence_score_component("
                    f"{row['id']},'{stage}','filter-before-ranking-stage',"
                    f"'{status}',{value})"))

    def exact_search(self, ast, filters=None, **kwargs):
        profiles = {f"{stage}_profile": "filter-before-ranking-stage"
                    for stage in ("graph", "semantic", "rerank")}
        return super().exact_search(ast, {**profiles, **(filters or {})}, **kwargs)

    def test_complete_mixed_search_and_new_constructor_contract(self):
        # Reuse the independent pre-migration full JSON reference, mixed-layout
        # collision checks, broad duplicate scopes, races and unauthorized reads.
        super().test_complete_mixed_search_and_new_constructor_contract()
        occurrence = int(self.sql("SELECT min(id) FROM occurrence WHERE source_id=6"))
        scoped = (f"storage_v2_authorized_lexical_matches(ARRAY[{occurrence},"
                  f"{occurrence},NULL]::BIGINT[],ARRAY[6]::BIGINT[],'alpha')")
        prefix = "SET ROLE mainrag_v2_frontier_owner; "
        admin = prefix + f"SET app.user_id='{schema.ADMIN_ID}'; "
        other = prefix + f"SET app.user_id='{schema.OTHER_ID}'; "
        self.assertEqual(self.sql(admin + f"SELECT count(*) FROM {scoped}"), "1")
        self.assertEqual(self.sql(other + f"SELECT count(*) FROM {scoped}"), "0")
        self.assertEqual(self.sql(admin +
            "SELECT count(*) FROM storage_v2_authorized_lexical_matches("
            "ARRAY[]::BIGINT[],ARRAY[6]::BIGINT[],'alpha')"), "0")
        small = "ARRAY(SELECT id FROM occurrence WHERE source_id=6 ORDER BY id)"
        broad = ("ARRAY(SELECT occurrence.id FROM occurrence "
                 "CROSS JOIN generate_series(1,11000) WHERE source_id=6 "
                 "UNION ALL SELECT NULL::BIGINT)")
        for helper in ("storage_v2_source_segment_ranks", "storage_v2_source_segment_ranks_precise"):
            query = "SELECT coalesce(jsonb_agg(to_jsonb(rank) ORDER BY occurrence_id),'[]'::JSONB) "
            expected = self.sql(admin + query + f"FROM {helper}({small},'alpha') rank")
            self.assertEqual(self.sql(admin + query + f"FROM {helper}({broad},'alpha') rank"), expected)
            self.assertEqual(self.sql(other + f"SELECT count(*) FROM {helper}({broad},'alpha')"), "0")
        drift = self.command("--command", "BEGIN; "
            "DROP INDEX idx_storage_v2_nonzero_stage_score; "
            "CREATE INDEX idx_storage_v2_nonzero_stage_score "
            "ON storage_v2_occurrence_score_component(stage,profile_id,occurrence_id) "
            "INCLUDE(score) WHERE score>0; "
            + MIGRATION.read_text().replace("\nBEGIN;", "\n", 1).rsplit("COMMIT;", 1)[0]
            + "ROLLBACK;", check=False)
        self.assertNotEqual(drift.returncode, 0)
        self.assertIn("nonzero score index identity differs", drift.stderr)
