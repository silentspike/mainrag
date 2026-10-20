"""Reuse accepted immutable source observations through fresh metadata proofs.

This is not a watermark cache.  Every use checks the registered source, sealed
generation, accepted evidence, pinned descriptor and current kernel read-only
boundary.  Mutable adapters always use their original observation callback.

The protected bindings document has schema_version
``mainrag.storage-v2.immutable-watermark-bindings.v1``, accepted_audit and
accepted_acceptance references, and sources containing source_id, observation,
integrity and snapshot_review references.  References are {path, sha256}.
Integrity is the existing {checkpoint, verification} evidence, not a new pass.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import runpy
import time

CUT = runpy.run_path(str(Path(__file__).with_name("fs_cut.py")))
RELEASE = runpy.run_path(str(Path(__file__).with_name("release-candidate.py")))
CHECKS = runpy.run_path(str(Path(__file__).with_name("integrity_resume.py")))["INTEGRITY_CHECKS"]
GATES = frozenset({"live_adapter_watermarks", "writer_and_maintenance_inventory",
    "current_package_and_schema", "representative_gold_review",
    "per_class_and_aggregate_quality", "cumulative_resource_and_recovery_budget",
    "benchmark_result", "legacy_state_readback"})
METHOD = "mainrag.storage-v2.accepted-immutable-watermark-metadata.v1"


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def immutable(item: dict) -> bool:
    return isinstance(item.get("adapter_profile_id"), str) and item[
        "adapter_profile_id"].startswith(CUT["CUT_PROFILE"])


def reference(value: dict, read, maximum: int | None = None) -> dict:
    require(isinstance(value, dict) and set(value) == {"path", "sha256"}
            and isinstance(value["path"], str)
            and isinstance(value["sha256"], str)
            and re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is not None,
            "exact immutable evidence reference is required")
    if maximum is not None:
        return read(Path(value["path"]), value["sha256"], maximum)
    return read(Path(value["path"]), value["sha256"])


def bindings(candidates: list[dict], bundle: dict | None, read) -> dict[int, dict]:
    selected = {item["source_id"]: item for item in candidates if immutable(item)}
    if not selected:
        require(bundle is None or bundle.get("sources") == [],
                "immutable bindings contain unselected sources")
        return {}
    require(isinstance(bundle, dict) and bundle.get("schema_version") ==
            "mainrag.storage-v2.immutable-watermark-bindings.v1",
            "accepted immutable watermark bindings are required; no scan fallback")
    audit = reference(bundle["accepted_audit"], read)
    acceptance = reference(bundle["accepted_acceptance"], read)
    accepted = audit.get("candidate_set")
    require(audit.get("schema_version") == "mainrag.storage-v2.candidate-aggregate-audit.v2"
            and audit.get("persisted_candidate_set_complete") is True
            and isinstance(accepted, list) and bool(accepted)
            and digest(canonical(accepted)) == audit.get("candidate_set_sha256"),
            "complete accepted immutable candidate set is required")
    require(acceptance.get("schema_version") == "mainrag.storage-v2.aggregate-acceptance.v1"
            and acceptance.get("status") == "PASS"
            and acceptance.get("persisted_audit_sha256") == bundle["accepted_audit"]["sha256"]
            and acceptance.get("candidate_set_sha256") == audit["candidate_set_sha256"]
            and isinstance(acceptance.get("external_gates"), dict)
            and set(acceptance["external_gates"]) == GATES
            and all(isinstance(g, dict) and g.get("status") == "PASS"
                    and isinstance(g.get("evidence_sha256"), str)
                    and re.fullmatch(r"[0-9a-f]{64}", g["evidence_sha256"])
                    for g in acceptance["external_gates"].values()),
            "historical full aggregate acceptance is required")
    accepted_by_id = {item["source_id"]: item for item in accepted}
    require(len(accepted_by_id) == len(accepted), "accepted source set is duplicated")
    watermarks = acceptance.get("current_source_watermarks")
    require(acceptance.get("source_count") == len(accepted)
            and isinstance(watermarks, list) and len(watermarks) == len(accepted)
            and all(isinstance(row, dict) and type(row.get("observed_at_unix")) is int
                    and row["observed_at_unix"] > 0 for row in watermarks)
            and {row.get("source_id"): row.get("watermark_sha256") for row in watermarks}
                == {item["source_id"]: item["source_watermark_sha256"] for item in accepted},
            "historical accepted watermark source set differs")
    rows = bundle.get("sources")
    require(isinstance(rows, list) and len(rows) == len(selected)
            and all(isinstance(row, dict) and type(row.get("source_id")) is int for row in rows)
            and {row["source_id"] for row in rows} == set(selected),
            "immutable proof source set differs")
    prepared = {}
    for row in rows:
        item = selected[row["source_id"]]
        require(item == accepted_by_id.get(item["source_id"]),
                "immutable candidate differs from accepted candidate")
        observation = reference(row["observation"], read)
        integrity = reference(row["integrity"], read)
        checkpoint, verified = integrity.get("checkpoint", {}), integrity.get("verification", {})
        require(isinstance(checkpoint, dict) and isinstance(verified, dict)
                and isinstance(verified.get("checks"), dict)
                and CHECKS <= set(verified["checks"])
                and all(result == "PASS" for result in verified["checks"].values()),
                "completed original integrity verification is required")
        for key, candidate_key in (("source_id", "source_id"),
                ("generation_id", "candidate_generation_id"),
                ("generation_seq", "candidate_generation_seq"),
                ("source_watermark_sha256", "source_watermark_sha256"),
                ("item_count", "item_count")):
            require(checkpoint.get(key) == verified.get(key) == item[candidate_key],
                    "immutable First7 candidate identity differs")
        require(checkpoint.get("active_generation_id") is None
                and verified.get("active_generation_id") is None
                and item.get("expected_active_generation_id") is None
                and verified.get("verification_manifest_sha256") == item["verification_manifest_sha256"]
                and verified.get("adapter_profile_id") == item["adapter_profile_id"],
                "immutable original integrity root or profile differs")
        require(all(verified.get(key) == item[key] for key in
                    ("analysis_profile_id", "search_profile_id")),
                "immutable integrity producer profiles differ")
        for key in ("source_id", "source_watermark_sha256", "adapter_profile_id", "item_count"):
            require(observation.get(key) == item[key], "historical observation identity differs")
        require(type(observation.get("input_bytes")) is int and observation["input_bytes"] >= 0
                and "application_read_bytes" in observation
                and (observation["application_read_bytes"] is None
                     or type(observation["application_read_bytes"]) is int
                        and observation["application_read_bytes"] >= 0),
                "historical observation lacks byte accounting")
        snapshot_ref = row["snapshot_review"]
        # Reuse the original bounded snapshot validator, including its path/status
        # accounting; this reads only saved metadata, never source files.
        reference(snapshot_ref, read, 64 * 1024 * 1024)
        snapshot = RELEASE["read_snapshot_review"](Path(snapshot_ref["path"]),
            snapshot_ref["sha256"], checkpoint, item["adapter_profile_id"])
        cut = observation.get("filesystem_cut")
        require(cut == checkpoint.get("build", {}).get("filesystem_cut") == snapshot.get("filesystem_cut")
                and snapshot.get("item_count") == item["item_count"]
                and cut.get("input_bytes") == observation["input_bytes"],
                "immutable full snapshot or build cut differs")
        prepared[item["source_id"]] = dict(candidate=item, proof=row,
            observation=observation, checkpoint=checkpoint, verification=verified, snapshot=snapshot)
    return prepared


def process_stamp(pid: int) -> str:
    require(type(pid) is int and pid > 1, "live API process is required")
    text = Path(f"/proc/{pid}/stat").read_text()
    return text[text.rindex(")") + 2:].split()[19]


def api_registry(state: dict) -> tuple[Path, tuple[int, str]]:
    require(state.get("LoadState") == "loaded" and state.get("ActiveState") == "active"
            and str(state.get("MainPID", "")).isdecimal(), "live API registry identity is unavailable")
    pid = int(state["MainPID"])
    stamp = process_stamp(pid)
    with Path(f"/proc/{pid}/environ").open("rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    require(len(raw) <= 1024 * 1024, "API environment exceeds metadata bound")
    values = [entry.split(b"=", 1)[1] for entry in raw.split(b"\0")
              if entry.startswith(b"MAINRAG_STORAGE_V2_SOURCE_CUT_ROOT=")]
    require(len(values) <= 1, "API cut registry setting is ambiguous")
    registry = Path(values[0].decode() if values else "/var/lib/mainrag-source-cuts")
    require(registry.is_absolute() and registry.resolve(strict=True) == registry
            and process_stamp(pid) == stamp, "API cut registry or process changed")
    return registry, (pid, stamp)


def live_metadata(candidates: list[dict], query) -> list[dict]:
    require(all(type(item.get(key)) is int and item[key] > 0
                for item in candidates for key in ("source_id", "candidate_generation_id")),
            "immutable query identities are invalid")
    sources = ",".join(str(item["source_id"]) for item in candidates)
    generations = ",".join(str(item["candidate_generation_id"]) for item in candidates)
    sql = f"""BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout='15s'; SET LOCAL lock_timeout='3s';
SELECT COALESCE(jsonb_agg(jsonb_build_object(
 'source_id',s.id,'source_type',s.type,'path',s.path,'config',s.config,
 'generation_id',g.id,'generation_seq',g.generation_seq,'status',g.status::text,
 'producer',g.witness->>'commit_sha','filesystem_cut',g.witness->'filesystem_cut',
 'item_count',g.item_count,'verification_manifest_sha256',g.verification_manifest_sha256,
 'watermark',r.semantic_manifest_sha256,'adapter_profile_id',r.adapter_profile_id,
 'analysis_profile_id',e.analysis_profile_id,'search_profile_id',e.search_profile_id,
 'run_id',r.id,'run_status',r.status,'generation_root_sha256',r.generation_root_sha256,
 'expected_active_generation_id',r.expected_active_generation_id,
 'active_generation_id',l.active_generation_id,'evidence_id',e.id,'commit_sha',e.commit_sha,
 'evidence_manifest_sha256',encode(e.manifest_sha256,'hex'),
 'evidence_digest_matches',e.manifest_sha256=digest(convert_to(e.manifest::text,'UTF8'),'sha256'),
 'snapshot_review',e.manifest->'source_snapshot_review',
 'server_verification_sha256',e.manifest->>'server_verification_sha256'
) ORDER BY s.id),'[]'::jsonb)
FROM sources s JOIN source_generation g ON g.source_id=s.id
JOIN storage_v2_ingest_run r ON r.source_id=s.id AND r.generation_id=g.id
JOIN logical_source l ON l.id=s.id
JOIN storage_v2_release_candidate_evidence e ON e.source_id=s.id AND e.generation_id=g.id
WHERE s.id IN ({sources}) AND g.id IN ({generations}); COMMIT;
"""
    rows = json.loads(query(sql))
    require(isinstance(rows, list) and len(rows) == len(candidates)
            and all(isinstance(row, dict) for row in rows)
            and {row.get("source_id") for row in rows} == {item["source_id"] for item in candidates},
            "fresh immutable witness source set differs")
    return rows


def check_current(record: dict, current: dict, registry: Path) -> Path:
    item, cp, verified, snapshot = (record[key] for key in
        ("candidate", "checkpoint", "verification", "snapshot"))
    require(current.get("source_type") == "fs"
            and isinstance(current.get("path"), str)
            and digest(current["path"].encode()) == snapshot["source_root_sha256"]
            and digest(canonical(current.get("config"))) == snapshot["source_config_sha256"],
            "immutable source registration changed")
    expected = {"source_id": item["source_id"], "generation_id": item["candidate_generation_id"],
        "generation_seq": item["candidate_generation_seq"], "item_count": item["item_count"],
        "watermark": item["source_watermark_sha256"], "producer": cp["commit_sha"],
        "adapter_profile_id": item["adapter_profile_id"], "run_status": "sealed",
        "analysis_profile_id": item["analysis_profile_id"], "search_profile_id": item["search_profile_id"],
        "verification_manifest_sha256": item["verification_manifest_sha256"],
        "filesystem_cut": record["observation"]["filesystem_cut"], "active_generation_id": None,
        "expected_active_generation_id": None, "evidence_id": item["evidence_id"],
        "commit_sha": item["candidate_commit_sha"],
        "evidence_manifest_sha256": item["evidence_manifest_sha256"], "evidence_digest_matches": True,
        "run_id": verified.get("run_id"), "generation_root_sha256": verified.get("generation_root_sha256")}
    require(all(current.get(key) == value for key, value in expected.items())
            and current.get("status") in {"verified", "release_candidate"}
            and type(expected["run_id"]) is int and expected["run_id"] > 0
            and isinstance(expected["generation_root_sha256"], str)
            and re.fullmatch(r"[0-9a-f]{64}", expected["generation_root_sha256"]),
            "immutable sealed generation, producer or accepted evidence changed")
    summary = current.get("snapshot_review")
    require(isinstance(summary, dict)
            and summary.get("review_sha256") == record["proof"]["snapshot_review"]["sha256"]
            and summary.get("source_watermark_sha256") == item["source_watermark_sha256"]
            and summary.get("filesystem_cut") == expected["filesystem_cut"]
            and current.get("server_verification_sha256") == digest(
                json.dumps(verified, sort_keys=True).encode()),
            "accepted evidence does not bind the original snapshot and integrity result")
    observation = record["observation"]
    selected = CUT["read_root"](Path(current["path"]), observation, registry)
    root_sha = snapshot["source_root_sha256"]
    proof = observation["filesystem_cut"]["cut"]
    producer = CUT["PRODUCER"]
    current_descriptor, raw = producer["private_read"](registry / f"current-{root_sha}.json", with_bytes=True)
    history, historical = producer["private_read"](
        registry / "history" / f"{proof['cut_id']}-{root_sha}.json", with_bytes=True)
    require(raw == historical and current_descriptor == history
            and digest(raw) == proof["descriptor_sha256"],
            "current immutable descriptor differs from accepted pinned history")
    return selected


def observe(candidates: list[dict], http_observe, *, bundle: dict | None,
            read, query, api_state, captured_registry: Path | None = None) -> dict[int, dict]:
    prepared = bindings(candidates, bundle, read)
    # Historical JSON validation above finishes before observing mutable adapters.
    # Mutable timestamps therefore do not include that preparation interval.
    observed = {item["source_id"]: http_observe(item["source_id"])
                for item in candidates if not immutable(item)}
    if not prepared:
        return observed
    require(callable(query), "fresh immutable witness metadata reader is required")
    before = api_state() if captured_registry is None else None
    registry, identity = api_registry(before) if before is not None else (captured_registry, None)
    require(registry.is_absolute() and registry.resolve(strict=True) == registry,
            "captured API registry is redirected")
    selected = [record["candidate"] for record in prepared.values()]
    rows = live_metadata(selected, query)
    for current in rows:
        record = prepared[current["source_id"]]
        check_current(record, current, registry)
        value = deepcopy(record["observation"])
        # Preserve the entire historical payload, including its old timestamps
        # and application_read_bytes.  The new proof has a separate real clock.
        value["observation_origin"] = {
            "method": METHOD, "method_sha256": digest(Path(__file__).read_bytes()),
            "observed_at_unix": int(time.time()), "historical_observation": record["proof"]["observation"],
            "integrity": record["proof"]["integrity"], "snapshot_review": record["proof"]["snapshot_review"],
            "accepted_audit": bundle["accepted_audit"], "accepted_acceptance": bundle["accepted_acceptance"],
            "api_registry": str(registry), "api_process_identity": identity,
            "source_bodies_read": 0, "new_full_hash_runs": 0,
        }
        observed[current["source_id"]] = value
    require(canonical(rows) == canonical(live_metadata(selected, query)),
            "immutable registration or sealed witness changed during observation")
    if before is not None:
        require(api_state() == before and process_stamp(identity[0]) == identity[1],
                "API process changed during immutable observation")
    return observed
