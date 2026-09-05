"""Named-generation binding completeness without false one-to-one assumptions."""
from __future__ import annotations

import json
import re
import uuid

from eval.storage_v2.schema import test_shadow_ingest_schema as schema


MIGRATION = schema.ROOT / 'migrations/050_storage_v2_view_binding_verification.sql'
PREVIOUS = schema.ROOT / 'migrations/035_storage_v2_shadow_slice.sql'
ATTRIBUTES = ("SELECT json_build_array(pg_get_userbyid(proowner),proacl,provolatile,"
              "proisstrict,prosecdef,proparallel,proconfig,procost) FROM pg_proc WHERE oid="
              "'storage_v2_shadow_source_state(bigint,text,boolean)'::regprocedure")


class ViewBindingVerificationTests(schema.ShadowIngestSchemaTests):
    def setUp(self):
        self.file(MIGRATION)

    def source(self) -> int:
        source = int(self.sql('SELECT max(id)+1 FROM sources'))
        self.sql(f"INSERT INTO sources(id,name,type,path) VALUES ({source},"
                 f"'synthetic-binding-{source}','fixture','synthetic-binding-{source}'); "
                 f"INSERT INTO fixture_source_access VALUES ('{schema.WRITER_ID}',{source},TRUE,TRUE)")
        return source

    def state(self, source: int, generation: int = 1, user: str = schema.ADMIN_ID):
        return json.loads(self.sql(self.actor(user,
            f"SELECT storage_v2_shadow_source_state({source},'{generation}',FALSE)")))

    def document(self, kind: str, identity: int, content: str) -> int:
        return int(self.sql(self.admin(
            f"SELECT id FROM storage_v2_put_search_document('binding-fixture-v1',"
            f"'{kind}',{identity},'{content}',ARRAY[]::TEXT[])")))

    def bind(self, view: int, ordinal: int, document: int):
        self.sql(self.admin(f'SELECT storage_v2_bind_search_document({view},{ordinal},{document},1.0)'))

    def shared_fixture(self):
        source = self.source()
        content = f'synthetic shared content {source}'
        node, first_view, digest = self.make_projection(content, 'text')
        second_node, second_view, _ = self.make_projection(content, 'rust')
        self.assertEqual(node, second_node)
        self.assertNotEqual(first_view, second_view)
        run = self.begin(source, uuid.uuid4().hex * 2, uuid.uuid4().hex * 2)
        for ordinal, view in enumerate((first_view, second_view, first_view)):
            self.stage(run, f'item-{ordinal}', content, node, view, digest)
        self.complete_analysis(digest)
        self.commit(run, 3)
        document = self.document('node', node, content)
        self.bind(first_view, 0, document)
        self.bind(second_view, 0, document)
        return source, first_view, second_view, document

    def composed_fixture(self):
        source = self.source()
        first_text = f'synthetic first {source}'
        second_text = f'synthetic second {source}'
        node, _, digest = self.make_projection(first_text)
        second_node, _, _ = self.make_projection(second_text)
        body = int(self.sql(f'SELECT body_id FROM content_node WHERE id={second_node}'))
        view = int(self.sql(self.admin(
            "SELECT id FROM storage_v2_put_retrieval_view('composed','binding-fixture-v1',"
            "'text','fixture-tokenizer-v1',0,ARRAY['content','context'],ARRAY['node','body'],"
            f'ARRAY[{node}::BIGINT,{body}::BIGINT],ARRAY[0::BIGINT,0::BIGINT],'
            f'ARRAY[{len(first_text)}::BIGINT,{len(second_text)}::BIGINT])')))
        run = self.begin(source, uuid.uuid4().hex * 2, uuid.uuid4().hex * 2)
        self.stage(run, 'composed', first_text, node, view, digest)
        self.complete_analysis(digest)
        self.commit(run, 1)
        first = self.document('node', node, first_text)
        second = self.document('body', body, second_text)
        self.bind(view, 0, first)
        self.bind(view, 1, second)
        return source, view, first, second

    def corrupt_state(self, source: int, mutation: str):
        # Deliberately corrupt only this disposable fixture, in a rolled-back
        # transaction. No production connection or persistent trigger changes.
        return json.loads(self.sql(
            f'BEGIN; SET LOCAL session_replication_role=replica; {mutation}; '
            'SET LOCAL session_replication_role=origin; ' + self.admin(
                f"SELECT storage_v2_shadow_source_state({source},'1',FALSE)") + '; ROLLBACK'))

    def test_shared_documents_keep_distinct_counts_and_zero_binding_errors(self):
        source, _, _, _ = self.shared_fixture()
        before = self.sql('SELECT md5(jsonb_agg(to_jsonb(g) ORDER BY id)::TEXT) FROM source_generation g')
        state = self.state(source, user=schema.WRITER_ID)
        self.assertEqual([state[key] for key in ('item_count', 'occurrence_count', 'view_count',
                                                'search_document_count')], [3, 3, 2, 1])
        for key in ('unbound_view_count', 'search_binding_error_count', 'analysis_incomplete_count'):
            self.assertEqual(state[key], 0, key)
        self.assertIsNone(state['active_generation_id'])
        self.assertEqual(before, self.sql(
            'SELECT md5(jsonb_agg(to_jsonb(g) ORDER BY id)::TEXT) FROM source_generation g'))

    def test_composed_body_and_node_bindings_detect_every_incomplete_mapping(self):
        source, view, first, second = self.composed_fixture()
        complete = self.state(source)
        self.assertEqual(complete['view_count'], 1)
        self.assertEqual(complete['search_document_count'], 2)
        self.assertEqual(complete['search_binding_error_count'], 0)
        self.assertEqual(complete['unbound_view_count'], 0)
        cases = [
            (f'DELETE FROM storage_v2_search_view_document WHERE view_id={view} AND ordinal=1', 0, 1),
            (f'DELETE FROM storage_v2_search_view_document WHERE view_id={view}', 1, 2),
            (f'UPDATE storage_v2_search_view_document SET document_id={second} '
             f'WHERE view_id={view} AND ordinal=0', 0, 1),
            (f'UPDATE storage_v2_search_view_document SET document_id={first} '
             f'WHERE view_id={view} AND ordinal=1', 0, 1),
            (f'INSERT INTO storage_v2_search_view_document(view_id,ordinal,document_id,role_weight) '
             f'VALUES ({view},5,{first},1.0)', 0, 1),
            (f'DELETE FROM view_component WHERE view_id={view} AND ordinal=1', 0, 1),
            (f'DELETE FROM view_component WHERE view_id={view}', 1, 2),
            (f'UPDATE storage_v2_search_view_document SET ordinal=5 '
             f'WHERE view_id={view} AND ordinal=1', 0, 2),
        ]
        for mutation, unbound, errors in cases:
            with self.subTest(mutation=mutation):
                state = self.corrupt_state(source, mutation)
                self.assertEqual(state['unbound_view_count'], unbound)
                self.assertEqual(state['search_binding_error_count'], errors)
                self.assertEqual(self.state(source), complete, 'corruption must roll back')

    def test_equal_cardinalities_do_not_hide_a_wrong_component_identity(self):
        source, first_view, _, document = self.shared_fixture()
        node, _, _ = self.make_projection('synthetic unrelated node')
        wrong_document = self.document('node', node, 'synthetic unrelated node')
        state = self.corrupt_state(source,
            f'UPDATE storage_v2_search_view_document SET document_id={wrong_document} '
            f'WHERE view_id={first_view}')
        self.assertEqual(state['view_count'], state['search_document_count'])
        self.assertEqual(state['unbound_view_count'], 0)
        self.assertEqual(state['search_binding_error_count'], 1,
                         'multiple occurrences of one view must not multiply binding errors')
        self.assertNotEqual(document, wrong_document)

    def test_binding_checks_remain_scoped_to_the_named_source_and_generation(self):
        source, _, _, _ = self.shared_fixture()
        other, _, _, _ = self.shared_fixture()
        first = self.state(source)
        node, view, digest = self.make_projection(f'synthetic next generation {source}')
        run = self.begin(source, uuid.uuid4().hex * 2, uuid.uuid4().hex * 2)
        self.stage(run, 'next', f'synthetic next generation {source}', node, view, digest)
        self.complete_analysis(digest)
        self.commit(run, 1)
        self.assertEqual(self.state(source), first)
        self.assertEqual(self.state(source, 2)['unbound_view_count'], 1)
        self.assertEqual(self.state(source, 2)['search_binding_error_count'], 1)
        self.assertEqual(self.state(other)['search_binding_error_count'], 0)
        result = self.command('--command', self.actor(schema.OTHER_ID,
            f"SELECT storage_v2_shadow_source_state({source},'1',FALSE)"), check=False)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout.strip(), '')

    def test_empty_generation_has_explicit_zero_completeness_counts(self):
        source = self.source()
        run = self.begin(source, uuid.uuid4().hex * 2, uuid.uuid4().hex * 2)
        self.commit(run, 0)
        state = self.state(source)
        for key in ('item_count', 'view_count', 'search_document_count',
                    'unbound_view_count', 'search_binding_error_count', 'analysis_incomplete_count'):
            self.assertEqual(state[key], 0, key)

    def test_generic_plan_never_rescans_binding_ctes_per_component(self):
        source = self.source()
        content = 'synthetic fanout content'
        node, _, digest = self.make_projection(content)
        document = self.document('node', node, content)
        run = self.begin(source, uuid.uuid4().hex * 2, uuid.uuid4().hex * 2)
        self.sql('CREATE TABLE fixture_binding_views(n INTEGER PRIMARY KEY,id BIGINT NOT NULL); '
                 'GRANT SELECT,INSERT ON fixture_binding_views TO storage_v2_shadow_worker')
        self.sql(self.admin(
            'INSERT INTO fixture_binding_views '
            "SELECT n,id FROM generate_series(1,512) n CROSS JOIN LATERAL "
            "storage_v2_put_retrieval_view('chunk','binding-plan-v1','language-' || n,"
            "'fixture-tokenizer-v1',0,ARRAY['content'],ARRAY['node'],"
            f'ARRAY[{node}::BIGINT],ARRAY[0::BIGINT],ARRAY[{len(content)}::BIGINT]) view_row; '
            f'SELECT count(storage_v2_bind_search_document(id,0,{document},1.0)) FROM fixture_binding_views; '
            'SELECT count((storage_v2_stage_shadow_item('
            f"{run},'fanout-' || n,'document','synthetic-item','{{}}'::JSONB,'fixture-adapter-v1',"
            f"{node},NULL,'{digest}',{len(content)},decode('{digest}','hex'),'fixture-analysis-v1',"
            "id,'/synthetic/fanout-' || n,'{}'::JSONB)).source_item_id) FROM fixture_binding_views"))
        self.complete_analysis(digest)
        self.commit(run, 512)
        generation, sequence = self.sql(
            'SELECT g.id || \':\' || g.generation_seq FROM source_generation g '
            f'JOIN storage_v2_ingest_run r ON r.generation_id=g.id WHERE r.id={run}').split(':')
        definition = self.sql("SELECT pg_get_functiondef("
                              "'storage_v2_shadow_source_state(bigint,text,boolean)'::regprocedure)")
        query = re.search(r'WITH visible_membership AS \(.*?\) INTO v_result;', definition, re.DOTALL)
        self.assertIsNotNone(query)
        query = query.group().replace(' INTO v_result', '')
        replacements = {'v_generation.generation_seq': '$2', 'v_generation.id': '$3',
                        'v_generation.item_count': '$4', 'v_generation.status': "'sealed'::TEXT",
                        'v_generation.verification_manifest_sha256': 'NULL::TEXT',
                        'v_active_generation_id': 'NULL::BIGINT', 'p_source_id': '$1'}
        for key, value in replacements.items():
            query = query.replace(key, value)
        plan = json.loads(self.sql(self.admin(
            'SET plan_cache_mode=force_generic_plan; '
            f'PREPARE fixture_binding_state(BIGINT,BIGINT,BIGINT,BIGINT) AS {query} '
            'EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) '
            f'EXECUTE fixture_binding_state({source},{sequence},{generation},512)')))[0]['Plan']

        def nodes(node):
            yield node
            for child in node.get('Plans', []):
                yield from nodes(child)

        # Guard the new completeness work, not the unchanged membership/run-item
        # reconciliation, whose join strategy depends on the fixture statistics.
        binding_ctes = {'visible_view', 'visible_component', 'visible_binding'}
        scans = [node for node in nodes(plan) if node['Node Type'] == 'CTE Scan'
                 and node['CTE Name'] in binding_ctes]
        self.assertEqual({scan['CTE Name'] for scan in scans}, binding_ctes)
        for scan in scans:
            self.assertLessEqual(scan['Actual Loops'], 1, scan['CTE Name'])
        for table in ('view_component', 'storage_v2_search_view_document', 'storage_v2_search_document'):
            probes = [node for node in nodes(plan) if node.get('Relation Name') == table
                      and node['Actual Loops'] > (0 if table == 'storage_v2_search_document' else 1)]
            self.assertTrue(probes, table)
            conditions = []
            for probe in probes:
                self.assertIn(probe['Node Type'], ('Index Scan', 'Index Only Scan', 'Bitmap Heap Scan'), table)
                condition = ' '.join(child.get('Index Cond', '') for child in nodes(probe))
                self.assertIn('id', condition, table)
                conditions.append(condition)
                self.assertLessEqual(probe['Actual Rows'], 1, table)
                self.assertEqual(probe.get('Rows Removed by Filter', 0), 0, table)
            if table != 'storage_v2_search_document':
                self.assertTrue(any('view_id' in condition and 'ordinal' in condition
                                    for condition in conditions), table)
        state = self.state(source)
        self.assertEqual([state[key] for key in ('view_count', 'search_document_count',
                                                'unbound_view_count', 'search_binding_error_count')],
                         [512, 1, 0, 0])

    def test_replay_preserves_prior_fields_ownership_and_security_attributes(self):
        source, _, _, _ = self.shared_fixture()
        previous = re.search(r'CREATE OR REPLACE FUNCTION storage_v2_shadow_source_state\(.*?END\n\$\$;',
                             PREVIOUS.read_text(), re.DOTALL)
        self.assertIsNotNone(previous)
        self.sql(previous.group())
        before = self.state(source)
        attributes = self.sql(ATTRIBUTES)
        for _ in range(2):
            self.file(MIGRATION)
            state = self.state(source)
            self.assertEqual({key: state[key] for key in before}, before)
            self.assertEqual(set(state) - set(before), {'unbound_view_count', 'search_binding_error_count'})
            self.assertEqual(self.sql(ATTRIBUTES), attributes)
