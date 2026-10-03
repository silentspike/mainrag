#!/usr/bin/env python3
"""Plan and resume bounded old-hit bootstrap; never activation or cleanup authority."""
from __future__ import annotations
import argparse
from decimal import Decimal
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import time

HERE=Path(__file__).resolve().parent
SPEC=importlib.util.spec_from_file_location('legacy_hit_release_helpers',HERE/'release-candidate.py')
R=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(R)
R.THIN_POOL_MAX_METADATA_PERCENT=Decimal(60)

def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def private_parent(path):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    metadata=path.parent.stat()
    if metadata.st_uid!=os.getuid() or stat.S_IMODE(metadata.st_mode)&0o077:
        raise RuntimeError('operator output parent must be owned and private')

def inventory(args,token,source):
    files=[];cursor=0;epoch=None
    while True:
        page=R.request(args.api_url,token,'POST',f"/api/v1/admin/sources/{source['source_id']}/storage-v2-legacy-hit-inventory",
            dict(generation_id=source['generation_id'],after_file_id=cursor,include_test=args.include_test))
        if page.get('source_id')!=source['source_id'] or page.get('generation_id')!=source['generation_id']:
            raise RuntimeError('legacy inventory source identity differs')
        observed=page.get('legacy_epoch')
        if type(observed) is not int or observed<0 or epoch is not None and observed!=epoch:
            raise RuntimeError('legacy inventory epoch changed')
        epoch=observed
        rows=page.get('files')
        if not isinstance(rows,list) or len(rows)>128:
            raise RuntimeError('legacy inventory page is invalid')
        for row in rows:
            if type(row.get('file_id')) is not int or row['file_id']<=cursor \
                or any(type(row.get(key)) is not int or row[key]<0 for key in ('hit_count','completed_hits','legacy_revision')) \
                or row['completed_hits']>row['hit_count'] \
                or not isinstance(row.get('file_sha256'),str) or not R.re.fullmatch('[0-9a-f]{64}',row['file_sha256']):
                raise RuntimeError('legacy file inventory is invalid')
            cursor=row['file_id'];files.append(row)
        if not rows:break
    return dict(source_id=source['source_id'],generation_id=source['generation_id'],legacy_epoch=epoch,files=files)

def immutable_inventory(source):
    return dict(source_id=source['source_id'],generation_id=source['generation_id'],legacy_epoch=source['legacy_epoch'],
        files=[{key:file[key] for key in ('file_id','file_sha256','legacy_revision','hit_count')} for file in source['files']])

def capacity(args):
    if shutil.disk_usage('/').free<20*1024**3 or shutil.disk_usage(args.pack_root).free<42*1024**3+args.source_spool_budget_bytes:
        raise RuntimeError('local root or data reserve is exhausted')
    observed=R.thin_pool_capacity(args.pack_root,args.maximum_growth_bytes+42*1024**3,require_estimate=True)
    if observed is None:
        raise RuntimeError('explicit physical thin-pool admission is required for this operator')
    return observed

def wait_unknown_outcome(args,token,source,file):
    # Only a currently held backend lease proves ongoing producer work. A
    # checkpoint file and an old RUNNING label are never treated as a process.
    deadline=time.monotonic()+7200
    while True:
        state=R.request(args.api_url,token,'POST',f"/api/v1/admin/sources/{source['source_id']}/storage-v2-legacy-hit-progress",
            dict(generation_id=source['generation_id'],file_id=file['file_id'],include_test=args.include_test),timeout_seconds=60)
        if (state.get('source_id'),state.get('generation_id'),state.get('file_id'))!=(source['source_id'],source['generation_id'],file['file_id']) \
            or type(state.get('producer_running')) is not bool or type(state.get('completed_hits')) is not int:
            raise RuntimeError('producer liveness readback differs')
        if not state['producer_running']:return state
        if time.monotonic()>=deadline:
            raise RuntimeError('producer is still running; leave its checkpoint for later observation')
        time.sleep(15)

def apply(args,token,plan,package):
    if plan.get('schema_version')!='mainrag.storage-v2.legacy-hit-plan.v1' or plan.get('package')!=package \
        or plan.get('include_test')!=args.include_test or plan.get('source_spool_budget_bytes')!=args.source_spool_budget_bytes \
        or plan.get('maximum_growth_bytes')!=args.maximum_growth_bytes:
        raise RuntimeError('reviewed legacy plan package, scope or resource admission differs')
    state=dict(schema_version='mainrag.storage-v2.legacy-hit-run.v1',plan_sha256=args.apply,
        status='RUNNING',source_index=0,file_index=0,after_hit_id=0,batches=0)
    if args.state.exists():
        raw=args.state.read_bytes()
        prior=R.read_private_receipt(args.state,hashlib.sha256(raw).hexdigest())
        if prior.get('plan_sha256')!=args.apply or prior.get('schema_version')!=state['schema_version']:
            raise RuntimeError('checkpoint belongs to another legacy plan')
        if prior.get('status')=='FAILED_REQUEST':
            raise RuntimeError('previous request was definitively rejected; correct its cause before a new attempt')
        state=prior
        if state.get('status') in ('RUNNING','OUTCOME_UNKNOWN'):
            index=state['source_index'];file_index=state['file_index']
            if index<len(plan['sources']) and file_index<len(plan['sources'][index]['files']):
                wait_unknown_outcome(args,token,plan['sources'][index],plan['sources'][index]['files'][file_index])
    for index,source in enumerate(plan['sources']):
        if index<state['source_index']:continue
        current=inventory(args,token,source)
        if immutable_inventory(current)!=immutable_inventory(source):
            raise RuntimeError('legacy source inventory changed before resume')
        for file_index,file in enumerate(current['files']):
            if index==state['source_index'] and file_index<state['file_index']:continue
            state.update(source_index=index,file_index=file_index,status='RUNNING')
            if file['completed_hits']==file['hit_count']:
                state.update(file_index=file_index+1,after_hit_id=0)
                R.atomic_private_json(args.state,state);continue
            cursor=state['after_hit_id']
            while True:
                admission=capacity(args)
                state.update(after_hit_id=cursor,capacity=admission,observed_at_unix=time.time())
                R.atomic_private_json(args.state,state)
                body=dict(generation_id=source['generation_id'],file_id=file['file_id'],
                    expected_file_sha256=file['file_sha256'],expected_file_revision=file['legacy_revision'],
                    expected_legacy_epoch=source['legacy_epoch'],after_hit_id=cursor,
                    source_spool_budget_bytes=args.source_spool_budget_bytes,include_test=args.include_test)
                path=f"/api/v1/admin/sources/{source['source_id']}/storage-v2-legacy-hit-producer"
                try:result=R.request(args.api_url,token,'POST',path,body,timeout_seconds=7200)
                except Exception as error:
                    # A 4xx is a definite rejected request. Other failures can
                    # have committed: observe the live lease before one replay.
                    if isinstance(error,RuntimeError) and R.re.fullmatch(r'API request failed with HTTP 4\d\d',str(error)):
                        state.update(status='FAILED_REQUEST',failure_kind='HTTP4XX')
                        R.atomic_private_json(args.state,state)
                        raise
                    state.update(status='OUTCOME_UNKNOWN');R.atomic_private_json(args.state,state)
                    wait_unknown_outcome(args,token,source,file)
                    result=R.request(args.api_url,token,'POST',path,body,timeout_seconds=7200)
                if (result.get('source_id'),result.get('generation_id'),result.get('file_id'))!=(source['source_id'],source['generation_id'],file['file_id']) \
                    or type(result.get('after_hit_id')) is not int or type(result.get('file_done')) is not bool \
                    or result['after_hit_id']<cursor or result['after_hit_id']==cursor and not result['file_done']:
                    raise RuntimeError('producer committed cursor or identity differs')
                cursor=result['after_hit_id'];state.update(after_hit_id=cursor,batches=state['batches']+1,last_batch=result,status='RUNNING')
                R.atomic_private_json(args.state,state)
                if result['file_done']:break
            state.update(file_index=file_index+1,after_hit_id=0);R.atomic_private_json(args.state,state)
        accepted=inventory(args,token,source)
        if immutable_inventory(accepted)!=immutable_inventory(source) or any(file['hit_count']!=file['completed_hits'] for file in accepted['files']):
            raise RuntimeError('source all-hit completeness or inventory drift failed')
        state.update(source_index=index+1,file_index=0,after_hit_id=0);R.atomic_private_json(args.state,state)
    if R.observe_reader_package(args)!=package:
        raise RuntimeError('installed producer package changed')
    # Refresh every source: later mapping changes can invalidate an earlier
    # source receipt. This is a coverage gate, not cutover or deletion approval.
    final=[inventory(args,token,source) for source in plan['sources']]
    if any(immutable_inventory(current)!=immutable_inventory(expected)
        or any(file['hit_count']!=file['completed_hits'] for file in current['files'])
        for current,expected in zip(final,plan['sources'])):
        raise RuntimeError('final legacy coverage set changed or is incomplete')
    state.update(status='COMPLETE_FOR_PLAN_NOT_CLEANUP_AUTHORITY',completed_at_unix=time.time(),
        final_inventory_sha256=fingerprint(final),completed_hits=sum(file['hit_count'] for source in final for file in source['files']))
    R.atomic_private_json(args.state,state)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api-url',required=True);parser.add_argument('--token-file',type=Path,required=True)
    parser.add_argument('--reader-package-receipt',type=Path,required=True)
    parser.add_argument('--expected-reader-package-receipt-sha256',required=True)
    parser.add_argument('--pack-root',type=Path,required=True);parser.add_argument('--maximum-growth-bytes',type=int,required=True)
    parser.add_argument('--source-spool-budget-bytes',type=int,required=True)
    parser.add_argument('--include-test',action='store_true');parser.add_argument('--plan-file',type=Path,required=True)
    parser.add_argument('--candidate-set',type=Path);parser.add_argument('--candidate-set-sha256')
    parser.add_argument('--apply');parser.add_argument('--state',type=Path)
    args=parser.parse_args()
    if args.maximum_growth_bytes<=0 or not 0<args.source_spool_budget_bytes<=4*1024**3:
        raise RuntimeError('positive bounded reviewed resource estimates required')
    private_parent(args.plan_file)
    token=R.load_token(args.token_file,'MAINRAG_TOKEN');package=R.observe_reader_package(args)
    capacity(args)
    if args.apply:
        if args.state is None:raise RuntimeError('durable private state path required')
        private_parent(args.state)
        descriptor=os.open(str(args.state)+'.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        with os.fdopen(descriptor,'w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            plan=R.read_private_receipt(args.plan_file,args.apply)
            apply(args,token,plan,package)
    else:
        if args.candidate_set is None:raise RuntimeError('protected candidate set required')
        candidate=R.read_private_receipt(args.candidate_set,args.candidate_set_sha256)
        sources=candidate.get('sources')
        if not isinstance(sources,list) or not sources or len(sources)>4096 \
            or any(not isinstance(source,dict) or any(type(source.get(key)) is not int or source[key]<=0 for key in ('source_id','generation_id')) for source in sources) \
            or len({source['source_id'] for source in sources})!=len(sources):
            raise RuntimeError('bounded unique source candidate identities required')
        snapshots=[inventory(args,token,dict(source_id=source['source_id'],generation_id=source['generation_id'])) for source in sources]
        plan=dict(schema_version='mainrag.storage-v2.legacy-hit-plan.v1',package=package,
            include_test=args.include_test,source_spool_budget_bytes=args.source_spool_budget_bytes,
            maximum_growth_bytes=args.maximum_growth_bytes,sources=snapshots,candidate_set_sha256=args.candidate_set_sha256)
        R.atomic_private_json(args.plan_file,plan,replace=False)
        print(hashlib.sha256(args.plan_file.read_bytes()).hexdigest())
    return 0

if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as error:
        # Do not expose credentials, private origins, paths or HTTP bodies.
        print(json.dumps(dict(status='FAILED',error_type=type(error).__name__)))
        raise SystemExit(1)
