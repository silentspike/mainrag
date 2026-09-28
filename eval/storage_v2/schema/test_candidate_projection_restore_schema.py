"""Real PostgreSQL projection restore, replay, guards and original identity."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import time
from unittest.mock import patch
from eval.storage_v2.test_candidate_projection_restore import M

class CandidateProjectionSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from eval.storage_v2.schema import test_shadow_ingest_schema as schema
        cls.schema = schema
        schema.ShadowIngestSchemaTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        cls.schema.ShadowIngestSchemaTests.tearDownClass.__func__(cls)

    @classmethod
    def command(cls, *args, **kwargs):
        if "--command" in args:
            index = args.index("--command")
            statement = args[index + 1]
            command = ["psql", "-X", "--no-psqlrc", "-qAt", "-v", "ON_ERROR_STOP=1", "--host", str(cls.socket), "-d", cls.database]
            result = subprocess.run(command, input=statement, capture_output=True, text=True, cwd=cls.schema.ROOT)
            if kwargs.get("check", True) and result.returncode:
                raise AssertionError(result.stderr)
            return result
        return cls.schema.ShadowIngestSchemaTests.command.__func__(cls, *args, **kwargs)

    @classmethod
    def sql(cls, statement):
        return cls.schema.ShadowIngestSchemaTests.sql.__func__(cls, statement)

    @classmethod
    def file(cls, path):
        return cls.schema.ShadowIngestSchemaTests.file.__func__(cls, path)

    @classmethod
    def admin(cls, statement):
        return cls.schema.ShadowIngestSchemaTests.admin.__func__(cls, statement)

    actor = staticmethod(lambda user, sql: __import__("eval.storage_v2.schema.test_shadow_ingest_schema", fromlist=["ShadowIngestSchemaTests"]).ShadowIngestSchemaTests.actor(user, sql))

    def test_real_guarded_restore_replays_unknown_commit_and_preserves_original(self):
        S = self.schema
        original = S.ShadowIngestSchemaTests
        self.sql("INSERT INTO sources(id,name,type,path) VALUES (31,'synthetic-restore','fs','synthetic-restore')")
        witness = {"kind": "release-candidate-build", "commit_sha": "a" * 40,
                   "fixture_sha256": "b" * 64, "source_watermark_sha256": "c" * 64,
                   "adapter_profile_id": "fixture-adapter-v1", "opaque": "$projection_guard$'; SELECT 1; --"}
        run = int(self.sql(self.admin("SELECT (storage_v2_begin_shadow_ingest(31,'" + "d" * 64
            + "','" + "e" * 64 + "','fixture-adapter-v1','synthetic-snapshot',"
            + M["literal"](json.dumps(witness)) + "::jsonb,false)).id")))
        content = ("Unicode 🧪 naïve 日本語 $projection_guard$'; SELECT 1; --\n" * 800)
        node, view, sha = original.make_projection(self, content)
        original.stage(self, run, "original.txt", content, node, view, sha)
        original.stage(self, run, "ranked.txt", content, node, view, sha)
        original.complete_analysis(self, sha)
        document = self.sql(self.admin(f"SELECT id FROM storage_v2_put_search_document('mainrag.lexical-simple.v1','node',{node},"
                                      + M["literal"](content) + ",ARRAY[]::text[])"))
        self.sql(self.admin(f"SELECT storage_v2_bind_search_document({view},0,{document},1.0)"))
        original.commit(self, run, 2)
        generation = int(self.sql(f"SELECT generation_id FROM storage_v2_ingest_run WHERE id={run}"))
        self.sql(self.admin(f"SELECT storage_v2_verify_generation({generation},'{ 'f' * 64}')"))
        for number in range(66, 86):
            files = list((S.ROOT / "migrations").glob(f"{number:03}_*.sql"))
            self.assertEqual(len(files), 1)
            self.file(files[0])
        self.sql("GRANT SELECT ON ALL TABLES IN SCHEMA public TO mainrag; GRANT TEMP ON DATABASE " + self.database + " TO mainrag")
        # Empty legacy files: canonical fallback must be complete and immutable.
        db = M["Database"](self.database, False)
        db.command += ["--host", str(self.socket)]
        snap = M["snapshot"](db, 31, generation)
        plan = {"schema_version": "mainrag.storage-v2.projection-restore-plan.v1", "original": snap,
                "captured_at_unix": int(time.time()), "source_observation": {
                    "source_id": 31, "item_count": 2, "source_watermark_sha256": "c" * 64,
                    "adapter_profile_id": "fixture-adapter-v1"},
                "operator_sha256": M["digest"](Path(M["__file__"]).read_bytes()), "planner": M["PLANNER"],
                "canonical_chunker_sha256": M["digest"]((S.ROOT / "api/src/services/chunker/character.rs").read_bytes())}
        ranked = db.query(M["member_query"](plan, 0))[1]
        initial = [{**{key: ranked[key] for key in ("occurrence_id", "artifact_version_id", "document_id", "expected_content_hash", "body_bytes")},
                    "mode": "restore-segments"}]
        db.query(M["write_statement"](plan, [ranked], initial), write=True)
        self.sql("WITH file AS (INSERT INTO files(source_id,path,hash,content,content_text,size_original,size_compressed,last_modified) "
                 f"VALUES(31,'/synthetic/ranked.txt',decode('{sha}','hex'),''," + M["literal"](content)
                 + f",{len(content.encode())},0,now()) RETURNING id) "
                 "INSERT INTO chunks(file_id,chunk_type,content_hash,content_compressed,content_text,start_line,end_line) "
                 "SELECT id,'text',digest('Unicode','sha256'),'','Unicode',1,1 FROM file")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); root.chmod(0o700)
            proof = root / "preflight.json"
            proof.write_text(json.dumps({"schema_version": "mainrag-storage-v2-preflight/v1", "mode": "check",
                                        "overall_status": "PASS", "checks": dict.fromkeys(M["PREFLIGHT_CHECKS"], "PASS")}));proof.chmod(0o600)
            args = argparse.Namespace(state=root / "state.json", preflight=proof,
                                      expected_preflight_sha256=M["digest"](proof.read_bytes()))
            real = db.query
            unknown = False
            def lose_reply(sql, **kwargs):
                nonlocal unknown
                result = real(sql, **kwargs)
                if kwargs.get("write") and not unknown:
                    unknown = True
                    raise RuntimeError("synthetic lost commit reply")
                return result
            with patch.object(db, "query", side_effect=lose_reply):
                with self.assertRaisesRegex(RuntimeError, "lost commit reply"):
                    M["apply"](db, plan, "a" * 64, args)
            state = json.loads(args.state.read_text())
            self.assertEqual(state["status"], "PENDING_COMMIT")
            self.assertEqual(state["completed_documents"], 0)
            self.assertEqual(state["pending"][0]["mode"], "restore-segments")
            self.assertEqual(state["pending"][1]["mode"], "rank-only")
            expected_count = len(M["canonical_segments"](content))
            self.assertGreater(expected_count, 1)
            self.assertEqual(int(self.sql("SELECT count(*) FROM storage_v2_lexical_segment")), 2 * expected_count)
            fresh_plan = json.loads(json.dumps(plan));fresh_plan["captured_at_unix"] = int(time.time())
            result = M["apply"](db, fresh_plan, "b" * 64, args)
            self.assertEqual(result["status"], "PASS_PROJECTIONS_ONLY")
            self.assertEqual(result["completed_documents"], 2)
            self.assertEqual(M["snapshot"](db, 31, generation), snap)
            self.assertEqual(int(self.sql("SELECT count(*) FROM storage_v2_lexical_segment")), 2 * expected_count)
            self.assertEqual(self.sql("SELECT count(*) FROM storage_v2_legacy_lexical_segment"), "1")
            self.assertFalse(result["qualification"])
            self.assertEqual(result["prior_plan_sha256"], ["a" * 64])
            # Metadata drift and altered original body identities reject writes.
            drift = json.loads(json.dumps(plan));drift["original"]["generation_sha256"] = "0" * 64
            with self.assertRaises(RuntimeError):
                db.query(M["guard_sql"](drift), write=True)
            row = db.query(M["member_query"](plan, 0))[0]
            row["expected_content_hash"] = "0" * 64
            with self.assertRaises(RuntimeError):
                M["check_members"]([row])
