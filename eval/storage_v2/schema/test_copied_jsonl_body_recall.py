"""Strict JSONL string body recall against actual generated lexical blocks."""
from __future__ import annotations

import hashlib
import json

from eval.storage_v2.schema import test_copied_body_recall as previous

MIGRATION = previous.ROOT / 'migrations/181_storage_v2_copied_jsonl_body_recall.sql'
SIGNATURES = previous.SIGNATURES
NATIVE_VERIFIER = previous.NATIVE_VERIFIER


class CopiedJsonlBodyRecallTests(previous.CopiedBodyRecallTests):
    test_generated_block_literal_body_recall_and_all_provenance_refusals = None
    test_only_two_definitions_existing_hash_pins_and_seal_change = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls.original181 = {}
            for signature in SIGNATURES:
                definition = cls.sql(f"SELECT pg_get_functiondef('{signature}'::REGPROCEDURE)")
                cls.original181[signature] = definition
                declaration = 'FUNCTION public.storage_v2_source_segment_rank_candidates('
                reference = definition.replace(declaration,
                    'FUNCTION public.fixture181_original_rank(', 1)
                if reference == definition:
                    raise AssertionError('actual migration180 rank declaration required')
                args = signature.split('(', 1)[1]
                cls.sql(reference + f';ALTER FUNCTION fixture181_original_rank({args} '
                    'OWNER TO mainrag_v2_frontier_owner;'
                    f'REVOKE ALL ON FUNCTION fixture181_original_rank({args} FROM PUBLIC;'
                    f'GRANT EXECUTE ON FUNCTION fixture181_original_rank({args} TO mainrag;')
            cls.before181 = cls.function_hashes()
            cls.authority181 = cls.authority_catalog()
            cls.contracts181 = json.loads(cls.contracts())
            cls.sql(f"SET app.user_id='{cls.schema.ADMIN_ID}';" + MIGRATION.read_text())
            cls.after181 = cls.function_hashes()
            cls.contracts_after181 = json.loads(cls.contracts())
        except BaseException:
            cls.base.tearDownClass.__func__(cls)
            raise

    def ranks180(self, row, query='314159', *, scoped=False):
        scope = f",ARRAY[{row['source']}]::BIGINT[]" if scoped else ''
        return json.loads(self.sql(self.admin(
            "SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY occurrence_id,score,segment_order),'[]'::JSONB) "
            f"FROM fixture181_original_rank(ARRAY[{row['occurrence']}]::BIGINT[],"
            f"{self.quote(query)}{scope}) r")))

    def test_decoded_body_strict_boundaries_and_provenance(self):
        # Complete canonical JSONL has escaped delimiters. The copied extractor
        # decoded them, whereas the raw canonical literal oracle sees n314159.
        body = '\n \t\r\n'.join(json.dumps(value) for value in (
            {'text': 'before\n314159: after'}, {'messages': [{'text': 'ordinary'}]}))
        row = self.copied(body)
        observed = json.loads(self.sql(f"""SELECT jsonb_build_object(
 'raw_literal',storage_v2_literal_term_count(d.search_text,'314159'),
 'canonical_fts',d.fts_simple@@websearch_to_tsquery('simple','314159'),
 'copied_fts',(SELECT bool_or(p.fts_vector@@websearch_to_tsquery('simple','314159'))
 FROM storage_v2_legacy_lexical_segment p WHERE p.occurrence_id={row['occurrence']}),
 'generated_fallback',storage_v2_source_legacy_segment_matches({row['occurrence']},'314159'))
 FROM storage_v2_search_document d WHERE d.id={row['document']}"""))
        self.assertEqual(observed, dict(raw_literal=0, canonical_fts=False,
                                       copied_fts=True, generated_fallback=False))
        for scoped in (False, True):
            for large in (False, True):
                actual = self.ranks(row, scoped=scoped, large=large)
                self.assertEqual(len(actual), 1)
                self.assertEqual(actual[0]['occurrence_id'], row['occurrence'])
                self.assertEqual(actual[0]['segment_order'], row['chunk'])
                self.assertGreaterEqual(actual[0]['score'], 1000000)
            self.assertEqual(self.ranks180(row, scoped=scoped), [])
            self.assertEqual(self.ranks(row, scoped=scoped, actor=self.schema.OTHER_ID), [])
            for query in ('314159 ordinary', '314159 OR absent', '314159 -absent',
                          '"314159 ordinary"', 'AND', 'OR', 'NOT'):
                self.assertEqual(self.ranks(row, query, scoped=scoped),
                                 self.ranks180(row, query, scoped=scoped), query)
        self.assertEqual(self.ranks(row, scoped=True, source=row['source'] + 100000), [])
        unchanged = self.copied()
        for scoped in (False, True):
            self.assertEqual(self.ranks(unchanged, scoped=scoped),
                             self.ranks180(unchanged, scoped=scoped))
        tab = self.copied(json.dumps([{'text': 'before\t314159: after'}]))
        carriage = self.copied(json.dumps({'nested': {'text': 'before\r314159: after'}}))
        escaped = self.copied(json.dumps({'text': 'before\\n314159: after'}))
        invalid = self.copied(body + '\ninvalid JSON record')
        scalar = self.copied(body + '\n"scalar record"')
        context = self.copied(json.dumps({'text': 'ordinary'}), chunk='ordinary', context='314159')
        fragment = self.copied(body, legacy_body='different canonical file')
        glued = self.copied(json.dumps({'text': 'before\n314159suffix'}))
        split = self.copied(json.dumps({'left': '314', 'right': '159'}))
        for valid in (tab, carriage):
            for scoped in (False, True):
                self.assertEqual(len(self.ranks(valid, scoped=scoped)), 1)
                self.assertEqual(self.ranks180(valid, scoped=scoped), [])
        for refused in (escaped, invalid, scalar, context, fragment, glued, split):
            for scoped in (False, True):
                self.assertEqual(self.ranks(refused, scoped=scoped), [], refused['occurrence'])
                self.assertEqual(self.ranks180(refused, scoped=scoped), [])
        # Exact identity joins remain active under the original authorities.
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

    def test_only_two_definitions_contracts_and_native_verifier_preserved(self):
        self.assertEqual(set(self.before181), set(self.after181))
        self.assertEqual({sig for sig in self.before181 if self.before181[sig] != self.after181[sig]},
                         set(SIGNATURES))
        self.assertEqual(self.authority181, self.authority_catalog())
        self.assertEqual(self.before181[NATIVE_VERIFIER], self.after181[NATIVE_VERIFIER])
        old, new = self.contracts181, self.contracts_after181
        for key in ('posting_relations', 'lexical_relations'):
            self.assertEqual(old[key], new[key])
        for key in ('posting_functions', 'lexical_functions'):
            self.assertEqual(len(old[key]), len(new[key]))
            for before, after in zip(old[key], new[key]):
                expected = dict(before)
                if before['signature'] in SIGNATURES:
                    expected['definition_sha256'] = '\\x' + self.after181[before['signature']]
                self.assertEqual(expected, after)
        self.assertEqual({k:v for k,v in old['lexical_seal'].items() if k != 'contract_sha256'},
                         {k:v for k,v in new['lexical_seal'].items() if k != 'contract_sha256'})
        sql = MIGRATION.read_text()
        marker = sql.split(' marker TEXT:=$old$', 1)[1].split('$old$;', 1)[0]
        replacement = sql.split(' replacement TEXT:=$new$', 1)[1].split('$new$;', 1)[0]
        for signature in SIGNATURES:
            candidate = self.sql(f"SELECT pg_get_functiondef('{signature}'::REGPROCEDURE)")
            self.assertEqual(candidate, self.original181[signature].replace(marker, replacement))
        print('COPIED_JSONL_BODY_RECALL181 ' + json.dumps({
            'migration_sha256': hashlib.sha256(MIGRATION.read_bytes()).hexdigest(),
            'before': {sig:self.before181[sig] for sig in SIGNATURES},
            'after': {sig:self.after181[sig] for sig in SIGNATURES},
            'unchanged_native_verifier': {NATIVE_VERIFIER:self.after181[NATIVE_VERIFIER]},
            'unchanged_authority': True, 'unchanged_other_definitions': True,
        }, sort_keys=True), flush=True)


if __name__ == '__main__':
    import unittest
    unittest.main()
