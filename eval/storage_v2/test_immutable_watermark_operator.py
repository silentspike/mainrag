"""Public fixtures for accepted immutable watermark reuse and operator routing."""
from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import runpy
from types import SimpleNamespace
import tempfile
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def module(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"),
                                                ROOT / "ops/storage-v2" / name)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


ACTIVATION = module("activation-set.py")
DELTA = module("final-delta.py")
H = ACTIVATION.IMMUTABLE
SCOPE = runpy.run_path(str(ROOT / "ops/storage-v2/fs_scope.py"))


class ImmutableWatermarkOperatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.origin = self.base / "origin"
        self.root = self.origin / "records"
        self.registry = self.base / "registry"
        cut_id = str(uuid.uuid4())
        self.snapshot = self.registry / "views" / cut_id
        self.view = self.snapshot / "records"
        for directory in (self.root, self.view, self.registry / "history"):
            directory.mkdir(parents=True, exist_ok=True)
        for directory in (self.root, self.view):
            (directory / "item.txt").write_bytes(b"fixture\n")
        self.root_hash = H["digest"](str(self.root).encode())
        descriptor = {"format": "mainrag.fs-read-cut.v1", "cut_id": cut_id,
            "source_root_sha256": self.root_hash, "registered_root": str(self.root),
            "origin_subvolume": str(self.origin), "snapshot_root": str(self.snapshot),
            "read_root": str(self.view), "snapshot_uuid": str(uuid.uuid4()),
            "origin_uuid": str(uuid.uuid4()), "captured_at_unix": 100}
        raw = H["CUT"]["PRODUCER"]["canonical"](descriptor)
        self.history = self.registry / "history" / f"{cut_id}-{self.root_hash}.json"
        self.current = self.registry / f"current-{self.root_hash}.json"
        for path in (self.history, self.current):
            path.write_bytes(raw)
            path.chmod(0o600)
        cut = {"cut": {key: descriptor[key] for key in ("format", "cut_id",
            "source_root_sha256", "snapshot_uuid", "origin_uuid", "captured_at_unix")},
            "fixture_sha256": "a" * 64, "item_count": 1, "input_bytes": 8}
        cut["cut"]["descriptor_sha256"] = H["digest"](raw)
        profile = SCOPE["scope_profile"]("unfiltered", True)
        digest = hashlib.sha256(b"mainrag.storage-v2.source-watermark.v1\0")
        for part in ("fs", str(self.root), profile, cut["fixture_sha256"]):
            encoded = part.encode()
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        self.item = {"source_id": 7, "candidate_generation_id": 41,
            "candidate_generation_seq": 3, "candidate_commit_sha": "b" * 40,
            "expected_active_generation_id": None, "evidence_id": str(uuid.uuid4()),
            "evidence_manifest_sha256": "c" * 64, "source_watermark_sha256": digest.hexdigest(),
            "item_count": 1, "verification_manifest_sha256": "d" * 64,
            "adapter_profile_id": profile, "analysis_profile_id": "fixture-analysis",
            "search_profile_id": "fixture-search", "gold_suite_sha256": "e" * 64}
        self.observation = {"source_id": 7, "adapter_profile_id": profile, "item_count": 1,
            "input_bytes": 8, "application_read_bytes": 8, "observed_at_unix": 101,
            "source_watermark_sha256": self.item["source_watermark_sha256"], "filesystem_cut": cut}
        self.cp = {"source_id": 7, "generation_id": 41, "generation_seq": 3,
            "commit_sha": "f" * 40, "active_generation_id": None, "item_count": 1,
            "source_watermark_sha256": self.item["source_watermark_sha256"],
            "build": {"filesystem_cut": cut}}
        self.verified = {**self.cp, "status": "verified", "adapter_profile_id": profile,
            "analysis_profile_id": "fixture-analysis", "search_profile_id": "fixture-search",
            "run_id": 61, "generation_root_sha256": "f" * 64,
            "verification_manifest_sha256": self.item["verification_manifest_sha256"],
            "checks": {name: "PASS" for name in H["CHECKS"]}}
        self.review = {"schema_version": "mainrag.storage-v2.source-snapshot-review.v1",
            "source_id": 7, "source_type": "fs", "source_watermark_sha256":
            self.item["source_watermark_sha256"], "adapter_profile_id": profile,
            "item_count": 1, "captured_at_unix": 102, "source_root_sha256": self.root_hash,
            "source_config_sha256": H["digest"](H["canonical"]({})), "filesystem_scope": None,
            "filesystem_cut": cut, "paths": {}, "status_counts": {}}
        self.audit = {"schema_version": "mainrag.storage-v2.candidate-aggregate-audit.v2",
            "persisted_candidate_set_complete": True, "candidate_set": [self.item],
            "candidate_set_sha256": H["digest"](H["canonical"]([self.item]))}
        audit_ref = self.save("audit", self.audit)
        self.acceptance = {"schema_version": "mainrag.storage-v2.aggregate-acceptance.v1",
            "status": "PASS", "persisted_audit_sha256": audit_ref["sha256"],
            "candidate_set_sha256": self.audit["candidate_set_sha256"], "source_count": 1,
            "current_source_watermarks": [{"source_id": 7,
                "watermark_sha256": self.item["source_watermark_sha256"], "observed_at_unix": 103}],
            "external_gates": {name: {"status": "PASS", "evidence_sha256": "1" * 64}
                               for name in H["GATES"]}}
        row = {"source_id": 7, "observation": self.save("observation", self.observation),
            "integrity": self.save("integrity", {"checkpoint": self.cp, "verification": self.verified}),
            "snapshot_review": self.save("snapshot", self.review)}
        self.bundle = {"schema_version": "mainrag.storage-v2.immutable-watermark-bindings.v1",
            "accepted_audit": audit_ref, "accepted_acceptance": self.save("acceptance", self.acceptance),
            "sources": [row]}
        self.live = {"source_id": 7, "source_type": "fs", "path": str(self.root), "config": {},
            "generation_id": 41, "generation_seq": 3, "status": "release_candidate",
            "producer": self.cp["commit_sha"], "filesystem_cut": cut, "item_count": 1,
            "verification_manifest_sha256": self.item["verification_manifest_sha256"],
            "watermark": self.item["source_watermark_sha256"], "adapter_profile_id": profile,
            "analysis_profile_id": "fixture-analysis", "search_profile_id": "fixture-search",
            "run_id": 61, "run_status": "sealed", "generation_root_sha256": "f" * 64,
            "expected_active_generation_id": None, "active_generation_id": None,
            "evidence_id": self.item["evidence_id"], "commit_sha": self.item["candidate_commit_sha"],
            "evidence_manifest_sha256": self.item["evidence_manifest_sha256"],
            "evidence_digest_matches": True, "snapshot_review": {
                "review_sha256": row["snapshot_review"]["sha256"],
                "source_watermark_sha256": self.item["source_watermark_sha256"], "filesystem_cut": cut},
            "server_verification_sha256": H["digest"](json.dumps(self.verified, sort_keys=True).encode())}
        self.state = {"LoadState": "loaded", "ActiveState": "active", "MainPID": "1234"}
        self.queries = []
        self.kernel_read_only = True

    def save(self, name, value):
        path = self.base / (name + ".json")
        raw = H["canonical"](value)
        path.write_bytes(raw)
        path.chmod(0o600)
        return {"path": str(path), "sha256": H["digest"](raw)}

    def query(self, sql):
        self.queries.append(sql)
        self.assertIn("READ ONLY", sql)
        return json.dumps([self.live])

    def environment(self):
        stack = ExitStack()
        cut = H["CUT"]
        original_read = cut["read_root"]
        original_private = cut["PRODUCER"]["private_read"]
        # The fixture substitutes only authority/kernel observations. The actual
        # read_root descriptor, canonical path and manifest validators run.
        stack.enter_context(patch.dict(cut, {"read_root": lambda *a: original_read(*a, _owner=os.getuid())}))
        stack.enter_context(patch.dict(cut["PRODUCER"], {
            "private_read": lambda p, owner=0, **kw: original_private(p, os.getuid(), **kw)}))
        def inspect_kernel(snapshot, proof, owner):
            self.assertEqual(snapshot, self.snapshot)
            self.assertEqual(proof, self.observation["filesystem_cut"]["cut"])
            if not self.kernel_read_only:
                raise RuntimeError("cut kernel identity is not immutable")
        stack.enter_context(patch.dict(original_read.__globals__, {"inspect_kernel": inspect_kernel}))
        stack.enter_context(patch.dict(H["observe"].__globals__, {
            "api_registry": lambda _: (self.registry, (1234, "fixture-start")),
            "process_stamp": lambda _: "fixture-start"}))
        original_open = Path.open
        def metadata_open(path, *args, **kwargs):
            if path.is_relative_to(self.root) or path.is_relative_to(self.view):
                raise AssertionError("source body was opened during metadata reuse")
            return original_open(path, *args, **kwargs)
        stack.enter_context(patch.object(Path, "open", metadata_open))
        return stack

    def observe(self, **kwargs):
        options = dict(bundle=self.bundle, read=ACTIVATION.read_private,
                       query=self.query, api_state=lambda: self.state)
        options.update(kwargs)
        return H["observe"]([self.item], lambda _: self.fail("immutable HTTP replay"), **options)

    def test_original_payload_is_preserved_with_fresh_metadata_and_no_body_reads(self):
        with self.environment(), patch.object(H["time"], "time", return_value=5000):
            observed = self.observe()[7]
        origin = observed.pop("observation_origin")
        self.assertEqual(observed, self.observation)
        self.assertEqual(origin["observed_at_unix"], 5000)
        self.assertEqual(origin["source_bodies_read"], 0)
        self.assertEqual(len(self.queries), 2)
        self.assertEqual(origin["historical_observation"], self.bundle["sources"][0]["observation"])

    def test_missing_proofs_failed_integrity_and_unaccepted_candidate_never_fallback(self):
        with self.assertRaisesRegex(RuntimeError, "no scan fallback"):
            self.observe(bundle=None)
        for field in ("candidate_commit_sha", "candidate_generation_id", "evidence_id"):
            old = self.item[field]
            self.item[field] = "different"
            with self.assertRaisesRegex(RuntimeError, "accepted candidate"):
                self.observe()
            self.item[field] = old
        integrity = {"checkpoint": self.cp, "verification": deepcopy(self.verified)}
        integrity["verification"]["checks"][next(iter(H["CHECKS"]))] = "FAIL"
        self.bundle["sources"][0]["integrity"] = self.save("failed-integrity", integrity)
        with self.assertRaisesRegex(RuntimeError, "original integrity"):
            self.observe()
        self.assertEqual(self.queries, [])

    def test_registration_generation_producer_profile_and_active_pointer_drift_refuse(self):
        cases = {"config": {"changed": True}, "path": str(self.origin), "producer": "a" * 40,
            "generation_id": 999, "generation_seq": 99, "item_count": 2,
            "run_status": "building", "active_generation_id": 41,
            "expected_active_generation_id": 41, "adapter_profile_id": "mutable-profile",
            "generation_root_sha256": "0" * 64, "evidence_digest_matches": False,
            "evidence_manifest_sha256": "0" * 64, "status": "building"}
        with self.environment():
            for key, bad in cases.items():
                with self.subTest(field=key):
                    old = self.live[key]
                    self.live[key] = bad
                    with self.assertRaises(RuntimeError):
                        self.observe()
                    self.live[key] = old

    def test_current_history_kernel_snapshot_and_registry_process_drift_refuse(self):
        with self.environment():
            raw = self.current.read_bytes()
            self.current.write_bytes(raw + b" ")
            with self.assertRaisesRegex(RuntimeError, "pinned history"):
                self.observe()
            self.current.write_bytes(raw)
            self.kernel_read_only = False
            with self.assertRaisesRegex(RuntimeError, "kernel"):
                self.observe()
            self.kernel_read_only = True
            with patch.dict(H["observe"].__globals__, {"process_stamp": lambda _: "changed"}):
                with self.assertRaisesRegex(RuntimeError, "API process changed"):
                    self.observe()
            self.history.unlink()
            with self.assertRaises(OSError):
                self.observe()

    def test_mutable_http_remains_once_per_source_and_its_timestamp_is_not_relabelled(self):
        candidate = {"source_id": 19, "source_watermark_sha256": "9" * 64,
                     "adapter_profile_id": "fixture-mutable", "item_count": 2}
        raw = {"source_id": 19, "source_watermark_sha256": "9" * 64,
               "adapter_profile_id": "fixture-mutable", "item_count": 2}
        class Response(io.BytesIO):
            pass
        opener = SimpleNamespace(open=lambda request, timeout: Response(json.dumps(raw).encode()))
        times = iter([100, 450])
        with patch.object(ACTIVATION.urllib.request, "build_opener", return_value=opener), \
                patch.object(ACTIVATION.time, "time", side_effect=lambda: next(times)):
            rows = ACTIVATION.verify_current_api_watermarks("http://127.0.0.1:3001", "fixture-token", [candidate])
        self.assertEqual(rows[0]["observed_at_unix"], 100)
        receipt = {"schema_version": "mainrag.storage-v2.quiesced-watermarks.v1",
            "status": "PASS_LIVE_WATERMARKS_BEFORE_API_STOP", "candidate_set_sha256":
            ACTIVATION.sha256(ACTIVATION.canonical([candidate])), "installed_binary_sha256": "1" * 64,
            "reader_pid": 1234, "source_count": 1, "captured_at_unix": 450, "watermarks": rows}
        with self.assertRaisesRegex(RuntimeError, "observation differs"):
            ACTIVATION.verify_quiesced_watermarks(receipt, [candidate], "1" * 64, 450)

    def test_registry_comes_from_actual_api_environment_and_rejects_ambiguity_or_pid_drift(self):
        original_open = Path.open
        payload = b"UNRELATED=not-exported\0MAINRAG_STORAGE_V2_SOURCE_CUT_ROOT=" + str(self.registry).encode() + b"\0"
        def metadata_open(path, *args, **kwargs):
            if path == Path("/proc/1234/environ"):
                return io.BytesIO(payload)
            return original_open(path, *args, **kwargs)
        globals_ = H["api_registry"].__globals__
        with patch.object(Path, "open", metadata_open), \
                patch.dict(os.environ, {"MAINRAG_STORAGE_V2_SOURCE_CUT_ROOT": "/unrelated-caller-setting"}), \
                patch.dict(globals_, {"process_stamp": lambda _: "same-start"}):
            self.assertEqual(H["api_registry"](self.state), (self.registry, (1234, "same-start")))
            original = payload
            payload += b"MAINRAG_STORAGE_V2_SOURCE_CUT_ROOT=/another-registry\0"
            with self.assertRaisesRegex(RuntimeError, "ambiguous"):
                H["api_registry"](self.state)
            payload = original
            stamps = iter(["old-start", "new-start"])
            with patch.dict(globals_, {"process_stamp": lambda _: next(stamps)}):
                with self.assertRaisesRegex(RuntimeError, "process changed"):
                    H["api_registry"](self.state)

    def test_activation_live_and_quiesced_use_same_proofs_with_original_stop_and_age_gates(self):
        with self.environment():
            rows = ACTIVATION.verify_current_api_watermarks("http://127.0.0.1:3001", "fixture-token",
                [self.item], immutable_bindings=self.bundle, query=self.query, api_state=lambda: self.state)
            now = rows[0]["observed_at_unix"]
            bundle_ref = self.save("bindings", self.bundle)
            receipt = {"schema_version": "mainrag.storage-v2.quiesced-watermarks.v1",
                "status": "PASS_LIVE_WATERMARKS_BEFORE_API_STOP",
                "candidate_set_sha256": ACTIVATION.sha256(ACTIVATION.canonical([self.item])),
                "installed_binary_sha256": "1" * 64, "reader_pid": 1234,
                "source_count": 1, "captured_at_unix": now, "watermarks": rows,
                "immutable_watermarks_sha256": bundle_ref["sha256"]}
            receipt_ref = self.save("quiesced", receipt)
            args = SimpleNamespace(quiesced_watermarks=Path(receipt_ref["path"]),
                quiesced_watermarks_sha256=receipt_ref["sha256"],
                immutable_watermarks=Path(bundle_ref["path"]),
                immutable_watermarks_sha256=bundle_ref["sha256"], database="fixture", local_postgres=False)
            with patch.object(ACTIVATION, "psql", side_effect=lambda *a, **kw: self.query(a[1])), \
                    patch.object(ACTIVATION, "require_stopped_api") as stopped:
                ACTIVATION.verify_activation_watermarks(args, [self.item], {"installed_binary_sha256": "1" * 64})
                self.assertEqual(stopped.call_count, 2)
            with self.assertRaisesRegex(RuntimeError, "stale"):
                ACTIVATION.verify_quiesced_watermarks(receipt, [self.item], "1" * 64, now + 301)

    def test_final_delta_retains_candidate_and_historical_accounting_with_metadata_origin(self):
        bundle_ref = self.save("bindings", self.bundle)
        args = SimpleNamespace(immutable_watermarks=Path(bundle_ref["path"]),
            immutable_watermarks_sha256=bundle_ref["sha256"], database="fixture", local_postgres=False,
            api_url="http://127.0.0.1:3001")
        with self.environment(), patch.object(DELTA.ACTIVATION, "IMMUTABLE", H), \
                patch.object(DELTA.ACTIVATION, "api_unit_state", return_value=self.state), \
                patch.object(DELTA.ACTIVATION, "psql", side_effect=lambda *a, **kw: self.query(a[1])), \
                patch.object(DELTA, "observation", side_effect=AssertionError("immutable HTTP replay")):
            observed = DELTA.observe_sources(args, [self.item], "fixture-token")
        generation = {"generation_id": 41, "generation_seq": 3, "status": "release_candidate",
            "commit_sha": self.item["candidate_commit_sha"], "evidence_id": self.item["evidence_id"],
            "source_watermark_sha256": self.item["source_watermark_sha256"],
            "adapter_profile_id": self.item["adapter_profile_id"]}
        rows = [{"source_id": 7, "active_generation_id": None, "generations": [generation]}]
        plan = DELTA.make_plan(self.audit, "1" * 64, rows, observed, "2" * 40, 5000,
                              "3" * 40, "4" * 64, "5" * 64, "6" * 64)
        self.assertEqual(plan["sources"][0]["action"], "RETAIN")
        self.assertEqual(plan["sources"][0]["application_read_bytes"], 8)
        self.assertEqual(plan["sources"][0]["observation_origin"]["source_bodies_read"], 0)


if __name__ == "__main__":
    unittest.main()
