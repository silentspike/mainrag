"""Exact persistent derived-mask proofs in one owned disposable native cohort.

A miss executes the original reconstruction; an immutable hit must not invoke it.
No fixture duration is a production throughput or full-source acceptance claim.
"""
from __future__ import annotations

import hashlib
import json

from eval.storage_v2.schema import test_linear_posting_run_classifier as previous
from eval.storage_v2.schema import test_retained_lexical_conversion as lexical

ROOT = previous.ROOT
MIGRATION = ROOT / 'migrations/177_storage_v2_cached_derived_mask_verification.sql'
SIGNATURE = 'storage_v2_verify_derived_lexical_masks(bigint)'
PREDECESSOR = 'b714012ee76962f4119b8cd8f3dd9e961da9642a50b427951fc2715600dedd9a'
TABLE = 'storage_v2_derived_mask_verification_proof'
REFERENCE = 'fixture177_uncached_masks'


class CachedDerivedMaskVerificationTests(previous.LinearPostingRunClassifierTests):
    test_native_run_predicate_and_one_realistic_byte_write = None
    array = lexical.RetainedLexicalConversionTests.array
    retained177 = lexical.RetainedLexicalConversionTests.retained
    operator = lexical.RetainedLexicalConversionTests.operator
    call = lexical.RetainedLexicalConversionTests.call
    apply = lexical.RetainedLexicalConversionTests.apply
    operation = '00000000-0000-0000-0000-000000000177'
    manifest = 'd7' * 32

    def verify177(self, row, *, original=False):
        name = REFERENCE if original else 'storage_v2_verify_derived_lexical_masks'
        tail = '' if original else ";SELECT current_setting('mainrag.derived_mask177_last_disposition')"
        return self.sql(self.admin(f"SELECT {name}({row['occurrence']})" + tail))

    def proof_count(self, row):
        return int(self.sql(f"SELECT count(*) FROM {TABLE} WHERE occurrence_id={row['occurrence']}"))

    def corrupt(self, row, assignment):
        return ('ALTER TABLE storage_v2_derived_lexical_block DISABLE TRIGGER USER;'
                f"UPDATE storage_v2_derived_lexical_block SET {assignment} WHERE occurrence_id={row['occurrence']};"
                'ALTER TABLE storage_v2_derived_lexical_block ENABLE TRIGGER USER;')

    def test_exact_miss_hit_drift_corruption_authority_and_progress(self):
        # Existing native construction, seal and lossless conversion provide the
        # oracle. Both owners are prepared before the new proof relation exists.
        rows = [self.retained177(segment_count=64) for _ in range(2)]
        for row in rows:
            self.assertEqual(self.apply(row)['disposition'], 'CONVERTED')
        original = self.sql(f"SELECT pg_get_functiondef('{SIGNATURE}'::REGPROCEDURE)")
        self.assertEqual(self.function_hashes()[SIGNATURE], PREDECESSOR)
        reference = original.replace('FUNCTION public.storage_v2_verify_derived_lexical_masks(',
                                     'FUNCTION public.' + REFERENCE + '(', 1)
        self.assertNotEqual(reference, original)
        self.sql(reference + f';ALTER FUNCTION {REFERENCE}(BIGINT) OWNER TO mainrag_v2_frontier_owner;'
                 f'REVOKE ALL ON FUNCTION {REFERENCE}(BIGINT) FROM PUBLIC;'
                 f'GRANT EXECUTE ON FUNCTION {REFERENCE}(BIGINT) TO mainrag;')
        before = self.function_hashes()
        authority = self.authority_catalog()
        contracts = json.loads(self.contracts())
        self.sql(f"SET app.user_id='{self.schema.ADMIN_ID}';" + MIGRATION.read_text())
        after = self.function_hashes()
        self.assertEqual(set(before), set(after))
        self.assertEqual({SIGNATURE}, {s for s in before if before[s] != after[s]})
        expected_authority = json.loads(json.dumps(authority))
        expected_authority[SIGNATURE]['provolatile'] = 'v'
        self.assertEqual(expected_authority, self.authority_catalog())
        after_contracts = json.loads(self.contracts())
        for key in ('posting_functions', 'posting_relations', 'lexical_relations'):
            self.assertEqual(contracts[key], after_contracts[key])
        self.assertEqual(contracts['lexical_seal']['function_count'], after_contracts['lexical_seal']['function_count'])
        self.assertEqual(contracts['lexical_seal']['catalog_count'], after_contracts['lexical_seal']['catalog_count'])
        for old, new in zip(contracts['lexical_functions'], after_contracts['lexical_functions']):
            expected = dict(old)
            if expected['signature'] == SIGNATURE:
                expected['definition_sha256'] = '\\x' + after[SIGNATURE]
            self.assertEqual(expected, new)

        row, pending = rows
        self.assertEqual(self.proof_count(row), 0)
        self.verify177(row, original=True)
        self.assertEqual(self.verify177(row), 'MISS_EXACTLY_VERIFIED_AND_PERSISTED')
        self.assertEqual(self.verify177(row), 'HIT_EXACT_IMMUTABLE_PROOF')
        self.assertEqual(self.proof_count(row), 1)
        self.assertEqual(self.proof_count(pending), 0)  # No full coverage before EOF.

        # A rollback-owned sentinel proves a hit skips the original vector/cache
        # work. The negative control invokes the unchanged old verifier and
        # MUST enter that work. No production verifier identity is altered.
        sentinel = '''CREATE OR REPLACE FUNCTION storage_v2_lexical_first_term_cache(p_vectors TSVECTOR[])
RETURNS TABLE(terms TEXT[],ordinals SMALLINT[]) LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE
SET search_path=pg_catalog,public AS $sentinel$
BEGIN RAISE EXCEPTION 'owned177 reconstruction sentinel' USING ERRCODE='P1771';END $sentinel$;'''
        observed_hit = f'''DO $observed_hit$
DECLARE before_calls BIGINT;original_calls BIGINT;hit_calls BIGINT;
BEGIN
 before_calls:=coalesce(pg_stat_get_xact_function_calls('storage_v2_lexical_first_term_cache(tsvector[])'::REGPROCEDURE),0);
 PERFORM {REFERENCE}({row['occurrence']});
 original_calls:=coalesce(pg_stat_get_xact_function_calls('storage_v2_lexical_first_term_cache(tsvector[])'::REGPROCEDURE),0);
 IF original_calls<=before_calls THEN RAISE EXCEPTION 'owned177 original reconstruction was not observed';END IF;
 PERFORM storage_v2_verify_derived_lexical_masks({row['occurrence']});
 hit_calls:=coalesce(pg_stat_get_xact_function_calls('storage_v2_lexical_first_term_cache(tsvector[])'::REGPROCEDURE),0);
 IF hit_calls<>original_calls OR current_setting('mainrag.derived_mask177_last_disposition')<>'HIT_EXACT_IMMUTABLE_PROOF' THEN
  RAISE EXCEPTION 'owned177 immutable hit reconstructed lexical vectors';END IF;
END $observed_hit$;'''
        self.sql("BEGIN;SET LOCAL track_functions='all';" + f"SET LOCAL app.user_id='{self.schema.ADMIN_ID}';" + observed_hit + 'ROLLBACK;')
        # Dependency drift invalidates the proof even with identical payload.
        # The private sentinel is rolled back with its exact original helper.
        self.assert_sql_fails('BEGIN;' + sentinel + self.admin(
            f"SELECT storage_v2_verify_derived_lexical_masks({row['occurrence']})"), 'owned177 reconstruction sentinel')
        self.assertEqual(self.verify177(row), 'HIT_EXACT_IMMUTABLE_PROOF')
        candidate = self.sql(f"SELECT pg_get_functiondef('{SIGNATURE}'::REGPROCEDURE)")
        reconstruction = original[original.index(' SELECT convert_to(document.search_text'):original.index(' END LOOP;')]
        self.assertIn(reconstruction, candidate)
        self.assertLess(candidate.index("'HIT_EXACT_IMMUTABLE_PROOF'"), candidate.index(' SELECT convert_to(document.search_text'))

        # Same-valued tuple update changes xmin/ctid: it is a new document
        # identity and must undergo the full reconstruction, never a hit.
        no_op = ('ALTER TABLE storage_v2_search_document DISABLE TRIGGER USER;'
                 f"UPDATE storage_v2_search_document SET search_text=search_text WHERE id={row['document']};"
                 'ALTER TABLE storage_v2_search_document ENABLE TRIGGER USER;')
        self.assertEqual(self.sql('BEGIN;' + no_op + self.admin(
            f"SELECT storage_v2_verify_derived_lexical_masks({row['occurrence']});"
            "SELECT current_setting('mainrag.derived_mask177_last_disposition');") + 'ROLLBACK;'),
            'MISS_EXACTLY_VERIFIED_AND_PERSISTED')
        body_drift = no_op.replace('search_text=search_text', "search_text=repeat('x',char_length(search_text))")
        self.assert_sql_fails('BEGIN;' + body_drift + self.admin(
            f"SELECT storage_v2_verify_derived_lexical_masks({row['occurrence']})"), 'differs from complete')
        self.assertEqual(self.proof_count(row), 1)

        for assignment, error in (("segment_masks=array_fill(B'0'::BIT(128),ARRAY[cardinality(segment_masks)])", 'segment mask differs'),
                                  ("first_terms=ARRAY['bogus'],first_term_ordinals=ARRAY[1]::SMALLINT[]", 'first term cache differs')):
            mutation = self.corrupt(row, assignment)
            for name in (REFERENCE, 'storage_v2_verify_derived_lexical_masks'):
                self.assert_sql_fails('BEGIN;' + mutation + self.admin(f"SELECT {name}({row['occurrence']})"), error)
        self.assertEqual(self.proof_count(row), 1)
        # Complete block manifest, including a newly appended valid block.
        added = ("INSERT INTO storage_v2_derived_lexical_block SELECT (jsonb_populate_record("
                 "NULL::storage_v2_derived_lexical_block,to_jsonb(b)||jsonb_build_object('block_order',1,'segment_orders',"
                 "(SELECT jsonb_agg(n+64 ORDER BY ordinal) FROM unnest(b.segment_orders) WITH ORDINALITY a(n,ordinal))))).* "
                 f"FROM storage_v2_derived_lexical_block b WHERE occurrence_id={row['occurrence']} AND block_order=0;")
        self.assertEqual(self.sql('BEGIN;' + added + self.admin(
            f"SELECT storage_v2_verify_derived_lexical_masks({row['occurrence']});"
            "SELECT current_setting('mainrag.derived_mask177_last_disposition');") + 'ROLLBACK;'),
            'MISS_EXACTLY_VERIFIED_AND_PERSISTED')
        self.assertEqual(self.proof_count(row), 1)

        for statement in (f'INSERT INTO {TABLE} SELECT * FROM {TABLE}',
                          f'UPDATE {TABLE} SET block_count=block_count', f'TRUNCATE {TABLE}'):
            self.assert_sql_fails(self.admin(statement), 'permission denied')
        self.assertEqual(self.sql("SELECT pg_has_role('mainrag','mainrag_v2_frontier_owner','MEMBER')"), 'f')
        self.assert_sql_fails(f'UPDATE {TABLE} SET block_count=block_count', 'immutable')
        self.sql(f"INSERT INTO fixture_source_access VALUES('{self.schema.OTHER_ID}',{row['source']},TRUE,FALSE)")
        self.assertEqual(self.sql(self.actor(self.schema.OTHER_ID,
            f"SELECT storage_v2_can_access_source({row['source']},'read')")), 't')
        self.assert_sql_fails(self.actor(self.schema.OTHER_ID,
            f"SELECT storage_v2_verify_derived_lexical_masks({row['occurrence']})"), 'source write authority')

        # An interrupted transaction cannot publish its proof, while the prior
        # committed owner remains reusable. The second owner then commits once.
        self.sql('BEGIN;' + self.admin(f"SELECT storage_v2_verify_derived_lexical_masks({pending['occurrence']});") + 'ROLLBACK;')
        self.assertEqual(self.proof_count(pending), 0)
        self.assertEqual(self.proof_count(row), 1)
        self.assertEqual(self.verify177(pending), 'MISS_EXACTLY_VERIFIED_AND_PERSISTED')
        self.assertTrue(all(self.proof_count(r) == 1 for r in rows))
        self.assertEqual(self.verify177(row), 'HIT_EXACT_IMMUTABLE_PROOF')
        self.sql(f"SET app.user_id='{self.schema.ADMIN_ID}';SELECT storage_v2_require_lexical_conversion_operator();")
        table = json.loads(self.sql(f"SELECT jsonb_build_object('owner',relowner::REGROLE::TEXT,'rls',relrowsecurity,"
            f"'force_rls',relforcerowsecurity,'acl',relacl::TEXT,'bytes',pg_total_relation_size(oid)) FROM pg_class WHERE oid='{TABLE}'::REGCLASS"))
        self.assertEqual(table['owner'], 'mainrag_v2_frontier_owner')
        self.assertTrue(table['rls'] and table['force_rls'])
        print('CACHED_DERIVED_MASK_VERIFICATION_NATIVE_PARITY ' + json.dumps(dict(
            cases=1, migration_sha256=hashlib.sha256(MIGRATION.read_bytes()).hexdigest(),
            predecessor_function_sha256=PREDECESSOR,resulting_function_sha256=after[SIGNATURE],
            authority_unchanged_except_known_volatility=True,reader_functions_unchanged=True,
            producer_semantics_unchanged=True,cached_and_uncached_results_exact=True,
            changed_identity_forces_reverification=True,bounded_commit_proofs_exact=True,
            cache_acl_protected=True,original_reconstruction_unchanged=True,
            public_function_signature_set_unchanged=True,proof_relation=table,
            performance_sla_claimed=False),sort_keys=True),flush=True)
