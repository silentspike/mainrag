"""Complete metadata reuse, mutation fallback, authority and lifecycle proofs."""
import json
import time
from concurrent.futures import ThreadPoolExecutor

from eval.storage_v2.harness import PsqlSession

from eval.storage_v2.schema.presence_reader_fixture import register_metadata_role_cleanup


def verify_generation_metadata(test, migration, envelopes, baseline):
    test.sql('ALTER TABLE users ADD COLUMN IF NOT EXISTS fixture_private_value TEXT')
    exact = 'storage_v2_search_exact(bigint,text,jsonb,jsonb,bigint)'
    active = 'storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'
    lifecycle = ('storage_v2_verify_generation(bigint,text)',
                 'storage_v2_requalify_generation(bigint,text)')
    # Match the production lifecycle ACLs, instead of retaining the fixture's
    # generic grant of every historical function to its synthetic worker.
    for signature in lifecycle:
        test.sql(f'REVOKE ALL ON FUNCTION {signature} FROM storage_v2_shadow_worker')
    reference = test.sql(f"SELECT pg_get_functiondef('{exact}'::REGPROCEDURE)").replace(
        'public.storage_v2_search_exact', 'public.fixture_metadata_exact', 1)
    test.sql(reference + '; ALTER FUNCTION fixture_metadata_exact(BIGINT,TEXT,JSONB,JSONB,BIGINT) OWNER TO mainrag')
    def active_scopes():
        result = []
        for user in (test.schema.ADMIN_ID, test.schema.WRITER_ID, test.schema.OTHER_ID):
            includes = ('TRUE', 'FALSE') if user==test.schema.ADMIN_ID else ('FALSE',)
            sources = ('NULL', '6', '9') if user==test.schema.ADMIN_ID else (
                ('NULL', '6') if user==test.schema.WRITER_ID else ('NULL',))
            statements = [f"SELECT storage_v2_search_active_unchecked('{'a'*64}',"
                f"'{{\"type\":\"term\",\"value\":\"alpha\"}}','{{}}',10,{source},{include});"
                for source in sources for include in includes]
            result.extend(json.loads(row) for row in test.sql(test.actor(user, '\n'.join(statements))).splitlines())
        return result
    active_before = active_scopes()
    body = migration.read_text().replace('BEGIN;', '', 1).rsplit('COMMIT;', 1)[0]
    for signature in (exact, active, *lifecycle):
        old = test.sql(f"SELECT pg_get_functiondef('{signature}'::REGPROCEDURE)")
        test.assert_sql_fails('BEGIN;' + old.replace('BEGIN', '/* fixture drift */ BEGIN', 1)
                              + ';' + body + 'ROLLBACK;', 'reader definition differs')
        test.assert_sql_fails('BEGIN; GRANT EXECUTE ON FUNCTION ' + signature
                              + ' TO storage_v2_shadow_worker;' + body + 'ROLLBACK;', 'reader authority differs')
    test.assert_sql_fails('BEGIN; GRANT EXECUTE ON FUNCTION ' + active
                          + ' TO PUBLIC;' + body + 'ROLLBACK;', 'reader authority differs')
    test.assert_sql_fails('BEGIN; REVOKE EXECUTE ON FUNCTION ' + exact
                          + ' FROM PUBLIC;' + body + 'ROLLBACK;', 'reader authority differs')
    test.assert_sql_fails('BEGIN; DROP INDEX idx_occurrence_view; CREATE INDEX idx_occurrence_view '
                          'ON occurrence(source_id,view_id);' + body + 'ROLLBACK;', 'invalidation indexes differ')
    test.assert_sql_fails('BEGIN; CREATE ROLE mainrag_v2_metadata_reader LOGIN;'
                          + body + 'ROLLBACK;', 'namespace already exists')
    test.file(migration)
    register_metadata_role_cleanup(test.stack, test.socket)
    test.assertEqual(baseline, envelopes(), 'missing metadata must use the complete original reader')
    generations = json.loads(test.sql('SELECT jsonb_agg(id ORDER BY id) FROM source_generation'))
    gen6 = int(test.sql('SELECT id FROM source_generation WHERE source_id=6 AND generation_seq=1'))
    gen9 = int(test.sql('SELECT id FROM source_generation WHERE source_id=9 AND generation_seq=1'))
    for generation in generations:
        call = f'SELECT row_to_json(x) FROM storage_v2_materialize_reader_metadata({generation}) x'
        initial = json.loads(test.sql(test.admin(call)))
        repeated = json.loads(test.sql(test.admin(call)))
        test.assertFalse(initial['reused'])
        test.assertEqual({**initial, 'reused': True}, repeated)
    test.assertEqual(baseline, envelopes(), 'cached named and active full envelopes differ')
    test.assertEqual(active_before, active_scopes(), 'combined sources, test inclusion and authorization differ')
    for user in (test.schema.WRITER_ID, test.schema.OTHER_ID):
        test.assert_sql_fails(test.actor(user, f"SELECT storage_v2_search_active_unchecked('{'a'*64}',"
            "'{\"type\":\"term\",\"value\":\"alpha\"}','{}',10,NULL,TRUE)"),
            'test scope requires administrator authority')
    for user, source in ((test.schema.WRITER_ID,9),(test.schema.OTHER_ID,6),(test.schema.OTHER_ID,9)):
        test.assert_sql_fails(test.actor(user, f"SELECT storage_v2_search_active_unchecked('{'a'*64}',"
            f"'{{\"type\":\"term\",\"value\":\"alpha\"}}','{{}}',10,{source},FALSE)"), 'source access denied')
    # Derive metadata independently from the authoritative membership join.
    equality = f"""WITH expected AS (
        SELECT o.id occurrence_id,b.ordinal component_ordinal,b.document_id,b.role_weight,d.token_count
          FROM occurrence o JOIN generation_item_version m ON m.source_id=o.source_id
           AND m.artifact_version_id=o.artifact_version_id
          JOIN source_generation g ON g.id={gen6} AND g.source_id=m.source_id
          JOIN storage_v2_search_view_document b ON b.view_id=o.view_id
          JOIN storage_v2_search_document d ON d.id=b.document_id
         WHERE m.valid_from_seq<=g.generation_seq AND (m.valid_to_seq IS NULL OR m.valid_to_seq>g.generation_seq)
    ), actual AS (SELECT * FROM storage_v2_reader_metadata_bindings(ARRAY[{gen6},{gen6}]::BIGINT[])),
    expected_views AS (SELECT occurrence_id,SUM(token_count)::DOUBLE PRECISION view_length FROM expected GROUP BY 1),
    actual_views AS (SELECT * FROM storage_v2_reader_metadata_views(ARRAY[{gen6}]::BIGINT[]))
    SELECT NOT EXISTS(SELECT * FROM expected EXCEPT ALL SELECT * FROM actual)
       AND NOT EXISTS(SELECT * FROM actual EXCEPT ALL SELECT * FROM expected)
       AND NOT EXISTS(SELECT * FROM expected_views EXCEPT ALL SELECT * FROM actual_views)
       AND NOT EXISTS(SELECT * FROM actual_views EXCEPT ALL SELECT * FROM expected_views)"""
    test.assertEqual(test.sql(test.admin(equality)), 't')
    for ids, expected in [('NULL::BIGINT[]', 'f'), ('ARRAY[]::BIGINT[]', 't'),
                          (f'ARRAY[{gen6},NULL]::BIGINT[]', 'f'), ('ARRAY[-1]::BIGINT[]', 'f'),
                          (f'ARRAY[{gen6},{gen6}]::BIGINT[]', 't')]:
        test.assertEqual(test.sql(test.admin(f'SELECT storage_v2_reader_metadata_ready({ids})')), expected)
    test.assertEqual(test.sql(test.actor(test.schema.WRITER_ID,
        f'SELECT storage_v2_reader_metadata_ready(ARRAY[{gen6}]::BIGINT[]); '
        f'SELECT storage_v2_reader_metadata_ready(ARRAY[{gen6},{gen9}]::BIGINT[])')), 't\nf')
    test.assertEqual(test.sql(test.actor(test.schema.OTHER_ID,
        f'SELECT count(*) FROM storage_v2_reader_metadata_bindings(ARRAY[{gen6}]::BIGINT[])')), '0')
    test.assert_sql_fails(test.actor(test.schema.OTHER_ID,
        f'SELECT * FROM storage_v2_materialize_reader_metadata({gen6})'), 'source write authority')
    test.assert_sql_fails('SET SESSION AUTHORIZATION mainrag; SET ROLE mainrag_v2_metadata_reader',
                          'permission denied to set role')
    for relation in ('epoch', 'header', 'binding', 'view'):
        table = 'storage_v2_reader_metadata_' + relation
        test.assert_sql_fails(f'SET ROLE mainrag; SELECT * FROM {table}', 'permission denied')
        test.assert_sql_fails(f'SET ROLE mainrag_v2_metadata_reader; DELETE FROM {table} WHERE FALSE',
                              'permission denied')
    test.assert_sql_fails('SET ROLE mainrag_v2_metadata_reader; SELECT fixture_private_value FROM users',
                          'permission denied')
    test.assertEqual(test.sql("SELECT NOT rolcanlogin AND NOT rolsuper AND NOT rolbypassrls "
        "AND NOT rolcreaterole AND NOT rolcreatedb AND NOT rolinherit FROM pg_roles "
        "WHERE rolname='mainrag_v2_metadata_reader'"), 't')
    test.assertEqual(test.sql("SELECT count(*) FROM pg_auth_members WHERE roleid="
        "'mainrag_v2_metadata_reader'::REGROLE OR member='mainrag_v2_metadata_reader'::REGROLE"), '0')
    # Missing, partial, stale and identity-mismatched caches must fall back.
    query = f"SELECT storage_v2_search_exact(6,'1','{{\"type\":\"term\",\"value\":\"alpha\"}}','{{}}',10)"
    original = test.sql(test.admin(query))
    mutations = [
        f'UPDATE storage_v2_reader_metadata_header SET complete=FALSE WHERE generation_id={gen6};',
        f'UPDATE storage_v2_reader_metadata_header SET generation_seq=generation_seq+1 WHERE generation_id={gen6};',
        f'UPDATE storage_v2_reader_metadata_epoch SET revision=revision+1 WHERE source_id=6;',
        f'DELETE FROM storage_v2_reader_metadata_binding WHERE generation_id={gen6} AND occurrence_id='
            f'(SELECT min(occurrence_id) FROM storage_v2_reader_metadata_binding WHERE generation_id={gen6});',
        f'DELETE FROM storage_v2_reader_metadata_view WHERE generation_id={gen6} AND occurrence_id='
            f'(SELECT min(occurrence_id) FROM storage_v2_reader_metadata_view WHERE generation_id={gen6});',
        f'DELETE FROM storage_v2_reader_metadata_header WHERE generation_id={gen6};',
    ]
    for mutation in mutations:
        result = test.sql('BEGIN;' + mutation + test.admin(
            f'SELECT storage_v2_reader_metadata_ready(ARRAY[{gen6}]::BIGINT[]);' + query + ';') + 'ROLLBACK;')
        ready, envelope = result.splitlines()
        test.assertEqual(ready, 'f')
        test.assertEqual(json.loads(envelope), json.loads(original))
    # Statement transition tables invalidate by source, including shared views.
    # No-op row updates are used only inside this disposable rolled-back DB.
    invalidation = (
        ('generation_item_version', 'UPDATE generation_item_version SET valid_from_seq=valid_from_seq WHERE source_id=6;'),
        ('occurrence', 'UPDATE occurrence SET ordinal=ordinal WHERE source_id=6;'),
        ('artifact_version', 'UPDATE artifact_version SET item_id=item_id WHERE source_id=6;'),
    )
    # Keep the new statement trigger enabled while bypassing only the original
    # immutable-row guards to exercise UPDATE transitions on real fixture rows.
    for table, update in invalidation:
        mutation = (f'ALTER TABLE {table} DISABLE TRIGGER USER; '
                    f'ALTER TABLE {table} ENABLE TRIGGER storage_v2_reader_metadata_update; '
                    + update + f'ALTER TABLE {table} ENABLE TRIGGER USER;')
        result = test.sql('BEGIN;' + mutation + test.admin(
            f'SELECT storage_v2_reader_metadata_ready(ARRAY[{gen6}]::BIGINT[]); '
            f'SELECT storage_v2_reader_metadata_ready(ARRAY[{gen9}]::BIGINT[]);') + 'ROLLBACK;')
        test.assertEqual(result, 'f\nt')
    # A publication error cannot replace an already complete header or leave
    # its partial rows visible after the transaction aborts.
    test.assert_sql_fails('BEGIN; DELETE FROM storage_v2_reader_metadata_header '
        f'WHERE generation_id={gen6}; CREATE FUNCTION fixture_metadata_failure() RETURNS TRIGGER '
        "LANGUAGE plpgsql AS $$BEGIN RAISE EXCEPTION 'fixture metadata publication failure'; END$$; "
        'CREATE TRIGGER fixture_metadata_failure BEFORE UPDATE ON storage_v2_reader_metadata_header '
        'FOR EACH ROW EXECUTE FUNCTION fixture_metadata_failure(); ' + test.admin(
        f'SELECT * FROM storage_v2_materialize_reader_metadata({gen6});') + 'ROLLBACK;',
        'fixture metadata publication failure')
    test.assertEqual(test.sql(test.admin(f'SELECT storage_v2_reader_metadata_ready(ARRAY[{gen6}]::BIGINT[])')), 't')

    # Removing a shared binding invalidates both sources. Preserve the
    # original missing-document rejection even when metadata is republished.
    binding = json.loads(test.sql("""SELECT row_to_json(row) FROM (
        SELECT b.view_id,b.ordinal,b.document_id,b.role_weight
          FROM storage_v2_search_view_document b JOIN occurrence o ON o.view_id=b.view_id
         WHERE o.source_id=6 ORDER BY b.view_id LIMIT 1) row"""))
    mutation = ('ALTER TABLE storage_v2_search_view_document DISABLE TRIGGER USER; '
        'ALTER TABLE storage_v2_search_view_document ENABLE TRIGGER storage_v2_reader_metadata_delete; '
        f"DELETE FROM storage_v2_search_view_document WHERE view_id={binding['view_id']} AND ordinal={binding['ordinal']}; "
        'ALTER TABLE storage_v2_search_view_document ENABLE TRIGGER USER;')
    test.assertEqual(test.sql('BEGIN;' + mutation + test.admin(
        f'SELECT storage_v2_reader_metadata_ready(ARRAY[{gen6}]::BIGINT[]); '
        f'SELECT storage_v2_reader_metadata_ready(ARRAY[{gen9}]::BIGINT[]);') + 'ROLLBACK;'), 'f\nf')
    test.assert_sql_fails('BEGIN;' + mutation + test.admin(
        f'SELECT * FROM storage_v2_materialize_reader_metadata({gen6}); ' + query + ';') + 'ROLLBACK;',
        'required lexical search document missing')
    test.assertEqual(test.sql('BEGIN; TRUNCATE storage_v2_search_document CASCADE; ' + test.admin(
        f'SELECT storage_v2_reader_metadata_ready(ARRAY[{gen6}]::BIGINT[]); '
        f'SELECT storage_v2_reader_metadata_ready(ARRAY[{gen9}]::BIGINT[]);') + 'ROLLBACK;'), 'f\nf')
    test.assertEqual(test.sql('BEGIN; ALTER TABLE storage_v2_search_document DISABLE TRIGGER USER; '
        'ALTER TABLE storage_v2_search_document ENABLE TRIGGER storage_v2_reader_metadata_update; '
        f"UPDATE storage_v2_search_document SET token_count=token_count WHERE id={binding['document_id']}; "
        + test.admin(f'SELECT storage_v2_reader_metadata_ready(ARRAY[{gen6}]::BIGINT[]); '
                     f'SELECT storage_v2_reader_metadata_ready(ARRAY[{gen9}]::BIGINT[]);') + 'ROLLBACK;'), 'f\nf')
    # A real ordinary-ingest commit closes the old membership intervals while
    # leaving that historical generation visible. It waits on the cache's
    # source revision fence; readers retain the complete old snapshot.
    run = test.begin(6, 'e5'*32, 'e6'*32, user_id=test.schema.WRITER_ID)
    with PsqlSession(test.socket) as holder, ThreadPoolExecutor(max_workers=1) as executor:
        holder.sql('\\connect ' + test.database)
        holder.sql('BEGIN; SELECT revision FROM storage_v2_reader_metadata_epoch WHERE source_id=6 FOR UPDATE;')
        # Set the backend label in the same connection as the supported commit.
        root = test.sql(test.actor(test.schema.WRITER_ID, f'SELECT storage_v2_shadow_generation_root({run})'))
        worker = executor.submit(test.sql,
            "SET application_name='fixture_metadata_concurrent'; SET statement_timeout='15s'; "
            + test.actor(test.schema.WRITER_ID,
                f"SELECT (storage_v2_commit_shadow_ingest({run},0,'{root}')).status;"))
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if test.sql("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE "
                            "application_name='fixture_metadata_concurrent' AND wait_event_type='Lock')") == 't':
                    break
                time.sleep(0.05)
            else:
                test.fail('the actual membership invalidator did not wait for the source publication fence')
            frozen, _ = holder.sql(test.admin(query + '; ' + query.replace(
                'storage_v2_search_exact', 'fixture_metadata_exact') + ';'))
            old, independent = frozen.splitlines()
            test.assertEqual(json.loads(old), json.loads(independent))
        except (RuntimeError, BrokenPipeError):
            holder.process.wait(timeout=5)
            raise
        finally:
            if holder.process.poll() is None:
                holder.sql('COMMIT;')
            test.assertEqual(worker.result(timeout=15), 'sealed')
    test.assertEqual(test.sql(test.admin(
        f'SELECT storage_v2_reader_metadata_ready(ARRAY[{gen6}]::BIGINT[]); '
        f'SELECT storage_v2_reader_metadata_ready(ARRAY[{gen9}]::BIGINT[]);')), 'f\nt')
    test.assertEqual(json.loads(test.sql(test.admin(query))), json.loads(original))
    test.sql(test.admin(f'SELECT * FROM storage_v2_materialize_reader_metadata({gen6});'))
    # The cached path keeps path/role/time filters on their original join.
    for filters in ({'path_prefix': 'bounded-request-6-'}, {'role': 'source'},
                    {'occurred_from': '2000-01-01T00:00:00Z'}, {'occurred_to': '2100-01-01T00:00:00Z'}):
        filtered = query.replace("'{}'", test.quote(json.dumps(filters)) + '::JSONB')
        cached = test.sql(test.admin(filtered))
        fallback = test.sql('BEGIN; UPDATE storage_v2_reader_metadata_header SET complete=FALSE; '
                            + test.admin(filtered + ';') + 'ROLLBACK;')
        test.assertEqual(json.loads(cached), json.loads(fallback))
    # Populate through the normal verification lifecycle as a non-admin writer.
    generation = int(test.sql(f'SELECT generation_id FROM storage_v2_ingest_run WHERE id={run}'))
    test.assertEqual(test.sql(test.actor(test.schema.WRITER_ID,
        f"SELECT (storage_v2_verify_generation({generation},repeat('e',64))).status; "
        f'SELECT storage_v2_reader_metadata_ready(ARRAY[{generation}]::BIGINT[])')), 'verified\nt')
    print('Complete generation metadata, fallback, source isolation and regular verification validated', flush=True)
