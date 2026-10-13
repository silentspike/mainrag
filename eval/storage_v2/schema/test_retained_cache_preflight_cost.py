"""Early cost decisions issue no rejected FTS UPDATE in the real PG fixture.

Both the existing166 method and this sentinel method share one disposable
cluster. The sentinel and its exactly updated fixture catalog seals roll back.
"""
from __future__ import annotations

import hashlib

from eval.storage_v2.schema import test_retained_document_cache_admission as previous

ROOT = previous.ROOT
MIGRATION = ROOT / 'migrations/168_storage_v2_retained_cache_preflight_cost.sql'


class RetainedCachePreflightCostTests(previous.RetainedDocumentCacheAdmissionTests):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls.sql(f"SET app.user_id='{cls.schema.ADMIN_ID}';" + MIGRATION.read_text())
        except BaseException:
            cls.base.tearDownClass.__func__(cls)
            raise

    def test_early_ids_no_savings_and_mismatch_never_attempt_fts_update(self):
        keys = ['key_' + hashlib.sha256(f'preflight-{n}'.encode()).hexdigest()[:12] for n in range(192)]
        row, occurrence = self.retained('alpha beta 日本語 ' + ' '.join(keys))
        generation = int(self.sql('SELECT r.generation_id FROM storage_v2_ingest_run_item item '
            f'JOIN storage_v2_ingest_run r ON r.id=item.run_id WHERE item.occurrence_id={occurrence} LIMIT 1'))
        self.sql(self.admin(f"SELECT storage_v2_verify_generation({generation},'{ 'c8' * 32}')"))
        tiny, _ = self.retained('alpha beta tiny_preflight_key')
        corrupt, _ = self.retained('alpha beta corrupt_preflight_key')
        self.sql('BEGIN;ALTER TABLE storage_v2_search_document DISABLE TRIGGER storage_v2_search_document_immutable;'
                 "UPDATE storage_v2_search_document SET fts_simple=to_tsvector('simple',"
                 "'wrong unrelated invented bogus foreign nonexistent fabricated false extra tokens') "
                 f'WHERE id={corrupt["document"]};'
                 'ALTER TABLE storage_v2_search_document ENABLE TRIGGER storage_v2_search_document_immutable;COMMIT;')
        # The early branch predicate is measured from these real stored rows,
        # not a test-only threshold or copy of the candidate converter.
        for entry in (row, tiny, corrupt):
            self.assertEqual(self.sql('SELECT 96::BIGINT*cardinality(storage_v2_lexical_block_fingerprints(ARRAY[d.fts_simple]))'
                '>=coalesce(pg_column_size(d.fts_simple),0)::BIGINT+'
                'coalesce(pg_column_size(d.fts_simple_fingerprints),0)+pg_column_size(d.exact_identifiers) '
                f'FROM storage_v2_search_document d WHERE d.id={entry["document"]}'), 't')
        self.assert_sql_fails(self.admin(self.call([row])), 'permission denied')
        self.assert_sql_fails("SET app.user_id='" + str(self.schema.OTHER_ID) + "';" + self.call([row]), 'administrative operator')
        self.assert_sql_fails(self.operator(self.call([row], maximum=1)), 'body-byte admission exceeded')
        before = self.snapshots(row, occurrence)
        original_identity = self.invariant(row['document'], occurrence)
        original_payload = self.payload(row['document'])
        tiny_payload, corrupt_payload = self.payload(tiny['document']), self.payload(corrupt['document'])
        parameters = lambda entry: (f"'{self.operation}',decode('{self.manifest}','hex'),ARRAY[{entry['document']}]::BIGINT[],"
            f"ARRAY[decode('{entry['identity']}','hex')],8388608,10000")
        q_array = 'ARRAY[\'alpha\',\'alpha beta\',\'alpha -beta\',\'alpha -missing\',\'"alpha beta"\']'
        sql = f"""
CREATE FUNCTION fixture_cache_preflight_reject_fts_update() RETURNS TRIGGER LANGUAGE plpgsql AS $sentinel$
BEGIN RAISE EXCEPTION 'owned sentinel: an excluded FTS UPDATE was attempted' USING ERRCODE='P1681';END $sentinel$;
CREATE TRIGGER a_fixture_cache_preflight_fts BEFORE UPDATE OF fts_simple,fts_simple_derived,fts_simple_fingerprints
 ON storage_v2_search_document FOR EACH ROW EXECUTE FUNCTION fixture_cache_preflight_reject_fts_update();
-- Only this rollback-owned fixture admits its additional rejection trigger.
-- Every original definition/guard remains checked; production contracts stay intact.
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
DO $proof$
DECLARE result RECORD; before_ids TEXT[]; before_vector TSVECTOR; before_fingerprints INTEGER[];
 before_derived BOOLEAN; armed BOOLEAN:=FALSE; query TEXT; before_ranks JSONB:='{{}}'::JSONB;
 ranks JSONB; cases INTEGER:=0;
BEGIN
 -- Negative control proves the exact UPDATE OF sentinel is active, even if
 -- the SET would leave the vector value unchanged.
 BEGIN
  UPDATE storage_v2_search_document SET fts_simple=fts_simple WHERE id={row['document']};
 EXCEPTION WHEN SQLSTATE 'P1681' THEN armed:=TRUE;
 END;
 IF NOT armed THEN RAISE EXCEPTION 'owned FTS rejection sentinel is inactive';END IF;
 SELECT storage_v2_document_exact_identifiers(d.id),d.fts_simple,d.fts_simple_fingerprints,d.fts_simple_derived
  INTO before_ids,before_vector,before_fingerprints,before_derived
  FROM storage_v2_search_document d WHERE d.id={row['document']};
 FOREACH query IN ARRAY {q_array} LOOP
  SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY occurrence_id,score,segment_order),'[]'::JSONB)
   INTO ranks FROM storage_v2_source_segment_ranks_precise(ARRAY[{occurrence}]::BIGINT[],query) r;
  before_ranks:=before_ranks||jsonb_build_object(query,ranks);
 END LOOP;
 SELECT * INTO result FROM storage_v2_convert_retained_document_caches({parameters(row)});
 IF result.disposition<>'CONVERTED' OR result.fingerprint_index_bytes<>0 THEN
  RAISE EXCEPTION 'early identifier-only conversion did not commit its modeled saving';END IF;
 IF NOT EXISTS(SELECT 1 FROM storage_v2_retained_document_cache_receipt r
    WHERE r.document_id={row['document']} AND r.identifiers_changed AND NOT r.fts_changed
     AND r.stored_cache_bytes+r.audit_bytes<r.original_stored_cache_bytes) THEN
  RAISE EXCEPTION 'identifier-only cost/payload ledger differs';END IF;
 IF EXISTS(SELECT 1 FROM storage_v2_document_cache_conversion_receipt WHERE document_id={row['document']}) THEN
  RAISE EXCEPTION 'early identifier path created a SQL161 FTS receipt';END IF;
 IF NOT EXISTS(SELECT 1 FROM storage_v2_search_document d WHERE d.id={row['document']}
     AND d.fts_simple IS NOT DISTINCT FROM before_vector
     AND d.fts_simple_fingerprints IS NOT DISTINCT FROM before_fingerprints
     AND d.fts_simple_derived IS NOT DISTINCT FROM before_derived
     AND d.exact_identifiers_derived AND cardinality(d.exact_identifiers)=0
     AND storage_v2_document_exact_identifiers(d.id) IS NOT DISTINCT FROM before_ids) THEN
  RAISE EXCEPTION 'early identifier conversion changed full FTS or logical identifiers';END IF;
 FOREACH query IN ARRAY {q_array} LOOP
  SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY occurrence_id,score,segment_order),'[]'::JSONB)
   INTO ranks FROM storage_v2_source_segment_ranks_precise(ARRAY[{occurrence}]::BIGINT[],query) r;
  IF ranks IS DISTINCT FROM before_ranks->query THEN RAISE EXCEPTION 'actual phrase/negation rank changed';END IF;
  cases:=cases+1;
 END LOOP;
 IF cases<>5 THEN RAISE EXCEPTION 'five actual rank cases required';END IF;
 SELECT * INTO result FROM storage_v2_convert_retained_document_caches({parameters(row)});
 IF result.disposition<>'REPLAY' THEN RAISE EXCEPTION 'early identifier exact replay failed';END IF;
 SELECT * INTO result FROM storage_v2_convert_retained_document_caches({parameters(tiny)});
 IF result.disposition<>'RETAIN_NO_SAVINGS' OR result.audit_bytes<>0
    OR result.cache_payload_sha256 IS DISTINCT FROM decode('{tiny_payload}','hex')
    OR EXISTS(SELECT 1 FROM storage_v2_retained_document_cache_receipt WHERE document_id={tiny['document']}) THEN
  RAISE EXCEPTION 'no-savings refusal changed the original cache';END IF;
 SELECT * INTO result FROM storage_v2_convert_retained_document_caches({parameters(corrupt)});
 IF result.disposition<>'RETAIN_VECTOR_MISMATCH' OR result.audit_bytes<>0
    OR result.cache_payload_sha256 IS DISTINCT FROM decode('{corrupt_payload}','hex')
    OR EXISTS(SELECT 1 FROM storage_v2_retained_document_cache_receipt WHERE document_id={corrupt['document']}) THEN
  RAISE EXCEPTION 'early canonical mismatch refusal changed the original cache';END IF;
END $proof$;
"""
        self.sql(self.operator(sql).removesuffix('COMMIT;') + 'ROLLBACK;')
        self.assertEqual(self.payload(row['document']), original_payload)
        self.assertEqual(self.payload(tiny['document']), tiny_payload)
        self.assertEqual(self.payload(corrupt['document']), corrupt_payload)
        self.assertEqual(self.invariant(row['document'], occurrence), original_identity)
        self.assertEqual(self.snapshots(row, occurrence), before)
        self.assertEqual(self.sql("SELECT count(*) FROM pg_trigger WHERE tgname='a_fixture_cache_preflight_fts'"), '0')
        self.assertEqual(self.sql("SELECT to_regprocedure('fixture_cache_preflight_reject_fts_update()') IS NULL"), 't')
