"""Broad ID planning bounds, full envelopes, ACLs and immutable native matches."""
import json
from unittest.mock import patch

from eval.storage_v2.schema import test_empty_and_shared_reader_scopes as previous

shared = previous.previous
bound = shared.previous.parent
MIGRATION = shared.ROOT / 'migrations/125_storage_v2_bounded_reader_planning.sql'
PREDECESSOR = shared.ROOT / 'migrations/124_storage_v2_empty_and_shared_reader_scopes.sql'


class BoundedReaderPlanningTests(previous.EmptyAndSharedReaderScopeTests):
    @classmethod
    def setUpClass(cls):
        shared.SharedQueryPostingTests.setUpClass.__func__(cls)
        try:
            cls.file(shared.METADATA)
            cls.file(shared.QUERY)
            cls.file(PREDECESSOR)
        except BaseException:
            cls.stack.close()
            raise

    def test_a_complete_active_and_exact_envelopes(self):
        def complete(instance):
            # Compare the actual preceding reader before applying this batch.
            with patch.object(bound, 'MIGRATION', MIGRATION):
                bound.BoundNativeRankTests.test_complete_results_authorization_ties_and_replay(instance)
        with patch.object(previous, 'MIGRATION', MIGRATION), patch.object(
                shared.SharedQueryPostingTests, 'test_a_complete_active_and_exact_envelopes', complete):
            previous.EmptyAndSharedReaderScopeTests.test_a_complete_active_and_exact_envelopes(self)
        # Native rows exist, but an exact query has no match. The requested
        # occurrence set must remain unexpanded, even for a broad caller scope.
        result = self.command('--command', "LOAD 'auto_explain'; SET auto_explain.log_min_duration=0; "
            "SET auto_explain.log_nested_statements=on; SET auto_explain.log_analyze=on; "
            "SET auto_explain.log_format=json; SET auto_explain.log_level=notice; "
            f"SET ROLE mainrag_v2_frontier_owner; SET app.user_id='{self.schema.ADMIN_ID}'; "
            "SELECT count(*) FROM storage_v2_authorized_lexical_candidates("
            "ARRAY(SELECT i::bigint FROM generate_series(1,130908) i),ARRAY[6]::bigint[],'never_present_native_word');")
        self.assertEqual(result.stdout.strip(), '0')
        plans = self.plans(result.stderr)
        ordinary = [p for p in plans if 'SELECT matching.* FROM matching' in p['Query Text']]
        self.assertEqual(len(ordinary), 1)
        requested = [n for n in self.nodes(ordinary[0]['Plan']) if n.get('CTE Name') == 'requested']
        self.assertTrue(requested)
        self.assertTrue(all(n['Actual Loops'] == 0 for n in requested))

    @staticmethod
    def nodes(node):
        yield node
        for child in node.get('Plans', []):
            yield from BoundedReaderPlanningTests.nodes(child)

    def test_sparse_dense_multi_term_duplicate_null_and_collision_scopes(self):
        with patch.object(previous, 'MIGRATION', MIGRATION), patch.object(shared, 'METADATA', MIGRATION):
            previous.EmptyAndSharedReaderScopeTests.test_sparse_dense_multi_term_duplicate_null_and_collision_scopes(self)
        scope = "ARRAY(SELECT i::bigint FROM generate_series(2,130908) i WHERE i%5<>0)"
        result = self.command('--command', "LOAD 'auto_explain'; SET auto_explain.log_min_duration=0; "
            "SET auto_explain.log_nested_statements=on; SET auto_explain.log_analyze=on; "
            "SET auto_explain.log_format=json; SET auto_explain.log_level=notice; "
            f"SELECT count(*) FROM query_fixture.scoped({scope},ARRAY['common']);")
        self.assertEqual(result.stdout.strip(), '39999')
        plans = self.plans(result.stderr)
        guards = [p for p in plans if 'NOT EXISTS (' in p['Query Text'] and 'unnest(p_document_ids)' in p['Query Text']]
        self.assertEqual(len(guards), 1)
        indexes = [n for n in self.nodes(guards[0]['Plan']) if n.get('Index Name') == 'compact_pkey']
        self.assertTrue(indexes)
        self.assertTrue(all('requested.id' in n.get('Index Cond', '') for n in indexes))
        self.assertFalse(any('matching_block AS MATERIALIZED' in p['Query Text'] for p in plans))
        print('130,908-item ordinary-only scope: keyed compact checks, no literal-array planning or global term scan', flush=True)

    def test_metadata_scope_does_not_expand_unrelated_vectors(self):
        with patch.object(shared, 'QUERY', MIGRATION), patch.object(shared, 'METADATA', MIGRATION):
            shared.SharedQueryPostingTests.test_metadata_scope_does_not_expand_unrelated_vectors(self)

    def test_function_and_index_drift_abort(self):
        self.file(MIGRATION)
        self.file(MIGRATION)
        body = MIGRATION.read_text().replace('BEGIN;', '', 1).rsplit('COMMIT;', 1)[0]
        for signature in ('storage_v2_scoped_query_posting(bigint[],text[])',
                          'storage_v2_source_segment_presence(bigint[])',
                          'storage_v2_authorized_lexical_candidates(bigint[],bigint[],text)'):
            definition = self.sql(f"SELECT pg_get_functiondef('{signature}'::regprocedure)")
            owner = self.sql(f"SELECT proowner::regrole FROM pg_proc WHERE oid='{signature}'::regprocedure")
            for change, error in (
                (definition.replace('RETURN QUERY', '/* drift */ RETURN QUERY', 1) + ';', 'definition'),
                (f'GRANT EXECUTE ON FUNCTION {signature} TO storage_v2_shadow_worker;', 'authority'),
                (f'REVOKE EXECUTE ON FUNCTION {signature} FROM {owner};', 'authority'),
                (f'ALTER FUNCTION {signature} SET row_security=off;', 'definition'),
                (f'ALTER FUNCTION {signature} OWNER TO storage_v2_shadow_worker;', 'definition'),
            ):
                result = self.command('--command', 'BEGIN;' + change + body + 'ROLLBACK;', check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('bounded reader ' + error + ' differs', result.stderr)
