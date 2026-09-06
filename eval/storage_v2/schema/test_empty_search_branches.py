"""Complete-result and real-plan regressions for empty query-class guards."""
import json

from eval.storage_v2.schema import test_shadow_ingest_schema as schema
from eval.storage_v2.schema import test_materialized_search_aggregates as materialized
from eval.storage_v2.schema import test_search_materialization as search

MIGRATION = schema.ROOT / 'migrations/054_storage_v2_empty_search_branch_guards.sql'
GUARDS = (
    'cardinality((SELECT phrases FROM query_values)) > 0 AND ',
    'cardinality((SELECT exact_values FROM query_values)) > 0 AND ',
)


def before_definition(definition):
    for guard in GUARDS:
        definition = definition.replace(guard, '')
    return definition


class EmptySearchBranchTests(schema.ShadowIngestSchemaTests):
    make_search_fixture = search.SearchMaterializationTests.make_search_fixture
    definition = materialized.MaterializedSearchAggregateTests.definition

    def test_full_results_preserve_all_query_classes_filters_and_negative_branches(self):
        self.make_search_fixture()
        term = {'type': 'term', 'value': 'alpha'}
        phrase = {'type': 'phrase', 'value': 'alpha beta'}
        exact = {'type': 'exact', 'value': 'exact_key'}
        queries = [term, phrase, exact, {'type': 'term', 'value': 'missing'},
                   {'type': 'phrase', 'value': 'missing phrase'},
                   {'type': 'exact', 'value': 'missing_identifier'},
                   {'type': 'and', 'children': [term, phrase, exact]},
                   {'type': 'or', 'children': [phrase, exact]},
                   {'type': 'and', 'children': [term, {'type': 'not', 'children': [phrase]}]},
                   {'type': 'and', 'children': [term, {'type': 'not', 'children': [exact]}]},
                   {'type': 'term', 'value': 'token_' + 'x' * 4096}]
        filters = [{}, {'path_prefix': '/synthetic/late-000'}, {'role': 'heading'},
                   {'graph_profile': 'missing', 'semantic_profile': 'missing', 'rerank_profile': 'missing'},
                   {'occurred_from': '2100-01-01T00:00:00Z'}]
        final = self.definition()
        try:
            self.sql(before_definition(final))
            before = [self.exact_search(query, filters_value, source_id=15)
                      for query in queries for filters_value in filters]
            self.file(MIGRATION)
            after = [self.exact_search(query, filters_value, source_id=15)
                     for query in queries for filters_value in filters]
            self.assertEqual(before, after)
            for index in (0, 5, 10):
                self.assertEqual(after[index]['total'], 24)
                self.assertEqual(after[index]['fully_scored_views'], 24)
                self.assertEqual(len(after[index]['results']), 10)
            self.assertGreater(len(after[0]['results'][0]['content']), 16000)
        finally:
            self.sql(final)

    def test_empty_branches_never_scan_documents_under_custom_or_generic_plans(self):
        self.make_search_fixture()
        for mode in ('force_custom_plan', 'force_generic_plan'):
            for kind, value in (('term', 'alpha'), ('phrase', 'alpha beta'), ('exact', 'exact_key')):
                with self.subTest(mode=mode, query_class=kind):
                    literal = json.dumps({'type': kind, 'value': value})
                    statement = materialized.search_statement(self.definition())
                    plan = json.loads(self.sql(
                        f'SET plan_cache_mode={mode}; SET jit=off; '
                        f'PREPARE fixture(BIGINT,BIGINT,JSONB,JSONB,BIGINT) AS {statement}; '
                        'EXPLAIN (ANALYZE,BUFFERS,TIMING OFF,FORMAT JSON) '
                        f"EXECUTE fixture(15,1,'{literal}','{{}}',10);"))[0]['Plan']
                    for branch in ('phrase', 'exact'):
                        aggregate = [node for node in search.nodes(plan)
                                     if node.get('Subplan Name') == f'CTE {branch}_aggregate']
                        self.assertEqual(len(aggregate), 1)
                        document_scans = [node for node in search.nodes(aggregate[0])
                                          if node.get('Relation Name') == 'storage_v2_search_document']
                        self.assertTrue(document_scans)
                        if branch != kind:
                            self.assertTrue(all(node['Actual Loops'] == 0 for node in document_scans))
                        else:
                            self.assertTrue(any(node['Actual Loops'] > 0 for node in document_scans))

    def test_replay_preserves_complete_function_authority_and_configuration(self):
        final = self.definition()
        metadata_sql = ("SELECT to_jsonb(p) - 'prosrc' FROM pg_proc p "
                        f"WHERE oid='{search.SIGNATURE}'::REGPROCEDURE")
        try:
            self.sql(before_definition(final))
            authority = self.sql(metadata_sql)
            for _ in range(2):
                self.file(MIGRATION)
                self.assertEqual(self.definition(), final)
                self.assertEqual(self.sql(metadata_sql), authority)
        finally:
            self.sql(final)

    def test_partial_or_drifted_predicates_fail_without_modifying_the_function(self):
        final = self.definition()
        baseline = before_definition(final)
        variants = [final.replace(GUARDS[0], ''),
                    baseline.replace('WHERE exact.value = ANY(binding.exact_identifiers)',
                                     'WHERE (exact.value = ANY(binding.exact_identifiers))'),
                    baseline.replace('phrase_aggregate AS MATERIALIZED (', 'phrase_aggregate AS (')]
        try:
            for definition in variants:
                self.sql(definition)
                drifted = self.definition()
                result = self.command('--file', str(MIGRATION), check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('storage-v2 empty-branch', result.stderr)
                self.assertEqual(self.definition(), drifted)
        finally:
            self.sql(final)
