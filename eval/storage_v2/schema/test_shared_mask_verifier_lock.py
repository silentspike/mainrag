"""One native cohort for a shared proof lock and byte-exact177 proof reuse.

The canonical151 reconstruction is unchanged. Concurrent proof readers retain
an ingest-writer exclusion boundary; timings are not a throughput guarantee.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import time

from eval.storage_v2.schema import test_cached_derived_mask_verification as previous

ROOT = previous.ROOT
MIGRATION = ROOT / 'migrations/179_storage_v2_shared_mask_verifier_lock.sql'
PREDECESSOR = '5d6fd919df0d3fe0bc1e40e8252eb15a7b90a32f5dd43a817c0511449f7d0235'
SIGNATURE = previous.SIGNATURE
SEMANTIC = 'storage_v2_mask_verifier_semantic_identity'


class SharedMaskVerifierLockTests(previous.CachedDerivedMaskVerificationTests):
    test_exact_miss_hit_drift_corruption_authority_and_progress = None

    @staticmethod
    def marker(source, name):
        delimiter = '$' + name.replace('_query', '') + '$'
        start = source.index(name + ' TEXT:=' + delimiter) + len(name + ' TEXT:=' + delimiter)
        return source[start:source.index(delimiter + ';', start)].strip()

    def proof_bytes(self, row):
        return self.sql("SELECT coalesce(jsonb_agg(jsonb_build_array(encode(record_send(p),'hex'),to_jsonb(p)) "
            f"ORDER BY verifier_sha256),'[]'::JSONB) FROM {previous.TABLE} p WHERE occurrence_id={row['occurrence']}")

    def proof_result(self, row):
        return self.sql(f"SELECT to_jsonb(p)-'verified_at' FROM {previous.TABLE} p WHERE occurrence_id={row['occurrence']}")

    @staticmethod
    def lock_key(source):
        return f"hashtextextended('mainrag.storage-v2-ingest-source:{int(source)}',0)"

    def held(self, source, mode):
        key = self.lock_key(source)
        return int(self.sql("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND granted "
            f"AND classid::BIGINT=(({key}>>32)&4294967295) AND objid::BIGINT=({key}&4294967295) "
            f"AND objsubid=1 AND mode='{mode}'"))

    def wait_locks(self, source, mode, count, futures):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if self.held(source, mode) == count:
                return
            if any(f.done() for f in futures):
                for future in futures:
                    if future.done():
                        future.result()
                self.fail('owned verifier transactions ended before simultaneous lock observation')
            time.sleep(0.02)
        self.fail('bounded concurrent verifier lock observation timed out')

    def test_parallel_proof_lock_and_exact_historical_identity(self):
        old, pending = [self.retained177(segment_count=64) for _ in range(2)]
        for row in (old, pending):
            self.assertEqual(self.apply(row)['disposition'], 'CONVERTED')
        no_mask_occurrence, _ = self.fixture('public synthetic no derived mask owner')
        no_mask = {'occurrence': no_mask_occurrence}
        self.sql(f"SET app.user_id='{self.schema.ADMIN_ID}';" + previous.MIGRATION.read_text())
        self.assertEqual(self.function_hashes()[SIGNATURE], PREDECESSOR)
        original = self.sql(f"SELECT pg_get_functiondef('{SIGNATURE}'::REGPROCEDURE)")
        self.assertEqual(self.verify177(old), 'MISS_EXACTLY_VERIFIED_AND_PERSISTED')
        old_bytes = self.proof_bytes(old)
        # Compare a genuine old177 miss with the new miss, not a fabricated
        # expected proof. The rollback leaves the pending owner unproved.
        begun = time.monotonic()
        expected = self.sql('BEGIN;' + self.admin(
            f"SELECT storage_v2_verify_derived_lexical_masks({pending['occurrence']});") +
            f"RESET ROLE;SELECT to_jsonb(p)-'verified_at' FROM {previous.TABLE} p "
            f"WHERE occurrence_id={pending['occurrence']};ROLLBACK;")
        old_seconds = time.monotonic() - begun
        self.assertEqual(self.proof_count(pending), 0)
        before = self.function_hashes(); authority = self.authority_catalog(); contracts = json.loads(self.contracts())
        self.sql(f"SET app.user_id='{self.schema.ADMIN_ID}';" + MIGRATION.read_text())
        after = self.function_hashes()
        self.assertEqual(set(before), set(after))
        self.assertEqual({SIGNATURE}, {name for name in before if before[name] != after[name]})
        self.assertEqual(authority, self.authority_catalog())
        candidate = self.sql(f"SELECT pg_get_functiondef('{SIGNATURE}'::REGPROCEDURE)")
        reconstruction_start = original.index(' SELECT convert_to(document.search_text')
        reconstruction_end = original.index(' END LOOP;', reconstruction_start) + len(' END LOOP;')
        oracle = original[reconstruction_start:reconstruction_end]
        self.assertIn(oracle, candidate)  # Exact151 bytes, including full vectors.
        self.assertEqual(candidate.count(oracle), 1)
        self.assertIn('pg_try_advisory_xact_lock_shared(', candidate)
        self.assertNotIn('pg_try_advisory_xact_lock(', candidate)
        self.assertLess(candidate.index('NO_DERIVED_MASKS_REQUIRED'),
                        candidate.index('storage_v2_mask_verifier_semantic_identity'))
        self.assertEqual(old_bytes, self.proof_bytes(old))
        self.assertEqual(self.verify177(old), 'HIT_EXACT_IMMUTABLE_PROOF')
        self.assertEqual(old_bytes, self.proof_bytes(old))
        migration = MIGRATION.read_text()
        for name in ('identity_query', 'manifest_query'):
            self.assertEqual(self.marker(migration, name), self.marker(previous.MIGRATION.read_text(), name))
        semantic = json.loads(self.sql(f'SELECT to_jsonb(s) FROM {SEMANTIC} s'))
        self.assertEqual(semantic['semantic_definition_sha256'], '\\x' + PREDECESSOR)
        self.assertEqual(semantic['approved_implementation_sha256'], '\\x' + after[SIGNATURE])
        self.assertEqual(semantic['semantic_version_sha256'], json.loads(self.proof_result(old))['verifier_sha256'])
        self.assertEqual(self.sql(self.marker(migration, 'version_query')), semantic['semantic_version_sha256'])

        # An owned, rolled-back sentinel proves the actual old proof hit never
        # reaches the unchanged reconstruction. Its missing-proof control does.
        marker = ' SELECT convert_to(document.search_text'
        sentinel = candidate.replace(marker,
            " RAISE EXCEPTION 'owned179 reconstruction sentinel' USING ERRCODE='P1791';\n" + marker, 1)
        approve = (f'ALTER TABLE {SEMANTIC} DISABLE TRIGGER mask_semantic_identity_immutable;'
            f"UPDATE {SEMANTIC} SET approved_implementation_sha256=sha256(convert_to(pg_get_functiondef('{SIGNATURE}'::REGPROCEDURE),'UTF8'));"
            f'ALTER TABLE {SEMANTIC} ENABLE TRIGGER mask_semantic_identity_immutable;')
        self.assertEqual(self.sql('BEGIN;' + sentinel + ';' + approve + self.admin(
            f"SELECT storage_v2_verify_derived_lexical_masks({old['occurrence']});"
            "SELECT current_setting('mainrag.derived_mask177_last_disposition');") + 'ROLLBACK;'),
            'HIT_EXACT_IMMUTABLE_PROOF')
        self.assert_sql_fails('BEGIN;' + sentinel + ';' + approve + self.admin(
            f"SELECT storage_v2_verify_derived_lexical_masks({pending['occurrence']})"), 'owned179 reconstruction sentinel')
        drift = candidate.replace(marker, ' -- unapproved implementation\n' + marker, 1)
        self.assert_sql_fails('BEGIN;' + drift + ';' + self.admin(
            f"SELECT storage_v2_verify_derived_lexical_masks({old['occurrence']})"), 'unapproved mask verifier')
        # No-mask owners retain their original early return and do not hash a
        # semantic implementation that they do not invoke.
        self.assertEqual(self.sql('BEGIN;' + drift + ';' + self.admin(
            f"SELECT storage_v2_verify_derived_lexical_masks({no_mask_occurrence});"
            "SELECT current_setting('mainrag.derived_mask177_last_disposition');") + 'ROLLBACK;'),
            'NO_DERIVED_MASKS_REQUIRED')
        self.assertEqual(self.proof_count(no_mask), 0)

        begun = time.monotonic()
        self.assertEqual(self.verify177(pending), 'MISS_EXACTLY_VERIFIED_AND_PERSISTED')
        new_seconds = time.monotonic() - begun
        self.assertEqual(self.proof_result(pending), expected)
        self.assertEqual(self.proof_count(pending), 1)
        for assignment, message in (
            ("segment_masks=array_fill(B'0'::BIT(128),ARRAY[cardinality(segment_masks)])", 'segment mask differs'),
            ("first_terms=ARRAY['bogus'],first_term_ordinals=ARRAY[1]::SMALLINT[]", 'first term cache differs')):
            self.assert_sql_fails('BEGIN;' + self.corrupt(pending, assignment) + self.admin(
                f"SELECT storage_v2_verify_derived_lexical_masks({pending['occurrence']})"), message)
        self.assert_sql_fails(self.actor(self.schema.OTHER_ID,
            f"SELECT storage_v2_verify_derived_lexical_masks({old['occurrence']})"), 'source write authority')
        for action in (f'SELECT * FROM {SEMANTIC}', f'UPDATE {SEMANTIC} SET singleton=singleton', f'TRUNCATE {SEMANTIC}'):
            self.assert_sql_fails(self.admin(action), 'permission denied')
        self.assert_sql_fails(f'UPDATE {SEMANTIC} SET singleton=singleton', 'immutable')
        helper = self.sql("SELECT pg_get_functiondef('storage_v2_posting_fingerprint(text)'::REGPROCEDURE)")
        self.assert_sql_fails('BEGIN;' + helper.replace('SELECT ', 'SELECT /* unapproved helper */ ', 1) + ';' + self.admin(
            f"SELECT storage_v2_verify_derived_lexical_masks({old['occurrence']})"), 'unapproved mask verifier')

        # Four actual PostgreSQL sessions retain the same source's shared lock
        # simultaneously. No row or source-lock exemption is used by the fixture.
        key = self.lock_key(old['source'])
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(self.sql, "BEGIN;SET LOCAL statement_timeout='12s';" + self.admin(
                f"SELECT storage_v2_verify_derived_lexical_masks({old['occurrence']});"
                "SELECT current_setting('mainrag.derived_mask177_last_disposition');") +
                'RESET ROLE;SELECT pg_sleep(5);COMMIT;') for _ in range(4)]
            self.wait_locks(old['source'], 'ShareLock', 4, futures)
            self.assertEqual(self.sql(f'SELECT pg_try_advisory_xact_lock({key})'), 'f')
            for future in futures:
                self.assertEqual(future.result(), 'HIT_EXACT_IMMUTABLE_PROOF')
        self.assertEqual(self.sql(f'SELECT pg_try_advisory_xact_lock({key})'), 't')
        # Conversely, a genuine exclusive ingest-writer source lock excludes the
        # verifier. The original source authority and try-lock failure remain.
        with ThreadPoolExecutor(max_workers=1) as pool:
            writer = pool.submit(self.sql, "BEGIN;SET LOCAL statement_timeout='12s';"
                f'SELECT pg_advisory_xact_lock({key});SELECT pg_sleep(5);COMMIT;')
            self.wait_locks(old['source'], 'ExclusiveLock', 1, [writer])
            self.assert_sql_fails(self.admin(
                f"SELECT storage_v2_verify_derived_lexical_masks({old['occurrence']})"), 'source writer boundary is owned')
            writer.result()
        self.assertEqual(old_bytes, self.proof_bytes(old))
        final_contracts = json.loads(self.contracts())
        for name in ('posting_functions', 'posting_relations', 'lexical_relations'):
            self.assertEqual(contracts[name], final_contracts[name])
        self.assertEqual(contracts['lexical_seal']['function_count'], final_contracts['lexical_seal']['function_count'])
        self.assertEqual(contracts['lexical_seal']['catalog_count'], final_contracts['lexical_seal']['catalog_count'])
        self.assertEqual(after, self.function_hashes())
        self.assertEqual(authority, self.authority_catalog())
        self.sql(f"SET app.user_id='{self.schema.ADMIN_ID}';SELECT storage_v2_require_lexical_conversion_operator();")
        print('SHARED_MASK_VERIFIER_LOCK_NATIVE_PARITY ' + json.dumps(dict(
            migration_sha256=hashlib.sha256(MIGRATION.read_bytes()).hexdigest(),
            predecessor_function_sha256=PREDECESSOR, resulting_function_sha256=after[SIGNATURE],
            semantic_proof_keys_preserved=True, old_proof_payloads_unchanged=True,
            old_hit_skips_reconstruction=True, complete_window_lexemes_exact=True,
            masks_and_first_terms_exact=True, authority_unchanged=True,
            reader_functions_unchanged=True, producer_semantics_unchanged=True,
            public_function_signature_set_unchanged=True, original_oracle_byte_exact=True,
            four_shared_verifier_sessions=True, exclusive_ingest_writer_excluded=True,
            exclusive_writer_excludes_verifier=True, semantic_identity_acl_protected=True,
            unapproved_implementation_and_helper_rejected=True, no_mask_early_return_preserved=True,
            elapsed_seconds=dict(old177_miss=old_seconds, shared179_miss=new_seconds),
            performance_sla_claimed=False), sort_keys=True), flush=True)
