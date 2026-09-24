# Issue 55: supported current-path baseline evidence

This mapping covers the public, eager-filesystem, CPU-mode fixture baseline.
It does not qualify any private source generation or activate storage v2.

## Frozen evidence

- [Hosted CI run 36035909912](https://github.com/silentspike/mainrag/actions/runs/36035909912)
  passed every required job for candidate
  `466c562e56a9d205c943be6632aeeec38f776381`.
- The actual tested CI merge commit recorded by both fixture processes is
  `871dfec180618b0c92ffc0bb0992dd25430387a9`.
- [Committed report](baselines/supported-current-path.json), original SHA-256:
  `142c699de7b4e505aff8ea61e21ef2663977cee7480088110f985f7dec132d16`.
- Two independent fixture processes; each ingested twelve frozen documents,
  repeated their unchanged ingestion and evaluated all eleven queries with
  three warmups followed by thirty measured iterations each.
- Both runs have result identity
  `99b3e51ddc23b6f4ceae1ac3cc087a41f888b4959659ec07fa262148a063eab4`.

The report remains bound to that tested commit. The later commit storing this
evidence does not retroactively become its measured subject.

## Acceptance mapping

| Issue requirement | Authoritative implementation and observed evidence |
| --- | --- |
| One documented command | `run_supported_baseline.sh` runs the read-only inventory, both real fixture processes and schema-validated comparison; the hosted measurement step executes this command. |
| Exact identity and configuration | Report binds commit, corpus/query/template hashes, fixture-definition hash, observed schema-column hash, PostgreSQL version, pinned lexical asset, explicit profile, concurrency and timestamps. |
| Exact Top-10, Recall@10 and MRR | All eleven per-query records retain ordered public paths and both quality metrics; shared SQL uses score/path/id tie-breaking. All query outcomes are PASS in both runs. |
| Evaluated work versus shortlist | SQL counts matched chunks and scored channel rows before Top-10 selection; report retains these separately from returned paths. |
| Repeated latency distributions | Each run has 330 measured query roundtrips; per-query and aggregate p50/p95/p99 plus first-roundtrip timings are retained. No OS cold-cache claim. |
| Real ingestion work | Each initial run reports 2,576 logical bytes, 5,152 actual content-read bytes, twelve chunker calls, twelve intelligence-parser calls, twelve chunks and 1,826 compressed chunk bytes. |
| Unchanged repeat | Each repeat still reads 5,152 bytes but makes zero chunker/parser calls and creates zero chunks; the fixture verifies stored content and stable chunk IDs, not just row counts. |
| Zero/invalid runs fail closed | Tests reject absent queries, missing/nonfinite samples, partial reads, row-count aliases and zero-test output. The shell coordinator also rejects a failed command containing a partial success summary. |
| Honest outcome states | Schema preserves PASS/FAIL/BLOCKED/SKIP/NOT_RUN; failed quality/timing cannot pass, missing runs remain NOT_RUN, and unaccounted writers block acceptance. |
| Read-only writer gate | Both the pre-run gate and retained manifest cover 22 named, classified, content-hashed repository writers. No service is stopped or changed. Unknown external writers remain an explicit limitation. |
| Committed reproducible baseline | The public report is committed alongside the frozen inputs, schema, shared query template and runner; a regression validates the archived evidence. |

The harness reuses `eval_common` quality/percentile helpers and the existing
fixture/query set. Storage-v2 generation-specific comparison remains owned by
the supported shadow/candidate verifiers; this baseline does not replace them
or the broader golden-set scope of issue 34.

## Observed boundaries and retained failures

- The minimal schema exercises actual `IndexService::index_source`, adapter,
  default chunker, intelligence and persistence operations. It is not a
  production migration/RLS acceptance test.
- FTS runs over the actual ingested chunks using the shared query shape. It is
  not a full hybrid API, vector, reranker, streaming or other-adapter benchmark.
- The corpus fixture checks reconstruction, unchanged identities, both ledger
  writes, no vector connection and no outbox rows. It explicitly removes its
  owned source files; the surrounding test drops its owned schema. The hosted
  job also destroys the disposable database container.
- Source-content reads include binary probes but exclude metadata, walker
  configuration, lexical-asset reads, pack reads and OS/device I/O. Device I/O
  remains null. Compressed chunk bytes are not total PostgreSQL disk usage.
- [Run 36033379335](https://github.com/silentspike/mainrag/actions/runs/36033379335)
  remains failed: the initial report validator incorrectly expected a remote
  default metadata label in explicitly configured hosted CI. The correction
  preserves both profiles separately; it does not change tokenizer behavior.
- Preliminary remote-tunnel timing comparison failed the same 50% tolerance.
  Its outcome was recorded in issue 55. Remote roundtrip measurements are not
  compared with hosted local-PostgreSQL timings, and no threshold was relaxed.
- The old `current-path.json` SQL-only snapshot remains historical evidence;
  its row-count aliases are not reinterpreted as actual ingestion work.

This evidence permits review of issue 55's fixture baseline only. Issues 60,
66, 67 and 68 retain their append, all-source quality/resource/recovery,
activation and destructive-cleanup gates.
