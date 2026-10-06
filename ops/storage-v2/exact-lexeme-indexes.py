#!/usr/bin/env python3
"""Add two optional GIN probes under one local, resumable maintenance operation.

No imports connect to a database. Preparation captures the original catalog;
execution requires its exact private manifest digest and installed package digest.
An interrupted/invalid concurrent index is retained, never automatically retried.
The installed reader helpers are retained on rollback so the fallback stays usable.
"""
import argparse
from contextlib import ExitStack
from decimal import Decimal
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import queue
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid

HERE = Path(__file__).resolve().parent
GIB = 1024 ** 3
SCHEMA = 'mainrag.storage-v2.exact-lexeme-indexes.v1'
LOCK_KEY = 'storage-v2-posting-conversion-v1'
TABLES = ('storage_v2_compact_lexical_block', 'storage_v2_compact_posting_block')
INDEXES = ('idx_storage_v2_compact_lexical_exact_lexemes',
           'idx_storage_v2_compact_posting_exact_terms')
EXPRESSIONS = ('public.storage_v2_compact_lexical_exact_lexemes(fts_vectors)',
               'public.storage_v2_compact_posting_exact_term_keys(terms)')
SIGNATURES = (
    'storage_v2_compact_lexical_exact_lexemes(tsvector[])',
    'storage_v2_compact_posting_exact_term_keys(text[])',
    'storage_v2_exact_lexeme_probes_ready()',
    'storage_v2_authorized_cached_lexical_first_candidates(bigint[],bigint[],text)',
    'storage_v2_scoped_query_posting(bigint[],text[])')


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


P = module('exact_lexeme_posting_support', HERE / 'posting-compaction.py')
R = module('exact_lexeme_resource_support', HERE / 'release-candidate.py')
L = module('exact_lexeme_kernel_support', HERE / 'legacy_capacity.py')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def operator_digest():
    paths = (Path(__file__), HERE / 'posting-compaction.py', HERE / 'release-candidate.py',
             HERE / 'legacy_capacity.py', HERE.parent.parent /
             'migrations/159_storage_v2_exact_lexeme_probes.sql')
    return digest({p.name: P.file_sha(p) for p in paths})


def integer(value, minimum=1):
    if type(value) is not int or value < minimum:
        raise RuntimeError('a reviewed positive integer bound is required')
    return value


def catalog_oid(value):
    # PostgreSQL's JSON serializer represents OIDs as decimal strings, while
    # explicit BIGINT casts produce integers. Compare their typed OID value.
    if type(value) is str and re.fullmatch(r'0|[1-9][0-9]*', value):
        value = int(value)
    if type(value) is not int or not 0 <= value <= 4294967295:
        raise RuntimeError('catalog OID is not an unsigned PostgreSQL identifier')
    return value


def ddl(phase, drop=False):
    if type(phase) is not int or phase not in (0, 1):
        raise ValueError('unknown constant index phase')
    if drop:
        return 'DROP INDEX CONCURRENTLY public.' + INDEXES[phase]
    return ('CREATE INDEX CONCURRENTLY ' + INDEXES[phase] + ' ON public.' +
            TABLES[phase] + ' USING gin (' + EXPRESSIONS[phase] + ')')


def clean_environment():
    # In particular no PGPASSWORD/PGSERVICE, connection URLs or secret variables.
    return {k: os.environ[k] for k in ('PATH', 'LANG', 'LC_ALL') if k in os.environ}


class LocalDatabase:
    def __init__(self, config):
        name = config['database']
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,62}', name):
            raise RuntimeError('a local database identifier is required')
        socket = Path(config['socket']).resolve(strict=True)
        if not socket.is_dir() or not Path(config['socket']).is_absolute():
            raise RuntimeError('an explicit local Unix socket directory is required')
        self.database = name
        self.user_id = str(uuid.UUID(config['user_id']))
        peer_sudo = config.get('peer_admin_via_sudo', True)
        if type(peer_sudo) is not bool:
            raise RuntimeError('local peer administration mode must be explicit boolean')
        self.command = (['sudo', '-n', '-u', 'postgres'] if peer_sudo else []) + ['psql', '-X', '--no-psqlrc',
                        '-qAt', '-v', 'ON_ERROR_STOP=1', '-h', str(socket), '-d', name]

    def query(self, sql, arguments=(), *, readonly=True, timeout=15):
        parts = sql.split('%s')
        if len(parts) != len(arguments) + 1:
            raise ValueError('SQL argument count differs')
        statement = parts[0] + ''.join(P.literal(v) + tail for v, tail in zip(arguments, parts[1:]))
        text = ('BEGIN' + (' READ ONLY' if readonly else '') + ';' +
                'SET LOCAL app.user_id=' + P.literal(self.user_id) + ';' +
                'SET LOCAL statement_timeout=' + P.literal(str(timeout) + 's') + ';' +
                "SET LOCAL application_name='exact-lexeme-observer';" +
                "SELECT coalesce(jsonb_agg(to_jsonb(v)),'[]') FROM (" + statement + ') v;COMMIT;')
        result = subprocess.run(self.command, input=text, text=True, capture_output=True,
                                env=clean_environment(), timeout=timeout + 5)
        if result.returncode:
            raise RuntimeError('local catalog observation failed; raw server output is private')
        return json.loads(result.stdout)


class Session:
    """One peer-authenticated administrative backend; CIC stays in autocommit."""
    def __init__(self, db, operation):
        self.db = db
        self.app = 'exact-lexeme-' + str(uuid.UUID(operation))
        self.lines = queue.Queue()
        self.process = subprocess.Popen(db.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, bufsize=1,
                                        env=clean_environment())
        def output():
            for line in self.process.stdout:
                self.lines.put(line)
            self.lines.put(None)
        def discard_private_errors():
            # Drain stderr without publishing or retaining private SQL diagnostics.
            while self.process.stderr.read(4096):
                pass
        threading.Thread(target=output, daemon=True).start()
        threading.Thread(target=discard_private_errors, daemon=True).start()
        self.pending = None
        self.statement('SET application_name=' + P.literal(self.app) +
                       ';SET app.user_id=' + P.literal(db.user_id) +
                       ";SET lock_timeout='3s';SET statement_timeout='15s'")
        self.identity = self.scalar("SELECT jsonb_build_object('pid',pid,'start',backend_start::text," +
                                    "'app',application_name,'db',datname,'role',usename) " +
                                    'FROM pg_stat_activity WHERE pid=pg_backend_pid()')
        if self.scalar('SELECT pg_try_advisory_lock(hashtextextended(' + P.literal(LOCK_KEY) + ',0))') is not True:
            self.close()
            raise RuntimeError('another conversion/index operator holds the global session lock')

    def begin(self, text, expression="'DONE'::text"):
        if self.pending is not None:
            raise RuntimeError('the owned backend already has an unacknowledged statement')
        self.pending = str(uuid.uuid4())
        marker = 'SELECT jsonb_build_object(\'tag\',' + P.literal(self.pending) + ',\'value\',(' + expression + '));'
        self.process.stdin.write(text + ';' + marker + '\n')
        self.process.stdin.flush()

    def poll(self, timeout=0):
        try:
            line = self.lines.get(timeout=timeout)
        except queue.Empty:
            return False, None
        if line is None:
            raise RuntimeError('owned backend disconnected; outcome UNKNOWN, reconcile this operation')
        if not line.strip():
            return False, None  # Void guard SELECTs have an empty result row.
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            raise RuntimeError('unexpected owned backend protocol output; outcome UNKNOWN') from None
        if value.get('tag') != self.pending:
            raise RuntimeError('owned backend acknowledgement identity differs')
        self.pending = None
        return True, value['value']

    def statement(self, text):
        self.begin(text)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            done, value = self.poll(.2)
            if done:
                return value
        raise RuntimeError('owned backend acknowledgement timed out; outcome UNKNOWN')

    def scalar(self, select):
        self.begin('', select)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            done, value = self.poll(.2)
            if done:
                return value
        raise RuntimeError('owned backend readback timed out')

    def close(self):
        # Closing this client releases only its own session, never another backend.
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5)


# Same fields and OID ordering as the SQL157 relation fingerprint, with indexes
# separately represented to permit exactly the two audited additions.
CORE_SQL = """SELECT c.oid::bigint oid,c.relname name,c.reltablespace::bigint tablespace,jsonb_build_object(
 'owner',c.relowner,'acl',c.relacl,'kind',c.relkind,'options',c.reloptions,
 'rls',c.relrowsecurity,'force_rls',c.relforcerowsecurity,'view',CASE WHEN c.relkind='v' THEN pg_get_viewdef(c.oid,true) ELSE NULL END,
 'columns',(SELECT jsonb_agg(jsonb_build_array(a.attnum,a.attname,a.atttypid,a.atttypmod,
 a.attnotnull,a.attgenerated,a.attcompression,a.attcollation,pg_get_expr(d.adbin,d.adrelid)) ORDER BY a.attnum)
 FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
 WHERE a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped),
 'constraints',(SELECT jsonb_agg(jsonb_build_array(conname,convalidated,pg_get_constraintdef(oid)) ORDER BY conname) FROM pg_constraint WHERE conrelid=c.oid),
 'policies',(SELECT jsonb_agg(jsonb_build_array(polname,polcmd,polroles,polpermissive,pg_get_expr(polqual,polrelid),pg_get_expr(polwithcheck,polrelid)) ORDER BY polname) FROM pg_policy WHERE polrelid=c.oid),
 'triggers',(SELECT jsonb_agg(jsonb_build_array(tgname,tgenabled,tgfoid,pg_get_triggerdef(oid)) ORDER BY tgname) FROM pg_trigger WHERE tgrelid=c.oid)) core
 FROM pg_class c WHERE c.oid IN ('public.storage_v2_compact_lexical_block'::regclass,'public.storage_v2_compact_posting_block'::regclass) ORDER BY c.oid"""
INDEX_SQL = """SELECT i.indexrelid::bigint oid,i.indrelid::bigint table_oid,c.relname name,c.relowner owner,
 c.reloptions options,c.reltablespace::bigint tablespace,am.amname method,i.indisunique AS "unique",i.indisprimary primary_key,
 i.indisexclusion exclusion,i.indnatts natts,i.indnkeyatts nkeys,pg_get_expr(i.indpred,i.indrelid) predicate,
 i.indkey::text keys,pg_get_expr(i.indexprs,i.indrelid) expression,op.opcname opclass,
 op.opcnamespace::regnamespace::text opnamespace,i.indoption[0] option,i.indcollation[0]::bigint collation,
 i.indisvalid valid,i.indisready ready,i.indislive live,i.indcheckxmin checkxmin,
 pg_get_indexdef(i.indexrelid) definition
 FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_am am ON am.oid=c.relam
 JOIN pg_opclass op ON op.oid=i.indclass[0]
 WHERE i.indrelid IN ('public.storage_v2_compact_lexical_block'::regclass,'public.storage_v2_compact_posting_block'::regclass)
 ORDER BY i.indexrelid"""
FUNCTION_SQL = """SELECT p.oid::regprocedure::text signature,r.rolname owner,
 encode(sha256(convert_to(pg_get_functiondef(p.oid),'UTF8')),'hex') definition_sha256,
 encode(sha256(convert_to((to_jsonb(p)-'prosrc')::text,'UTF8')),'hex') metadata_sha256,
 p.provolatile volatility,p.proisstrict strict,p.proparallel parallel,p.prosecdef definer,
 p.proleakproof leakproof,p.prosupport::bigint support,l.lanname language,p.prorettype::regtype::text returns,
 p.proconfig config,(SELECT coalesce(jsonb_agg(jsonb_build_array(a.grantee::regrole::text,a.privilege_type,a.is_grantable)
 ORDER BY a.grantee,a.privilege_type),'[]') FROM aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a) acl
 FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner JOIN pg_language l ON l.oid=p.prolang
 WHERE p.pronamespace='public'::regnamespace AND p.prokind IN ('f','p') ORDER BY p.oid"""
CONTRACT_SQL = """SELECT relation_oid::bigint oid,encode(identity_sha256,'hex') pin,
 encode(storage_v2_posting_conversion_relation_sha256(relation_oid::regclass),'hex') actual
 FROM storage_v2_posting_conversion_catalog_contract ORDER BY relation_oid"""


def catalog(db):
    result = {'cores': db.query(CORE_SQL), 'indexes': db.query(INDEX_SQL),
              'functions': db.query(FUNCTION_SQL), 'contract': db.query(CONTRACT_SQL),
              'function_contract': db.query("SELECT signature,owner_oid::bigint owner,encode(definition_sha256,'hex') definition,encode(acl_sha256,'hex') acl FROM storage_v2_posting_conversion_contract ORDER BY signature"),
              'state': P.state_identities(db)}
    if len(result['cores']) != 2 or len(result['contract']) != 7 or len(result['function_contract']) != 23:
        raise RuntimeError('the installed relation/SQL157 catalog contract is incomplete')
    # Detect same-name objects on another table/schema, not just matching targets.
    result['names'] = db.query("SELECT n.nspname schema,c.relname name,c.oid::bigint oid FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relname IN (%s,%s) ORDER BY c.oid", INDEXES)
    return result


def exact_index(row, phase, snapshot):
    table = next(r for r in snapshot['cores'] if r['name'] == TABLES[phase])
    expected_expression = ('storage_v2_compact_lexical_exact_lexemes(fts_vectors)',
                           'storage_v2_compact_posting_exact_term_keys(terms)')[phase]
    checks = {'table_oid': table['oid'], 'owner': table['core']['owner'], 'method': 'gin',
              'options': None, 'tablespace': 0, 'unique': False, 'primary_key': False, 'exclusion': False,
              'natts': 1, 'nkeys': 1, 'predicate': None, 'keys': '0',
              'expression': expected_expression, 'opclass': 'array_ops', 'opnamespace': 'pg_catalog', 'option': 0}
    if any(row.get(k) != v for k, v in checks.items()):
        raise RuntimeError('a same-name index has a conflicting exact definition')
    if phase == 1:
        # Full-term SHA256 keys are BYTEA[], which have no collation. The raw
        # terms column remains untouched, including arbitrary-length literals.
        collation = 0
    else:
        # The reviewed installed catalog records the database default collation OID.
        collation = snapshot['default_collation_oid']
    if catalog_oid(row['collation']) != catalog_oid(collation):
        raise RuntimeError('exact index collation differs')
    return row['valid'] and row['ready'] and row['live'] and not row['checkxmin']


def delta(original, current, intents, accepted_pins=()):
    if original['cores'] != current['cores'] or original['functions'] != current['functions'] \
            or original['function_contract'] != current['function_contract'] or original['state'] != current['state']:
        raise RuntimeError('original columns, authority, functions, generation or pointer state drifted')
    old = {r['oid']: r for r in original['indexes']}
    now = {r['oid']: r for r in current['indexes']}
    if any(now.get(oid) != row for oid, row in old.items()):
        raise RuntimeError('an original index changed or disappeared')
    additions = {}
    for oid, row in now.items():
        if oid in old:
            continue
        if row['name'] not in INDEXES:
            raise RuntimeError('an unrelated index was added')
        phase = INDEXES.index(row['name'])
        if str(phase) not in intents or intents[str(phase)]['ddl_sha256'] != digest(ddl(phase)):
            raise RuntimeError('an index addition lacks this operation durable pre-DDL intent')
        additions[phase] = (row, exact_index(row, phase, original))
    names = [{'schema': 'public', 'name': r['name'], 'oid': r['oid']} for r, _ in additions.values()]
    if sorted(current['names'], key=lambda r: r['oid']) != sorted(names, key=lambda r: r['oid']):
        raise RuntimeError('same-name catalog objects are conflicting or outside the owned targets')
    before = {r['oid']: r for r in original['contract']}
    after = {r['oid']: r for r in current['contract']}
    compact = next(r['oid'] for r in original['cores'] if r['name'] == TABLES[1])
    if before.keys() != after.keys():
        raise RuntimeError('catalog contract membership changed')
    for oid, row in after.items():
        if oid != compact and row != before[oid]:
            raise RuntimeError('an unrelated SQL157 catalog pin changed')
        if oid == compact and row['pin'] not in (before[oid]['pin'], row['actual'], *accepted_pins):
            raise RuntimeError('compact catalog pin is neither original nor exact current identity')
        if oid == compact and 1 not in additions and row['actual'] != before[oid]['actual']:
            raise RuntimeError('compact catalog changed without the exact owned addition')
    return additions


def gate_helpers(snapshot, expected):
    functions = {r['signature']: r for r in snapshot['functions']}
    if set(expected) != set(SIGNATURES):
        raise RuntimeError('all five exact installed SQL159 function identities are required')
    for signature in SIGNATURES:
        row = functions.get(signature)
        if row is None or expected[signature] != {k: row[k] for k in ('definition_sha256', 'metadata_sha256')}:
            raise RuntimeError('installed helper/reader byte or authority identity differs')
        for sha in expected[signature].values():
            P.sha_bytes(sha)
    for i, owner, grants, volatility, strict, language, returns in (
            (0, 'mainrag_v2_lexical_rank_owner', {'mainrag_v2_lexical_rank_owner', 'mainrag_v2_frontier_owner'}, 'i', True, 'sql', 'text[]'),
            (1, 'mainrag_v2_lexical_rank_owner', {'mainrag_v2_lexical_rank_owner', 'mainrag_v2_frontier_owner', 'mainrag'}, 'i', True, 'sql', 'bytea[]'),
            (2, 'mainrag_v2_lexical_rank_owner', {'mainrag_v2_lexical_rank_owner', 'mainrag_v2_frontier_owner', 'mainrag'}, 's', False, 'plpgsql', 'boolean')):
        row = functions[SIGNATURES[i]]
        if row['owner'] != owner or row['volatility'] != volatility or row['strict'] != strict \
                or row['definer'] or row['parallel'] != 's' \
                or row['leakproof'] or catalog_oid(row['support']) != 0 \
                or row['language'] != language or row['returns'] != returns \
                or row['config'] != ['search_path=pg_catalog, public, pg_temp'] \
                or {r[0] for r in row['acl']} != grants \
                or any(r[1:] != ['EXECUTE', False] for r in row['acl']):
            raise RuntimeError('SQL159 helper owner, ACL, execution or planner properties differ')


def writer_gate(db, own_pid=None):
    row = db.query("""SELECT
      (SELECT count(*) FROM storage_v2_ingest_run WHERE status='building') building,
      (SELECT count(*) FROM logical_source WHERE active_generation_id IS NOT NULL) pointers,
      (SELECT count(*) FROM pg_locks l JOIN logical_source s ON
       l.classid::bigint=((hashtextextended('mainrag.storage-v2-ingest-source:'||s.id::text,0)>>32)&4294967295)
       AND l.objid::bigint=(hashtextextended('mainrag.storage-v2-ingest-source:'||s.id::text,0)&4294967295)
       WHERE l.locktype='advisory' AND l.granted AND l.objsubid=1) source_locks,
      (SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()
       AND pid<>%s AND state<>'idle' AND application_name NOT IN ('exact-lexeme-observer')
       AND query ~* '(insert|update|delete|merge|copy|create[[:space:]]+index|reindex|vacuum|cluster|refresh[[:space:]]+materialized|storage_v2_materialize_reader_metadata)') writers,
      (SELECT count(*) FROM pg_stat_progress_create_index WHERE pid<>%s) index_jobs""",
                   (own_pid or 0, own_pid or 0))[0]
    if any(v != 0 for v in row.values()):
        raise RuntimeError('writer, source-lock, active-pointer or index maintenance gate is not empty')
    return row


def runtime_gate(config, package):
    P.sha_bytes(package)
    receipt, sha = P.private_read(Path(config['runtime_admission_file']))
    if sha != config['runtime_admission_sha256'] or receipt.get('status') != 'PASS':
        raise RuntimeError('private existing runtime admission differs or is not PASS')
    expected = config['runtime_binding']
    if expected['package_sha256'] != package or receipt.get('commit') != expected['commit'] \
            or receipt.get('running_api', {}).get('pid') != expected['pid'] \
            or receipt.get('running_api', {}).get('binary_sha256') != expected['binary_sha256'] \
            or receipt.get('binaries', {}).get('mainrag-api', {}).get('sha256') != expected['binary_sha256'] \
            or receipt.get('api_features') != expected['api_features'] \
            or expected['storage_root'] != config['storage_root']:
        raise RuntimeError('existing installed receipt must bind unchanged package commit, runtime binary, PID, features and roots')
    # This uses the existing controlled-maintenance/drain record; it is evidence,
    # not an additional owner authorization or a synthesized approval.
    drained, drain_sha = P.private_read(Path(config['drain_receipt_file']))
    if drain_sha != config['drain_receipt_sha256'] \
            or drained.get('reader_package', {}).get('installation_receipt_sha256') != sha \
            or drained.get('reader_package', {}).get('binary_sha256') != expected['binary_sha256'] \
            or drained.get('reader_package', {}).get('commit_sha') != expected['commit'] \
            or drained.get('controlled_maintenance') is not True \
            or drained.get('model_gpu_jobs') != 0 or drained.get('model_cpu_jobs') != 0:
        raise RuntimeError('digest-bound existing drain evidence must prove controlled maintenance and no model jobs')
    for field in ('building_runs', 'registered_writer_locks', 'active_mutating_statements', 'active_pointers'):
        if drained.get('writer_free_admission', {}).get(field) != 0:
            raise RuntimeError('existing drain evidence does not establish the required writer-free maintenance scope')
    identity = list(P.runtime_identity(integer(expected['pid']), Path(expected['binary'])))
    if identity != expected['process_identity'] or P.file_sha(expected['binary']) != expected['binary_sha256']:
        raise RuntimeError('actual package runtime PID, binary bytes or process identity differs')


def bounds(config, phase, drop=False):
    reviewed = config['drop_bounds' if drop else 'bounds'][str(phase)]
    keys = {'temp_bytes', 'wal_bytes', 'ram_bytes', 'seconds'} | (set() if drop else {'index_bytes'})
    if set(reviewed) != keys:
        raise RuntimeError('explicit conservative per-index/temp/WAL/RAM/runtime bounds are required')
    for value in reviewed.values():
        integer(value)
    if reviewed['wal_bytes'] > 4 * GIB or reviewed['seconds'] > 24 * 3600:
        raise RuntimeError('reviewed WAL burst or runtime exceeds the bounded operator policy')
    if not drop:
        estimate = config['estimates'][str(phase)]
        integer(estimate['input_relation_bytes'])
        if not isinstance(estimate.get('conservative_method'), str) or len(estimate['conservative_method'].strip()) < 20 \
                or estimate.get('complete_existing_cache_arrays') is not True:
            raise RuntimeError('unknown index size cannot be admitted; a complete-cache conservative estimate is required')
    return reviewed


def resource_observation(config, db, growth=0):
    root, temp = Path(config['storage_root']), Path(config['temp_root'])
    pool = db.capacity.observe()
    if pool is None:
        raise RuntimeError('actual thin-pool identity is not proven')
    pool['projected_data_percent'] = float(Decimal(str(pool['data_percent_before_build'])) +
                                         Decimal(growth) * 100 / pool['pool_size_bytes'])
    if Decimal(str(pool['metadata_percent_before_build'])) >= 60:
        raise RuntimeError('actual thin-pool identity/metadata60 gate is not proven')
    limit = min(Decimal(75), Decimal(pool['autoextend_threshold_percent']) - 5)
    if Decimal(str(pool['projected_data_percent'])) > limit:
        raise RuntimeError('ordinary75/autoextend-margin5 projected pool admission failed')
    # Round upward at one allocation block, never credit an under-rounded byte.
    used = int(Decimal(str(pool['pool_size_bytes'])) * Decimal(str(pool['data_percent_before_build'])) / 100) + db.capacity.block_sectors * 512
    if int(pool['pool_size_bytes']) - used - growth < 42 * GIB:
        raise RuntimeError('physical thin-pool42GiB reserve failed')
    free = shutil.disk_usage(root).free
    root_free = shutil.disk_usage('/').free
    temp_free = shutil.disk_usage(temp).free
    if free - growth < 42 * GIB or root_free < 20 * GIB:
        raise RuntimeError('data42GiB/root20GiB filesystem reserve failed')
    db_info = db.query("SELECT current_setting('data_directory') directory,current_setting('temp_tablespaces') temp_spaces,current_setting('default_tablespace') default_space,(SELECT dattablespace=(SELECT oid FROM pg_tablespace WHERE spcname='pg_default') FROM pg_database WHERE datname=current_database()) default_database_space,(SELECT coalesce(sum(size),0)::bigint FROM pg_ls_waldir()) wal")[0]
    if os.stat(db_info['directory']).st_dev != os.stat(root).st_dev or db_info['temp_spaces'] \
            or db_info['default_space'] or db_info['default_database_space'] is not True:
        raise RuntimeError('DB data or nondefault temporary tablespaces differ from the admitted physical roots')
    if os.stat(temp).st_dev != os.stat(root).st_dev:
        raise RuntimeError('reviewed temporary root must cover the actual default DB filesystem')
    mem = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key, value = line.split(':', 1)
        mem[key] = int(value.strip().split()[0]) * 1024
    if db_info['wal'] >= 28 * GIB:
        raise RuntimeError('actual WAL reached high28GiB (hard stock32GiB remains enforced)')
    return {'pool': pool, 'used': used, 'free': free, 'root_free': root_free,
            'temp_free': temp_free, 'wal': db_info['wal'], 'ram': mem['MemAvailable']}


def admission(config, db, phase, drop=False):
    b = bounds(config, phase, drop)
    growth = b.get('index_bytes', 0) + b['temp_bytes'] + b['wal_bytes']
    r = resource_observation(config, db, growth)
    if r['wal'] > 24 * GIB or r['wal'] + b['wal_bytes'] > 28 * GIB:
        raise RuntimeError('WAL must be drained to low24GiB and the reviewed burst fit high28GiB')
    if r['ram'] < b['ram_bytes'] + 2 * GIB or r['temp_free'] < b['temp_bytes'] + 42 * GIB:
        raise RuntimeError('conservative RAM or temporary allocation admission failed')
    r['pool_absolute_ceiling'] = r['used'] + growth
    r['filesystem_absolute_floor'] = r['free'] - growth
    r['wal_absolute_ceiling'] = r['wal'] + b['wal_bytes']
    return r


def monitor_budget(config, db, phase, start, started, drop=False):
    # Already allocated index bytes are debited against a fixed phase ceiling,
    # rather than reserving the complete index bound again on every observation.
    b = bounds(config, phase, drop)
    r = resource_observation(config, db)
    size = db.query('SELECT coalesce(pg_total_relation_size(to_regclass(%s)),0)::bigint bytes',
                    ('public.' + INDEXES[phase],))[0]['bytes']
    if r['used'] > start['pool_absolute_ceiling'] or r['free'] < start['filesystem_absolute_floor'] \
            or r['wal'] > start['wal_absolute_ceiling'] or r['wal'] > 32 * GIB \
            or (not drop and size > b['index_bytes']) or r['ram'] < 2 * GIB \
            or time.monotonic() - started > b['seconds']:
        raise RuntimeError('owned index phase exceeded a reviewed physical/WAL/RAM/runtime bound')
    return r


def cancel_owned(db, identity, query):
    rows = db.query("""SELECT pg_cancel_backend(pid) cancelled FROM pg_stat_activity
        WHERE pid=%s AND backend_start::text=%s AND application_name=%s AND datname=%s
        AND usename=%s AND state='active' AND query=%s""",
                    tuple(identity[k] for k in ('pid', 'start', 'app', 'db', 'role')) + (query,), readonly=False)
    return bool(rows and rows[0]['cancelled'])


class Workflow:
    def __init__(self, db, session, manifest, state, save, config):
        self.db, self.session, self.manifest = db, session, manifest
        self.state, self.save, self.config = state, save, config

    def checkpoint(self, status):
        self.state['status'] = status
        self.state['updated_at_unix'] = time.time()
        self.save(self.state)

    def reconcile(self):
        current = catalog(self.db)
        current['default_collation_oid'] = self.manifest['original']['default_collation_oid']
        additions = delta(self.manifest['original'], current, self.state['intents'],
                          self.state.get('accepted_compact_pins', ()))
        compact = next(r for r in current['contract'] if r['oid'] ==
                       next(c['oid'] for c in current['cores'] if c['name'] == TABLES[1]))
        if compact['pin'] != compact['actual']:
            # Narrow compare-and-swap ONLY the known compact row after the entire
            # original catalog/function contract and owned additions were compared.
            self.state.setdefault('accepted_compact_pins', []).append(compact['actual'])
            self.checkpoint('CATALOG_PIN_ACK_PENDING')
            self.session.statement('DO $pin$ BEGIN UPDATE public.storage_v2_posting_conversion_catalog_contract '
                "SET identity_sha256=decode(" + P.literal(compact['actual']) + ",'hex') WHERE relation_oid=" +
                str(compact['oid']) + " AND identity_sha256=decode(" + P.literal(compact['pin']) + ",'hex');" +
                "IF NOT FOUND THEN RAISE EXCEPTION 'compact catalog compare-and-swap failed'; END IF;" +
                'PERFORM public.storage_v2_posting_conversion_require_operator();END $pin$')
        else:
            self.session.statement('SELECT public.storage_v2_posting_conversion_require_operator()')
        self.state['reconciled'] = {str(i): {'oid': row['oid'], 'valid_ready': ready}
                                    for i, (row, ready) in additions.items()}
        self.checkpoint('RECONCILED')
        return additions

    def run_phase(self, phase, drop=False):
        runtime_gate(self.config, self.manifest['package_sha256'])
        writer_gate(self.db, self.session.identity['pid'])
        start = admission(self.config, self.db, phase, drop)
        text = ddl(phase, drop)
        self.session.statement("SET statement_timeout=" + P.literal(str(bounds(self.config, phase, drop)['seconds']) + 's') +
            ';SET maintenance_work_mem=' + P.literal(str(max(1, bounds(self.config, phase, drop)['ram_bytes'] // 1024)) + 'kB') +
            ';SET max_parallel_maintenance_workers=0')
        if not drop:
            if str(phase) in self.state['intents']:
                raise RuntimeError('an earlier phase intent has no index acknowledgement; reconcile without retry')
            self.state['intents'][str(phase)] = {'ddl_sha256': digest(text), 'backend': self.session.identity,
                                               'before': start, 'at_unix': time.time()}
        else:
            if str(phase) in self.state.get('rollback_intents', {}):
                raise RuntimeError('an earlier DROP remains unacknowledged; reconcile without automatic retry')
            self.state.setdefault('rollback_intents', {})[str(phase)] = {
                'ddl_sha256': digest(text), 'backend': self.session.identity, 'before': start}
        self.checkpoint('DROP_PENDING' if drop else 'DDL_PENDING')
        started = time.monotonic()
        try:
            self.session.begin(text)
            while True:
                done, _ = self.session.poll(.2)
                if done:
                    break
                monitor_budget(self.config, self.db, phase, start, started, drop)
                runtime_gate(self.config, self.manifest['package_sha256'])
                writer_gate(self.db, self.session.identity['pid'])
                time.sleep(1)
            monitor_budget(self.config, self.db, phase, start, started, drop)
        except Exception:
            try:
                self.state['cancelled_exact_owned_backend'] = cancel_owned(self.db, self.session.identity, text + ';')
            finally:
                self.checkpoint('OUTCOME_UNKNOWN_RECONCILE_REQUIRED')
            raise
        self.reconcile()

    def build(self):
        additions = self.reconcile()
        for phase in range(2):
            if phase in additions:
                if not additions[phase][1]:
                    self.checkpoint('INVALID_INDEX_RETAINED')
                    raise RuntimeError('owned invalid/incomplete index retained; explicit reconciliation or rollback required')
                continue
            self.run_phase(phase)
            additions = self.reconcile()
        if set(additions) != {0, 1} or not all(v[1] for v in additions.values()):
            raise RuntimeError('both exact owned indexes are not valid and ready')
        if self.session.scalar('SELECT public.storage_v2_exact_lexeme_probes_ready()') is not True:
            raise RuntimeError('installed two-index ready gate did not acknowledge completion')
        self.checkpoint('COMPLETE_TWO_EXACT_INDEXES_READY')

    def rollback(self):
        additions = self.reconcile()
        # No helper drop: the installed159 readers depend on all three helpers and
        # their fallback is precisely what makes a two-index rollback safe.
        self.state['helpers_retained_for_installed_reader'] = list(SIGNATURES[:3])
        for phase in (1, 0):
            if phase in additions:
                self.run_phase(phase, drop=True)
                additions = self.reconcile()
        current = catalog(self.db)
        if current['contract'] != self.manifest['original']['contract']:
            raise RuntimeError('rollback did not restore the original compact catalog contract')
        if self.session.scalar('SELECT public.storage_v2_exact_lexeme_probes_ready()') is not False:
            raise RuntimeError('reader fallback not restored after owned-index rollback')
        self.checkpoint('ROLLED_BACK_ORIGINAL_CATALOG_HELPERS_RETAINED')


def private_lock(path):
    # Reuse protected ownership/mode checks without truncating a retained slot.
    parent = Path(path).parent
    parent_stat = parent.stat()
    owners = {os.geteuid(), int(os.environ.get('SUDO_UID', '-1'))}
    if parent_stat.st_mode & 0o077 or parent_stat.st_uid not in owners:
        raise RuntimeError('operator lock parent must be private')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_uid not in owners:
        os.close(fd)
        raise RuntimeError('operator slot must be private and operator-owned')
    stream = os.fdopen(fd, 'a')
    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return stream


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'build', 'reconcile', 'rollback'))
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--config', type=Path, help='private reviewed local bounds and runtime binding; prepare only')
    parser.add_argument('--expected-manifest-sha256')
    parser.add_argument('--expected-package-sha256', required=True)
    args = parser.parse_args()
    P.sha_bytes(args.expected_package_sha256)
    if args.action == 'prepare':
        if args.config is None or args.manifest.exists():
            raise RuntimeError('preparation needs a private config and a new manifest path')
        config, config_sha = P.private_read(args.config)
        state = {'operation_id': str(uuid.uuid4()), 'intents': {}, 'status': 'PREPARING'}
    else:
        manifest, sha = P.private_read(args.manifest)
        if sha != args.expected_manifest_sha256 or manifest['schema_version'] != SCHEMA \
                or manifest['operator_sha256'] != operator_digest() \
                or manifest['package_sha256'] != args.expected_package_sha256:
            raise RuntimeError('exact private manifest/package/operator binding differs')
        config = manifest['config']
        state, _ = P.private_read(args.manifest.with_suffix('.state.json'))
        if state.get('manifest_sha256') != sha or state.get('operation_id') != manifest['operation_id']:
            raise RuntimeError('durable state belongs to a different operation or manifest')
    runtime_gate(config, args.expected_package_sha256)
    db = LocalDatabase(config)
    for phase in range(2):
        bounds(config, phase)
        bounds(config, phase, drop=True)
    with ExitStack() as stack:
        stack.enter_context(private_lock(args.manifest.with_suffix('.lock')))
        stack.enter_context(private_lock(Path(config['shared_operator_slot'])))
        # Pack writers hold a shared flock; this exclusive lock spans all DDL.
        maintenance = Path(config['storage_root']) / '.maintenance.lock'
        fd = os.open(maintenance, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        stream = stack.enter_context(os.fdopen(fd, 'a'))
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise RuntimeError('the pack maintenance lock must remain a regular local file')
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        session = Session(db, state['operation_id'])
        stack.callback(session.close)
        writer_gate(db, session.identity['pid'])
        def full_pool_check():
            observed = R.thin_pool_capacity(Path(config['storage_root']), 0, require_estimate=False)
            if observed is None:
                raise RuntimeError('actual physical thin-pool identity is unavailable')
            if Decimal(str(observed['metadata_percent_before_build'])) >= 60:
                raise RuntimeError('actual thin-pool metadata60 admission failed')
            return dict(observed, maximum_metadata_percent=60)
        db.capacity = L.LiveKernelCapacity(full_pool_check, config['storage_root'])
        stack.callback(db.capacity.observer.close)
        if args.action == 'prepare':
            session.statement('SELECT public.storage_v2_posting_conversion_require_operator()')
            original = catalog(db)
            original['default_collation_oid'] = db.query("SELECT 'default'::regcollation::oid::bigint oid")[0]['oid']
            if original['names'] or any(r['pin'] != r['actual'] for r in original['contract']):
                raise RuntimeError('initial indexes must be absent and the original seven catalog pins exact')
            if any(r['tablespace'] != 0 for r in original['cores']):
                raise RuntimeError('target relations must use the admitted default DB tablespace')
            gate_helpers(original, config['installed_functions'])
            for phase in range(2):
                actual = db.query('SELECT pg_total_relation_size(%s::regclass)::bigint bytes',
                                  ('public.' + TABLES[phase],))[0]['bytes']
                if actual != config['estimates'][str(phase)]['input_relation_bytes']:
                    raise RuntimeError('conservative estimate does not bind the actual original cache relation allocation')
                admission(config, db, phase)
            manifest = {'schema_version': SCHEMA, 'operation_id': state['operation_id'],
                'operator_sha256': operator_digest(), 'package_sha256': args.expected_package_sha256,
                'config_sha256': config_sha, 'config': config, 'original': original,
                'created_at_unix': time.time(), 'owner_uid': os.geteuid()}
            R.atomic_private_json(args.manifest, manifest, replace=False)
            _, sha = P.private_read(args.manifest)
            state.update(manifest_sha256=sha, status='PREPARED_NO_DDL')
            R.atomic_private_json(args.manifest.with_suffix('.state.json'), state, replace=False)
            print(json.dumps({'status': state['status'], 'manifest_sha256': sha}))
            return
        gate_helpers(catalog(db), config['installed_functions'])
        workflow = Workflow(db, session, manifest, state,
            lambda value: R.atomic_private_json(args.manifest.with_suffix('.state.json'), value), config)
        try:
            getattr(workflow, args.action)()
        except Exception:
            if state['status'] not in ('INVALID_INDEX_RETAINED', 'OUTCOME_UNKNOWN_RECONCILE_REQUIRED'):
                workflow.checkpoint('STOPPED_RECONCILE_REQUIRED')
            raise
        print(json.dumps({'status': state['status'], 'operation_id': state['operation_id']}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Exact lexeme operator stopped: ' + type(error).__name__ +
              '; preserve this operation and reconcile its private manifest/state.', file=sys.stderr)
        raise SystemExit(1) from None
