#!/usr/bin/env python3
"""One bounded local pg_dump backup with retained external immutable packs.

No scheduler, archive transport, restore, or automatic retry. --plan reads only
metadata; --run is an explicit Root operation. This is not self-contained DR.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import time

SCHEMA = "mainrag.local-logical-backup.v1"
ROOT_UID = 0
MIB = 1024**2
MAX_METADATA = 16 * MIB
COMPLETION_RESERVE = 256 * 1024
PG = ["/usr/bin/sudo", "-n", "-u", "postgres", "--"]
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PGAPPNAME": "mainrag-local-logical-backup"}
CONNECTION = ["--host=/var/run/postgresql", "--port=5432", "--no-password"]
METADATA_SQL = """SELECT jsonb_build_object(
 'snapshot',pg_export_snapshot(),'server_version_num',current_setting('server_version_num')::INTEGER,
 'database_identity',jsonb_build_object('system_identifier',(SELECT system_identifier::TEXT FROM pg_control_system()),
  'database',current_database(),'database_oid',(SELECT oid FROM pg_database WHERE datname=current_database())),
 'database_bytes',pg_database_size(current_database()),
 'table_bytes',(SELECT coalesce(sum(pg_table_size(c.oid)),0) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
  WHERE c.relkind IN ('r','m') AND n.nspname NOT IN ('pg_catalog','information_schema')),
 'index_bytes',(SELECT coalesce(sum(pg_indexes_size(c.oid)),0) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
  WHERE c.relkind IN ('r','m') AND n.nspname NOT IN ('pg_catalog','information_schema')),
 'source_writers',(SELECT count(*) FROM pg_locks l JOIN logical_source s ON
  l.classid::BIGINT=((hashtextextended('mainrag.storage-v2-ingest-source:'||s.id::TEXT,0)>>32)&4294967295)
  AND l.objid::BIGINT=(hashtextextended('mainrag.storage-v2-ingest-source:'||s.id::TEXT,0)&4294967295)
  WHERE l.locktype='advisory' AND l.granted AND l.objsubid=1),
 'active_conversions',(SELECT count(*) FROM storage_v2_lexical_conversion_intent)+
  (SELECT count(*) FROM storage_v2_retained_posting_conversion_intent)+(SELECT count(*) FROM storage_v2_posting_conversion_intent),
 'function_map_sha256',(SELECT encode(sha256(convert_to(coalesce(jsonb_object_agg(p.oid::REGPROCEDURE::TEXT,
  encode(sha256(convert_to(pg_get_functiondef(p.oid),'UTF8')),'hex')),'{}'::JSONB)::TEXT,'UTF8')),'hex')
  FROM pg_proc p WHERE p.pronamespace='public'::REGNAMESPACE AND p.prokind='f'),
 'roles_sha256',encode(sha256(convert_to(jsonb_build_array(
  (SELECT jsonb_agg(to_jsonb(r) ORDER BY oid) FROM pg_roles r),
  (SELECT jsonb_agg(to_jsonb(m) ORDER BY roleid,member,grantor) FROM pg_auth_members m))::TEXT,'UTF8')),'hex'),
 'packs',(SELECT coalesce(jsonb_agg(jsonb_build_object('pack_id',p.id,'storage_key',p.storage_key,
  'manifest_sha256',encode(p.manifest_sha256,'hex'),'stored_bytes',p.stored_bytes) ORDER BY p.id),'[]'::JSONB)
  FROM content_pack p WHERE EXISTS(SELECT 1 FROM content_body b WHERE b.pack_id=p.id)));
"""


def require(value, reason):
    if not value:
        raise RuntimeError(reason)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def normalized_schema(raw):
    lines = []
    for line in raw.splitlines(keepends=True):
        ending = b"\r\n" if line.endswith(b"\r\n") else (b"\n" if line.endswith(b"\n") else b"")
        content = line[:-len(ending)] if ending else line
        if content.startswith(b"\\restrict "):
            content = b"\\restrict <TOKEN>"
        elif content.startswith(b"\\unrestrict "):
            content = b"\\unrestrict <TOKEN>"
        lines.append(content + ending)
    return b"".join(lines)


def unique_keys(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, "duplicate local backup metadata key")
        value[key] = item
    return value


def identity(info):
    return dict(device=info.st_dev, inode=info.st_ino, bytes=info.st_size, uid=info.st_uid,
                mode=stat.S_IMODE(info.st_mode), mtime_ns=info.st_mtime_ns, ctime_ns=info.st_ctime_ns)


def private_bytes(path, maximum=MAX_METADATA):
    path = Path(path)
    require(path.is_absolute() and path.resolve() == path, "canonical local private path required")
    parent = path.parent.stat()
    require(parent.st_uid == ROOT_UID and stat.S_IMODE(parent.st_mode) == 0o700,
            "local backup directory must be Root700")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_uid == ROOT_UID
                and stat.S_IMODE(before.st_mode) == 0o600 and 0 < before.st_size <= maximum,
                "bounded single-link Root600 backup metadata required")
        raw = stream.read(maximum + 1)
        require(identity(before) == identity(os.fstat(stream.fileno())) and len(raw) == before.st_size,
                "backup metadata changed during read")
    return raw


def read_json(path):
    value = json.loads(private_bytes(path), object_pairs_hook=unique_keys)
    require(isinstance(value, dict), "backup metadata object required")
    return value


def reference(path):
    return dict(path=str(path), sha256=hashlib.sha256(private_bytes(path)).hexdigest())


def read_reference(ref):
    require(set(ref) == {"path", "sha256"} and re.fullmatch("[0-9a-f]{64}", ref["sha256"]),
            "exact local backup reference required")
    raw = private_bytes(Path(ref["path"]))
    require(hashlib.sha256(raw).hexdigest() == ref["sha256"], "local backup reference digest differs")
    return json.loads(raw, object_pairs_hook=unique_keys)


def create_json(path, value):
    raw = canonical(value) + b"\n"
    require(len(raw) <= MAX_METADATA, "backup metadata bound exceeded")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(Path(path).parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return reference(path)


def command(argv, timeout=15):
    value = subprocess.run(argv, env=ENV, capture_output=True, timeout=timeout, check=False)
    require(value.returncode == 0, "local backup metadata command failed")
    require(len(value.stdout) <= MAX_METADATA and len(value.stderr) <= MIB, "metadata response bound exceeded")
    return value.stdout


class Snapshot:
    """One exported read-only snapshot, held until pg_dump has really exited."""
    def __init__(self, database, seconds):
        self.process = subprocess.Popen(PG + ["/usr/bin/psql", *CONNECTION, "-X", "-qAt",
                                       "--set=ON_ERROR_STOP=1", "--dbname=" + database],
                                       env=ENV, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            self.process.stdin.write(("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;"
                                      "SET LOCAL statement_timeout='15s';"
                                      f"SET LOCAL idle_in_transaction_session_timeout='{seconds + 60}s';"
                                      + METADATA_SQL).encode())
            self.process.stdin.flush()
            selector = selectors.DefaultSelector()
            try:
                selector.register(self.process.stdout, selectors.EVENT_READ)
                require(selector.select(20), "snapshot metadata timed out")
                raw = b"";end = time.monotonic() + 20
                while not raw.endswith(b"\n"):
                    require(time.monotonic() < end and selector.select(max(0, end - time.monotonic())),
                            "snapshot metadata timed out")
                    chunk = os.read(self.process.stdout.fileno(), min(65536, MAX_METADATA + 1 - len(raw)))
                    require(chunk, "snapshot metadata backend exited")
                    raw += chunk
                    require(len(raw) <= MAX_METADATA, "snapshot metadata bound exceeded")
            finally:
                selector.close()
            require(raw.endswith(b"\n") and len(raw) <= MAX_METADATA, "incomplete snapshot metadata")
            self.value = json.loads(raw, object_pairs_hook=unique_keys)
            require(re.fullmatch(r"[0-9A-Fa-f]+-[0-9A-Fa-f]+-[0-9]+", self.value["snapshot"]),
                    "actual PostgreSQL exported snapshot required")
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        if self.process.stdin:
            self.process.stdin.close()
        if self.process.stdout:
            self.process.stdout.close()

    def __enter__(self):
        return self.value

    def __exit__(self, *_):
        self.close()


def pack_closure(metadata, pack_root):
    require(pack_root.is_absolute() and pack_root.resolve() == pack_root and pack_root.is_dir(),
            "canonical retained pack root required")
    rows = []
    for row in metadata["packs"]:
        require(re.fullmatch(r"[0-9a-f]{8}-[0-9a-f-]{27}\.pack", row["storage_key"])
                and re.fullmatch("[0-9a-f]{64}", row["manifest_sha256"] or "")
                and type(row["stored_bytes"]) is int and row["stored_bytes"] >= 0, "native pack identity missing")
        path = pack_root / row["storage_key"]
        info = path.lstat()
        require(path.resolve() == path and stat.S_ISREG(info.st_mode) and info.st_size >= row["stored_bytes"],
                "external pack missing or shorter than immutable metadata")
        rows.append(dict(row, path=str(path), stat_identity=identity(info)))
    require(len({r["pack_id"] for r in rows}) == len(rows), "duplicate pack closure identity")
    return dict(kind="mainrag.local-logical-backup.pack-closure.v1", pack_root=str(pack_root), packs=rows,
                pack_count=len(rows), pack_contents_copied=False, file_stat_identity_not_content_hash=True)


def validate_retention(decision, plan_ref, closure, now, deadline):
    require(decision["status"] == "ROOT_APPROVED_LOCAL_LOGICAL_BACKUP_RETENTION"
            and decision["approved"] is True and decision["root_reviewer_uid"] == ROOT_UID
            and decision["independent_review_claimed"] is False and decision["plan"] == plan_ref
            and decision["pack_closure_sha256"] == digest(closure)
            and decision["preserve_pack_bytes_and_paths"] is True
            and decision["exclude_pack_ids_from_gc_repack_unlink"] is True
            and decision["preserve_backup_artifacts"] is True
            and decision["credential_recovery_external"] is True
            and type(decision["retain_until_unix"]) is int and decision["retain_until_unix"] >= deadline
            and type(decision["at_unix"]) is int and 0 <= now - decision["at_unix"] <= 300,
            "exact Root external-pack and artifact retention decision required")
    integrity = read_reference(decision["pack_integrity_evidence"])
    require(integrity.get("kind") == "mainrag.local-logical-backup.pack-integrity.v1"
            and integrity.get("status") == "PASS_ALL_REFERENCED_PACKS_INTEGRITY"
            and integrity.get("pack_closure_sha256") == digest(closure),
            "actual pack integrity evidence must bind this exact closure")
    pack_ids = [row["pack_id"] for row in closure["packs"]]
    require(len(set(pack_ids)) == len(pack_ids) == closure["pack_count"]
            and ("pack_count" in integrity or "pack_ids" in integrity),
            "complete pack integrity scope required")
    if "pack_count" in integrity:
        require(type(integrity["pack_count"]) is int and integrity["pack_count"] == len(pack_ids),
                "complete pack integrity scope required")
    if "pack_ids" in integrity:
        require(isinstance(integrity["pack_ids"], list) and all(isinstance(item, str) for item in integrity["pack_ids"])
                and sorted(integrity["pack_ids"]) == sorted(pack_ids), "complete pack integrity scope required")


def pool_observation(name):
    require(re.fullmatch("[A-Za-z0-9_+-]+", name), "local thin-pool mapper name required")
    status_row = command(["/usr/sbin/dmsetup", "status", name]).decode().split()
    table_row = command(["/usr/sbin/dmsetup", "table", name]).decode().split()
    index = status_row.index("thin-pool")
    table_index = table_row.index("thin-pool")
    used_meta, total_meta = map(int, status_row[index + 2].split("/"))
    used, total = map(int, status_row[index + 3].split("/"))
    block = int(table_row[table_index + 3]) * 512
    uuid = command(["/usr/sbin/dmsetup", "info", "-c", "--noheadings", "-o", "uuid", name]).decode().strip()
    require(uuid and 0 < used <= total and 0 <= used_meta < total_meta, "actual thin-pool counters required")
    return dict(uuid=uuid, used_bytes=used * block, size_bytes=total * block,
                metadata_used=used_meta, metadata_total=total_meta)


def local_filesystem(path, thin_pool):
    value = json.loads(command(["/usr/bin/findmnt", "--json", "--output", "SOURCE,FSTYPE,TARGET", "--target", str(path)]))
    rows = value["filesystems"]
    require(len(rows) == 1 and rows[0]["fstype"] in ("ext4", "xfs", "btrfs")
            and rows[0]["source"].startswith("/dev/"), "direct local filesystem required, no network or overlay")
    source = rows[0]["source"].split("[", 1)[0]
    mapping = command(["/usr/sbin/dmsetup", "table", source]).decode().split()
    pool_device = command(["/usr/sbin/dmsetup", "info", "-c", "--noheadings", "--separator", ":",
                           "-o", "major,minor", thin_pool]).decode().strip()
    require(len(mapping) >= 5 and mapping[2] == "thin" and mapping[3] == pool_device,
            "backup filesystem must use the explicitly budgeted local thin pool")
    return dict(rows[0], device=path.stat().st_dev, thin_pool_device=pool_device)


def resource_guard(plan, directory, origin, written):
    current = pool_observation(plan["thin_pool"])
    free = os.statvfs(directory)
    available = free.f_bavail * free.f_frsize
    reserve = plan["reserve_bytes"]
    require(local_filesystem(directory, plan["thin_pool"]) == plan["filesystem"] and current["uuid"] == origin["uuid"]
            and current["size_bytes"] == origin["size_bytes"], "backup storage identity changed")
    # Observed global growth is conservative; later unrelated frees give no
    # credit against an already observed peak of this distinct backup window.
    plan["_observed_peak_pool_growth_bytes"] = max(plan.get("_observed_peak_pool_growth_bytes", 0),
                                                  current["used_bytes"] - origin["used_bytes"])
    require(written <= plan["maximum_output_bytes"] and available >= reserve
            and current["used_bytes"] + reserve <= current["size_bytes"] * 85 // 100
            and current["metadata_used"] * 100 < current["metadata_total"] * 60
            and plan["_observed_peak_pool_growth_bytes"] <= plan["maximum_pool_growth_bytes"],
            "local backup output or physical pool reserve exhausted")
    return current


def stream_dump(argv, path, plan, origin, deadline, written):
    """Bound stdout before each write. Only this operation's partial inode is removed."""
    require(not path.exists(), "existing backup attempt requires reconciliation")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    owned = identity(os.fstat(fd));process = None;complete = False;checksum = hashlib.sha256();count = 0
    try:
        with os.fdopen(fd, "wb") as output:
            process = subprocess.Popen(argv, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            checked = 0.0;checked_bytes = 0
            try:
                while True:
                    require(time.monotonic() < deadline, "local backup finite deadline exhausted")
                    if time.monotonic() - checked >= 2 or count - checked_bytes >= 8 * MIB:
                        resource_guard(plan, path.parent, origin, written + count)
                        checked = time.monotonic();checked_bytes = count
                    if not selector.select(min(1, max(0, deadline - time.monotonic()))):
                        continue
                    chunk = os.read(process.stdout.fileno(), MIB)
                    if not chunk:
                        break
                    require(written + count + len(chunk) + COMPLETION_RESERVE <= plan["maximum_output_bytes"],
                            "local backup output cap exhausted")
                    free = os.statvfs(path.parent)
                    require(free.f_bavail * free.f_frsize >= plan["reserve_bytes"] + len(chunk),
                            "local backup filesystem reserve exhausted")
                    output.write(chunk);checksum.update(chunk);count += len(chunk)
                require(process.wait(timeout=max(1, min(5, deadline - time.monotonic()))) == 0,
                        "local backup pg_dump exited unsuccessfully")
                require(count > 0, "empty backup artifact refused")
                output.flush();os.fsync(output.fileno())
                complete = True
            finally:
                selector.close()
        return dict(file=path.name, sha256=checksum.hexdigest(), bytes=count,
                    stat_identity=identity(path.stat()), exit_code=0)
    finally:
        if process:
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
            process.stdout.close()
        if not complete and path.exists():
            current = path.lstat()
            if current.st_dev == owned["device"] and current.st_ino == owned["inode"]:
                path.unlink()


def validate_evidence(path, maximum_age, now, expected_identity=None):
    receipt = read_json(path)
    require(receipt["schema_version"] == SCHEMA and receipt["provider"] == "pg_dump_custom_local"
            and receipt["status"] == "COMPLETED_LOCAL_LOGICAL_BACKUP"
            and receipt["restore_tested"] is False and receipt["self_contained"] is False
            and receipt["credential_recovery_external"] is True
            and receipt["snapshot_consistent"] is True and receipt["dump_exit_code"] == 0
            and receipt["globals_exit_code"] == 0 and receipt["source"] == "local_unix_socket"
            and receipt["archive_toc_readable"] is True
            and re.fullmatch("[0-9a-f]{64}", receipt["archive_toc_sha256"]),
            "unsupported or incomplete local logical backup")
    require(type(receipt["started_at_unix"]) is int and type(receipt["completed_at_unix"]) is int
            and receipt["started_at_unix"] <= receipt["completed_at_unix"] <= now
            and receipt["observed_at_unix"] == receipt["completed_at_unix"], "invalid local backup times")
    require(type(receipt["server_version_num"]) is int and receipt["server_version_num"] >= 180000
            and re.fullmatch("[0-9a-f]{64}", receipt["schema_sha256"]), "backup PostgreSQL/schema identity missing")
    if expected_identity is not None:
        require(receipt["database_identity"] == expected_identity, "local backup belongs to another database")
    root = Path(path).parent
    for key in ("archive", "globals", "schema"):
        item = receipt[key]
        require(Path(item["file"]).name == item["file"] and item["file"] not in (".", "..")
                and re.fullmatch("[0-9a-f]{64}", item["sha256"]) and item["exit_code"] == 0,
                "backup artifact digest/exit missing")
        target = root / item["file"]
        info = target.lstat()
        require(target.resolve() == target and stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                and info.st_uid == ROOT_UID and stat.S_IMODE(info.st_mode) == 0o600
                and identity(info) == item["stat_identity"] and 0 < item["bytes"] == info.st_size,
                "backup artifact changed since streaming SHA completion")
    with (root / receipt["archive"]["file"]).open("rb") as stream:
        require(stream.read(5) == b"PGDMP", "custom PostgreSQL archive header missing")
    closure = read_reference(receipt["external_pack_closure"])
    retention = read_reference(receipt["retention_decision"])
    plan = read_reference(receipt["plan"])
    require(plan["status"] == "PLANNED_METADATA_ONLY_NOT_A_BACKUP"
            and plan["metadata"]["database_identity"] == receipt["database_identity"]
            and plan["metadata"]["function_map_sha256"] == receipt["function_map_sha256"],
            "backup differs from exact planned database/schema")
    validate_retention(retention, receipt["plan"], closure, receipt["started_at_unix"], receipt["completed_at_unix"])
    schema_raw = private_bytes(root / receipt["schema"]["file"])
    require(hashlib.sha256(schema_raw).hexdigest() == receipt["schema"]["sha256"]
            and hashlib.sha256(normalized_schema(schema_raw)).hexdigest() == receipt["schema_sha256"],
            "actual backup schema binding differs")
    require(retention["pack_closure_sha256"] == digest(closure) and retention["retain_until_unix"] >= now
            and retention["preserve_pack_bytes_and_paths"] is True
            and retention["exclude_pack_ids_from_gc_repack_unlink"] is True
            and retention["preserve_backup_artifacts"] is True
            and retention["root_reviewer_uid"] == ROOT_UID and retention["approved"] is True,
            "backup external retention missing or expired")
    for pack in closure["packs"]:
        target = Path(pack["path"])
        require(target.resolve() == target and target.parent == Path(closure["pack_root"])
                and target.name == pack["storage_key"] and stat.S_ISREG(target.lstat().st_mode)
                and identity(target.lstat()) == pack["stat_identity"], "retained backup pack changed or missing")
    return dict(status="PASS" if now - receipt["completed_at_unix"] <= maximum_age else "BLOCKED",
                evidence_level="local-logical-dump-with-retained-external-packs", restore_tested=False,
                self_contained=False, credential_recovery_external=True,
                artifact_validation="UNCHANGED_STAT_BOUND_STREAMING_SHA_AT_COMPLETION",
                age_seconds=now - receipt["completed_at_unix"], observation_age_seconds=0,
                artifact_sha256=receipt["archive"]["sha256"], evidence_sha256=reference(path)["sha256"],
                database_identity_sha256=digest(receipt["database_identity"]), backup_schema_sha256=receipt["schema_sha256"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--plan", action="store_true");action.add_argument("--run", action="store_true")
    parser.add_argument("--database", default="mainrag")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--pack-root", type=Path)
    parser.add_argument("--thin-pool", default="vg_dev-thinpool")
    parser.add_argument("--maximum-output-bytes", type=int)
    parser.add_argument("--maximum-pool-growth-bytes", type=int)
    parser.add_argument("--reserve-bytes", type=int, default=512 * MIB)
    parser.add_argument("--maximum-seconds", type=int, default=3600)
    parser.add_argument("--plan-sha256");parser.add_argument("--retention-decision", type=Path)
    args = parser.parse_args()
    require(os.getuid() == os.geteuid() == ROOT_UID, "Root is sole local backup operator")
    root = args.output_dir
    require(root.is_absolute() and root.resolve() == root and args.database == "mainrag", "local mainrag backup directory required")
    if args.plan:
        require(args.pack_root and type(args.maximum_output_bytes) is int and args.maximum_output_bytes > 0
                and type(args.maximum_pool_growth_bytes) is int and args.maximum_pool_growth_bytes >= args.maximum_output_bytes
                and args.reserve_bytes >= 128 * MIB and 1 <= args.maximum_seconds <= 14400,
                "explicit finite output/pool/time policy required")
        root.mkdir(mode=0o700, exist_ok=False)
        with Snapshot(args.database, 30) as metadata:
            require(metadata["source_writers"] == metadata["active_conversions"] == 0,
                    "source/proof/conversion writers must be terminal before backup")
            closure = pack_closure(metadata, args.pack_root)
        client = command(PG + ["/usr/bin/pg_dump", "--version"]).decode()
        major = re.search(r"\b([0-9]+)\.[0-9]+", client)
        require(major and int(major[1]) == metadata["server_version_num"] // 10000,
                "matching local pg_dump/server major version required")
        pool = pool_observation(args.thin_pool);filesystem = local_filesystem(root, args.thin_pool)
        require(args.maximum_output_bytes + args.reserve_bytes <= os.statvfs(root).f_bavail * os.statvfs(root).f_frsize
                and pool["used_bytes"] + args.maximum_pool_growth_bytes + args.reserve_bytes <= pool["size_bytes"] * 85 // 100,
                "projected backup does not fit actual filesystem/thin-pool budget")
        value = dict(schema_version=SCHEMA, status="PLANNED_METADATA_ONLY_NOT_A_BACKUP", database=args.database,
                     metadata=metadata, pack_closure=closure, filesystem=filesystem, thin_pool=args.thin_pool,
                     maximum_output_bytes=args.maximum_output_bytes, maximum_pool_growth_bytes=args.maximum_pool_growth_bytes,
                     reserve_bytes=args.reserve_bytes, maximum_seconds=args.maximum_seconds,
                     client_version=client.strip(), expected_compressed_bytes=None,
                     compression_fit_guaranteed=False, planned_at_unix=int(time.time()))
        print(json.dumps(dict(status=value["status"], plan=create_json(root / "plan.json", value),
                              pack_closure_sha256=digest(closure), table_bytes=metadata["table_bytes"], index_bytes=metadata["index_bytes"])))
        return
    require(args.plan_sha256 and args.retention_decision, "actual plan and retention decision required")
    plan_ref = dict(path=str(root / "plan.json"), sha256=args.plan_sha256);plan = read_reference(plan_ref)
    require(plan["status"] == "PLANNED_METADATA_ONLY_NOT_A_BACKUP" and plan["database"] == args.database,
            "exact planned local backup required")
    require(not (root / "intent.json").exists(), "existing backup attempt must not be retried")
    decision_ref = reference(args.retention_decision);decision = read_reference(decision_ref)
    started = int(time.time());deadline = time.monotonic() + plan["maximum_seconds"]
    origin = pool_observation(plan["thin_pool"]);resource_guard(plan, root, origin, 0)
    require(origin["used_bytes"] + plan["maximum_pool_growth_bytes"] + plan["reserve_bytes"] <= origin["size_bytes"] * 85 // 100,
            "current projected backup pool admission failed")
    require(plan["maximum_output_bytes"] + plan["reserve_bytes"] <= os.statvfs(root).f_bavail * os.statvfs(root).f_frsize,
            "current projected backup filesystem admission failed")
    with Snapshot(args.database, plan["maximum_seconds"]) as metadata:
        client = command(PG + ["/usr/bin/pg_dump", "--version"]).decode().strip()
        require(client == plan["client_version"], "local pg_dump changed after plan")
        closure = pack_closure(metadata, Path(plan["pack_closure"]["pack_root"]))
        require(metadata["database_identity"] == plan["metadata"]["database_identity"]
                and metadata["function_map_sha256"] == plan["metadata"]["function_map_sha256"]
                and metadata["source_writers"] == metadata["active_conversions"] == 0
                and closure == plan["pack_closure"], "backup database/schema/pack closure drifted after plan")
        validate_retention(decision, plan_ref, closure, started, started + plan["maximum_seconds"])
        create_json(root / "intent.json", dict(plan=plan_ref, retention_decision=decision_ref,
                    started_at_unix=started, database_identity=metadata["database_identity"], snapshot=metadata["snapshot"], automatic_retry=False))
        closure_ref = create_json(root / "pack-closure.json", closure)
        written = sum(p.stat().st_size for p in root.iterdir() if p.is_file());artifacts = []
        try:
            globals_ = stream_dump(PG + ["/usr/bin/pg_dumpall", *CONNECTION, "--globals-only", "--no-role-passwords"],
                                   root / "globals.sql", plan, origin, deadline, written);written += globals_["bytes"];artifacts.append(globals_)
            schema = stream_dump(PG + ["/usr/bin/pg_dump", *CONNECTION, "--dbname=" + args.database,
                                 "--snapshot=" + metadata["snapshot"], "--schema-only", "--no-owner", "--no-privileges"],
                                 root / "schema.sql", plan, origin, deadline, written);written += schema["bytes"];artifacts.append(schema)
            archive = stream_dump(PG + ["/usr/bin/pg_dump", *CONNECTION, "--dbname=" + args.database,
                                  "--snapshot=" + metadata["snapshot"], "--format=custom", "--compress=zstd:3"],
                                  root / "database.dump", plan, origin, deadline, written)
            written += archive["bytes"];artifacts.append(archive)
            resource_guard(plan, root, origin, written + COMPLETION_RESERVE)
            toc = command(["/usr/bin/pg_restore", "--list", str(root / "database.dump")], timeout=30)
            with Snapshot(args.database, 30) as after:
                require(after["roles_sha256"] == metadata["roles_sha256"], "global roles changed during backup")
            finished = int(time.time())
            result = dict(schema_version=SCHEMA, provider="pg_dump_custom_local", status="COMPLETED_LOCAL_LOGICAL_BACKUP",
                          source="local_unix_socket", plan=plan_ref, database_identity=metadata["database_identity"],
                          server_version_num=metadata["server_version_num"], function_map_sha256=metadata["function_map_sha256"],
                          schema_sha256=hashlib.sha256(normalized_schema(private_bytes(root / "schema.sql"))).hexdigest(),
                          archive=archive, globals=globals_, schema=schema,
                          external_pack_closure=closure_ref, retention_decision=decision_ref,
                          snapshot_consistent=True, dump_exit_code=0, globals_exit_code=0,
                          started_at_unix=started, completed_at_unix=finished, observed_at_unix=finished,
                          restore_tested=False, self_contained=False, credential_recovery_external=True,
                          archive_toc_readable=True, archive_toc_sha256=hashlib.sha256(toc).hexdigest(),
                          external_pack_contents_copied=False, own_output_bytes=written,
                          completion_metadata_reserve_bytes=COMPLETION_RESERVE,
                          observed_peak_pool_growth_bytes=plan["_observed_peak_pool_growth_bytes"],
                          pool_origin=origin, pool_final=pool_observation(plan["thin_pool"]))
            output = create_json(root / "completion.json", result)
            validate_evidence(root / "completion.json", 129600, finished)
            print(json.dumps(dict(status=result["status"], evidence=output, own_output_bytes=written,
                                  restore_tested=False, self_contained=False)))
        except BaseException as error:
            # These are newly created artifacts of this unaccepted operation,
            # never an old backup or foreign file. Preserve failure metadata.
            for artifact in artifacts:
                target = root / artifact["file"]
                if target.exists() and identity(target.lstat()) == artifact["stat_identity"]:
                    target.unlink()
            create_json(root / "failure.json", dict(status="LOCAL_BACKUP_FAILED_NOT_ACCEPTANCE", error_type=type(error).__name__,
                        reason=str(error) if isinstance(error, RuntimeError) else "non-constant local backup error",
                        started_at_unix=started, ended_at_unix=int(time.time()), automatic_retry=False))
            raise


if __name__ == "__main__":
    main()
