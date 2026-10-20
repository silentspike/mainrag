#!/usr/bin/env python3
"""Refresh only live metadata for an unchanged, previously accepted candidate set.

Capture watermarks with activation-set.py before stopping the API. This command
runs afterwards, under the existing atomic activation decision. It never stops
services, sends source/qualification requests, reads source bodies, dumps a
schema/database, benchmarks queries, changes pointers or dispatches activation.
Old acceptance and measurements remain immutable historical evidence.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import runpy
import shlex
import shutil
import stat
import subprocess
import time
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
GIB = 1024 ** 3
MIB = 1024 ** 2
MAX_METADATA = 32 * MIB
GATES = frozenset({"live_adapter_watermarks", "writer_and_maintenance_inventory",
    "current_package_and_schema", "representative_gold_review",
    "per_class_and_aggregate_quality", "cumulative_resource_and_recovery_budget",
    "benchmark_result", "legacy_state_readback"})
STATIC = frozenset({"representative_gold_review", "per_class_and_aggregate_quality"})
VOLATILE = GATES - STATIC
SCHEMA = "mainrag.storage-v2.activation-evidence-refresh.v1"
CONFIG_SCHEMA = "mainrag.storage-v2.activation-evidence-refresh-config.v1"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def reference(path):
    return {"path": str(path), "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()}


def checked_ref(value):
    require(isinstance(value, dict) and set(value) == {"path", "sha256"}
            and isinstance(value["path"], str) and Path(value["path"]).is_absolute()
            and isinstance(value["sha256"], str)
            and re.fullmatch(r"[0-9a-f]{64}", value["sha256"]), "exact protected metadata reference required")
    return value


def read_reference(value):
    checked_ref(value)
    path = Path(value["path"])
    require(path.resolve() == path, "canonical metadata path required")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                and stat.S_IMODE(before.st_mode) == 0o600 and 0 < before.st_size <= MAX_METADATA,
                "bounded private metadata required")
        raw = stream.read(MAX_METADATA + 1)
        after = os.fstat(stream.fileno())
    stamp = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_uid, s.st_mode)
    require(stamp(before) == stamp(after) and len(raw) == before.st_size
            and hashlib.sha256(raw).hexdigest() == value["sha256"], "protected metadata changed")
    def unique(pairs):
        result = {}
        for key, item in pairs:
            require(key not in result, "duplicate metadata key")
            result[key] = item
        return result
    result = json.loads(raw, object_pairs_hook=unique)
    require(isinstance(result, dict), "metadata object required")
    return result


def operators(directory=HERE):
    """Load existing supported operators without executing their CLIs."""
    names = {"activation": "activation-set.py", "inventory": "candidate-inventory.py",
             "audit": "candidate-aggregate-audit.py", "preflight": "preflight.py",
             "backup": "local-backup.py", "cleanup": "cleanup-plan.py",
             "switch": "default-read-switch.py"}
    return SimpleNamespace(**{key: SimpleNamespace(**runpy.run_path(str(Path(directory) / name)))
                              for key, name in names.items()})


def historical_acceptance(audit, accepted, audit_ref, read, now):
    candidates = audit["candidate_set"]
    require(audit["schema_version"] == "mainrag.storage-v2.candidate-aggregate-audit.v2"
            and audit["persisted_candidate_set_complete"] is True and not audit["persisted_gate_blockers"]
            and candidates and len({r["source_id"] for r in candidates}) == len(candidates)
            and audit["candidate_set_sha256"] == digest(candidates), "complete historical candidate audit required")
    require(accepted["schema_version"] == "mainrag.storage-v2.aggregate-acceptance.v1"
            and accepted["status"] == "PASS" and accepted["persisted_audit_sha256"] == audit_ref["sha256"]
            and accepted["candidate_set_sha256"] == audit["candidate_set_sha256"]
            and accepted["source_count"] == len(candidates) and set(accepted["external_gates"]) == GATES,
            "original complete aggregate acceptance required")
    for key in ("schema_sha256", "backend_package_sha256", "installed_binary_sha256"):
        require(re.fullmatch(r"[0-9a-f]{64}", accepted[key]), "accepted package identity missing")
    for key in ("code_commit_sha", "preflight_operator_commit_sha"):
        require(re.fullmatch(r"[0-9a-f]{40}", accepted[key]), "accepted code identity missing")
    watermarks = accepted["current_source_watermarks"]
    require(len(watermarks) == len(candidates)
            and {r["source_id"]: r["watermark_sha256"] for r in watermarks}
                == {r["source_id"]: r["source_watermark_sha256"] for r in candidates}
            and all(type(r["observed_at_unix"]) is int and 0 < r["observed_at_unix"] <= now for r in watermarks),
            "historical accepted watermark set differs")
    original_map = digest([{'source_id': row['source_id'], 'generation_id': row['candidate_generation_id'],
        'commit_sha': row['candidate_commit_sha'], 'source_watermark_sha256': row['source_watermark_sha256']}
        for row in sorted(candidates, key=lambda row: row['source_id'])])
    proofs = {}
    for name in sorted(GATES):
        entry = accepted["external_gates"][name]
        ref = checked_ref(entry["evidence_reference"])
        require(entry["status"] == "PASS" and entry["evidence_sha256"] == ref["sha256"],
                "original gate evidence reference differs")
        proof = read(ref)
        require(proof["status"] == "PASS" and proof["gate"] == name
                and proof["raw_observation_references"]
                and (proof.get("candidate_set_sha256") == audit["candidate_set_sha256"]
                     or proof.get("candidate_map_sha256") == original_map), "original gate evidence missing or different candidate scope")
        for origin in proof["raw_observation_references"]:
            read(checked_ref(origin))
        proofs[name] = proof
    return proofs


def atomic_budget(config, request, decision, *, wall, mono, boot):
    require(decision["actual_default_runtime"] == config["reader_receipt"]
            and decision["default_binding"] == request["default_binding"]
            and decision["approved"] is True and decision["root_reviewer_uid"] == 0
            and decision["independent_review_claimed"] is False
            and decision["purpose"] == "ATOMIC_ACTIVATION_AND_DEFAULT_SWITCH_ONLY"
            and decision["index_authority_inherited"] is False and decision["source_work_admitted"] is False
            and decision["counters_reset"] is decision["baseline_rebased"] is False
            and decision["subject_payload_sha256"] == digest({k: v for k, v in request.items() if k != "capacity_purpose_decision"})
            and request["capacity_purpose_decision"] == config["capacity_decision"]
            and request["candidate_audit"] == config["audit"]
            and request["persisted66_acceptance"] == config["accepted"]
            and request["limits"] == decision["limits"]
            and request["archive_command_sha256"] == config["archive_command_sha256"]
            and request["spool_root"] == config["spool_root"]
            and request["global_wal_origin"] == config["global_wal_origin"]
            and request["maximum_generated_wal_bytes"] == 96 * GIB, "same original atomic activation approval required")
    times = ("at_unix", "at_monotonic", "valid_until_unix", "valid_until_monotonic")
    require(all(type(decision[k]) in (int, float) and math.isfinite(decision[k]) for k in times)
            and decision["boot_id"] == request["boot_id"] == boot
            and decision["at_unix"] <= wall < decision["valid_until_unix"]
            and decision["at_monotonic"] <= mono < decision["valid_until_monotonic"]
            and abs((decision["valid_until_unix"] - decision["at_unix"])
                    - (decision["valid_until_monotonic"] - decision["at_monotonic"])) <= 2
            and 0 < decision["valid_until_monotonic"] - decision["at_monotonic"]
                <= request["limits"]["maximum_work_seconds"] + request["limits"]["stop_tail_seconds"],
            "finite same-boot original atomic deadline required")
    require(type(decision["maximum_pool_percent"]) is int and decision["maximum_pool_percent"] in (85, 95)
            and decision["pool_uuid"] == request["expected_pool_uuid"]
            and decision["minimum_physical_free_bytes"] == 42 * GIB
            and decision["maximum_metadata_percent"] == 60 and decision["autoextend_credit_bytes"] == 0,
            "original85or95 physical42 metadata60 no-autoextend required")


def legacy_query(baseline):
    """The supported legacy observer, restricted to its original relation scope."""
    names = [r["object_identity"] for r in baseline["postgresql_legacy_objects"]]
    allowed = {"public." + name for name in ("files", "chunks", "embeddings", "chunk_embeddings", "indexing_outbox")}
    require(names and len(names) == len(set(names)) and set(names) <= allowed, "supported legacy relation scope required")
    counted = ",".join("('" + name + "',(SELECT count(*) FROM public.\"" + name.split('.')[1] + '\"))' for name in sorted(names))
    return """SELECT jsonb_build_object('postgresql_legacy_objects',(
      SELECT jsonb_agg(jsonb_build_object('object_identity',identity,'row_count',row_count,
        'definition_sha256',encode(sha256(convert_to(jsonb_build_object(
          'kind',c.relkind,'owner',c.relowner,'acl',c.relacl,'rls',c.relrowsecurity,'force',c.relforcerowsecurity,
          'columns',(SELECT jsonb_agg(jsonb_build_array(a.attname,format_type(a.atttypid,a.atttypmod),
            a.attnotnull,pg_get_expr(d.adbin,d.adrelid)) ORDER BY a.attnum)
            FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
            WHERE a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped),
          'indexes',(SELECT jsonb_agg(pg_get_indexdef(i.indexrelid) ORDER BY i.indexrelid)
            FROM pg_index i WHERE i.indrelid=c.oid),
          'constraints',(SELECT jsonb_agg(pg_get_constraintdef(k.oid) ORDER BY k.conname)
            FROM pg_constraint k WHERE k.conrelid=c.oid),
          'triggers',(SELECT jsonb_agg(pg_get_triggerdef(t.oid) ORDER BY t.tgname)
            FROM pg_trigger t WHERE t.tgrelid=c.oid AND NOT t.tgisinternal)
        )::text,'UTF8')),'hex')) ORDER BY identity)
      FROM (VALUES """ + counted + """) counted(identity,row_count)
      JOIN pg_class c ON c.oid=to_regclass(identity)),
      'outbox_state',(SELECT jsonb_build_object('total',count(*),'pending',count(*) FILTER(WHERE status='pending'),
        'processing',count(*) FILTER(WHERE status='processing'),'done',count(*) FILTER(WHERE status='done'),
        'failed',count(*) FILTER(WHERE status='failed'),'unknown_status',count(*) FILTER(WHERE status NOT IN
        ('pending','processing','done','failed'))) FROM indexing_outbox),
      'active_pointers',(SELECT jsonb_agg(jsonb_build_object('source_id',id,
        'active_generation_id',active_generation_id) ORDER BY id) FROM logical_source))"""


CATALOG_SQL = """SELECT jsonb_build_object(
 'functions',(SELECT jsonb_object_agg(p.oid::regprocedure::text,
    encode(sha256(convert_to(pg_get_functiondef(p.oid),'UTF8')),'hex'))
    FROM pg_proc p WHERE p.pronamespace='public'::regnamespace AND p.prokind='f'),
 'authorities',(SELECT jsonb_object_agg(p.oid::regprocedure::text,
    (to_jsonb(p)-'prosrc')||jsonb_build_object('proargtypes',to_jsonb(p.proargtypes::oid[])))
    FROM pg_proc p WHERE p.pronamespace='public'::regnamespace AND p.prokind='f'),
 'wal_bytes',(SELECT coalesce(sum(size),0) FROM pg_ls_waldir()),
 'wal_ready_bytes',storage_v2_local_wal_ready_bytes(),
 'wal_lsn',pg_current_wal_insert_lsn()::text,
 'data_directory',current_setting('data_directory'),
 'archive_command_sha256',encode(sha256(convert_to(current_setting('archive_command'),'UTF8')),'hex'))"""


def allocated_files(directory, *, wal=False, deadline=None):
    """Actual physical stock; only a disappearing direct WAL24 leaf retries.

    No partial sum is returned. Missing roots, other leaves, permissions and
    identity changes remain failures. This is a finite observation, not a daemon.
    """
    directory = Path(directory)
    require(directory.resolve() == directory and directory.is_dir(), 'canonical stock directory required')
    initial = directory.stat()
    boot_path = Path('/proc/sys/kernel/random/boot_id')
    boot = boot_path.read_text().strip()
    end = min(time.monotonic() + 2, deadline if deadline is not None else float('inf'))
    attempts = 0
    while True:
        require(attempts < 64 and time.monotonic() < end, 'finite complete WAL observation failed')
        attempts += 1
        current = directory.stat()
        require((initial.st_dev, initial.st_ino) == (current.st_dev, current.st_ino)
                and boot_path.read_text().strip() == boot, 'stock root or boot changed')
        try:
            total = 0
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except FileNotFoundError as error:
                        if not (wal and error.errno == 2 and re.fullmatch(r'[0-9A-F]{24}', entry.name)
                                and error.filename is not None and Path(error.filename) == directory / entry.name):
                            raise
                        raise _WalEntryRace() from error
                    require(not stat.S_ISLNK(info.st_mode), 'stock symlink cannot be counted as local payload')
                    if stat.S_ISREG(info.st_mode):total += info.st_blocks * 512
            final = directory.stat()
            require((initial.st_dev, initial.st_ino) == (final.st_dev, final.st_ino)
                    and boot_path.read_text().strip() == boot, 'stock root or boot changed')
            return total
        except _WalEntryRace:
            continue


class _WalEntryRace(Exception):
    pass


class LiveObservations:
    """Only local metadata operations; every used PASS is derived below."""
    def __init__(self, config, ops, decision, read=read_reference):
        self.c, self.o, self.d, self.read = config, ops, decision, read
        self.boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()

    def check(self):
        require(time.time() < self.d['valid_until_unix'] and time.monotonic() < self.d['valid_until_monotonic']
                and Path('/proc/sys/kernel/random/boot_id').read_text().strip() == self.boot,
                'original finite refresh deadline or boot changed')
        self.o.activation.require_stopped_api()

    def sql(self, text):
        self.check()
        seconds = min(30, self.d['valid_until_monotonic'] - time.monotonic())
        require(seconds > 0, 'no readonly statement after original deadline')
        statement = "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;SET LOCAL row_security=off;SET LOCAL search_path=pg_catalog,public;"
        statement += "SET LOCAL statement_timeout='" + str(max(1, int(seconds * 1000))) + "ms';SET LOCAL lock_timeout='3s';"
        result = json.loads(self.o.activation.psql(self.c['database'], statement + text + ';COMMIT;', True, readonly=True))
        self.check()
        return result

    def watermarks(self, candidates, accepted):
        self.check()
        args = SimpleNamespace(database=self.c['database'], local_postgres=True,
            quiesced_watermarks=Path(self.c['quiesced_watermarks']['path']),
            quiesced_watermarks_sha256=self.c['quiesced_watermarks']['sha256'],
            immutable_watermarks=Path(self.c['immutable_watermarks']['path']),
            immutable_watermarks_sha256=self.c['immutable_watermarks']['sha256'])
        self.o.activation.verify_activation_watermarks(args, candidates, accepted)
        self.check()
        return self.read(self.c['quiesced_watermarks'])

    def inventory(self, expected_reader):
        statement = self.o.inventory.INVENTORY_SQL
        rows = self.sql(statement[statement.index('SELECT '):statement.rindex('COMMIT;')])
        # A reader upgrade is not a new generation producer for retained sources.
        accepted = self.read(self.c['audit'])['candidate_set']
        producer_map = {'schema_version': 'mainrag.storage-v2.final-candidate-commit-map.v1',
            'sources': [{key: row[key] for key in ('source_id', 'candidate_generation_id', 'candidate_commit_sha')}
                        for row in accepted]}
        inventory, _ = self.o.inventory.capture(rows, self.c['operator_commit_sha'], None,
            candidate_commit_map=producer_map, candidate_commit_map_sha256=digest(producer_map))
        audit, _ = self.o.audit.audit(inventory, digest(inventory), expected_reader)
        require(audit['persisted_candidate_set_complete'] is True and not audit['persisted_gate_blockers'],
                'fresh persisted qualification audit failed')
        return {'inventory': inventory, 'audit': audit}

    def package(self):
        self.check()
        binary = Path(self.c['installed_binary'])
        require(not binary.is_symlink() and binary.resolve() == self.o.switch.API_BINARY.resolve(),
                'actual supported installed API executable required')
        catalog = self.sql(CATALOG_SQL)
        # Original metadata query includes only catalog, pack metadata and writer
        # locks. Its short-lived exported snapshot is not consumed by any dump.
        metadata = self.sql(self.o.backup.METADATA_SQL)
        return {'catalog': catalog, 'metadata': metadata,
                'binary_sha256': reference(self.c['installed_binary'])['sha256']}

    def maintenance(self, policy):
        apps = policy['allowed_database_applications']
        require(all(re.fullmatch(r'[A-Za-z0-9_. -]*', a) for a in apps), 'bounded declared database applications required')
        prefix = "SET LOCAL mainrag.preflight_allowed_apps='" + ','.join(apps) + "';"
        snapshot = self.sql(prefix + self.o.preflight.DATABASE_SNAPSHOT_SQL)
        units = self.o.preflight.collect_units(policy)
        self.check()
        client = self.o.preflight.run(['sudo', '-n', '-u', 'postgres', 'psql', '--version']).stdout
        return {'database': snapshot, 'units': units, 'client_version': self.o.preflight.parse_version(client)}

    def backup(self, expected_identity):
        self.check()
        result = self.o.backup.validate_evidence(Path(self.c['backup_evidence']['path']), 129600,
            int(time.time()), expected_identity=expected_identity)
        self.check()
        return result

    def legacy(self, baseline):
        value = self.sql(legacy_query(baseline))
        vector = self.o.cleanup.qdrant_inventory(self.c['qdrant_url'],
            Path(self.c['qdrant_key_file']) if self.c.get('qdrant_key_file') else None)
        value['qdrant_collections'] = sorted([{'collection_identity': r['name'], 'point_count': r['exact_point_count']}
                                             for r in vector['collections']], key=lambda r: r['collection_identity'])
        value['qdrant_aliases'] = sorted([{'alias_identity': r['alias_name'], 'collection_identity': r['collection_name']}
                                        for r in vector['aliases']], key=lambda r: r['alias_identity'])
        switch = self.o.switch
        require(not switch.ENV_FILE.exists() and not switch.ENV_FILE.is_symlink()
                and not switch.DROPIN.exists() and not switch.DROPIN.is_symlink(), 'default selector files changed before activation')
        # Capture only the two selector fields, never persist the environment or
        # credentials. Stopped service configuration remains observable.
        raw = switch.systemctl('show', switch.UNIT, '--property=Environment', '--value')
        fields = shlex.split(raw)
        selectors = (switch.ENV_NAME, switch.ACTIVE_COMMIT_ENV_NAME)
        require(not any(v.partition('=')[0] in selectors and v.partition('=')[2] for v in fields),
                'default service selector already changed')
        env_files = shlex.split(switch.systemctl('show', switch.UNIT, '--property=EnvironmentFiles', '--value'))
        require(len(env_files) % 2 == 0 and len(env_files) <= 32, 'bounded declared selector environment files required')
        for path, mode in zip(env_files[::2], env_files[1::2]):
            require(mode in ('(ignore_errors=yes)', '(ignore_errors=no)'), 'unsupported environment file declaration')
            file = Path(path)
            if not file.exists():
                require(mode == '(ignore_errors=yes)' and not file.is_symlink(), 'declared environment file missing')
                continue
            info = file.lstat()
            require(file.is_absolute() and file.resolve() == file and stat.S_ISREG(info.st_mode)
                    and info.st_nlink == 1 and not info.st_mode & 0o022 and info.st_size <= MIB,
                    'trusted bounded selector environment file required')
            for line in file.read_text().splitlines():
                key, equals, selected = line.strip().partition('=')
                require(not (equals and key in selectors and selected.strip() not in ('', '""', "''")),
                        'declared default selector already changed')
        value['default_read_selector'] = 'legacy'
        self.check()
        return {'legacy': value, 'vector_metadata': vector,
                'selector_observation': 'STOPPED_API_NO_ACTIVATION_FILES_OR_SELECTOR_ENV'}

    def resources(self, request, kernel, package):
        self.check()
        backup = self.o.backup
        plan = self.read(self.read(self.c['backup_evidence'])['plan'])
        pool = backup.pool_observation(plan['thin_pool'])
        filesystem = backup.local_filesystem(Path(self.c['pack_root']), plan['thin_pool'])
        spool = Path(self.c['spool_root'])
        require(spool.resolve() == spool and spool.is_dir() and filesystem == plan['filesystem'],
                'actual pack filesystem or archive spool changed')
        # Do not cache stock. Any disappearing spool entry refuses this entire
        # observation; no partial sum can become a passing resource observation.
        spool_bytes = allocated_files(spool, deadline=self.d['valid_until_monotonic'])
        free = shutil.disk_usage(self.c['pack_root']).free
        catalog = self.sql(CATALOG_SQL)
        require(catalog['functions'] == package['catalog']['functions']
                and catalog['authorities'] == package['catalog']['authorities'], 'reader changed during resource observation')
        lsn = lambda text: (int(text.split('/')[0], 16) << 32) + int(text.split('/')[1], 16)
        origin = self.read(self.c['global_wal_origin'])
        generated = lsn(catalog['wal_lsn']) - lsn(origin['wal_start_lsn'])
        wal_bytes = allocated_files(Path(catalog['data_directory']) / 'pg_wal', wal=True, deadline=self.d['valid_until_monotonic'])
        return {'pool': pool, 'filesystem': filesystem, 'physical_free_bytes': free,
                'spool_allocated_bytes': spool_bytes, 'generated_wal_bytes': generated,
                'wal_bytes': wal_bytes, 'wal_ready_bytes': catalog['wal_ready_bytes'],
                'archive_command_sha256': catalog['archive_command_sha256'], 'original_wal_origin': self.c['global_wal_origin']}

    def code(self):
        commit, clean = self.o.preflight.git_state()
        return {'operator_commit_sha': commit, 'tracked_clean': clean}


def refresh(config_ref, config, *, read, observe, save, now=time.time, mono=time.monotonic):
    """One metadata refresh; callbacks are fixed to LiveObservations in the CLI."""
    checked_ref(config_ref)
    require(read(config_ref) == config and config['schema_version'] == CONFIG_SCHEMA, 'actual reviewed configuration reference differs')
    review = read(config['root_review'])
    require(review['status'] == 'ROOT_APPROVED_SAME_SET_ACTIVATION_EVIDENCE_REFRESH'
            and review['approved'] is True and review['root_reviewer_uid'] == 0
            and review['independent_review_claimed'] is False
            and review['payload_sha256'] == digest({k: v for k, v in config.items() if k != 'root_review'})
            and review['method'] == reference(__file__)
            and type(review['at_unix']) in (int, float) and 0 <= now() - review['at_unix'] <= 120,
            'exact fresh Root refresh method and finite input review required')
    require(re.fullmatch(r'[A-Za-z0-9_-]+', config['database']), 'local database name required')
    audit = read(config['audit']); accepted = read(config['accepted'])
    proofs = historical_acceptance(audit, accepted, config['audit'], read, now())
    request = read(config['atomic_capacity_request']); decision = read(config['capacity_decision'])
    atomic_budget(config, request, decision, wall=now(), mono=mono(), boot=observe.boot)
    kernel = read(request['kernel_readback'])['resource']
    runtime = read(config['reader_receipt']); installation = read(config['reader_installation'])
    binding = read(request['default_binding']); writer_ref = binding['writer_runtime']; writer = read(writer_ref)
    require(all(binding[key] == config['reader_receipt'] for key in ('actual_runtime', 'consumer_runtime', 'actual176_runtime'))
            and runtime['parent_runtime'] == writer_ref and writer['function_identities'] == runtime['function_identities']
            and writer['schema_version'] == runtime['schema_version'], 'actual Default and writer binding lineage differs')
    require(runtime['function_identities'] == installation['after']
            and runtime['schema_version'] == installation['schema_after']
            and runtime['reader' + str(runtime['schema_version']) + '_installation'] == config['reader_installation']
            and runtime['binary_sha256'] == accepted['installed_binary_sha256']
            and all(proof['reader_installation_sha256'] == config['reader_receipt']['sha256'] for proof in proofs.values()),
            'same accepted actual reader installation and original binary required')
    expected_reader = audit['expected_reader_package']
    require(expected_reader['commit_sha'] == accepted['code_commit_sha']
            and expected_reader['binary_sha256'] == accepted['installed_binary_sha256']
            and expected_reader['installation_receipt_sha256'] == config['reader_receipt']['sha256'],
            'accepted reader code package identity changed')
    preflight = read(config['preflight']); policy = read(config['preflight_policy'])
    require(policy['backup_max_age_seconds'] == 129600, 'original mandatory backup freshness bound required')
    op = observe.o.activation
    op.validate_preflight(preflight, config['operator_commit_sha'], accepted['schema_sha256'], int(now()), maximum_age=300)
    require(preflight['candidate']['backend_lock_sha256'] == accepted['backend_package_sha256']
            and preflight['backup']['evidence_sha256'] == config['backup_evidence']['sha256'],
            'fresh preflight backend or mandatory backup identity differs')
    baseline = read(config['legacy_baseline'])
    require(config['legacy_baseline'] in proofs['legacy_state_readback']['raw_observation_references'],
            'legacy baseline must be actual accepted evidence')
    original_origin = read(config['global_wal_origin'])
    require(isinstance(original_origin['wal_start_lsn'], str) and kernel['original_origin'] == config['global_wal_origin'],
            'original cumulative WAL cap must be retained')
    before = observe.inventory(expected_reader)
    require(before['audit']['candidate_set'] == audit['candidate_set'], 'candidate set changed before refresh')
    package_at = now()
    current = observe.package(); catalog = current['catalog']; metadata = current['metadata']
    require(catalog['functions'] == runtime['function_identities'] and catalog['authorities'] == installation['after_authorities']
            and current['binary_sha256'] == accepted['installed_binary_sha256'],
            'current function authority or installed binary drift')
    fixture_refs = [v for k, v in runtime.items() if k.endswith('_fixture_gate')]
    require(fixture_refs and len(fixture_refs) <= 64, 'accepted fixture provenance missing')
    for ref in fixture_refs: read(checked_ref(ref))
    maintenance_at = now()
    live_maintenance = observe.maintenance(policy)
    units = live_maintenance['units']; snapshot = live_maintenance['database']
    require(not units['discovery_failed'] and units['active_writer_count'] == units['unknown_runtime_count'] == 0
            and all(type(snapshot['activity'][k]) is int and snapshot['activity'][k] == 0 for k in
                    ('active_writer_count', 'unknown_application_count', 'active_index_build_count', 'active_vacuum_count', 'active_backfill_count'))
            and metadata['source_writers'] == metadata['active_conversions'] == 0,
            'current writers maintenance or conversion state failed')
    require(metadata['database_identity'] == snapshot['database_identity'], 'current database identity changed')
    backup_at = now()
    backup = observe.backup(metadata['database_identity'])
    backup_raw = read(config['backup_evidence']); closure = read(backup_raw['external_pack_closure'])
    backup_plan = read(backup_raw['plan'])
    require(backup['status'] == 'PASS' and backup['evidence_sha256'] == config['backup_evidence']['sha256']
            and backup['backup_schema_sha256'] == accepted['schema_sha256']
            and metadata['function_map_sha256'] == backup_raw['function_map_sha256']
            and metadata['roles_sha256'] == backup_plan['metadata']['roles_sha256']
            and str(closure['pack_root']) == config['pack_root']
            and [{k: r[k] for k in ('pack_id', 'storage_key', 'manifest_sha256', 'stored_bytes')} for r in closure['packs']] == metadata['packs'],
            'actual mandatory backup or retained sealed pack closure differs')
    code = observe.code()
    require(code == {'operator_commit_sha': config['operator_commit_sha'], 'tracked_clean': True},
            'fresh preflight operator commit or tracked source drift')
    evaluated = observe.o.preflight.evaluate(snapshot, live_maintenance['client_version'], accepted['schema_sha256'],
        units, backup, policy, commit_sha=code['operator_commit_sha'], tracked_clean=code['tracked_clean'])
    require(evaluated['overall_status'] == 'PASS' and set(evaluated['checks'].values()) == {'PASS'},
            'actual repeated supported preflight checks failed')
    captured = observe.watermarks(audit['candidate_set'], accepted)
    legacy_at = now()
    legacy = observe.legacy(baseline)
    require(legacy['legacy'] == baseline, 'legacy vector outbox or default baseline changed')
    resource_at = now()
    resource = observe.resources(request, kernel, current); pool = resource['pool']
    require(pool['uuid'] == kernel['kernel_pool_uuid'] and decision['pool_uuid'] == kernel['pool_uuid']
            and pool['size_bytes'] == decision['pool_size_bytes'] == kernel['pool_size_bytes']
            and decision['absolute_pool_ceiling_bytes'] <= pool['size_bytes'] * decision['maximum_pool_percent'] // 100
            and pool['used_bytes'] + 128 * MIB <= decision['absolute_pool_ceiling_bytes']
            and pool['metadata_used'] * 100 < pool['metadata_total'] * 60
            and resource['physical_free_bytes'] >= 42 * GIB and 0 <= resource['generated_wal_bytes'] <= 96 * GIB
            and resource['generated_wal_bytes'] >= kernel['generated_wal_bytes']
            and resource['spool_allocated_bytes'] <= 32 * GIB and resource['wal_bytes'] <= 32 * GIB
            and resource['wal_ready_bytes'] <= 24 * GIB
            and resource['archive_command_sha256'] == config['archive_command_sha256'],
            'fresh original resource stock or cumulative recovery budget failed')
    observed = now()
    raw_ref = save('actual-refresh-observations', {'schema_version': SCHEMA, 'configuration': config_ref,
        'observed_at_unix': observed, 'package': current, 'maintenance': live_maintenance,
        'backup': backup, 'resources': resource, 'legacy': legacy, 'watermarks': config['quiesced_watermarks'],
        'live_preflight_revalidation': evaluated, 'sample_started_at_unix': {'package': package_at,
        'maintenance': maintenance_at, 'backup': backup_at, 'legacy': legacy_at, 'resources': resource_at}})
    final_package = observe.package()
    require(final_package['catalog']['functions'] == catalog['functions']
            and final_package['catalog']['authorities'] == catalog['authorities']
            and final_package['binary_sha256'] == current['binary_sha256']
            and all(final_package['metadata'][key] == metadata[key] for key in
                    ('database_identity', 'function_map_sha256', 'roles_sha256', 'packs'))
            and final_package['metadata']['source_writers'] == final_package['metadata']['active_conversions'] == 0,
            'reader role sealed-pack or writer identity changed during refresh')
    after = observe.inventory(expected_reader)
    stable_sources = lambda value: [{k: v for k, v in row.items() if k != 'source_ref'} for row in value['inventory']['sources']]
    require(stable_sources(before) == stable_sources(after)
            and not any(g['status'] == 'building' for row in after['inventory']['sources'] for g in row['generations'])
            and after['audit']['candidate_set'] == before['audit']['candidate_set'] == audit['candidate_set']
            and after['audit']['candidate_set_sha256'] == audit['candidate_set_sha256'],
            'persisted candidate evidence changed during refresh')
    code = observe.code()
    require(code == {'operator_commit_sha': config['operator_commit_sha'], 'tracked_clean': True},
            'fresh preflight operator commit or tracked source drift')
    observe.watermarks(audit['candidate_set'], accepted)
    atomic_budget(config, request, decision, wall=now(), mono=mono(), boot=observe.boot)
    sample_times = {'live_adapter_watermarks': captured['captured_at_unix'],
        'writer_and_maintenance_inventory': min(package_at, maintenance_at),
        'current_package_and_schema': min(package_at, preflight['generated_at_unix']),
        'cumulative_resource_and_recovery_budget': min(resource_at, backup_at),
        'benchmark_result': package_at, 'legacy_state_readback': legacy_at}
    require(all(0 <= now() - at <= 300 for at in sample_times.values()), 'actual live gate samples exceeded300s')
    gates = {name: copy.deepcopy(accepted['external_gates'][name]) for name in STATIC}
    for name in sorted(VOLATILE):
        proof = {'gate': name, 'status': 'PASS', 'candidate_set_sha256': audit['candidate_set_sha256'],
            'reader_installation_sha256': config['reader_receipt']['sha256'], 'observed_at_unix': sample_times[name],
            'raw_observation_references': [raw_ref], 'previous_gate': accepted['external_gates'][name]['evidence_reference'],
            'performed_by': reference(__file__)}
        if name == 'live_adapter_watermarks':
            proof['observed_at_unix'] = captured['captured_at_unix']
        if name == 'benchmark_result':
            proof.update(check_kind='CURRENT_PACKAGE_SCHEMA_FUNCTION_AUTHORITY_AND_FIXTURE_COMPATIBILITY_ONLY',
                historical_measurements_reused=True, new_benchmark_measurements=0,
                compatibility_observed_at_unix=sample_times[name],
                historical_measurement_evidence=accepted['external_gates'][name]['evidence_reference'],
                original_benchmark_evidence_unchanged=True, fixture_references=fixture_refs,
                measurement_timestamps_not_rewritten=True)
        proof_ref = save(name + '-proof', proof)
        gates[name] = {'status': 'PASS', 'evidence_reference': proof_ref, 'evidence_sha256': proof_ref['sha256'],
                       'observed_at_unix': int(proof['observed_at_unix'])}
    result = copy.deepcopy(accepted)
    result.update(external_gates=gates, current_source_watermarks=copy.deepcopy(captured['watermarks']),
        schema_observation=config['preflight'], schema_observation_overall_status='PASS', activation_preflight_pass_claimed=True,
        observed_at_unix=now(),
        preflight_operator_commit_sha=config['operator_commit_sha'],
        refresh_provenance={'method': reference(__file__), 'configuration': config_ref, 'previous_acceptance': config['accepted'],
            'previous_preflight_operator_commit_sha': accepted['preflight_operator_commit_sha'],
            'previous_export_observed_at_unix': accepted.get('observed_at_unix'),
            'preflight': config['preflight'], 'backup': config['backup_evidence'], 'quiesced_watermarks': config['quiesced_watermarks'],
            'atomic_capacity_request': config['atomic_capacity_request'], 'capacity_decision': config['capacity_decision'],
            'observed_at_unix': observed, 'new_qualification_posts': 0, 'new_full_source_verifications': 0,
            'new_benchmark_measurements': 0, 'schema_or_binary_changed': False, 'counters_reset': False, 'baseline_rebased': False})
    # Run the unmodified complete public contract. No stale gate or row receives
    # acceptance merely because a fresh capture/preflight now exists.
    op.validate_acceptance(audit, config['audit']['sha256'], result, digest(result),
                          Path(config['installed_binary']), preflight, int(now()))
    observe.check()
    return save('aggregate-acceptance', result)


def verify_refresh_lineage(current, *, original_reference, audit_reference, reader_reference,
                           request_reference, decision_reference, method_reference, read):
    """Bind new live evidence to an existing unchanged atomic approval.

    No decision, timestamp, limit or subject hash is modified. The caller still
    runs the original atomic and full live activation validators afterwards.
    """
    previous = read(checked_ref(original_reference))
    provenance = current['refresh_provenance']
    require(provenance['previous_acceptance'] == original_reference
            and provenance['method'] == method_reference
            and provenance['atomic_capacity_request'] == request_reference
            and provenance['capacity_decision'] == decision_reference
            and provenance['counters_reset'] is provenance['baseline_rebased'] is False
            and provenance['new_qualification_posts'] == provenance['new_full_source_verifications'] == 0,
            'same original acceptance and atomic receipt ancestry required')
    configuration_ref = checked_ref(provenance['configuration']);config = read(configuration_ref)
    review = read(checked_ref(config['root_review']))
    require(config['accepted'] == original_reference and config['audit'] == audit_reference
            and config['reader_receipt'] == reader_reference
            and config['atomic_capacity_request'] == request_reference and config['capacity_decision'] == decision_reference
            and review['status'] == 'ROOT_APPROVED_SAME_SET_ACTIVATION_EVIDENCE_REFRESH'
            and review['method'] == method_reference and review['approved'] is True and review['root_reviewer_uid'] == 0
            and review['independent_review_claimed'] is False
            and review['payload_sha256'] == digest({k: v for k, v in config.items() if k != 'root_review'}),
            'exact reviewed refresh producer and parent configuration required')
    unchanged = ('schema_version','status','persisted_audit_sha256','candidate_set_sha256','source_count',
                 'schema_sha256','backend_package_sha256','installed_binary_sha256','code_commit_sha')
    require(all(current[key] == previous[key] for key in unchanged)
            and current['persisted_audit_sha256'] == audit_reference['sha256']
            and current['preflight_operator_commit_sha'] == config['operator_commit_sha']
            and provenance['previous_preflight_operator_commit_sha'] == previous['preflight_operator_commit_sha']
            and set(current['external_gates']) == GATES
            and all(current['external_gates'][name] == previous['external_gates'][name] for name in STATIC),
            'refresh changed an accepted source package schema or quality identity')
    for name in VOLATILE:
        gate = current['external_gates'][name];proof = read(checked_ref(gate['evidence_reference']))
        require(gate['status'] == proof['status'] == 'PASS' and proof['gate'] == name
                and gate['evidence_sha256'] == gate['evidence_reference']['sha256']
                and proof['performed_by'] == method_reference
                and proof['reader_installation_sha256'] == reader_reference['sha256']
                and proof['candidate_set_sha256'] == current['candidate_set_sha256']
                and proof['previous_gate'] == previous['external_gates'][name]['evidence_reference']
                and proof['raw_observation_references'], 'actual refresh gate producer ancestry differs')
        for origin in proof['raw_observation_references']:
            raw = read(checked_ref(origin))
            require(raw['schema_version'] == SCHEMA and raw['configuration'] == configuration_ref,
                    'actual refresh observation belongs to another reviewed configuration')
    return current


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--config-sha256', required=True)
    parser.add_argument('--output-directory', type=Path, required=True)
    args = parser.parse_args()
    require(os.getuid() == os.geteuid() == 0, 'Root owns the approved refresh operation')
    config_ref = {'path': str(args.config), 'sha256': args.config_sha256}
    require(args.config.lstat().st_uid == 0, 'Root-owned refresh configuration required')
    config = read_reference(config_ref)
    for key in ('root_review', 'atomic_capacity_request', 'capacity_decision'):
        require(Path(checked_ref(config[key])['path']).lstat().st_uid == 0, 'Root-owned refresh authority required')
    decision = read_reference(config['capacity_decision'])
    output = args.output_directory
    require(output.is_absolute() and output.resolve() == output and not os.path.lexists(output), 'new private output directory required')
    output.mkdir(mode=0o700)
    written = 0
    def save(label, value):
        nonlocal written
        require(re.fullmatch(r'[a-z][a-z0-9-]+', label), 'fixed owned metadata label required')
        path = output / (label + '.json');temporary = output / (label + '.pending')
        require(not os.path.lexists(path) and not os.path.lexists(temporary), 'existing refresh output requires reconciliation')
        raw = canonical(value) + b'\n'
        emergency = label == 'stopped'
        require(len(raw) <= (MIB if emergency else MAX_METADATA) and written + len(raw) <= (32 if emergency else 31) * MIB,
                'finite owned metadata output bound exceeded')
        require(shutil.disk_usage(output).free >= len(raw) + MIB, 'metadata write reserve unavailable')
        def deadline():
            require(emergency or time.time() < decision['valid_until_unix'] and time.monotonic() < decision['valid_until_monotonic'],
                    'no refresh publication after original atomic deadline')
        deadline()
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw);stream.flush();os.fsync(stream.fileno())
            deadline()
            os.link(temporary, path)
            written += len(raw)
        finally:
            temporary.unlink()
        directory = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:os.fsync(directory)
        finally:os.close(directory)
        return reference(path)
    try:
        ref = refresh(config_ref, config, read=read_reference,
                      observe=LiveObservations(config, operators(), decision), save=save)
    except Exception as error:
        save('stopped', {'status': 'STOPPED_NO_REFRESH_ACCEPTANCE', 'error_type': type(error).__name__,
            'configuration': config_ref, 'automatic_retry': False})
        raise
    print(json.dumps({'status': 'PASS_SAME_SET_METADATA_REFRESH_NOT_ACTIVATION', 'acceptance': ref}))


if __name__ == '__main__':
    main()
