"""Exact SQL160/162 projection parity in one disposable database and gate."""
from __future__ import annotations

import json
import unittest

from eval.storage_v2.schema import test_transaction_lexical_context as previous
from eval.storage_v2.schema import test_posting_compaction as compaction

MIGRATION = previous.MIGRATION.parent / '162_storage_v2_shared_lexical_projections.sql'
PRIVATE_SIGNATURE = ('storage_v2_put_lexical_segments_document_private('
    'bigint,bigint,bigint[],text[],text[],text[],bigint[],bigint[],bigint,bytea,integer,integer)')
BASELINE_FINISH = 'fixture_original_lexical_finish'


class SharedLexicalProjectionTests(unittest.TestCase):
    schema = previous.TransactionLexicalContextTests.schema
    command = classmethod(compaction.PostingCompactionTests.command.__func__)
    sql = classmethod(compaction.PostingCompactionTests.sql.__func__)
    file = classmethod(compaction.PostingCompactionTests.file.__func__)
    actor = staticmethod(compaction.PostingCompactionTests.actor)
    admin = classmethod(compaction.PostingCompactionTests.admin.__func__)
    quote = staticmethod(compaction.PostingCompactionTests.quote)
    make_projection = previous.TransactionLexicalContextTests.make_projection
    begin = previous.TransactionLexicalContextTests.begin
    stage = previous.TransactionLexicalContextTests.stage
    complete_analysis = previous.TransactionLexicalContextTests.complete_analysis
    assert_sql_fails = previous.TransactionLexicalContextTests.assert_sql_fails
    fixture = previous.TransactionLexicalContextTests.fixture
    array = previous.TransactionLexicalContextTests.array
    arguments = previous.TransactionLexicalContextTests.arguments
    begin_sql = previous.TransactionLexicalContextTests.begin_sql
    count_sql = previous.TransactionLexicalContextTests.count_sql
    representation = previous.TransactionLexicalContextTests.representation

    @classmethod
    def function_catalog(cls):
        return json.loads(cls.sql("SELECT jsonb_object_agg(oid::regprocedure::TEXT,"
            "jsonb_build_object('definition',pg_get_functiondef(oid),'owner',proowner,"
            "'acl',proacl,'config',proconfig)) FROM pg_proc WHERE pronamespace='public'::regnamespace AND prokind='f'"))

    @classmethod
    def setUpClass(cls):
        try:
            previous.TransactionLexicalContextTests.setUpClass.__func__(cls)
            # Freeze the actual SQL160 bodies before SQL162. The baseline finish
            # uses the exact baseline private writer; no new payload builds gold.
            original = cls.sql(f"SELECT pg_get_functiondef('{PRIVATE_SIGNATURE}'::REGPROCEDURE)")
            finish = cls.sql("SELECT pg_get_functiondef('storage_v2_finish_lexical_document_context(bigint,bigint)'::REGPROCEDURE)")
            baseline = original.replace('CREATE OR REPLACE FUNCTION public.storage_v2_put_lexical_segments_document_private(',
                'CREATE FUNCTION public.fixture_original_lexical_private(', 1)
            if baseline == original:
                raise AssertionError('exact SQL160 private function boundary is missing')
            baseline_finish = finish.replace('CREATE OR REPLACE FUNCTION public.storage_v2_finish_lexical_document_context(',
                f'CREATE FUNCTION public.{BASELINE_FINISH}(', 1).replace(
                    'public.storage_v2_put_lexical_segments_document_private(',
                    'public.fixture_original_lexical_private(')
            if baseline_finish == finish or 'fixture_original_lexical_private(' not in baseline_finish:
                raise AssertionError('exact SQL160 finish function boundary is missing')
            cls.sql(baseline + ';' + baseline_finish + ';'
                'ALTER FUNCTION fixture_original_lexical_private(bigint,bigint,bigint[],text[],text[],text[],bigint[],bigint[],bigint,bytea,integer,integer) OWNER TO mainrag_v2_frontier_owner;'
                f'ALTER FUNCTION {BASELINE_FINISH}(bigint,bigint) OWNER TO mainrag_v2_frontier_owner;'
                'REVOKE ALL ON FUNCTION fixture_original_lexical_private(bigint,bigint,bigint[],text[],text[],text[],bigint[],bigint[],bigint,bytea,integer,integer) FROM PUBLIC,mainrag;'
                f'REVOKE ALL ON FUNCTION {BASELINE_FINISH}(bigint,bigint) FROM PUBLIC;'
                f'GRANT EXECUTE ON FUNCTION {BASELINE_FINISH}(bigint,bigint) TO mainrag;')
            cls.before = cls.function_catalog()
            cls.file(MIGRATION)
            cls.after = cls.function_catalog()
            cls.sql('CREATE TABLE fixture_projection_timings(label TEXT PRIMARY KEY,ms DOUBLE PRECISION NOT NULL,segments BIGINT NOT NULL);'
                'GRANT INSERT ON fixture_projection_timings TO mainrag;')
        except BaseException:
            if hasattr(cls, 'database'):
                compaction.PostingCompactionTests.tearDownClass.__func__(cls)
            elif hasattr(cls, 'stack'):
                cls.stack.close()
            raise

    @classmethod
    def tearDownClass(cls):
        compaction.PostingCompactionTests.tearDownClass.__func__(cls)

    def perform(self, identity, groups, *, baseline=False, measured=False):
        script = 'BEGIN;' + self.begin_sql(identity, sum(len(orders) for orders, _ in groups))
        for _, arguments in groups:
            script += f'SELECT {previous.STAGE}({arguments});'
        finish = BASELINE_FINISH if baseline else previous.FINISH
        if measured:
            # Exactly one measurement per fixture/original/new finish in the same gate.
            # Fixture setup and transport are excluded; no source/reader benchmark.
            script += ("DO $measure$ DECLARE started TIMESTAMPTZ:=clock_timestamp(); count BIGINT; BEGIN "
                f'count:={finish}({identity[1]},{identity[2]}); '
                f'INSERT INTO fixture_projection_timings VALUES({self.quote(str(measured) + ("_original160" if baseline else "_shared162"))},'
                "extract(epoch FROM clock_timestamp()-started)*1000,count); END $measure$;")
        else:
            script += f'SELECT {finish}({identity[1]},{identity[2]});'
        return self.sql(self.admin(script + 'COMMIT;'))

    def unchanged_document(self, identity):
        return self.sql(self.admin("SELECT to_jsonb(document) FROM occurrence occurrence_row "
            'JOIN storage_v2_search_view_document binding ON binding.view_id=occurrence_row.view_id AND binding.ordinal=0 '
            f'JOIN storage_v2_search_document document ON document.id=binding.document_id WHERE occurrence_row.id={identity[1]}'))

    def compare_group(self, text, segments, positions, *, orders=None, prefixes=None, types=None, measured=False):
        orders = list(range(len(segments))) if orders is None else orders
        old, new = self.fixture(text), self.fixture(text)
        baseline = self.arguments(old, text, orders, positions, segments=segments, prefixes=prefixes, types=types)
        current = self.arguments(new, text, orders, positions, segments=segments, prefixes=prefixes, types=types)
        before = self.unchanged_document(new)
        self.perform(old, [(orders, baseline)], baseline=True, measured=measured)
        self.perform(new, [(orders, current)], measured=measured)
        expected = self.representation(old)
        self.assertEqual(expected, self.representation(new))
        self.assertEqual(before, self.unchanged_document(new))
        return old, new, current, expected

    def assert_projection_admission(self, segments, prefixes, types, expected):
        # A bounded disposable query outside the finish measurement checks the
        # actual weighted vector sizes. It changes no writer/probe function.
        sizes = json.loads(self.sql("SELECT jsonb_build_object('vector_bytes',sum(pg_column_size("
            "setweight(to_tsvector('simple',t),'A')||setweight(to_tsvector('simple',p),'B')||"
            "setweight(to_tsvector('simple',k),'C'))),'segments',count(*),'payload_bytes',"
            "sum(octet_length(t)+octet_length(p)+octet_length(k))) FROM unnest("
            + self.array(segments, 'TEXT') + ',' + self.array(prefixes, 'TEXT') + ','
            + self.array(types, 'TEXT') + ') input(t,p,k)'))
        admitted = (sizes['payload_bytes'] <= 8388608 and sizes['vector_bytes'] <= 8388608
                    and sizes['vector_bytes'] >= sizes['segments'] * 2048)
        self.assertEqual(admitted, expected, sizes)
        print('Shared lexical projection density admission: ' + json.dumps(sizes, sort_keys=True), flush=True)

    def test_one_mib_256_segment_exact_payload_and_single_phase_measurement(self):
        # Real UTF8 locators, weighted prefixes/types, repeated lexemes and
        # punctuation-preserving parser forms. A full256 group spans four blocks.
        marker = 'alpha é🙂甲Ω api::call() host.example q_1 ' + 'long' + 'x' * 256 + ' '
        segments = []
        for n in range(256):
            token = f'key_{n % 17} '
            value = marker + token * ((4096 - len(marker.encode())) // len(token))
            segments.append(value + ' ' * (4096 - len(value.encode())))
        text = ''
        positions = []
        for segment in segments:
            positions.append(len(text)); text += segment + '\n'
        self.assertGreaterEqual(len(text.encode()), 1024 * 1024)
        prefixes = ['common prefix é'] * 256
        types = ['text' if n % 2 else 'function' for n in range(256)]
        _, new, arguments, expected = self.compare_group(text, segments, positions,
            prefixes=prefixes, types=types, measured='repeated')
        self.assertEqual(len(expected['derived']), 4)
        self.perform(new, [(list(range(256)), arguments)])
        self.assertEqual(expected, self.representation(new))
        timings = json.loads(self.sql("SELECT jsonb_object_agg(label,jsonb_build_object('ms',ms,'segments',segments)) FROM fixture_projection_timings WHERE label LIKE 'repeated_%'"))
        self.assertEqual(set(timings), {'repeated_original160', 'repeated_shared162'})
        self.assertTrue(all(row['segments'] == 256 and row['ms'] >= 0 for row in timings.values()))
        print('Shared lexical projection same-gate finish measurements: ' + json.dumps(timings, sort_keys=True), flush=True)
        self.assert_projection_admission(segments, prefixes, types, False)

    def test_one_mib_diverse_tokens_exact_payload_and_single_phase_measurement(self):
        # A deterministic mixture of prose and technical forms with local and
        # document-wide vocabulary. Both algorithms receive identical bytes,
        # weighted context and the actual writer's 256-segment batch size.
        prose = ('The document describes incremental content storage, exact permission checks, '
                 'transaction recovery and a stable search projection. é🙂甲Ω ')
        vocabulary = [f'symbol_{n:04d} api::method_{n % 137}() host{n % 97}.example '
                      for n in range(2048)]
        segments = []
        positions = []
        text = ''
        for n in range(256):
            value = prose + ''.join(vocabulary[(n * 73 + j) % len(vocabulary)] for j in range(128))
            value = value.encode()[:4096].decode('UTF8', errors='ignore')
            value += ' ' * (4096 - len(value.encode()))
            segments.append(value)
            positions.append(len(text))
            text += value + '\n'
        self.assertGreaterEqual(len(text.encode()), 1024 * 1024)
        prefixes = [f'Content section {n // 16} recovery' for n in range(256)]
        types = ['function' if n % 3 == 0 else 'text' for n in range(256)]
        _, new, arguments, expected = self.compare_group(text, segments, positions,
            prefixes=prefixes, types=types, measured='diverse')
        self.assertEqual(len(expected['derived']), 4)
        self.perform(new, [(list(range(256)), arguments)])
        self.assertEqual(expected, self.representation(new))
        timings = json.loads(self.sql("SELECT jsonb_object_agg(label,jsonb_build_object('ms',ms,'segments',segments)) FROM fixture_projection_timings WHERE label LIKE 'diverse_%'"))
        self.assertEqual(set(timings), {'diverse_original160', 'diverse_shared162'})
        self.assertTrue(all(row['segments'] == 256 and row['ms'] >= 0 for row in timings.values()))
        print('Shared lexical projection diverse-token same-gate finish measurements: ' + json.dumps(timings, sort_keys=True), flush=True)
        self.assert_projection_admission(segments, prefixes, types, True)

    def test_oversized_projection_payload_preserves_original_fallback(self):
        # Exceed the shared projection's 8 MiB payload admission without a
        # large lexical vocabulary. The canonical body remains independently
        # bound and this derived block must use the original projection path.
        text = 'alpha ' + ' ' * 262144
        self.compare_group(text, ['alpha'], [0], prefixes=['!' * 8388609], types=['text'])

    def test_empty_vectors_unicode_boundaries_first_cache_and_relative_ordinals(self):
        segments = ['?!'] * 64 + ['é🙂甲Ω beta_beta alpha alpha x9'] * 64 + ['zeta'] * 2
        positions = []
        text = 'prelude ' * 40000
        for segment in segments:
            positions.append(len(text)); text += segment + '\n'
        prefixes = [''] * 64 + ['weight context'] * 66
        types = ['?!'] * 64 + ['type'] * 66
        _, new, arguments, expected = self.compare_group(text, segments, positions, prefixes=prefixes, types=types)
        self.assertEqual(len(expected['derived']), 3)
        empty = expected['derived'][0]
        self.assertEqual(empty['fingerprints'], [])
        self.assertEqual(empty['first_terms'], [])
        self.assertTrue(all(mask == '0' * 128 for mask in empty['segment_masks']))
        for block in expected['derived']:
            self.assertTrue(all(1 <= ordinal <= len(block['segment_orders']) for ordinal in block['first_term_ordinals']))
        # An original parameter changes no materialization and cannot replay.
        collision = arguments.replace("'weight context'", "'changed context'", 1)
        self.assert_sql_fails(self.admin('BEGIN;' + self.begin_sql(new, len(segments)) +
            f'SELECT {previous.STAGE}({collision});SELECT {previous.FINISH}({new[1]},{new[2]});'),
            'lexical segment identity collision')
        self.assertEqual(expected, self.representation(new))

    def test_small_compact_and_noncontiguous_row_representations_remain_exact(self):
        text = 'alpha beta β field_2 punct::token() !? title\n'
        for orders in ([0, 1, 2], [1, 3, 5]):
            with self.subTest(orders=orders):
                self.compare_group(text, ['alpha', 'beta', 'β'], [0, 6, 11], orders=orders,
                    prefixes=['prefix', '', 'β'], types=['function', '?!', 'text'])

    def test_authority_direct_helper_forgery_and_utf8_rejections(self):
        text = 'é🙂x甲Ωz / alpha beta'
        identity = self.fixture(text)
        good = self.arguments(identity, text, [0], [9], segments=['alpha'])
        begin = self.begin_sql(identity, 1)
        self.assert_sql_fails(self.actor(self.schema.OTHER_ID, 'BEGIN;' + begin), 'authorized bound lexical document required')
        self.assert_sql_fails(self.admin(f"SELECT {previous.PRIVATE}({good},1,convert_to('forged','UTF8'),6,6)"),
            'permission denied for function')
        self.assert_sql_fails(self.admin('BEGIN;CREATE TEMP TABLE pg_temp.mainrag_v2_lexical_context(x INTEGER);' + begin),
            'untrusted lexical scratch relation')
        invalid = self.arguments(identity, text, [0], [0], segments=['é'], byte_starts=[2])
        self.assert_sql_fails(self.admin('BEGIN;' + begin + f'SELECT {previous.STAGE}({invalid});'
            f'SELECT {previous.FINISH}({identity[1]},{identity[2]});'), 'UTF8')
        self.assertEqual(self.sql(self.count_sql(identity)), '0')

    def test_exact_writer_only_function_change_and_predecessor_guard(self):
        self.assertEqual(set(self.before), set(self.after))
        self.assertEqual({name for name in self.before if self.before[name] != self.after[name]}, {PRIVATE_SIGNATURE})
        for property_name in ('owner', 'acl', 'config'):
            self.assertEqual(self.before[PRIVATE_SIGNATURE][property_name], self.after[PRIVATE_SIGNATURE][property_name])
        result = self.command('--file', str(MIGRATION), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('shared lexical projection predecessor or authority differs', result.stderr)
        self.assertEqual(self.after, self.function_catalog())


if __name__ == '__main__':
    unittest.main()
