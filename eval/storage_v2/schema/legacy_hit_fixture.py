#!/usr/bin/env python3
"""Own one disposable native producer fixture until terminated by its caller."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import threading

from eval.storage_v2.schema.test_native_legacy_hit_resolution import NativeLegacyHitResolutionTests


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    stopped = threading.Event()
    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, lambda *_: stopped.set())
    fixture = NativeLegacyHitResolutionTests
    fixture.setUpClass()
    try:
        if not os.environ.get('STORAGE_V2_TEST_SOCKET'):
            # This role exists only inside the helper-owned temporary cluster.
            fixture.sql('CREATE ROLE fixture LOGIN SUPERUSER;')
        case = fixture()
        run = case.begin(3, '31'*32, '32'*32)
        offset = 0
        for index, text in enumerate(('alpha Über', '🙂 beta')):
            node, view, digest = case.make_projection(text)
            length = len(text.encode())
            locator = json.dumps(dict(byte_start=offset, byte_end=offset+length, fragmented=True))
            statement = (
                f"SELECT storage_v2_stage_shadow_item({run},'producer-part-{index}',"
                f"'document','synthetic-item','{{\"fixture\":true}}'::JSONB,'fixture-adapter-v1',"
                f"{node},NULL,'{digest}',{length},decode('{digest}','hex'),'fixture-analysis-v1',"
                f"{view},'/synthetic/producer.txt',{case.quote(locator)}::JSONB)"
            )
            case.sql(case.admin(statement))
            case.complete_analysis(digest)
            offset += length
        case.commit(run, 2)
        metadata = dict(database=fixture.database, socket=str(fixture.socket),
                        source_id=3, native_fragments=2, logical_bytes=offset)
        temporary = args.output.with_suffix('.preparing')
        with temporary.open('x') as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(metadata, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, args.output)
        print('READY: two verified synthetic native fragments', flush=True)
        stopped.wait(1800)
    finally:
        fixture.tearDownClass()
        args.output.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
