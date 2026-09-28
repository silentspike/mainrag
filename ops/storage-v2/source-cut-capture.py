#!/usr/bin/python3 -I
"""Publish one root-bound, read-only Btrfs cut through a root-owned policy.

Install a reviewed copy as the fixed privileged helper. The command accepts only
an opaque registered-root digest; callers cannot choose a path, policy or command.
Cuts and immutable descriptors are retained until their owned manifest cleanup.
"""
from __future__ import annotations
import argparse
import fcntl
import grp
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import uuid

POLICY=Path('/etc/mainrag/source-cut-policy.json')
FORMAT='mainrag.fs-read-cut.v1'
SHA256=re.compile(r'[0-9a-f]{64}\Z')
MAX_BYTES=16384

def canonical(value):
    return (json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False)+'\n').encode()

def trusted_directory(path: Path,owner=0):
    if not path.is_absolute() or path.resolve(strict=True)!=path:
        raise RuntimeError('cut directory must be canonical')
    for ancestor in [path,*path.parents]:
        meta=ancestor.lstat()
        sticky=meta.st_uid==0 and bool(meta.st_mode&stat.S_ISVTX)
        if (not stat.S_ISDIR(meta.st_mode) or meta.st_uid not in {0,owner}
                or meta.st_mode&0o022 and not sticky):
            raise RuntimeError('cut directory authority is unsafe')

def private_read(path: Path,owner=0,with_bytes=False):
    trusted_directory(path.parent,owner)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        before=os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid!=owner
                or before.st_mode&0o027 or before.st_nlink!=1 or before.st_size>MAX_BYTES):
            raise RuntimeError('cut policy or descriptor authority is unsafe')
        raw=os.read(fd,MAX_BYTES+1)
        after=os.fstat(fd)
        key=lambda m:(m.st_dev,m.st_ino,m.st_size,m.st_mtime_ns,m.st_ctime_ns)
        if len(raw)!=before.st_size or key(before)!=key(after):
            raise RuntimeError('cut control file changed during read')
    finally:
        os.close(fd)
    def unique(pairs):
        value={}
        for key,item in pairs:
            if key in value:raise RuntimeError('duplicate cut control key')
            value[key]=item
        return value
    value=json.loads(raw,object_pairs_hook=unique)
    return (value,raw) if with_bytes else value

def inspector():
    for name in ['/usr/sbin/btrfs','/usr/bin/btrfs']:
        path=Path(name)
        if path.exists():
            path=path.resolve(strict=True);meta=path.stat()
            if not stat.S_ISREG(meta.st_mode) or meta.st_uid or meta.st_mode&0o022:
                raise RuntimeError('Btrfs inspector authority is unsafe')
            return path
    raise RuntimeError('Btrfs inspector unavailable')

def command(binary: Path,*arguments):
    result=subprocess.run([str(binary),*map(str,arguments)],capture_output=True,check=True,timeout=120)
    if len(result.stdout)>MAX_BYTES:raise RuntimeError('filesystem inspection exceeds its bound')
    return result.stdout.decode()

def identity(binary: Path,root: Path):
    output=command(binary,'subvolume','show',root)
    def field(name):
        values=[line.strip()[len(name):].strip() for line in output.splitlines() if line.strip().startswith(name)]
        if len(values)!=1:raise RuntimeError('filesystem identity is ambiguous')
        return '-' if values[0]=='-' else str(uuid.UUID(values[0]))
    return {'uuid':field('UUID:'),'parent_uuid':field('Parent UUID:')}

def validate_policy(policy: object,digest: str):
    if (not isinstance(policy,dict) or set(policy)!={'format','registry_root','reader_group','sources'}
            or policy['format']!='mainrag.fs-cut-policy.v1' or not isinstance(policy['sources'],dict)
            or not SHA256.fullmatch(digest) or digest not in policy['sources']):
        raise RuntimeError('root is not authorized by the cut policy')
    selected=policy['sources'][digest]
    if not isinstance(selected,dict) or set(selected)!={'registered_root','origin_subvolume','origin_uuid'}:
        raise RuntimeError('source-cut policy identity is incomplete')
    root=Path(selected['registered_root']);origin=Path(selected['origin_subvolume']);registry=Path(policy['registry_root'])
    if (not root.is_absolute() or root.resolve(strict=True)!=root or not root.is_dir()
            or not origin.is_absolute() or origin.resolve(strict=True)!=origin
            or not root.is_relative_to(origin) or root==origin
            or hashlib.sha256(str(root).encode()).hexdigest()!=digest
            or not registry.is_absolute() or registry.is_relative_to(origin)):
        raise RuntimeError('cut policy source boundary differs')
    expected=str(uuid.UUID(selected['origin_uuid']))
    origin_meta=origin.stat()
    if origin_meta.st_uid!=0 or origin_meta.st_mode&0o022:
        raise RuntimeError('snapshot origin authority is unsafe')
    if uuid.UUID(expected).int==0:raise RuntimeError('cut origin identity is missing')
    return root,origin,registry,expected

def write_new(path: Path,raw: bytes,gid: int):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o640)
    try:
        os.fchown(fd,0,gid);os.fchmod(fd,0o640)
        with os.fdopen(fd,'wb',closefd=False) as output:
            output.write(raw);output.flush();os.fsync(fd)
    finally:os.close(fd)

def no_nested_subvolumes(root: Path):
    # A nonrecursive snapshot represents a nested subvolume as an empty inode-2
    # stub. Reject it instead of claiming complete registered-source discovery.
    for parent,dirs,_ in os.walk(root,followlinks=False):
        for name in dirs:
            path=Path(parent)/name
            meta=path.lstat()
            if stat.S_ISDIR(meta.st_mode) and meta.st_ino in {2,256}:
                raise RuntimeError('nested source subvolume is not covered by this cut')

def capture(digest: str,policy_path=POLICY):
    if os.geteuid()!=0:raise RuntimeError('source-cut producer requires its privileged identity')
    policy=private_read(policy_path)
    root,origin,registry,expected=validate_policy(policy,digest)
    gid=grp.getgrnam(policy['reader_group']).gr_gid
    trusted_directory(registry)
    marker=private_read(registry/'registry-owner.json')
    if (set(marker)!={'format','owner','registry_root','nonce'}
            or marker['format']!='mainrag.fs-cut-registry.v1'
            or marker['owner']!='storage-v2-source-cut' or marker['registry_root']!=str(registry)
            or uuid.UUID(marker['nonce']).int==0):
        raise RuntimeError('cut registry ownership is not established')
    for child in ['views','history']:
        path=registry/child
        path.mkdir(mode=0o750,exist_ok=True)
        trusted_directory(path)
        os.chown(path,0,gid);os.chmod(path,0o750)
    if os.statvfs(registry).f_bavail*os.statvfs(registry).f_frsize<20*1024**3:
        raise RuntimeError('source-cut filesystem reserve is insufficient')
    lock=os.open(registry/'capture.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        meta=os.fstat(lock)
        if meta.st_uid!=0 or not stat.S_ISREG(meta.st_mode) or meta.st_nlink!=1 or meta.st_mode&0o077:
            raise RuntimeError('cut producer lock authority differs')
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        binary=inspector()
        if identity(binary,origin)['uuid']!=expected:raise RuntimeError('registered cut origin changed')
        cut_id=str(uuid.uuid4());snapshot=registry/'views'/cut_id
        if snapshot.exists():raise RuntimeError('cut identity already exists')
        # Every created cut is owned and retained, including failed captures.
        intent={'cut_id':cut_id,'source_root_sha256':digest,'status':'CAPTURE_PENDING',
                'owner':'storage-v2-source-cut','cleanup':'manifest-bound after durable body proof'}
        intent_path=registry/'history'/f'{cut_id}-{digest}-intent.json'
        write_new(intent_path,canonical(intent),gid)
        command(binary,'subvolume','snapshot','-r',origin,snapshot)
        view=snapshot/root.relative_to(origin)
        if view.resolve(strict=True)!=view or not view.is_dir():raise RuntimeError('cut source boundary is redirected')
        no_nested_subvolumes(view)
        observed=identity(binary,snapshot)
        if observed['parent_uuid']!=expected or command(binary,'property','get','-ts',snapshot,'ro')!='ro=true\n':
            raise RuntimeError('cut lost its immutable origin identity')
        value={'format':FORMAT,'cut_id':cut_id,'source_root_sha256':digest,'registered_root':str(root),
               'origin_subvolume':str(origin),'snapshot_root':str(snapshot),'read_root':str(view),
               'snapshot_uuid':observed['uuid'],'origin_uuid':expected,'captured_at_unix':int(time.time())}
        raw=canonical(value)
        if len(raw)>MAX_BYTES:raise RuntimeError('cut descriptor exceeds its bound')
        history=registry/'history'/f'{cut_id}-{digest}.json'
        write_new(history,raw,gid)
        temporary=registry/f'.current-{digest}-{cut_id}.tmp'
        write_new(temporary,raw,gid)
        os.replace(temporary,registry/f'current-{digest}.json')
        directory=os.open(registry,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
        write_new(registry/'history'/f'{cut_id}-{digest}-published.json',canonical({
            **intent,'status':'PUBLISHED','descriptor_sha256':hashlib.sha256(raw).hexdigest(),
            'snapshot_uuid':observed['uuid'],'captured_at_unix':value['captured_at_unix']}),gid)
        return {'status':'PASS','cut_id':cut_id,'source_root_sha256':digest,
                'descriptor_sha256':hashlib.sha256(raw).hexdigest(),'snapshot_uuid':observed['uuid'],
                'origin_uuid':expected,'captured_at_unix':value['captured_at_unix']}
    finally:os.close(lock)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root-sha256',required=True)
    args=parser.parse_args()
    if not SHA256.fullmatch(args.source_root_sha256):parser.error('invalid registered-root digest')
    try:result=capture(args.source_root_sha256)
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError,KeyError) as error:
        # Keep private filesystem paths and command stderr out of API output.
        print(json.dumps({'status':'FAIL','error_type':type(error).__name__}),flush=True)
        return 1
    print(json.dumps(result,sort_keys=True),flush=True)
    return 0

if __name__=='__main__':raise SystemExit(main())
