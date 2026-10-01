"""Active symbol IDs round-trip through graph, file and card inspection."""
import json

from eval.storage_v2.schema import test_bounded_intelligence_cards as base
from eval.storage_v2.schema import test_active_set_search as parent


class ActiveSymbolIdentityTests(base.BoundedIntelligenceCardsTests):
    def extra_intelligence_tests(self,digest):
        migration=parent.ROOT/'migrations/112_storage_v2_active_symbol_identity.sql'
        self.command(self.database,file=migration)
        roots=self.active(digest,dict(limit=200),command='symbols')
        one=roots['results'][0]['value']
        caller=next(r for r in one if r['name']=='fixture_1')
        callee=next(r for r in one if r['name']=='fixture_2')
        other=roots['results'][1]['value'][0]
        def graph(row,actor=parent.ADMIN,limit=200):
            return json.loads(self.sql(self.actor(actor,
                f"SELECT storage_v2_active_symbol_callgraph('{digest}',{row['id']},{limit})")))
        first=graph(caller)
        self.assertEqual(first['symbol'],caller)
        self.assertEqual(first['callees'],['fixture_2','unknown_fixture'])
        self.assertEqual(first['callers'],[])
        self.assertTrue(first['callers_complete'])
        self.assertTrue(first['callees_complete'])
        self.assertEqual([r['proven'] for r in first['call_evidence']],[True,False])
        self.assertEqual(first,graph(caller,actor=parent.READER))
        self.assertEqual(graph(callee)['callers'][0]['symbol_id'],caller['id'])
        self.assertFalse(graph(caller,limit=1)['callees_complete'])
        for row in (caller,callee):
            card=self.active(digest,dict(limit=1,occurrence_id=-row['id']),command='card')['results'][0]['value'][0]
            self.assertEqual(card['symbol_id'],row['id'])
            self.assertEqual(card['name'],row['name'])
            self.assertEqual(card['source_name'],'cards-one')
            self.assertEqual(card['line_start'],1)
            self.assertEqual(card['file_path'],row['file_path'])
        files=json.loads(self.sql(self.admin(
            f"SELECT storage_v2_active_file_symbols('{digest}',{caller['file_id']},200)")))
        self.assertEqual(len(files),137)
        self.assertEqual({r['id'] for r in files},{r['id'] for r in one})
        names=json.loads(self.sql(self.admin(
            f"SELECT storage_v2_active_callee_names('{digest}','fixture_1')")))
        self.assertEqual(names,['fixture_2','unknown_fixture'])
        self.assertEqual(self.sql(self.admin(f"SELECT storage_v2_active_callee_names('{digest}','fixture_1',NULL,1)")),'["fixture_2"]')
        self.assertEqual(self.sql(self.admin(f"SELECT storage_v2_active_callee_names('{digest}','fixture_10')")),'[]')
        self.assert_sql_fails(self.actor(parent.READER,
            f"SELECT storage_v2_active_symbol_callgraph('{digest}',{other['id']})"),'active symbol not found')
        self.assert_sql_fails(self.actor(parent.READER,
            f"SELECT storage_v2_active_file_symbols('{digest}',{other['file_id']})"),'active file not found')
        self.assert_sql_fails(self.actor(parent.READER,
            f"SELECT storage_v2_active_callee_names('{digest}','fixture_1',2)"),'source access denied')
        hidden=int(self.sql("SELECT visible.id FROM storage_v2_symbol_occurrence visible JOIN storage_v2_symbol stable ON stable.id=visible.symbol_id WHERE stable.symbol_key='000-hidden'"))
        self.assert_sql_fails(self.admin(
            f"SELECT storage_v2_active_symbol_callgraph('{digest}',{-hidden})"),'active symbol not found')
        for symbol_id in (0,1,-9223372036854775808):
            self.assert_sql_fails(self.admin(
                f"SELECT storage_v2_active_symbol_callgraph('{digest}',{symbol_id})"),'negative active symbol ID')
        definitions=self.sql("SELECT jsonb_agg(to_jsonb(p)-'prosrc' ORDER BY proname) FROM pg_proc p WHERE proname IN ('storage_v2_active_symbol_callgraph','storage_v2_active_file_symbols','storage_v2_active_callee_names')")
        self.command(self.database,file=migration)
        self.assertEqual(self.sql("SELECT jsonb_agg(to_jsonb(p)-'prosrc' ORDER BY proname) FROM pg_proc p WHERE proname IN ('storage_v2_active_symbol_callgraph','storage_v2_active_file_symbols','storage_v2_active_callee_names')"),definitions)
        self.assertEqual(graph(caller),first)
        self.assertEqual(self.sql("SELECT bool_and(NOT has_function_privilege('public',oid,'EXECUTE')) FROM pg_proc WHERE proname IN ('storage_v2_active_symbol_callgraph','storage_v2_active_file_symbols','storage_v2_active_callee_names')"),'t')
