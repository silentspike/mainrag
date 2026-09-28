"""Drain one supported build while retaining attempt-bound live progress.

A missing/stale progress endpoint never means the writer stopped. A mismatched
record fails only after the owned POST has drained, so no second writer starts.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import math
from pathlib import Path
import time
import uuid


def validate_observation(value: dict, source_id: int, commit: str, attempt: str) -> None:
    if not isinstance(value, dict) or value.get("schema_version") != "mainrag.storage-v2.build-progress.v1" \
            or value.get("source_id") != source_id or value.get("commit_sha") != commit \
            or value.get("attempt_id") != attempt:
        raise ValueError("progress identity differs from the owned build")
    staged, planned = value.get("staged_items"), value.get("planned_items")
    if type(staged) is not int or staged < 0 or (planned is None and staged != 0) or (planned is not None and (
            type(planned) is not int or planned < staged)) \
            or type(value.get("transaction_committed")) is not bool \
            or not isinstance(value.get("phase"), str):
        raise ValueError("progress count or transaction state is invalid")
    for key in ("elapsed_seconds", "db_staging_ms"):
        number = value.get(key)
        if type(number) not in (int, float) or not math.isfinite(number) or number < 0:
            raise ValueError("progress timing is invalid")


def monitored_build(arguments, token: str, request, write, *, interval: float = 30) -> tuple[dict, str]:
    path = arguments.checkpoint.with_suffix(arguments.checkpoint.suffix + ".progress.json")
    if path.exists() or path.is_symlink():
        raise RuntimeError("build progress attempt exists; reconcile before another write")
    attempt = str(uuid.uuid4())
    state = {"schema_version": "mainrag.storage-v2.build-monitor.v1",
             "attempt_id": attempt, "source_id": arguments.source_id,
             "commit_sha": arguments.commit_sha, "status": "starting",
             "observed_at_unix": int(time.time()), "progress": None}
    write(path, state, replace=False)
    endpoint = f"/api/v1/admin/sources/{arguments.source_id}/storage-v2-release-candidate"
    invalid = False
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="owned-candidate-build") as executor:
        future = executor.submit(request, arguments.api_url, token, "POST", endpoint + "-build",
                                 {"commit_sha": arguments.commit_sha, "progress_id": attempt})
        while True:
            try:
                result = future.result(timeout=interval)
                break
            except FutureTimeout:
                if future.done():  # Distinguish transport failure from the observation timer.
                    try:
                        result = future.result()
                    except BaseException:
                        state.update(status="request_failed_requires_reconciliation", observed_at_unix=int(time.time()))
                        write(path, state)
                        raise
                    break
                try:
                    observation = request(arguments.api_url, token, "GET",
                        endpoint + f"-progress?attempt_id={attempt}&commit_sha={arguments.commit_sha}",
                        timeout_seconds=15)
                    validate_observation(observation, arguments.source_id, arguments.commit_sha, attempt)
                    previous = state["progress"]
                    if previous is not None and observation["staged_items"] < previous["staged_items"]:
                        raise ValueError("progress reversed within the same attempt")
                    state.update(status="observed", progress=observation)
                    print(json.dumps({"phase": observation["phase"],
                        "staged_items": observation["staged_items"], "planned_items": observation["planned_items"],
                        "elapsed_seconds": observation["elapsed_seconds"],
                        "transaction_committed": observation["transaction_committed"]}), flush=True)
                except ValueError:
                    invalid = True
                    state["status"] = "identity_error_requires_reconciliation"
                except (OSError, RuntimeError):
                    state["status"] = "progress_unavailable_writer_unresolved"
                state["observed_at_unix"] = int(time.time())
                write(path, state)
            except BaseException:
                state.update(status="request_failed_requires_reconciliation", observed_at_unix=int(time.time()))
                write(path, state)
                raise
    state.update(status="response_received" if not invalid else "identity_error_requires_reconciliation",
                 observed_at_unix=int(time.time()), generation_id=result.get("generation_id"),
                 committed_items=result.get("item_count"), transaction_committed=True)
    write(path, state)
    if invalid:
        raise RuntimeError("build drained after mismatched progress; reconcile its persisted witness")
    return result, attempt
