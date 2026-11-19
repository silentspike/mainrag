"""Exercise the copied rank optimization against the unchanged evidence oracle."""
import json

from eval.storage_v2.schema import test_copied_fragment_body_recall as fragment

MIGRATION = fragment.ROOT / "migrations/184_storage_v2_copied_literal_existence.sql"
BATCH_MIGRATION = fragment.ROOT / "migrations/187_storage_v2_batch_copied_generated_zero.sql"


class CopiedLiteralExistenceTests(fragment.CopiedFragmentBodyRecallTests):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls.sql(f"SET app.user_id='{cls.schema.ADMIN_ID}';" + MIGRATION.read_text())
        except BaseException:
            cls.base.tearDownClass.__func__(cls)
            raise

    def test_only_rank_bodies_change_and_word_boundaries_match_count(self):
        after = self.function_hashes()
        self.assertEqual(set(after), set(self.after183))
        self.assertEqual({name for name in after if after[name] != self.after183[name]},
                         set(fragment.RANKS))
        self.assertEqual(self.authority183, self.authority_catalog())
        cases = [
            ('word', 'WORD'), ('sword', 'word'), ('word_tail', 'word'),
            ('word\nWORD', 'word'), ('"word": value', 'word'), ('word-tail', 'word'),
            ('éword', 'word'), ('élan!', 'ÉLAN'), ('3141592', '314159'),
            ('a 314159.b', '314159'), ('_', '_'), ('', 'word'),
        ]
        rows = ','.join(f"({self.quote(body)},{self.quote(term)})" for body, term in cases)
        for signature in fragment.RANKS:
            definition = self.sql(f"SELECT pg_get_functiondef('{signature}'::REGPROCEDURE)")
            expression = next(line.split('THEN (', 1)[1]
                              for line in definition.splitlines()
                              if 'THEN (CASE WHEN lower(p_query)' in line)
            self.assertTrue(expression.endswith(' END'))
            expression = expression.replace('literal_document.search_text', 'body')
            self.assertEqual(self.sql(
                f"SELECT count(*) FROM (VALUES {rows}) input(body,p_query) "
                f"WHERE ({expression}) IS DISTINCT FROM "
                "(storage_v2_literal_term_count(body,p_query)>0)"), '0')

    def test_batched_generated_zero_rank_parity_and_atomic_rollback(self):
        generated = self.copied(json.dumps({'text': 'before\n314159: after'}))
        survivor = self.copied(chunk='314159 suffix')
        occurrences = f"ARRAY[{generated['occurrence']},{survivor['occurrence']}]::BIGINT[]"
        sources = f"ARRAY[{generated['source']},{survivor['source']}]::BIGINT[]"
        phrase = '"314159 suffix"'
        queries = ','.join(f'({self.quote(query)})' for query in (
            '314159', phrase, '314159 OR ordinary', 'nothingmatches',
        ))
        signatures = ','.join(self.quote(signature) for signature in fragment.RANKS)

        def replay(stage, actor):
            return self.actor(actor, f"""
                SELECT jsonb_build_object('stage', {self.quote(stage)}, 'result', jsonb_object_agg(
                    query, jsonb_build_object(
                        'scoped', (SELECT coalesce(jsonb_agg(to_jsonb(r)
                            ORDER BY occurrence_id,score,segment_order),'[]'::JSONB)
                            FROM storage_v2_source_segment_rank_candidates({occurrences},query,{sources}) r),
                        'unscoped', (SELECT coalesce(jsonb_agg(to_jsonb(r)
                            ORDER BY occurrence_id,score,segment_order),'[]'::JSONB)
                            FROM storage_v2_source_segment_rank_candidates({occurrences},query) r))))
                FROM (VALUES {queries}) input(query);
                RESET ROLE;
            """)

        def catalog(stage):
            return f"""
                SELECT jsonb_build_object('stage', {self.quote(stage)}, 'result', jsonb_build_object(
                    'definitions', (SELECT jsonb_object_agg(oid::REGPROCEDURE::TEXT,
                        encode(sha256(convert_to(pg_get_functiondef(oid),'UTF8')),'hex'))
                        FROM pg_proc WHERE pronamespace='public'::REGNAMESPACE AND prokind='f'),
                    'authorities', (SELECT jsonb_object_agg(oid::REGPROCEDURE::TEXT,to_jsonb(r)-'prosrc')
                        FROM pg_proc r WHERE oid::REGPROCEDURE::TEXT IN ({signatures})),
                    'posting', (SELECT jsonb_agg(to_jsonb(r) ORDER BY signature)
                        FROM storage_v2_posting_conversion_contract r),
                    'lexical', (SELECT jsonb_object_agg(signature, to_jsonb(r))
                        FROM storage_v2_lexical_conversion_function_contract r),
                    'seal', (SELECT to_jsonb(r)
                        FROM storage_v2_lexical_conversion_contract_seal r WHERE singleton)));
            """

        # Stage an ordinary positive-offset segment for the survivor using the
        # real writer. Only this disposable transaction removes its block-zero
        # fixture header. The changed representation and migration roll back.
        replace_marker = f"""
            ALTER TABLE storage_v2_compact_lexical_block DISABLE TRIGGER storage_v2_compact_lexical_immutable;
            DELETE FROM storage_v2_compact_lexical_block WHERE occurrence_id={survivor['occurrence']};
            ALTER TABLE storage_v2_compact_lexical_block ENABLE TRIGGER storage_v2_compact_lexical_immutable;
        """ + self.admin(
            'SELECT storage_v2_put_lexical_segments_located('
            f"{survivor['occurrence']},{survivor['artifact']},ARRAY[1]::BIGINT[],"
            "ARRAY['314159 suffix'],ARRAY[''],ARRAY['text'],ARRAY[9]::BIGINT[],ARRAY[9]::BIGINT[]);"
            'RESET ROLE;'
        )
        helper_witness = f"""
            SELECT jsonb_build_object('stage','predicate','result',jsonb_build_array(
                storage_v2_source_legacy_segment_matches({generated['occurrence']},'314159'),
                storage_v2_source_legacy_segment_matches({survivor['occurrence']},{self.quote(phrase)})));
        """
        migration = BATCH_MIGRATION.read_text().split('BEGIN;', 1)[1].rsplit('COMMIT;', 1)[0]
        before = replay('before', self.schema.ADMIN_ID) + replay('other_before', self.schema.OTHER_ID)
        after = replay('after', self.schema.ADMIN_ID) + replay('other_after', self.schema.OTHER_ID)
        result = self.sql(
            f"BEGIN; SET app.user_id='{self.schema.ADMIN_ID}';" + replace_marker
            + helper_witness + before + catalog('catalog_before') + 'SAVEPOINT before_batch;'
            + f"SET app.user_id='{self.schema.ADMIN_ID}';" + migration + after + catalog('catalog_after')
            + 'ROLLBACK TO SAVEPOINT before_batch;'
            + catalog('catalog_reverted') + replay('reverted', self.schema.ADMIN_ID) + 'ROLLBACK;'
        )
        records = (json.loads(line) for line in result.splitlines() if line.startswith('{'))
        observed = {record['stage']: record['result'] for record in records}
        self.assertEqual(observed['predicate'], [False, True])
        self.assertEqual(observed['before'], observed['after'])
        self.assertEqual(observed['before'], observed['reverted'])
        self.assertEqual(observed['other_before'], observed['other_after'])
        for scope in ('scoped', 'unscoped'):
            self.assertTrue(all(rows[scope] == [] for rows in observed['other_after'].values()))
            self.assertIn(generated['occurrence'], {
                row['occurrence_id'] for row in observed['after']['314159'][scope]
                if row['score'] >= 1000000
            })
            self.assertIn(survivor['occurrence'], {
                row['occurrence_id'] for row in observed['after'][phrase][scope]
                if row['score'] >= 1000000
            })
        catalog_before, catalog_after = observed['catalog_before'], observed['catalog_after']
        self.assertEqual(catalog_before, observed['catalog_reverted'])
        self.assertEqual(set(catalog_before['definitions']), set(catalog_after['definitions']))
        self.assertEqual({signature for signature, digest in catalog_before['definitions'].items()
                          if catalog_after['definitions'][signature] != digest}, set(fragment.RANKS))
        self.assertEqual(catalog_before['authorities'], catalog_after['authorities'])
        self.assertEqual(catalog_before['posting'], catalog_after['posting'])
        self.assertEqual(set(catalog_before['lexical']), set(catalog_after['lexical']))
        self.assertEqual({signature for signature, pin in catalog_before['lexical'].items()
                          if catalog_after['lexical'][signature] != pin}, set(fragment.RANKS))
        self.assertNotEqual(catalog_before['seal'], catalog_after['seal'])
