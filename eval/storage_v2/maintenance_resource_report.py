#!/usr/bin/env python3
"""Validate repeated integrated PG/file operator measurements; not SQL-only timing."""
import argparse
import itertools
import json
import math
from pathlib import Path
import re
import statistics

FIELDS = ("repack_ms", "finish_ms", "process_peak_rss_bytes", "process_baseline_hwm_bytes",
          "moved_entries", "logical_bytes", "old_file_bytes", "new_file_bytes",
          "dead_entry_bytes", "reclaimed_file_bytes", "integrity_passed")


def report(log, revision):
    if not re.fullmatch(r"[0-9a-f]{40}", revision) or "test result: ok. 1 passed; 0 failed; 0 ignored;" not in log:
        raise ValueError("exact revision and successful parent test required")
    rows = [json.loads(line.removeprefix("PACK_MAINTENANCE ")) for line in log.splitlines()
            if line.startswith("PACK_MAINTENANCE ")]
    seen, profiles, runs, groups = set(), set(), [], {}
    metrics = {f"pack_maintenance.{key}": {"kind": "gauge", "v": {}, "z": {}} for key in FIELDS}
    for row in rows:
        if set(row) != set(FIELDS) | {"schema", "scope", "profile", "repetition", "buffer_bytes", "database_server_rss_bytes", "device_io_bytes", "sql_only_ms"}:
            raise ValueError("unexpected public fields")
        if type(row["buffer_bytes"]) is not int or type(row["repetition"]) is not int:
            raise ValueError("integer matrix dimensions required")
        key = row["repetition"], row["buffer_bytes"]
        if key not in set(itertools.product((1, 2, 3), (4096, 65536))) or key in seen:
            raise ValueError("unexpected or duplicate cell")
        seen.add(key)
        profiles.add(row["profile"])
        if row["schema"] != "pack-maintenance-resource-v1" or row["scope"] != "integrated_pg_file_operator":
            raise ValueError("wrong measurement scope")
        for field in FIELDS:
            value = row[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"invalid measurement: {field}")
        if (row["moved_entries"] != 2 or row["logical_bytes"] != 1114112
                or row["old_file_bytes"] != 1245184 or row["dead_entry_bytes"] != 131072
                or row["reclaimed_file_bytes"] != row["old_file_bytes"]
                or not row["new_file_bytes"] < row["logical_bytes"] or row["integrity_passed"] != 1
                or not row["process_baseline_hwm_bytes"] <= row["process_peak_rss_bytes"] <= 128*1024*1024
                or any(row[key] is not None for key in ("database_server_rss_bytes", "device_io_bytes", "sql_only_ms"))):
            raise ValueError("integrity, accounting, memory or attribution gate failed")
        base = f"maintenance-buf{row['buffer_bytes']}"
        name = base if row["repetition"] == 1 else f"{base}-{row['repetition']}"
        groups.setdefault(base, []).append(name)
        runs.append({"name": name, "git_commit": revision, "git_dirty": False, "start": "",
                     "beschreibung": "Integrated operator wall time; client lifetime RSS; not database-server RSS",
                     "dauer_s": (row["repack_ms"]+row["finish_ms"])/1000, "messpunkte": 1})
        for field in FIELDS:
            metrics[f"pack_maintenance.{field}"]["v"][name] = {"r": row[field], "min": row[field], "max": row[field]}
    if len(seen) != 6 or len(profiles) != 1 or not profiles <= {"debug", "release"}:
        raise ValueError("incomplete matrix or inconsistent profile")
    states = []
    for base, names in groups.items():
        states.append({"name": base, "laeufe": names, "anzahl": 3, "vollstaendig": True,
                       "git_commit": revision, "git_dirty": False, "start": "", "beschreibung": "Same logical/dead bytes and zstd policy"})
        for metric in metrics.values():
            values = [metric["v"][name]["r"] for name in names]
            metric["z"][base] = {"werte": values, "median": statistics.median(values), "min": min(values),
                                     "max": max(values), "n": 3, "streuung": (max(values)-min(values))/max(values)*100}
    return {"schema": "maintenance-resource-report-v1", "revision": revision, "scope": "integrated_pg_file_operator",
            "profile": profiles.pop(), "qualification": "diagnostic_only", "selected_default": None,
            "measurements": rows, "runs": runs, "zustaende": states, "wiederholungen": groups, "metrics": metrics,
            "noise": {key: max(v["streuung"] for v in metric["z"].values()) for key, metric in metrics.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = report(args.log.read_text(), args.revision)
    with args.output.open("x") as target:
        json.dump(result, target, indent=2, allow_nan=False)
    print("PASS: 6 integrated maintenance runs, exact accounting and client RSS; diagnostic only")


if __name__ == "__main__":
    main()
