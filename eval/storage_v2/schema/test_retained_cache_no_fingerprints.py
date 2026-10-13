"""Exact nonnull stripped caches use existing readers without new fingerprints.

Only the changed169 routes are gated here. Existing166/168 proofs and fixture
setup are reused; no production connection or performance claim is introduced.
"""
from __future__ import annotations

import hashlib
import json
import string

from eval.storage_v2.schema import test_retained_cache_preflight_cost as previous

ROOT = previous.ROOT
MIGRATION = ROOT / 'migrations/169_storage_v2_retained_cache_without_fingerprints.sql'
OLD = 'storage_v2_convert_document_caches(uuid,bytea,bigint[],bytea[],bigint,integer)'
STANDARD = 'storage_v2_convert_retained_document_caches(uuid,bytea,bigint[],bytea[],bigint,integer,bigint,bytea)'
NO_FINGERPRINTS = 'storage_v2_convert_retained_document_caches_without_fingerprint'


class RetainedCacheNoFingerprintsTests(previous.RetainedCachePreflightCostTests):
    operation = '00000000-0000-0000-0000-000000000169'
    manifest = 'a9' * 32
    # These passed base gates stay in their own classes. The169 aggregate only
    # includes its two changed methods rather than inherited writer suites.
    test_named_frozen_run_parity_cost_admission_replay_authority_writer_and_rollback = None
    test_early_ids_no_savings_and_mismatch_never_attempt_fts_update = None
    rank_readers = ('storage_v2_source_segment_ranks', 'storage_v2_source_segment_ranks_precise',
                    'storage_v2_source_segment_rank_candidates', 'storage_v2_source_segment_rank_candidates_scoped')
    queries = ('alpha', 'alpha beta', 'alpha OR missing', 'alpha -beta', 'alpha -missing',
               '"alpha beta"', 'alpha -"alpha beta"', 'alpha -"beta gamma"')

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls.old161_definition = cls.sql(f"SELECT pg_get_functiondef('{OLD}'::REGPROCEDURE)")
            cls.standard_authority = cls.sql("SELECT jsonb_build_object('owner',proowner,'acl',proacl::TEXT,"
                "'configuration',proconfig,'security_definer',prosecdef,'arguments',proargnames) "
                f"FROM pg_proc WHERE oid='{STANDARD}'::REGPROCEDURE")
            paths = list(MIGRATION.parent.glob('169_*.sql'))
            if len(paths) != 1:
                raise AssertionError('one actual169 migration required')
            cls.sql(f"SET app.user_id='{cls.schema.ADMIN_ID}';" + paths[0].read_text())
        except BaseException:
            cls.base.tearDownClass.__func__(cls)
            raise

    @staticmethod
    def position_rich_text(suffix=''):
        words = ['alpha', 'beta', 'gamma'] + ['plain' + c for c in string.ascii_lowercase[:21]]
        paragraphs = [' '.join(sorted(words, key=lambda word: hashlib.sha256(f'{n}:{word}'.encode()).digest()))
                      for n in range(240)]
        return 'alpha beta 日本語 key_169\n' + '\n'.join(paragraphs) + '\n' + suffix

    def call_without_fingerprints(self, rows, **kwargs):
        return super().call(rows, **kwargs).replace('storage_v2_convert_retained_document_caches(',
                                                   NO_FINGERPRINTS + '(', 1)

    def apply_without_fingerprints(self, rows, **kwargs):
        return json.loads(self.sql(self.operator(self.call_without_fingerprints(rows, **kwargs))))

    def generation(self, occurrence):
        return int(self.sql('SELECT r.generation_id FROM storage_v2_ingest_run_item item '
            f'JOIN storage_v2_ingest_run r ON r.id=item.run_id WHERE item.occurrence_id={occurrence} LIMIT 1'))

    def all_ranks(self, occurrence):
        return {(reader, query): self.rank(occurrence, query, reader=reader)
                for reader in self.rank_readers for query in self.queries}

    def wide_rank(self, occurrence, source, query):
        return self.sql(self.admin("SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY occurrence_id,score,segment_order),"
            "'[]'::JSONB) FROM storage_v2_source_segment_rank_candidates("
            f'array_fill({occurrence}::BIGINT,ARRAY[1024]),{self.quote(query)},ARRAY[{source}]::BIGINT[]) r'))

    def test_no_fingerprint_storage_all_readers_metadata_replay_and_insert_sentinel(self):
        row, occurrence = self.retained(self.position_rich_text())
        control, _ = self.retained(self.position_rich_text('standard_mode_control'))
        doc = row['document'];generation = self.generation(occurrence)
        self.sql(self.admin(f"SELECT storage_v2_verify_generation({generation},'{ 'a9' * 32}')"))
        self.sql(self.admin(f'SELECT * FROM storage_v2_materialize_reader_metadata({generation})'))
        metadata = self.sql(f'SELECT to_jsonb(h) FROM storage_v2_reader_metadata_header h WHERE generation_id={generation}')
        readiness = f'SELECT storage_v2_reader_metadata_ready(ARRAY[{generation}]::BIGINT[])'
        self.assertEqual(self.sql(self.admin(readiness)), 't')
        before = self.snapshots(row, occurrence);ranks = self.all_ranks(occurrence)
        invariant = self.invariant(doc, occurrence);original = self.payload(doc)
        wide = {q: self.wide_rank(occurrence,row['source'],q) for q in ('alpha','"alpha beta"')}
        for reader in self.rank_readers:
            self.assertTrue(json.loads(ranks[reader,'alpha']), reader)
        # The default control must actually enter its standard FTS branch, so
        # a passing sentinel proves mode restoration rather than an IDs skip.
        self.assertEqual(self.sql('SELECT 96::BIGINT*cardinality(storage_v2_lexical_block_fingerprints(ARRAY[d.fts_simple]))'
            '<coalesce(pg_column_size(d.fts_simple),0)::BIGINT+coalesce(pg_column_size(d.fts_simple_fingerprints),0)'
            f'+pg_column_size(d.exact_identifiers) FROM storage_v2_search_document d WHERE id={control["document"]}'),'t')
        self.assert_sql_fails(self.admin(self.call_without_fingerprints([row])), 'permission denied')
        self.assert_sql_fails("SET app.user_id='" + str(self.schema.OTHER_ID) + "';"+
                              self.call_without_fingerprints([row]), 'administrative operator')
        self.assert_sql_fails(self.operator(self.call_without_fingerprints([row],maximum=1)), 'body-byte admission exceeded')
        self.assert_sql_fails(self.operator(self.call_without_fingerprints([row],identity='00'*32)), 'materialization identity changed')
        sentinel = """
CREATE FUNCTION fixture169_reject_fingerprint_array() RETURNS TRIGGER LANGUAGE plpgsql AS $sentinel$
BEGIN
 IF NEW.fts_simple_derived AND NEW.fts_simple_fingerprints IS NOT NULL THEN
  RAISE EXCEPTION 'owned169 sentinel: new fingerprint array forbidden' USING ERRCODE='P1691';
 END IF;
 RETURN NEW;
END $sentinel$;
CREATE TRIGGER a_fixture169_no_fingerprints BEFORE UPDATE OF fts_simple,fts_simple_derived,fts_simple_fingerprints
 ON storage_v2_search_document FOR EACH ROW EXECUTE FUNCTION fixture169_reject_fingerprint_array();
UPDATE storage_v2_posting_conversion_catalog_contract
 SET identity_sha256=storage_v2_posting_conversion_relation_sha256(relation_oid::REGCLASS)
 WHERE relation_oid='storage_v2_search_document'::REGCLASS;
UPDATE storage_v2_lexical_conversion_catalog_contract
 SET identity_sha256=storage_v2_posting_conversion_relation_sha256(relation_oid::REGCLASS)
 WHERE relation_oid='storage_v2_search_document'::REGCLASS;
ALTER TABLE storage_v2_lexical_conversion_contract_seal DISABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
UPDATE storage_v2_lexical_conversion_contract_seal SET contract_sha256=sha256(convert_to(jsonb_build_object(
 'functions',(SELECT jsonb_agg(to_jsonb(c) ORDER BY signature) FROM storage_v2_lexical_conversion_function_contract c),
 'relations',(SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM storage_v2_lexical_conversion_catalog_contract c)
 )::TEXT,'UTF8')) WHERE singleton;
ALTER TABLE storage_v2_lexical_conversion_contract_seal ENABLE TRIGGER storage_v2_lexical_conversion_seal_immutable;
"""
        parameters = lambda entry: (f"'{self.operation}',decode('{self.manifest}','hex'),ARRAY[{entry['document']}]::BIGINT[],"
            f"ARRAY[decode('{entry['identity']}','hex')],8388608,10000,NULL::BIGINT,NULL::BYTEA")
        proof = f"""DO $sentinel_proof$
DECLARE converted RECORD; armed BOOLEAN:=FALSE; standard_blocked BOOLEAN:=FALSE;
BEGIN
 BEGIN
  UPDATE storage_v2_search_document SET fts_simple_derived=TRUE,fts_simple_fingerprints=ARRAY[17] WHERE id={doc};
 EXCEPTION WHEN SQLSTATE 'P1691' THEN armed:=TRUE;
 END;
 IF NOT armed THEN RAISE EXCEPTION 'owned169 fingerprint sentinel inactive';END IF;
 SELECT * INTO converted FROM {NO_FINGERPRINTS}({parameters(row)});
 IF converted.disposition<>'CONVERTED' OR converted.fingerprint_index_bytes<>0 THEN
  RAISE EXCEPTION 'exact no-fingerprint format did not convert with zero fingerprint charge';END IF;
 IF NOT EXISTS(SELECT 1 FROM storage_v2_search_document d WHERE d.id={doc}
   AND d.fts_simple IS NOT NULL AND d.fts_simple_derived AND d.fts_simple_fingerprints IS NULL
   AND d.fts_simple=strip(storage_v2_safe_tsvector(d.search_text))) THEN
  RAISE EXCEPTION 'nonnull exact stripped cache without fingerprints missing';END IF;
 -- Same transaction: the new wrapper must restore its mode before the normal
 -- eight-argument route, whose nonnull fingerprint array is caught here.
 BEGIN
  PERFORM * FROM storage_v2_convert_retained_document_caches({parameters(control)});
 EXCEPTION WHEN SQLSTATE 'P1691' THEN standard_blocked:=TRUE;
 END;
 IF NOT standard_blocked THEN RAISE EXCEPTION 'normal168 route inherited the no-fingerprint mode';END IF;
END $sentinel_proof$;
"""
        # Sentinel DDL and exactly amended fixture contracts are rollback-owned.
        # This proves the real new converter under the sentinel, then preserves
        # its original input for the ordinary commit/readback below.
        self.sql(self.operator(sentinel+proof).removesuffix('COMMIT;')+'ROLLBACK;')
        self.assertEqual(self.payload(doc),original)
        self.assertEqual(invariant,self.invariant(doc,occurrence))
        self.assertEqual(self.sql("SELECT count(*) FROM pg_trigger WHERE tgname='a_fixture169_no_fingerprints'"),'0')
        result = self.apply_without_fingerprints([row])[0]
        self.assertEqual(result['disposition'],'CONVERTED');self.assertEqual(result['fingerprint_index_bytes'],0)
        self.assertLess(result['stored_cache_bytes']+result['audit_bytes'],result['original_stored_cache_bytes'])
        self.assertEqual(self.sql(f'SELECT fts_simple IS NOT NULL AND fts_simple_derived AND fts_simple_fingerprints IS NULL '
            f'AND fts_simple=strip(storage_v2_safe_tsvector(search_text)) FROM storage_v2_search_document WHERE id={doc}'),'t')
        self.assertEqual(self.sql('SELECT fts_changed AND fingerprint_index_bytes=0 FROM '
            f'storage_v2_retained_document_cache_receipt WHERE document_id={doc}'),'t')
        self.assertEqual(before,self.snapshots(row,occurrence));self.assertEqual(ranks,self.all_ranks(occurrence))
        self.assertEqual(wide,{q:self.wide_rank(occurrence,row['source'],q) for q in wide})
        for reader in self.rank_readers:
            self.assertEqual(self.rank(occurrence,'alpha',actor=self.schema.OTHER_ID,reader=reader),'[]')
        self.assertEqual(invariant,self.invariant(doc,occurrence))
        self.assertEqual(metadata,self.sql(f'SELECT to_jsonb(h) FROM storage_v2_reader_metadata_header h WHERE generation_id={generation}'))
        self.assertEqual(self.sql(self.admin(readiness)),'t')
        self.sql(self.admin(f'SELECT * FROM storage_v2_materialize_reader_metadata({generation})'))
        self.assertEqual(self.sql(self.admin(readiness)),'t')
        # A positive phrase requires the logical full-vector path: stripped
        # positionless data alone cannot make this positive phrase match.
        self.assertEqual(self.sql(f'SELECT (fts_simple@@websearch_to_tsquery(\'simple\',\'"alpha beta"\')) '
            f'IS DISTINCT FROM (storage_v2_safe_tsvector(search_text)@@websearch_to_tsquery(\'simple\',\'"alpha beta"\')) '
            f'FROM storage_v2_search_document WHERE id={doc}'),'t')
        self.assertEqual(self.apply_without_fingerprints([row])[0]['disposition'],'REPLAY')
        self.assertEqual(self.payload(doc),result['cache_payload_sha256'][2:])
        self.assert_sql_fails(self.operator(self.call_without_fingerprints([row],operation='00000000-0000-0000-0000-000000000999')),
                              'exact manifest')
        self.assertEqual(self.old161_definition,self.sql(f"SELECT pg_get_functiondef('{OLD}'::REGPROCEDURE)"))

    def test_mismatch_rollback_and_unchanged_standard_161_and_168_modes(self):
        fresh, occurrence = self.retained(self.position_rich_text('rollback_unique'))
        doc=fresh['document'];payload=self.payload(doc);invariant=self.invariant(doc,occurrence)
        self.assert_sql_fails(self.operator(self.call_without_fingerprints([fresh])+';SELECT 1/0'),'division by zero')
        self.assertEqual(self.payload(doc),payload);self.assertEqual(self.invariant(doc,occurrence),invariant)
        for table in ('storage_v2_retained_document_cache_receipt','storage_v2_document_cache_conversion_receipt'):
            self.assertEqual(self.sql(f'SELECT count(*) FROM {table} WHERE document_id={doc}'),'0')
        tiny, tiny_occurrence = self.retained('alpha beta tiny_no_fingerprint_key')
        tiny_doc=tiny['document'];tiny_payload=self.payload(tiny_doc)
        tiny_invariant=self.invariant(tiny_doc,tiny_occurrence)
        refusal=self.apply_without_fingerprints([tiny])[0]
        self.assertEqual(refusal['disposition'],'RETAIN_NO_SAVINGS')
        self.assertEqual(refusal['audit_bytes'],0);self.assertEqual(refusal['fingerprint_index_bytes'],0)
        self.assertEqual(refusal['stored_cache_bytes'],refusal['original_stored_cache_bytes'])
        self.assertEqual(refusal['cache_payload_sha256'][2:],tiny_payload)
        self.assertEqual(self.payload(tiny_doc),tiny_payload)
        self.assertEqual(self.invariant(tiny_doc,tiny_occurrence),tiny_invariant)
        for table in ('storage_v2_retained_document_cache_receipt','storage_v2_document_cache_conversion_receipt'):
            self.assertEqual(self.sql(f'SELECT count(*) FROM {table} WHERE document_id={tiny_doc}'),'0')
        corrupt, _ = self.retained('alpha beta corrupt_no_fingerprint_key')
        bad=corrupt['document']
        self.sql('BEGIN;ALTER TABLE storage_v2_search_document DISABLE TRIGGER storage_v2_search_document_immutable;'
            f"UPDATE storage_v2_search_document SET fts_simple=to_tsvector('simple','invented wrong cache') WHERE id={bad};"
            'ALTER TABLE storage_v2_search_document ENABLE TRIGGER storage_v2_search_document_immutable;COMMIT;')
        old=self.payload(bad)
        refusal=self.apply_without_fingerprints([corrupt])[0]
        self.assertEqual(refusal['disposition'],'RETAIN_VECTOR_MISMATCH')
        self.assertEqual(self.payload(bad),old)
        self.assertEqual(self.sql(f'SELECT count(*) FROM storage_v2_retained_document_cache_receipt WHERE document_id={bad}'),'0')
        standard, _ = self.retained(self.position_rich_text('default_168_unique'))
        parameters = lambda entry, identity=None: (f"'{self.operation}',decode('{self.manifest}','hex'),"
            f"ARRAY[{entry['document']}]::BIGINT[],ARRAY[decode('{identity or entry['identity']}','hex')],"
            '8388608,10000,NULL::BIGINT,NULL::BYTEA')
        # Catch a failure after the wrapper arms its transaction-local marker.
        # The same transaction then executes the ordinary168 converter against
        # fresh input; a real fingerprint array proves the mode did not leak.
        marker='mainrag.retained_cache_no_fingerprint_operation'
        proof=f"""DO $mode_restore$
DECLARE before_mode TEXT; rejected BOOLEAN:=FALSE; converted RECORD;
BEGIN
 PERFORM set_config('{marker}','fixture_previous_mode',TRUE);
 before_mode:=current_setting('{marker}',TRUE);
 BEGIN
  PERFORM * FROM {NO_FINGERPRINTS}({parameters(fresh,'00'*32)});
 EXCEPTION WHEN raise_exception THEN
  IF SQLERRM<>'named document materialization identity changed' THEN RAISE;END IF;
  rejected:=TRUE;
 END;
 IF NOT rejected OR current_setting('{marker}',TRUE) IS DISTINCT FROM before_mode THEN
  RAISE EXCEPTION 'failed169 wrapper did not restore its prior transaction-local mode';END IF;
 SELECT * INTO converted FROM storage_v2_convert_retained_document_caches({parameters(standard)});
 IF converted.disposition<>'CONVERTED' OR converted.fingerprint_index_bytes<=0
   OR NOT EXISTS(SELECT 1 FROM storage_v2_search_document d WHERE d.id={standard['document']}
      AND d.fts_simple_derived AND d.fts_simple_fingerprints IS NOT NULL) THEN
  RAISE EXCEPTION 'normal168 route inherited failed169 no-fingerprint mode';END IF;
 IF current_setting('{marker}',TRUE) IS DISTINCT FROM before_mode THEN
  RAISE EXCEPTION 'standard168 route altered the restored marker';END IF;
END $mode_restore$;"""
        self.sql(self.operator(proof))
        self.assertEqual(self.payload(doc),payload);self.assertEqual(self.invariant(doc,occurrence),invariant)
        for table in ('storage_v2_retained_document_cache_receipt','storage_v2_document_cache_conversion_receipt'):
            self.assertEqual(self.sql(f'SELECT count(*) FROM {table} WHERE document_id={doc}'),'0')
        self.assertEqual(self.sql(f'SELECT fts_simple_derived AND fts_simple_fingerprints IS NOT NULL '
            f'FROM storage_v2_search_document WHERE id={standard["document"]}'),'t')
        self.assertEqual(self.sql('SELECT fts_changed FROM storage_v2_retained_document_cache_receipt '
            f'WHERE document_id={standard["document"]}'),'t')
        self.assertEqual(self.apply([standard])[0]['disposition'],'REPLAY')
        self.assert_sql_fails(self.operator(self.call_without_fingerprints([standard])),'exact manifest')
        converted=self.apply_without_fingerprints([fresh])[0]
        self.assertEqual(converted['disposition'],'CONVERTED')
        self.assertEqual(self.sql('SELECT fts_changed FROM storage_v2_retained_document_cache_receipt '
            f'WHERE document_id={doc}'),'t')
        self.assertEqual(self.apply_without_fingerprints([fresh])[0]['disposition'],'REPLAY')
        self.assert_sql_fails(self.operator(self.call([fresh])),'exact manifest')
        self.assertEqual(self.payload(doc),converted['cache_payload_sha256'][2:])
        original161, _ = self.retained(self.position_rich_text('default_161_unique'))
        legacy=self.call([original161]).replace('storage_v2_convert_retained_document_caches(',
                                               'storage_v2_convert_document_caches(',1)
        self.sql(self.operator(legacy))
        self.assertEqual(self.sql(f'SELECT fts_simple_derived AND fts_simple_fingerprints IS NOT NULL '
            f'FROM storage_v2_search_document WHERE id={original161["document"]}'),'t')
        self.sql(self.operator(legacy))
        self.assertEqual(self.old161_definition,self.sql(f"SELECT pg_get_functiondef('{OLD}'::REGPROCEDURE)"))
        authority=self.sql("SELECT jsonb_build_object('owner',proowner,'acl',proacl::TEXT,"
            "'configuration',proconfig,'security_definer',prosecdef,'arguments',proargnames) "
            f"FROM pg_proc WHERE oid='{STANDARD}'::REGPROCEDURE")
        self.assertEqual(authority,self.standard_authority)
