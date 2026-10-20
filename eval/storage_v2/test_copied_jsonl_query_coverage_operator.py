"""Explicit v5 decoded proof joins the unchanged source/path/actor gates."""
from __future__ import annotations
import copy,importlib.util,os,unittest
from pathlib import Path
from eval.storage_v2 import test_release_candidate_operator as previous
ROOT=previous.ROOT
CANDIDATES=os.environ.get('MAINRAG_182_CANDIDATE_DIR')
OPERATOR=Path(CANDIDATES)/'schema182-release-candidate.py' if CANDIDATES else ROOT/'ops/storage-v2/release-candidate.py'
SPEC=importlib.util.spec_from_file_location('actual_evidence182_operator',OPERATOR)
MODULE=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(MODULE)

class CopiedJsonlQueryCoverageOperatorTests(unittest.TestCase):
    def test_explicit_v5_truthful_false_fts_and_all_original_scope_guards(self):
        seed,current,storage,proof,checkpoint=previous.ReleaseCandidateOperatorTests().coverage_fixture()
        seed['query']='314159';proof['query_sha256']=MODULE.sha256_text(seed['query'])
        proof['schema_version']='mainrag.storage-v2.query-coverage.v5'
        for hit in proof['candidate']:
            hit.update(reference_frequency=0,posting_frequency=0,fts_body_matches=False,segment_matches=False,
                       legacy_segment_matches=False,copied_jsonl_body_matches=True)
        frozen=copy.deepcopy(proof)
        self.assertTrue(MODULE.query_coverage_gates(seed,current,storage,proof,checkpoint)['passed'])
        self.assertEqual(proof,frozen)
        for field,value in (('copied_jsonl_body_matches',False),('copied_jsonl_body_matches','true'),
                            ('body_text_matches',False),('external_hit_id','foreign'),('path_sha256','f'*64)):
            bad=copy.deepcopy(proof);bad['candidate'][0][field]=value
            self.assertFalse(MODULE.query_coverage_gates(seed,current,storage,bad,checkpoint)['passed'],field)
        bad=copy.deepcopy(proof);del bad['candidate'][0]['copied_jsonl_body_matches']
        self.assertFalse(MODULE.query_coverage_gates(seed,current,storage,bad,checkpoint)['passed'])
        for version in ('mainrag.storage-v2.query-coverage.v4','mainrag.storage-v2.query-coverage.v6'):
            bad=copy.deepcopy(proof);bad['schema_version']=version
            self.assertFalse(MODULE.query_coverage_gates(seed,current,storage,bad,checkpoint)['passed'])
        for query in ('314159 ordinary','314159 OR ordinary','314159 -ordinary','"314159"','AND','OR','NOT'):
            bad=copy.deepcopy(proof);qseed=dict(seed,query=query);bad['query_sha256']=MODULE.sha256_text(query)
            self.assertFalse(MODULE.query_coverage_gates(qseed,current,storage,bad,checkpoint)['passed'],query)
        for field in ('source_id','generation_id','generation_seq','commit_sha'):
            bad=copy.deepcopy(proof);bad[field]=('b'*40 if field=='commit_sha' else checkpoint[field]+1)
            self.assertFalse(MODULE.query_coverage_gates(seed,current,storage,bad,checkpoint)['passed'],field)
        cp=dict(checkpoint,source_watermark_sha256='c'*64)
        source=dict(source_id=cp['source_id'],source_watermark_sha256=cp['source_watermark_sha256'],review_sha256='d'*64,
                    paths={r['path_sha256']:dict(status='same_bytes') for r in proof['legacy_paths']})
        self.assertTrue(MODULE.query_coverage_gates(seed,current,storage,proof,cp,source)['passed'])
        bad=copy.deepcopy(proof);bad['current'][0]['indexed_match']=False
        self.assertFalse(MODULE.query_coverage_gates(seed,current,storage,bad,cp,source)['passed'])
        bad=copy.deepcopy(proof);bad['legacy_paths'][0]['indexed_matches']=2
        self.assertFalse(MODULE.query_coverage_gates(seed,current,storage,bad,cp,source)['passed'])
        bad_storage=copy.deepcopy(storage);bad_storage['results']=bad_storage['results'][1:]
        self.assertFalse(MODULE.query_coverage_gates(seed,current,bad_storage,proof,cp,source)['passed'])

if __name__=='__main__':unittest.main()
