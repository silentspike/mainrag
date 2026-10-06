"""Optional exact indexes preserve complete native matches and posting readers."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import unittest
import zlib

from eval.storage_v2.schema import test_cache_reader_metadata as cache
from eval.storage_v2.schema import test_posting_compaction as compaction


MIGRATION = compaction.PostingCompactionTests.schema.ROOT / 'migrations/159_storage_v2_exact_lexeme_probes.sql'
NATIVE_INDEX = 'idx_storage_v2_compact_lexical_exact_lexemes'
POSTING_INDEX = 'idx_storage_v2_compact_posting_exact_terms'
CACHED = 'storage_v2_authorized_cached_lexical_first_candidates(bigint[],bigint[],text)'
POSTING = 'storage_v2_scoped_query_posting(bigint[],text[])'
NATIVE_DDL = (f'CREATE INDEX {NATIVE_INDEX} ON storage_v2_compact_lexical_block USING GIN'
              '(storage_v2_compact_lexical_exact_lexemes(fts_vectors))')
POSTING_DDL = (f'CREATE INDEX {POSTING_INDEX} ON storage_v2_compact_posting_block USING GIN'
               '(storage_v2_compact_posting_exact_term_keys(terms))')


class ExactLexemeProbeTests(unittest.TestCase):
    schema = compaction.PostingCompactionTests.schema
    command = classmethod(compaction.PostingCompactionTests.command.__func__)
    sql = classmethod(compaction.PostingCompactionTests.sql.__func__)
    file = classmethod(compaction.PostingCompactionTests.file.__func__)
    actor = staticmethod(compaction.PostingCompactionTests.actor)
    admin = classmethod(compaction.PostingCompactionTests.admin.__func__)
    quote = staticmethod(compaction.PostingCompactionTests.quote)
    make_projection = compaction.PostingCompactionTests.make_projection
    begin = compaction.PostingCompactionTests.begin
    stage = compaction.PostingCompactionTests.stage
    complete_analysis = compaction.PostingCompactionTests.complete_analysis
    commit = compaction.PostingCompactionTests.commit
    assert_sql_fails = compaction.PostingCompactionTests.assert_sql_fails
    fixture = cache.CacheReaderMetadataTests.fixture

    @classmethod
    def setUpClass(cls):
        try:
            compaction.PostingCompactionTests.setUpClass.__func__(cls)
            cls.file(next(MIGRATION.parent.glob('157_*.sql')))
            cls.file(next(MIGRATION.parent.glob('158_*.sql')))
        except BaseException:
            if hasattr(cls, 'database'):
                compaction.PostingCompactionTests.tearDownClass.__func__(cls)
            elif hasattr(cls, 'stack'):
                cls.stack.close()
            raise

    @classmethod
    def tearDownClass(cls):
        compaction.PostingCompactionTests.tearDownClass.__func__(cls)

    def operator(self, statement):
        return f"SET app.user_id='{self.schema.ADMIN_ID}';" + statement

    def readiness(self):
        return self.sql(self.admin('SELECT storage_v2_exact_lexeme_probes_ready()'))

    def native(self, occurrence, source, query, *, old=False, user=None, empty=False):
        function = 'fixture_pre_exact_cached' if old else 'storage_v2_authorized_cached_lexical_first_candidates'
        ids = 'ARRAY[]::BIGINT[]' if empty else f'ARRAY[{occurrence},{occurrence},NULL]::BIGINT[]'
        return self.sql(f"SET app.user_id='{self.schema.ADMIN_ID if user is None else user}';" +
            'SELECT coalesce(jsonb_agg(to_jsonb(p) ORDER BY occurrence_id,segment_order),\'[]\'::JSONB) '
            f'FROM {function}({ids},ARRAY[{source}]::BIGINT[],{self.quote(query)}) p')

    def post(self, document, terms, *, old=False):
        function = 'fixture_pre_exact_posting' if old else 'storage_v2_scoped_query_posting'
        array = 'ARRAY[' + ','.join(self.quote(v) for v in terms) + ']::TEXT[]'
        return self.sql(self.admin('SELECT coalesce(jsonb_agg(to_jsonb(p) ORDER BY term),\'[]\'::JSONB) '
            f'FROM {function}(array_fill({document}::BIGINT,ARRAY[1025]),{array}) p'))

    def build_native(self, text, segments, offsets):
        occurrence, artifact = self.fixture(text)
        source = int(self.sql(f'SELECT source_id FROM occurrence WHERE id={occurrence}'))

        def array(values, kind):
            return 'ARRAY[' + ','.join(str(v) if isinstance(v, int) else self.quote(v)
                                      for v in values) + f']::{kind}[]'

        self.sql(self.admin(f'SELECT storage_v2_put_lexical_segments_located({occurrence},{artifact},'
            f'{array(list(range(len(segments))), "BIGINT")},{array(segments, "TEXT")},'
            f'{array(["titleproof"] * len(segments), "TEXT")},'
            f'{array(["text"] * len(segments), "TEXT")},'
            f'{array([n + 1 for n in offsets], "BIGINT")},'
            f'{array([len(text[:n].encode()) + 1 for n in offsets], "BIGINT")})'))
        self.assertEqual(self.sql('SELECT count(*) FROM storage_v2_compact_lexical_block '
                                  f'WHERE occurrence_id={occurrence}'), '4')
        return occurrence, source

    def test_optional_indexes_fallback_exact_matches_authority_and_contract(self):
        # One owned disposable database; no production service, source jobs or
        # timing assertions. Existing migration harness owns all role cleanup.
        seen = {}
        for number in range(20000):
            word = f'lexemecollision{number}'
            fingerprint = hashlib.sha256(word.encode()).digest()[:2]
            if fingerprint in seen:
                a, z = seen[fingerprint], word
                break
            seen[fingerprint] = word
        else:
            self.fail('bounded public sixteen-bit collision fixture required')

        # Save the actual158 definitions in fixture-only readers. These copies
        # have the same source authorization and original exact checks.
        for signature, name in ((CACHED, 'fixture_pre_exact_cached'),
                                (POSTING, 'fixture_pre_exact_posting')):
            definition = self.sql(f"SELECT pg_get_functiondef('{signature}'::REGPROCEDURE)")
            original = signature.split('(')[0]
            self.sql(definition.replace(f'FUNCTION public.{original}(', f'FUNCTION public.{name}('))
            arguments = signature.split('(', 1)[1]
            owner = 'mainrag_v2_lexical_rank_owner' if signature == CACHED else 'mainrag'
            self.sql(f'ALTER FUNCTION {name}({arguments} OWNER TO {owner}; '
                     f'REVOKE ALL ON FUNCTION {name}({arguments} FROM PUBLIC; '
                     f'GRANT EXECUTE ON FUNCTION {name}({arguments} TO mainrag')

        collision_text = (a + '\n') * 256
        collision_occurrence, collision_source = self.build_native(
            collision_text, [a] * 256, [n * (len(a) + 1) for n in range(256)])
        line = f'{z} beta {a} latepositive Ω\n'
        text = line * 256
        segments, offsets = [], []
        for n in range(256):
            if n < 64 or 128 <= n < 192:
                value = z if n % 2 == 0 else 'beta'
            elif n < 128:
                value = a if n % 2 == 0 else 'beta'
            else:
                value = 'latepositive' if n == 255 else z + ' beta'
            segments.append(value)
            offsets.append(n * len(line) + line.index(value))
        native, source = self.build_native(text, segments, offsets)

        # Canonical terms have no GIN-key-sized input limit. Independent hash
        # chunks produce a long token without the trivial compression of a
        # repeated character; its complete bytes and frequency must survive.
        long_term = 'zzoversizedterm' + ''.join(
            hashlib.sha256(f'public-long-term-{n}'.encode()).hexdigest() for n in range(256))
        self.assertGreater(len(zlib.compress(long_term.encode())), 8192)
        posting_text = (a + ' ' + ' '.join(f'postingword{n}' for n in range(1000)) +
                        ' ' + long_term + ' ' + long_term)
        node, _, _ = self.make_projection(posting_text)
        document = int(self.sql(self.admin('SELECT id FROM storage_v2_put_search_document('
            f"'exact-probe-posting-fixture','node',{node},{self.quote(posting_text)},ARRAY[]::TEXT[])")))
        self.assertGreater(int(self.sql('SELECT count(*) FROM storage_v2_compact_posting_block '
                                        f'WHERE document_id={document}')), 1)
        # A synthetic large frequency tests BIGINT transport. Only this fixture
        # disables its immutable trigger; the normal trigger is restored before
        # migration/contract assertions, and this is not a body-frequency proof.
        self.sql('ALTER TABLE storage_v2_compact_posting_block DISABLE TRIGGER storage_v2_compact_posting_immutable;'
            'UPDATE storage_v2_compact_posting_block SET term_frequencies['
            "array_position(terms,'postingword42')]=1099511627776 "
            f"WHERE document_id={document} AND 'postingword42'=ANY(terms);"
            'ALTER TABLE storage_v2_compact_posting_block ENABLE TRIGGER storage_v2_compact_posting_immutable;')
        native_cases = [(collision_occurrence, collision_source, z),
                        (collision_occurrence, collision_source, 'unknownlexemeprobe'),
                        (native, source, z + ' beta'), (native, source, 'latepositive'),
                        (native, source, 'titleproof'), (native, source, z + ' titleproof')]
        terms = [z, 'unknownlexemeprobe', 'postingword42', 'postingword43', z, long_term]
        old_native = [self.native(o, s, q, old=True) for o, s, q in native_cases]
        old_posting = self.post(document, terms, old=True)
        self.assertEqual(old_native[0], '[]')
        self.assertEqual(json.loads(old_native[2])[0]['segment_order'], 192)
        self.assertEqual(json.loads(old_native[3])[0]['segment_order'], 255)
        posting_frequencies = {row['term']: row['term_frequency'] for row in json.loads(old_posting)}
        self.assertEqual(posting_frequencies['postingword42'], 1099511627776)
        self.assertEqual(posting_frequencies[long_term], 2)

        contract_sql = 'SELECT jsonb_agg(to_jsonb(c) ORDER BY signature) FROM storage_v2_posting_conversion_contract c'
        catalog_sql = 'SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM storage_v2_posting_conversion_catalog_contract c'
        old_contract = json.loads(self.sql(contract_sql))
        old_catalog = self.sql(catalog_sql)
        guard_sql = "SELECT pg_get_functiondef('storage_v2_posting_conversion_require_operator()'::REGPROCEDURE)"
        old_guard = self.sql(guard_sql)
        data_sql = ('SELECT jsonb_build_object(\'documents\',(SELECT jsonb_agg(to_jsonb(d) ORDER BY id) '
                    'FROM storage_v2_search_document d),\'blocks\',(SELECT jsonb_agg(to_jsonb(b) '
                    'ORDER BY occurrence_id,block_order) FROM storage_v2_compact_lexical_block b))')
        old_data = self.sql(data_sql)
        metadata = {signature: self.sql(f"SELECT (to_jsonb(p)-'prosrc')::TEXT FROM pg_proc p "
                                       f"WHERE oid='{signature}'::REGPROCEDURE")
                    for signature in (CACHED, POSTING)}
        body = MIGRATION.read_text().replace('BEGIN;', '', 1).rsplit('COMMIT;', 1)[0]
        self.assert_sql_fails('BEGIN; GRANT EXECUTE ON FUNCTION ' + CACHED + ' TO PUBLIC;' + body,
                              'predecessor or authority differs')
        self.file(MIGRATION)
        self.assertEqual(self.readiness(), 'f')
        self.assertEqual(old_data, self.sql(data_sql))
        self.assertEqual(old_catalog, self.sql(catalog_sql))
        self.assertEqual(old_guard, self.sql(guard_sql))
        for signature, previous in metadata.items():
            self.assertEqual(previous, self.sql(f"SELECT (to_jsonb(p)-'prosrc')::TEXT FROM pg_proc p "
                                               f"WHERE oid='{signature}'::REGPROCEDURE"))
        new_contract = json.loads(self.sql(contract_sql))
        self.assertEqual(len(new_contract), 23)
        for before, after in zip(old_contract, new_contract):
            if before['signature'] == POSTING:
                self.assertNotEqual(before['definition_sha256'], after['definition_sha256'])
                self.assertEqual({k: v for k, v in before.items() if k != 'definition_sha256'},
                                 {k: v for k, v in after.items() if k != 'definition_sha256'})
            else:
                self.assertEqual(before, after)
        self.sql(self.operator('SELECT storage_v2_posting_conversion_require_operator()'))

        # An absent index uses the predecessor path, not a whole-block exact
        # union scan. A same-name unrelated index must never pass readiness.
        for (o, s, q), expected in zip(native_cases, old_native):
            self.assertEqual(self.native(o, s, q), expected)
        self.assertEqual(self.post(document, terms), old_posting)
        # Exercise the operator's real persistent-session/CIC protocol in this
        # already owned database, including exact pin reconciliation and DROP.
        operator_tests = MIGRATION.parent.parent / 'ops/storage-v2/test_exact_lexeme_indexes.py'
        spec = importlib.util.spec_from_file_location('exact_probe_operator_smoke', operator_tests)
        operator_fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(operator_fixture)
        smoke = operator_fixture.exercise_disposable_catalog_protocol(
            self.database, self.socket, self.schema.ADMIN_ID)
        self.assertEqual(smoke, {'status': 'PASS', 'sequential_concurrent_indexes': 2,
                                'original_catalog_restored': True, 'helpers_retained': True})
        self.assertEqual(old_catalog, self.sql(catalog_sql))
        self.sql(f'CREATE INDEX {NATIVE_INDEX} ON occurrence(id)')
        self.assert_sql_fails(self.admin('SELECT storage_v2_exact_lexeme_probes_ready()'),
                              'exact lexeme index definition differs')
        self.sql(f'DROP INDEX {NATIVE_INDEX}')
        self.sql(NATIVE_DDL)
        self.assertEqual(self.readiness(), 'f')
        self.sql(POSTING_DDL)
        self.assertEqual(self.readiness(), 't')
        self.assertEqual(self.sql(f"SELECT indkey::TEXT||':'||indcollation::TEXT "
                                 f'FROM pg_index WHERE indexrelid={self.quote(POSTING_INDEX)}::REGCLASS'), '0:0')
        # The complete long canonical term is represented by one bounded key,
        # rather than excluded or truncated to make the index build succeed.
        self.assertEqual(self.sql(self.admin(
            "SELECT encode(key,'hex') FROM unnest(storage_v2_compact_posting_exact_term_keys("
            f'ARRAY[{self.quote(long_term)},{self.quote(long_term)}]::TEXT[])) key')),
            hashlib.sha256(long_term.encode()).hexdigest())
        self.assertEqual(self.sql(self.admin(
            'SELECT storage_v2_compact_posting_exact_term_keys(ARRAY[]::TEXT[])')), '{}')
        # The exact installed GIN indexes alone never re-sign157's catalog.
        self.assert_sql_fails(self.operator('SELECT storage_v2_posting_conversion_require_operator()'),
                              'relation/view/trigger identity changed')
        self.assertEqual(old_guard, self.sql(guard_sql))
        self.assertEqual(old_catalog, self.sql(catalog_sql))
        self.assertEqual(old_data, self.sql(data_sql))
        for (o, s, q), expected in zip(native_cases, old_native):
            self.assertEqual(self.native(o, s, q), expected)
            self.assertEqual(self.native(o, s, q, user=self.schema.OTHER_ID), '[]')
            self.assertEqual(self.native(o, s, q, empty=True), '[]')
        self.assertEqual(self.post(document, terms), old_posting)
        # Reuse the exact156 canonical-boundary/context/authorization example.
        cache.CacheReaderMetadataTests.assert_compact_first_match_preserves_boundary_and_metadata_terms(self)
        self.assertEqual(self.sql(
            "SELECT storage_v2_compact_lexical_exact_lexemes(ARRAY[]::TSVECTOR[])"), '{}')

        # Catalog-only fixtures cover unfinished index state without launching
        # a concurrent build. Every catalog change rolls back immediately.
        self.assertEqual(self.sql('BEGIN; UPDATE pg_index SET indisvalid=FALSE '
            f"WHERE indexrelid='{POSTING_INDEX}'::REGCLASS;" +
            self.admin('SELECT storage_v2_exact_lexeme_probes_ready();') + 'ROLLBACK;'), 'f')
        self.assertEqual(self.sql('BEGIN; UPDATE pg_index SET indisready=FALSE '
            f"WHERE indexrelid='{POSTING_INDEX}'::REGCLASS;" +
            self.admin('SELECT storage_v2_exact_lexeme_probes_ready();') + 'ROLLBACK;'), 'f')
        self.assertEqual(self.readiness(), 't')
        self.assert_sql_fails('BEGIN; DROP INDEX ' + POSTING_INDEX + '; ' +
            POSTING_DDL + ' WHERE document_id>0;' +
            self.admin('SELECT storage_v2_exact_lexeme_probes_ready();'),
            'exact lexeme index definition differs')
        self.assert_sql_fails('BEGIN; ALTER FUNCTION storage_v2_compact_lexical_exact_lexemes(TSVECTOR[]) '
            "SET search_path='pg_catalog';" + self.admin('SELECT storage_v2_exact_lexeme_probes_ready();'),
            'union helper identity differs')
        self.assert_sql_fails('BEGIN; GRANT EXECUTE ON FUNCTION '
            'storage_v2_compact_posting_exact_term_keys(TEXT[]) TO PUBLIC;' +
            self.admin('SELECT storage_v2_exact_lexeme_probes_ready();'),
            'union helper identity differs')
        self.assertEqual(self.readiness(), 't')


if __name__ == '__main__':
    unittest.main()
