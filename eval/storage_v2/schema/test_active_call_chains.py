"""Multi-hop identity traversal, branches, cycles, caps and terminal evidence."""
import json
from eval.storage_v2.schema import test_active_ownership as base
from eval.storage_v2.schema import test_active_set_search as parent


class ActiveCallChainTests(base.ActiveOwnershipTests):
    def extra_intelligence_tests(self, digest):
        super().extra_intelligence_tests(digest)
        migration = parent.ROOT / 'migrations/114_storage_v2_bounded_call_chains.sql'
        self.command(self.database, file=migration)
        symbols = {}
        for n in range(1, 6):
            symbols[n] = tuple(map(int, self.sql(
                f"SELECT visible.id||':'||stable.id FROM storage_v2_symbol_occurrence visible "
                f"JOIN storage_v2_symbol stable ON stable.id=visible.symbol_id "
                f"WHERE visible.source_id=1 AND stable.symbol_key='fixture-{n:03}'").split(':')))
        for a, b in ((2, 3), (3, 1), (2, 4), (4, 5)):
            self.sql(self.admin(f"SELECT storage_v2_record_call({symbols[a][0]},{symbols[b][1]},"
                f"'fixture_{b}','call','{{\"resolution_kind\":\"parser_symbol_id\",\"line\":{a}}}')"))
        self.sql(self.admin(f"SELECT storage_v2_record_call({symbols[1][0]},NULL,'fixture_3','call',"
            "'{\"resolution_kind\":\"unresolved\",\"line\":8}')"))
        self.sql(self.admin(f"SELECT storage_v2_put_symbol_annotation(1,{symbols[2][1]},{symbols[2][0]},"
            "'thread','\"main\"','{\"evidence\":\"fixture annotation\"}','parser')"))
        def chain(**changes):
            query = dict(name='fixture_1', exact_name=True, max_depth=6, limit=100)
            query.update(changes)
            return self.active(digest, query, command='explain')
        full = chain()
        one = full['results'][0]['value']
        self.assertTrue(one['complete'])
        self.assertEqual(max(e['depth'] for e in one['entries']), 3)
        self.assertEqual({p['termination'] for p in one['paths']}, {'cycle', 'leaf', 'unresolved'})
        cycle = next(p for p in one['paths'] if p['termination'] == 'cycle')
        self.assertEqual([s['card']['name'] for s in cycle['steps']], ['fixture_2', 'fixture_3', 'fixture_1'])
        leaf = next(p for p in one['paths'] if p['termination'] == 'leaf')
        self.assertEqual([s['card']['name'] for s in leaf['steps']], ['fixture_2', 'fixture_4', 'fixture_5'])
        annotation = leaf['steps'][0]['annotations'][0]
        self.assertEqual(annotation['value'], 'main')
        self.assertIsNone(annotation['confidence'])
        self.assertEqual(annotation['metadata_scope'], 'occurrence')
        unresolved = next(p for p in one['paths'] if p.get('terminal_evidence', {}).get('callee_name') == 'fixture_3')
        self.assertFalse(unresolved['terminal_evidence']['proven'])
        self.assertEqual(unresolved['steps'], [])
        shallow = chain(max_depth=1)['results'][0]['value']
        self.assertEqual(max(e['depth'] for e in shallow['entries']), 1)
        self.assertFalse(shallow['complete'])
        self.assertIn('depth_limit', {p['termination'] for p in shallow['paths']})
        self.assertEqual(chain(), full)
        # A resolved stable identity can lack a visible occurrence. Its name is
        # evidence, but an unsealed artifact must never become another hop.
        hidden = tuple(map(int, self.sql("SELECT visible.id||':'||stable.id "
            "FROM storage_v2_symbol_occurrence visible JOIN storage_v2_symbol stable ON stable.id=visible.symbol_id "
            "WHERE stable.symbol_key='000-hidden'").split(':')))
        self.sql(self.admin(f"SELECT storage_v2_record_call({symbols[5][0]},{hidden[1]},'hidden','call',"
            "'{\"resolution_kind\":\"parser_symbol_id\",\"line\":9}')"))
        fenced = chain()['results'][0]['value']
        hidden_path = next(p for p in fenced['paths'] if p['termination'] == 'target_not_visible')
        self.assertEqual([s['card']['name'] for s in hidden_path['steps']], ['fixture_2', 'fixture_4', 'fixture_5'])
        self.assertFalse(fenced['complete'])
        self.assertFalse(any(s['symbol']['id'] == -hidden[0] for p in fenced['paths'] for s in p['steps']))
        self.sql(f'DELETE FROM storage_v2_call_edge WHERE caller_occurrence_id={symbols[5][0]}')
        self.assertEqual(chain(), full)
        reverse = chain(name='fixture_5', direction='callers')['results'][0]['value']
        self.assertEqual([e['from_name'] for e in reverse['entries']][:3], ['fixture_4', 'fixture_2', 'fixture_1'])
        self.assertIn('cycle', {p['termination'] for p in reverse['paths']})
        limited = chain(limit=3)
        values = [r['value'] for r in limited['results']]
        self.assertLessEqual(sum(len(v['entries']) for v in values), 3)
        self.assertLessEqual(sum(v['work_nodes'] for v in values), 3)
        self.assertFalse(all(v['complete'] for v in values))
        definition = self.sql("SELECT pg_get_functiondef('storage_v2_intelligence_chain(bigint,text,jsonb)'::regprocedure)")
        try:
            self.sql(definition.replace('BEGIN\n', "BEGIN\n    IF p_source<>1 THEN RAISE EXCEPTION 'exhausted chain budget read another source'; END IF;\n", 1))
            self.assertEqual(chain(limit=1)['results'][1]['value']['entries'], [])
        finally:
            self.sql(definition)
        scoped = self.active(digest, dict(name='fixture_1', exact_name=True),
            actor=parent.READER, command='explain')
        self.assertEqual(scoped['source_count'], 1)
        self.assertEqual(scoped['results'][0]['value'], one)
        for bad in ({'max_depth': 0}, {'max_depth': 11}, {'max_depth': '2'},
                    {'max_depth': 1.5}, {'direction': 'wrong'}, {'name': ''}, {'limit': 201}):
            q = dict(name='fixture_1', **bad) if 'name' not in bad else bad
            literal = json.dumps(q).replace("'", "''")
            with self.subTest(bad=bad):
                self.assert_sql_fails(self.admin(
                    f"SELECT storage_v2_intelligence_chain(1,'1','{literal}')"), 'call-chain')
        self.assert_sql_fails(self.actor(parent.READER,
            "SELECT storage_v2_intelligence_chain(2,'1','{\"name\":\"fixture_1\"}')"), 'authorized generation')
        before = self.sql("SELECT jsonb_agg(to_jsonb(p)-'prosrc' ORDER BY proname) FROM pg_proc p "
            "WHERE proname IN ('storage_v2_chain_node','storage_v2_intelligence_chain','storage_v2_active_intelligence_command')")
        self.command(self.database, file=migration)
        self.assertEqual(self.sql("SELECT jsonb_agg(to_jsonb(p)-'prosrc' ORDER BY proname) FROM pg_proc p "
            "WHERE proname IN ('storage_v2_chain_node','storage_v2_intelligence_chain','storage_v2_active_intelligence_command')"), before)
        self.assertEqual(chain(), full)
