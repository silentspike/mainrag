"""Bounded retained lexical representation conversion in an owned disposable DB."""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
import uuid

from eval.storage_v2.schema import test_document_cache_conversion as previous

ROOT = previous.ROOT
MIGRATION = ROOT / 'migrations/163_storage_v2_convert_retained_lexical_blocks.sql'
SIGNATURE = 'storage_v2_convert_retained_lexical_block(uuid,bytea,bigint,bigint,bytea,bytea,bigint,integer,bigint,bytea)'
LEDGER = 'storage_v2_lexical_conversion_receipt'


class RetainedLexicalConversionTests(unittest.TestCase):
    base = previous.DocumentCacheConversionTests.base
    schema = base.schema
    command = classmethod(base.command.__func__)
    sql = classmethod(base.sql.__func__)
    file = classmethod(base.file.__func__)
    actor = staticmethod(base.actor)
    admin = classmethod(base.admin.__func__)
    quote = staticmethod(base.quote)
    make_projection = base.make_projection
    begin = base.begin
    stage = base.stage
    complete_analysis = base.complete_analysis
    commit = base.commit
    fixture = base.fixture
    assert_sql_fails = base.assert_sql_fails
    rank = previous.DocumentCacheConversionTests.rank
    envelope = previous.DocumentCacheConversionTests.envelope
    operation = '00000000-0000-0000-0000-000000000163'
    manifest = 'c3' * 32

    @classmethod
    def functions(cls):
        return json.loads(cls.sql("SELECT jsonb_object_agg(oid::REGPROCEDURE::TEXT,"
            "jsonb_build_object('definition',pg_get_functiondef(oid),'owner',proowner,'acl',proacl,'config',proconfig))"
            " FROM pg_proc WHERE pronamespace='public'::REGNAMESPACE AND prokind='f'"))

    @classmethod
    def setUpClass(cls):
        try:
            previous.DocumentCacheConversionTests.setUpClass.__func__(cls)
            cls.file(next(MIGRATION.parent.glob('162_*.sql')))
            cls.before = cls.functions()
            cls.sql(f"SET app.user_id='{cls.schema.ADMIN_ID}';" + MIGRATION.read_text())
            cls.after = cls.functions()
        except BaseException:
            if hasattr(cls, 'database'):
                cls.base.tearDownClass.__func__(cls)
            elif hasattr(cls, 'stack'):
                cls.stack.close()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.base.tearDownClass.__func__(cls)

    def array(self, values, kind):
        return 'ARRAY[' + ','.join(str(v) if isinstance(v, int) else self.quote(v) for v in values) + f']::{kind}[]'

    def retained(self, *, long=False, segment_count=64):
        prefix = 'é🙂甲Ω prelude\n' * (3000 if long else 3)
        segments = ['alpha beta é🙂甲Ω token_' + str(n % 11) for n in range(segment_count)]
        text = prefix
        positions = []
        for segment in segments:
            positions.append(len(text)); text += segment + '\n'
        occurrence, artifact = self.fixture(text)
        arguments = ','.join((str(occurrence), str(artifact), self.array(list(range(segment_count)), 'BIGINT'),
            self.array(segments, 'TEXT'), self.array(['titleproof'] * segment_count, 'TEXT'),
            self.array(['text'] * segment_count, 'TEXT'), self.array([p + 1 for p in positions], 'BIGINT'),
            self.array([len(text[:p].encode()) + 1 for p in positions], 'BIGINT')))
        self.sql(self.admin(f'SELECT storage_v2_put_lexical_segments_located({arguments})'))
        row = json.loads(self.sql("SELECT jsonb_build_object('occurrence',o.id,'source',o.source_id,'artifact',o.artifact_version_id,"
            "'document',d.id,'identity',encode(d.materialization_sha256,'hex'),"
            "'original',encode(sha256(convert_to(to_jsonb(block)::TEXT,'UTF8')),'hex'),"
            "'generation',(SELECT r.generation_id FROM storage_v2_ingest_run_item item JOIN storage_v2_ingest_run r ON r.id=item.run_id WHERE item.occurrence_id=o.id LIMIT 1)) "
            'FROM occurrence o JOIN storage_v2_compact_lexical_block block ON block.occurrence_id=o.id AND block.block_order=0 '
            'JOIN storage_v2_search_view_document b ON b.view_id=o.view_id AND b.ordinal=0 '
            f'JOIN storage_v2_search_document d ON d.id=b.document_id WHERE o.id={occurrence}'))
        self.sql(self.admin(f"SELECT storage_v2_verify_generation({row['generation']},'{ 'a3' * 32}')"))
        return row

    def operator(self, statement, *, timeout='10s'):
        return ('BEGIN;SET LOCAL lock_timeout=\'1s\';SET LOCAL statement_timeout=' + self.quote(timeout) + ';'
            f"SET LOCAL app.user_id='{self.schema.ADMIN_ID}';" + statement + ';COMMIT;')

    def call(self, row, *, operation=None, manifest=None, original=None, identity=None, maximum=8388608,
             retained_run=None, retained_sha=None):
        tail = '' if retained_run is None else f",{retained_run},decode('{retained_sha}','hex')"
        return ('SELECT row_to_json(r) FROM storage_v2_convert_retained_lexical_block('
            f"'{operation or self.operation}',decode('{manifest or self.manifest}','hex'),{row['occurrence']},0,"
            f"decode('{original or row['original']}','hex'),decode('{identity or row['identity']}','hex'),"
            f'{maximum},10000{tail}) r')

    def apply(self, row, **kwargs):
        return json.loads(self.sql(self.operator(self.call(row, **kwargs))))

    def logical(self, row):
        return self.sql(self.admin("SELECT jsonb_agg(to_jsonb(s) ORDER BY segment_order)::TEXT "
            f"FROM storage_v2_lexical_segment_all s WHERE occurrence_id={row['occurrence']}"))

    def owner_state(self, row):
        return self.sql("SELECT jsonb_build_object('occurrence',to_jsonb(o),'artifact',to_jsonb(a),"
            "'document',to_jsonb(d),'generation',to_jsonb(g),'logical_source',to_jsonb(l),"
            "'ingest_runs',(SELECT jsonb_agg(to_jsonb(r) ORDER BY id) FROM storage_v2_ingest_run r WHERE r.source_id=o.source_id),"
            "'reader_headers',(SELECT jsonb_agg(to_jsonb(h) ORDER BY generation_id) FROM storage_v2_reader_metadata_header h),"
            "'reader_epochs',(SELECT jsonb_agg(to_jsonb(e) ORDER BY source_id) FROM storage_v2_reader_metadata_epoch e))::TEXT "
            'FROM occurrence o JOIN artifact_version a ON a.id=o.artifact_version_id '
            'JOIN storage_v2_search_view_document b ON b.view_id=o.view_id AND b.ordinal=0 '
            f"JOIN storage_v2_search_document d ON d.id=b.document_id JOIN source_generation g ON g.id={row['generation']} "
            f"JOIN logical_source l ON l.id=o.source_id WHERE o.id={row['occurrence']}")

    def first(self, row, query, *, actor=None):
        # This invoker is not application-granted; it retains explicit per-source
        # authorization in its owner. Actual positive and hidden-source cases.
        return self.sql(f"SET app.user_id='{actor or self.schema.ADMIN_ID}';" +
            'SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY segment_order),\'[]\'::JSONB) '
            f"FROM storage_v2_authorized_lexical_first_candidates(ARRAY[{row['occurrence']},{row['occurrence']},NULL]::BIGINT[],"
            f"ARRAY[{row['source']}]::BIGINT[],{self.quote(query)}) r")

    def verify(self, row):
        result = self.sql(self.admin(f"SELECT storage_v2_verify_lexical_segment_page({row['generation']},0,64)"))
        self.sql(self.admin(f"SELECT storage_v2_verify_derived_lexical_masks({row['occurrence']})"))
        return result

    def test_exact_unicode_boundary_payload_rank_envelopes_verifier_and_replay(self):
        row = self.retained(long=True)
        queries = ('alpha', 'titleproof', 'alpha beta', 'alpha missing', 'alpha OR missing',
                   'alpha -beta', '"alpha beta"', 'alpha -"beta gamma"')
        readers = ('storage_v2_source_segment_ranks', 'storage_v2_source_segment_ranks_precise',
                   'storage_v2_source_segment_rank_candidates', 'storage_v2_source_segment_rank_candidates_scoped')
        ranks = {(reader, query): self.rank(row['occurrence'], query, reader=reader)
                 for reader in readers for query in queries}
        for reader in readers:
            self.assertTrue(json.loads(ranks[reader, 'alpha']))
        first = {query: self.first(row, query) for query in queries}
        self.assertTrue(json.loads(first['alpha']))
        asts = ({'type':'term','value':'alpha'}, {'type':'phrase','value':'alpha beta'}, {'type':'phrase','value':'alpha gamma'})
        envelopes = [self.envelope(row['source'], ast) for ast in asts]
        original = self.logical(row); owners = self.owner_state(row); verifier = self.verify(row)
        converted = self.apply(row)
        self.assertEqual((converted['disposition'], converted['segment_count']), ('CONVERTED', 64))
        self.assertEqual(self.logical(row), original)
        self.assertEqual(self.owner_state(row), owners)
        self.assertEqual(self.verify(row), verifier)
        self.assertEqual(self.sql(f"SELECT count(*) FROM storage_v2_compact_lexical_block WHERE occurrence_id={row['occurrence']}"), '0')
        self.assertEqual(self.sql(f"SELECT count(*) FROM storage_v2_derived_lexical_block WHERE occurrence_id={row['occurrence']}"), '1')
        for reader in readers:
            for query in queries:
                self.assertEqual(self.rank(row['occurrence'], query, reader=reader), ranks[reader, query])
            self.assertEqual(self.rank(row['occurrence'], 'alpha', self.schema.OTHER_ID, reader=reader), '[]')
        for query in queries:
            self.assertEqual(self.first(row, query), first[query])
            self.assertEqual(self.first(row, query, actor=self.schema.OTHER_ID), '[]')
        self.assertEqual([self.envelope(row['source'], ast) for ast in asts], envelopes)
        self.assertEqual(self.apply(row)['disposition'], 'REPLAY')
        self.assertEqual(self.sql('SELECT count(*) FROM storage_v2_lexical_conversion_intent'), '0')
        for changes in ({'manifest':'c4'*32}, {'original':'f0'*32}, {'operation':str(uuid.uuid4())}):
            self.assert_sql_fails(self.operator(self.call(row, **changes)), 'replay requires exact')

    def test_authority_unarmed_budget_named_identity_and_immutable_delete(self):
        row = self.retained(segment_count=7)
        original = self.logical(row)
        self.assert_sql_fails(self.admin(self.call(row)), 'permission denied for function')
        self.assert_sql_fails(self.actor(self.schema.OTHER_ID, self.call(row)), 'permission denied for function')
        self.assert_sql_fails(self.call(row), 'local administrative operator')
        self.assert_sql_fails(f"SET app.user_id='{self.schema.ADMIN_ID}';" + self.call(row), 'prearmed statement/lock deadlines')
        for changes in ({'identity':'f0'*32}, {'maximum':1}, {'original':'f0'*32}):
            self.assert_sql_fails(self.operator(self.call(row, **changes)), 'lexical')
        self.assert_sql_fails(self.operator(f"DELETE FROM storage_v2_compact_lexical_block WHERE occurrence_id={row['occurrence']}"),
            'same-transaction lexical transition')
        self.assert_sql_fails(self.operator(f"UPDATE storage_v2_compact_lexical_block SET text_starts=text_starts WHERE occurrence_id={row['occurrence']}"),
            'immutable')
        self.assertEqual(self.logical(row), original)
        self.assertEqual(self.apply(row)['segment_count'], 7)

    def test_corrupted_vector_missing_body_and_bad_character_anchors_roll_back(self):
        row = self.retained(segment_count=3)
        original = self.logical(row)
        for assignment in ("fts_vectors=array_fill(to_tsvector('simple','wrong'),ARRAY[3])", "text_starts=ARRAY[2,2,2]"):
            # Corruption is fixture-only and rolled back. Re-enable the actual
            # guard before admission, preserving its exact catalog contract.
            mutation = ('ALTER TABLE storage_v2_compact_lexical_block DISABLE TRIGGER storage_v2_compact_lexical_immutable;'
                f"UPDATE storage_v2_compact_lexical_block SET {assignment} WHERE occurrence_id={row['occurrence']};"
                'ALTER TABLE storage_v2_compact_lexical_block ENABLE TRIGGER storage_v2_compact_lexical_immutable;')
            call = self.call(row).replace("decode('"+row['original']+"','hex')", "(SELECT sha256(convert_to(to_jsonb(b)::TEXT,'UTF8')) FROM storage_v2_compact_lexical_block b WHERE occurrence_id="+str(row['occurrence'])+')')
            self.assert_sql_fails(self.operator(mutation+call), 'canonical hashes differ')
        self.assertEqual(self.logical(row), original)
        mutation = ('ALTER TABLE storage_v2_search_view_document DISABLE TRIGGER USER;'
            f"DELETE FROM storage_v2_search_view_document WHERE view_id=(SELECT view_id FROM occurrence WHERE id={row['occurrence']});"
            'ALTER TABLE storage_v2_search_view_document ENABLE TRIGGER USER;')
        self.assert_sql_fails(self.operator(mutation+self.call(row)), 'canonical lexical document required')
        self.assertEqual(self.logical(row), original)

    def test_inert_building_run_requires_exact_frozen_metadata_not_new_generation(self):
        row = self.retained(segment_count=3)
        source = int(self.sql("INSERT INTO sources(id,name,type,path) SELECT max(id)+1,'inert-conversion-run','fixture','synthetic-inert' FROM sources RETURNING id"))
        run = self.begin(source, uuid.uuid4().hex * 2, uuid.uuid4().hex * 2)
        state = self.sql(f"SELECT jsonb_build_object('run',to_jsonb(r),'generation',to_jsonb(g))::TEXT FROM storage_v2_ingest_run r JOIN source_generation g ON g.id=r.generation_id WHERE r.id={run}")
        digest = self.sql("SELECT encode(sha256(convert_to("+self.quote(state)+",'UTF8')),'hex')")
        try:
            self.assert_sql_fails(self.operator(self.call(row)), 'unbound building run')
            self.assert_sql_fails(self.operator(self.call(row, retained_run=run, retained_sha='f0'*32)), 'frozen owner state')
            self.assertEqual(self.apply(row, retained_run=run, retained_sha=digest)['disposition'], 'CONVERTED')
            self.assertEqual(self.sql(f"SELECT jsonb_build_object('run',to_jsonb(r),'generation',to_jsonb(g))::TEXT FROM storage_v2_ingest_run r JOIN source_generation g ON g.id=r.generation_id WHERE r.id={run}"), state)
        finally:
            self.sql(self.admin(f'SELECT (storage_v2_cancel_shadow_ingest({run},0)).status'))

    def test_existing_contracts_gc_roots_ledger_and_missing_destination_replay(self):
        self.assertEqual({name for name in self.before if self.before[name] != self.after[name]}, set())
        row = self.retained(segment_count=5)
        def load(label, filename):
            spec = importlib.util.spec_from_file_location(label, ROOT / 'ops/storage-v2' / filename)
            module = importlib.util.module_from_spec(spec);sys.modules[label] = module;spec.loader.exec_module(module)
            return module
        gc = load('retained_lexical_gc_fixture', 'native-gc.py')
        cleanup = load('retained_lexical_cleanup_fixture', 'cleanup-manifest.py')
        def roots():
            return self.sql('BEGIN;' + gc.graph_sql([]) + "SELECT jsonb_agg(jsonb_build_array(kind,id) ORDER BY kind,id)::TEXT FROM gc_mark;ROLLBACK;")
        before = roots()
        self.apply(row)
        self.assertEqual(roots(), before)
        self.assertEqual(self.sql(f"SELECT count(*) FROM pg_constraint WHERE conrelid='{LEDGER}'::REGCLASS AND contype='f'"), '0')
        self.assertNotIn(LEDGER, gc.TARGETS);self.assertNotIn(LEDGER, gc.LINKS)
        self.assertTrue(cleanup.retained_relation(LEDGER))
        self.sql(self.operator('SELECT storage_v2_posting_conversion_require_operator()'))
        self.assert_sql_fails(self.operator(f"DELETE FROM {LEDGER} WHERE occurrence_id={row['occurrence']}"), 'immutable')
        # A fixture-only missing destination must not turn its historic audit
        # into live acceptance. This is absence semantics, not production GC.
        mutation = ('ALTER TABLE storage_v2_derived_lexical_block DISABLE TRIGGER storage_v2_derived_lexical_immutable;'
            f"DELETE FROM storage_v2_derived_lexical_block WHERE occurrence_id={row['occurrence']};"
            'ALTER TABLE storage_v2_derived_lexical_block ENABLE TRIGGER storage_v2_derived_lexical_immutable;')
        self.assert_sql_fails(self.operator(mutation+self.call(row)), 'replay requires exact live')
        self.assertEqual(self.apply(row)['disposition'], 'REPLAY')


if __name__ == '__main__':
    unittest.main()
