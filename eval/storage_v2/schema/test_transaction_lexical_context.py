"""Document-global UTF8 validation retains lexical representations and authority."""
from __future__ import annotations

import json
import unittest
import uuid

from eval.storage_v2.schema import test_posting_compaction as compaction
from eval.storage_v2.schema import test_bounded_lexical_verification as bounded

MIGRATION = compaction.PostingCompactionTests.schema.ROOT / 'migrations/160_storage_v2_transaction_lexical_context.sql'
OLD = 'storage_v2_put_lexical_segments_located(bigint,bigint,bigint[],text[],text[],text[],bigint[],bigint[])'
BEGIN = 'storage_v2_begin_lexical_document_context'
STAGE = 'storage_v2_stage_lexical_document_context'
FINISH = 'storage_v2_finish_lexical_document_context'
PRIVATE = 'storage_v2_put_lexical_segments_document_private'


class TransactionLexicalContextTests(unittest.TestCase):
    schema = compaction.PostingCompactionTests.schema
    command = classmethod(compaction.PostingCompactionTests.command.__func__)
    sql = classmethod(compaction.PostingCompactionTests.sql.__func__)
    file = classmethod(compaction.PostingCompactionTests.file.__func__)
    actor = staticmethod(compaction.PostingCompactionTests.actor)
    admin = classmethod(compaction.PostingCompactionTests.admin.__func__)
    quote = staticmethod(compaction.PostingCompactionTests.quote)
    # The existing constructor uses real immutable inline leaves + concatenation
    # for large bodies; it does not replace canonical content with a small leaf.
    make_projection = bounded.BoundedLexicalVerificationTests.make_projection
    begin = compaction.PostingCompactionTests.begin
    stage = compaction.PostingCompactionTests.stage
    complete_analysis = compaction.PostingCompactionTests.complete_analysis
    assert_sql_fails = compaction.PostingCompactionTests.assert_sql_fails

    @classmethod
    def setUpClass(cls):
        try:
            compaction.PostingCompactionTests.setUpClass.__func__(cls)
            for number in (157, 158, 159):
                cls.file(next(MIGRATION.parent.glob(f'{number}_*.sql')))
            cls.previous = cls.sql(f"SELECT pg_get_functiondef('{OLD}'::REGPROCEDURE)")
            cls.previous_catalog = cls.sql(f"SELECT to_jsonb(p) FROM pg_proc p WHERE oid='{OLD}'::REGPROCEDURE")
            cls.file(MIGRATION)
        except BaseException:
            if hasattr(cls, 'database'):
                compaction.PostingCompactionTests.tearDownClass.__func__(cls)
            elif hasattr(cls, 'stack'):
                cls.stack.close()
            raise

    @classmethod
    def tearDownClass(cls):
        compaction.PostingCompactionTests.tearDownClass.__func__(cls)

    def fixture(self, text):
        """Use actual supported constructors and an actual building ingest run."""
        token = uuid.uuid4().hex
        source = int(self.sql('INSERT INTO sources(id,name,type,path) '
            f"SELECT max(id)+1,'document-context-{token}','fixture','public-synthetic' FROM sources RETURNING id"))
        node, view, digest = self.make_projection(text)
        run = self.begin(source, token * 2, uuid.uuid4().hex * 2, commit_sha='a' * 40)
        self.stage(run, 'document.txt', text, node, view, digest)
        self.complete_analysis(digest)
        document = int(self.sql(self.admin('SELECT id FROM storage_v2_put_search_document('
            f"'mainrag.lexical-simple.v1','node',{node},{self.quote(text)},ARRAY[]::TEXT[])")))
        self.sql(self.admin(f'SELECT storage_v2_bind_search_document({view},0,{document},1.0)'))
        occurrence, artifact = map(int, self.sql('SELECT occurrence_id::TEXT||\':\'||artifact_version_id::TEXT '
            f'FROM storage_v2_ingest_run_item WHERE run_id={run}').split(':'))
        return run, occurrence, artifact

    def array(self, values, kind):
        return 'ARRAY[' + ','.join('NULL' if v is None else str(v) if isinstance(v, int)
                                  else self.quote(v) for v in values) + f']::{kind}[]'

    def arguments(self, identity, text, orders, positions, *, segments=None, prefixes=None,
                  types=None, character_starts=None, byte_starts=None):
        _, occurrence, artifact = identity
        size = len(orders)
        segments = [text[p:p + 5] for p in positions] if segments is None else segments
        character_starts = [p + 1 for p in positions] if character_starts is None else character_starts
        byte_starts = [len(text[:p].encode()) + 1 for p in positions] if byte_starts is None else byte_starts
        return ','.join((str(occurrence), str(artifact), self.array(orders, 'BIGINT'),
            self.array(segments, 'TEXT'), self.array(['title β'] * size if prefixes is None else prefixes, 'TEXT'),
            self.array(['text'] * size if types is None else types, 'TEXT'),
            self.array(character_starts, 'BIGINT'), self.array(byte_starts, 'BIGINT')))

    def begin_sql(self, identity, count):
        return f'SELECT {BEGIN}({identity[0]},{identity[1]},{identity[2]},{count});'

    def finish_sql(self, identity):
        return f'SELECT {FINISH}({identity[1]},{identity[2]});'

    def count_sql(self, identity):
        occurrence = identity[1]
        return (f'SELECT (SELECT count(*) FROM storage_v2_lexical_segment WHERE occurrence_id={occurrence})+'
                f'(SELECT count(*) FROM storage_v2_compact_lexical_block WHERE occurrence_id={occurrence})+'
                f'(SELECT count(*) FROM storage_v2_derived_lexical_block WHERE occurrence_id={occurrence})')

    def representation(self, identity):
        result = {}
        for label, table, order in (('rows', 'storage_v2_lexical_segment', 'segment_order'),
                ('compact', 'storage_v2_compact_lexical_block', 'block_order'),
                ('derived', 'storage_v2_derived_lexical_block', 'block_order')):
            result[label] = json.loads(self.sql('SELECT coalesce(jsonb_agg('
                "to_jsonb(s)-'occurrence_id'-'source_id'-'artifact_version_id'-'created_at' "
                f"ORDER BY {order}),'[]'::JSONB) FROM {table} s WHERE occurrence_id={identity[1]}"))
        return result

    def perform_document(self, identity, groups, *, check_empty=False):
        script = 'BEGIN;' + self.begin_sql(identity, sum(len(g[0]) for g in groups))
        for _, arguments in groups:
            script += f'SELECT {STAGE}({arguments});'
        if check_empty:
            script += 'DO $check$ BEGIN IF (' + self.count_sql(identity).removeprefix('SELECT ') + \
                ")<>0 THEN RAISE EXCEPTION 'semantic rows written before finish'; END IF; END $check$;"
        script += self.finish_sql(identity) + 'COMMIT;'
        return self.sql(self.admin(script))

    def test_multibatch_unicode_equivalence_replay_and_all_representations(self):
        # One disposable schema/database, no timing or production assertions.
        # Two groups cross the256 boundary; canonical text starts far into a
        # toasted body, contains 2/3/4-byte characters, and repeats locators.
        for representation, large, contiguous in (('derived', True, True),
                ('compact', False, True), ('rows', False, False)):
            with self.subTest(representation=representation):
                line = 'alpha é🙂甲Ω beta\n'
                prefix = 'prelude é🙂甲Ω\n' * (23000 if large else 1)
                text = prefix + line * 600
                base = len(prefix)
                orders = list(range(512)) if contiguous else [n * 2 + 1 for n in range(512)]
                # Reversed and duplicate starts exercise global sorting without
                # changing transport/insertion order or first-term ordinals.
                positions = [base + ((511 - n) // 2) * len(line) for n in range(512)]
                old, new = self.fixture(text), self.fixture(text)
                old_groups, new_groups = [], []
                for start in (0, 256):
                    selected = orders[start:start + 256]
                    locators = positions[start:start + 256]
                    old_args = self.arguments(old, text, selected, locators)
                    new_args = self.arguments(new, text, selected, locators)
                    old_groups.append((selected, old_args))
                    new_groups.append((selected, new_args))
                    self.assertEqual(self.sql(self.admin(f'SELECT storage_v2_put_lexical_segments_located({old_args})')), '256')
                output = self.perform_document(new, new_groups, check_empty=True)
                self.assertEqual(output.splitlines()[-1], '512')
                expected = self.representation(old)
                self.assertTrue(expected[representation])
                self.assertEqual(expected, self.representation(new))
                self.perform_document(new, new_groups)
                self.assertEqual(expected, self.representation(new))
                # Metadata is independently included in vectors/masks/cache and
                # immutable replay identity; it cannot be silently changed.
                collision = new_groups[0][1].replace("'title β'", "'changed β'", 1)
                self.assert_sql_fails(self.admin('BEGIN;' + self.begin_sql(new, 256) +
                    f'SELECT {STAGE}({collision});' + self.finish_sql(new)), 'lexical segment identity collision')
                self.assertEqual(expected, self.representation(new))
        # Existing binaries retain every catalog property, ACL and body byte.
        self.assertEqual(self.previous, self.sql(f"SELECT pg_get_functiondef('{OLD}'::REGPROCEDURE)"))
        self.assertEqual(self.previous_catalog, self.sql(f"SELECT to_jsonb(p) FROM pg_proc p WHERE oid='{OLD}'::REGPROCEDURE"))

    def test_fail_closed_provenance_temp_forgery_utf8_and_atomicity(self):
        text = 'é🙂x甲Ωz / alpha beta'
        identity = self.fixture(text)
        run, occurrence, artifact = identity
        good = self.arguments(identity, text, [0], [9], segments=['alpha'])
        begin = self.begin_sql(identity, 1)
        stage = f'SELECT {STAGE}({good});'
        finish = self.finish_sql(identity)
        tx = self.admin('BEGIN;' + begin)
        self.assert_sql_fails(self.actor(self.schema.OTHER_ID, 'BEGIN;' + begin), 'authorized bound lexical document required')
        self.assert_sql_fails(self.admin(f'SELECT {BEGIN}({run},{occurrence},{artifact + 1},1)'),
                              'authorized bound lexical document required')
        self.assert_sql_fails(self.admin(self.begin_sql(identity, 65537)), 'optimization segment bound exceeded')
        self.assert_sql_fails(tx + begin, 'one active lexical document context required')
        self.assert_sql_fails(tx + stage.replace(f'{occurrence},{artifact},', f'{occurrence},{artifact + 1},', 1),
                              'authorized bound lexical document context required')
        self.assert_sql_fails(tx + f"SET app.user_id='{self.schema.OTHER_ID}';" + stage,
                              'authorized bound lexical document context required')
        self.assert_sql_fails(tx + finish, 'complete lexical document stage required')
        self.assert_sql_fails(self.admin('BEGIN;' + self.begin_sql(identity, 2) + stage + stage),
                              'globally distinct lexical document segment orders required')
        # Caller has neither raw scratch read/write nor unchecked helper access.
        for action in ('SELECT * FROM pg_temp.mainrag_v2_lexical_context',
                       'DELETE FROM pg_temp.mainrag_v2_lexical_batches',
                       'UPDATE pg_temp.mainrag_v2_lexical_context SET staged_count=1',
                       'INSERT INTO pg_temp.mainrag_v2_lexical_anchors VALUES(1,1)'):
            self.assert_sql_fails(tx + action, 'permission denied')
        self.assert_sql_fails(self.admin(f'SELECT {PRIVATE}({good},1,convert_to(\'forged\',\'UTF8\'),6,6)'),
                              'permission denied for function')
        # Caller-created same-name tables/views are rejected before data access.
        for forged in ('CREATE TEMP TABLE pg_temp.mainrag_v2_lexical_context(x INTEGER);',
                       'CREATE TEMP VIEW pg_temp.mainrag_v2_lexical_context AS SELECT 1 AS x;'):
            self.assert_sql_fails(self.admin('BEGIN;' + forged + begin), 'untrusted lexical scratch relation')
        # A schema owner may drop/replace a relation. The recreated caller-owned
        # shape is rejected; successful prior begin is not enduring authority.
        self.assert_sql_fails(tx + 'RESET ROLE; DROP TABLE pg_temp.mainrag_v2_lexical_batches; SET ROLE mainrag;'
            'CREATE TEMP TABLE pg_temp.mainrag_v2_lexical_batches(x INTEGER);' + stage,
            'untrusted lexical scratch relation')
        # Deliberate administrator mutations only in this disposable transaction
        # independently cover ACL, column, trigger, identity/xact/backend checks.
        mutations = (
            ('GRANT SELECT ON pg_temp.mainrag_v2_lexical_context TO mainrag;', 'untrusted lexical scratch relation'),
            ('ALTER TABLE pg_temp.mainrag_v2_lexical_batches ADD COLUMN extra INTEGER;', 'lexical scratch shape differs'),
            ("CREATE FUNCTION pg_temp.fixture_hook() RETURNS TRIGGER LANGUAGE plpgsql AS $hook$ BEGIN RAISE EXCEPTION 'hook executed'; END $hook$;"
             'CREATE TRIGGER forged BEFORE INSERT ON pg_temp.mainrag_v2_lexical_batches FOR EACH ROW EXECUTE FUNCTION pg_temp.fixture_hook();',
             'untrusted lexical scratch relation'),
            ('CREATE RULE forged AS ON INSERT TO pg_temp.mainrag_v2_lexical_context DO ALSO SELECT 1;',
             'untrusted lexical scratch relation'),
            ("UPDATE pg_temp.mainrag_v2_lexical_context SET transaction_id='0'::XID8;", 'authorized bound lexical document context required'),
            ('UPDATE pg_temp.mainrag_v2_lexical_context SET backend_id=backend_id+1;', 'authorized bound lexical document context required'),
            ("UPDATE pg_temp.mainrag_v2_lexical_context SET document_profile='forged';", 'authorized bound lexical document context required'),
            (f"SET ROLE mainrag; UPDATE storage_v2_ingest_run SET status='cancelled' WHERE id={run}; RESET ROLE;", 'authorized bound lexical document context required'),
        )
        for mutation, error in mutations:
            with self.subTest(mutation=mutation):
                self.assert_sql_fails(tx + 'RESET ROLE;' + mutation + 'SET ROLE mainrag;' + stage, error)
        self.sql(self.admin('BEGIN;' + begin + 'COMMIT;'))
        self.assert_sql_fails(self.admin(stage), 'untrusted lexical scratch relation')

        invalid_inputs = (
            self.arguments(identity, text, [0], [0], segments=['é'], byte_starts=[2]),
            self.arguments(identity, text, [0], [0], segments=['é'], character_starts=[2]),
            self.arguments(identity, text, [0], [0], segments=['z']),
        )
        for arguments in invalid_inputs:
            # UTF8 gap or independently matched source slice must fail at finish;
            # staged client text never establishes a canonical anchor/hash.
            self.assert_sql_fails(tx + f'SELECT {STAGE}({arguments});' + finish,
                                  'UTF8' if arguments == invalid_inputs[0] else 'valid source-backed lexical segment group required')
        for arguments in (self.arguments(identity, text, [0, 0], [9, 9], segments=['alpha', 'alpha']),
                          self.arguments(identity, text, [0], [9], segments=['alpha'], prefixes=[None]),
                          self.arguments(identity, text, [0], [9], segments=['alpha'], types=[''])):
            self.assert_sql_fails(tx + f'SELECT {STAGE}({arguments});', 'valid bounded lexical segment inputs required')
        # A later invalid group rolls back the earlier group's real64-row/row
        # insertion in the same finish statement, not just the temp stage.
        invalid_second = self.arguments(identity, text, [1], [0], segments=['z'])
        self.assert_sql_fails(self.admin('BEGIN;' + self.begin_sql(identity, 2) + stage +
            f'SELECT {STAGE}({invalid_second});' + finish), 'valid source-backed lexical segment group required')
        self.assertEqual(self.sql(self.count_sql(identity)), '0')


if __name__ == '__main__':
    unittest.main()
