"""Native note ACLs, lossless legacy retention, atomic drift rejection and limits."""
import json

from eval.storage_v2.schema import test_active_call_chains as base
from eval.storage_v2.schema import test_active_set_search as parent


class ActiveIntelligenceNoteTests(base.ActiveCallChainTests):
    def extra_intelligence_tests(self, digest):
        super().extra_intelligence_tests(digest)
        migration = parent.ROOT / 'migrations/115_storage_v2_intelligence_notes.sql'
        self.command(self.database, file=migration)

        def literal(value):
            return "'" + json.dumps(value).replace("'", "''") + "'::jsonb"

        def search(actor=parent.ADMIN, source=None, limit=20, manifest=digest):
            return json.loads(self.sql(self.actor(actor,
                f"SELECT storage_v2_search_intelligence_notes('{manifest}','fixture',"
                f"{'NULL' if source is None else source},{limit})")))

        def create(actor, source=None, **changes):
            record = dict(source_id=source, concept='fixture', path_description='fixture_1',
                reason='synthetic dead end', symbols=['fixture_1'], severity='warning',
                created_by=parent.READER)
            record.update(changes)
            return int(self.sql(self.actor(actor,
                f"SELECT storage_v2_create_intelligence_note('{digest}',{literal(record)})")))

        def import_statement(records, sha=None):
            value = literal(records)
            hash_value = f"'{sha}'" if sha is not None else \
                f"encode(digest(convert_to(({value})::text,'UTF8'),'sha256'),'hex')"
            return f'SELECT storage_v2_import_intelligence_notes({value},{hash_value})'

        admin_global = create(parent.ADMIN)
        reader_global = create(parent.READER, severity='custom', symbols=None)
        source_note = create(parent.ADMIN, 1)
        private_note = create(parent.ADMIN, 2)
        self.assertTrue(all(n < 0 and n % 2 == 1 for n in
            (admin_global, reader_global, source_note, private_note)))
        self.assertEqual({n['id'] for n in search(parent.READER)}, {reader_global, source_note})
        self.assertEqual({n['id'] for n in search(parent.READER, 1)}, {source_note})
        self.assertEqual({n['id'] for n in search()},
            {admin_global, reader_global, source_note, private_note})
        self.assertIsNone(next(n for n in search() if n['id'] == reader_global)['symbols'])
        for actor, source in [(parent.READER, 1), (parent.READER, 2)]:
            with self.subTest(source=source):
                self.assert_sql_fails(self.actor(actor,
                    f"SELECT storage_v2_create_intelligence_note('{digest}',"
                    f"{literal(dict(source_id=source,concept='fixture',path_description='',reason='',severity='warning'))})"),
                    'write access denied')
        self.assert_sql_fails(self.actor(parent.READER,
            f"SELECT storage_v2_search_intelligence_notes('{digest}','fixture',2)"), 'read access denied')
        self.assert_sql_fails(self.admin(
            f"SELECT storage_v2_create_intelligence_note('{digest}',"
            f"{literal(dict(source_id=3,concept='fixture',path_description='',reason='',severity='warning'))})"),
            'explicit test scope')
        self.assert_sql_fails(f"RESET app.user_id; SELECT storage_v2_create_intelligence_note('{digest}',"
            f"{literal(dict(concept='fixture',path_description='',reason='',severity='warning'))})", 'actor required')

        # A legacy label resembling an account is not proof of ownership. Keep
        # unowned global imports protected for admins while retaining every bit
        # of the original JSON record, including future fields and SQL/JSON null.
        originals = [dict(id=701+i, source_id=source, concept='fixture', path_description='',
            reason='original', symbols=None, severity='legacy-custom', created_by=parent.READER,
            domain_profile='fixture-domain', created_at='2020-01-01T00:00:00+00:00',
            future_field=dict(value="don't discard this")) for i, source in enumerate([None, 1, 2, 3])]
        receipt = json.loads(self.sql(self.admin(import_statement(originals))))
        self.assertEqual((receipt['imported'], receipt['existing']), (4, 0))
        repeated = json.loads(self.sql(self.admin(import_statement(originals))))
        self.assertEqual((repeated['imported'], repeated['existing']), (0, 4))
        self.assertEqual({n['id'] for n in search(parent.READER)}, {reader_global, source_note, 702})
        self.assertEqual({n['id'] for n in search()} -
            {admin_global, reader_global, source_note, private_note}, {701, 702, 703})
        exported = json.loads(self.sql(self.admin(
            'SELECT jsonb_agg(value) FROM storage_v2_export_intelligence_notes() value')))
        self.assertEqual([n['legacy_record'] for n in exported if n['legacy_id'] is not None], originals)
        self.assertTrue(all(n['owner_id'] is None for n in exported if n['legacy_id'] is not None))
        self.assert_sql_fails(self.actor(parent.READER,import_statement(originals)), 'administrator authority')
        self.assert_sql_fails(self.actor(parent.READER,
            'SELECT storage_v2_export_intelligence_notes()'), 'administrator authority')
        self.assert_sql_fails(self.admin(import_statement(originals, '0'*64)), 'matching canonical digest')
        before = self.sql('SELECT count(*) FROM storage_v2_intelligence_note')
        drift = [dict(originals[0], id=705), dict(originals[1], reason='changed')]
        self.assert_sql_fails(self.admin(import_statement(drift)), 'import drift')
        self.assertEqual(self.sql('SELECT count(*) FROM storage_v2_intelligence_note'), before)
        self.assert_sql_fails(self.admin(import_statement([originals[0], originals[0]])), 'duplicate')
        self.assert_sql_fails(self.admin(import_statement([dict(originals[0], id=706, source_id=999)])),
            'not mapped')

        # Migration 033 source evidence remains searchable in its separate,
        # even-negative identity namespace and never acquires name provenance.
        self.sql(self.admin("SELECT storage_v2_put_negative_evidence(1,'fixture-note-key','fixture',"
            "'source evidence','reason','[\"fixture-001\"]','warning','parser')"))
        native = next(n for n in search() if n['read_provenance']['identity_namespace'] == 'source_negative_evidence')
        self.assertEqual(native['id'] % 2, 0)
        self.assertEqual(native['read_provenance']['symbols_namespace'], 'storage_v2_symbol_key')
        for limit in (1, 2, 3):
            self.assertEqual(len(search(limit=limit)), limit)
        for limit in (0, 201):
            self.assert_sql_fails(self.admin(
                f"SELECT storage_v2_search_intelligence_notes('{digest}','fixture',NULL,{limit})"), 'limit')
        self.assert_sql_fails(self.admin(
            f"SELECT storage_v2_search_intelligence_notes('{'0'*64}','fixture')"), 'active')
        # Actual SQL RLS and mutation privileges, not just definer-function ACLs.
        rows = self.sql(self.actor(parent.READER,
            "SET ROLE mainrag; SELECT string_agg(COALESCE(legacy_id,-(2*id+1))::text,',' "
            "ORDER BY id) FROM storage_v2_intelligence_note; RESET ROLE"))
        self.assertEqual(set(map(int, rows.split(','))), {reader_global, source_note, 702})
        self.assert_sql_fails(self.actor(parent.READER,
            "SET ROLE mainrag; DELETE FROM storage_v2_intelligence_note"), 'permission denied')
        # Keep full export coverage beyond the interactive limit.
        more = [dict(originals[1], id=800+i) for i in range(35)]
        self.sql(self.admin(import_statement(more)))
        self.assertEqual(len(search()), 20)
        self.assertEqual(self.sql(self.admin('SELECT count(*) FROM storage_v2_export_intelligence_notes()')), '43')
        expected = search(limit=200)
        self.command(self.database, file=migration)
        self.assertEqual(search(limit=200), expected)
        # No legacy runtime table is required for these operations.
        self.assertEqual(self.sql("SELECT to_regclass('negative_evidence') IS NULL"), 't')
        self.assertEqual(len(search(parent.READER)), 20)
