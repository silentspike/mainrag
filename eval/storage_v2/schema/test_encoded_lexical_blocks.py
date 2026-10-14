"""One owned native cohort for optional encoded lexical blocks.

The actual retained operators/readers supply the oracle. Timings and stored
column costs are diagnostics, never a latency or physical reclamation gate.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
import time
import unittest

from eval.storage_v2.schema import test_fresh_document_no_fingerprints as current
from eval.storage_v2.schema import test_retained_lexical_conversion as lexical

ROOT = current.ROOT
MIGRATION = ROOT / 'migrations/172_storage_v2_encoded_lexical_blocks.sql'
CONVERTER = 'storage_v2_encode_retained_lexical_block'
TABLE = 'storage_v2_encoded_lexical_block'
LEDGER = 'storage_v2_encoded_lexical_receipt'
CACHED = 'storage_v2_authorized_cached_lexical_first_candidates'


class EncodedLexicalBlockTests(current.FreshDocumentNoFingerprintTests):
    # Reuse the genuine setup MRO; this gate owns only its native codec cohort.
    # Earlier format suites retain their separate acceptance gates.
    test_named_frozen_run_parity_cost_admission_replay_authority_writer_and_rollback = None
    test_early_ids_no_savings_and_mismatch_never_attempt_fts_update = None
    test_no_fingerprint_storage_all_readers_metadata_replay_and_insert_sentinel = None
    test_mismatch_rollback_and_unchanged_standard_161_and_168_modes = None
    test_fresh_insert_formats_helper_skip_logical_postings_readers_and_authority = None
    base = current.FreshDocumentNoFingerprintTests.base
    schema = base.schema
    command = classmethod(current.FreshDocumentNoFingerprintTests.command.__func__)
    sql = classmethod(base.sql.__func__)
    file = classmethod(base.file.__func__)
    actor = staticmethod(base.actor)
    admin = classmethod(base.admin.__func__)
    quote = staticmethod(base.quote)
    begin = base.begin
    stage = base.stage
    complete_analysis = base.complete_analysis
    commit = base.commit
    fixture = lexical.RetainedLexicalConversionTests.fixture
    assert_sql_fails = base.assert_sql_fails
    array = lexical.RetainedLexicalConversionTests.array
    operator = lexical.RetainedLexicalConversionTests.operator
    logical = lexical.RetainedLexicalConversionTests.logical
    owner_state = lexical.RetainedLexicalConversionTests.owner_state
    verify = lexical.RetainedLexicalConversionTests.verify
    rank = lexical.RetainedLexicalConversionTests.rank
    operation = '00000000-0000-0000-0000-000000000172'
    manifest = 'b2' * 32

    @classmethod
    def setUpClass(cls):
        current.FreshDocumentNoFingerprintTests.setUpClass.__func__(cls)
        try:
            cls.sql(f"SET app.user_id='{cls.schema.ADMIN_ID}';" +
                    next(MIGRATION.parent.glob('171_*.sql')).read_text())
            cls.before_functions = json.loads(cls.sql("SELECT jsonb_object_agg(oid::REGPROCEDURE::TEXT,"
                "jsonb_build_object('definition',pg_get_functiondef(oid),'authority',to_jsonb(p)-'prosrc')) "
                "FROM pg_proc p WHERE pronamespace='public'::REGNAMESPACE AND prokind='f'"))
            cls.original_types = cls.sql("SELECT jsonb_agg(jsonb_build_array(attrelid,attnum,attname,"
                "atttypid,attnotnull,attgenerated) ORDER BY attrelid,attnum) FROM pg_attribute "
                "WHERE attrelid IN ('storage_v2_compact_lexical_block'::REGCLASS,"
                "'storage_v2_derived_lexical_block'::REGCLASS) AND attnum>0 AND NOT attisdropped")
            cls.sql(f"SET app.user_id='{cls.schema.ADMIN_ID}';" + MIGRATION.read_text())
        except BaseException:
            cls.base.tearDownClass.__func__(cls)
            raise

    @classmethod
    def tearDownClass(cls):
        cls.base.tearDownClass.__func__(cls)

    def call(self, row, *, original=None, identity=None, stored=None, manifest=None):
        return ('SELECT row_to_json(r) FROM ' + CONVERTER + '('
                f"'{self.operation}',decode('{manifest or self.manifest}','hex'),{row['occurrence']},0,"
                f"decode('{original or row['original']}','hex'),decode('{identity or row['identity']}','hex'),"
                f"{row['stored'] if stored is None else stored},30000) r")

    def apply(self, row, **kwargs):
        return json.loads(self.sql(self.operator(self.call(row, **kwargs), timeout='30s')))

    def native(self, row, query, *, actor=None):
        return self.sql(f"SET app.user_id='{actor or self.schema.ADMIN_ID}';"
            "SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY occurrence_id,segment_order),'[]'::JSONB) "
            f"FROM {CACHED}(ARRAY[{row['occurrence']},{row['occurrence']},NULL]::BIGINT[],"
            f"ARRAY[{row['source']}]::BIGINT[],{self.quote(query)}) r")

    def block(self, segments, *, orphan=False):
        unique_segments = list(dict.fromkeys(segments))
        text = '\n'.join(unique_segments) + '\n'
        occurrence, artifact = self.fixture(text)
        if orphan:
            # Separate artifact prevents a retained artifact from implicitly
            # retaining every occurrence made from it. Only synthetic state.
            row = json.loads(self.sql(f"""WITH a AS (
 INSERT INTO artifact_version(item_id,source_id,witness_type,witness,adapter_profile_id,
  content_root_node_id,expected_content_hash,byte_length)
 SELECT item_id,source_id,'public-fixture-item','{{}}','encoded172-orphan-v1',
  content_root_node_id,expected_content_hash,byte_length FROM artifact_version WHERE id={artifact}
 RETURNING id,source_id
), o AS (
 INSERT INTO occurrence(source_id,artifact_version_id,view_id,role,ordinal,source_path,locator)
 SELECT a.source_id,a.id,original.view_id,'encoded-orphan',0,'encoded-orphan.txt','{{}}'
 FROM a CROSS JOIN occurrence original WHERE original.id={occurrence} RETURNING id,artifact_version_id
) SELECT row_to_json(o) FROM o"""))
            occurrence, artifact = row['id'], row['artifact_version_id']
        starts = {}; cursor = 1
        for segment in unique_segments:
            starts[segment] = cursor; cursor += len(segment) + 1
        positions = [starts[segment] for segment in segments]
        self.sql(self.admin('SELECT storage_v2_put_lexical_segments_located(' + ','.join((str(occurrence),
            str(artifact), self.array(list(range(len(segments))), 'BIGINT'), self.array(segments, 'TEXT'),
            self.array(['headingproof'] * len(segments), 'TEXT'), self.array(['text'] * len(segments), 'TEXT'),
            self.array(positions, 'BIGINT'), self.array([len(text[:p-1].encode()) + 1 for p in positions], 'BIGINT'))) + ')'))
        result = json.loads(self.sql("SELECT jsonb_build_object('occurrence',o.id,'source',o.source_id,"
            "'artifact',o.artifact_version_id,'document',d.id,'identity',encode(d.materialization_sha256,'hex'),"
            "'original',encode(sha256(convert_to(to_jsonb(c)::TEXT,'UTF8')),'hex'),"
            "'stored',32::BIGINT+pg_column_size(c.segment_orders)+pg_column_size(c.text_starts)+"
            "pg_column_size(c.text_lengths)+pg_column_size(c.text_hashes)+pg_column_size(c.context_prefixes)+"
            "pg_column_size(c.chunk_types)+pg_column_size(c.fts_vectors)+pg_column_size(c.fingerprints),"
            "'generation',(SELECT r.generation_id FROM storage_v2_ingest_run_item i JOIN storage_v2_ingest_run r "
            "ON r.id=i.run_id WHERE i.occurrence_id=o.id LIMIT 1)) "
            'FROM occurrence o JOIN storage_v2_compact_lexical_block c ON c.occurrence_id=o.id AND c.block_order=0 '
            'JOIN storage_v2_search_view_document b ON b.view_id=o.view_id AND b.ordinal=0 '
            f'JOIN storage_v2_search_document d ON d.id=b.document_id WHERE o.id={occurrence}'))
        if result['generation'] is not None:
            self.sql(self.admin(f"SELECT storage_v2_verify_generation({result['generation']},'{ 'b2' * 32}')"))
        return result, text, positions

    @staticmethod
    def cohort():
        # Five distinct canonical windows stay below the fresh derived-format
        # threshold. Repeated/overlapping immutable windows are legal; their
        # distinct word positions exercise stored LZ4 and local dictionary cost.
        words = ['word' + hashlib.sha256(f'encoded172-{n}'.encode()).hexdigest()[:28] for n in range(1500)]
        return ['alpha beta 日本語 ' + ' '.join(sorted(words,
            key=lambda word: hashlib.sha256(f'{n % 5}:{word}'.encode()).digest())) for n in range(64)]

    def test_atomic_native_cohort_replay_rank_witness_acl_cost_and_gc(self):
        segments = self.cohort()
        after_functions = json.loads(self.sql("SELECT jsonb_object_agg(oid::REGPROCEDURE::TEXT,"
            "jsonb_build_object('definition',pg_get_functiondef(oid),'authority',to_jsonb(p)-'prosrc')) "
            "FROM pg_proc p WHERE pronamespace='public'::REGNAMESPACE AND prokind='f'"))
        read_names = {'storage_v2_put_lexical_segments_located',CACHED,
            'storage_v2_verify_lexical_segment_page','storage_v2_put_lexical_segments_document_private'}
        allowed = read_names | {'storage_v2_guard_converted_lexical_delete'}
        changed = set()
        for signature, before in self.before_functions.items():
            self.assertEqual(after_functions[signature]['authority'],before['authority'],signature)
            if after_functions[signature]['definition'] != before['definition']:
                changed.add(signature.split('(')[0]);self.assertIn(signature.split('(')[0],allowed)
                if signature.split('(')[0] in read_names:
                    expected = re.sub(r'\b(FROM|JOIN)(\s+)(?:public\.)?storage_v2_compact_lexical_block\b',
                        r'\1\2public.storage_v2_compact_lexical_block_all',before['definition'],flags=re.IGNORECASE)
                    # Whole-definition equality proves every original writer,
                    # scope, shape, budget and collision clause is unchanged.
                    self.assertEqual(after_functions[signature]['definition'],expected,signature)
        self.assertEqual(changed,allowed)
        row, text, positions = self.block(segments)
        logical = self.logical(row); owners = self.owner_state(row); verification = self.verify(row)
        native_before = {q:self.native(row,q) for q in ('alpha beta','headingproof','alpha absent172', '"alpha beta"')}
        self.assertTrue(json.loads(native_before['alpha beta']))
        ranks = {q:self.rank(row['occurrence'],q) for q in ('alpha beta','"alpha beta"','headingproof')}
        started = time.perf_counter(); self.native(row,'alpha beta'); original_seconds = time.perf_counter()-started
        for changes, error in (({'original':'00'*32},'exact unconverted'),
                               ({'identity':'00'*32},'immutable lexical owner'),
                               ({'stored':row['stored']+1},'stored column costs changed')):
            self.assert_sql_fails(self.operator(self.call(row,**changes),timeout='30s'),error)
        self.assert_sql_fails(self.operator("SET LOCAL client_encoding='LATIN1';"+self.call(row),timeout='30s'),
                              'prearmed UTF8 client encoding')
        self.assert_sql_fails(self.admin(self.call(row)),'permission denied')
        self.assert_sql_fails(self.actor(self.schema.OTHER_ID,self.call(row)),'permission denied')
        self.assertEqual(self.logical(row),logical)
        converted = self.apply(row)
        self.assertEqual(converted['disposition'],'CONVERTED')
        self.assertLess(converted['encoded_stored_bytes']+converted['audit_bytes']+converted['index_growth_bytes'],
                        converted['original_stored_bytes'])
        self.assertEqual(self.logical(row),logical)
        self.assertEqual(self.owner_state(row),owners)
        self.assertEqual(self.verify(row),verification)
        for q,result in native_before.items(): self.assertEqual(self.native(row,q),result)
        for q,result in ranks.items(): self.assertEqual(self.rank(row['occurrence'],q),result)
        self.assertEqual(self.native(row,'alpha',actor=self.schema.OTHER_ID),'[]')
        self.assertEqual(self.sql(self.actor(self.schema.OTHER_ID,
            f"SELECT count(*) FROM {TABLE} WHERE occurrence_id={row['occurrence']}")),'0')
        self.assertEqual(self.sql(self.admin(
            f"SELECT count(*) FROM {TABLE} WHERE occurrence_id={row['occurrence']}")),'1')
        started = time.perf_counter(); self.native(row,'alpha beta'); encoded_seconds = time.perf_counter()-started
        print('encoded172 diagnostics '+json.dumps({'original_cached_seconds':original_seconds,
            'encoded_cached_seconds':encoded_seconds,'stored_cost_admission':converted},sort_keys=True))
        self.assertEqual(self.apply(row)['disposition'],'REPLAY')
        self.assert_sql_fails(self.operator(self.call(row,manifest='c2'*32),timeout='30s'),'replay requires exact')
        # Replay the real producer after atomic move: no original owner is recreated.
        self.sql(self.admin('SELECT storage_v2_put_lexical_segments_located(' + ','.join((str(row['occurrence']),
            str(row['artifact']),self.array(list(range(64)),'BIGINT'),self.array(segments,'TEXT'),
            self.array(['headingproof']*64,'TEXT'),self.array(['text']*64,'TEXT'),self.array(positions,'BIGINT'),
            self.array([len(text[:p-1].encode())+1 for p in positions],'BIGINT'))) + ')'))
        self.assertEqual(self.sql(f"SELECT count(*) FROM storage_v2_compact_lexical_block WHERE occurrence_id={row['occurrence']}"),'0')
        self.assertEqual(self.sql(f"SELECT encode(sha256(convert_to(to_jsonb(c)::TEXT,'UTF8')),'hex') "
            f"FROM storage_v2_compact_lexical_block_all c WHERE occurrence_id={row['occurrence']} AND block_order=0"),row['original'])
        self.assert_sql_fails(self.operator(f"DELETE FROM {TABLE} WHERE occurrence_id={row['occurrence']}"),'immutable')
        self.assert_sql_fails(self.operator(f"DELETE FROM {LEDGER} WHERE occurrence_id={row['occurrence']}"),'immutable')
        # A tiny unprofitable owner is refused before representation writes;
        # subsequent inverse-budget failure covers rollback after actual writes.
        tiny, _, _ = self.block(['alpha beta'])
        tiny_logical = self.logical(tiny)
        self.assert_sql_fails(self.operator(self.call(tiny),timeout='30s'),'stored costs do not improve')
        self.assertEqual(self.logical(tiny),tiny_logical)
        self.assertEqual(self.sql(f"SELECT count(*) FROM {TABLE} WHERE occurrence_id={tiny['occurrence']}"),'0')
        self.assertEqual(self.sql('SELECT count(*) FROM storage_v2_encoded_lexical_intent'),'0')
        # Guard/ACL/view/catalog drift is observed before any representation move.
        self.assert_sql_fails(self.operator('GRANT SELECT ON '+TABLE+' TO PUBLIC;'+self.call(row),timeout='30s'),
                              'representation/catalog changed')
        self.assertEqual(self.sql("SELECT jsonb_agg(jsonb_build_array(attrelid,attnum,attname,atttypid,"
            "attnotnull,attgenerated) ORDER BY attrelid,attnum) FROM pg_attribute WHERE attrelid IN "
            "('storage_v2_compact_lexical_block'::REGCLASS,'storage_v2_derived_lexical_block'::REGCLASS) "
            'AND attnum>0 AND NOT attisdropped'),self.original_types)
        # The located producer permits65 input segments, not65 elements in
        # one persisted block. Keep the real constraints: legal65 input yields
        # original64+1 blocks; a65-vector persisted block is not admissible.
        group65,_,_ = self.block(['alpha beta']*65)
        self.assertEqual(self.sql("SELECT storage_v2_lexical_block_orders_valid(0,"
            "ARRAY(SELECT n::BIGINT FROM generate_series(0,64) n))"),'f')
        self.assertEqual(json.loads(self.sql('SELECT jsonb_agg(cardinality(fts_vectors) ORDER BY block_order) '
            f"FROM storage_v2_compact_lexical_block WHERE occurrence_id={group65['occurrence']}")),[64,1])
        self.gc_classification(row, segments)
        # Exact inverse after COMMIT is also bounded and audited. A deliberately
        # insufficient reservation fails after staging the original, then the
        # entire operation rolls back without changing the live encoded owner.
        owners_after_gc = self.owner_state(row)
        forward_audit = self.sql(f"SELECT to_jsonb(r) FROM {LEDGER} r WHERE occurrence_id={row['occurrence']}")
        inverse = self.call(row).replace(CONVERTER,'storage_v2_restore_encoded_lexical_block')
        small_inverse = inverse.replace(f",{row['stored']},30000)",',1,30000)')
        self.assert_sql_fails(self.operator(small_inverse,timeout='30s'),'restore reservation')
        self.assertEqual(self.sql(f"SELECT count(*) FROM {TABLE} WHERE occurrence_id={row['occurrence']}"),'1')
        self.assertEqual(self.sql(f"SELECT count(*) FROM storage_v2_compact_lexical_block WHERE occurrence_id={row['occurrence']}"),'0')
        inverse = inverse.replace(f",{row['stored']},30000)",',33554432,30000)')
        restored = json.loads(self.sql(self.operator(inverse,timeout='30s')))
        self.assertEqual(restored['disposition'],'RESTORED')
        self.assertEqual(json.loads(self.sql(self.operator(inverse,timeout='30s')))['disposition'],'REPLAY_RESTORED')
        self.assertEqual(self.sql(f"SELECT to_jsonb(r) FROM {LEDGER} r WHERE occurrence_id={row['occurrence']}"),forward_audit)
        self.assertEqual(self.logical(row),logical)
        self.assertEqual(self.native(row,'alpha beta'),native_before['alpha beta'])
        self.assertEqual(self.owner_state(row),owners_after_gc)

    def gc_classification(self, retained, segments):
        spec = importlib.util.spec_from_file_location('encoded172_gc',ROOT/'ops/storage-v2/native-gc.py')
        gc = importlib.util.module_from_spec(spec);sys.modules[spec.name]=gc;spec.loader.exec_module(gc)
        self.assertEqual(gc.LINKS[TABLE],[('occurrence_id','occurrence')])
        self.assertEqual(gc.SCOPED_IMMUTABILITY_GUARDS[TABLE],('storage_v2_guard_encoded_lexical_mutation',))
        self.assertNotIn(LEDGER,gc.LINKS)
        dead,_,_ = self.block(segments,orphan=True)
        self.assertEqual(self.apply(dead)['disposition'],'CONVERTED')
        external,_,_ = self.block(segments,orphan=True)
        self.assertEqual(self.apply(external)['disposition'],'CONVERTED')
        # Unknown external FK remains a root. No full cleanup rehearsal here.
        external_sql = ('CREATE TABLE fixture172_external(occurrence_id BIGINT REFERENCES occurrence(id));'
                        f"INSERT INTO fixture172_external VALUES({external['occurrence']});")
        graph = json.loads(self.sql('BEGIN;'+external_sql+gc.graph_sql([])+
            "SELECT jsonb_build_object('marked',(SELECT jsonb_agg(jsonb_build_array(kind,id)) FROM gc_mark),"
            "'encoded',(SELECT to_jsonb(s) FROM gc_dependents s WHERE name='"+TABLE+"'));ROLLBACK;"))
        marked = {tuple(item) for item in graph['marked']}
        self.assertIn(('occurrence',retained['occurrence']),marked)
        self.assertNotIn(('occurrence',dead['occurrence']),marked)
        self.assertIn(('occurrence',external['occurrence']),marked)
        self.assertEqual(graph['encoded']['total'],3)
        self.assertEqual(graph['encoded']['kept'],2)
        self.assertEqual(self.sql(f"SELECT count(*) FROM pg_constraint WHERE conrelid='{LEDGER}'::REGCLASS AND contype='f'"),'0')


if __name__ == '__main__':
    unittest.main()
