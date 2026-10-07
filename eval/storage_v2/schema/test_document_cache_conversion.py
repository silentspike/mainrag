"""Named document-cache representation parity in one disposable SQL fixture.

No timing or physical-saving acceptance claim is made. Never point this
harness at production.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
import unittest

from eval.storage_v2.schema import test_exact_lexeme_probes as previous

ROOT = previous.ExactLexemeProbeTests.schema.ROOT
MIGRATION = ROOT / 'migrations/161_storage_v2_document_cache_conversion.sql'
SIGNATURE = 'storage_v2_convert_document_caches(uuid,bytea,bigint[],bytea[],bigint,integer)'


class DocumentCacheConversionTests(unittest.TestCase):
    base = previous.ExactLexemeProbeTests
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
    operation = '00000000-0000-0000-0000-000000000161'
    manifest = 'c1' * 32

    @classmethod
    def setUpClass(cls):
        try:
            cls.base.setUpClass.__func__(cls)
            cls.file(previous.MIGRATION)
            # SQL161 touches document predicates, not lexical-context constructor
            # state. Include actual SQL160 when root integrates that migration.
            candidates = list((ROOT / 'migrations').glob('160_*.sql'))
            if candidates:
                if len(candidates) != 1:
                    raise AssertionError('one actual SQL160 migration required')
                cls.file(candidates[0])
            cls.sql(f"SET app.user_id='{cls.schema.ADMIN_ID}';" + MIGRATION.read_text())
        except BaseException:
            if hasattr(cls, 'database'):
                cls.base.tearDownClass.__func__(cls)
            elif hasattr(cls, 'stack'):
                cls.stack.close()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.base.tearDownClass.__func__(cls)

    def retained(self, text='alpha beta gamma alpha key_1 日本語9_unicode'):
        occurrence, artifact = self.fixture(text)
        row = json.loads(self.sql('SELECT json_build_object(\'document\',d.id,\'source\',o.source_id,'
            "'identity',encode(d.materialization_sha256,'hex')) "
            'FROM occurrence o JOIN storage_v2_search_view_document b ON b.view_id=o.view_id AND b.ordinal=0 '
            f'JOIN storage_v2_search_document d ON d.id=b.document_id WHERE o.id={occurrence}'))
        self.assertEqual(self.sql(f"SELECT NOT fts_simple_derived AND fts_simple=storage_v2_safe_tsvector(search_text) "
                                 f"FROM storage_v2_search_document WHERE id={row['document']}"), 't')
        # A real native segment supplies a nonempty weighted rank result.
        self.sql(self.admin(f'SELECT storage_v2_put_lexical_segments_located({occurrence},{artifact},'
            f'ARRAY[0]::BIGINT[],ARRAY[{self.quote(text)}],ARRAY[\'heading\'],ARRAY[\'text\'],'
            'ARRAY[1]::BIGINT[],ARRAY[1]::BIGINT[])'))
        return row, occurrence

    def call(self, rows, *, maximum=8388608, budget=10000, operation=None, identity=None):
        ids = ','.join(str(row['document']) for row in rows)
        identities = ','.join("decode('" + (identity or row['identity']) + "','hex')" for row in rows)
        return (f"SELECT coalesce(json_agg(r),'[]'::JSON) FROM storage_v2_convert_document_caches("
                f"'{operation or self.operation}',decode('{self.manifest}','hex'),ARRAY[{ids}]::BIGINT[],"
                f'ARRAY[{identities}]::BYTEA[],{maximum},{budget}) r')

    def operator(self, statement, *, timeout='10s'):
        # A prearmed statement timeout is part of the API contract; a setting
        # changed inside a running SQL function would not arm that statement.
        return ("BEGIN;SET LOCAL lock_timeout='1s';SET LOCAL statement_timeout=" + self.quote(timeout) + ';'
                f"SET LOCAL app.user_id='{self.schema.ADMIN_ID}';" + statement + ';COMMIT;')

    def apply(self, rows, **kwargs):
        return json.loads(self.sql(self.operator(self.call(rows, **kwargs))))

    def invariant(self, document):
        return self.sql("SELECT (to_jsonb(d)-ARRAY['fts_simple','fts_simple_derived','fts_simple_fingerprints'])::TEXT "
                        f'FROM storage_v2_search_document d WHERE id={document}')

    rank_readers = ('storage_v2_source_segment_ranks',
                    'storage_v2_source_segment_ranks_precise',
                    'storage_v2_source_segment_rank_candidates',
                    'storage_v2_source_segment_rank_candidates_scoped')

    def rank(self, occurrence, query, actor=None, *, reader='storage_v2_source_segment_ranks_precise'):
        arguments = f'ARRAY[{occurrence}]::BIGINT[],{self.quote(query)}'
        if reader == 'storage_v2_source_segment_rank_candidates_scoped':
            reader = 'storage_v2_source_segment_rank_candidates'
            arguments += f',ARRAY[(SELECT source_id FROM occurrence WHERE id={occurrence})]::BIGINT[]'
        return self.sql(self.actor(actor or self.schema.ADMIN_ID,
            "SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY occurrence_id,score,segment_order),'[]'::JSONB) "
            f'FROM {reader}({arguments}) r'))

    def envelope(self, source, ast, actor=None):
        return self.sql(self.actor(actor or self.schema.ADMIN_ID,
            f"SELECT storage_v2_search_exact({source},(SELECT max(generation_seq)::TEXT FROM source_generation "
            f"WHERE source_id={source}),{self.quote(json.dumps(ast))}::JSONB,'{{}}',10)::TEXT"))

    def test_original_to_stripped_identity_phrase_rank_lexeme_and_replay(self):
        row, occurrence = self.retained()
        doc = row['document']
        invariant = self.invariant(doc)
        epochs = self.sql("SELECT coalesce(jsonb_agg(to_jsonb(e) ORDER BY source_id),'[]'::JSONB) "
                          'FROM storage_v2_reader_metadata_epoch e')
        queries = ('alpha', 'alpha beta', 'alpha missing', 'alpha OR missing',
                   'alpha -beta', '"alpha beta"', 'alpha -"alpha beta"', 'alpha -"beta gamma"')
        before = {(reader, query): self.rank(occurrence, query, reader=reader)
                  for reader in self.rank_readers for query in queries}
        for reader in self.rank_readers:
            self.assertTrue(json.loads(before[reader, 'alpha']),
                            f'actual ranked fixture must emit a positive row for {reader}')
        asts = ({'type': 'term', 'value': 'alpha'}, {'type': 'phrase', 'value': 'alpha beta'},
                {'type': 'phrase', 'value': 'alpha gamma'})
        envelopes = [self.envelope(row['source'], ast) for ast in asts]
        self.assertGreater(json.loads(envelopes[0])['total'], 0)
        result = self.apply([row])
        self.assertEqual(result[0]['disposition'], 'CONVERTED')
        self.assertEqual(self.invariant(doc), invariant)
        self.assertEqual(epochs, self.sql("SELECT coalesce(jsonb_agg(to_jsonb(e) ORDER BY source_id),'[]'::JSONB) "
                                         'FROM storage_v2_reader_metadata_epoch e'))
        self.assertEqual(self.sql(f'SELECT fts_simple_derived AND fts_simple=strip(storage_v2_safe_tsvector(search_text)) '
            f'AND fts_simple_fingerprints=storage_v2_lexical_block_fingerprints(ARRAY[storage_v2_safe_tsvector(search_text)]) '
            f'FROM storage_v2_search_document WHERE id={doc}'), 't')
        for query in queries:
            with self.subTest(query=query):
                for reader in self.rank_readers:
                    with self.subTest(reader=reader):
                        self.assertEqual(before[reader, query], self.rank(occurrence, query, reader=reader))
                self.assertEqual(self.sql(f'SELECT storage_v2_logical_document_fts(fts_simple,fts_simple_derived,search_text) '
                    f'@@websearch_to_tsquery(\'simple\',{self.quote(query)}) IS NOT DISTINCT FROM '
                    f'(storage_v2_safe_tsvector(search_text)@@websearch_to_tsquery(\'simple\',{self.quote(query)})) '
                    f'FROM storage_v2_search_document WHERE id={doc}'), 't')
        self.assertEqual(envelopes, [self.envelope(row['source'], ast) for ast in asts])
        for reader in self.rank_readers:
            self.assertEqual(json.loads(self.rank(occurrence, 'alpha', self.schema.OTHER_ID, reader=reader)), [])
        self.assertEqual(self.sql(f'SELECT tsvector_to_array(fts_simple)=tsvector_to_array(storage_v2_safe_tsvector(search_text)) '
                                 f'FROM storage_v2_search_document WHERE id={doc}'), 't')
        # This positive phrase actually distinguishes stripped from full positions.
        self.assertEqual(self.sql(f'SELECT (fts_simple@@websearch_to_tsquery(\'simple\',\'"alpha beta"\')) '
            f'IS DISTINCT FROM (storage_v2_safe_tsvector(search_text)@@websearch_to_tsquery(\'simple\',\'"alpha beta"\')) '
            f'FROM storage_v2_search_document WHERE id={doc}'), 't')
        for signature in ('storage_v2_source_segment_ranks(bigint[],text)',
                          'storage_v2_source_segment_ranks_precise(bigint[],text)',
                          'storage_v2_source_segment_rank_candidates(bigint[],text)',
                          'storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])'):
            self.assertIn('fts_simple_derived IS FALSE OR public.storage_v2_plain_document_fingerprints(p_query) IS NOT NULL',
                          self.sql(f'SELECT pg_get_functiondef({self.quote(signature)}::REGPROCEDURE)'))
        self.assertEqual(self.apply([row])[0]['disposition'], 'REPLAY')
        self.assertEqual(self.sql(f'SELECT count(*) FROM storage_v2_document_cache_conversion_receipt WHERE document_id={doc}'), '1')
        self.assert_sql_fails(self.operator(self.call([row], operation='00000000-0000-0000-0000-000000000162')), 'replay receipt')
        self.assert_sql_fails(self.admin(f'UPDATE storage_v2_search_document SET search_text=search_text||\' changed\' WHERE id={doc}'), 'immutable')
        self.assert_sql_fails(self.admin(f"UPDATE storage_v2_search_document SET fts_simple=to_tsvector('simple','forged') WHERE id={doc}"), 'immutable')

    def test_mismatch_authority_identity_and_budgets_retain_original(self):
        row, _ = self.retained('budget alpha beta key_2')
        doc = row['document']; invariant = self.invariant(doc)
        self.assert_sql_fails(self.admin(self.call([row])), 'permission denied')
        # Even granting EXECUTE in this disposable fixture must not turn an
        # application administrator into the local maintenance operator.
        self.sql(f'GRANT EXECUTE ON FUNCTION {SIGNATURE} TO mainrag')
        try:
            self.assert_sql_fails(self.admin(self.call([row])), 'local administrative operator')
        finally:
            self.sql(f'REVOKE EXECUTE ON FUNCTION {SIGNATURE} FROM mainrag')
        self.assert_sql_fails(self.operator(self.call([row], maximum=1)), 'body-byte admission')
        self.assert_sql_fails(self.operator(self.call([row], identity='ff'*32)), 'identity changed')
        self.assert_sql_fails(self.operator(self.call([row, row])), 'exact named documents')
        self.assert_sql_fails(self.operator(self.call([row]), timeout='0'), 'prearmed')
        self.assert_sql_fails(self.admin(f"SET mainrag.document_cache_document='{doc}';UPDATE storage_v2_search_document "
            "SET fts_simple=strip(fts_simple),fts_simple_derived=TRUE,"
            "fts_simple_fingerprints=storage_v2_lexical_block_fingerprints(ARRAY[fts_simple]) "
            f"WHERE id={doc}"), 'immutable')
        self.assertEqual(self.invariant(doc), invariant)
        self.assertEqual(self.sql(f'SELECT count(*) FROM storage_v2_document_cache_conversion_receipt WHERE document_id={doc}'), '0')
        # Fixture-only corruption proves that complete old vector equality is
        # required, not merely lexeme equality/fingerprints. No production path
        # disables triggers or changes immutable source content.
        self.sql('ALTER TABLE storage_v2_search_document DISABLE TRIGGER storage_v2_search_document_immutable;'
                 f'UPDATE storage_v2_search_document SET fts_simple=strip(fts_simple) WHERE id={doc};'
                 'ALTER TABLE storage_v2_search_document ENABLE TRIGGER storage_v2_search_document_immutable;')
        corrupted = self.sql(f'SELECT fts_simple::TEXT FROM storage_v2_search_document WHERE id={doc}')
        self.assertEqual(self.apply([row])[0]['disposition'], 'RETAIN_VECTOR_MISMATCH')
        self.assertEqual(self.sql(f'SELECT fts_simple::TEXT FROM storage_v2_search_document WHERE id={doc}'), corrupted)
        self.assertEqual(self.invariant(doc), invariant)
        self.assertEqual(self.sql(f'SELECT count(*) FROM storage_v2_document_cache_conversion_receipt WHERE document_id={doc}'), '0')

    def test_writer_gate_and_existing_identifier_transition(self):
        row, _ = self.retained('identifiers alpha id_7 id_7 42')
        doc = int(self.sql(self.admin("SELECT id FROM storage_v2_put_search_document("
            "'document-cache-identifiers-v1','node',"
            f"(SELECT node_id FROM storage_v2_search_document WHERE id={row['document']}),"
            "'identifiers alpha id_7 id_7 42',ARRAY['42','id_7'])")))
        row = {'document':doc,'source':row['source'],'identity':self.sql(
            f"SELECT encode(materialization_sha256,'hex') FROM storage_v2_search_document WHERE id={doc}")}
        # Retain the actual old, redundant identifier representation in the
        # disposable fixture, then use the original SQL138 conversion surface.
        self.sql(self.admin('SELECT * FROM storage_v2_restore_document_identifiers('
                            f'{doc-1},1)'))
        identifiers = self.sql(f'SELECT to_json(storage_v2_document_exact_identifiers({doc}))')
        self.sql(self.admin(f'SELECT * FROM storage_v2_derive_document_identifiers({doc-1},1,TRUE)'))
        self.assertEqual(self.sql(f'SELECT to_json(storage_v2_document_exact_identifiers({doc}))'), identifiers)
        self.assertEqual(self.sql(f'SELECT exact_identifiers_derived AND cardinality(exact_identifiers)=0 '
                                 f'FROM storage_v2_search_document WHERE id={doc}'), 't')
        # A real, unfinished constructor run blocks maintenance; exact return
        # to the former fixture state is provided by rollback, not fake PASS.
        result = self.command('--command', self.operator(
            f"SELECT storage_v2_begin_shadow_ingest({row['source']},repeat('ab',32),"
            "repeat('cd',32),'document-cache-writer-fixture-v1','synthetic-snapshot','{}'::JSONB,FALSE);" + self.call([row])), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('writer-free controlled maintenance', result.stderr)
        self.assertEqual(self.apply([row])[0]['disposition'], 'CONVERTED')


    def test_audit_ledger_gc_roots_cleanup_inventory_and_posting_contract(self):
        # Use the actual operators' generated SQL and allowlists, not a copied
        # approximation of their root policy. All queries remain in this owned
        # disposable database; no apply/production entrypoint is invoked.
        def load_operator(label, filename):
            spec = importlib.util.spec_from_file_location(label, ROOT / 'ops/storage-v2' / filename)
            module = importlib.util.module_from_spec(spec)
            sys.modules[label] = module
            spec.loader.exec_module(module)
            return module

        gc = load_operator('document_cache_gc_fixture', 'native-gc.py')
        cleanup = load_operator('document_cache_cleanup_fixture', 'cleanup-manifest.py')
        ledger = 'storage_v2_document_cache_conversion_receipt'
        row, _ = self.retained('retained alpha beta')
        protected = row['document']

        def roots():
            return json.loads(self.sql('BEGIN;' + gc.graph_sql([]) +
                "SELECT coalesce(jsonb_agg(jsonb_build_array(kind,id) ORDER BY kind,id),'[]'::JSONB) FROM gc_mark;ROLLBACK;"))

        before_roots = roots()
        self.assertIn(['storage_v2_search_document', protected], before_roots)
        before_generations = self.sql('SELECT coalesce(jsonb_agg(to_jsonb(g) ORDER BY id),\'[]\'::JSONB) FROM source_generation g')
        before_pointers = self.sql('SELECT coalesce(jsonb_agg(jsonb_build_array(id,active_generation_id) ORDER BY id),\'[]\'::JSONB) FROM logical_source')
        self.assertEqual(self.apply([row])[0]['disposition'], 'CONVERTED')
        self.assertEqual(roots(), before_roots)
        self.assertEqual(before_generations, self.sql('SELECT coalesce(jsonb_agg(to_jsonb(g) ORDER BY id),\'[]\'::JSONB) FROM source_generation g'))
        self.assertEqual(before_pointers, self.sql('SELECT coalesce(jsonb_agg(jsonb_build_array(id,active_generation_id) ORDER BY id),\'[]\'::JSONB) FROM logical_source'))

        # An unbound alternate-profile document supplies a real unreachable
        # owner. Its source component remains protected by the original view;
        # the audit receipt must not invent a new document retention root.
        transient_doc = int(self.sql(self.admin('SELECT id FROM storage_v2_put_search_document('
            "'document-cache-unbound-profile','node',"
            f'(SELECT node_id FROM storage_v2_search_document WHERE id={protected}),'
            "'retained alpha beta',ARRAY[]::TEXT[])")))
        transient = {'document':transient_doc, 'identity':self.sql(
            f"SELECT encode(materialization_sha256,'hex') FROM storage_v2_search_document WHERE id={transient_doc}")}
        self.assertNotIn(['storage_v2_search_document', transient_doc], roots())
        roots_before_transient = roots()
        self.assertEqual(self.apply([transient])[0]['disposition'], 'CONVERTED')
        self.assertEqual(roots(), roots_before_transient)
        self.assertEqual(self.sql(f"SELECT count(*) FROM pg_constraint WHERE conrelid='{ledger}'::REGCLASS AND contype='f'"), '0')
        self.sql(self.operator('SELECT storage_v2_posting_conversion_require_operator()'))

        # The real exhaustive cleanup inventory captures the ledger's exact
        # count, PK/checks/immutable trigger and converter as retained objects.
        captured = json.loads(self.sql(cleanup.CAPTURE.catalog_statement(
            relation_names=(ledger,), retain_all=True, historical_hit_roots=True, outbox_present=True)))
        observed = next(item for item in captured['relations'] if item['name'] == ledger)
        self.assertEqual(captured['exact_rows'][ledger], int(self.sql(f'SELECT count(*) FROM {ledger}')))
        self.assertTrue(cleanup.retained_relation(ledger))
        with self.assertRaisesRegex(RuntimeError, 'retained native'):
            cleanup.validate_delete({'kind':'relation','disposition':'DELETE','observed':observed}, {})
        self.assertTrue(any(item['name']=='storage_v2_convert_document_caches' for item in captured['functions']))
        self.assertTrue(any(item['name']=='storage_v2_document_cache_receipt_immutable' for item in captured['triggers']))
        self.assertTrue(any(item['relation_oid']==observed['oid'] and item['kind']=='p'
                            for item in captured['constraints']))
        # GC enumerates all public schema/trigger/constraint/function metadata;
        # the audit relation stays outside its deletion targets/dependents.
        self.assertNotIn(ledger, gc.TARGETS)
        self.assertNotIn(ledger, gc.LINKS)

        # Demonstrate absence semantics after an actual fixture-only deletion
        # of the unrooted document. The FK leaf deletion order mirrors known GC
        # owners; no retained owner or ledger row is deleted. Rollback restores
        # the fixture completely, avoiding side effects on other test methods.
        delete_sql = ''
        for table, owners in gc.LINKS.items():
            if ('document_id','storage_v2_search_document') not in owners:
                continue
            delete_sql += f"""DO $fixture_leaf$ DECLARE hook TEXT; hooks TEXT[]; BEGIN
 IF to_regclass('public.{table}') IS NOT NULL THEN
  SELECT array_agg(t.tgname) INTO hooks FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid
   WHERE t.tgrelid='public.{table}'::REGCLASS AND p.proname='storage_v2_reject_retrieval_mutation' AND t.tgenabled='O';
  FOREACH hook IN ARRAY coalesce(hooks,ARRAY[]::TEXT[]) LOOP
    EXECUTE format('ALTER TABLE public.{table} DISABLE TRIGGER %I',hook);
  END LOOP;
  DELETE FROM public.{table} WHERE document_id={transient_doc};
  FOREACH hook IN ARRAY coalesce(hooks,ARRAY[]::TEXT[]) LOOP
    EXECUTE format('ALTER TABLE public.{table} ENABLE TRIGGER %I',hook);
  END LOOP;
 END IF;
END $fixture_leaf$;"""
        missing_call = self.call([transient])
        # Exception handling retains the deliberate deletion in this disposable
        # transaction long enough to prove that its historical receipt alone
        # cannot authorize a replay. Raising after our own sentinel is forbidden.
        self.sql('BEGIN;' + gc.graph_sql([]) +
            f"DO $root_guard$ BEGIN IF EXISTS(SELECT 1 FROM gc_mark WHERE kind='storage_v2_search_document' AND id={transient_doc}) THEN RAISE EXCEPTION 'fixture document is retained'; END IF;END $root_guard$;" +
            delete_sql + 'ALTER TABLE storage_v2_search_document DISABLE TRIGGER storage_v2_search_document_immutable;' +
            f'DELETE FROM storage_v2_search_document WHERE id={transient_doc};' +
            'ALTER TABLE storage_v2_search_document ENABLE TRIGGER storage_v2_search_document_immutable;' +
            "SET LOCAL lock_timeout='1s';SET LOCAL statement_timeout='10s';" +
            f"SET LOCAL app.user_id='{self.schema.ADMIN_ID}';" +
            f"""DO $missing$ DECLARE rejected BOOLEAN:=FALSE; BEGIN
 BEGIN PERFORM 1 FROM ({missing_call}) replay;
 EXCEPTION WHEN raise_exception THEN
  IF SQLERRM NOT LIKE '%named document absent%' THEN RAISE; END IF;
  rejected:=TRUE;
 END;
 IF NOT rejected THEN RAISE EXCEPTION 'missing document replay unexpectedly accepted'; END IF;
 IF NOT EXISTS(SELECT 1 FROM {ledger} WHERE document_id={transient_doc}) THEN
  RAISE EXCEPTION 'historical conversion audit was lost'; END IF;
END $missing$;ROLLBACK;""")
        self.assertEqual(self.apply([transient])[0]['disposition'], 'REPLAY')


if __name__ == '__main__':
    unittest.main()
