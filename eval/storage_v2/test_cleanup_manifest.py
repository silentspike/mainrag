"""Safety boundaries for protected cleanup disposition drafts."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[2] / "ops/storage-v2/cleanup-manifest.py"
SPEC = importlib.util.spec_from_file_location("cleanup_manifest", MODULE_PATH)
MANIFEST = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MANIFEST)


def fixture() -> dict:
    catalog = {field: [] for _, field, _ in MANIFEST.KINDS}
    catalog.update({
        "relations": [{"oid": 7, "name": "fixture", "total_bytes": 12}],
        "outbox_classes": [{"action": "upsert", "status": "pending",
                            "row_count": 2, "min_id": 1, "max_id": 2}],
        "dependencies": [], "pointer_set_sha256": "a" * 64,
        "reachability": None, "exact_rows": {},
    })
    return {"schema_version": "mainrag.storage-v2.cleanup-catalog.v1",
            "status": "OBSERVED_ONLY", "catalog": catalog,
            "qdrant": None, "runtime_search": None,
            "before_state_sha256": hashlib.sha256(
                MANIFEST.CAPTURE.canonical(catalog)).hexdigest(),
            "operator_sha256": "b" * 64}


class CleanupManifestTests(unittest.TestCase):
    def test_draft_requires_explicit_dispositions_and_stays_blocked(self) -> None:
        inventory = fixture()
        objects = MANIFEST.observed_objects(inventory)
        self.assertEqual(len(objects), 2)
        selected = objects[0]["key"]
        decision = {"key": selected, "disposition": "DELETE",
                    "reason": "review fixture", "authority": "fixture only"}
        draft = MANIFEST.draft(inventory, "c" * 64, {selected: decision})
        self.assertEqual(draft["status"], "DRAFT_BLOCKED")
        self.assertFalse(draft["apply_allowed"])
        self.assertEqual([item["disposition"] for item in draft["objects"]],
                         ["DELETE", "UNREVIEWED"])
        self.assertIn("OBJECT_DISPOSITIONS_INCOMPLETE", draft["blockers"])
        inventory["before_state_sha256"] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "binding"):
            MANIFEST.draft(inventory, "c" * 64, {})

    def test_relation_delete_requires_exact_count_and_keeps_mapping(self) -> None:
        inventory = fixture()
        relation = next(item for item in MANIFEST.observed_objects(inventory)
                        if item["kind"] == "relation")
        delete = {"key": relation["key"], "disposition": "DELETE",
                  "reason": "fixture", "authority": "fixture"}
        with self.assertRaisesRegex(RuntimeError, "exact row count"):
            MANIFEST.draft(inventory, "c" * 64, {relation["key"]: delete})
        inventory["catalog"]["exact_rows"] = {"fixture": 3}
        inventory["before_state_sha256"] = hashlib.sha256(
            MANIFEST.CAPTURE.canonical(inventory["catalog"])).hexdigest()
        draft = MANIFEST.draft(inventory, "c" * 64, {relation["key"]: delete})
        selected = next(item for item in draft["objects"] if item["key"] == relation["key"])
        self.assertEqual(selected["observed"]["exact_row_count"], 3)
        inventory["catalog"]["relations"][0]["name"] = "legacy_hit_mapping"
        inventory["catalog"]["exact_rows"] = {"legacy_hit_mapping": 3}
        inventory["before_state_sha256"] = hashlib.sha256(
            MANIFEST.CAPTURE.canonical(inventory["catalog"])).hexdigest()
        with self.assertRaisesRegex(RuntimeError, "cannot be deleted"):
            MANIFEST.draft(inventory, "c" * 64, {relation["key"]: delete})

    def test_private_input_and_exact_catalog_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "private"
            root.mkdir(mode=0o700)
            inventory = fixture()
            source = root / "catalog.json"
            source.write_bytes(MANIFEST.CAPTURE.canonical(inventory))
            source.chmod(0o600)
            read, source_sha = MANIFEST.private_read(source, MANIFEST.CATALOG_LIMIT)
            self.assertEqual(read, inventory)
            key = MANIFEST.observed_objects(inventory)[0]["key"]
            decisions = root / "decisions.json"
            decisions.write_text(json.dumps({
                "schema_version": "mainrag.storage-v2.cleanup-decisions.v1",
                "catalog_sha256": source_sha,
                "objects": [{"key": key, "disposition": "KEEP",
                             "reason": "fixture", "authority": "fixture"}],
            }))
            decisions.chmod(0o600)
            self.assertEqual(MANIFEST.decisions_for(decisions, source_sha, {key})[key]
                             ["disposition"], "KEEP")
            with self.assertRaisesRegex(RuntimeError, "exact catalog"):
                MANIFEST.decisions_for(decisions, "0" * 64, {key})
            source.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, "private"):
                MANIFEST.private_read(source, MANIFEST.CATALOG_LIMIT)
            source.chmod(0o600)
            link = root / "link.json"
            link.symlink_to(source)
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                MANIFEST.private_read(link, MANIFEST.CATALOG_LIMIT)


if __name__ == "__main__":
    unittest.main()
