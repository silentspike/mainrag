"""Exact first-match reduction, full search envelopes and installation fences."""
import json
import unittest

from eval.storage_v2.schema import test_bound_query_and_presence_work as previous

MIGRATION = previous.previous.ROOT / 'migrations/137_storage_v2_first_lexical_candidates.sql'
INDEXED = MIGRATION.parent / '139_storage_v2_index_ordinary_first_terms.sql'
SCOPED = MIGRATION.parent / '140_storage_v2_scoped_first_projection_reader.sql'
FULL = 'storage_v2_authorized_lexical_candidates(bigint[],bigint[],text)'
FIRST = 'storage_v2_authorized_lexical_first_candidates(bigint[],bigint[],text)'
RANKS = ('storage_v2_source_segment_rank_candidates(bigint[],text)',
         'storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])')


class FirstLexicalCandidateTests(unittest.TestCase):
    schema = previous.BoundQueryAndPresenceTests.schema
    command = classmethod(previous.BoundQueryAndPresenceTests.command.__func__)
    sql = classmethod(previous.BoundQueryAndPresenceTests.sql.__func__)
    file = classmethod(previous.BoundQueryAndPresenceTests.file.__func__)
    admin = classmethod(previous.BoundQueryAndPresenceTests.admin.__func__)
    actor = staticmethod(previous.BoundQueryAndPresenceTests.actor)
    quote = staticmethod(previous.BoundQueryAndPresenceTests.quote)
    make_projection = previous.BoundQueryAndPresenceTests.make_projection
    begin = previous.BoundQueryAndPresenceTests.begin
    stage = previous.BoundQueryAndPresenceTests.stage
    complete_analysis = previous.BoundQueryAndPresenceTests.complete_analysis
    commit = previous.BoundQueryAndPresenceTests.commit
    put = previous.BoundQueryAndPresenceTests.put
    assert_sql_fails = previous.BoundQueryAndPresenceTests.assert_sql_fails
    profile = previous.BoundQueryAndPresenceTests.profile

    @classmethod
    def setUpClass(cls):
        previous.BoundQueryAndPresenceTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        previous.BoundQueryAndPresenceTests.tearDownClass.__func__(cls)

    def test_complete_reduction_envelopes_and_installation_fences(self):
        # Reuse real named/active fixtures, optional score components, copied
        # ranks and authorization. The earlier migration is installed once.
        previous.BoundQueryAndPresenceTests.test_a_constraint_and_authority_drift(self)
        previous.BoundQueryAndPresenceTests.test_b_complete_named_active_and_authorization_envelopes(self)
        rows = json.loads(self.sql("""SELECT jsonb_agg(jsonb_build_object(
            'id',o.id,'artifact',o.artifact_version_id,'text',d.search_text) ORDER BY o.id)
            FROM occurrence o JOIN storage_v2_search_view_document b
              ON b.view_id=o.view_id AND b.ordinal=0
            JOIN storage_v2_search_document d ON d.id=b.document_id
            WHERE o.source_id IN (6,9) AND d.search_text IN
                ('alpha beta gamma','alpha beta 日本語')"""))
        for row in rows:
            # Sparse orders remain ordinary; aligned orders produce real
            # compact blocks without block zero. Each occurrence is mixed.
            orders = ([1001+2*n for n in range(96)] if row['text'].endswith('gamma')
                      else list(range(128,192)))
            count = len(orders)
            text = self.quote(row['text'])
            self.sql(self.actor(self.schema.ADMIN_ID,
                'SELECT storage_v2_put_lexical_segments_located('
                f"{row['id']},{row['artifact']},ARRAY{orders}::BIGINT[],"
                f"array_fill({text}::TEXT,ARRAY[{count}]),"
                f"array_fill(''::TEXT,ARRAY[{count}]),array_fill('text'::TEXT,ARRAY[{count}]),"
                f"array_fill(1::BIGINT,ARRAY[{count}]),array_fill(1::BIGINT,ARRAY[{count}]))"))
        self.assertGreater(int(self.sql('SELECT count(*) FROM storage_v2_lexical_segment')), 100)
        self.assertGreater(int(self.sql('SELECT count(*) FROM storage_v2_compact_lexical_block')), 0)

        original = {s: self.sql(f"SELECT pg_get_functiondef('{s}'::REGPROCEDURE)")
                    for s in (FULL,)+RANKS}
        for index, signature in enumerate(RANKS):
            name = f'fixture_complete_ranks_{index}'
            args = signature[signature.index('('):]
            definition = original[signature].replace(
                'public.storage_v2_source_segment_rank_candidates', 'public.'+name, 1)
            self.sql(definition+f'; ALTER FUNCTION {name}{args} OWNER TO mainrag_v2_frontier_owner; '
                     f'REVOKE ALL ON FUNCTION {name}{args} FROM PUBLIC; '
                     f'GRANT EXECUTE ON FUNCTION {name}{args} TO mainrag;')

        body = MIGRATION.read_text().replace('BEGIN;', '', 1).rsplit('COMMIT;', 1)[0]
        for signature in (FULL,)+RANKS:
            self.assert_sql_fails('BEGIN;'+original[signature].replace(
                'RETURN QUERY', '/* fixture drift */ RETURN QUERY', 1)+';'+body+'ROLLBACK;',
                'first lexical candidate definition differs')
            self.assert_sql_fails('BEGIN;'+f'GRANT EXECUTE ON FUNCTION {signature} TO storage_v2_shadow_worker;'
                +body+'ROLLBACK;', 'first lexical candidate authority differs')
        for change in (
            'ALTER TABLE storage_v2_compact_lexical_block ALTER COLUMN segment_orders DROP NOT NULL;',
            'ALTER TABLE storage_v2_compact_lexical_block DROP CONSTRAINT storage_v2_compact_lexical_block_pkey; '
            'ALTER TABLE storage_v2_compact_lexical_block ALTER COLUMN block_order DROP NOT NULL;',
            'ALTER TABLE storage_v2_compact_lexical_block DROP CONSTRAINT storage_v2_compact_lexical_block_check;',
            'ALTER TABLE storage_v2_compact_lexical_block DROP CONSTRAINT storage_v2_compact_lexical_block_check; '
            'ALTER TABLE storage_v2_compact_lexical_block ADD CHECK(storage_v2_lexical_block_orders_valid(block_order,segment_orders)) NOT VALID;',
            'ALTER TABLE storage_v2_compact_lexical_block DROP CONSTRAINT storage_v2_compact_lexical_bl_occurrence_id_source_id_arti_fkey;',
        ):
            self.assert_sql_fails('BEGIN;'+change+body+'ROLLBACK;',
                                  'first lexical candidate compact identity constraints differ')
        self.assert_sql_fails('BEGIN;'+original[FULL].replace(
            'public.storage_v2_authorized_lexical_candidates',
            'public.storage_v2_authorized_lexical_first_candidates', 1)+';'+body+'ROLLBACK;',
            'first lexical candidate helper already exists')
        self.assertEqual(self.sql(f"SELECT to_regprocedure('{FIRST}') IS NULL"), 't')

        term = lambda value: {'type': 'term', 'value': value}
        asts = [term('alpha'),term('common'),term('missing'),term('日本語'),
                {'type':'and','children':[term('alpha'),term('beta')]},
                {'type':'or','children':[term('alpha'),term('gamma')]},
                {'type':'and','children':[term('common'),{'type':'not','children':[term('beta')]}]},
                {'type':'phrase','value':'alpha beta'}, {'type':'exact','value':'key_identifier'}]

        def envelopes():
            statements = []
            for ast in asts:
                for filters in ({}, {s+'_profile':'request-fixture' for s in ('graph','semantic','rerank')}):
                    for limit in (1, 3, 10):
                        a, f = self.quote(json.dumps(ast)), self.quote(json.dumps(filters))
                        statements.extend([
                            f"SELECT storage_v2_search_exact(6,'1',{a}::JSONB,{f}::JSONB,{limit});",
                            f"SELECT storage_v2_search_active_unchecked('{'a'*64}',{a}::JSONB,{f}::JSONB,{limit},6,FALSE);"])
            return [json.loads(line) for user in (self.schema.ADMIN_ID,self.schema.WRITER_ID)
                    for line in self.sql(self.actor(user,'\n'.join(statements))).splitlines()]

        before = envelopes()
        self.file(MIGRATION)
        self.assertEqual(before, envelopes())
        first_original = self.sql(f"SELECT pg_get_functiondef('{FIRST}'::REGPROCEDURE)")
        indexed_body = INDEXED.read_text().replace('BEGIN;', '', 1).rsplit('COMMIT;', 1)[0]
        self.assert_sql_fails('BEGIN;' + first_original.replace(
            'RETURN QUERY', '/* fixture drift */ RETURN QUERY', 1) + ';' + indexed_body + 'ROLLBACK;',
            'ordinary first-term reader definition differs')
        self.assert_sql_fails('BEGIN; GRANT EXECUTE ON FUNCTION ' + FIRST
            + ' TO storage_v2_shadow_worker;' + indexed_body + 'ROLLBACK;',
            'ordinary first-term reader authority differs')
        self.assert_sql_fails('BEGIN; ALTER TABLE storage_v2_lexical_segment '
            'DISABLE TRIGGER storage_v2_flat_lexical_identity;' + indexed_body + 'ROLLBACK;',
            'ordinary first-term vector immutability differs')
        self.file(INDEXED)
        scoped_body = SCOPED.read_text().replace('BEGIN;', '', 1).rsplit('COMMIT;', 1)[0]
        for change, expected in (
            ('ALTER ROLE mainrag_v2_lexical_rank_owner LOGIN;', 'isolated reader role differs'),
            ('GRANT mainrag_v2_lexical_rank_owner TO storage_v2_shadow_worker;', 'isolated reader role differs'),
            ('GRANT SELECT ON storage_v2_ordinary_first_term TO mainrag;', 'projection authority differs'),
            ('GRANT UPDATE ON storage_v2_ordinary_first_coverage TO mainrag_v2_lexical_rank_owner;', 'projection authority differs'),
            ('ALTER TABLE storage_v2_ordinary_first_coverage NO FORCE ROW LEVEL SECURITY;', 'projection authority differs'),
            (f'GRANT EXECUTE ON FUNCTION {FIRST} TO mainrag;', 'bounded reader authority differs'),
        ):
            self.assert_sql_fails('BEGIN;' + change + scoped_body + 'ROLLBACK;', expected)
        self.file(SCOPED)
        self.assert_sql_fails('SET SESSION AUTHORIZATION mainrag; SET ROLE mainrag_v2_lexical_rank_owner',
                              'permission denied to set role')
        self.assertEqual(before, envelopes())
        self.assert_sql_fails(self.actor(self.schema.WRITER_ID,
            'SELECT * FROM storage_v2_materialize_ordinary_first_terms(6,0,128)'),
            'administrator authority')
        self.assert_sql_fails(self.admin(
            'SELECT * FROM storage_v2_materialize_ordinary_first_terms(6,0,129)'),
            'bounded ordinary first-term cursor')
        self.assert_sql_fails('SET ROLE mainrag; SELECT * FROM storage_v2_ordinary_first_term',
                              'permission denied')
        partial = json.loads(self.sql(self.admin(
            'SELECT row_to_json(x) FROM storage_v2_materialize_ordinary_first_terms(6,0,1) x')))
        self.assertEqual(partial['scanned'], 1)
        self.assertEqual(partial['materialized'], 1)
        self.assertEqual(before, envelopes())
        for source in (6, 9):
            built = json.loads(self.sql(self.admin(
                f'SELECT row_to_json(x) FROM storage_v2_materialize_ordinary_first_terms({source},0,128) x')))
            self.assertGreater(built['scanned'], 0)
            repeated = json.loads(self.sql(self.admin(
                f'SELECT row_to_json(x) FROM storage_v2_materialize_ordinary_first_terms({source},0,128) x')))
            self.assertEqual(repeated['materialized'], 0)
            self.assertEqual(repeated['inserted_terms'], 0)
        self.assertEqual(before, envelopes())
        self.assertEqual(self.sql("""WITH expected AS (
            SELECT segment.occurrence_id,item.lexeme,min(segment.segment_order) segment_order
            FROM storage_v2_lexical_segment segment CROSS JOIN LATERAL unnest(segment.fts_vector) item
            WHERE segment.source_id IN (6,9) GROUP BY 1,2
        ), actual AS (SELECT occurrence_id,lexeme,segment_order FROM storage_v2_ordinary_first_term)
        SELECT NOT EXISTS(SELECT * FROM expected EXCEPT ALL SELECT * FROM actual)
           AND NOT EXISTS(SELECT * FROM actual EXCEPT ALL SELECT * FROM expected)"""), 't')
        # A supported writer inserts a lower sparse order. Publication must
        # invalidate just that occurrence; complete vector fallback stays exact.
        changed = next(row for row in rows if row['text'].endswith('gamma'))
        coverage_before = int(self.sql('SELECT count(*) FROM storage_v2_ordinary_first_coverage'))
        self.sql(self.actor(self.schema.ADMIN_ID, 'SELECT storage_v2_put_lexical_segments_located('
            f"{changed['id']},{changed['artifact']},ARRAY[1000::BIGINT],"
            f"ARRAY[{self.quote(changed['text'])}],ARRAY[''],ARRAY['text'],"
            'ARRAY[1::BIGINT],ARRAY[1::BIGINT])'))
        self.assertEqual(int(self.sql('SELECT count(*) FROM storage_v2_ordinary_first_coverage')),
                         coverage_before - 1)
        self.assertEqual(self.sql('SELECT count(*) FROM storage_v2_ordinary_first_term '
                                 f"WHERE occurrence_id={changed['id']}"), '0')
        self.assertEqual(before, envelopes())
        self.sql(self.admin('SELECT * FROM storage_v2_materialize_ordinary_first_terms(6,0,128)'))
        self.sql(self.admin('SELECT * FROM storage_v2_materialize_ordinary_first_terms(9,0,128)'))
        self.assertEqual(self.sql("SELECT segment_order FROM storage_v2_ordinary_first_term "
                                 f"WHERE occurrence_id={changed['id']} AND lexeme='alpha'"), '1000')
        self.assertEqual(original[FULL], self.sql(f"SELECT pg_get_functiondef('{FULL}'::REGPROCEDURE)"))
        self.assert_sql_fails(self.actor(self.schema.ADMIN_ID,
            "SELECT * FROM storage_v2_authorized_lexical_first_candidates(ARRAY[1::BIGINT],ARRAY[6::BIGINT],'alpha')"),
            'permission denied for function storage_v2_authorized_lexical_first_candidates')
        scope = 'ARRAY(SELECT id FROM occurrence WHERE source_id IN (6,9))'
        comparisons = 0
        for user in (self.schema.ADMIN_ID,self.schema.WRITER_ID,self.schema.OTHER_ID):
            candidate_checks, rank_checks = [], []
            for requested in (scope,scope+'||'+scope+'||ARRAY[NULL]::BIGINT[]','ARRAY[]::BIGINT[]','NULL::BIGINT[]'):
                for sources in ('ARRAY[6,9]::BIGINT[]','ARRAY[6,6,NULL]::BIGINT[]','ARRAY[1,2]::BIGINT[]','ARRAY[]::BIGINT[]'):
                    for query in ('alpha','alpha beta','"alpha beta"','alpha OR gamma','alpha -beta','日本語','missing'):
                        arguments = f'{requested},{sources},{self.quote(query)}'
                        # Full enumeration is unchanged and independently
                        # reduced here, rather than using the new reader.
                        candidate_checks.append(f"""
                            WITH a AS (SELECT occurrence_id,source_id,artifact_version_id,min(segment_order) AS segment_order
                                FROM storage_v2_authorized_lexical_first_candidates({arguments}) GROUP BY 1,2,3),
                            r AS (SELECT occurrence_id,source_id,artifact_version_id,min(segment_order) AS segment_order
                                FROM storage_v2_authorized_lexical_candidates({arguments}) GROUP BY 1,2,3)
                            SELECT NOT EXISTS(SELECT * FROM a EXCEPT ALL SELECT * FROM r)
                               AND NOT EXISTS(SELECT * FROM r EXCEPT ALL SELECT * FROM a);""")
                        comparisons += 1
                        for index, suffix in enumerate(('', ','+sources)):
                            args = f'{requested},{self.quote(query)}'+suffix
                            rank_checks.append(f"""
                                WITH a AS (SELECT * FROM storage_v2_source_segment_rank_candidates({args})),
                                     r AS (SELECT * FROM fixture_complete_ranks_{index}({args}))
                                SELECT NOT EXISTS(SELECT * FROM a EXCEPT ALL SELECT * FROM r)
                                   AND NOT EXISTS(SELECT * FROM r EXCEPT ALL SELECT * FROM a);""")
            values = self.sql(f"SET ROLE mainrag_v2_frontier_owner; SET app.user_id='{user}'; "+'\n'.join(candidate_checks)).splitlines()
            self.assertEqual(values, ['t']*len(candidate_checks))
            values = self.sql(self.actor(user,'\n'.join(rank_checks))).splitlines()
            self.assertEqual(values, ['t']*len(rank_checks))
        fanout = self.sql(f"SET ROLE mainrag_v2_frontier_owner; SET app.user_id='{self.schema.ADMIN_ID}'; "+f"""
            SELECT (SELECT count(*) FROM storage_v2_authorized_lexical_candidates({scope},ARRAY[6,9]::BIGINT[],'alpha'))
                ||':'||(SELECT count(*) FROM storage_v2_authorized_lexical_first_candidates({scope},ARRAY[6,9]::BIGINT[],'alpha'))""")
        full, first = map(int, fanout.split(':'))
        self.assertGreater(full, first*10)
        self.assertLessEqual(first, int(self.sql('SELECT count(*)*2 FROM occurrence WHERE source_id IN (6,9)')))
        print(f'{len(before)} full envelopes; {comparisons} independent first-match and {comparisons*2} rank comparisons; fanout {full}->{first}', flush=True)
