#!/usr/bin/env python3
"""Validate a complete physical-pack matrix and export viewer-compatible telemetry.

No default is selected: different content/size cohorts must not be ranked together.
"""
import argparse
import itertools
import json
import math
from pathlib import Path
import re
import statistics

FIELDS = ("logical_bytes", "stored_bytes", "source_stored_bytes", "build_ms",
          "rewrite_ms", "verify_ms", "rewrite_mib_s", "process_peak_rss_bytes",
          "process_baseline_hwm_bytes", "integrity_passed", "entry_count")


def report(log, revision):
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("exact source revision required")
    if "test result: ok. 1 passed; 0 failed; 0 ignored;" not in log:
        raise ValueError("successful parent test and cleanup required")
    rows = [json.loads(line.removeprefix("PACK_RESOURCE "))
            for line in log.splitlines() if line.startswith("PACK_RESOURCE ")]
    expected = set(itertools.product((1, 2, 3), (1048576, 16777216),
                                    ("repeat", "random"), ("identity", "zstd"), (4096, 65536)))
    seen, profiles = set(), set()
    runs, groups, metrics = [], {}, {f"pack_resource.{key}": {"kind": "gauge", "v": {}, "z": {}} for key in FIELDS}
    for row in rows:
        if set(row) != set(FIELDS) | {"schema", "scope", "profile", "repetition", "large_body_bytes", "pattern", "codec", "buffer_bytes", "sql_ms", "device_io_bytes"}:
            raise ValueError("unexpected fields in public measurement")
        if any(type(row[field]) is not int for field in ("repetition", "large_body_bytes", "buffer_bytes")):
            raise ValueError("invalid matrix dimensions")
        key = tuple(row[field] for field in ("repetition", "large_body_bytes", "pattern", "codec", "buffer_bytes"))
        if key not in expected or key in seen:
            raise ValueError("unexpected or duplicate matrix cell")
        seen.add(key)
        profiles.add(row["profile"])
        if row["schema"] != "pack-resource-v1" or row["scope"] != "physical_pack_only":
            raise ValueError("wrong measurement scope")
        for field in FIELDS:
            value = row[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"invalid measurement: {field}")
        if (row["integrity_passed"] != 1 or row["entry_count"] != 3
                or row["logical_bytes"] != 4096 + 262144 + row["large_body_bytes"]
                or row["source_stored_bytes"] != row["logical_bytes"]
                or not 0 < row["process_baseline_hwm_bytes"] <= row["process_peak_rss_bytes"] <= 128*1024*1024
                or row["sql_ms"] is not None or row["device_io_bytes"] is not None):
            raise ValueError("integrity, memory or scope gate failed")
        if row["codec"] == "identity" and row["stored_bytes"] != row["logical_bytes"]:
            raise ValueError("identity representation length differs")
        calculated = row["logical_bytes"] / (row["rewrite_ms"] / 1000) / 1048576
        if not math.isclose(calculated, row["rewrite_mib_s"], rel_tol=1e-9):
            raise ValueError("throughput denominator differs")
        base = f"pack-{row['pattern']}-size{row['large_body_bytes']}-{row['codec']}-buf{row['buffer_bytes']}"
        name = base if row["repetition"] == 1 else f"{base}-{row['repetition']}"
        groups.setdefault(base, []).append(name)
        runs.append({"name": name, "git_commit": revision, "git_dirty": False,
                     "beschreibung": "Physical pack only; fresh-process VmHWM; no SQL or device attribution",
                     "dauer_s": sum(row[k] for k in ("build_ms", "rewrite_ms", "verify_ms"))/1000,
                     "messpunkte": 1, "start": "", "root": False})
        for field in FIELDS:
            value = row[field]
            metrics[f"pack_resource.{field}"]["v"][name] = {"r": value, "min": value, "max": value}
    if seen != expected or len(profiles) != 1 or not profiles <= {"debug", "release"}:
        raise ValueError("incomplete matrix or mixed build profiles")
    states = []
    for base, names in groups.items():
        states.append({"name": base, "laeufe": names, "anzahl": 3, "vollstaendig": True,
                       "git_commit": revision, "git_dirty": False, "start": "",
                       "beschreibung": "Compare settings only within the same size/pattern cohort"})
        for metric in metrics.values():
            values = [metric["v"][name]["r"] for name in names]
            metric["z"][base] = {"werte": values, "median": statistics.median(values),
                                     "min": min(values), "max": max(values), "n": len(values),
                                     "streuung": (max(values)-min(values))/max(values)*100}
    return {"schema": "pack-resource-report-v1", "scope": "physical_pack_only", "profile": profiles.pop(),
            "revision": revision, "qualification": "diagnostic_only", "selected_default": None,
            "runs": runs, "zustaende": states, "wiederholungen": groups, "metrics": metrics,
            "noise": {key: max(v["streuung"] for v in metric["z"].values()) for key, metric in metrics.items()},
            "measurements": rows,
            "limitations": ["No SQL, ingestion, device I/O or isolated CPU attribution",
                            "VmHWM is whole child lifetime, not stage-specific incremental RSS",
                            "Cache is not flushed; CI/debug results do not select production defaults"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = report(args.log.read_text(), args.revision)
    # Never replace an existing telemetry summary or earlier measurement package.
    with args.output.open("x") as target:
        json.dump(result, target, indent=2, allow_nan=False)
    print(f"PASS: {len(result['runs'])} runs, {len(result['zustaende'])} settings; {result['profile']} diagnostic only")


if __name__ == "__main__":
    main()
