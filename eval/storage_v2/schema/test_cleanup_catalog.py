"""Actual PostgreSQL cleanup identities, caller edges and retained ownership."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from unittest.mock import patch

from eval.storage_v2.schema import test_active_set_search as base


SPEC = importlib.util.spec_from_file_location(
    "cleanup_catalog_manifest", base.ROOT / "ops/storage-v2/cleanup-manifest.py")
MANIFEST = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MANIFEST)
CAPTURE = MANIFEST.CAPTURE


class CleanupCatalogTests(base.ActiveSetSearchTests):
    # Reuse the owned disposable cluster, not the unrelated active-set suite.
    test_active_set_requires_receipt_and_reads_authorized_sources = None

    def capture(self):
        with patch.dict(os.environ, {"PGHOST": str(self.socket)}):
            return CAPTURE.catalog(self.database, False)

    def test_definitions_authority_external_edges_and_native_retention(self):
        self.sql("""
CREATE TABLE cleanup_legacy_fixture(id BIGINT GENERATED ALWAYS AS IDENTITY, value TEXT);
CREATE INDEX cleanup_legacy_fixture_value ON cleanup_legacy_fixture(value);
CREATE FUNCTION cleanup_routine_fixture() RETURNS INTEGER
LANGUAGE SQL STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$SELECT 1$$;
CREATE SCHEMA cleanup_external_fixture;
CREATE VIEW cleanup_external_fixture.routine_use AS SELECT cleanup_routine_fixture();
CREATE VIEW cleanup_external_fixture.relation_use AS SELECT * FROM cleanup_legacy_fixture;
CREATE TYPE cleanup_enum_fixture AS ENUM ('before','after');
CREATE TABLE cleanup_external_fixture.type_use(value cleanup_enum_fixture);
CREATE FUNCTION cleanup_string_caller() RETURNS BIGINT LANGUAGE plpgsql AS $$
BEGIN RETURN (SELECT count(*) FROM cleanup_legacy_fixture); END $$;
CREATE FUNCTION cleanup_trigger_fixture() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN RETURN NEW; END $$;
CREATE TRIGGER cleanup_trigger BEFORE INSERT ON cleanup_legacy_fixture
FOR EACH ROW EXECUTE FUNCTION cleanup_trigger_fixture();
ALTER TABLE cleanup_legacy_fixture ENABLE ROW LEVEL SECURITY;
CREATE POLICY cleanup_policy ON cleanup_legacy_fixture USING (value='before');
""")
        before = self.capture()
        functions = {row['name']: row for row in before['functions']}
        relations = {row['name']: row for row in before['relations']}
        function_oid = functions['cleanup_routine_fixture']['oid']
        relation_oid = relations['cleanup_legacy_fixture']['oid']
        type_oid = self.sql("SELECT 'cleanup_enum_fixture'::regtype::oid")
        for target in (function_oid, relation_oid, type_oid):
            self.assertTrue(any(str(row['referenced_object_oid']) == str(target)
                                for row in before['dependencies']))
        self.assertIn({'function_oid': functions['cleanup_string_caller']['oid'],
                       'relation_oid': relation_oid}, before['routine_relation_references'])
        routine = functions['cleanup_routine_fixture']
        self.assertTrue(routine['security_definer'])
        self.assertEqual(routine['configuration'], ['search_path=pg_catalog, public'])
        self.sql("""
ALTER FUNCTION cleanup_routine_fixture() SECURITY INVOKER;
REVOKE EXECUTE ON FUNCTION cleanup_routine_fixture() FROM PUBLIC;
ALTER POLICY cleanup_policy ON cleanup_legacy_fixture USING (value='after');
CREATE OR REPLACE FUNCTION cleanup_trigger_fixture() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'changed'; END $$;
DROP INDEX cleanup_legacy_fixture_value;
CREATE INDEX cleanup_legacy_fixture_value ON cleanup_legacy_fixture(lower(value));
""")
        after = self.capture()
        functions_after = {row['name']: row for row in after['functions']}
        self.assertFalse(functions_after['cleanup_routine_fixture']['security_definer'])
        self.assertNotEqual(routine['acl'], functions_after['cleanup_routine_fixture']['acl'])
        self.assertNotEqual(functions['cleanup_trigger_fixture']['definition_sha256'],
                            functions_after['cleanup_trigger_fixture']['definition_sha256'])
        for field, name in (('policies','cleanup_policy'),
                            ('indexes','cleanup_legacy_fixture_value')):
            old = next(row for row in before[field] if row['name'] == name)
            new = next(row for row in after[field] if row['name'] == name)
            self.assertNotEqual(old, new)
        # A native identity sequence and native index columns retain their
        # parent protection even without a storage_v2 name prefix.
        sequence = next(row for row in after['relations']
                        if row.get('owned_by_relation_oid') == relations['content_body']['oid'])
        native_index = next(row for row in after['indexes']
                            if row['relation_oid'] == relations['content_body']['oid'])
        native_index_column = next(row for row in after['columns']
                                   if row['relation_oid'] == native_index['oid'])
        inventory = dict(schema_version='mainrag.storage-v2.cleanup-catalog.v1',
                         status='OBSERVED_ONLY', catalog=after, qdrant=None,
                         runtime_search=None, exports=None,
                         before_state_sha256=hashlib.sha256(CAPTURE.canonical(after)).hexdigest())
        for kind, identity in [('relation', sequence['oid']),
                               ('column', (native_index_column['relation_oid'],
                                           native_index_column['number']))]:
            if kind == 'relation':
                after['exact_rows'][sequence['name']] = 1
            inventory['before_state_sha256'] = hashlib.sha256(CAPTURE.canonical(after)).hexdigest()
            key = MANIFEST.object_key(kind, identity if isinstance(identity, tuple) else (identity,))
            decision = dict(key=key, disposition='DELETE', reason='fixture only', authority='fixture only')
            with self.subTest(kind=kind), self.assertRaisesRegex(RuntimeError, 'cannot be deleted'):
                MANIFEST.draft(inventory, 'c'*64, {key: decision})
        print('PASS: actual catalog binds definitions, authority, cross-schema callers and native owned objects', flush=True)
