"""Validate the registered globset byte matcher bound into the adapter profile.

Source reviews use the adapter's exact compiled matcher, not an approximation
with a second glob dialect. Excluded files are classified without opening them.
"""

import hashlib
import json
import re

PROJECTION_SUFFIX = ".text-utf8-nul-space-v1"


def scope_profile(sha256: str, cut: bool = False) -> str:
    version = "v4.btrfs-cut-v1" if cut else "v3"
    return f"mainrag.fs-release-candidate.{version}.scope-{sha256}.fragment-1048576-newline-65536"


def scope_matcher(proof: object, profile: str):
    profile = profile.removesuffix(PROJECTION_SUFFIX)
    if proof is None:
        if profile.startswith("mainrag.fs-release-candidate.v3.") or (
                profile.startswith("mainrag.fs-release-candidate.v4.") and profile != scope_profile("unfiltered", True)):
            raise RuntimeError("configured filesystem scope proof is missing")
        return lambda relative: True
    if (not isinstance(proof, dict) or proof.get("format") != "mainrag.fs-scope.v1"
            or not isinstance(proof.get("patterns"), list)
            or not 1 <= len(proof["patterns"]) <= 64
            or any(not isinstance(value, str) or not value or len(value.encode()) > 512
                   for value in proof["patterns"])
            or proof["patterns"] != sorted(set(proof["patterns"]))
            or not isinstance(proof.get("byte_regexes"), list)
            or len(proof["byte_regexes"]) != len(proof["patterns"])):
        raise RuntimeError("filesystem scope proof is invalid")
    payload = json.dumps([proof["patterns"], proof["byte_regexes"]],
                         ensure_ascii=False, separators=(",", ":")).encode()
    digest = hashlib.sha256(b"mainrag.fs-scope.v1\0" + payload).hexdigest()
    if proof.get("sha256") != digest or profile not in {scope_profile(digest), scope_profile(digest, True)}:
        raise RuntimeError("filesystem scope profile binding differs")
    matchers = []
    for expression in proof["byte_regexes"]:
        if not isinstance(expression, str) or not expression.startswith("(?-u)^") \
                or not expression.endswith("$") or len(expression.encode()) > 16384:
            raise RuntimeError("filesystem scope byte regex is invalid")
        try:
            matchers.append(re.compile(expression.removeprefix("(?-u)").encode(), re.DOTALL))
        except re.error as error:
            raise RuntimeError("filesystem scope byte regex is unsupported") from error
    return lambda relative: any(matcher.fullmatch(relative.encode()) for matcher in matchers)


def registered_scope_matcher(config: object, observation: dict):
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except ValueError as error:
            raise RuntimeError("registered filesystem config is invalid") from error
    if config is not None and not isinstance(config, dict):
        raise RuntimeError("registered filesystem config is invalid")
    patterns = (config or {}).get("file_patterns")
    consistency = (config or {}).get("filesystem_consistency")
    projection = (config or {}).get("conversation_text_projection")
    if projection not in {None, "utf8-nul-space-v1"} or (
            projection is not None and consistency != "btrfs-cut-v1"):
        raise RuntimeError("registered conversation text projection is unsupported")
    if (projection == "utf8-nul-space-v1") != observation["adapter_profile_id"].endswith(PROJECTION_SUFFIX):
        raise RuntimeError("conversation text projection differs from registration")
    cut = observation.get("filesystem_cut")
    if consistency not in {None, "btrfs-cut-v1"}:
        raise RuntimeError("registered filesystem consistency is unsupported")
    if (consistency == "btrfs-cut-v1") != (cut is not None) or (
            (consistency == "btrfs-cut-v1") != observation["adapter_profile_id"].startswith(
                "mainrag.fs-release-candidate.v4.btrfs-cut-v1.")):
        raise RuntimeError("filesystem consistency differs from registration")
    proof = observation.get("filesystem_scope")
    if patterns is not None:
        if (not isinstance(patterns, list) or not patterns
                or any(not isinstance(value, str) for value in patterns)
                or not isinstance(proof, dict)
                or proof.get("patterns") != sorted(set(patterns))):
            raise RuntimeError("adapter does not implement the registered filesystem filter")
    elif proof is not None:
        raise RuntimeError("adapter filesystem filter differs from registration")
    return scope_matcher(proof, observation["adapter_profile_id"])
