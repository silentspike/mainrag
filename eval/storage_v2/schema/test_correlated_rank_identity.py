"""Full reader equivalence and exhaustive secondary-prefix boundary checks."""
import json
from unittest.mock import patch
from eval.storage_v2.schema import test_bound_native_rank_work as parent

MIGRATION = parent.schema.ROOT / "migrations/107_storage_v2_correlated_rank_identity_and_scoped_postings.sql"

class CorrelatedRankIdentityTests(parent.BoundNativeRankTests):
    @classmethod
    def setUpClass(cls):
        parent.BoundNativeRankTests.setUpClass.__func__(cls)
        cls.file(parent.schema.ROOT / "migrations/106_storage_v2_bound_native_ranks_and_fragment_groups.sql")

    def test_complete_results_authorization_ties_and_replay(self):
        with patch.object(parent, "MIGRATION", MIGRATION):
            super().test_complete_results_authorization_ties_and_replay()

    def test_prefix_boundary_matches_exhaustive_full_identity_order(self):
        draft = MIGRATION.read_text()
        prefix_ctes = "    lexical_keyed AS" + draft.split("    lexical_keyed AS", 1)[1].split("    identified AS", 1)[0]
        statements = []
        for scenario in [0,1,2,3,4]:
            for limit in [1,3,10,1000]:
                prefix = ("WITH bounded AS MATERIALIZED (SELECT id,source_id,final_score,candidate_sort_key\n"
                          f" FROM prefix_fixture WHERE scenario={scenario}),\n"
                          "bounded_native_ranks AS MATERIALIZED (SELECT NULL::bigint occurrence_id,NULL::bigint segment_order WHERE false),\n"
                          "fragmented_match AS MATERIALIZED (SELECT id,source_id,source_path,final_score FROM prefix_fixture\n"
                          f" WHERE scenario={scenario} AND role='artifact' AND fragmented),\n")
                statements.append(prefix + EXHAUSTIVE.format(scenario=scenario,limit=limit)
                    + prefix_ctes.replace("LIMIT p_limit", f"LIMIT {limit}")
                    + SUFFIX.format(scenario=scenario,limit=limit))
        observations = [json.loads(x) for x in self.sql(SETUP + "\n" + "\n".join(statements)).splitlines()]
        self.assertEqual(len(observations),20)
        self.assertTrue(all(x["equal"] for x in observations), observations)
        broad = next(x for x in observations if x["scenario"]==0 and x["limit"]==10)
        null_tie = next(x for x in observations if x["scenario"]==2 and x["limit"]==1)
        self.assertEqual((broad["reference_hashes"],broad["candidate_hashes"]),(36000,10))
        self.assertEqual((null_tie["reference_hashes"],null_tie["candidate_hashes"]),(3000,3000))

SETUP = "\nCREATE TABLE prefix_fixture(scenario int,id bigint,source_id bigint,source_path text,\n role text,fragmented boolean,final_score double precision,candidate_sort_key bigint);\n-- A broad copied-score tie with distinct secondary keys: only K hashes needed.\nINSERT INTO prefix_fixture SELECT 0,i,1,'path-'||i,'artifact',false,1000000.1,i\n FROM generate_series(1,36000) i;\n-- Repeated groups, ties on both prefix keys, NULLs, mixed scores, and flagged\n-- logical records which must remain separate even when sharing a source path.\nINSERT INTO prefix_fixture\n SELECT 1,i,i%3+1,'path-'||(i%317),CASE WHEN i%9=0 THEN 'conversation' ELSE 'artifact' END,\n i%4<>0,(i%7-3)::double precision,\n CASE WHEN i%11=0 THEN NULL::bigint ELSE i%13 END FROM generate_series(1,12000) i;\n-- With equal scores and NULL secondary keys, every third-key contender remains.\nINSERT INTO prefix_fixture SELECT 2,i,1,'shared','conversation',true,0,NULL::bigint\n FROM generate_series(1,3000) i;\n-- Scenario 3 deliberately has no rows.\n-- Fragment groups with all NULL lexical keys and differing final scores.\nINSERT INTO prefix_fixture SELECT 4,i,i%2+1,'path-'||(i%41),'artifact',true,\n (i%11-5)::double precision,NULL::bigint FROM generate_series(1,4000) i;\nANALYZE;\n"

EXHAUSTIVE = "    reference_identified AS MATERIALIZED (\n        SELECT f.*,f.candidate_sort_key AS lexical_sort_key,md5('hit-'||f.id) AS external_hit_id\n          FROM prefix_fixture f WHERE scenario={scenario}\n    ),\n    reference_grouped AS MATERIALIZED (\n        SELECT * FROM reference_identified WHERE NOT (role='artifact' AND fragmented)\n        UNION ALL\n        SELECT * FROM (\n            SELECT DISTINCT ON (source_id,source_path) * FROM reference_identified\n             WHERE role='artifact' AND fragmented\n             ORDER BY source_id,source_path,final_score DESC,lexical_sort_key NULLS LAST,external_hit_id,id\n        ) best_fragment\n    ),\n    reference_ordered AS MATERIALIZED (\n        SELECT * FROM reference_grouped\n         ORDER BY final_score DESC,lexical_sort_key NULLS LAST,external_hit_id,id LIMIT {limit}\n    ),\n"

SUFFIX = "    candidate_identified AS MATERIALIZED (\n        SELECT keyed.*,f.source_path,f.role,f.fragmented,md5('hit-'||keyed.id) AS external_hit_id\n          FROM identity_candidates keyed JOIN prefix_fixture f ON f.id=keyed.id AND f.scenario={scenario}\n    ),\n    candidate_grouped AS MATERIALIZED (\n        SELECT * FROM candidate_identified WHERE NOT (role='artifact' AND fragmented)\n        UNION ALL\n        SELECT * FROM (\n            SELECT DISTINCT ON (source_id,source_path) * FROM candidate_identified\n             WHERE role='artifact' AND fragmented\n             ORDER BY source_id,source_path,final_score DESC,lexical_sort_key NULLS LAST,external_hit_id,id\n        ) best_fragment\n    ),\n    candidate_ordered AS MATERIALIZED (\n        SELECT * FROM candidate_grouped\n         ORDER BY final_score DESC,lexical_sort_key NULLS LAST,external_hit_id,id LIMIT {limit}\n    )\n    SELECT jsonb_build_object('scenario',{scenario},'limit',{limit},'equal',\n       COALESCE((SELECT jsonb_agg(jsonb_build_array(id,final_score,lexical_sort_key,external_hit_id)\n                  ORDER BY final_score DESC,lexical_sort_key NULLS LAST,external_hit_id,id) FROM reference_ordered),'[]'::jsonb)\n       =COALESCE((SELECT jsonb_agg(jsonb_build_array(id,final_score,lexical_sort_key,external_hit_id)\n                  ORDER BY final_score DESC,lexical_sort_key NULLS LAST,external_hit_id,id) FROM candidate_ordered),'[]'::jsonb),\n       'reference_hashes',(SELECT count(*) FROM reference_identified),\n       'candidate_hashes',(SELECT count(*) FROM candidate_identified),\n       'result_count',(SELECT count(*) FROM reference_ordered));\n"
