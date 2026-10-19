"""Copied whole-file body recall in one disposable native schema cohort.

The actual predecessor and candidate rank helpers see the same immutable
objects. No production data, reader request or full-source verification runs.
"""
from __future__ import annotations

import hashlib
import json

from eval.storage_v2.schema import test_linear_posting_run_classifier as previous

ROOT = previous.ROOT
MIGRATION = ROOT / 'migrations/180_storage_v2_copied_body_recall.sql'
SIGNATURES = ('storage_v2_source_segment_rank_candidates(bigint[],text)',
              'storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])')
NATIVE_VERIFIER = 'storage_v2_verify_derived_lexical_masks(bigint)'


class CopiedBodyRecallTests(previous.LinearPostingRunClassifierTests):
    test_native_run_predicate_and_one_realistic_byte_write = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            for number in (177, 178, 179):
                cls.sql(f"SET app.user_id='{cls.schema.ADMIN_ID}';" +
                        next(MIGRATION.parent.glob(f'{number}_*.sql')).read_text())
            cls.original180 = {}
            for signature in SIGNATURES:
                definition = cls.sql(f"SELECT pg_get_functiondef('{signature}'::REGPROCEDURE)")
                cls.original180[signature] = definition
                reference = definition.replace('FUNCTION public.storage_v2_source_segment_rank_candidates(',
                                               'FUNCTION public.fixture180_original_rank(', 1)
                if reference == definition:
                    raise AssertionError('actual rank helper declaration required')
                args = signature.split('(', 1)[1]
                cls.sql(reference + f';ALTER FUNCTION fixture180_original_rank({args} '
                        'OWNER TO mainrag_v2_frontier_owner;'
                        f'REVOKE ALL ON FUNCTION fixture180_original_rank({args} FROM PUBLIC;'
                        f'GRANT EXECUTE ON FUNCTION fixture180_original_rank({args} TO mainrag;')
            cls.before180 = cls.function_hashes()
            cls.authority180 = cls.authority_catalog()
            cls.contracts180 = json.loads(cls.contracts())
            cls.sql(f"SET app.user_id='{cls.schema.ADMIN_ID}';" + MIGRATION.read_text())
            cls.after180 = cls.function_hashes()
            cls.contracts_after180 = json.loads(cls.contracts())
        except BaseException:
            cls.base.tearDownClass.__func__(cls)
            raise

    def copied(self, body='prefix -314159 suffix', *, chunk='314159', context='',
               legacy_body=None):
        graph = self.seed(body)
        document = int(self.sql(self.admin('SELECT id FROM storage_v2_put_search_document('
            f"'mainrag.lexical-simple.v1','node',{graph['node']},{self.quote(body)},ARRAY[]::TEXT[])")))
        self.sql(self.admin(f"SELECT storage_v2_bind_search_document({graph['view']},0,{document},1.0)"))
        # The shared stage constructor prefixes item keys with /synthetic/.
        # Use its actual persisted identity rather than guessing that the item
        # key is itself the source path used by the legacy materializer.
        source_path = self.sql(f"SELECT source_path FROM occurrence WHERE id={graph['occurrence']}")
        self.assertEqual(source_path, '/synthetic/fresh170.txt')
        legacy = body if legacy_body is None else legacy_body
        chunk_id = int(self.sql(f"""WITH file AS (
 INSERT INTO files(source_id,path,hash,content,content_text,size_original,size_compressed,last_modified)
 VALUES({graph['source']},{self.quote(source_path)},sha256(convert_to({self.quote(legacy)},'UTF8')),
 decode('00','hex'),{self.quote(legacy)},{len(legacy.encode())},1,now()) RETURNING id
 ) INSERT INTO chunks(file_id,chunk_type,content_hash,content_compressed,content_text,
 context_prefix,start_line,end_line)
 SELECT id,'text',sha256(convert_to({self.quote(chunk)},'UTF8')),decode('00','hex'),
 {self.quote(chunk)},{self.quote(context)},1,1 FROM file RETURNING id"""))
        identity = json.loads(self.sql(f"""SELECT jsonb_build_object(
 'source',o.source_id=f.source_id,'path',o.source_path=f.path,
 'artifact',o.artifact_version_id=a.id,'node',d.node_id=a.content_root_node_id,
 'whole_hash',f.hash=decode(a.expected_content_hash,'hex'))
 FROM occurrence o JOIN artifact_version a ON a.id=o.artifact_version_id
 JOIN storage_v2_search_view_document b ON b.view_id=o.view_id AND b.ordinal=0
 JOIN storage_v2_search_document d ON d.id=b.document_id
 JOIN chunks c ON c.id={chunk_id} JOIN files f ON f.id=c.file_id
 WHERE o.id={graph['occurrence']}"""))
        self.assertEqual(identity, dict(source=True, path=True, artifact=True, node=True,
                                        whole_hash=legacy_body is None))
        self.assertEqual(self.sql(self.admin('SELECT storage_v2_materialize_legacy_chunk_ranks('
            f"{graph['occurrence']},{graph['artifact']})")), '1')
        # Real native writer creates a compact block containing the zero-order
        # generated marker. Repeated identical source slices have valid exact
        # character and UTF8 locations and need no synthetic vector injection.
        self.sql(self.admin('SELECT storage_v2_put_lexical_segments_located('
            f"{graph['occurrence']},{graph['artifact']},ARRAY(SELECT generate_series(0,63)::BIGINT),"
            f"array_fill({self.quote(body)}::TEXT,ARRAY[64]),array_fill(''::TEXT,ARRAY[64]),"
            "array_fill('text'::TEXT,ARRAY[64]),array_fill(1::BIGINT,ARRAY[64]),"
            'array_fill(1::BIGINT,ARRAY[64]))'))
        self.assertEqual(self.sql('SELECT EXISTS(SELECT 1 FROM storage_v2_lexical_block_all '
            f"WHERE occurrence_id={graph['occurrence']} AND block_order=0 AND 0=ANY(segment_orders))"), 't')
        return dict(graph, document=document, chunk=chunk_id)

    def ranks(self, row, query='314159', *, original=False, scoped=False, actor=None,
              large=False, source=None, prefix='', rollback=False):
        function = 'fixture180_original_rank' if original else 'storage_v2_source_segment_rank_candidates'
        ids = (f"array_fill({row['occurrence']}::BIGINT,ARRAY[1024])" if large
               else f"ARRAY[{row['occurrence']}]::BIGINT[]")
        scope = f",ARRAY[{row['source'] if source is None else source}]::BIGINT[]" if scoped else ''
        statement = ("SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY occurrence_id,score,segment_order),'[]'::JSONB) "
                     f'FROM {function}({ids},{self.quote(query)}{scope}) r')
        statement = prefix + self.actor(actor or self.schema.ADMIN_ID, statement)
        if rollback:
            statement = 'BEGIN;' + statement + ';ROLLBACK;'
        return json.loads(self.sql(statement))

    def test_generated_block_literal_body_recall_and_all_provenance_refusals(self):
        row = self.copied()
        observed = json.loads(self.sql(f"""SELECT jsonb_build_object(
 'document_fts',d.fts_simple@@websearch_to_tsquery('simple','314159'),
 'literal',storage_v2_literal_term_count(d.search_text,'314159'),
 'copied_fts',(SELECT bool_or(p.fts_vector@@websearch_to_tsquery('simple','314159'))
 FROM storage_v2_legacy_lexical_segment p WHERE p.occurrence_id={row['occurrence']}),
 'old_fallback',storage_v2_source_legacy_segment_matches({row['occurrence']},'314159'))
 FROM storage_v2_search_document d WHERE d.id={row['document']}"""))
        self.assertEqual(observed, dict(document_fts=False, literal=1, copied_fts=True, old_fallback=False))
        for scoped in (False, True):
            for large in (False, True):
                self.assertEqual(self.ranks(row, original=True, scoped=scoped, large=large), [])
                actual = self.ranks(row, scoped=scoped, large=large)
                self.assertEqual(len(actual), 1)
                self.assertEqual(actual[0]['occurrence_id'], row['occurrence'])
                self.assertEqual(actual[0]['segment_order'], row['chunk'])
                self.assertGreaterEqual(actual[0]['score'], 1000000)
            self.assertEqual(self.ranks(row, scoped=scoped, actor=self.schema.OTHER_ID), [])
            for query in ('314159 suffix', '314159 OR absent', '314159 -absent',
                          '"314159 suffix"', 'AND', 'OR', 'NOT'):
                self.assertEqual(self.ranks(row, query, scoped=scoped),
                                 self.ranks(row, query, original=True, scoped=scoped), query)
        self.assertEqual(self.ranks(row, scoped=True, source=row['source'] + 100000), [])
        context = self.copied('prefix ordinary suffix', chunk='ordinary', context='314159')
        fragment = self.copied(legacy_body='different whole file -314159')
        for invalid in (context, fragment):
            for scoped in (False, True):
                self.assertEqual(self.ranks(invalid, scoped=scoped), [])
                self.assertEqual(self.ranks(invalid, original=True, scoped=scoped), [])
        # Adversarial drift exists only within a rolled-back disposable fixture
        # transaction. FK enforcement stays enabled; no production row changes.
        foreign = self.copied('another canonical body')
        node_drift = ('ALTER TABLE storage_v2_search_view_document DISABLE TRIGGER USER;'
                      f"UPDATE storage_v2_search_view_document SET document_id={foreign['document']} "
                      f"WHERE view_id={row['view']} AND ordinal=0;")
        for scoped in (False, True):
            self.assertEqual(self.ranks(row, scoped=scoped, prefix=node_drift, rollback=True), [])
        for column, value in (('source_id', row['source'] + 100000),
                              ('artifact_version_id', foreign['artifact'])):
            self.assert_sql_fails('BEGIN;ALTER TABLE storage_v2_legacy_rank_binding DISABLE TRIGGER USER;'
                f'UPDATE storage_v2_legacy_rank_binding SET {column}={value} '
                f"WHERE occurrence_id={row['occurrence']};ROLLBACK;", 'foreign key constraint')

    def test_only_two_definitions_existing_hash_pins_and_seal_change(self):
        self.assertEqual(set(self.before180), set(self.after180))
        self.assertEqual({sig for sig in self.before180 if self.before180[sig] != self.after180[sig]},
                         set(SIGNATURES))
        self.assertEqual(self.authority180, self.authority_catalog())
        self.assertEqual(self.before180[NATIVE_VERIFIER], self.after180[NATIVE_VERIFIER])
        old, new = self.contracts180, self.contracts_after180
        for key in ('posting_relations', 'lexical_relations'):
            self.assertEqual(old[key], new[key])
        for key in ('posting_functions', 'lexical_functions'):
            self.assertEqual(len(old[key]), len(new[key]))
            for before, after in zip(old[key], new[key]):
                expected = dict(before)
                if before['signature'] in SIGNATURES:
                    expected['definition_sha256'] = '\\x' + self.after180[before['signature']]
                self.assertEqual(expected, after)
        self.assertEqual({k:v for k,v in old['lexical_seal'].items() if k != 'contract_sha256'},
                         {k:v for k,v in new['lexical_seal'].items() if k != 'contract_sha256'})
        for signature in SIGNATURES:
            candidate = self.sql(f"SELECT pg_get_functiondef('{signature}'::REGPROCEDURE)")
            marker = "        OR storage_v2_source_legacy_segment_matches(projection.occurrence_id,p_query)\n"
            old_tail = '    ), matching_legacy AS MATERIALIZED'
            start = candidate.index(marker) + len(marker)
            end = candidate.index(old_tail, start)
            self.assertEqual(candidate[:start] + candidate[end:], self.original180[signature])
        # Machine-readable handoff for exact downstream compatibility binding.
        print('COPIED_BODY_RECALL180 ' + json.dumps({
            'migration_sha256': hashlib.sha256(MIGRATION.read_bytes()).hexdigest(),
            'before': {sig:self.before180[sig] for sig in SIGNATURES},
            'after': {sig:self.after180[sig] for sig in SIGNATURES},
            'unchanged_native_verifier': {NATIVE_VERIFIER:self.after180[NATIVE_VERIFIER]},
            'unchanged_authority': True, 'unchanged_other_definitions': True,
        }, sort_keys=True), flush=True)


if __name__ == '__main__':
    import unittest
    unittest.main()
