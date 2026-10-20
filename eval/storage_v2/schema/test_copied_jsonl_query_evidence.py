"""Independent decoded JSONL evidence, with actual generated lexical blocks."""
from __future__ import annotations
import hashlib,json,os
from pathlib import Path
from eval.storage_v2.schema import test_copied_jsonl_body_recall as previous

ROOT=previous.previous.ROOT
CANDIDATES=Path(os.environ.get('MAINRAG_182_CANDIDATE_DIR',ROOT/'migrations'))
MIGRATION=CANDIDATES/'182_storage_v2_copied_jsonl_query_evidence.sql'
SIGNATURE='storage_v2_candidate_query_evidence(bigint,bigint,text,text,bigint[],bigint[])'
COMMIT='a'*40

class CopiedJsonlQueryEvidenceTests(previous.CopiedJsonlBodyRecallTests):
    test_decoded_body_strict_boundaries_and_provenance=None
    test_only_two_definitions_contracts_and_native_verifier_preserved=None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls.original182=cls.sql(f"SELECT pg_get_functiondef('{SIGNATURE}'::REGPROCEDURE)")
            cls.before182=cls.function_hashes();cls.authority182=cls.authority_catalog()
            cls.contracts182=json.loads(cls.contracts())
            cls.sql(f"SET app.user_id='{cls.schema.ADMIN_ID}';"+MIGRATION.read_text())
            cls.after182=cls.function_hashes();cls.contracts_after182=json.loads(cls.contracts())
        except BaseException:
            cls.base.tearDownClass.__func__(cls);raise

    def begin(self,*args,**kwargs):
        kwargs.setdefault('commit_sha',COMMIT)
        return super().begin(*args,**kwargs)

    def qualify(self,row):
        self.commit(row['run'],1)
        self.sql(self.admin(f"SELECT storage_v2_verify_generation({row['generation']},'{'e3'*32}')"))
        return row

    def evidence_sql(self,row,query='314159',source=None):
        return (f"SELECT storage_v2_candidate_query_evidence({row['source'] if source is None else source},"
                f"{row['generation']},'{COMMIT}',{self.quote(query)},"
                f"ARRAY[{row['occurrence']}]::BIGINT[],ARRAY[{row['chunk']}]::BIGINT[])")

    def test_real_generated_decoded_body_and_provenance_refusals(self):
        body='\n'.join(json.dumps(v) for v in ({'text':'before\n314159: after'}, {'nested':[{'text':'ordinary'}]}))
        row=self.qualify(self.copied(body))
        self.assertEqual(len(self.ranks(row)),1)
        proof=json.loads(self.sql(self.admin(self.evidence_sql(row))))
        self.assertEqual(proof['schema_version'],'mainrag.storage-v2.query-coverage.v5')
        hit=proof['candidate'][0]
        self.assertEqual({k:hit[k] for k in ('reference_frequency','posting_frequency','fts_body_matches','segment_matches','legacy_segment_matches')},
                         dict(reference_frequency=0,posting_frequency=0,fts_body_matches=False,segment_matches=False,legacy_segment_matches=False))
        self.assertIs(hit['body_text_matches'],True);self.assertIs(hit['copied_jsonl_body_matches'],True)
        self.assertEqual(hit['occurrence_id'],row['occurrence'])
        # The original SET-GUC function frame must restore either caller value.
        for mode in ('on','off'):
            call=self.evidence_sql(row).removeprefix('SELECT ')
            self.sql(self.admin(f"SET row_security={mode};DO $restore$ DECLARE before TEXT:=current_setting('row_security');BEGIN "
                f"PERFORM {call};IF current_setting('row_security') IS DISTINCT FROM before THEN "
                "RAISE EXCEPTION 'successful evidence leaked row_security';END IF;END $restore$;"))

        self.assert_sql_fails(self.actor(self.schema.OTHER_ID,self.evidence_sql(row)),'administrator source authority')
        self.assert_sql_fails(self.admin(self.evidence_sql(row,source=row['source']+100000)),'verified candidate identity')
        self.assert_sql_fails(self.admin(self.evidence_sql(row,query='314159 OR absent')),'bounded simple query')
        # Real immutable complete-body fixtures; copied vectors are intentionally
        # present but cannot replace the independent decoded body predicate.
        negatives=[
            self.copied(json.dumps({'text':'before\\n314159: after'})),
            self.copied(body+'\ninvalid JSON record'),
            self.copied(body+'\n"scalar record"'),
            self.copied(json.dumps({'text':'ordinary'}),chunk='ordinary',context='314159'),
            self.copied(body,legacy_body='foreign whole file'),
            self.copied(json.dumps({'text':'before\n314159suffix'})),
            self.copied(json.dumps({'left':'314','right':'159'})),
        ]
        for invalid in negatives:
            self.qualify(invalid)
            self.assert_sql_fails(self.admin(self.evidence_sql(invalid)),'query hit lacks consistent')
        for mode in ('on','off'):
            call=self.evidence_sql(negatives[0]).removeprefix('SELECT ')
            self.sql(self.admin(f"SET row_security={mode};DO $restore$ DECLARE before TEXT:=current_setting('row_security');caught BOOLEAN:=FALSE;BEGIN "
                f"BEGIN PERFORM {call};EXCEPTION WHEN raise_exception THEN caught:=TRUE;END;"
                "IF NOT caught OR current_setting('row_security') IS DISTINCT FROM before THEN "
                "RAISE EXCEPTION 'failed evidence leaked row_security';END IF;END $restore$;"))
        foreign=self.qualify(self.copied('foreign body'))
        drift=('BEGIN;ALTER TABLE storage_v2_search_view_document DISABLE TRIGGER USER;'
               f"UPDATE storage_v2_search_view_document SET document_id={foreign['document']} WHERE view_id={row['view']} AND ordinal=0;")
        self.assert_sql_fails(drift+self.admin(self.evidence_sql(row))+';ROLLBACK;','outside the supported named-generation source scope')
        for col,value in (('source_id',row['source']+100000),('artifact_version_id',foreign['artifact'])):
            self.assert_sql_fails('BEGIN;ALTER TABLE storage_v2_legacy_rank_binding DISABLE TRIGGER USER;'
                f'UPDATE storage_v2_legacy_rank_binding SET {col}={value} '
                f"WHERE occurrence_id={row['occurrence']};ROLLBACK;",'foreign key constraint')

    def test_only_evidence_definition_and_existing_dependency_rows_change(self):
        self.assertEqual(set(self.before182),set(self.after182))
        self.assertEqual({k for k in self.before182 if self.before182[k]!=self.after182[k]},{SIGNATURE})
        self.assertEqual(self.authority182,self.authority_catalog())
        self.assertEqual(self.before182[previous.NATIVE_VERIFIER],self.after182[previous.NATIVE_VERIFIER])
        for signature in previous.SIGNATURES:
            self.assertEqual(self.before182[signature],self.after182[signature])
        for key in ('posting_relations','lexical_relations'):
            self.assertEqual(self.contracts182[key],self.contracts_after182[key])
        for key in ('posting_functions','lexical_functions'):
            self.assertEqual(len(self.contracts182[key]),len(self.contracts_after182[key]))
            for old,new in zip(self.contracts182[key],self.contracts_after182[key]):
                expected=dict(old)
                if old['signature']==SIGNATURE:expected['definition_sha256']='\\x'+self.after182[SIGNATURE]
                self.assertEqual(expected,new)
        oldseal=self.contracts182['lexical_seal'];newseal=self.contracts_after182['lexical_seal']
        pinned=any(x['signature']==SIGNATURE for x in self.contracts182['lexical_functions'])
        if not pinned:self.assertEqual(oldseal,newseal)
        else:self.assertEqual({k:v for k,v in oldseal.items() if k!='contract_sha256'},
                              {k:v for k,v in newseal.items() if k!='contract_sha256'})
        print('COPIED_JSONL_QUERY_EVIDENCE182 '+json.dumps(dict(
            migration_sha256=hashlib.sha256(MIGRATION.read_bytes()).hexdigest(),
            before={SIGNATURE:self.before182[SIGNATURE]},after={SIGNATURE:self.after182[SIGNATURE]},
            unchanged_rank={s:self.after182[s] for s in previous.SIGNATURES},
            unchanged_native_verifier={previous.NATIVE_VERIFIER:self.after182[previous.NATIVE_VERIFIER]},
            unchanged_authorities=True,unchanged_other_definitions=True),sort_keys=True),flush=True)

if __name__=='__main__':
    import unittest
    unittest.main()
