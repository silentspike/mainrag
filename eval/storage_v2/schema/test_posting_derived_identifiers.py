"""Lossless identifier representation, complete readers and conversion boundaries."""
import json
import unittest

from eval.storage_v2.schema import test_native_legacy_hit_resolution as fixture

MIGRATION = fixture.MIGRATION.parent / '138_storage_v2_posting_derived_identifiers.sql'


class PostingDerivedIdentifierTests(unittest.TestCase):
    schema = fixture.NativeLegacyHitResolutionTests.schema
    command = classmethod(fixture.NativeLegacyHitResolutionTests.command.__func__)
    sql = classmethod(fixture.NativeLegacyHitResolutionTests.sql.__func__)
    file = classmethod(fixture.NativeLegacyHitResolutionTests.file.__func__)
    admin = classmethod(fixture.NativeLegacyHitResolutionTests.admin.__func__)
    actor = staticmethod(fixture.NativeLegacyHitResolutionTests.actor)
    quote = staticmethod(fixture.NativeLegacyHitResolutionTests.quote)
    make_projection = fixture.NativeLegacyHitResolutionTests.make_projection
    begin = fixture.NativeLegacyHitResolutionTests.begin
    stage = fixture.NativeLegacyHitResolutionTests.stage
    complete_analysis = fixture.NativeLegacyHitResolutionTests.complete_analysis
    commit = fixture.NativeLegacyHitResolutionTests.commit
    assert_sql_fails = fixture.NativeLegacyHitResolutionTests.assert_sql_fails

    @classmethod
    def setUpClass(cls):
        try:
            fixture.NativeLegacyHitResolutionTests.setUpClass.__func__(cls)
            cls.file(MIGRATION.parent / '118_storage_v2_reuse_constructor_tokens.sql')
            cls.file(MIGRATION.parent / '137_storage_v2_first_lexical_candidates.sql')
        except BaseException:
            if hasattr(cls, 'stack'):
                cls.stack.close()
            raise

    @classmethod
    def tearDownClass(cls):
        fixture.NativeLegacyHitResolutionTests.tearDownClass.__func__(cls)

    def put(self, node, text, identifiers):
        values = 'ARRAY[' + ','.join(self.quote(value) for value in identifiers) + ']::TEXT[]'
        return int(self.sql(self.admin(
            "SELECT id FROM storage_v2_put_search_document('posting-identifiers-fixture-v1','node',"
            f'{node},{self.quote(text)},{values})')))

    def envelopes(self, sequence, asts):
        result = {}
        for number, ast in enumerate(asts):
            quoted = self.quote(json.dumps(ast)) + '::JSONB'
            for actor in (self.schema.ADMIN_ID, self.schema.WRITER_ID):
                result[(number, actor, 'named')] = self.sql(self.actor(actor,
                    f"SELECT storage_v2_search_exact(6,'{sequence}',{quoted},'{{}}',10)::TEXT"))
                result[(number, actor, 'active')] = self.sql(self.actor(actor,
                    f"SELECT storage_v2_search_active_unchecked(repeat('a',64),{quoted},'{{}}',10,NULL,FALSE)::TEXT"))
        return result

    def test_complete_envelopes_identity_reuse_and_bounded_conversion(self):
        long_identifier = 'identifier_' + 'x' * 32000
        records = [
            ('alpha key_1 key_1 42 plain', ['42', 'key_1']),
            ('alpha custom payload', ['external::key']),
            ('alpha ' + long_identifier + ' alpha', [long_identifier]),
            ('alpha 日本語9_unicode', ['日本語9_unicode']),
        ]
        run = self.begin(6, '71' * 32, '72' * 32)
        documents = []
        for number, (text, identifiers) in enumerate(records):
            node, view, digest = self.make_projection(text)
            document = self.put(node, text, identifiers)
            self.sql(self.admin(f'SELECT storage_v2_bind_search_document({view},0,{document},1.0)'))
            self.stage(run, f'identifier-{number}.txt', text, node, view, digest)
            self.complete_analysis(digest)
            documents.append((document, node))
        self.commit(run, len(records))
        sequence = self.sql(f'SELECT generation_seq FROM source_generation WHERE id='
                            f'(SELECT generation_id FROM storage_v2_ingest_run WHERE id={run})')
        for source in json.loads(self.sql('SELECT json_agg(id ORDER BY id) FROM sources')):
            if source != 6:
                self.commit(self.begin(source, f'{source+100:064x}', f'{source+200:064x}'), 0)
        # Only this disposable fixture changes pointers; it does not claim an
        # accepted production activation or weaken the production procedure.
        self.sql("""ALTER TABLE source_generation DISABLE TRIGGER USER;
            ALTER TABLE logical_source DISABLE TRIGGER USER;
            UPDATE source_generation SET status='active',activated_at=now();
            UPDATE logical_source p SET active_generation_id=g.id
                FROM source_generation g WHERE g.source_id=p.id;
            ALTER TABLE source_generation ENABLE TRIGGER USER;
            ALTER TABLE logical_source ENABLE TRIGGER USER;
            ALTER TABLE storage_v2_activation_set_evidence DISABLE TRIGGER USER;
            INSERT INTO storage_v2_activation_set_evidence
                (id,manifest_sha256,source_count,pointer_set_sha256,source_classification_sha256)
                SELECT '00000000-0000-0000-0000-000000000138',repeat('a',64),count(*),repeat('b',64),
                    encode(digest(convert_to(jsonb_agg(jsonb_build_object('source_id',id,'is_test',is_test)
                        ORDER BY id)::TEXT,'UTF8'),'sha256'),'hex') FROM sources;
            ALTER TABLE storage_v2_activation_set_evidence ENABLE TRIGGER USER;""")
        asts = [
            {'type': 'term', 'value': 'alpha'},
            {'type': 'exact', 'value': 'key_1'},
            {'type': 'exact', 'value': '日本語9_unicode'},
            {'type': 'exact', 'value': '42'},
            {'type': 'exact', 'value': 'external::key'},
            {'type': 'exact', 'value': long_identifier},
            {'type': 'exact', 'value': 'missing_1'},
            {'type': 'phrase', 'value': 'alpha custom'},
            {'type': 'and', 'children': [
                {'type': 'term', 'value': 'alpha'}, {'type': 'exact', 'value': 'key_1'}]},
        ]
        expected = self.envelopes(sequence, asts)
        self.assertGreater(json.loads(expected[(1, self.schema.ADMIN_ID, 'named')])['total'], 0)
        self.assert_sql_fails(self.actor(self.schema.OTHER_ID,
            f"SELECT storage_v2_search_exact(6,'{sequence}','{{\"type\":\"term\",\"value\":\"alpha\"}}','{{}}',10)"),
            'authorized generation selector')
        hashes = self.sql('SELECT jsonb_agg(jsonb_build_array(id,encode(materialization_sha256,\'hex\')) '
                          'ORDER BY id)::TEXT FROM storage_v2_search_document')
        generations = self.sql('SELECT jsonb_agg(to_jsonb(g) ORDER BY id)::TEXT FROM source_generation g')
        body = MIGRATION.read_text().replace('BEGIN;', '', 1).rsplit('COMMIT;', 1)[0]
        self.assert_sql_fails('BEGIN; ALTER FUNCTION storage_v2_put_search_document('
            'TEXT,TEXT,BIGINT,TEXT,TEXT[]) OWNER TO storage_v2_shadow_worker; '
            + body + 'ROLLBACK;', 'posting-derived identifier authority differs')
        self.file(MIGRATION)
        self.assertEqual(self.envelopes(sequence, asts), expected)
        self.assert_sql_fails(self.admin('UPDATE storage_v2_search_document '
            f'SET exact_identifiers_derived=TRUE WHERE id={documents[0][0]}'),
            'require sealed postings')
        self.assertEqual(self.sql("SELECT storage_v2_document_has_exact_identifier(-1,'missing_1')"), 'f')
        for (document, node), (text, identifiers) in zip(documents, records):
            self.assertEqual(self.put(node, text, identifiers), document)
            self.assertEqual(json.loads(self.sql(
                f'SELECT to_json(storage_v2_document_exact_identifiers({document}))')), identifiers)

        self.assert_sql_fails(self.actor(self.schema.WRITER_ID,
            'SELECT * FROM storage_v2_derive_document_identifiers(0,256,FALSE)'),
            'administrator authority')
        self.assert_sql_fails(self.admin(
            'SELECT * FROM storage_v2_derive_document_identifiers(0,257,FALSE)'),
            'bounded identifier conversion cursor')
        converted = json.loads(self.sql(self.admin(
            'SELECT row_to_json(x) FROM storage_v2_derive_document_identifiers(0,256,FALSE) x')))
        self.assertGreaterEqual(converted['converted'], 2)
        self.assertEqual(converted['removed_logical_identifier_bytes'], 0)
        custom_id = documents[1][0]
        self.assertEqual(self.sql(f'SELECT exact_identifiers_derived FROM '
                                 f'storage_v2_search_document WHERE id={custom_id}'), 'f')
        self.assertEqual(self.envelopes(sequence, asts), expected)
        compacted = json.loads(self.sql(self.admin(
            'SELECT row_to_json(x) FROM storage_v2_derive_document_identifiers(0,256,TRUE) x')))
        self.assertGreaterEqual(compacted['removed_logical_identifier_bytes'], len(long_identifier))
        self.assertEqual(self.envelopes(sequence, asts), expected)
        for (document, node), (text, identifiers) in zip(documents, records):
            self.assertEqual(self.put(node, text, identifiers), document)
            self.assertEqual(json.loads(self.sql(
                f'SELECT to_json(storage_v2_document_exact_identifiers({document}))')), identifiers)
        self.assertEqual(self.sql('SELECT jsonb_agg(jsonb_build_array(id,encode(materialization_sha256,\'hex\')) '
                                  'ORDER BY id)::TEXT FROM storage_v2_search_document'), hashes)
        self.assertEqual(self.sql('SELECT jsonb_agg(to_jsonb(g) ORDER BY id)::TEXT FROM source_generation g'),
                         generations)
        self.assertEqual(json.loads(self.sql(self.admin(
            'SELECT row_to_json(x) FROM storage_v2_derive_document_identifiers(0,256,TRUE) x')))['converted'], 0)
        self.assert_sql_fails(self.actor(self.schema.WRITER_ID,
            'SELECT * FROM storage_v2_restore_document_identifiers(0,256)'), 'administrator authority')
        restored = json.loads(self.sql(self.admin(
            'SELECT row_to_json(x) FROM storage_v2_restore_document_identifiers(0,256) x')))
        self.assertGreaterEqual(restored['restored'], 2)
        for (document, _), (_, identifiers) in zip(documents, records):
            self.assertEqual(json.loads(self.sql(
                f'SELECT to_json(exact_identifiers) FROM storage_v2_search_document WHERE id={document}')),
                identifiers)
        self.assertEqual(self.envelopes(sequence, asts), expected)
        self.assertEqual(json.loads(self.sql(self.admin(
            'SELECT row_to_json(x) FROM storage_v2_restore_document_identifiers(0,256) x')))['restored'], 0)

        text = 'fresh_key 42 42 unicode_9'
        identifiers = ['42', 'fresh_key', 'unicode_9']
        node, view, digest = self.make_projection(text)
        fresh = self.put(node, text, identifiers)
        state = json.loads(self.sql(f"SELECT json_build_object('derived',exact_identifiers_derived,"
            f"'stored',exact_identifiers,'full',storage_v2_document_exact_identifiers(id)) "
            f'FROM storage_v2_search_document WHERE id={fresh}'))
        self.assertEqual(state, {'derived': True, 'stored': [], 'full': identifiers})
        self.assertEqual(self.put(node, text, identifiers), fresh)
        for statement in (
            f"INSERT INTO storage_v2_search_posting(document_id,term,term_frequency) VALUES({fresh},'invented_key',1)",
            f"INSERT INTO storage_v2_compact_posting_block(document_id,block_order,terms,term_frequencies) "
            f"VALUES({fresh},100000,ARRAY['invented_key'],ARRAY[1::BIGINT])",
        ):
            self.assert_sql_fails(self.admin(statement), 'sealed search-document postings are immutable')
        # C and Unicode-aware database locales can tokenize differently.
        # Eligibility is exact set equality, so Unicode always remains complete.
        unicode_text = 'Unicode_日本語9'
        unicode_identifiers = ['unicode_日本語9']
        unicode_node, _, _ = self.make_projection(unicode_text)
        unicode_document = self.put(unicode_node, unicode_text, unicode_identifiers)
        self.assertEqual(json.loads(self.sql(
            f'SELECT to_json(storage_v2_document_exact_identifiers({unicode_document}))')),
            unicode_identifiers)
        self.assertEqual(self.sql(f'SELECT storage_v2_document_has_exact_identifier('
            f'{unicode_document},{self.quote(unicode_identifiers[0])})'), 't')
        self.assert_sql_fails(self.admin('SELECT storage_v2_put_search_document('
            f"'posting-identifiers-fixture-v1','node',{node},{self.quote(text)},ARRAY['wrong_key'])"),
            'search-document profile collision')
        for update in (
            "search_text='changed'", "materialization_sha256=decode(repeat('ff',32),'hex')",
            "exact_identifiers=ARRAY['invented_key']", 'exact_identifiers_derived=FALSE',
        ):
            self.assert_sql_fails(self.admin(
                f'UPDATE storage_v2_search_document SET {update} WHERE id={fresh}'), 'immutable')
        self.assert_sql_fails(self.admin(f'UPDATE storage_v2_search_document '
            f'SET exact_identifiers_derived=TRUE WHERE id={custom_id}'), 'complete original set')
        self.assert_sql_fails(self.admin(f'DELETE FROM storage_v2_search_document WHERE id={fresh}'),
                              'immutable')
        new_run = self.begin(9, '73' * 32, '74' * 32)
        self.sql(self.admin(f'SELECT storage_v2_bind_search_document({view},0,{fresh},1.0)'))
        self.stage(new_run, 'fresh-identifiers.txt', text, node, view, digest)
        self.complete_analysis(digest)
        self.commit(new_run, 1)
        self.assertEqual(self.sql(f'SELECT count(*) FROM storage_v2_ingest_run_item WHERE run_id={new_run}'), '1')
        print('PASS: complete named/active envelopes, Unicode and unbounded identifiers, custom sets, '
              'exact reconstruction, reuse/collision, bounded authorized conversion and immutable identities',
              flush=True)
