"""Reuse completed immutable proof; repeat current reader and live-source gates."""
from __future__ import annotations

import json
import re
import subprocess
from typing import Any


# Migrations 155-156 change query readers and an exact positional-free cache.
# Cache transitions preserve every original field and are checked against the
# complete canonical vector by the mutation trigger. The integrity verifier,
# pack delivery, lexical reconstruction and intelligence export are unchanged.
QUERY_ONLY_CHANGES = frozenset({
    "storage_v2_source_segment_ranks(bigint[],text)",
    "storage_v2_source_segment_ranks_precise(bigint[],text)",
    "storage_v2_source_segment_rank_candidates(bigint[],text)",
    "storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])",
    "storage_v2_source_segment_body_matches(bigint,text)",
    "storage_v2_authorized_cached_lexical_first_candidates(bigint[],bigint[],text)",
    "storage_v2_derived_lexical_first_candidates(bigint[],bigint[],text)",
    "storage_v2_derived_lexical_candidate_occurrences(bigint[],bigint[],integer[])",
    "storage_v2_scoped_query_posting(bigint[],text[])",
    "storage_v2_prepare_document_vector()",
    "storage_v2_reject_document_mutation()",
    "storage_v2_invalidate_reader_metadata()",
})
QUERY_ONLY_ADDITIONS = frozenset({
    "storage_v2_cached_first_conjunction_order(text[],smallint[],bigint[],text[])",
})
INTEGRITY_CHECKS = frozenset({
    "artifact_root", "authorization", "body_pack_integrity", "intelligence",
    "intervals", "legacy_intelligence_export", "lexical_segment_integrity",
})
IDENTITY_FIELDS = (
    "source_id", "generation_id", "generation_seq", "source_watermark_sha256",
    "item_count", "active_generation_id", "verification_manifest_sha256",
)


def validate_reuse(prior: dict[str, Any], checkpoint: dict[str, Any],
                   state: dict[str, Any], previous_install: dict[str, Any],
                   previous_install_sha256: str, current_reader: dict[str, Any],
                   observed: dict[str, Any]) -> dict[str, Any]:
    """Fail closed on input, verifier, producer, or immutable identity drift."""
    verified = prior.get("verification", {})
    previous = prior.get("checkpoint", {})
    for name in ("source_id", "generation_id", "generation_seq", "commit_sha",
                 "source_watermark_sha256", "item_count", "active_generation_id",
                 "git_snapshot_commit_sha", "source_snapshot_review_sha256",
                 "source_snapshot_gold_review_sha256"):
        if previous.get(name) != checkpoint.get(name):
            raise RuntimeError("completed integrity checkpoint identity differs")
    if not isinstance(previous.get("build"), dict) or previous["build"] != checkpoint.get("build"):
        raise RuntimeError("completed integrity build identity differs")
    if not all(verified.get("checks", {}).get(name) == "PASS" for name in INTEGRITY_CHECKS):
        raise RuntimeError("completed integrity proof is incomplete")
    if previous_install.get("status") != "PASS" or prior.get("reader_package", {}).get(
            "installation_receipt_sha256") != previous_install_sha256:
        raise RuntimeError("completed integrity verifier installation differs")
    binary = previous_install.get("binaries", {}).get("mainrag-api", {}).get("sha256")
    if not re.fullmatch(r"[0-9a-f]{64}", binary or "") or current_reader.get(
            "binary_sha256") != binary or prior.get("reader_package", {}).get("binary_sha256") != binary:
        raise RuntimeError("completed integrity verifier binary differs")
    if state.get("status") not in {"verified", "release_candidate"}:
        raise RuntimeError("completed integrity generation is no longer verified")
    for name in IDENTITY_FIELDS:
        if name not in state or name not in verified or state[name] != verified[name]:
            raise RuntimeError("completed integrity live identity differs")
    identity = observed.get("identity", {})
    for name in IDENTITY_FIELDS + ("generation_root_sha256", "run_id"):
        if name not in identity or name not in verified or identity[name] != verified[name]:
            raise RuntimeError("completed integrity immutable identity differs")
    if identity.get("commit_sha") != checkpoint.get("commit_sha") or identity.get("run_status") != "sealed":
        raise RuntimeError("completed integrity producer is not sealed")
    if identity.get("adapter_profile_id") != verified.get("adapter_profile_id"):
        raise RuntimeError("completed integrity producer profile differs")
    for name in ("verification_manifest_sha256", "generation_root_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", verified.get(name) or ""):
            raise RuntimeError("completed integrity digest is invalid")
    lexical = verified.get("lexical_segment_verification", {})
    if lexical.get("generation_id") != checkpoint["generation_id"] or lexical.get(
            "occurrence_count") != checkpoint["item_count"] or any(
            lexical.get(name) != 0 for name in ("missing_count", "invalid_count")):
        raise RuntimeError("completed integrity lexical coverage is incomplete")
    old_functions = {name: value for name, value in previous_install.get(
        "function_identities", {}).items() if name.startswith("storage_v2_")}
    functions = observed.get("function_identities", {})
    if not old_functions or set(old_functions) - set(functions) or (
            set(functions) - set(old_functions)) - QUERY_ONLY_ADDITIONS:
        raise RuntimeError("completed integrity verifier catalog differs")
    if any(functions[name] != digest for name, digest in old_functions.items()
           if name not in QUERY_ONLY_CHANGES):
        raise RuntimeError("completed integrity verifier definitions differ")
    return verified


def observe_immutable_identity(source_id: int, generation_id: int) -> dict[str, Any]:
    """Bounded local catalog read; no source content, pack copy, or verifier run."""
    if type(source_id) is not int or type(generation_id) is not int or min(source_id, generation_id) <= 0:
        raise RuntimeError("positive immutable candidate identity required")
    sql = f"""BEGIN READ ONLY; SET LOCAL statement_timeout='15s';
SELECT jsonb_build_object(
 'identity',(SELECT jsonb_build_object(
  'source_id',g.source_id,'generation_id',g.id,'generation_seq',g.generation_seq,'status',g.status,
  'source_watermark_sha256',r.semantic_manifest_sha256,'item_count',r.expected_item_count,
  'active_generation_id',s.active_generation_id,
  'verification_manifest_sha256',g.verification_manifest_sha256,
  'generation_root_sha256',r.generation_root_sha256,'run_id',r.id,'run_status',r.status,
  'commit_sha',g.witness->>'commit_sha','adapter_profile_id',r.adapter_profile_id)
 FROM source_generation g JOIN storage_v2_ingest_run r ON r.generation_id=g.id
 JOIN logical_source s ON s.id=g.source_id
 WHERE g.id={generation_id} AND g.source_id={source_id}
  AND g.status IN ('verified','release_candidate')
  AND r.expected_active_generation_id IS NOT DISTINCT FROM s.active_generation_id),
 'function_identities',(SELECT jsonb_object_agg(p.oid::regprocedure::text,
  encode(sha256(convert_to(pg_get_functiondef(p.oid),'UTF8')),'hex'))
 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname='public' AND p.proname LIKE 'storage_v2_%'));
ROLLBACK;"""
    result = subprocess.run(
        ["sudo", "-n", "-u", "postgres", "psql", "-X", "-qAt", "-v",
         "ON_ERROR_STOP=1", "-d", "mainrag", "-c", sql],
        capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError("completed integrity identity observation failed")
    return json.loads(result.stdout)
