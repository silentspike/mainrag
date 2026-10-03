#!/usr/bin/env python3
"""Exact global native mark/sweep; pack bytes require the separate maintenance tool.

Generation membership, durable producer identities/receipts, intelligence,
current and historic hits, external foreign keys and explicit export roots are
retention roots. Reference counters are never GC authority.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import shutil
import time
from pathlib import Path

SPEC = importlib.util.spec_from_file_location('gc_cleanup', Path(__file__).with_name('cleanup-apply.py'))
A = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(A)
HEX = A.HEX
TARGETS = ('artifact_version', 'occurrence', 'retrieval_view',
           'storage_v2_search_document', 'content_node', 'content_body',
           'storage_v2_legacy_rank_payload')
# The owner determines whether a dependent row is retained. Unknown incoming
# foreign-key tables are roots, not implicitly approved deletion targets.
LINKS = {
    'content_node_edge': [('parent_node_id', 'content_node')],
    'view_component': [('view_id', 'retrieval_view')],
    'storage_v2_search_view_document': [('view_id', 'retrieval_view')],
    'occurrence_edge': [('from_occurrence_id', 'occurrence'), ('to_occurrence_id', 'occurrence')],
    'occurrence_scope': [('occurrence_id', 'occurrence')],
    'storage_v2_occurrence_score_component': [('occurrence_id', 'occurrence')],
    'storage_v2_lexical_segment': [('occurrence_id', 'occurrence')],
    'storage_v2_compact_lexical_block': [('occurrence_id', 'occurrence')],
    'storage_v2_legacy_lexical_segment': [('occurrence_id', 'occurrence')],
    'storage_v2_legacy_rank_binding': [('occurrence_id', 'occurrence')],
    'storage_v2_search_posting': [('document_id', 'storage_v2_search_document')],
    'storage_v2_compact_posting_block': [('document_id', 'storage_v2_search_document')],
}


def digest(value):
    return A.digest(value)


def operator_digest():
    directory = Path(__file__).parent
    return digest({name: hashlib.sha256((directory/name).read_bytes()).hexdigest()
                   for name in ('native-gc.py', 'cleanup-apply.py', 'cleanup-manifest.py', 'cleanup-plan.py',
                     '../../migrations/136_storage_v2_gc_body_identity.sql',
                     '../../api/src/services/pack_maintenance.rs', '../../api/src/bin/mainrag-pack-maintenance.rs')})


def sql(database, privileged, statement, error_path=None):
    command = (['sudo', '-n', '-u', 'postgres'] if privileged else []) + [
        'psql', '-X', '--no-psqlrc', '-qAt', '--set=ON_ERROR_STOP=1', '--dbname', database]
    env = os.environ.copy()
    env['PGAPPNAME'] = 'mainrag-storage-v2-native-gc'
    result = subprocess.run(command, input=statement, text=True, capture_output=True, env=env)
    if result.returncode:
        if error_path is not None:
            A.C.private_create(error_path, {'status': 'FAILED_OR_OUTCOME_UNKNOWN', 'stderr': result.stderr})
        raise RuntimeError('native GC failed or outcome is unknown; reconcile the database receipt')
    return result.stdout.strip()


def retention(value, evidence_root):
    if value.get('schema_version') != 'mainrag.storage-v2.gc-retention.v1' \
            or value.get('preserve_all_generations') is not True \
            or not isinstance(value.get('authority'), str) or not value['authority'].strip() \
            or not isinstance(value.get('roots'), list) or len(value['roots']) > 100000 \
            or not isinstance(value.get('proofs'), list) or not value['proofs']:
        raise RuntimeError('complete protected export retention and its proofs are required')
    seen = set()
    for root in value['roots']:
        if not isinstance(root, dict) or set(root) != {'table', 'id'} \
                or root['table'] not in TARGETS or type(root['id']) is not int or root['id'] <= 0 \
                or (root['table'], root['id']) in seen:
            raise RuntimeError('protected export root identity is invalid or repeated')
        seen.add((root['table'], root['id']))
    for proof in value['proofs']:
        if not isinstance(proof, dict) or set(proof) != {'file', 'sha256'} \
                or not isinstance(proof['file'], str) or Path(proof['file']).name != proof['file']:
            raise RuntimeError('retention proof reference is invalid')
        A.read_bound(evidence_root / proof['file'], proof['sha256'])


def graph_sql(roots):
    targets = ','.join(A.literal(name) for name in TARGETS)
    internal = ','.join(A.literal(name) for name in (*TARGETS, *LINKS))
    explicit = ''.join(f"INSERT INTO gc_root VALUES({A.literal(r['table'])},{r['id']}) ON CONFLICT DO NOTHING;\n"
                       for r in roots)
    owners = ','.join(f'({A.literal(table)},{A.literal(column)},{A.literal(target)})'
                      for table, columns in LINKS.items() for column, target in columns)
    return f"""
CREATE TEMP TABLE gc_target(name TEXT PRIMARY KEY);
INSERT INTO gc_target SELECT unnest(ARRAY[{targets}]::TEXT[]);
CREATE TEMP TABLE gc_owner(table_name TEXT,column_name TEXT,target_name TEXT);
INSERT INTO gc_owner VALUES {owners};
CREATE TEMP TABLE gc_root(kind TEXT,id BIGINT,PRIMARY KEY(kind,id));
CREATE TEMP TABLE gc_edge(from_kind TEXT,from_id BIGINT,to_kind TEXT,to_id BIGINT);
-- Unfinished publication is retained recovery state, never orphan GC input.
INSERT INTO gc_root SELECT 'content_body',body.id FROM content_body body
    JOIN content_pack pack ON pack.id=body.pack_id WHERE pack.status IN ('candidate','verified');
{explicit}
DO $graph$ DECLARE fk RECORD; owner RECORD; predicate TEXT;
BEGIN
 FOR fk IN SELECT con.oid,con.conrelid,con.confrelid,
     src_ns.nspname AS src_schema,src.relname AS src_name,
     dst.relname AS dst_name,con.conkey,con.confkey
   FROM pg_constraint con JOIN pg_class src ON src.oid=con.conrelid
   JOIN pg_namespace src_ns ON src_ns.oid=src.relnamespace
   JOIN pg_class dst ON dst.oid=con.confrelid
   JOIN gc_target target ON target.name=dst.relname AND dst.relnamespace='public'::regnamespace
   WHERE con.contype='f' ORDER BY con.oid LOOP
  SELECT string_agg(format('s.%I=d.%I',sa.attname,da.attname),' AND ' ORDER BY pair.n)
    INTO predicate FROM unnest(fk.conkey,fk.confkey) WITH ORDINALITY pair(s,d,n)
    JOIN pg_attribute sa ON sa.attrelid=fk.conrelid AND sa.attnum=pair.s
    JOIN pg_attribute da ON da.attrelid=fk.confrelid AND da.attnum=pair.d;
  IF fk.src_schema='public' AND fk.src_name IN ({targets}) THEN
   EXECUTE format('INSERT INTO gc_edge SELECT %L,s.id,%L,d.id FROM public.%I s JOIN public.%I d ON %s',
       fk.src_name,fk.dst_name,fk.src_name,fk.dst_name,predicate);
  ELSIF fk.src_schema='public' AND fk.src_name IN ({internal}) THEN
   FOR owner IN SELECT * FROM gc_owner WHERE table_name=fk.src_name LOOP
    EXECUTE format('INSERT INTO gc_edge SELECT %L,s.%I,%L,d.id FROM public.%I s JOIN public.%I d ON %s',
        owner.target_name,owner.column_name,fk.dst_name,fk.src_name,fk.dst_name,predicate);
   END LOOP;
  ELSE
   EXECUTE format('INSERT INTO gc_root SELECT %L,d.id FROM %I.%I s JOIN public.%I d ON %s ON CONFLICT DO NOTHING',
       fk.dst_name,fk.src_schema,fk.src_name,fk.dst_name,predicate);
  END IF;
 END LOOP;
 -- Retaining an artifact retains every one of its derived occurrences.
 INSERT INTO gc_edge SELECT 'artifact_version',artifact_version_id,'occurrence',id FROM occurrence;
END $graph$;
CREATE INDEX ON gc_edge(from_kind,from_id);
CREATE TEMP TABLE gc_mark AS WITH RECURSIVE walk(kind,id) AS (
 SELECT kind,id FROM gc_root UNION
 SELECT edge.to_kind,edge.to_id FROM walk JOIN gc_edge edge
     ON edge.from_kind=walk.kind AND edge.from_id=walk.id
) SELECT * FROM walk;
CREATE UNIQUE INDEX ON gc_mark(kind,id);
CREATE TEMP TABLE gc_state(name TEXT PRIMARY KEY,total BIGINT,kept BIGINT,
                           kept_sha256 TEXT,dead_sha256 TEXT,dead_bytes BIGINT);
DO $state$ DECLARE target RECORD; missing BIGINT;
BEGIN
 FOR target IN SELECT name FROM gc_target ORDER BY name LOOP
  EXECUTE format('SELECT count(*) FROM gc_root root WHERE root.kind=%L AND NOT EXISTS(SELECT 1 FROM public.%I row WHERE row.id=root.id)',
      target.name,target.name) INTO missing;
  IF missing<>0 THEN RAISE EXCEPTION 'protected root does not exist'; END IF;
  EXECUTE format($query$
   INSERT INTO gc_state SELECT %L,count(*),count(mark.id),
    encode(sha256(convert_to(COALESCE(string_agg(
       encode(sha256(convert_to((to_jsonb(row)-'inline_bytes'-'search_text'-'fts_simple'-'fts_vector')::text,'UTF8')),'hex'),
       ',' ORDER BY row.id) FILTER(WHERE mark.id IS NOT NULL),''),'UTF8')),'hex'),
    encode(sha256(convert_to(COALESCE(string_agg(row.id::text,',' ORDER BY row.id)
       FILTER(WHERE mark.id IS NULL),''),'UTF8')),'hex'),
    COALESCE(sum(octet_length(to_jsonb(row)::text)) FILTER(WHERE mark.id IS NULL),0)
   FROM public.%I row LEFT JOIN gc_mark mark ON mark.kind=%L AND mark.id=row.id
  $query$,target.name,target.name,target.name);
 END LOOP;
END $state$;
CREATE TEMP TABLE gc_dependents(LIKE gc_state INCLUDING ALL);
DO $dependents$ DECLARE relation RECORD; condition TEXT;
BEGIN
 FOR relation IN SELECT DISTINCT table_name FROM gc_owner ORDER BY table_name LOOP
  IF EXISTS(SELECT 1 FROM pg_class WHERE oid=to_regclass('public.'||relation.table_name) AND relkind IN ('r','p')) THEN
   SELECT string_agg(format('EXISTS(SELECT 1 FROM gc_mark WHERE kind=%L AND id=row.%I)',target_name,column_name),' AND ')
    INTO condition FROM gc_owner WHERE table_name=relation.table_name;
   EXECUTE format($query$
    INSERT INTO gc_dependents SELECT %L,count(*),count(*) FILTER(WHERE %s),
      encode(sha256(convert_to(COALESCE(string_agg(hash,',' ORDER BY hash) FILTER(WHERE %s),''),'UTF8')),'hex'),
      encode(sha256(convert_to(COALESCE(string_agg(hash,',' ORDER BY hash) FILTER(WHERE NOT (%s)),''),'UTF8')),'hex'),
      COALESCE(sum(_gc_bytes) FILTER(WHERE NOT (%s)),0)
    FROM (SELECT row.*,encode(sha256(convert_to(to_jsonb(row)::text,'UTF8')),'hex') AS hash,
                 octet_length(to_jsonb(row)::text) AS _gc_bytes FROM public.%I row) row
   $query$,relation.table_name,condition,condition,condition,condition,relation.table_name);
  END IF;
 END LOOP;
END $dependents$;
"""


SNAPSHOT_SQL = """
SELECT jsonb_build_object(
 'tables',(SELECT jsonb_agg(to_jsonb(s) ORDER BY name) FROM gc_state s),
 'dependents',(SELECT jsonb_agg(to_jsonb(s) ORDER BY name) FROM gc_dependents s),
 'root_set_sha256',(SELECT encode(sha256(convert_to(COALESCE(string_agg(kind||':'||id,',' ORDER BY kind,id),''),'UTF8')),'hex') FROM gc_mark),
 'generations',(SELECT COALESCE(jsonb_agg(to_jsonb(g) ORDER BY id),'[]'::jsonb) FROM source_generation g),
 'pointers',(SELECT COALESCE(jsonb_agg(jsonb_build_array(id,active_generation_id) ORDER BY id),'[]'::jsonb) FROM logical_source),
 'packs',(SELECT COALESCE(jsonb_agg(to_jsonb(p) ORDER BY id),'[]'::jsonb) FROM content_pack p),
 'schema_sha256',(SELECT encode(sha256(convert_to(COALESCE(string_agg(value,',' ORDER BY value),''),'UTF8')),'hex') FROM (
   SELECT pg_get_constraintdef(oid)||':'||oid::text value FROM pg_constraint WHERE connamespace='public'::regnamespace
   UNION ALL SELECT pg_get_triggerdef(oid)||':'||oid::text||':'||tgenabled::text FROM pg_trigger WHERE NOT tgisinternal AND tgrelid IN(SELECT oid FROM pg_class WHERE relnamespace='public'::regnamespace)
   UNION ALL SELECT pg_get_functiondef(oid)||':'||proowner::text||':'||COALESCE(proacl::text,'') FROM pg_proc WHERE pronamespace='public'::regnamespace AND prokind='f'
   UNION ALL SELECT oid::text||':'||relname||':'||relkind::text||':'||relowner||':'||relrowsecurity||':'||relforcerowsecurity||':'||COALESCE(relacl::text,'')
       FROM pg_class WHERE relnamespace='public'::regnamespace
   UNION ALL SELECT attrelid::text||':'||attnum||':'||attname||':'||atttypid||':'||attnotnull||':'||attisdropped
       FROM pg_attribute WHERE attrelid IN(SELECT oid FROM pg_class WHERE relnamespace='public'::regnamespace) AND attnum>0
   UNION ALL SELECT oid::text||':'||polname||':'||polrelid||':'||polroles::text||':'||COALESCE(pg_get_expr(polqual,polrelid),'')||':'||COALESCE(pg_get_expr(polwithcheck,polrelid),'') FROM pg_policy
 ) definitions)
)::text;
"""


def observation(database, privileged, roots, error_path=None):
    return json.loads(sql(database, privileged, 'BEGIN ISOLATION LEVEL REPEATABLE READ;\n' +
                          graph_sql(roots) + SNAPSHOT_SQL + '\nROLLBACK;', error_path))


def build_plan(observed, retained, retention_sha, activated_sha, cleanup_sha, runtime_sha, code_sha,
               resource_policy=None, maintenance_binary_sha=None):
    if any(not HEX.fullmatch(v) for v in (retention_sha, activated_sha, cleanup_sha, runtime_sha)) \
            or not re.fullmatch('[0-9a-f]{40}', code_sha):
        raise RuntimeError('exact package, activation, cleanup, retention and code identities are required')
    return {'schema_version': 'mainrag.storage-v2.native-gc-plan.v1',
            'phase': 'NATIVE_GLOBAL_MARK_SWEEP', 'status': 'PLANNED_REQUIRES_LIVE_GATES',
            'operator_sha256': operator_digest(), 'code_sha': code_sha,
            'retention_file_sha256': retention_sha, 'retention': retained,
            'activation_manifest_sha256': activated_sha, 'legacy_cleanup_manifest_sha256': cleanup_sha,
            'runtime_package_sha256': runtime_sha, 'before': observed,
            'maintenance_binary_sha256': maintenance_binary_sha,
            'resource_policy': resource_policy,
            'before_state_sha256': digest(observed),
            'physical_reclaim_proven': False, 'remaining': ['PACK_REPACK_UNLINK', 'DATABASE_COMPACTION_AND_PHYSICAL_READBACK']}


def resource_readback(plan, database, privileged, *, after=False):
    policy = plan.get('resource_policy')
    if not isinstance(policy, dict) or set(policy) != {'data_filesystem', 'root_filesystem', 'thinpool_uuid', 'pack_root'} \
            or not isinstance(policy['thinpool_uuid'], str) or not re.fullmatch('[A-Za-z0-9-]{6,128}', policy['thinpool_uuid']):
        raise RuntimeError('reviewed physical resource policy is missing')
    command = (['sudo', '-n'] if privileged else []) + ['lvs', '--reportformat', 'json', '--units', 'b',
               '--nosuffix', '-o', 'lv_uuid,lv_size,data_percent,metadata_percent']
    output = subprocess.run(command, text=True, capture_output=True)
    if output.returncode:
        raise RuntimeError('physical thinpool state could not be measured')
    pools = [row for report in json.loads(output.stdout)['report'] for row in report['lv']
             if row['lv_uuid'] == policy['thinpool_uuid']]
    if len(pools) != 1:
        raise RuntimeError('physical thinpool identity differs')
    pool = pools[0]
    total = int(float(pool['lv_size']))
    used = int(total*float(pool['data_percent'])/100)
    headroom = int(total*0.75)-used
    data_free = shutil.disk_usage(policy['data_filesystem']).free
    root_free = shutil.disk_usage(policy['root_filesystem']).free
    if not Path(policy['pack_root']).is_dir() or str(Path(policy['pack_root']).resolve()) != policy['pack_root'] \
            or os.stat(policy['pack_root']).st_dev != os.stat(policy['data_filesystem']).st_dev:
        raise RuntimeError('reviewed pack root and data filesystem differ')
    ready = int(sql(database, privileged, "SELECT COALESCE(sum((pg_stat_file('pg_wal/'||left(name,24),true)).size),0) FROM pg_ls_dir('pg_wal/archive_status') name WHERE name ~ '^[0-9A-F]{24}[.]ready$';"))
    states = plan['before']['tables'] + (plan['before']['dependents'] or [])
    # Full JSON lengths include detoasted data. Reserve full-page WAL and
    # tuple overhead conservatively; DELETE does not physically compact indexes.
    wal_bound = 8*sum(row['dead_bytes']+32768*(row['total']-row['kept']) for row in states)+1024*1024
    temporary_bound = 512*sum(row['total'] for row in states)+1024**3
    reserve = 42*1024**3
    additional = 0 if after else wal_bound+temporary_bound
    if headroom < reserve+additional or data_free < reserve+additional \
            or root_free < 20*1024**3 or float(pool['metadata_percent']) >= 60 \
            or ready > (28 if after else 24)*1024**3 or ready+(0 if after else wal_bound) > 28*1024**3:
        raise RuntimeError('native GC exceeds physical, temporary-space or local WAL admission')
    return {'status':'WITHIN_ADMITTED_NATIVE_GC_RESOURCE_BUDGET','observed_at_unix':int(time.time()),
            'physical_pool_used_bytes':used,'headroom_to75_bytes':headroom,'data_free_bytes':data_free,
            'root_free_bytes':root_free,'wal_ready_bytes':ready,'wal_admission_bytes':wal_bound,
            'temporary_admission_bytes':temporary_bound}


def validate_approval(plan, manifest_sha, approval, evidence_root, privileged):
    bindings = {key: plan[key] for key in ('before_state_sha256', 'activation_manifest_sha256',
               'legacy_cleanup_manifest_sha256', 'runtime_package_sha256', 'maintenance_binary_sha256')}
    now = int(time.time())
    if approval.get('schema_version') != 'mainrag.storage-v2.native-gc-approval.v1' \
            or approval.get('manifest_sha256') != manifest_sha \
            or approval.get('review_kind') != 'OWNER_AUTHORIZED_SELF_REVIEW' \
            or not isinstance(approval.get('authority'), str) or not approval['authority'].strip() \
            or type(approval.get('observed_at_unix')) is not int \
            or not 0 <= now-approval['observed_at_unix'] <= 300 \
            or set(approval.get('gates', {})) != {'native_integrity', 'export_retention', 'runtime_retirement', 'recovery_boundary', 'resource_budget'}:
        raise RuntimeError('fresh exact GC approval and all required gates are missing')
    gates = {}
    for name, reference in approval['gates'].items():
        if set(reference) != {'file', 'sha256'} or Path(reference['file']).name != reference['file']:
            raise RuntimeError('GC gate proof reference is invalid')
        gate = A.read_bound(evidence_root/reference['file'], reference['sha256'])
        if gate.get('status') != 'PASS' or gate.get('gate') != name or gate.get('bindings') != bindings \
                or type(gate.get('observed_at_unix')) is not int or not 0 <= now-gate['observed_at_unix'] <= 300 \
                or not isinstance(gate.get('proofs'), list) or not gate['proofs']:
            raise RuntimeError('GC gate is stale, incomplete or bound to another state')
        for proof in gate['proofs']:
            if set(proof) != {'file', 'sha256'} or Path(proof['file']).name != proof['file']:
                raise RuntimeError('underlying GC proof reference is invalid')
            A.read_bound(evidence_root/proof['file'], proof['sha256'])
        gates[name] = gate
    expected = gates['runtime_retirement'].get('runtime')
    if not isinstance(expected, dict) or A.runtime_readback(expected.get('pid'), privileged) != expected \
            or expected['binary_sha256'] != plan['runtime_package_sha256'] \
            or expected['settings']['MAINRAG_STORAGE_V2_DEFAULT_READ_MANIFEST_SHA256'] != plan['activation_manifest_sha256'] \
            or expected['settings']['MAINRAG_STORAGE_V2_LEGACY_RETIRED_MANIFEST_SHA256'] != plan['legacy_cleanup_manifest_sha256']:
        raise RuntimeError('live accepted runtime or retirement identity drifted')


def apply_sql(plan, manifest_sha, approval_sha, administrator):
    if not re.fullmatch('[0-9a-f-]{36}', administrator):
        raise RuntimeError('administrator UUID is required')
    before = A.literal(json.dumps(plan['before'], sort_keys=True))
    deletes = ''
    for name, owners in LINKS.items():
        condition = ' OR '.join(f"NOT EXISTS(SELECT 1 FROM gc_mark WHERE kind={A.literal(target)} AND id=row.{A.ident(column)})"
                                for column, target in owners)
        deletes += f"IF EXISTS(SELECT 1 FROM pg_class WHERE oid=to_regclass('public.{name}') AND relkind IN ('r','p')) THEN DELETE FROM public.{A.ident(name)} row WHERE {condition}; END IF;\n"
    # Foreign-key leaves first. Deferred constraints are explicitly checked
    # before publishing the receipt; no CASCADE and no session-wide bypass.
    for name in ('occurrence', 'artifact_version', 'retrieval_view', 'storage_v2_search_document',
                 'storage_v2_legacy_rank_payload', 'content_node', 'content_body'):
        deletes += f"DELETE FROM public.{A.ident(name)} row WHERE NOT EXISTS(SELECT 1 FROM gc_mark WHERE kind={A.literal(name)} AND id=row.id);\n"
    graph = graph_sql(plan['retention']['roots'])
    return f"""
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='30min';
SET LOCAL standard_conforming_strings=on;
SET LOCAL app.user_id={A.literal(administrator)};
SELECT pg_advisory_xact_lock(hashtextextended('mainrag.native-gc',0));
DO $guards$ DECLARE relation RECORD;
BEGIN
 IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname=current_user) OR NOT storage_v2_is_admin() THEN
  RAISE EXCEPTION 'native GC requires the administrator operator';
 END IF;
 FOR relation IN SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE c.relkind IN ('r','p') AND n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema' ORDER BY c.oid LOOP
  EXECUTE format('LOCK TABLE %s IN SHARE MODE',relation.oid::regclass);
 END LOOP;
 IF EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()
       AND backend_type='client backend' AND state<>'idle')
    OR EXISTS(SELECT 1 FROM storage_v2_ingest_run WHERE status='building')
    OR EXISTS(SELECT 1 FROM content_reader_epoch WHERE finished_at IS NULL) THEN
  RAISE EXCEPTION 'native GC requires drained readers and writers';
 END IF;
 PERFORM storage_v2_require_complete_active_set({A.literal(plan['activation_manifest_sha256'])});
 IF NOT EXISTS(SELECT 1 FROM storage_v2_active_ingest_receipt) OR NOT EXISTS(
     SELECT 1 FROM storage_v2_legacy_cleanup_receipt WHERE manifest_sha256={A.literal(plan['legacy_cleanup_manifest_sha256'])}) THEN
  RAISE EXCEPTION 'accepted ordinary ingest and committed legacy cleanup are required';
 END IF;
END $guards$;
{graph}
CREATE TEMP TABLE gc_before(value JSONB);
INSERT INTO gc_before {SNAPSHOT_SQL.replace('SELECT jsonb_build_object(', 'SELECT jsonb_build_object(', 1).replace(')::text;', ');')}
DO $compare$ BEGIN IF (SELECT value FROM gc_before)<>{before}::JSONB THEN
 RAISE EXCEPTION 'native GC before state drifted'; END IF; END $compare$;
CREATE TEMP TABLE gc_epoch_id(id BIGINT);
WITH inserted AS (
 INSERT INTO storage_v2_gc_epoch(source_id,status,root_manifest_sha256,code_sha)
 VALUES(NULL,'verified',{A.literal(manifest_sha)},{A.literal(plan['code_sha'])}) RETURNING id
) INSERT INTO gc_epoch_id SELECT id FROM inserted;
-- Only known immutable-content guards are suspended under the locked,
-- superuser transaction. FK, digest, controlled mapping and audit guards stay.
DO $sweep$ DECLARE trigger RECORD;
BEGIN
 CREATE TEMP TABLE gc_trigger_state AS SELECT t.tgname,t.tgrelid,t.tgenabled
   FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_class c ON c.oid=t.tgrelid
   WHERE c.relnamespace='public'::regnamespace
     AND c.relname IN ({','.join(A.literal(n) for n in (*TARGETS,*LINKS))})
     AND p.proname IN ('storage_v2_reject_graph_mutation','storage_v2_reject_immutable_content','storage_v2_reject_retrieval_mutation');
 FOR trigger IN SELECT t.tgname,t.tgrelid FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid
   JOIN pg_class c ON c.oid=t.tgrelid
   WHERE c.relnamespace='public'::regnamespace
     AND c.relname IN ({','.join(A.literal(n) for n in (*TARGETS,*LINKS))})
     AND p.proname IN ('storage_v2_reject_graph_mutation','storage_v2_reject_immutable_content','storage_v2_reject_retrieval_mutation') LOOP
  EXECUTE format('ALTER TABLE %s DISABLE TRIGGER %I',trigger.tgrelid::regclass,trigger.tgname);
 END LOOP;
 {deletes}
 SET CONSTRAINTS ALL IMMEDIATE;
 FOR trigger IN SELECT * FROM gc_trigger_state LOOP
  EXECUTE format('ALTER TABLE %s %s TRIGGER %I',trigger.tgrelid::regclass,
      CASE trigger.tgenabled WHEN 'O' THEN 'ENABLE' WHEN 'A' THEN 'ENABLE ALWAYS'
                            WHEN 'R' THEN 'ENABLE REPLICA' ELSE 'DISABLE' END,trigger.tgname);
 END LOOP;
END $sweep$;
SET CONSTRAINTS ALL IMMEDIATE;
ALTER TABLE gc_state RENAME TO gc_state_before;
ALTER TABLE gc_dependents RENAME TO gc_dependents_before;
DROP TABLE gc_mark,gc_root,gc_edge,gc_target,gc_owner;
{graph}
DO $after$ BEGIN
 IF EXISTS(SELECT 1 FROM gc_state after JOIN gc_state_before before USING(name)
     WHERE after.total<>before.kept OR after.kept<>before.kept OR after.kept_sha256<>before.kept_sha256)
    OR EXISTS(SELECT 1 FROM gc_dependents after JOIN gc_dependents_before before USING(name)
     WHERE after.total<>before.kept OR after.kept<>before.kept OR after.kept_sha256<>before.kept_sha256)
    OR (SELECT value->'root_set_sha256' FROM gc_before) IS DISTINCT FROM
       to_jsonb((SELECT encode(sha256(convert_to(COALESCE(string_agg(kind||':'||id,',' ORDER BY kind,id),''),'UTF8')),'hex') FROM gc_mark)) THEN
  RAISE EXCEPTION 'native roots or retained content changed during sweep';
 END IF;
END $after$;
UPDATE storage_v2_gc_epoch SET status='sweeping',verified_at=clock_timestamp()
    WHERE id=(SELECT id FROM gc_epoch_id);
INSERT INTO storage_v2_gc_receipt(manifest_sha256,gc_epoch_id,before_state_sha256,approval_sha256,result)
 SELECT {A.literal(manifest_sha)},id,{A.literal(plan['before_state_sha256'])},{A.literal(approval_sha)},
  jsonb_build_object('phase','DB_COMMITTED_PACK_RECLAIM_PENDING','root_set_sha256',
    (SELECT value->'root_set_sha256' FROM gc_before),
    'pack_targets',{A.literal(json.dumps(plan['before']['packs']))}::JSONB,
    'resource_policy',{A.literal(json.dumps(plan.get('resource_policy')))}::JSONB,
    'maintenance_binary_sha256',{A.literal(plan.get('maintenance_binary_sha256') or '')},
    'tables',(SELECT jsonb_agg(to_jsonb(s) ORDER BY name) FROM gc_state_before s),
    'physical_reclaim_proven',FALSE) FROM gc_epoch_id;
SELECT to_jsonb(receipt)::TEXT FROM storage_v2_gc_receipt receipt WHERE manifest_sha256={A.literal(manifest_sha)};
COMMIT;
"""


def receipt(database, privileged, manifest_sha):
    return json.loads(sql(database, privileged, "SELECT COALESCE((SELECT to_jsonb(r) FROM storage_v2_gc_receipt r "
                          f"WHERE manifest_sha256={A.literal(manifest_sha)}),'null'::jsonb)::text;"))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan', action='store_true')
    mode.add_argument('--apply', metavar='MANIFEST_SHA256')
    p.add_argument('--database', required=True)
    p.add_argument('--local-postgres', action='store_true')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--retention', type=Path)
    p.add_argument('--manifest', type=Path)
    p.add_argument('--approval', type=Path)
    p.add_argument('--administrator')
    p.add_argument('--activation-manifest-sha256')
    p.add_argument('--legacy-cleanup-manifest-sha256')
    p.add_argument('--runtime-package-sha256')
    p.add_argument('--maintenance-binary-sha256')
    p.add_argument('--code-sha')
    p.add_argument('--resource-policy', type=Path)
    args = p.parse_args()
    if args.plan:
        if not isinstance(args.maintenance_binary_sha256,str) or not HEX.fullmatch(args.maintenance_binary_sha256):
            raise RuntimeError('exact compiled maintenance binary digest is required')
        retained, retained_sha = A.M.private_read(args.retention, 64*1024*1024)
        retention(retained, args.retention.parent)
        policy, _ = A.M.private_read(args.resource_policy, 65536)
        observed = observation(args.database, args.local_postgres, retained['roots'], args.output.with_suffix('.database-error.json'))
        plan = build_plan(observed, retained, retained_sha, args.activation_manifest_sha256,
                          args.legacy_cleanup_manifest_sha256, args.runtime_package_sha256, args.code_sha, policy,
                          args.maintenance_binary_sha256)
        resource_readback(plan, args.database, args.local_postgres)
        manifest_sha = A.C.private_create(args.output, plan)
        print(json.dumps({'status': plan['status'], 'manifest_sha256': manifest_sha}))
        return
    if not HEX.fullmatch(args.apply):
        raise RuntimeError('exact GC manifest digest is required')
    plan = A.read_bound(args.manifest, args.apply)
    if plan.get('schema_version') != 'mainrag.storage-v2.native-gc-plan.v1' or plan.get('operator_sha256') != operator_digest():
        raise RuntimeError('GC operator or plan schema differs')
    # A committed receipt is reconciled before volatile gates or another write.
    committed = receipt(args.database, args.local_postgres, args.apply)
    if committed is not None:
        A.C.private_create(args.output, {'status': 'RECONCILED_DB_COMMITTED_PACK_RECLAIM_PENDING', 'receipt': committed})
        print(json.dumps({'status': 'RECONCILED_DB_COMMITTED_PACK_RECLAIM_PENDING'}))
        return
    if sql(args.database, args.local_postgres, "SELECT EXISTS(SELECT 1 FROM pg_locks lock JOIN pg_stat_activity activity ON activity.pid=lock.pid WHERE lock.locktype='advisory' AND lock.granted AND activity.application_name='mainrag-storage-v2-native-gc' AND activity.state<>'idle')::text;") == 'true':
        raise RuntimeError('an earlier native GC transaction remains live; observe that handle')
    retained = A.read_bound(args.retention, plan['retention_file_sha256'])
    if retained != plan['retention']:
        raise RuntimeError('protected retention input differs from the reviewed plan')
    retention(retained, args.retention.parent)
    approval, approval_sha = A.M.private_read(args.approval, 64*1024*1024)
    validate_approval(plan, args.apply, approval, args.approval.parent, args.local_postgres)
    budget = resource_readback(plan, args.database, args.local_postgres)
    A.C.private_create(args.output.with_suffix('.resource-before.json'), budget)
    A.C.private_create(args.output, {'status': 'DISPATCHED_RECONCILE_DATABASE_RECEIPT', 'manifest_sha256': args.apply})
    committed = sql(args.database, args.local_postgres, apply_sql(plan, args.apply, approval_sha, args.administrator),
                    args.output.with_suffix('.database-error.json'))
    A.C.private_create(args.output.with_suffix('.committed.json'), json.loads(committed))
    after = resource_readback(plan,args.database,args.local_postgres,after=True)
    after['pool_growth_bytes'] = after['physical_pool_used_bytes']-budget['physical_pool_used_bytes']
    if after['pool_growth_bytes'] > budget['wal_admission_bytes']+budget['temporary_admission_bytes']:
        raise RuntimeError('native GC physical growth exceeded its admitted bound; committed receipt retained')
    A.C.private_create(args.output.with_suffix('.resource-after.json'), after)
    print(json.dumps({'status': 'DB_COMMITTED_PACK_RECLAIM_PENDING'}))


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, TypeError, ValueError, KeyError, OSError) as error:
        raise SystemExit(str(error))
