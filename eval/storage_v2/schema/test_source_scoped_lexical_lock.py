"""Source-bounded lexical locks, mixed constructor races and migration drift."""

import unittest
import uuid

from eval.storage_v2.schema import test_compact_exact_postings as compact
from eval.storage_v2.schema import test_shadow_ingest_schema as schema

MIGRATION = schema.ROOT / "migrations/105_storage_v2_source_scoped_lexical_lock.sql"


class SourceScopedLexicalLockTests(unittest.TestCase):
    schema = schema
    # Reuse fixture helpers without replaying historical migration tests after
    # the current migration has deliberately changed their function bodies.
    command = classmethod(compact.CompactExactPostingTests.command.__func__)
    sql = classmethod(schema.ShadowIngestSchemaTests.sql.__func__)
    file = classmethod(schema.ShadowIngestSchemaTests.file.__func__)
    admin = classmethod(schema.ShadowIngestSchemaTests.admin.__func__)
    actor = staticmethod(schema.ShadowIngestSchemaTests.actor)
    make_projection = compact.CompactExactPostingTests.make_projection
    begin = compact.CompactExactPostingTests.begin
    stage = compact.CompactExactPostingTests.stage
    complete_analysis = compact.CompactExactPostingTests.complete_analysis
    put = compact.CompactExactPostingTests.put
    quote = staticmethod(compact.CompactExactPostingTests.quote)
    start_client = compact.CompactExactPostingTests.start_client
    wait_for_client = compact.CompactExactPostingTests.wait_for_client

    @classmethod
    def setUpClass(cls):
        compact.CompactExactPostingTests.setUpClass.__func__(cls)
        for number in (99, 101, 102, 105):
            cls.file(next((schema.ROOT / 'migrations').glob(f'{number:03}_*.sql')))

    @classmethod
    def tearDownClass(cls):
        compact.CompactExactPostingTests.tearDownClass.__func__(cls)

    def test_many_occurrences_hold_one_lexical_lock_and_reject_migration_drift(self):
        content = "alpha beta gamma"
        node, view, digest = self.make_projection(content)
        run = self.begin(1, "e1" * 32, "e2" * 32)
        self.stage(run, "many-items.txt", content, node, view, digest)
        self.complete_analysis(digest)
        document = self.put(node, content)
        self.sql(self.admin(f"SELECT storage_v2_bind_search_document({view},0,{document},1.0)"))
        schema.ShadowIngestSchemaTests.commit(self, run, 1)
        # The fixture creates distinct immutable occurrences over a shared valid
        # artifact/view. Calls below exercise both real constructor paths.
        self.sql("INSERT INTO occurrence(source_id,artifact_version_id,view_id,role,ordinal,"
                 "source_path,locator) SELECT source_id,artifact_version_id,view_id,"
                 "'lock-scale-fixture',n,source_path,locator FROM occurrence "
                 "CROSS JOIN generate_series(1,10000) n WHERE source_id=1 AND ordinal=0")
        observed = self.sql("BEGIN; SET LOCAL ROLE mainrag; "
            f"SET LOCAL app.user_id='{schema.ADMIN_ID}'; " + """
DO $test$ DECLARE item RECORD; BEGIN
 FOR item IN SELECT id,artifact_version_id,ordinal FROM occurrence
              WHERE source_id=1 AND role='lock-scale-fixture' LOOP
  IF item.ordinal%2=0 THEN
   PERFORM storage_v2_put_lexical_segments_located(item.id,item.artifact_version_id,
       ARRAY[0]::BIGINT[],ARRAY['alpha beta gamma'],ARRAY[''],ARRAY['text'],
       ARRAY[1]::BIGINT[],ARRAY[1]::BIGINT[]);
  ELSE
   PERFORM storage_v2_put_lexical_segment(item.id,item.artifact_version_id,
       0,'alpha beta gamma','','text');
  END IF;
 END LOOP;
END $test$;
SELECT count(*) FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory';
SELECT count(*) FROM storage_v2_lexical_segment_all WHERE source_id=1;
ROLLBACK;
""")
        self.assertEqual(observed.splitlines(), ['1', '10000'])
        occurrence, artifact = map(int, self.sql(
            "SELECT id||':'||artifact_version_id FROM occurrence "
            "WHERE source_id=1 AND role='lock-scale-fixture' AND ordinal=1").split(':'))
        self.sql("CREATE TABLE fixture_lexical_barrier(released BOOLEAN NOT NULL); "
                 "INSERT INTO fixture_lexical_barrier VALUES(FALSE); "
                 "GRANT SELECT ON fixture_lexical_barrier TO mainrag")
        writer = f"SET ROLE mainrag; SET app.user_id='{schema.ADMIN_ID}'; "
        for order, compact_first in ((0, True), (64, False)):
            self.sql("UPDATE fixture_lexical_barrier SET released=FALSE")
            native = ("SELECT storage_v2_put_lexical_segments_located("
                f"{occurrence},{artifact},ARRAY[{order}]::BIGINT[],ARRAY['alpha beta gamma'],"
                "ARRAY[''],ARRAY['text'],ARRAY[1]::BIGINT[],ARRAY[1]::BIGINT[])")
            flat = ("SELECT storage_v2_put_lexical_segment("
                f"{occurrence},{artifact},{order},'alpha beta gamma','','text')")
            first, second = (native, flat) if compact_first else (flat, native)
            winner_name, loser_name = 'winner-'+uuid.uuid4().hex, 'loser-'+uuid.uuid4().hex
            winner = self.start_client(winner_name, "BEGIN; " + writer + first + "; "
                "DO $$ BEGIN LOOP EXIT WHEN (SELECT released FROM fixture_lexical_barrier); "
                "PERFORM pg_sleep(0.05); END LOOP; END $$; COMMIT;")
            loser = None
            try:
                self.wait_for_client(winner_name, 'PgSleep', winner)
                loser = self.start_client(loser_name, writer + second)
                self.wait_for_client(loser_name, 'advisory', loser)
            finally:
                self.sql("UPDATE fixture_lexical_barrier SET released=TRUE")
                _, error = winner.communicate(timeout=20)
                self.assertEqual(winner.returncode, 0, error)
                if loser is not None:
                    _, error = loser.communicate(timeout=20)
                    self.assertEqual(loser.returncode, 0, error)
            self.assertEqual(self.sql("SELECT count(*) FROM storage_v2_lexical_segment_all "
                f"WHERE occurrence_id={occurrence} AND segment_order={order}"), '1')
        self.file(MIGRATION)
        # Idempotence must not bless arbitrary edits to an already migrated body.
        drift = self.command("--command", "BEGIN; DO $drift$ DECLARE body TEXT; BEGIN "
            "body:=pg_get_functiondef('storage_v2_guard_flat_lexical_insert()'::REGPROCEDURE); "
            "EXECUTE replace(body,'RETURN NEW;','RETURN NULL;'); END $drift$; "
            + MIGRATION.read_text().replace('BEGIN;', '', 1).rsplit('COMMIT;', 1)[0]
            + "ROLLBACK;", check=False)
        self.assertNotEqual(drift.returncode, 0)
        self.assertIn('lexical lock definition differs', drift.stderr)
