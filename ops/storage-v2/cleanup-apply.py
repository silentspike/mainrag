#!/usr/bin/env python3
"""Plan and atomically apply the PostgreSQL phase of exact legacy retirement.

Qdrant, operational components and native GC/repack are separate manifest
phases. A database receipt is never final #68 acceptance or physical reclaim.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SPEC = importlib.util.spec_from_file_location('cleanup_manifest', Path(__file__).with_name('cleanup-manifest.py'))
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)
C = M.CAPTURE
HEX = re.compile(r'[0-9a-f]{64}\Z')
GATES = frozenset({'post_activation_and_regular_ingest', 'runtime_retirement',
    'native_integrity', 'legacy_hit_coverage', 'dependency_and_caller_review',
    'export_retention', 'recovery_boundary'})
DB_KINDS = frozenset({'relation', 'column', 'constraint', 'policy', 'trigger', 'function', 'index', 'outbox_class'})
# Catalog class OIDs are PostgreSQL's fixed bootstrap identities.
CLASSES = {'relation': 1259, 'index': 1259, 'column': 1259, 'function': 1255,
           'constraint': 2606, 'trigger': 2620, 'policy': 3256}


def digest(value):
    return hashlib.sha256(C.canonical(value)).hexdigest()


def operator_digest():
    return digest({name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                   for name in ('cleanup-apply.py','cleanup-manifest.py','cleanup-plan.py')})


def read_bound(path, expected):
    value, observed = M.private_read(path, 128 * 1024 * 1024)
    if not isinstance(expected, str) or not HEX.fullmatch(expected) or observed != expected:
        raise RuntimeError('protected input digest differs')
    return value


def ident(value):
    if not isinstance(value, str) or '\x00' in value or not value:
        raise RuntimeError('catalog identifier is invalid')
    return '"' + value.replace('"', '""') + '"'


def literal(value):
    return "'" + value.replace("'", "''") + "'"


def oid(value):
    if not isinstance(value, (str, int)) or isinstance(value, bool) or not str(value).isdecimal() \
            or not 0 < int(value) <= 4294967295:
        raise RuntimeError('catalog OID is invalid')
    return int(value)


def targets_for(draft):
    objects = draft.get('objects')
    if not isinstance(objects, list) or not objects or len(objects) > 100000 \
            or len({item.get('key') for item in objects}) != len(objects) \
            or any(item.get('disposition') not in ('KEEP', 'DELETE') for item in objects):
        raise RuntimeError('every observed object requires an exact disposition')
    targets = [item for item in objects if item['disposition'] == 'DELETE' and item['kind'] in DB_KINDS]
    if not targets:
        raise RuntimeError('no approved PostgreSQL targets')
    return targets


def build_plan(inventory, catalog_sha, draft, draft_sha, runtime_package_sha):
    if not HEX.fullmatch(runtime_package_sha):
        raise RuntimeError('exact runtime package digest is required')
    if draft.get('schema_version') != 'mainrag.storage-v2.cleanup-manifest-draft.v1' \
            or draft.get('catalog_file_sha256') != catalog_sha:
        raise RuntimeError('disposition draft belongs to another catalog')
    decisions = {item['key']: {key: item[key] for key in ('key','disposition','reason','authority')}
                 for item in draft.get('objects', []) if item.get('disposition') in ('KEEP','DELETE')}
    reproduced = M.draft(inventory, catalog_sha, decisions)
    if reproduced != draft:
        raise RuntimeError('disposition draft or protected object identities differ')
    targets = targets_for(draft)
    catalog = inventory['catalog']
    if not any(row['name'] == 'storage_v2_legacy_cleanup_receipt' for row in catalog['relations']):
        raise RuntimeError('administrator-owned cleanup receipt migration is required')
    # DDL is resolved from live OIDs in the transaction; the plan contains no
    # executable SQL obtained from input files or arbitrary operation strings.
    addresses = []
    deleted_relations = {oid(item['observed']['oid']) for item in targets if item['kind'] == 'relation'}
    parents = {oid(row['oid']): oid(row['relation_oid']) for row in catalog['indexes']}
    parents.update({oid(row['oid']): oid(row['owned_by_relation_oid'])
                    for row in catalog['relations'] if row.get('owned_by_relation_oid') is not None})
    for item in targets:
        kind, row = item['kind'], item['observed']
        if kind == 'outbox_class':
            if not any(oid(r['oid']) in deleted_relations and r['name']=='indexing_outbox'
                       for r in catalog['relations']):
                raise RuntimeError('outbox classes can retire only with their exact whole relation')
            continue
        if kind == 'relation':
            if row.get('kind') not in ('r','p','v','m','S'):
                raise RuntimeError('unsupported legacy relation class')
            if row.get('kind') == 'S' and oid(row.get('owned_by_relation_oid', 0)) not in deleted_relations:
                raise RuntimeError('sequence retirement requires its deleted owning relation')
        elif kind not in ('function','outbox_class'):
            parent = oid(row['relation_oid'])
            parent = parents.get(parent,parent)
            if parent not in deleted_relations:
                raise RuntimeError('child retirement requires its deleted whole relation')
        if kind == 'function' and row.get('kind') != 'f':
            raise RuntimeError('unsupported legacy routine class')
        address = [CLASSES[kind], oid(row['relation_oid'] if kind=='column' else row['oid']),
                   row['number'] if kind=='column' else 0]
        if type(address[2]) is not int or address[2] < 0:
            raise RuntimeError('catalog subobject identity is invalid')
        addresses.append(address)
    return {'schema_version':'mainrag.storage-v2.postgres-cleanup-plan.v1',
        'phase':'POSTGRES_LEGACY', 'status':'PLANNED_REQUIRES_LIVE_GATES',
        'catalog_file_sha256':catalog_sha, 'draft_file_sha256':draft_sha,
        'before_state_sha256':inventory['before_state_sha256'],
        'pointer_set_sha256':catalog['pointer_set_sha256'],
        'runtime_package_sha256':runtime_package_sha, 'catalog':catalog,
        'operator_sha256':operator_digest(),
        'objects':draft['objects'], 'targets':targets, 'addresses':sorted(addresses),
        'pending_external_delete_count':sum(item['disposition']=='DELETE' and item['kind'] not in DB_KINDS
                                          for item in draft['objects']),
        'remaining_phases':['QDRANT_AND_OPERATIONAL_COMPONENTS','NATIVE_GC_REPACK','POST_CLEANUP_ACCEPTANCE']}


def validate_approval(plan, plan_sha, approval, evidence_root, now):
    if approval.get('schema_version') != 'mainrag.storage-v2.cleanup-approval.v1' \
            or approval.get('manifest_sha256') != plan_sha \
            or approval.get('review_kind') != 'OWNER_AUTHORIZED_SELF_REVIEW' \
            or not isinstance(approval.get('authority'), str) or not approval['authority'].strip() \
            or approval.get('accepts_loss_of_legacy_rollback') is not True \
            or type(approval.get('observed_at_unix')) is not int \
            or not 0 <= now-approval['observed_at_unix'] <= 300 \
            or not isinstance(approval.get('gates'), dict) or set(approval['gates']) != GATES:
        raise RuntimeError('fresh exact-manifest authority and complete gates are required')
    bindings = {key:plan[key] for key in ('before_state_sha256','pointer_set_sha256','runtime_package_sha256')}
    loaded = {}
    for name, reference in approval['gates'].items():
        if not isinstance(reference, dict) or set(reference) != {'file','sha256'} \
                or not isinstance(reference['file'], str) \
                or Path(reference['file']).name != reference['file']:
            raise RuntimeError('gate must name a protected evidence file in its evidence directory')
        evidence = read_bound(evidence_root/reference['file'], reference['sha256'])
        if evidence.get('schema_version') != 'mainrag.storage-v2.cleanup-gate.v1' \
                or evidence.get('gate') != name or evidence.get('status') != 'PASS' \
                or evidence.get('bindings') != bindings \
                or type(evidence.get('observed_at_unix')) is not int \
                or not 0 <= now-evidence['observed_at_unix'] <= 300 \
                or not isinstance(evidence.get('proofs'), list) or not evidence['proofs']:
            raise RuntimeError('a cleanup gate is stale, incomplete or bound to another state')
        # PASS labels alone are insufficient: open every exact underlying proof.
        for proof in evidence['proofs']:
            if not isinstance(proof, dict) or set(proof) != {'file','sha256'} \
                    or not isinstance(proof['file'], str) or Path(proof['file']).name != proof['file']:
                raise RuntimeError('underlying proof reference is invalid')
            read_bound(evidence_root/proof['file'], proof['sha256'])
        loaded[name] = evidence
    active = loaded['post_activation_and_regular_ingest']
    if not isinstance(active.get('activation_manifest_sha256'), str) \
            or not HEX.fullmatch(active['activation_manifest_sha256']) \
            or active.get('regular_ingest_completed') is not True:
        raise RuntimeError('accepted activation and first regular ingest are missing')
    deleted_relations = {item['observed']['oid'] for item in plan['targets'] if item['kind']=='relation'}
    deleted_functions = {item['observed']['oid'] for item in plan['targets'] if item['kind']=='function'}
    candidates = {(row['function_oid'],row['relation_oid']) for row in plan['catalog']['routine_relation_references']
                  if row['relation_oid'] in deleted_relations}
    functions = {row['oid']:row for row in plan['catalog']['functions']}
    reviews = loaded['dependency_and_caller_review'].get('callers')
    if not isinstance(reviews,list) or len(reviews)!=len(candidates):
        raise RuntimeError('logical caller review is incomplete')
    seen = set()
    for review in reviews:
        pair = (review.get('function_oid'),review.get('relation_oid'))
        if pair not in candidates or pair in seen \
                or review.get('definition_sha256')!=functions[pair[0]]['definition_sha256'] \
                or review.get('disposition') not in (
                    ('RETIRED_WITH_TARGET',) if pair[0] in deleted_functions else
                    ('LEXICAL_FALSE_POSITIVE','SUPPORTED_NATIVE_BRANCH_PROVEN')):
            raise RuntimeError('logical caller identity, definition or retirement proof differs')
        seen.add(pair)
    return loaded


def runtime_readback(pid, privileged=False):
    if type(pid) is not int or pid <= 1:
        raise RuntimeError('live API process identity is required')
    script = """import hashlib,json,os,sys
p='/proc/'+sys.argv[1]
with open(p+'/exe','rb') as f: h=hashlib.file_digest(f,'sha256').hexdigest()
s=open(p+'/stat').read().rsplit(')',1)[1].split()[19]
e=dict(x.split(b'=',1) for x in open(p+'/environ','rb').read().split(b'\\0') if b'=' in x)
keys=['MAINRAG_STORAGE_V2_DEFAULT_READ_MANIFEST_SHA256','MAINRAG_STORAGE_V2_ACTIVE_INGEST_COMMIT_SHA','MAINRAG_STORAGE_V2_LEGACY_RETIRED_MANIFEST_SHA256']
print(json.dumps({'pid':int(sys.argv[1]),'start_ticks':s,'binary_sha256':h,'settings':{k:e.get(k.encode(),b'').decode() for k in keys}}))
"""
    command = (['sudo','-n'] if privileged else []) + [sys.executable,'-c',script,str(pid)]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError('live API executable or startup settings could not be observed')
    return json.loads(result.stdout)


def validate_runtime(plan, plan_sha, gates, privileged):
    required = gates['runtime_retirement'].get('runtime')
    if not isinstance(required, dict):
        raise RuntimeError('live runtime retirement proof is missing')
    observed = runtime_readback(required.get('pid'), privileged)
    if observed != required or observed['binary_sha256'] != plan['runtime_package_sha256'] \
            or observed['settings']['MAINRAG_STORAGE_V2_LEGACY_RETIRED_MANIFEST_SHA256'] != plan_sha \
            or observed['settings']['MAINRAG_STORAGE_V2_DEFAULT_READ_MANIFEST_SHA256'] != \
               gates['post_activation_and_regular_ingest']['activation_manifest_sha256'] \
            or not re.fullmatch('[0-9a-f]{40}', observed['settings']['MAINRAG_STORAGE_V2_ACTIVE_INGEST_COMMIT_SHA']):
        raise RuntimeError('live runtime package, process or retirement binding drifted')


def query_for(plan, after=False):
    catalog = plan['catalog']
    dropped = {item['observed']['name'] for item in plan['targets'] if item['kind']=='relation'} if after else set()
    names = tuple(sorted(set(catalog['exact_rows']) - dropped))
    reach = catalog['reachability']
    generations = tuple(reach['retained_generation_ids']) if reach else ()
    names_present = {row['name'] for row in catalog['relations']} - dropped
    statement = C.catalog_statement(names, generations,
        historical_hit_roots='storage_v2_legacy_hit_history' in names_present,
        outbox_present='indexing_outbox' in names_present)
    return statement[statement.index('SELECT jsonb_build_object('):statement.rindex('COMMIT;')].strip().rstrip(';')


def sql_for(plan, plan_sha, approval_sha, activation_manifest_sha):
    """A single server transaction owns locks, drift guards, drops and receipt."""
    if not all(isinstance(value,str) and HEX.fullmatch(value) for value in (plan_sha,approval_sha,activation_manifest_sha)):
        raise RuntimeError('transaction identities are invalid')
    if plan.get('operator_sha256')!=operator_digest():
        raise RuntimeError('cleanup operator code drifted')
    catalog = plan['catalog']
    if catalog['open_reader_count'] or catalog['building_run_count'] or not catalog['active_pointer_count']:
        raise RuntimeError('cleanup requires active sources and no readers or writers')
    targets_for(plan)
    addresses = plan['addresses']
    rows = ','.join(f'({int(a)},{int(b)},{int(c)})' for a,b,c in addresses)
    expected = "convert_from(decode('"+C.canonical(catalog).hex()+"','hex'),'UTF8')::jsonb"
    lock_sql = '\n'.join('LOCK TABLE ONLY public.'+ident(row['name'])+' IN '+
        ('ACCESS EXCLUSIVE' if any(item['kind']=='relation' and item['observed']['oid']==row['oid']
                                   for item in plan['targets']) else 'SHARE')+' MODE;'
        for row in sorted(catalog['relations'],key=lambda row:oid(row['oid'])) if row['kind'] in ('r','p','m'))
    # Closure includes automatic and internal children of reviewed objects.
    # Normal dependents are not added: RESTRICT rejects unapproved consumers.
    # Every *observed* child in this closure must have an explicit DELETE.
    object_rows = []
    for item in plan['objects']:
        if item['kind'] not in CLASSES: continue
        row=item['observed']; kind=item['kind']
        subid = row['number'] if kind=='column' else 0
        if type(subid) is not int or subid < 0:
            raise RuntimeError('reviewed catalog subobject is invalid')
        object_rows.append(f"({CLASSES[kind]},{oid(row['relation_oid'] if kind=='column' else row['oid'])},"
            f"{subid},{literal(item['disposition'])})")
    reviewed = ','.join(object_rows)
    coverage = ''
    if {'files','chunks'} <= {row['name'] for row in catalog['relations']}:
        coverage = """
 IF EXISTS(SELECT 1 FROM public.chunks c JOIN public.files f ON f.id=c.file_id
   LEFT JOIN public.logical_source s ON s.id=f.source_id
   LEFT JOIN public.storage_v2_legacy_hit_proof p ON p.old_hit_id=c.id::text
     AND p.source_id=f.source_id AND p.generation_id=s.active_generation_id
     AND p.proof->>'file_id'=f.id::text
     AND p.proof->>'file_revision'=COALESCE((SELECT revision FROM public.storage_v2_legacy_rank_revision WHERE file_id=f.id),0)::text
     AND p.proof->>'chunk_sha256'=encode(c.content_hash,'hex')
     AND p.proof->>'file_sha256'=encode(f.hash,'hex')
     AND p.proof->>'source_path'=f.path AND p.proof->>'start_line'=c.start_line::text
     AND p.proof->>'end_line'=c.end_line::text
     AND p.mapping_sha256=public.storage_v2_legacy_hit_mapping_state(c.id::text)
   WHERE p.old_hit_id IS NULL) THEN
  RAISE EXCEPTION 'actual legacy hit coverage is incomplete or drifted';
 END IF;
"""
    return f"""
BEGIN;
SET LOCAL standard_conforming_strings=on;
SET LOCAL row_security=off;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='120s';
SET LOCAL idle_in_transaction_session_timeout='30s';
SELECT pg_advisory_xact_lock(hashtextextended('mainrag.legacy-cleanup',0));
{lock_sql}
CREATE TEMP TABLE cleanup_target(class_oid oid,object_oid oid,subid int) ON COMMIT DROP;
INSERT INTO cleanup_target VALUES {rows};
CREATE TEMP TABLE cleanup_closure ON COMMIT DROP AS
 WITH RECURSIVE root AS (
  SELECT * FROM cleanup_target
  UNION SELECT t.class_oid,t.object_oid,a.attnum::int FROM cleanup_target t
   JOIN pg_attribute a ON t.class_oid='pg_class'::regclass AND a.attrelid=t.object_oid
   WHERE t.subid=0 AND a.attnum>0 AND NOT a.attisdropped
 ), closure(class_oid,object_oid,subid) AS (
  SELECT * FROM root UNION
  SELECT d.classid,d.objid,d.objsubid FROM pg_depend d JOIN closure c
   ON d.refclassid=c.class_oid AND d.refobjid=c.object_oid AND d.refobjsubid=c.subid
   WHERE d.deptype IN ('a','i')
 ) SELECT * FROM closure;
DO $cleanup_guard$
DECLARE before_state jsonb; after_state jsonb; expected jsonb:={expected};
        cleanup_record record; drop_names text; initial_bytes bigint; target_bytes bigint;
BEGIN
 IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname=current_user) THEN
  RAISE EXCEPTION 'legacy cleanup requires the database administrator';
 END IF;
 IF EXISTS(SELECT 1 FROM pg_stat_activity WHERE datid=(SELECT oid FROM pg_database WHERE datname=current_database())
   AND pid<>pg_backend_pid() AND backend_type='client backend' AND state<>'idle') THEN
  RAISE EXCEPTION 'another database reader or writer is active';
 END IF;
 SELECT value INTO before_state FROM ({query_for(plan)}) snapshot(value);
 IF before_state IS DISTINCT FROM expected THEN RAISE EXCEPTION 'cleanup catalog drifted'; END IF;
 PERFORM public.storage_v2_require_complete_active_set({literal(activation_manifest_sha)});
 IF NOT EXISTS(SELECT 1 FROM public.storage_v2_active_ingest_receipt r
   JOIN public.storage_v2_activation_set_evidence a ON a.id=r.activation_id
   WHERE a.manifest_sha256={literal(activation_manifest_sha)}) THEN
  RAISE EXCEPTION 'first regular post-activation ingest is missing';
 END IF;
 {coverage}
 IF EXISTS(SELECT 1 FROM (VALUES {reviewed}) reviewed(class_oid,object_oid,subid,disposition)
  JOIN cleanup_closure c USING(class_oid,object_oid,subid) WHERE disposition<>'DELETE') THEN
  RAISE EXCEPTION 'an implicitly removed object has a retained disposition';
 END IF;
 SELECT pg_database_size(current_database()) INTO initial_bytes;
 SELECT COALESCE(sum(pg_total_relation_size(c.oid)),0) INTO target_bytes FROM pg_class c
  JOIN cleanup_target t ON t.class_oid='pg_class'::regclass AND t.object_oid=c.oid AND t.subid=0
  WHERE c.relkind IN ('r','p','m','S');
 -- Resolve signatures and names from catalog OIDs, never input SQL.
 SELECT string_agg(p.oid::regprocedure::text,',' ORDER BY p.oid) INTO drop_names FROM pg_proc p
  JOIN cleanup_target t ON t.class_oid='pg_proc'::regclass AND t.object_oid=p.oid;
 IF drop_names IS NOT NULL THEN EXECUTE 'DROP FUNCTION '||drop_names||' RESTRICT'; END IF;
 FOR cleanup_record IN SELECT relkind, CASE WHEN relkind='v' THEN 'VIEW' ELSE 'MATERIALIZED VIEW' END AS ddl_kind,
   string_agg(format('%I.%I',n.nspname,c.relname),',' ORDER BY c.oid) AS names
   FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
   JOIN cleanup_target t ON t.class_oid='pg_class'::regclass AND t.object_oid=c.oid AND t.subid=0
   WHERE c.relkind IN ('v','m') GROUP BY relkind LOOP
  EXECUTE 'DROP '||cleanup_record.ddl_kind||' '||cleanup_record.names||' RESTRICT';
 END LOOP;
 -- Whole tables retire together so approved inter-table FKs do not require CASCADE.
 SELECT string_agg(format('%I.%I',n.nspname,c.relname),',' ORDER BY c.oid) INTO drop_names
  FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
  JOIN cleanup_target t ON t.class_oid='pg_class'::regclass AND t.object_oid=c.oid AND t.subid=0
  WHERE c.relkind IN ('r','p');
 IF drop_names IS NOT NULL THEN EXECUTE 'DROP TABLE '||drop_names||' RESTRICT'; END IF;
 SELECT value INTO after_state FROM ({query_for(plan,True)}) snapshot(value);
 -- Verify the complete retained object set; automatic private catalog children
 -- are accounted for through the same dependency closure computed before DDL.
 FOR cleanup_record IN SELECT field FROM unnest(ARRAY['relations','columns','constraints','policies','triggers','functions','indexes']) field LOOP
  IF (SELECT COALESCE(jsonb_agg(r ORDER BY r::text),'[]'::jsonb) FROM jsonb_array_elements(before_state->cleanup_record.field) r
       WHERE NOT EXISTS(SELECT 1 FROM cleanup_closure c WHERE
        c.class_oid=CASE WHEN cleanup_record.field IN ('relations','columns','indexes') THEN 1259
                        WHEN cleanup_record.field='functions' THEN 1255 WHEN cleanup_record.field='constraints' THEN 2606
                        WHEN cleanup_record.field='policies' THEN 3256 ELSE 2620 END
        AND c.object_oid=(CASE WHEN cleanup_record.field='columns' THEN r->>'relation_oid' ELSE r->>'oid' END)::oid
        AND c.subid=CASE WHEN cleanup_record.field='columns' THEN (r->>'number')::int ELSE 0 END))
     IS DISTINCT FROM
     (SELECT COALESCE(jsonb_agg(r ORDER BY r::text),'[]'::jsonb) FROM jsonb_array_elements(after_state->cleanup_record.field) r) THEN
   RAISE EXCEPTION 'retained catalog object changed or a target remains';
  END IF;
 END LOOP;
 IF (before_state-ARRAY['relations','columns','constraints','policies','triggers','functions','indexes','dependencies','routine_relation_references','exact_rows','outbox_classes'])
  IS DISTINCT FROM
    (after_state-ARRAY['relations','columns','constraints','policies','triggers','functions','indexes','dependencies','routine_relation_references','exact_rows','outbox_classes']) THEN
  RAISE EXCEPTION 'native roots, pointers or writer state changed';
 END IF;
 IF (SELECT COALESCE(jsonb_agg(d ORDER BY d::text),'[]'::jsonb) FROM jsonb_array_elements(before_state->'dependencies') d
   WHERE NOT EXISTS(SELECT 1 FROM cleanup_closure c WHERE
     (c.class_oid=(d->>'class_oid')::oid AND c.object_oid=(d->>'object_oid')::oid AND c.subid=(d->>'object_subid')::int)
     OR (c.class_oid=(d->>'referenced_class_oid')::oid AND c.object_oid=(d->>'referenced_object_oid')::oid
         AND c.subid=(d->>'referenced_object_subid')::int))) IS DISTINCT FROM
   (SELECT COALESCE(jsonb_agg(d ORDER BY d::text),'[]'::jsonb) FROM jsonb_array_elements(after_state->'dependencies') d) THEN
  RAISE EXCEPTION 'dependency set changed outside the approved closure';
 END IF;
 IF (SELECT COALESCE(jsonb_agg(r ORDER BY r::text),'[]'::jsonb) FROM jsonb_array_elements(before_state->'routine_relation_references') r
   WHERE NOT EXISTS(SELECT 1 FROM cleanup_closure c WHERE
     (c.class_oid=1255 AND c.object_oid=(r->>'function_oid')::oid)
     OR (c.class_oid=1259 AND c.object_oid=(r->>'relation_oid')::oid))) IS DISTINCT FROM
   (SELECT COALESCE(jsonb_agg(r ORDER BY r::text),'[]'::jsonb) FROM jsonb_array_elements(after_state->'routine_relation_references') r) THEN
  RAISE EXCEPTION 'retained logical caller references changed';
 END IF;
 IF EXISTS(SELECT 1 FROM jsonb_each(after_state->'exact_rows') r
           WHERE before_state->'exact_rows'->r.key IS DISTINCT FROM r.value) THEN
  RAISE EXCEPTION 'retained exact row count changed';
 END IF;
 IF EXISTS(SELECT 1 FROM jsonb_array_elements(after_state->'relations') r WHERE r->>'name'='indexing_outbox')
    AND before_state->'outbox_classes' IS DISTINCT FROM after_state->'outbox_classes' THEN
  RAISE EXCEPTION 'retained outbox state changed';
 END IF;
 IF EXISTS(SELECT 1 FROM pg_stat_activity WHERE datid=(SELECT oid FROM pg_database WHERE datname=current_database())
   AND pid<>pg_backend_pid() AND backend_type='client backend' AND state<>'idle') THEN
  RAISE EXCEPTION 'another reader or writer appeared during cleanup';
 END IF;
 INSERT INTO public.storage_v2_legacy_cleanup_receipt(manifest_sha256,before_state_sha256,pointer_set_sha256,
    runtime_package_sha256,approval_sha256,result) VALUES (
  {literal(plan_sha)},{literal(plan['before_state_sha256'])},{literal(plan['pointer_set_sha256'])},
  {literal(plan['runtime_package_sha256'])},{literal(approval_sha)},jsonb_build_object(
    'phase','POSTGRES_LEGACY','status','DB_COMMITTED_POSTCHECK_PENDING',
    'initial_database_bytes',initial_bytes,'deleted_relation_bytes',target_bytes,
    'post_catalog_sha256',encode(sha256(convert_to(after_state::text,'UTF8')),'hex'),
    'pending_external_delete_count',{int(plan['pending_external_delete_count'])}));
END $cleanup_guard$;
COMMIT;
SELECT result::text FROM public.storage_v2_legacy_cleanup_receipt WHERE manifest_sha256={literal(plan_sha)};
"""


def psql(database, privileged, statement, *, error_path=None):
    command = (['sudo','-n','-u','postgres'] if privileged else []) + [
        'psql','-X','--no-psqlrc','-qAt','--set=ON_ERROR_STOP=1','--dbname',database]
    environment = os.environ.copy()
    environment['PGAPPNAME']='mainrag-storage-v2-legacy-cleanup'
    result = subprocess.run(command,input=statement,text=True,capture_output=True,env=environment,check=False)
    if result.returncode:
        # Diagnostics stay in the protected attempt directory, not public logs.
        if error_path is not None:
            C.private_create(error_path,{'status':'DATABASE_OPERATION_FAILED_OR_OUTCOME_UNKNOWN',
                                         'exit_code':result.returncode,'stderr':result.stderr})
        raise RuntimeError('cleanup transaction failed or outcome is unknown; read the durable database receipt')
    return result.stdout.strip()


def receipt(database, privileged, manifest_sha):
    result = psql(database,privileged,"SELECT COALESCE((SELECT to_jsonb(r)::text FROM public.storage_v2_legacy_cleanup_receipt r "
                  f"WHERE manifest_sha256={literal(manifest_sha)}),'null');")
    return json.loads(result)


def transaction_running(database, privileged):
    value = psql(database,privileged,"""SELECT EXISTS(
      SELECT 1 FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid
      WHERE l.locktype='advisory' AND l.granted
        AND l.database=(SELECT oid FROM pg_database WHERE datname=current_database())
        AND l.classid=((hashtextextended('mainrag.legacy-cleanup',0)>>32)&4294967295)::oid
        AND l.objid=(hashtextextended('mainrag.legacy-cleanup',0)&4294967295)::oid
        AND l.objsubid=1);""")
    if value not in ('t','f'):
        raise RuntimeError('cleanup transaction lease observation is invalid')
    return value=='t'


def post_readback(plan, database, privileged, committed):
    dropped = {item['observed']['name'] for item in plan['targets'] if item['kind']=='relation'}
    remaining = tuple(sorted(set(plan['catalog']['exact_rows'])-dropped))
    reach = plan['catalog']['reachability']
    current = C.catalog(database,privileged,remaining,
                        tuple(reach['retained_generation_ids']) if reach else ())
    for key in ('database_oid','pointer_set_sha256','active_pointer_count','generations','packs','reachability'):
        if current[key]!=plan['catalog'][key]:
            raise RuntimeError('post-cleanup native pointers, roots or package catalog drifted')
    if current['open_reader_count'] or current['building_run_count']:
        raise RuntimeError('post-cleanup reader or writer state is not quiescent')
    # Inspect every declared address again. A durable receipt proves commit,
    # not the continued absence of a recreated object or a changed root set.
    found = set()
    for item in M.observed_objects({'catalog':current}):
        if item['kind'] not in CLASSES: continue
        row=item['observed'];kind=item['kind']
        found.add((CLASSES[kind],oid(row['relation_oid'] if kind=='column' else row['oid']),
                   row['number'] if kind=='column' else 0))
    if any(tuple(address) in found for address in plan['addresses']) \
            or dropped & {row['name'] for row in current['relations']}:
        raise RuntimeError('a retired object remains or was recreated')
    # Same-named routines with new OIDs also count as recreation.
    retired = {(item['observed']['name'],item['observed']['arguments'])
               for item in plan['targets'] if item['kind']=='function'}
    if retired & {(row['name'],row['arguments']) for row in current['functions']}:
        raise RuntimeError('a retired routine was recreated')
    after_bytes = int(psql(database,privileged,'SELECT pg_database_size(current_database());'))
    ledger = next(row for row in current['relations'] if row['name']=='storage_v2_legacy_cleanup_receipt')
    before_ledger = next(row for row in plan['catalog']['relations'] if row['name']==ledger['name'])
    result = committed['result']
    ledger_growth = ledger['total_bytes']-before_ledger['total_bytes']
    observed_delta = result['initial_database_bytes']-after_bytes
    return {'status':'POSTGRES_OBJECTS_VERIFIED_REMAINING_PHASES_PENDING',
        'targets_absent':True,'native_root_set_unchanged':True,
        'before_database_bytes':result['initial_database_bytes'],'after_database_bytes':after_bytes,
        'measured_database_bytes_released':observed_delta,
        'planned_relation_bytes_removed':result['deleted_relation_bytes'],
        'cleanup_receipt_bytes_added':ledger_growth,
        'space_reconciliation_pending':observed_delta != result['deleted_relation_bytes']-ledger_growth,
        'filesystem_or_thinpool_reclaim_proven':False,
        'post_catalog_sha256':digest(current),'remaining_phases':plan['remaining_phases']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    modes=parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--plan',action='store_true');modes.add_argument('--apply')
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--catalog',type=Path);parser.add_argument('--catalog-sha256')
    parser.add_argument('--draft',type=Path);parser.add_argument('--draft-sha256')
    parser.add_argument('--runtime-package-sha256')
    parser.add_argument('--approval',type=Path);parser.add_argument('--approval-sha256')
    parser.add_argument('--database');parser.add_argument('--local-postgres',action='store_true')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if args.plan:
        if args.catalog is None or args.draft is None or args.runtime_package_sha256 is None:
            parser.error('planning requires catalog, draft and runtime package bindings')
        catalog=read_bound(args.catalog,args.catalog_sha256);draft=read_bound(args.draft,args.draft_sha256)
        plan=build_plan(catalog,args.catalog_sha256,draft,args.draft_sha256,args.runtime_package_sha256)
        sha=C.private_create(args.manifest,plan)
        print(json.dumps({'status':plan['status'],'sha256':sha,'target_count':len(plan['targets'])}))
        return 0
    if args.approval is None or args.database is None or args.output is None or args.catalog is None or args.draft is None:
        parser.error('apply requires original catalog/draft, protected authority, database and create-only attempt output')
    plan=read_bound(args.manifest,args.apply)
    if plan.get('schema_version')!='mainrag.storage-v2.postgres-cleanup-plan.v1':
        raise RuntimeError('only an exact executable PostgreSQL phase manifest is accepted')
    inventory=read_bound(args.catalog,plan['catalog_file_sha256'])
    draft=read_bound(args.draft,plan['draft_file_sha256'])
    if build_plan(inventory,plan['catalog_file_sha256'],draft,plan['draft_file_sha256'],plan['runtime_package_sha256'])!=plan:
        raise RuntimeError('executable manifest differs from protected catalog and dispositions')
    # Create-only attempts and flock prevent a second local dispatcher. The
    # database advisory lock is the actual cross-process transaction lease.
    descriptor=os.open(str(args.manifest)+'.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    with os.fdopen(descriptor,'w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        prior=receipt(args.database,args.local_postgres,args.apply)
        if prior is not None:
            if any(prior.get(key)!=plan[key] for key in ('before_state_sha256','pointer_set_sha256','runtime_package_sha256')):
                raise RuntimeError('committed receipt identity differs')
            readback=post_readback(plan,args.database,args.local_postgres,prior)
            C.private_create(args.output,{'status':'DB_ALREADY_COMMITTED','receipt':prior,'readback':readback})
            return 0
        if transaction_running(args.database,args.local_postgres):
            raise RuntimeError('cleanup transaction lease is live; observe the same transaction before another dispatch')
        approval=read_bound(args.approval,args.approval_sha256)
        gates=validate_approval(plan,args.apply,approval,args.approval.parent,int(time.time()))
        validate_runtime(plan,args.apply,gates,args.local_postgres)
        statement=sql_for(plan,args.apply,args.approval_sha256,gates['post_activation_and_regular_ingest']['activation_manifest_sha256'])
        C.private_create(args.output,{'status':'DISPATCHED_OBSERVE_DATABASE_RECEIPT_ON_INTERRUPTION',
                                     'manifest_sha256':args.apply,'approval_sha256':args.approval_sha256})
        psql(args.database,args.local_postgres,statement,
             error_path=args.output.with_name(args.output.name+'.database-error.json'))
        committed=receipt(args.database,args.local_postgres,args.apply)
        if committed is None:
            raise RuntimeError('no durable commit receipt; do not assume cleanup succeeded')
        C.private_create(args.output.with_name(args.output.name+'.committed.json'),committed)
        validate_runtime(plan,args.apply,gates,args.local_postgres)
        readback=post_readback(plan,args.database,args.local_postgres,committed)
        C.private_create(args.output.with_name(args.output.name+'.postcheck.json'),readback)
        print(json.dumps({'status':readback['status'],'manifest_sha256':args.apply,
                          'pending_external_delete_count':plan['pending_external_delete_count']}))
    return 0


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except (RuntimeError,OSError,ValueError) as error:
        print(str(error),file=sys.stderr)
        raise SystemExit(2)
