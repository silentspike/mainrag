"""Full-result differential and execution-count guards for search aggregates."""
from __future__ import annotations

import json

from eval.storage_v2.schema import test_search_materialization as previous
from eval.storage_v2.schema import test_shadow_ingest_schema as schema

MIGRATION = schema.ROOT / 'migrations/052_storage_v2_materialized_search_aggregates.sql'
NAMES = ('term_match_aggregate', 'term_aggregate', 'phrase_aggregate', 'exact_aggregate')


def before_definition(definition):
    for name in NAMES:
        definition = definition.replace(f'\n    {name} AS MATERIALIZED (', f'\n    {name} AS (')
    definition = definition.replace(" SET plan_cache_mode TO 'force_custom_plan'\n", '')
    return definition


def search_statement(definition):
    statement = 'WITH RECURSIVE' + definition.split('    WITH RECURSIVE', 1)[1].split(' INTO v_result;', 1)[0]
    for old, new in (('v_generation.generation_seq', '$2'), ('p_source_id', '$1'),
                     ('p_ast', '$3'), ('p_filters', '$4'), ('p_limit', '$5')):
        statement = statement.replace(old, new)
    return statement


class MaterializedSearchAggregateTests(schema.ShadowIngestSchemaTests):
    make_search_fixture = previous.SearchMaterializationTests.make_search_fixture

    def definition(self):
        return self.sql(f"SELECT pg_get_functiondef('{previous.SIGNATURE}'::REGPROCEDURE)")

    def plan(self, statement, ast):
        literal = json.dumps(ast).replace("'", "''")
        return json.loads(self.sql(
            'SET plan_cache_mode=force_generic_plan; SET jit=off; '
            f'PREPARE fixture_search(BIGINT,BIGINT,JSONB,JSONB,BIGINT) AS {statement}; '
            'EXPLAIN (ANALYZE,BUFFERS,VERBOSE,FORMAT JSON) EXECUTE fixture_search('
            f"15,1,'{literal}','{{}}',3);"
        ))[0]

    def test_complete_results_are_identical_for_all_query_classes(self):
        self.make_search_fixture()
        queries = [
            {'type': 'term', 'value': 'alpha'},
            {'type': 'term', 'value': 'missing'},
            {'type': 'term', 'value': 'token_' + 'x' * 4096},
            {'type': 'phrase', 'value': 'alpha beta'},
            {'type': 'phrase', 'value': 'missing phrase'},
            {'type': 'exact', 'value': 'exact_key'},
            {'type': 'and', 'children': [
                {'type': 'term', 'value': 'alpha'}, {'type': 'term', 'value': 'alpha'}]},
            {'type': 'and', 'children': [
                {'type': 'term', 'value': 'alpha'},
                {'type': 'not', 'children': [{'type': 'term', 'value': 'forbidden'}]}]},
            {'type': 'or', 'children': [
                {'type': 'term', 'value': 'entry0'}, {'type': 'term', 'value': 'entry1'}]},
        ]
        filters = [{}, {'path_prefix': '/synthetic/late-000'}, {'role': 'heading'},
                   {'graph_profile': 'missing', 'semantic_profile': 'missing', 'rerank_profile': 'missing'},
                   {'occurred_from': '2100-01-01T00:00:00Z'}]
        final = self.definition()
        try:
            self.sql(before_definition(final))
            before = [self.exact_search(q, f, source_id=15) for q in queries for f in filters]
            self.file(MIGRATION)
            self.file(MIGRATION)
            after = [self.exact_search(q, f, source_id=15) for q in queries for f in filters]
            self.assertEqual(before, after)
            self.assertEqual(after[0]['fully_scored_views'], 24)
            self.assertEqual(after[0]['total'], 24)
            self.assertEqual(len(after[0]['results']), 10)
            self.assertGreater(len(after[0]['results'][0]['content']), 16000)
        finally:
            self.sql(final)

    def test_actual_generic_plan_computes_each_aggregate_at_most_once(self):
        self.make_search_fixture()
        for query in ({'type': 'term', 'value': 'alpha'},
                      {'type': 'phrase', 'value': 'alpha beta'},
                      {'type': 'exact', 'value': 'exact_key'}):
            plan = self.plan(search_statement(self.definition()), query)['Plan']
            nodes = list(previous.nodes(plan))
            for name in NAMES:
                aggregate = [node for node in nodes if node.get('Subplan Name') == 'CTE ' + name]
                self.assertEqual(len(aggregate), 1, name)
                self.assertLessEqual(aggregate[0]['Actual Loops'], 1, name)
            scope = [node for node in nodes if node.get('Subplan Name') == 'CTE scoped_binding']
            self.assertEqual(scope[0]['Actual Rows'], 24)
            content = [node for node in nodes if node['Node Type'] == 'Aggregate'
                       and any('string_agg' in value for value in node.get('Output', []))]
            self.assertEqual(len(content), 1)
            self.assertEqual(content[0]['Actual Loops'], 3)
            self.assertNotIn('idx_storage_v2_search_posting_term', [node.get('Index Name') for node in nodes])

    def test_replay_preserves_authority_and_partial_or_missing_anchors_fail_atomically(self):
        metadata = f"SELECT jsonb_build_object('oid',oid,'owner',proowner,'acl',proacl,'config',proconfig," \
                   f"'security_definer',prosecdef) FROM pg_proc WHERE oid='{previous.SIGNATURE}'::REGPROCEDURE"
        final = self.definition()
        authority = self.sql(metadata)
        self.file(MIGRATION)
        self.file(MIGRATION)
        self.assertEqual(self.sql(metadata), authority)
        self.assertEqual(self.definition(), final)
        baseline = before_definition(final)
        variants = [baseline.replace('term_aggregate AS (', 'term_aggregate AS MATERIALIZED ('),
                    baseline.replace('term_aggregate AS (', 'term_aggregate AS NOT MATERIALIZED (')]
        try:
            for definition in variants:
                self.sql(definition)
                before = self.definition()
                result = self.command('--file', str(MIGRATION), check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('storage-v2 search aggregate definition', result.stderr)
                self.assertEqual(self.definition(), before)
        finally:
            self.sql(final)

    def test_function_local_custom_planning_preserves_the_callers_configuration(self):
        self.make_search_fixture()
        final = self.definition()
        self.assertIn(" SET plan_cache_mode TO 'force_custom_plan'\n", final)
        result = self.sql(self.admin(
            "SET plan_cache_mode=force_generic_plan; "
            "SELECT storage_v2_search_exact(15,'1','{\"type\":\"term\",\"value\":\"alpha\"}','{}',10); "
            'SHOW plan_cache_mode;'))
        lines = result.splitlines()
        self.assertEqual(lines.pop(), 'force_generic_plan')
        self.assertEqual(json.loads('\n'.join(lines))['fully_scored_views'], 24)
        self.assertEqual(self.sql(self.admin("""
SET plan_cache_mode=force_generic_plan;
DO $error_boundary$
BEGIN
    BEGIN
        PERFORM storage_v2_search_exact(15,'invalid-selector',
            '{"type":"term","value":"alpha"}','{}',10);
        RAISE EXCEPTION 'expected invalid-selector rejection';
    EXCEPTION WHEN OTHERS THEN
        IF SQLERRM NOT LIKE 'generation selector must%' THEN RAISE; END IF;
    END;
    IF current_setting('plan_cache_mode') <> 'force_generic_plan' THEN
        RAISE EXCEPTION 'function planner policy leaked through an error';
    END IF;
END
$error_boundary$;
SHOW plan_cache_mode;
""")), 'force_generic_plan')
        try:
            self.sql(before_definition(final))
            before = json.loads(self.sql(f"SELECT to_jsonb(p) - 'prosrc' - 'proconfig' FROM pg_proc p "
                                         f"WHERE oid='{previous.SIGNATURE}'::REGPROCEDURE"))
            configuration = json.loads(self.sql(f"SELECT to_json(COALESCE(proconfig,ARRAY[]::TEXT[])) "
                                                f"FROM pg_proc WHERE oid='{previous.SIGNATURE}'::REGPROCEDURE"))
            self.file(MIGRATION)
            after = json.loads(self.sql(f"SELECT to_jsonb(p) - 'prosrc' - 'proconfig' FROM pg_proc p "
                                        f"WHERE oid='{previous.SIGNATURE}'::REGPROCEDURE"))
            self.assertEqual(after, before)
            expected = [*configuration, 'plan_cache_mode=force_custom_plan']
            self.assertEqual(json.loads(self.sql(f"SELECT to_json(proconfig) FROM pg_proc "
                                                 f"WHERE oid='{previous.SIGNATURE}'::REGPROCEDURE")), expected)
            self.sql(f'ALTER FUNCTION {previous.SIGNATURE} SET plan_cache_mode=force_generic_plan')
            drifted = self.definition()
            rejected = self.command('--file', str(MIGRATION), check=False)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn('plan configuration differs', rejected.stderr)
            self.assertEqual(self.definition(), drifted)
        finally:
            self.sql(final)
