"""One-snapshot Explore, exact overload roots, note namespaces and request caps."""
import json

from eval.storage_v2.schema import test_active_intelligence_notes as base
from eval.storage_v2.schema import test_active_set_search as parent


class ActiveExploreTests(base.ActiveIntelligenceNoteTests):
    def extra_intelligence_tests(self, digest):
        super().extra_intelligence_tests(digest)
        migration = parent.ROOT / 'migrations/116_storage_v2_active_explore.sql'
        self.command(self.database, file=migration)

        def literal(value):
            return "'" + json.dumps(value).replace("'", "''") + "'::jsonb"

        def explore(actor=parent.ADMIN, source=1, **changes):
            query = dict(concept='fixture', queries=['fixture_1'], operation_symbols=[], intent=None)
            query.update(changes)
            return json.loads(self.sql(self.actor(actor,
                f"SELECT storage_v2_active_explore('{digest}',{literal(query)},"
                f"{'NULL' if source is None else source})")))

        full = explore()
        self.assertEqual(full['results'], explore(parent.READER)['results'])
        self.assertLessEqual(full['candidate_count'], 4)
        self.assertLessEqual(full['card_rows_read'], 100)
        self.assertLessEqual(full['chain_work'], 100)
        self.assertFalse(full['cards_complete'])
        self.assertTrue(all(r['source_id'] == 1 for r in full['results']))
        self.assert_sql_fails(self.actor(parent.READER,
            f"SELECT storage_v2_active_explore('{digest}',"
            f"{literal(dict(concept='fixture',queries=['fixture'],operation_symbols=[]))},2)"),
            'access denied')
        self.assert_sql_fails(self.admin(f"SELECT storage_v2_active_explore('{digest}',"
            f"{literal(dict(concept='fixture',queries=['fixture'],operation_symbols=[]))},3)"),
            'explicit test scope')
        # Two visible occurrences now have the same display name; each selected
        # root must still traverse only its exact native occurrence identity.
        self.sql("UPDATE storage_v2_symbol_card card SET generic_card=card.generic_card||"
            "'{\"name\":\"fixture_1\"}' FROM storage_v2_symbol_occurrence visible "
            "JOIN storage_v2_symbol stable ON stable.id=visible.symbol_id "
            "WHERE visible.id=card.symbol_occurrence_id AND visible.source_id=1 "
            "AND stable.symbol_key='fixture-002'")
        overloaded = explore(queries=['fixture_1'])
        for result in overloaded['results']:
            roots = {p['root']['symbol']['id'] for p in result['value']['paths']}
            self.assertEqual(len(roots), 1)
            for path in result['value']['paths']:
                self.assertEqual(path['root']['symbol']['id'], path['root']['card']['symbol_id'])
        note = dict(read_provenance=dict(source_id=1,symbols_namespace='storage_v2_symbol_key'),
            symbols=['fixture-001'], path_description='fixture_1')
        card = dict(source_id=1,symbol_key='fixture-001',name='fixture_1')
        def matches(n, c):
            return self.sql(f'SELECT storage_v2_note_matches_card({literal(n)},{literal(c)})')
        self.assertEqual(matches(note,card), 't')
        self.assertEqual(matches(note,dict(card,source_id=2)), 'f')
        self.assertEqual(matches(dict(note,symbols=['fixture_1']),card), 'f')
        self.assertEqual(matches(dict(note,symbols={'fixture-001':True}),card), 'f')
        for changes in (dict(queries=[]),dict(queries=['fixture']*13),dict(queries=[None]),
                        dict(queries=['']),dict(operation_symbols=[1]),dict(intent=True)):
            q = dict(concept='fixture',queries=['fixture'],operation_symbols=[],intent=None)
            q.update(changes)
            self.assert_sql_fails(self.admin(
                f"SELECT storage_v2_active_explore('{digest}',{literal(q)},1)"), 'Explore')
        before = explore()
        self.command(self.database, file=migration)
        self.assertEqual(explore(), before)
        self.assertEqual(explore(queries=['missing'])['results'], [])
        self.assertEqual(self.sql("SELECT to_regclass('symbols') IS NULL AND "
            "to_regclass('negative_evidence') IS NULL"), 't')
