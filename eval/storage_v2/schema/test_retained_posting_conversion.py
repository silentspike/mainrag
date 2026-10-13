"""Atomic retained posting conversion, actual readers and exact recovery.

One disposable PostgreSQL fixture; no production, timing or saving claims.
"""
from __future__ import annotations

import hashlib
import json
import unittest

from eval.storage_v2.schema import test_document_cache_conversion as previous

ROOT = previous.ROOT
MIGRATION = ROOT / 'migrations/164_storage_v2_convert_retained_posting_blocks.sql'
SIGNATURE = 'storage_v2_convert_retained_postings(uuid,bytea,bigint,bytea,bytea,bigint,integer,bigint,bytea)'


class RetainedPostingConversionTests(unittest.TestCase):
    base = previous.DocumentCacheConversionTests.base
    schema = base.schema
    command = classmethod(base.command.__func__)
    sql = classmethod(base.sql.__func__)
    file = classmethod(base.file.__func__)
    actor = staticmethod(base.actor)
    admin = classmethod(base.admin.__func__)
    quote = staticmethod(base.quote)
    make_projection = base.make_projection
    begin = base.begin
    stage = base.stage
    complete_analysis = base.complete_analysis
    commit = base.commit
    fixture = base.fixture
    assert_sql_fails = base.assert_sql_fails
    retained = previous.DocumentCacheConversionTests.retained
    rank = previous.DocumentCacheConversionTests.rank
    envelope = previous.DocumentCacheConversionTests.envelope
    operation = '00000000-0000-0000-0000-000000000164'
    manifest = 'd4' * 32

    @classmethod
    def setUpClass(cls):
        previous.DocumentCacheConversionTests.setUpClass.__func__(cls)
        try:
            cls.file(next(MIGRATION.parent.glob('162_*.sql')))
            cls.sql(f"SET app.user_id='{cls.schema.ADMIN_ID}';" + next(MIGRATION.parent.glob('163_*.sql')).read_text())
            cls.sql(f"SET app.user_id='{cls.schema.ADMIN_ID}';" + MIGRATION.read_text())
        except BaseException:
            cls.base.tearDownClass.__func__(cls)
            raise

    @classmethod
    def tearDownClass(cls):
        cls.base.tearDownClass.__func__(cls)

    def create(self, text, *, flat=False):
        row, occurrence = self.retained(text)
        doc = row['document']
        if flat:
            # Only this owned fixture recreates historical flat rows. Every
            # pair is copied before the ordinary immutable guards resume.
            self.sql(f'''BEGIN;
                ALTER TABLE storage_v2_search_posting DISABLE TRIGGER storage_v2_search_posting_sealed;
                ALTER TABLE storage_v2_compact_posting_block DISABLE TRIGGER storage_v2_compact_posting_immutable;
                INSERT INTO storage_v2_search_posting(document_id,term,term_frequency)
                SELECT {doc},pair.term,pair.frequency FROM storage_v2_compact_posting_block b
                CROSS JOIN LATERAL unnest(b.terms,b.term_frequencies) pair(term,frequency)
                WHERE b.document_id={doc};
                DELETE FROM storage_v2_compact_posting_block WHERE document_id={doc};
                ALTER TABLE storage_v2_search_posting ENABLE TRIGGER storage_v2_search_posting_sealed;
                ALTER TABLE storage_v2_compact_posting_block ENABLE TRIGGER storage_v2_compact_posting_immutable;
                COMMIT;''')
        row['pairs'] = self.sql(f"SELECT encode(storage_v2_retained_posting_pairs_sha256({doc}),'hex')")
        return row, occurrence

    def call(self, row, *, maximum=8388608, budget=10000, operation=None, pairs=None, extra=''):
        return ("SELECT coalesce(json_agg(r),'[]'::JSON) FROM storage_v2_convert_retained_postings("
                f"'{operation or self.operation}',decode('{self.manifest}','hex'),{row['document']},"
                f"decode('{row['identity']}','hex'),decode('{pairs or row['pairs']}','hex'),"
                f'{maximum},{budget}{extra}) r')

    def operator(self, statement, *, timeout='10s'):
        return ('BEGIN;SET LOCAL lock_timeout=\'1s\';SET LOCAL statement_timeout=' + self.quote(timeout) + ';'
                f"SET LOCAL app.user_id='{self.schema.ADMIN_ID}';" + statement + ';COMMIT;')

    def apply(self, row, **kwargs):
        return json.loads(self.sql(self.operator(self.call(row, **kwargs))))[0]

    def readers(self, doc, terms):
        array = 'ARRAY[' + ','.join(self.quote(t) for t in terms) + ']::TEXT[]'
        calls = {
            'point': f'unnest({array}) t(term) CROSS JOIN LATERAL storage_v2_document_posting({doc},t.term)',
            'scoped_term': f'unnest({array}) t(term) CROSS JOIN LATERAL storage_v2_scoped_term_posting(ARRAY[{doc},{doc},NULL]::BIGINT[],t.term)',
            'scoped_query': f'storage_v2_scoped_query_posting(array_fill({doc}::BIGINT,ARRAY[1025]),{array})',
            'probe': f'unnest({array}) t(term) CROSS JOIN LATERAL storage_v2_posting_probe(t.term,4097)',
        }
        result = {}
        for label, source in calls.items():
            # Every public posting path supplies its own term/frequency columns.
            if label in ('point', 'scoped_term', 'probe'):
                source = source.replace(' CROSS JOIN LATERAL ', ' CROSS JOIN LATERAL ') + ' p'
                select = 'p.term,p.term_frequency'
                where = f' WHERE p.document_id={doc}' if label == 'probe' else ''
            else:
                source += ' p'
                select = 'p.term,p.term_frequency'
                where = ''
            result[label] = self.sql(self.admin("SELECT coalesce(jsonb_agg(to_jsonb(q) ORDER BY term,term_frequency),'[]'::JSONB) FROM ("
                                              f'SELECT {select} FROM {source}{where}) q'))
        # The private complete-block helper covers cached/byte blocks; combine
        # its exact pairs with visible flat rows for representation-independent
        # logical comparison, rather than comparing physical block partitions.
        result['complete'] = self.sql(self.admin("SELECT coalesce(jsonb_agg(jsonb_build_array(term,frequency) ORDER BY term COLLATE \"C\"),'[]'::JSONB) FROM ("
            f'SELECT term,term_frequency AS frequency FROM storage_v2_visible_flat_posting WHERE document_id={doc} '
            f'UNION ALL SELECT pair.term,pair.frequency FROM storage_v2_complete_document_posting_blocks({doc}) b '
            'CROSS JOIN LATERAL unnest(b.terms,b.term_frequencies) pair(term,frequency)) complete'))
        result['identifiers'] = self.sql(self.admin(f'SELECT to_json(storage_v2_document_exact_identifiers({doc}))'))
        result['words'] = self.sql(self.admin(f'SELECT to_json(storage_v2_document_word_identifiers({doc}))'))
        return result

    def test_compact_and_flat_exact_reader_scope_seal_identity_and_replay(self):
        long_term = 'long_' + 'é' * 2048
        collision = {}
        for n in range(20000):
            word = f'collision{n}'
            fingerprint = hashlib.sha256(word.encode()).digest()[:2]
            if fingerprint in collision:
                pair = (collision[fingerprint], word)
                break
            collision[fingerprint] = word
        else:
            self.fail('deterministic public fingerprint collision not found')
        text = ('alpha beta gamma alpha api::call() quote\'s::value() 日本語9 key_42 İ ' + long_term + ' ' + pair[0]
                + ' ' + ' '.join(f'key_{n:04}' for n in range(600)))
        for flat in (False, True):
            with self.subTest(flat=flat):
                # Identical source text intentionally shares its native
                # document. Give the historical layout fixtures distinct
                # content so the second case cannot reuse the first case's
                # already converted BYTE owner.
                fixture_text = text + (' retained_flat_fixture' if flat else ' retained_compact_fixture')
                row, occurrence = self.create(fixture_text, flat=flat)
                doc = row['document']
                terms = ['alpha', 'api::call()', "quote's::value()", '日本語9', 'key_42', long_term, *pair, 'missing']
                before = self.readers(doc, terms)
                self.assertTrue(json.loads(before['point']))
                generation = int(self.sql('SELECT r.generation_id FROM storage_v2_ingest_run_item item '
                    f'JOIN storage_v2_ingest_run r ON r.id=item.run_id WHERE item.occurrence_id={occurrence} LIMIT 1'))
                self.sql(self.admin(f"SELECT storage_v2_verify_generation({generation},'{ 'a3' * 32}')"))
                verification = self.sql(self.admin(f'SELECT storage_v2_verify_lexical_segment_page({generation},0,64)'))
                query = {'type': 'phrase', 'value': 'alpha beta'}
                envelope = self.envelope(row['source'], query)
                self.assertGreater(json.loads(envelope)['total'], 0)
                ranks = {q: self.rank(occurrence, q) for q in ('alpha', 'alpha -beta', '"alpha beta"')}
                self.assertTrue(json.loads(ranks['alpha']))
                document = self.sql(f'SELECT to_jsonb(d)::TEXT FROM storage_v2_search_document d WHERE id={doc}')
                seal = self.sql(f'SELECT to_jsonb(s)::TEXT FROM storage_v2_document_postings_seal s WHERE document_id={doc}')
                generations = self.sql('SELECT jsonb_agg(to_jsonb(g) ORDER BY id)::TEXT FROM source_generation g')
                result = self.apply(row)
                self.assertEqual(result['disposition'], 'CONVERTED')
                self.assertEqual(result['original_representation'], 'FLAT' if flat else 'COMPACT')
                after = self.readers(doc, terms)
                self.assertEqual(before, after)
                self.assertEqual(document, self.sql(f'SELECT to_jsonb(d)::TEXT FROM storage_v2_search_document d WHERE id={doc}'))
                self.assertEqual(seal, self.sql(f'SELECT to_jsonb(s)::TEXT FROM storage_v2_document_postings_seal s WHERE document_id={doc}'))
                self.assertEqual(generations, self.sql('SELECT jsonb_agg(to_jsonb(g) ORDER BY id)::TEXT FROM source_generation g'))
                self.assertEqual(envelope, self.envelope(row['source'], query))
                self.assertEqual(verification, self.sql(self.admin(f'SELECT storage_v2_verify_lexical_segment_page({generation},0,64)')))
                for q in ranks:
                    self.assertEqual(ranks[q], self.rank(occurrence, q))
                    self.assertEqual(self.rank(occurrence, q, actor=self.schema.OTHER_ID), '[]')
                self.assertEqual(self.apply(row)['disposition'], 'REPLAY')
                # The ordinary constructor must reuse that converted native
                # document, preserving its conversion receipt and byte payload.
                reused = self.sql(self.admin("SELECT (storage_v2_put_search_document('mainrag.lexical-simple.v1','node',"
                    f"(SELECT node_id FROM storage_v2_search_document WHERE id={doc}),{self.quote(fixture_text)},ARRAY[]::TEXT[])).id"))
                self.assertEqual(int(reused), doc)
                self.assertEqual(self.sql(f"SELECT storage_v2_retained_byte_posting_payload_sha256({doc})="
                    f"byte_payload_sha256 FROM storage_v2_retained_posting_conversion_receipt WHERE document_id={doc}"), 't')
                self.assert_sql_fails(self.operator(self.call(row, operation='00000000-0000-0000-0000-000000000999')),
                                      'replay identity or representation differs')
                self.assertEqual(self.sql(f'SELECT count(*) FROM storage_v2_retained_posting_conversion_intent WHERE document_id={doc}'), '0')

    def test_authority_frozen_manifest_budget_and_missing_replay_target(self):
        row, _ = self.create('alpha beta key_77 日本語9')
        self.assert_sql_fails(self.admin(self.call(row)), 'permission denied')
        self.assert_sql_fails("SET app.user_id='" + str(self.schema.OTHER_ID) + "';" + self.call(row), 'administrative operator')
        self.assert_sql_fails(self.operator(self.call(row, pairs='01' * 32)), 'frozen exact manifest')
        self.assert_sql_fails(self.operator(self.call(row, maximum=1)), 'body-byte admission exceeded')
        self.assert_sql_fails(self.operator(self.call(row, budget=100), timeout='1s'), 'prearmed')
        self.assert_sql_fails(self.operator(self.call(dict(row, document=999999999))), 'document absent')
        self.assertEqual(self.apply(row)['disposition'], 'CONVERTED')

    def test_canonical_frequency_failure_and_transaction_rollback(self):
        row, _ = self.create('alpha alpha beta key_88')
        doc = row['document']
        self.sql('ALTER TABLE storage_v2_compact_posting_block DISABLE TRIGGER storage_v2_compact_posting_immutable;'
                 f'UPDATE storage_v2_compact_posting_block SET term_frequencies=array_fill(999::BIGINT,ARRAY[cardinality(terms)]) WHERE document_id={doc};'
                 'ALTER TABLE storage_v2_compact_posting_block ENABLE TRIGGER storage_v2_compact_posting_immutable;')
        row['pairs'] = self.sql(f"SELECT encode(storage_v2_retained_posting_pairs_sha256({doc}),'hex')")
        before = self.sql(f'SELECT to_jsonb(b)::TEXT FROM storage_v2_compact_posting_block b WHERE document_id={doc}')
        self.assert_sql_fails(self.operator(self.call(row)), 'differ from canonical content')
        self.assertEqual(before, self.sql(f'SELECT to_jsonb(b)::TEXT FROM storage_v2_compact_posting_block b WHERE document_id={doc}'))
        self.assertEqual(self.sql(f'SELECT count(*) FROM storage_v2_byte_posting_block WHERE document_id={doc}'), '0')
        row, _ = self.create('alpha beta rollback_key')
        before = self.sql(f'SELECT to_jsonb(b)::TEXT FROM storage_v2_compact_posting_block b WHERE document_id={row["document"]}')
        # An acknowledged SQL function result inside an uncommitted transaction
        # is not success. Force failure after the complete delete+insert switch.
        self.assert_sql_fails(self.operator(self.call(row) + ';SELECT 1/0'), 'division by zero')
        self.assertEqual(before, self.sql(f'SELECT to_jsonb(b)::TEXT FROM storage_v2_compact_posting_block b WHERE document_id={row["document"]}'))
        self.assertEqual(self.sql(f'SELECT count(*) FROM storage_v2_retained_posting_conversion_receipt WHERE document_id={row["document"]}'), '0')

    def test_existing_guard_contract_and_controlled_inert_run(self):
        row, _ = self.create('alpha writer_key')
        doc = row['document']
        self.sql(f"SET app.user_id='{self.schema.ADMIN_ID}';SELECT storage_v2_posting_conversion_require_operator()")
        self.sql(f"SET app.user_id='{self.schema.ADMIN_ID}';SELECT storage_v2_require_lexical_conversion_operator()")
        self.assert_sql_fails(self.operator(f"SET LOCAL mainrag.retained_posting_operation='{self.operation}';DELETE FROM storage_v2_compact_posting_block WHERE document_id={doc}"),
                              'immutable outside its exact conversion intent')
        self.assert_sql_fails(self.operator(f"INSERT INTO storage_v2_compact_posting_block(document_id,block_order,terms,term_frequencies) VALUES({doc},999,ARRAY['forged'],ARRAY[1::BIGINT])"),
                              'sealed search-document postings are immutable')
        # Keep the real unfinished run and its authorized exception in one
        # rolled-back transaction. No fake terminal run status is introduced.
        begin = (f"SELECT (storage_v2_begin_shadow_ingest({row['source']},repeat('ab',32),repeat('cd',32),"
                 "'retained-posting-pause','synthetic-snapshot','{}'::JSONB,FALSE)).id")
        self.assert_sql_fails(self.operator(begin + ';' + self.call(row)), 'exact quiescent maintenance boundary')
        call = self.call(row, extra=",run_id,decode(repeat('00',32),'hex')")
        self.sql(self.operator(f'''DO $inert$ DECLARE run_id BIGINT; frozen BYTEA; disposition TEXT; rejected BOOLEAN:=FALSE;
BEGIN
 run_id:=({begin});frozen:=storage_v2_retained_posting_run_sha256(run_id);
 BEGIN PERFORM 1 FROM ({call}) wrong;
 EXCEPTION WHEN raise_exception THEN
  IF SQLERRM NOT LIKE '%exact quiescent maintenance boundary%' THEN RAISE;END IF;rejected:=TRUE;
 END;
 IF NOT rejected THEN RAISE EXCEPTION 'incorrect run hash admitted';END IF;
 SELECT result.disposition INTO disposition FROM storage_v2_convert_retained_postings(
  '{self.operation}',decode('{self.manifest}','hex'),{doc},decode('{row['identity']}','hex'),
  decode('{row['pairs']}','hex'),8388608,10000,run_id,frozen) result;
 IF disposition<>'CONVERTED' OR storage_v2_retained_posting_run_sha256(run_id) IS DISTINCT FROM frozen THEN
  RAISE EXCEPTION 'inert run identity or conversion changed';END IF;
END $inert$''').rsplit('COMMIT;', 1)[0] + 'ROLLBACK;')

    def test_ledger_gc_roots_missing_target_and_legitimate_cache_transition(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('retained_posting_gc_fixture', ROOT / 'ops/storage-v2/native-gc.py')
        gc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gc)
        row, _ = self.create('alpha beta audit_key')
        def roots():
            return self.sql('BEGIN;' + gc.graph_sql([]) +
                "SELECT coalesce(jsonb_agg(jsonb_build_array(kind,id) ORDER BY kind,id),'[]'::JSONB) FROM gc_mark;ROLLBACK;")
        before = roots()
        self.assertEqual(self.apply(row)['disposition'], 'CONVERTED')
        self.assertEqual(roots(), before)
        ledger = 'storage_v2_retained_posting_conversion_receipt'
        self.assertEqual(self.sql(f"SELECT count(*) FROM pg_constraint WHERE conrelid='{ledger}'::REGCLASS AND contype='f'"), '0')
        self.assertNotIn(ledger, gc.TARGETS)
        self.assertNotIn(ledger, gc.LINKS)
        # Existing legitimate SQL161 and SQL138 transitions stay usable after
        # the guard contract advances, and do not invalidate logical replay.
        cache = (f"SELECT * FROM storage_v2_convert_document_caches('{self.operation}',decode('{self.manifest}','hex'),"
                 f"ARRAY[{row['document']}]::BIGINT[],ARRAY[decode('{row['identity']}','hex')],8388608,10000)")
        self.sql(self.operator(cache))
        self.sql(self.admin(f"SELECT * FROM storage_v2_derive_document_identifiers({row['document']-1},1,TRUE)"))
        self.assertEqual(self.apply(row)['disposition'], 'REPLAY')
        transient = int(self.sql(self.admin("SELECT id FROM storage_v2_put_search_document('retained-posting-unbound','node',"
            f"(SELECT node_id FROM storage_v2_search_document WHERE id={row['document']}),'alpha beta audit_key',ARRAY[]::TEXT[])")))
        target = {'document':transient,'identity':self.sql(f"SELECT encode(materialization_sha256,'hex') FROM storage_v2_search_document WHERE id={transient}"),
                  'pairs':self.sql(f"SELECT encode(storage_v2_retained_posting_pairs_sha256({transient}),'hex')")}
        self.assertEqual(self.apply(target)['disposition'], 'CONVERTED')
        self.assertNotIn(['storage_v2_search_document', transient], json.loads(roots()))
        self.sql('BEGIN;' + f'''
ALTER TABLE storage_v2_byte_posting_block DISABLE TRIGGER storage_v2_byte_posting_immutable;
DELETE FROM storage_v2_byte_posting_block WHERE document_id={transient};
ALTER TABLE storage_v2_byte_posting_block ENABLE TRIGGER storage_v2_byte_posting_immutable;
ALTER TABLE storage_v2_document_postings_seal DISABLE TRIGGER document_postings_seal_immutable;
DELETE FROM storage_v2_document_postings_seal WHERE document_id={transient};
ALTER TABLE storage_v2_document_postings_seal ENABLE TRIGGER document_postings_seal_immutable;
ALTER TABLE storage_v2_search_document DISABLE TRIGGER storage_v2_search_document_immutable;
DELETE FROM storage_v2_search_document WHERE id={transient};
ALTER TABLE storage_v2_search_document ENABLE TRIGGER storage_v2_search_document_immutable;
SET LOCAL lock_timeout='1s';SET LOCAL statement_timeout='10s';SET LOCAL app.user_id='{self.schema.ADMIN_ID}';
DO $missing$ DECLARE rejected BOOLEAN:=FALSE;BEGIN
 BEGIN PERFORM 1 FROM ({self.call(target)}) replay;
 EXCEPTION WHEN raise_exception THEN
  IF SQLERRM NOT LIKE '%named posting document absent%' THEN RAISE;END IF;rejected:=TRUE;
 END;
 IF NOT rejected THEN RAISE EXCEPTION 'missing live target accepted';END IF;
 IF NOT EXISTS(SELECT 1 FROM {ledger} WHERE document_id={transient}) THEN RAISE EXCEPTION 'audit lost';END IF;
END $missing$;ROLLBACK;''')


if __name__ == '__main__':
    unittest.main()
