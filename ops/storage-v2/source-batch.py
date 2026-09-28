#!/usr/bin/env python3
"""Run protected source-candidate phases with durable, source-boundary stops.

The private plan names existing storage-v2 tools and their arguments. A crashed
phase requires reconciliation against its persisted result and live generation;
the operator never repeats a possibly completed write by assumption.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid


TOOLS = {
    "source-review": "source-snapshot-review.py",
    "candidate-build": "release-candidate.py",
    "candidate-reconstruct": "reconstruct-candidate-checkpoint.py",
    "candidate-verify": "release-candidate.py",
}
PHASE = {"candidate-build": "build", "candidate-verify": "verify"}
NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")


def private_file(path: Path) -> bytes:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RuntimeError("operator input must be a private regular file")
    return path.read_bytes()


def write_state(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
        raise RuntimeError("operator state directory is not private")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_plan(plan: dict) -> None:
    if plan.get("schema_version") != "mainrag.storage-v2.source-batch-plan.v1" \
            or not isinstance(plan.get("package_commit_sha"), str) \
            or not SHA.fullmatch(plan["package_commit_sha"]) \
            or not isinstance(plan.get("sources"), list) or not plan["sources"]:
        raise RuntimeError("source batch plan identity is invalid")
    seen = set()
    for source in plan["sources"]:
        sid = source.get("source_id")
        if type(sid) is not int or sid <= 0 or sid in seen \
                or source.get("adapter") not in {"fs", "git", "managed_append", "pdf"} \
                or not isinstance(source.get("failure_group"), str) \
                or not NAME.fullmatch(source["failure_group"]) \
                or type(source.get("planned_items")) is not int \
                or source["planned_items"] < 0 \
                or not isinstance(source.get("steps"), list) or not source["steps"]:
            raise RuntimeError("source batch contains an invalid source")
        seen.add(sid)
        step_names = set()
        for step in source["steps"]:
            name, tool, arguments = step.get("name"), step.get("tool"), step.get("args")
            if not isinstance(name, str) or not NAME.fullmatch(name) or name in step_names \
                    or tool not in TOOLS or not isinstance(arguments, list) \
                    or not all(isinstance(value, str) and value and "\x00" not in value
                               for value in arguments) \
                    or not isinstance(step.get("result_path"), str) \
                    or not Path(step["result_path"]).is_absolute():
                raise RuntimeError("source batch contains an invalid step")
            if tool in PHASE and (not arguments or arguments[0] != PHASE[tool]):
                raise RuntimeError("candidate step does not name its supported phase")
            required = "--protected-output" if tool == "source-review" else (
                "--checkpoint" if tool in {"candidate-build", "candidate-reconstruct"}
                else "--output")
            if arguments.count("--source-id") != 1 or arguments[arguments.index("--source-id") + 1:
                    arguments.index("--source-id") + 2] != [str(sid)] \
                    or arguments.count(required) != 1 or arguments[arguments.index(required) + 1:
                    arguments.index(required) + 2] != [step["result_path"]]:
                raise RuntimeError("source step arguments differ from the protected plan")
            if tool == "candidate-build" and (arguments.count("--commit-sha") != 1 or
                    arguments[arguments.index("--commit-sha") + 1:
                    arguments.index("--commit-sha") + 2] != [plan["package_commit_sha"]]):
                raise RuntimeError("build package differs from the protected plan")
            step_names.add(name)


def initial_state(plan: dict, digest: str) -> dict:
    return {
        "schema_version": "mainrag.storage-v2.source-batch-state.v1",
        "plan_sha256": digest,
        "package_commit_sha": plan["package_commit_sha"],
        "created_at_unix": int(time.time()),
        "sources": [{
            "source_id": source["source_id"],
            "failure_group": source["failure_group"],
            "planned_items": source["planned_items"],
            "completed_items": 0,
            "status": "pending",
            "steps": [{"name": step["name"], "status": "pending"}
                      for step in source["steps"]],
        } for source in plan["sources"]],
    }


def process_start_ticks(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


def reconcile_crashed_steps(state: dict) -> bool:
    changed = False
    for source in state["sources"]:
        for step in source["steps"]:
            if step["status"] != "running":
                continue
            if step.get("process_start_ticks") is not None and (
                    process_start_ticks(step["pid"]) == step["process_start_ticks"]):
                raise RuntimeError("an earlier source phase is still running")
            step["status"] = "needs_reconciliation"
            source["status"] = "needs_reconciliation"
            changed = True
    return changed


def validate_result(step_plan: dict, result_path: Path,
                    source_id: int, package_commit: str) -> tuple[dict, str]:
    result_bytes = private_file(result_path)
    result = json.loads(result_bytes)
    result_source = (result.get("checkpoint", {}).get("source_id")
                     if step_plan["tool"] == "candidate-verify" else result.get("source_id"))
    if result_source != source_id:
        raise ValueError("step result source identity differs")
    if step_plan["tool"] == "candidate-build" \
            and result.get("commit_sha") != package_commit:
        raise ValueError("build package identity differs")
    if step_plan["tool"] == "candidate-reconstruct" \
            and result.get("reconstructed_from_persisted_witness") is not True:
        raise ValueError("persisted build witness was not reconciled")
    if step_plan["tool"] == "candidate-verify" and (
            result.get("qualification", {}).get("manifest", {}).get("status") != "PASS"
            or result.get("result", {}).get("status") != "release_candidate"):
        raise ValueError("qualification result is not accepted")
    return result, hashlib.sha256(result_bytes).hexdigest()


def observe_live_build(step_plan: dict, source_state: dict, step_state: dict, state: dict, state_path: Path) -> None:
    if step_plan["tool"] != "candidate-build": return
    path=Path(step_plan["result_path"])
    path=path.with_suffix(path.suffix+".progress.json")
    if not path.exists(): return
    try:
        value=json.loads(private_file(path))
        if value.get("schema_version")!="mainrag.storage-v2.build-monitor.v1" or value.get("source_id")!=source_state["source_id"] or value.get("commit_sha")!=state["package_commit_sha"]:
            raise ValueError("progress identity differs")
        attempt=value["attempt_id"]
        if step_state.get("progress_attempt_id",attempt)!=attempt: raise ValueError("progress attempt changed")
        step_state["progress_attempt_id"]=attempt
        source_state["live_progress"]={k:value.get(k) for k in ["status","observed_at_unix","transaction_committed","committed_items","generation_id"]}
        if value.get("progress") is not None:
            progress=value["progress"]
            if progress.get("attempt_id")!=attempt or progress.get("source_id")!=source_state["source_id"] or progress.get("commit_sha")!=state["package_commit_sha"]:
                raise ValueError("backend progress identity differs")
            source_state["live_progress"]["backend"]=progress
    except (OSError,ValueError,KeyError,TypeError,RuntimeError):
        source_state["live_progress"]={"status":"unavailable_requires_reconciliation"}
    write_state(state_path,state)


def invoke(step_plan: dict, state_path: Path, source_state: dict,
           step_state: dict, state: dict) -> bool:
    script = Path(__file__).with_name(TOOLS[step_plan["tool"]])
    command = [sys.executable, str(script), *step_plan["args"]]
    result_path = Path(step_plan["result_path"])
    if result_path.exists() or result_path.is_symlink():
        raise RuntimeError("step result already exists; reconcile before execution")
    log_path = state_path.parent / (f"source-{source_state['source_id']}-"
                                   f"{step_plan['name']}-{uuid.uuid4().hex[:12]}.log")
    descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        process = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[2],
                                   stdout=output, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        step_state.update(status="running", pid=process.pid,
                          process_start_ticks=process_start_ticks(process.pid),
                          started_at_unix=int(time.time()), log_name=log_path.name)
        source_state["status"] = "running"
        try:
            write_state(state_path, state)
        except OSError:
            process.wait()
            raise
        while True:
            try:
                code = process.wait(timeout=30)
                break
            except subprocess.TimeoutExpired:
                observe_live_build(step_plan, source_state, step_state, state, state_path)
        observe_live_build(step_plan, source_state, step_state, state, state_path)
    step_state["exit_code"] = code
    step_state["finished_at_unix"] = int(time.time())
    if code != 0 or not result_path.is_file():
        step_state["status"] = "failed"
        step_state["error_code"] = "command_failed" if code else "result_missing"
        source_state["status"] = "failed"
        write_state(state_path, state)
        return False
    try:
        result, result_sha256 = validate_result(
            step_plan, result_path, source_state["source_id"], state["package_commit_sha"])
    except (OSError, ValueError, TypeError, RuntimeError):
        step_state["status"] = "failed"
        step_state["error_code"] = "result_invalid"
        source_state["status"] = "failed"
        write_state(state_path, state)
        return False
    step_state["status"] = "passed"
    step_state["result_sha256"] = result_sha256
    if step_plan["tool"] in {"candidate-build", "candidate-reconstruct"}:
        source_state["completed_items"] = result.get("item_count", 0)
    write_state(state_path, state)
    return True


def run(args: argparse.Namespace) -> int:
    raw = private_file(args.plan)
    plan = json.loads(raw)
    validate_plan(plan)
    digest = hashlib.sha256(raw).hexdigest()
    lock_path = args.state.with_suffix(args.state.suffix + ".lock")
    args.state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(lock_path, "a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stop_path = args.state.with_suffix(args.state.suffix + ".stop")
        if args.resume and stop_path.exists():
            stop_path.unlink()
        if args.state.exists():
            state = json.loads(private_file(args.state))
            if state.get("plan_sha256") != digest:
                raise RuntimeError("source batch plan changed after the run began")
        else:
            state = initial_state(plan, digest)
            write_state(args.state, state)
        if reconcile_crashed_steps(state):
            write_state(args.state, state)
        stop_requested = False

        def stop_after_source(_signal: int, _frame: object) -> None:
            nonlocal stop_requested
            stop_requested = True

        signal.signal(signal.SIGTERM, stop_after_source)
        signal.signal(signal.SIGINT, stop_after_source)
        failed_groups = {source["failure_group"] for source in state["sources"]
                         if source["status"] in {"failed", "needs_reconciliation"}}
        for source_plan, source_state in zip(plan["sources"], state["sources"], strict=True):
            if source_state["status"] in {"passed", "failed", "needs_reconciliation", "skipped"}:
                continue
            if stop_requested or stop_path.exists():
                break
            if source_state["failure_group"] in failed_groups:
                source_state["status"] = "skipped"
                source_state["error_code"] = "same_failure_group"
                write_state(args.state, state)
                continue
            for step_plan, step_state in zip(source_plan["steps"], source_state["steps"], strict=True):
                if step_state["status"] == "passed":
                    continue
                if step_state["status"] != "pending":
                    break
                try:
                    passed = invoke(step_plan, args.state, source_state, step_state, state)
                except (OSError, RuntimeError, ValueError):
                    if step_state.get("status") == "running" \
                            and step_state.get("process_start_ticks") is not None \
                            and process_start_ticks(step_state["pid"]) == step_state["process_start_ticks"]:
                        raise RuntimeError("source phase remains live after operator error")
                    step_state["status"] = "needs_reconciliation"
                    step_state["error_code"] = "operator_exception"
                    source_state["status"] = "needs_reconciliation"
                    write_state(args.state, state)
                    failed_groups.add(source_state["failure_group"])
                    break
                if not passed:
                    failed_groups.add(source_state["failure_group"])
                    break
            else:
                source_state["status"] = "passed"
                write_state(args.state, state)
        counts = {status: sum(source["status"] == status for source in state["sources"])
                  for status in ("pending", "running", "passed", "failed",
                                 "needs_reconciliation", "skipped")}
        complete = counts["passed"] == len(state["sources"])
        stopped = not complete and (stop_requested or stop_path.exists())
        status = "PASS" if complete else "STOPPED" if stopped else "FAIL"
        print(json.dumps({"status": status, "source_counts": counts}, sort_keys=True))
        return 0 if complete else 2 if stopped else 1


def reconcile(args: argparse.Namespace) -> None:
    if args.plan is None or args.source_id is None or args.step_name is None \
            or args.outcome is None or args.evidence_file is None:
        raise RuntimeError("reconciliation requires plan, source, step, outcome and evidence")
    raw = private_file(args.plan)
    plan = json.loads(raw)
    validate_plan(plan)
    evidence_sha256 = hashlib.sha256(private_file(args.evidence_file)).hexdigest()
    lock_path = args.state.with_suffix(args.state.suffix + ".lock")
    with open(lock_path, "a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = json.loads(private_file(args.state))
        if state.get("plan_sha256") != hashlib.sha256(raw).hexdigest():
            raise RuntimeError("reconciliation plan identity differs")
        for source_plan, source in zip(plan["sources"], state["sources"], strict=True):
            if source["source_id"] != args.source_id:
                continue
            for step_plan, step in zip(source_plan["steps"], source["steps"], strict=True):
                if step["name"] != args.step_name:
                    continue
                if step["status"] not in {"needs_reconciliation", "failed"}:
                    raise RuntimeError("only an interrupted or failed phase can be reconciled")
                if args.outcome == "passed":
                    result, digest = validate_result(
                        step_plan, Path(step_plan["result_path"]), args.source_id,
                        state["package_commit_sha"])
                    step["status"] = "passed"
                    step["result_sha256"] = digest
                    if step_plan["tool"] in {"candidate-build", "candidate-reconstruct"}:
                        source["completed_items"] = result.get("item_count", 0)
                else:
                    if Path(step_plan["result_path"]).exists():
                        raise RuntimeError("persisted result exists; preserve and review it")
                    step["status"] = "pending"
                step["reconciliation_evidence_sha256"] = evidence_sha256
                source["status"] = "pending"
                write_state(args.state, state)
                print(json.dumps({"status": "RECONCILED", "outcome": args.outcome}))
                return
        raise RuntimeError("source phase is absent from the protected plan")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "stop", "status", "reconcile"))
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--source-id", type=int)
    parser.add_argument("--step-name")
    parser.add_argument("--outcome", choices=("passed", "retry"))
    parser.add_argument("--evidence-file", type=Path)
    args = parser.parse_args()
    if args.command == "run":
        if args.plan is None:
            parser.error("run requires --plan")
        return run(args)
    elif args.command == "stop":
        stop = args.state.with_suffix(args.state.suffix + ".stop")
        stop.touch(mode=0o600, exist_ok=True)
        print(json.dumps({"status": "STOP_REQUESTED"}))
    elif args.command == "reconcile":
        reconcile(args)
    else:
        state = json.loads(private_file(args.state))
        print(json.dumps({"status": "OBSERVED", "sources": [{
            "source_id": source["source_id"], "status": source["status"],
            "completed_items": source["completed_items"],
            "planned_items": source["planned_items"],
            "phase": next((step["name"] for step in source["steps"]
                           if step["status"] in {"running", "failed", "needs_reconciliation"}), None),
        } for source in state["sources"]]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        print(json.dumps({"status": "BLOCKED", "reason": str(error)}), file=sys.stderr)
        raise SystemExit(1) from error
