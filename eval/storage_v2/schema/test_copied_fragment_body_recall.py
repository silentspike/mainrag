"""Complete fragment identity and own-body copied recall in one native cohort."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

from eval.storage_v2.schema import test_copied_jsonl_query_evidence as previous

ROOT = previous.ROOT
MIGRATION = Path(os.environ.get('MAINRAG_183_CANDIDATE_DIR', ROOT / 'migrations')) / (
    '183_storage_v2_copied_fragment_body_recall.sql')
RANKS = previous.previous.SIGNATURES
EVIDENCE = previous.SIGNATURE
CHANGED = {*RANKS, EVIDENCE}
QUERY = '314159'


class CopiedFragmentBodyRecallTests(previous.CopiedJsonlQueryEvidenceTests):
    test_real_generated_decoded_body_and_provenance_refusals = None
    test_only_evidence_definition_and_existing_dependency_rows_change = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls.before183 = cls.function_hashes()
            cls.authority183 = cls.authority_catalog()
            cls.contracts183 = json.loads(cls.contracts())
            cls.sql(f"SET app.user_id='{cls.schema.ADMIN_ID}';" + MIGRATION.read_text())
            cls.after183 = cls.function_hashes()
            cls.contracts_after183 = json.loads(cls.contracts())
        except BaseException:
            cls.base.tearDownClass.__func__(cls)
            raise

    def fragmented(self, parts, *, offsets=None, legacy_body=None):
        """Stage real sibling artifacts; retain the whole legacy file separately."""
        token = uuid.uuid4().hex
        source = int(self.sql('INSERT INTO sources(id,name,type,path) '
            f"SELECT max(id)+1,'fragment183-{token}','fixture','synthetic-fragment183' "
            'FROM sources RETURNING id'))
        run = self.begin(source, token * 2, uuid.uuid4().hex * 2)
        path = '/synthetic/fragment183-' + token + '.jsonl'
        whole = ''.join(parts) if legacy_body is None else legacy_body
        chunk = int(self.sql(f"""WITH file AS (
 INSERT INTO files(source_id,path,hash,content,content_text,size_original,size_compressed,last_modified)
 VALUES({source},{self.quote(path)},sha256(convert_to({self.quote(whole)},'UTF8')),
 decode('00','hex'),{self.quote(whole)},{len(whole.encode())},1,now()) RETURNING id
 ) INSERT INTO chunks(file_id,chunk_type,content_hash,content_compressed,content_text,
 context_prefix,start_line,end_line)
 SELECT id,'text',sha256(convert_to('{QUERY}','UTF8')),decode('00','hex'),
 '{QUERY}','',1,1 FROM file RETURNING id"""))
        rows = []
        position = 0
        for index, body in enumerate(parts):
            start = position if offsets is None else offsets[index]
            end = start + len(body.encode())
            node, view, digest = self.make_projection(body)
            witness = dict(path=path, byte_start=start, byte_end=end, sha256=digest)
            locator = dict(byte_start=start, byte_end=end, fragmented=True,
                           line_start=None, line_end=None, line_scope='unknown', level=0,
                           language='text')
            item = f'fragment183-{token}-{index}'
            row = json.loads(self.sql(self.admin(f"""SELECT jsonb_build_object(
 'occurrence',staged.occurrence_id,'artifact',staged.artifact_version_id,
 'generation',(SELECT generation_id FROM storage_v2_ingest_run WHERE id={run}))
 FROM storage_v2_stage_shadow_item({run},{self.quote(item)},'document',
 'release-candidate-item',{self.quote(json.dumps(witness))}::JSONB,
 'fixture-adapter-v1',{node},NULL,'{digest}',{len(body.encode())},decode('{digest}','hex'),
 'fixture-analysis-v1',{view},{self.quote(path)},
 {self.quote(json.dumps(locator))}::JSONB) staged""")))
            self.complete_analysis(digest)
            text = body
            document = int(self.sql(self.admin('SELECT id FROM storage_v2_put_search_document('
                f"'mainrag.lexical-simple.v1','node',{node},{self.quote(text)},ARRAY[]::TEXT[])")))
            self.sql(self.admin(f'SELECT storage_v2_bind_search_document({view},0,{document},1.0)'))
            self.assertEqual(self.sql(self.admin('SELECT storage_v2_materialize_legacy_chunk_ranks('
                f"{row['occurrence']},{row['artifact']})")), '1')
            # The supported writer creates a real generated zero-order marker.
            self.sql(self.admin('SELECT storage_v2_put_lexical_segments_located('
                f"{row['occurrence']},{row['artifact']},ARRAY(SELECT generate_series(0,63)::BIGINT),"
                f"array_fill({self.quote(text)}::TEXT,ARRAY[64]),array_fill(''::TEXT,ARRAY[64]),"
                "array_fill('text'::TEXT,ARRAY[64]),array_fill(1::BIGINT,ARRAY[64]),"
                'array_fill(1::BIGINT,ARRAY[64]))'))
            rows.append(dict(row, source=source, run=run, node=node, view=view,
                             document=document, chunk=chunk, digest=digest))
            position = end
        self.commit(run, len(rows))
        self.sql(self.admin(f"SELECT storage_v2_verify_generation({rows[0]['generation']},'{'e3' * 32}')"))
        return rows

    def fragment_ranks(self, rows, *, scoped=False, selected=None, actor=None, source=None,
                       prefix='', rollback=False):
        chosen = rows if selected is None else selected
        ids = ','.join(str(row['occurrence']) for row in chosen)
        extra = (f",ARRAY[{rows[0]['source'] if source is None else source}]::BIGINT[]"
                 if scoped else '')
        call = ('SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY occurrence_id),\'[]\'::JSONB) '
                f"FROM storage_v2_source_segment_rank_candidates(ARRAY[{ids}]::BIGINT[],'{QUERY}'{extra}) r")
        sql = prefix + self.actor(actor or self.schema.ADMIN_ID, call)
        if rollback:
            sql = 'BEGIN;' + sql + ';ROLLBACK;'
        return json.loads(self.sql(sql))

    def fragment_evidence(self, rows, hit, *, actor=None, source=None, prefix='', rollback=False):
        # Evidence obtains siblings from the named generation, independently of
        # the single returned hit. The request does not grant arbitrary scope.
        call = self.evidence_sql(hit, source=source)
        sql = prefix + self.actor(actor or self.schema.ADMIN_ID, call)
        if rollback:
            sql = 'BEGIN;' + sql + ';ROLLBACK;'
        return json.loads(self.sql(sql))

    def test_complete_fragments_own_body_and_provenance_refusals(self):
        cases = (
            ['ordinary prefix\n', 'tail -314159 suffix'],
            [json.dumps({'text': 'ordinary'}) + '\n',
             json.dumps({'text': 'before\n314159: after'})],
        )
        for parts in cases:
            rows = self.fragmented(parts)
            sibling, hit = rows
            observed = json.loads(self.sql(f"""SELECT jsonb_agg(jsonb_build_object(
 'occurrence',o.id,'whole_hash',p.legacy_file_hash=decode(a.expected_content_hash,'hex'),
 'canonical_fts',d.fts_simple@@websearch_to_tsquery('simple','{QUERY}'),
 'generated_fallback',storage_v2_source_legacy_segment_matches(o.id,'{QUERY}')) ORDER BY o.id)
 FROM occurrence o JOIN artifact_version a ON a.id=o.artifact_version_id
 JOIN storage_v2_search_view_document b ON b.view_id=o.view_id AND b.ordinal=0
 JOIN storage_v2_search_document d ON d.id=b.document_id
 JOIN storage_v2_legacy_lexical_segment p ON p.occurrence_id=o.id
 WHERE o.id=ANY(ARRAY[{sibling['occurrence']},{hit['occurrence']}]::BIGINT[])"""))
            self.assertEqual(len(observed), 2)
            self.assertTrue(all(not row['whole_hash'] and not row['canonical_fts']
                                and not row['generated_fallback'] for row in observed))
            for scoped in (False, True):
                result = self.fragment_ranks(rows, scoped=scoped)
                self.assertEqual([row['occurrence_id'] for row in result], [hit['occurrence']])
                self.assertGreaterEqual(result[0]['score'], 1000000)
                self.assertEqual(result[0]['segment_order'], hit['chunk'])
                self.assertEqual(self.fragment_ranks(rows, scoped=scoped, selected=[hit]), [])
                self.assertEqual(self.fragment_ranks(rows, scoped=scoped,
                                                     actor=self.schema.OTHER_ID), [])
            self.assertEqual(self.fragment_ranks(rows, scoped=True, source=hit['source'] + 100000), [])
            evidence = self.fragment_evidence(rows, hit)
            self.assertEqual(evidence['schema_version'], 'mainrag.storage-v2.query-coverage.v5')
            self.assertTrue(evidence['candidate'][0]['body_text_matches'])
            # The new provenance route must not relabel old independent flags.
            self.assertFalse(evidence['candidate'][0]['fts_body_matches'])
            self.assertFalse(evidence['candidate'][0]['segment_matches'])
            if parts is cases[1]:
                self.assertTrue(evidence['candidate'][0]['copied_jsonl_body_matches'])
            self.assert_sql_fails(self.actor(self.schema.OTHER_ID, self.evidence_sql(hit)),
                                  'administrator source authority')
            self.assert_sql_fails(self.admin(self.evidence_sql(sibling)), 'query hit lacks consistent')

        # Every refusal is a real staged source graph; no vector or hash result
        # is substituted for the original SQL predicates.
        body = cases[1]
        bad_sets = [
            self.fragmented(body, legacy_body='different complete file'),
            self.fragmented(body, offsets=[0, len(body[0].encode()) + 1]),
            self.fragmented(body, offsets=[0, len(body[0].encode()) - 1]),
        ]
        for rows in bad_sets:
            for scoped in (False, True):
                self.assertEqual(self.fragment_ranks(rows, scoped=scoped), [])
            self.assert_sql_fails(self.admin(self.evidence_sql(rows[1])), 'query hit lacks consistent')
        # First construct the immutable document through its original valid
        # constructor. Contradictory text for that same node/profile must fail
        # there, rather than stand in for a reader's digest-drift refusal.
        digest_rows = self.fragmented(body)
        digest_drift = ('ALTER TABLE storage_v2_search_document DISABLE TRIGGER USER;'
                        "UPDATE storage_v2_search_document SET search_text=search_text||' ' "
                        f"WHERE id={digest_rows[1]['document']};")
        for scoped in (False, True):
            self.assertEqual(self.fragment_ranks(digest_rows, scoped=scoped,
                                                prefix=digest_drift, rollback=True), [])
        self.assert_sql_fails('BEGIN;' + digest_drift +
                              self.admin(self.evidence_sql(digest_rows[1])) + ';ROLLBACK;',
                              'query hit lacks consistent')
        rows = self.fragmented(body)
        foreign = self.fragmented([json.dumps({'text': 'foreign'}) + '\n',
                                   json.dumps({'text': 'ordinary'})])
        drift = ('ALTER TABLE storage_v2_search_view_document DISABLE TRIGGER USER;'
                 f"UPDATE storage_v2_search_view_document SET document_id={foreign[1]['document']} "
                 f"WHERE view_id={rows[1]['view']} AND ordinal=0;")
        for scoped in (False, True):
            self.assertEqual(self.fragment_ranks(rows, scoped=scoped, prefix=drift, rollback=True), [])
        self.assert_sql_fails('BEGIN;' + drift + self.admin(self.evidence_sql(rows[1])) + ';ROLLBACK;',
                              'outside the supported named-generation source scope')

    def test_actual_bounded_metadata_selection_preserves_small_file_capacity(self):
        # Execute the actual installed CTE on metadata rows, without allocating
        # any of the represented body bytes or invoking another reader run.
        start = 'fragment_bounded AS MATERIALIZED ('
        end = '), fragment_bytes AS MATERIALIZED ('
        mib = 1024 ** 2
        cases = (
            ([('oversized', 8 * mib + 1, True), ('small', mib, True)], ['small']),
            # Four individually bounded files exceed 8 MiB in aggregate but
            # fit the existing statement window; none may be starved.
            ([(f'file-{n}', size, True) for n, size in enumerate(
                (2_000_000, 2_000_000, 2_000_000, 3_695_081))],
             [f'file-{n}' for n in range(4)]),
            ([(f'edge-{n}', 8 * mib, True) for n in range(4)],
             [f'edge-{n}' for n in range(4)]),
            ([(f'excess-{n}', 8 * mib, True) for n in range(5)], []),
            ([('invalid', 32 * mib, False), ('valid', mib, True)], ['valid']),
        )
        for signature in sorted(CHANGED):
            definition = self.sql(f"SELECT pg_get_functiondef('{signature}'::REGPROCEDURE)")
            self.assertEqual(definition.count(start), 1)
            tail = definition.split(start, 1)[1]
            self.assertEqual(tail.count(end), 1)
            bounded = start + tail.split(end, 1)[0] + ')'
            for layout, expected in cases:
                values = ','.join(f'({self.quote(name)},2::BIGINT,{size}::NUMERIC,'
                                  f'{str(valid).lower()})' for name, size, valid in layout)
                result = json.loads(self.sql(
                    'WITH fragment_layout(source_path,pieces,total_bytes,valid) AS (VALUES ' +
                    values + '),' + bounded +
                    " SELECT coalesce(jsonb_agg(source_path ORDER BY source_path),'[]'::JSONB) "
                    'FROM fragment_bounded'))
                self.assertEqual(result, sorted(expected), (signature, layout))

    def test_only_three_definitions_existing_contracts_and_authority_change(self):
        self.assertEqual(set(self.before183), set(self.after183))
        self.assertEqual({s for s in self.before183 if self.before183[s] != self.after183[s]}, CHANGED)
        self.assertEqual(self.authority183, self.authority_catalog())
        self.assertEqual(self.before183[previous.previous.NATIVE_VERIFIER],
                         self.after183[previous.previous.NATIVE_VERIFIER])
        old, new = self.contracts183, self.contracts_after183
        for key in ('posting_relations', 'lexical_relations'):
            self.assertEqual(old[key], new[key])
        for key in ('posting_functions', 'lexical_functions'):
            self.assertEqual(len(old[key]), len(new[key]))
            for before, after in zip(old[key], new[key]):
                expected = dict(before)
                if before['signature'] in CHANGED:
                    expected['definition_sha256'] = '\\x' + self.after183[before['signature']]
                self.assertEqual(expected, after)
        self.assertEqual({k: v for k, v in old['lexical_seal'].items() if k != 'contract_sha256'},
                         {k: v for k, v in new['lexical_seal'].items() if k != 'contract_sha256'})
        print('COPIED_FRAGMENT_BODY_RECALL183 ' + json.dumps(dict(
            migration_sha256=hashlib.sha256(MIGRATION.read_bytes()).hexdigest(),
            before={s: self.before183[s] for s in sorted(CHANGED)},
            after={s: self.after183[s] for s in sorted(CHANGED)},
            unchanged_native_verifier={previous.previous.NATIVE_VERIFIER:
                                       self.after183[previous.previous.NATIVE_VERIFIER]},
            unchanged_authorities=True, unchanged_other_definitions=True), sort_keys=True), flush=True)


if __name__ == '__main__':
    import unittest
    unittest.main()
