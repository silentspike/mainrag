#!/usr/bin/python3
"""Own one public synthetic 33-window complete-file fixture until Root stops it.

No private corpus or production URL is accepted. Setup reuses the existing
native legacy fixture schema; it does not replay its unrelated test methods.
"""
import argparse,hashlib,json,os,signal,threading
from pathlib import Path
from eval.storage_v2.schema.test_native_legacy_hit_resolution import NativeLegacyHitResolutionTests

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--read-boundary',type=Path,required=True);p.add_argument('--read-boundary-sha256',required=True);a=p.parse_args()
 code=a.read_boundary.read_bytes()
 if hashlib.sha256(code).hexdigest()!=a.read_boundary_sha256:raise RuntimeError('EXACT_NEW_BOUNDARY_REQUIRED')
 stopped=threading.Event()
 for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:stopped.set())
 fixture=NativeLegacyHitResolutionTests;fixture.setUpClass()
 try:
  if not os.environ.get('STORAGE_V2_TEST_SOCKET'):fixture.sql('CREATE ROLE fixture LOGIN SUPERUSER')
  fixture.sql(code.decode());case=fixture();run=case.begin(3,'71'*32,'72'*32);offset=0
  for index in range(33):
   text=f'part {index:02} α🙂 '+('x'*index)+'\n';node,view,digest=case.make_projection(text);length=len(text.encode())
   locator=json.dumps(dict(byte_start=offset,byte_end=offset+length,fragmented=True))
   statement=(f"SELECT storage_v2_stage_shadow_item({run},'complete-part-{index}',"
      f"'document','synthetic-item','{{\"fixture\":true}}'::JSONB,'fixture-adapter-v1',"
      f"{node},NULL,'{digest}',{length},decode('{digest}','hex'),'fixture-analysis-v1',"
      f"{view},'/synthetic/full-page.txt',{case.quote(locator)}::JSONB)")
   case.sql(case.admin(statement));case.complete_analysis(digest);offset+=length
  case.commit(run,33)
  generation=int(case.sql(f'SELECT generation_id FROM storage_v2_ingest_run WHERE id={run}'))
  # Existing disposable-reader fixture setup: install a genuine pointer/state
  # pair without claiming release qualification or production activation. Only
  # the fixture owner touches its own newly created source3 generation here.
  case.sql(f"""BEGIN;
    ALTER TABLE source_generation DISABLE TRIGGER USER;
    ALTER TABLE logical_source DISABLE TRIGGER USER;
    UPDATE source_generation SET status='active',activated_at=now()
      WHERE id={generation} AND source_id=3 AND generation_seq=1 AND status='verified';
    UPDATE logical_source SET active_generation_id={generation}
      WHERE id=3 AND active_generation_id IS NULL;
    COMMIT;
    ALTER TABLE source_generation ENABLE TRIGGER USER;
    ALTER TABLE logical_source ENABLE TRIGGER USER;""")
  active=case.sql(f"""SELECT count(*) FROM logical_source source
    JOIN source_generation generation ON generation.id=source.active_generation_id
      AND generation.source_id=source.id
    WHERE source.id=3 AND generation.id={generation} AND generation.generation_seq=1
      AND generation.status='active' AND generation.verified_at IS NOT NULL""")
  if active!='1':raise RuntimeError('DISPOSABLE_NATIVE_ACTIVE_POINTER_SETUP_FAILED')
  metadata=dict(database=fixture.database,socket=str(fixture.socket),source_id=3,
    native_fragments=33,logical_bytes=offset,read_boundary_sha256=a.read_boundary_sha256,
    active_generation_id=generation,active_generation_seq=1,activation_acceptance_claimed=False,
    production_data_imported=False)
  with a.output.open('x') as f:os.fchmod(f.fileno(),0o600);json.dump(metadata,f,sort_keys=True);f.flush();os.fsync(f.fileno())
  print('READY:33 verified public native windows; no tests executed',flush=True)
  stopped.wait(1800)
 finally:
  fixture.tearDownClass();a.output.unlink(missing_ok=True)
if __name__=='__main__':main()
