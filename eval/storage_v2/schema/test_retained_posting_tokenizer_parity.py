"""Complete original034 and modern posting grammars survive retained conversion.

One focused disposable fixture; the frozen original INSERT supplies the legacy
oracle independently of the candidate converter's regexp_matches locators.
"""
from __future__ import annotations

import json
import unittest

from eval.storage_v2.schema import test_retained_posting_conversion as previous
from eval.storage_v2.schema.local_operator_fixture import command as local_command

ROOT = previous.ROOT
MIGRATION = ROOT / 'migrations/165_storage_v2_retained_posting_tokenizer_parity.sql'
SIGNATURE = previous.SIGNATURE


class RetainedPostingTokenizerParityTests(unittest.TestCase):
    base = previous.RetainedPostingConversionTests.base
    schema = base.schema
    command = classmethod(local_command)
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
    retained = previous.RetainedPostingConversionTests.retained
    rank = previous.RetainedPostingConversionTests.rank
    envelope = previous.RetainedPostingConversionTests.envelope
    create = previous.RetainedPostingConversionTests.create
    call = previous.RetainedPostingConversionTests.call
    operator = previous.RetainedPostingConversionTests.operator
    apply = previous.RetainedPostingConversionTests.apply
    readers = previous.RetainedPostingConversionTests.readers
    operation = '00000000-0000-0000-0000-000000000165'
    manifest = 'd5' * 32

    @classmethod
    def authority(cls):
        return cls.sql("SELECT jsonb_build_object('owner',proowner,'acl',proacl::TEXT,"
                       "'configuration',proconfig,'security_definer',prosecdef,'arguments',proargnames)::TEXT "
                       f"FROM pg_proc WHERE oid='{SIGNATURE}'::REGPROCEDURE")

    @classmethod
    def setUpClass(cls):
        previous.RetainedPostingConversionTests.setUpClass.__func__(cls)
        try:
            cls.before_authority = cls.authority()
            cls.before_body = cls.sql(f"SELECT prosrc FROM pg_proc WHERE oid='{SIGNATURE}'::REGPROCEDURE")
            cls.sql(f"SET app.user_id='{cls.schema.ADMIN_ID}';" + MIGRATION.read_text())
        except BaseException:
            cls.base.tearDownClass.__func__(cls)
            raise

    @classmethod
    def tearDownClass(cls):
        cls.base.tearDownClass.__func__(cls)

    def original034(self, doc, *, flat):
        source = (ROOT / 'migrations/034_storage_v2_retrieval.sql').read_text()
        start = source.index('    INSERT INTO storage_v2_search_posting(document_id, term, term_frequency)')
        end = source.index('    RETURN v_document;', start)
        original = source[start:end].replace('v_document.id', str(doc)).replace(
            'p_search_text', f'(SELECT search_text FROM storage_v2_search_document WHERE id={doc})')
        # Owned fixture only: create the complete original034 representation,
        # then restore every ordinary sealed/immutable guard in this transaction.
        compact = '' if flat else f'''
            INSERT INTO storage_v2_compact_posting_block(document_id,block_order,terms,term_frequencies)
            SELECT {doc},(n-1)/256,array_agg(term ORDER BY n),array_agg(term_frequency ORDER BY n)
            FROM (SELECT term,term_frequency,row_number() OVER(ORDER BY term COLLATE "C") n
                  FROM storage_v2_search_posting WHERE document_id={doc}) ordered
            GROUP BY (n-1)/256;
            DELETE FROM storage_v2_search_posting WHERE document_id={doc};'''
        self.sql(f'''BEGIN;
            ALTER TABLE storage_v2_search_posting DISABLE TRIGGER storage_v2_search_posting_sealed;
            ALTER TABLE storage_v2_search_posting DISABLE TRIGGER storage_v2_search_posting_immutable;
            ALTER TABLE storage_v2_compact_posting_block DISABLE TRIGGER storage_v2_compact_posting_sealed;
            ALTER TABLE storage_v2_compact_posting_block DISABLE TRIGGER storage_v2_compact_posting_immutable;
            DELETE FROM storage_v2_search_posting WHERE document_id={doc};
            DELETE FROM storage_v2_compact_posting_block WHERE document_id={doc};
            {original}{compact}
            ALTER TABLE storage_v2_search_posting ENABLE TRIGGER storage_v2_search_posting_sealed;
            ALTER TABLE storage_v2_search_posting ENABLE TRIGGER storage_v2_search_posting_immutable;
            ALTER TABLE storage_v2_compact_posting_block ENABLE TRIGGER storage_v2_compact_posting_sealed;
            ALTER TABLE storage_v2_compact_posting_block ENABLE TRIGGER storage_v2_compact_posting_immutable;
            COMMIT;''')

    def pairs(self, doc):
        return self.sql(self.admin('SELECT coalesce(jsonb_agg(jsonb_build_array(term,frequency) '
            'ORDER BY term COLLATE "C"),\'[]\'::JSONB)::TEXT FROM ('
            f'SELECT term,term_frequency AS frequency FROM storage_v2_visible_flat_posting WHERE document_id={doc} '
            f'UNION ALL SELECT pair.term,pair.frequency FROM storage_v2_complete_document_posting_blocks({doc}) b '
            'CROSS JOIN LATERAL unnest(b.terms,b.term_frequencies) pair(term,frequency)) complete'))

    def identity(self, doc, occurrence):
        return self.sql(f'''SELECT jsonb_build_object(
            'document',to_jsonb(d),'seal',(SELECT to_jsonb(s) FROM storage_v2_document_postings_seal s
                WHERE s.document_id=d.id),'occurrence',to_jsonb(o),
            'bindings',(SELECT jsonb_agg(to_jsonb(b) ORDER BY ordinal) FROM storage_v2_search_view_document b
                WHERE b.view_id=o.view_id),
            'generations',(SELECT jsonb_agg(to_jsonb(g) ORDER BY id) FROM source_generation g
                WHERE g.source_id=o.source_id))::TEXT
            FROM storage_v2_search_document d CROSS JOIN occurrence o
            WHERE d.id={doc} AND o.id={occurrence}''')

    def corrupt(self, doc, *, flat, kind):
        if flat:
            if kind == 'frequency':
                change = f"UPDATE storage_v2_search_posting SET term_frequency=999 WHERE document_id={doc} AND term='.';"
            elif kind == 'incomplete':
                change = f"DELETE FROM storage_v2_search_posting WHERE document_id={doc} AND term='.';"
            else:
                change = f"INSERT INTO storage_v2_search_posting(document_id,term,term_frequency) VALUES({doc},'???',1);"
            self.sql(f'''BEGIN;
                ALTER TABLE storage_v2_search_posting DISABLE TRIGGER storage_v2_search_posting_immutable;
                ALTER TABLE storage_v2_search_posting DISABLE TRIGGER storage_v2_search_posting_sealed;
                {change}
                ALTER TABLE storage_v2_search_posting ENABLE TRIGGER storage_v2_search_posting_immutable;
                ALTER TABLE storage_v2_search_posting ENABLE TRIGGER storage_v2_search_posting_sealed;
                COMMIT;''')
        else:
            if kind == 'frequency':
                change = ("UPDATE storage_v2_compact_posting_block b SET term_frequencies=ARRAY("
                          "SELECT CASE WHEN term='.' THEN 999::BIGINT ELSE frequency END "
                          "FROM unnest(b.terms,b.term_frequencies) WITH ORDINALITY p(term,frequency,n) ORDER BY n) "
                          f'WHERE b.document_id={doc};')
            elif kind == 'incomplete':
                change = ("UPDATE storage_v2_compact_posting_block b SET "
                          "terms=array_remove(b.terms,'.'),term_frequencies=ARRAY(SELECT frequency "
                          "FROM unnest(b.terms,b.term_frequencies) WITH ORDINALITY p(term,frequency,n) "
                          f"WHERE term<>'.' ORDER BY n) WHERE b.document_id={doc};")
            else:
                change = ("UPDATE storage_v2_compact_posting_block SET terms=array_append(terms,'???'),"
                          f'term_frequencies=array_append(term_frequencies,1::BIGINT) WHERE document_id={doc};')
            self.sql(f'''BEGIN;
                ALTER TABLE storage_v2_compact_posting_block DISABLE TRIGGER storage_v2_compact_posting_immutable;
                {change}
                ALTER TABLE storage_v2_compact_posting_block ENABLE TRIGGER storage_v2_compact_posting_immutable;
                COMMIT;''')

    def test_complete_historical_and_modern_grammars_exact_readers_replay_and_corruption(self):
        self.assertEqual(self.before_authority, self.authority())
        body = self.sql(f"SELECT prosrc FROM pg_proc WHERE oid='{SIGNATURE}'::REGPROCEDURE")
        self.assertEqual(body.count('regexp_matches('), self.before_body.count('regexp_matches('))
        self.assertIn("OR keep_punctuation_only", body)
        # Whitespace separates one-byte punctuation, repeated punctuation, and
        # multibyte punctuation. UTF-8 before each locator differs from characters.
        text = 'alpha beta alpha 日本語9 api::call() long_identifier_ééééé \t.\n!\r\n. -- — — '
        terms = ['alpha', '日本語9', 'api::call()', '.', '!', '--', '—', 'missing']
        for legacy in (False, True):
            for flat in (False, True):
                with self.subTest(legacy=legacy, flat=flat):
                    row, occurrence = self.create(text + f'epoch_{legacy}_flat_{flat}', flat=flat)
                    doc = row['document']
                    if legacy:
                        self.original034(doc, flat=flat)
                        row['pairs'] = self.sql(f"SELECT encode(storage_v2_retained_posting_pairs_sha256({doc}),'hex')")
                    before_pairs = self.pairs(doc)
                    expected = dict(json.loads(before_pairs))
                    self.assertEqual(expected.get('.'), 2 if legacy else None)
                    self.assertEqual(expected.get('—'), 2 if legacy else None)
                    self.assertEqual(expected.get('!'), 1 if legacy else None)
                    before = self.readers(doc, terms)
                    identity = self.identity(doc, occurrence)
                    ranks = {q: self.rank(occurrence, q) for q in ('alpha', 'alpha -beta', '"alpha beta"')}
                    self.assertTrue(json.loads(ranks['alpha']))
                    receipt = self.apply(row)
                    self.assertEqual(receipt['disposition'], 'CONVERTED')
                    self.assertEqual(receipt['original_representation'], 'FLAT' if flat else 'COMPACT')
                    self.assertEqual(receipt['posting_count'], len(expected))
                    self.assertEqual(before_pairs, self.pairs(doc))
                    self.assertEqual(before, self.readers(doc, terms))
                    self.assertEqual(identity, self.identity(doc, occurrence))
                    for query, rank in ranks.items():
                        self.assertEqual(rank, self.rank(occurrence, query))
                        self.assertEqual(self.rank(occurrence, query, actor=self.schema.OTHER_ID), '[]')
                    self.assertEqual(self.apply(row)['disposition'], 'REPLAY')
                    self.assertEqual(before_pairs, self.pairs(doc))
                    self.assertEqual(self.sql(f'SELECT count(*) FROM storage_v2_retained_posting_conversion_intent '
                                             f'WHERE document_id={doc}'), '0')
        # Having one punctuation-only retained row chooses the whole original
        # grammar: forged extras, wrong frequencies and partial subsets all fail.
        for flat in (False, True):
            for kind in ('noncanonical', 'frequency', 'incomplete'):
                with self.subTest(flat=flat, corruption=kind):
                    row, occurrence = self.create(text + f'corrupt_{kind}_{flat}', flat=flat)
                    doc = row['document']
                    self.original034(doc, flat=flat)
                    self.corrupt(doc, flat=flat, kind=kind)
                    row['pairs'] = self.sql(f"SELECT encode(storage_v2_retained_posting_pairs_sha256({doc}),'hex')")
                    before_pairs = self.pairs(doc)
                    identity = self.identity(doc, occurrence)
                    self.assert_sql_fails(self.operator(self.call(row)), 'differ from canonical content')
                    self.assertEqual(before_pairs, self.pairs(doc))
                    self.assertEqual(identity, self.identity(doc, occurrence))
                    self.assertEqual(self.sql(f'SELECT count(*) FROM storage_v2_byte_posting_block WHERE document_id={doc}'), '0')
                    self.assertEqual(self.sql(f'SELECT count(*) FROM storage_v2_retained_posting_conversion_receipt '
                                             f'WHERE document_id={doc}'), '0')
                    self.assertEqual(self.sql(f'SELECT count(*) FROM storage_v2_retained_posting_conversion_intent '
                                             f'WHERE document_id={doc}'), '0')
        self.assert_sql_fails(f"SET app.user_id='{self.schema.ADMIN_ID}';" + MIGRATION.read_text(),
                              'predecessor body differs')


if __name__ == '__main__':
    unittest.main()
