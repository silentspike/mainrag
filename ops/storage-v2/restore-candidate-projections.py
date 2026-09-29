#!/usr/bin/env python3
"""Resume missing deterministic projections without rebuilding sealed candidates.

Only supported immutable projection writers are called. Plan, preflight and
state are private; this operation is neither qualification nor activation.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import signal
import stat
import subprocess
import time
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
batch = runpy.run_path(str(HERE / "source-batch.py"))
watermark = runpy.run_path(str(HERE / "source-snapshot-review.py"))["observation"]
load_token = runpy.run_path(str(HERE / "operator_token.py"))["load_token"]
SHA = re.compile(r"[0-9a-f]{64}\Z")
FUNCTIONS = [
    "storage_v2_materialize_legacy_chunk_ranks(bigint,bigint)",
    "storage_v2_copy_legacy_lexical_segments(bigint,bigint)",
    "storage_v2_put_lexical_segments_at(bigint,bigint,bigint[],text[],text[],text[],bigint[])",
    "storage_v2_verify_lexical_segments(bigint)",
]
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_BATCH_BYTES = 16 * 1024 * 1024
MAX_DOCUMENTS = 32
CANONICAL_CHUNKER_SHA256 = "83a46a1d3017b74b842545d84f3daa7684a045e3a660c064295eb6d87c84e914"
PREFLIGHT_CHECKS = {"candidate_identity", "postgresql_version", "extensions", "configuration",
                    "collation", "capacity", "backup", "maintenance", "active_operations",
                    "backend_lock", "backend_index"}
PLANNER = {"max_characters": 1000, "overlap_characters": 100,
           "chunk_type": "text", "context_prefix": "", "positions": "first-Unicode-match"}


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def literal(value: str) -> str:
    if "\x00" in value:
        raise RuntimeError("PostgreSQL text cannot contain a zero byte")
    return "'" + value.replace("'", "''") + "'"


def private_json(path: Path, expected: str | None = None) -> tuple[dict, str]:
    metadata = path.lstat()
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077
            or metadata.st_size > 64 * 1024 * 1024):
        raise RuntimeError("bounded private regular input required")
    raw = batch["private_file"](path)
    sha = digest(raw)
    if expected is not None and (not SHA.fullmatch(expected) or sha != expected):
        raise RuntimeError("protected input digest differs")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError("protected input object required")
    return value, sha


class Database:
    def __init__(self, database: str, local_postgres: bool):
        self.command = (["sudo", "-n", "-u", "postgres", "--"] if local_postgres else []) + [
            "psql", "-X", "--no-psqlrc", "-qAt", "-v", "ON_ERROR_STOP=1", "-d", database]

    def query(self, statement: str, *, write: bool = False) -> list[Any]:
        transaction = "BEGIN;" if write else "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;"
        sql = (transaction + "\nSET LOCAL standard_conforming_strings=on;"
               "SET LOCAL lock_timeout='5s'; SET LOCAL statement_timeout='120s';\n"
               + statement + "\nCOMMIT;\n")
        environment = os.environ.copy()
        environment["PGAPPNAME"] = "mainrag-storage-v2-projection-restore"
        result = subprocess.run(self.command, input=sql, text=True,
                                capture_output=True, env=environment, check=False)
        if result.returncode:
            # SQL error context can contain private body text. It must not be
            # printed, embedded in state, or mistaken for a known rollback.
            raise RuntimeError("projection database call failed; retain pending state and reconcile")
        return [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]


def snapshot_statement(source_id: int, generation_id: int) -> str:
    signatures = ",".join(literal(value) + "::regprocedure" for value in FUNCTIONS)
    return f"""
SELECT jsonb_build_object(
 'source_id',g.source_id,'generation_id',g.id,'generation_seq',g.generation_seq,
 'status',g.status::text,'item_count',g.item_count,'witness',g.witness,
 'generation_sha256',encode(sha256(convert_to(to_jsonb(g)::text,'UTF8')),'hex'),
 'run_status',r.status,'run_sha256',encode(sha256(convert_to(to_jsonb(r)::text,'UTF8')),'hex'),
 'source_type',s.type,'registry_sha256',encode(sha256(convert_to(
    jsonb_build_array(s.type,s.path,s.config,s.is_test)::text,'UTF8')),'hex'),
 'active_generation_id',l.active_generation_id,
 'lexical_relation',CASE WHEN to_regclass('public.storage_v2_lexical_segment_all') IS NULL
    THEN 'storage_v2_lexical_segment' ELSE 'storage_v2_lexical_segment_all' END,
 'open_runs',(SELECT count(*) FROM storage_v2_ingest_run WHERE source_id=g.source_id AND status='building'),
 'function_sha256',(SELECT jsonb_object_agg(oid::regprocedure::text,
    encode(sha256(convert_to(jsonb_build_array(pg_get_functiondef(oid),proowner,proacl,proconfig)::text,'UTF8')),'hex'))
    FROM pg_proc WHERE oid IN ({signatures}))
) FROM source_generation g JOIN storage_v2_ingest_run r ON r.generation_id=g.id
 JOIN logical_source l ON l.id=g.source_id JOIN sources s ON s.id=g.source_id
 WHERE g.id={generation_id} AND g.source_id={source_id};
"""


def snapshot(db: Database, source_id: int, generation_id: int) -> dict:
    rows = db.query(snapshot_statement(source_id, generation_id))
    if len(rows) != 1:
        raise RuntimeError("exactly one sealed generation required")
    value = rows[0]
    witness = value.get("witness")
    if (value["status"] not in {"verified", "release_candidate"}
            or value["run_status"] != "sealed" or value["active_generation_id"] is not None
            or value["open_runs"] != 0 or value["source_type"] not in {"fs", "git", "pdf", "managed_append"}
            or not isinstance(witness, dict) or witness.get("kind") != "release-candidate-build"
            or not SHA.fullmatch(witness.get("source_watermark_sha256", ""))
            or not SHA.fullmatch(witness.get("fixture_sha256", ""))
            or not re.fullmatch(r"[0-9a-f]{40}", witness.get("commit_sha", ""))
            or len(value["function_sha256"]) != len(FUNCTIONS)):
        raise RuntimeError("original sealed inactive candidate identity required")
    return value


def canonical_segments(text: str) -> list[tuple[int, str, int]]:
    """Match CharacterChunker::default and lexical_first_character_positions."""
    if len(text.encode()) > MAX_DOCUMENT_BYTES or any(0xD800 <= ord(c) <= 0xDFFF for c in text):
        raise RuntimeError("bounded Unicode source document required")
    rows = []
    start = 0
    while start < len(text):
        end = min(start + 1000, len(text))
        piece = text[start:end]
        rows.append((len(rows), piece, text.find(piece) + 1))
        if end == len(text):
            break
        start = end - 100
    return rows


def member_query(plan: dict, after: int, pending: list[dict] | None = None) -> str:
    frozen = plan["original"]
    source_id, seq = frozen["source_id"], frozen["generation_seq"]
    lexical_relation = frozen["lexical_relation"]
    if lexical_relation not in {"storage_v2_lexical_segment", "storage_v2_lexical_segment_all"}:
        raise RuntimeError("frozen lexical projection layout is invalid")
    selected = ("o.id IN (" + ",".join(str(row["occurrence_id"]) for row in pending) + ")"
                if pending else f"o.id>{after}")
    return f"""
WITH selected AS MATERIALIZED (
 SELECT o.id occurrence_id,o.artifact_version_id,document.id document_id,
        artifact.expected_content_hash,octet_length(document.search_text) body_bytes,
        (SELECT count(*) FROM {lexical_relation} WHERE occurrence_id=o.id) segment_count
 FROM occurrence o JOIN generation_item_version membership
   ON membership.source_id=o.source_id AND membership.artifact_version_id=o.artifact_version_id
 JOIN artifact_version artifact ON artifact.id=o.artifact_version_id
 JOIN storage_v2_search_view_document binding ON binding.view_id=o.view_id AND binding.ordinal=0
 JOIN storage_v2_search_document document ON document.id=binding.document_id
   AND document.component_kind='node' AND document.node_id=artifact.content_root_node_id
 WHERE o.source_id={source_id} AND membership.valid_from_seq<={seq}
  AND (membership.valid_to_seq IS NULL OR membership.valid_to_seq>{seq}) AND {selected}
 ORDER BY o.id LIMIT {MAX_DOCUMENTS}
), bounded AS (
 SELECT *,sum(body_bytes) OVER (ORDER BY occurrence_id) cumulative_bytes FROM selected
)
SELECT jsonb_build_object('occurrence_id',b.occurrence_id,'artifact_version_id',b.artifact_version_id,
 'document_id',b.document_id,'expected_content_hash',b.expected_content_hash,'body_bytes',b.body_bytes,
 'segment_count',b.segment_count,'text',document.search_text)
 FROM bounded b JOIN storage_v2_search_document document ON document.id=b.document_id
 WHERE cumulative_bytes<={MAX_BATCH_BYTES} OR b.occurrence_id=(SELECT min(occurrence_id) FROM bounded)
 ORDER BY b.occurrence_id;
"""


def check_members(rows: list[dict], pending: list[dict] | None = None) -> None:
    if not rows or len(rows) > MAX_DOCUMENTS or sum(row["body_bytes"] for row in rows) > MAX_BATCH_BYTES:
        raise RuntimeError("bounded nonempty source member group required")
    identities = []
    for row in rows:
        if (type(row["occurrence_id"]) is not int or row["occurrence_id"] <= 0
                or type(row["artifact_version_id"]) is not int or row["artifact_version_id"] <= 0
                or not isinstance(row["text"], str) or row["body_bytes"] > MAX_DOCUMENT_BYTES
                or len(row["text"].encode()) != row["body_bytes"]
                or digest(row["text"].encode()) != row["expected_content_hash"]):
            raise RuntimeError("immutable original artifact body identity differs")
        identities.append({key: row[key] for key in ("occurrence_id", "artifact_version_id", "document_id", "expected_content_hash", "body_bytes")})
    if len({row["occurrence_id"] for row in rows}) != len(rows):
        raise RuntimeError("source member identity is duplicated")
    if pending is not None and identities != [{key: row[key] for key in identities[0]} for row in pending]:
        raise RuntimeError("pending source member identity differs")


def guard_sql(plan: dict) -> str:
    original = plan["original"]
    source_id, generation_id = original["source_id"], original["generation_id"]
    # This DO block contains identities only. Private source text is never
    # interpolated into a dollar-quoted procedural body.
    query = snapshot_statement(source_id, generation_id).strip().removesuffix(";")
    frozen_hex = json.dumps(original, sort_keys=True).encode().hex()
    return f"""
SELECT id FROM logical_source WHERE id={source_id} FOR UPDATE;
SELECT id FROM sources WHERE id={source_id} FOR UPDATE;
SELECT id FROM source_generation WHERE id={generation_id} FOR UPDATE;
SELECT id FROM storage_v2_ingest_run WHERE generation_id={generation_id} FOR UPDATE;
DO $projection_guard$
DECLARE current jsonb;
BEGIN
 SELECT ({query}) INTO current;
 IF current IS DISTINCT FROM convert_from(decode('{frozen_hex}','hex'),'UTF8')::jsonb THEN
  RAISE EXCEPTION 'original candidate or projection package drifted';
 END IF;
END $projection_guard$;
SET LOCAL ROLE mainrag;
SELECT set_config('app.user_id',(SELECT id::text FROM users WHERE is_admin ORDER BY id LIMIT 1),true),
       set_config('app.is_admin','true',true);
"""


def write_statement(plan: dict, rows: list[dict], pending: list[dict]) -> str:
    sql = guard_sql(plan)
    modes = {row["occurrence_id"]: row["mode"] for row in pending}
    for row in rows:
        oid, artifact = row["occurrence_id"], row["artifact_version_id"]
        sql += f"SELECT jsonb_build_object('occurrence_id',{oid},'legacy_rows',storage_v2_materialize_legacy_chunk_ranks({oid},{artifact}));\n"
        if modes[oid] == "rank-only":
            continue
        # Replays preserve this original mode, even after an unknown commit.
        # Materialized copy state is shared by all bounded segment groups.
        sql += f"CREATE TEMP TABLE projection_copy_{oid} ON COMMIT DROP AS SELECT storage_v2_copy_legacy_lexical_segments({oid},{artifact}) copied;\n"
        segments = canonical_segments(row["text"])
        for offset in range(0, len(segments), 256):
            group = segments[offset:offset + 256]
            orders = "ARRAY[" + ",".join(str(order) for order, _, _ in group) + "]::bigint[]"
            texts = "ARRAY[" + ",".join(literal(piece) for _, piece, _ in group) + "]::text[]"
            prefixes = "array_fill(''::text,ARRAY[" + str(len(group)) + "])"
            types = "array_fill('text'::text,ARRAY[" + str(len(group)) + "])"
            starts = "ARRAY[" + ",".join(str(position) for _, _, position in group) + "]::bigint[]"
            sql += (f"SELECT jsonb_build_object('occurrence_id',{oid},'segments',CASE WHEN copied=0 THEN "
                    f"storage_v2_put_lexical_segments_at({oid},{artifact},{orders},{texts},{prefixes},{types},{starts}) ELSE 0 END) "
                    f"FROM projection_copy_{oid};\n")
    return sql


def verify_preflight(path: Path, expected: str, now: int) -> dict:
    value, _ = private_json(path, expected)
    if (value.get("schema_version") != "mainrag-storage-v2-preflight/v1"
            or value.get("overall_status") != "PASS" or set(value.get("checks", {})) != PREFLIGHT_CHECKS
            or set(value["checks"].values()) != {"PASS"}
            or value.get("mode") != "check"
            or not 0 <= now - int(path.stat().st_mtime) <= 900):
        raise RuntimeError("fresh passing maintenance and resource preflight required")
    return value


def validate_plan(plan: dict) -> None:
    original = plan.get("original", {})
    observed = plan.get("source_observation", {})
    if (plan.get("schema_version") != "mainrag.storage-v2.projection-restore-plan.v1"
            or plan.get("operator_sha256") != digest(Path(__file__).read_bytes()) or plan.get("planner") != PLANNER
            or plan.get("canonical_chunker_sha256") != digest((ROOT / "api/src/services/chunker/character.rs").read_bytes())
            or plan.get("canonical_chunker_sha256") != CANONICAL_CHUNKER_SHA256
            or any(type(original.get(key)) is not int or original[key] <= 0
                   for key in ("source_id", "generation_id", "generation_seq"))
            or original.get("status") not in {"verified", "release_candidate"}
            or original.get("run_status") != "sealed" or original.get("active_generation_id") is not None
            or original.get("open_runs") != 0
            or original.get("lexical_relation") not in {
                "storage_v2_lexical_segment", "storage_v2_lexical_segment_all"}
            or type(plan.get("captured_at_unix")) is not int
            or not 0 <= int(time.time()) - plan["captured_at_unix"] <= 900
            or any(observed.get(key) != expected for key, expected in {
                "source_id": original.get("source_id"), "item_count": original.get("item_count"),
                "source_watermark_sha256": original.get("witness", {}).get("source_watermark_sha256"),
                "adapter_profile_id": original.get("witness", {}).get("adapter_profile_id")}.items())):
        raise RuntimeError("frozen inactive projection operator and candidate required")


def plan_identity(plan: dict) -> str:
    value = {key: plan[key] for key in ("schema_version", "original", "operator_sha256", "planner", "canonical_chunker_sha256")}
    value["source_observation"] = {key: plan["source_observation"][key] for key in (
        "source_id", "source_watermark_sha256", "adapter_profile_id", "item_count")}
    return digest(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def make_plan(db: Database, args: argparse.Namespace) -> dict:
    original = snapshot(db, args.source_id, args.generation_id)
    token = load_token(args.token_file, args.token_env)
    observed = watermark(args.api_url, token, args.source_id)
    witness = original["witness"]
    if any(observed.get(key) != expected for key, expected in {
        "source_id": args.source_id, "source_watermark_sha256": witness["source_watermark_sha256"],
        "adapter_profile_id": witness["adapter_profile_id"], "item_count": original["item_count"]}.items()):
        raise RuntimeError("current source differs from original sealed build")
    if snapshot(db, args.source_id, args.generation_id) != original:
        raise RuntimeError("candidate changed during source observation")
    return {"schema_version": "mainrag.storage-v2.projection-restore-plan.v1", "original": original,
            "source_observation": observed, "captured_at_unix": int(time.time()),
            "operator_sha256": digest(Path(__file__).read_bytes()), "planner": PLANNER,
            "canonical_chunker_sha256": digest((ROOT / "api/src/services/chunker/character.rs").read_bytes()),
            "qualification": False, "activation": False}


def apply(db: Database, plan: dict, plan_sha: str, args: argparse.Namespace) -> dict:
    validate_plan(plan)
    identity = plan_identity(plan)
    state_path = args.state
    state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with state_path.with_suffix(state_path.suffix + ".lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if state_path.exists():
            state, _ = private_json(state_path)
            if state.get("plan_identity_sha256") != identity:
                raise RuntimeError("resume state names a different plan")
            if state.get("schema_version") != "mainrag.storage-v2.projection-restore-state.v1" \
                    or type(state.get("last_occurrence_id")) is not int or state["last_occurrence_id"] < 0:
                raise RuntimeError("resume cursor identity is invalid")
            if state["plan_sha256"] != plan_sha:
                state.setdefault("prior_plan_sha256", []).append(state["plan_sha256"])
                state["plan_sha256"] = plan_sha
                batch["write_state"](state_path, state)
        else:
            state = {"schema_version": "mainrag.storage-v2.projection-restore-state.v1", "plan_sha256": plan_sha,
                     "plan_identity_sha256": identity,
                     "source_id": plan["original"]["source_id"], "generation_id": plan["original"]["generation_id"],
                     "last_occurrence_id": 0, "completed_documents": 0, "committed_batches": 0,
                     "pending": None, "status": "RUNNING", "started_at_unix": int(time.time())}
            batch["write_state"](state_path, state)
        stop = False
        def request_stop(_signum: int, _frame: object) -> None:
            nonlocal stop
            stop = True
        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        while not stop:
            verify_preflight(args.preflight, args.expected_preflight_sha256, int(time.time()))
            frozen = plan["original"]
            if snapshot(db, frozen["source_id"], frozen["generation_id"]) != frozen:
                raise RuntimeError("original candidate identity differs on resume")
            pending = state["pending"]
            if pending is not None and (not isinstance(pending, list) or not pending
                    or len(pending) > MAX_DOCUMENTS or any(
                        type(row.get("occurrence_id")) is not int or row["occurrence_id"] <= state["last_occurrence_id"]
                        or row.get("mode") not in {"rank-only", "restore-segments"} for row in pending)):
                raise RuntimeError("pending projection intent is invalid")
            rows = db.query(member_query(plan, state["last_occurrence_id"], pending))
            if not rows:
                if pending is not None:
                    raise RuntimeError("pending source members disappeared")
                result = db.query(guard_sql(plan) + f"SELECT storage_v2_verify_lexical_segments({frozen['generation_id']});", write=True)
                if len(result) != 1 or result[0].get("invalid_count") != 0 or result[0].get("missing_count") != 0:
                    raise RuntimeError("complete lexical projection verification required")
                state.update(status="PASS_PROJECTIONS_ONLY", verification=result[0], finished_at_unix=int(time.time()), qualification=False)
                batch["write_state"](state_path, state)
                return state
            check_members(rows, pending)
            if pending is None:
                pending = [{**{key: row[key] for key in ("occurrence_id", "artifact_version_id", "document_id", "expected_content_hash", "body_bytes")},
                            "mode": "rank-only" if row["segment_count"] else "restore-segments"} for row in rows]
                state.update(pending=pending, status="PENDING_COMMIT")
                batch["write_state"](state_path, state)
            db.query(write_statement(plan, rows, pending), write=True)
            state.update(last_occurrence_id=rows[-1]["occurrence_id"], pending=None, status="RUNNING",
                         completed_documents=state["completed_documents"] + len(rows),
                         committed_batches=state["committed_batches"] + 1, updated_at_unix=int(time.time()))
            batch["write_state"](state_path, state)
            print(json.dumps({key: state[key] for key in ("status", "completed_documents", "committed_batches")}), flush=True)
        state["status"] = "STOPPED_AT_BATCH_BOUNDARY"
        batch["write_state"](state_path, state)
        return state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "apply", "status"))
    parser.add_argument("--database", default="mainrag")
    parser.add_argument("--local-postgres", action="store_true")
    parser.add_argument("--source-id", type=int)
    parser.add_argument("--generation-id", type=int)
    parser.add_argument("--api-url", default="http://127.0.0.1:3001")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--token-env")
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--state", type=Path)
    parser.add_argument("--preflight", type=Path)
    parser.add_argument("--expected-preflight-sha256")
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == "status":
        state, _ = private_json(args.state)
        print(json.dumps({key: state.get(key) for key in ("status", "completed_documents", "committed_batches", "qualification")}))
        return 0
    db = Database(args.database, args.local_postgres)
    if args.command == "plan":
        if not args.source_id or args.source_id <= 0 or not args.generation_id or args.generation_id <= 0 or not args.plan:
            parser.error("positive source and generation plus private plan output required")
        if args.plan.exists():
            parser.error("plan output already exists")
        value = make_plan(db, args)
        batch["write_state"](args.plan, value)
        print(json.dumps({"status": "PLANNED", "plan_sha256": digest(args.plan.read_bytes()), "qualification": False}))
        return 0
    if not all((args.plan, args.expected_plan_sha256, args.state, args.preflight, args.expected_preflight_sha256)):
        parser.error("apply requires exact private plan, durable state and fresh preflight digests")
    plan, sha = private_json(args.plan, args.expected_plan_sha256)
    if args.source_id is not None and args.source_id != plan.get("original", {}).get("source_id"):
        raise RuntimeError("source batch identity differs from the original projection plan")
    result = apply(db, plan, sha, args)
    print(json.dumps({"status": result["status"], "completed_documents": result["completed_documents"], "qualification": False}))
    return 0 if result["status"] == "PASS_PROJECTIONS_ONLY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
