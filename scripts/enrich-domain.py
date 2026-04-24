#!/usr/bin/env python3
"""
MainRag Domain Enrichment Script

Generischer Enricher der ein Domain-Profil (TOML) laedt und auf code_sources anwendet.
Erstellt symbol_cards und symbol_annotations aus deterministischen Heuristiken.

Usage:
    python3 scripts/enrich-domain.py --profile bitwig [--batch-size 500] [--dry-run]
"""

import argparse
import json
import os
import re
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.extras


# =============================================================================
# Domain Profile Data Classes
# =============================================================================

@dataclass
class LayerRule:
    pattern_type: str  # path_contains, class_name_suffix, default
    pattern: str
    layer: str


@dataclass
class SideEffectRule:
    name_patterns: list[str]  # compiled later
    effect: str
    compiled: list[re.Pattern] = field(default_factory=list)


@dataclass
class ResourceRule:
    keywords: list[str]
    resource: str


@dataclass
class AnnotationPattern:
    regex: str
    annotation_type: str
    value: Optional[str] = None
    capture_group: Optional[int] = None
    compiled: Optional[re.Pattern] = None


@dataclass
class DelegationConfig:
    dispatch_indicators: list[str]
    proxy_class_patterns: list[re.Pattern]
    mutation_name_patterns: list[re.Pattern]
    max_candidates: int


@dataclass
class DomainProfile:
    name: str
    description: str
    language: str
    code_sources: list[str]
    support_sources: list[str]
    layers: list[LayerRule]
    side_effects: list[SideEffectRule]
    resources: list[ResourceRule]
    annotations: list[AnnotationPattern]
    delegation: DelegationConfig


def load_profile(profile_path: Path) -> DomainProfile:
    with open(profile_path, "rb") as f:
        data = tomllib.load(f)

    p = data["profile"]

    layers = []
    for lr in data.get("layers", []):
        layers.append(LayerRule(lr["pattern_type"], lr.get("pattern", ""), lr["layer"]))

    side_effects = []
    for se in data.get("side_effects", []):
        rule = SideEffectRule(se["name_patterns"], se["effect"])
        rule.compiled = [re.compile(pat) for pat in rule.name_patterns]
        side_effects.append(rule)

    resources = []
    for r in data.get("resources", []):
        resources.append(ResourceRule(r["path_or_class_keywords"], r["resource"]))

    annotations = []
    for a in data.get("annotations", []):
        ap = AnnotationPattern(
            regex=a["regex"],
            annotation_type=a["annotation_type"],
            value=a.get("value"),
            capture_group=a.get("capture_group"),
        )
        ap.compiled = re.compile(ap.regex)
        annotations.append(ap)

    d = data.get("delegation", {})
    delegation = DelegationConfig(
        dispatch_indicators=d.get("dispatch_indicators", []),
        proxy_class_patterns=[re.compile(p) for p in d.get("proxy_class_patterns", [])],
        mutation_name_patterns=[re.compile(p) for p in d.get("mutation_name_patterns", [])],
        max_candidates=d.get("max_delegation_candidates", 5),
    )

    return DomainProfile(
        name=p["name"],
        description=p["description"],
        language=p["language"],
        code_sources=p.get("code_sources", []),
        support_sources=p.get("support_sources", []),
        layers=layers,
        side_effects=side_effects,
        resources=resources,
        annotations=annotations,
        delegation=delegation,
    )


# =============================================================================
# Classification Functions
# =============================================================================

def classify_layer(file_path: str, class_name: str, rules: list[LayerRule]) -> tuple[str, float]:
    """Returns (layer, confidence)."""
    for rule in rules:
        if rule.pattern_type == "path_contains":
            if rule.pattern in file_path:
                return rule.layer, 0.85
        elif rule.pattern_type == "class_name_suffix":
            if class_name.endswith(rule.pattern):
                return rule.layer, 0.80
        elif rule.pattern_type == "default":
            return rule.layer, 0.3
    return "unknown", 0.3


def classify_side_effect(name: str, rules: list[SideEffectRule]) -> tuple[Optional[str], float]:
    """Returns (effect, confidence). None if no match (obfuscated)."""
    for rule in rules:
        for pat in rule.compiled:
            if pat.search(name):
                return rule.effect, 0.9
    # Check if name looks obfuscated (short, no camelCase pattern)
    if len(name) <= 4 and not any(c.isupper() for c in name[1:]):
        return "unknown", 0.2
    if not re.search(r'[a-z][A-Z]|^[a-z]{4,}', name):
        return "unknown", 0.2
    return None, 0.1


def classify_resource(file_path: str, class_name: str, rules: list[ResourceRule]) -> tuple[Optional[str], float]:
    """Returns (resource, confidence). None if no match."""
    combined = file_path + "/" + class_name
    for rule in rules:
        for kw in rule.keywords:
            if kw in combined:
                return rule.resource, 0.85
    return None, 0.1


def extract_class_name(file_path: str) -> str:
    """Extract class name from Java file path."""
    basename = file_path.rsplit("/", 1)[-1] if "/" in file_path else file_path
    return basename.replace(".java", "")


def compute_classification_confidence(
    layer_conf: Optional[float],
    side_effect_conf: Optional[float],
    resource_conf: Optional[float],
) -> float:
    """Weighted average over set fields. None (not-applicable) is ignored.
    Low values like 0.15 (unknown) actively drag the score down."""
    fields = [
        (layer_conf, 0.3),
        (side_effect_conf, 0.4),
        (resource_conf, 0.3),
    ]
    set_fields = [(conf, w) for conf, w in fields if conf is not None]
    if not set_fields:
        return 0.1
    return sum(c * w for c, w in set_fields) / sum(w for _, w in set_fields)


def classify_delegation_role(
    callee_name: str,
    caller_class: str,
    delegation: DelegationConfig,
) -> tuple[str, Optional[str], float]:
    """Returns (role, dispatch_via, confidence)."""
    # Check dispatch indicators
    for indicator in delegation.dispatch_indicators:
        if callee_name == indicator or callee_name.startswith(indicator + "("):
            return "dispatch", indicator, 0.9

    # Check mutation patterns
    for pat in delegation.mutation_name_patterns:
        if pat.search(callee_name):
            return "mutation", None, 0.85

    # Check if caller is a proxy class
    is_proxy_class = any(pat.search(caller_class) for pat in delegation.proxy_class_patterns)
    if is_proxy_class:
        return "proxy", None, 0.8

    # Obfuscated name?
    if len(callee_name) <= 4 and not re.search(r'[a-z][A-Z]', callee_name):
        return "unknown", None, 0.2

    return "unknown", None, 0.4


def build_summary(
    name: str,
    layer: str,
    side_effect: Optional[str],
    resource: Optional[str],
    delegation_targets: list[dict],
) -> str:
    """Build a compact summary. Skip unknown/None fields."""
    parts = [name]

    if side_effect and side_effect != "unknown":
        parts.append(f"[{side_effect}]")

    if layer and layer != "unknown":
        parts.append(f"in {layer}")

    if resource:
        parts.append(f"({resource})")

    # Delegation info
    if delegation_targets:
        named = [t["name"] for t in delegation_targets if t.get("role") != "unknown"]
        if named:
            parts.append(f"-> {', '.join(named[:3])}")

    return " ".join(parts)


# =============================================================================
# Main Enrichment Pipeline
# =============================================================================

def get_db_connection():
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("ERROR: Set DATABASE_URL env var (postgresql://user:pw@host:port/db)")
    return psycopg2.connect(dsn)


def find_source_ids(conn, source_names: list[str]) -> list[int]:
    """Find source IDs matching the given names."""
    if not source_names:
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name FROM sources WHERE name = ANY(%s)",
            (source_names,),
        )
        rows = cur.fetchall()
        found = {r[1]: r[0] for r in rows}
        for name in source_names:
            if name not in found:
                print(f"  WARNING: Source '{name}' not found in DB")
        return list(found.values())


def load_symbols(conn, source_ids: list[int], batch_size: int, offset: int) -> list[dict]:
    """Load a batch of symbols with file paths."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT s.id, s.name, s.type as symbol_type, s.line_start, s.line_end,
                   s.signature, s.qualified_name, s.file_id,
                   f.path as file_path
            FROM symbols s
            JOIN files f ON s.file_id = f.id
            WHERE f.source_id = ANY(%s)
            ORDER BY s.id
            LIMIT %s OFFSET %s
        """, (source_ids, batch_size, offset))
        return [dict(r) for r in cur.fetchall()]


def load_callees(conn, symbol_ids: list[int], max_candidates: int) -> dict[int, list[dict]]:
    """Load callees for multiple symbols at once."""
    if not symbol_ids:
        return {}
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT cg.caller_symbol_id, cg.callee_name, cg.callee_symbol_id,
                   cg.call_line, cg.call_type
            FROM call_graph cg
            WHERE cg.caller_symbol_id = ANY(%s)
            ORDER BY cg.caller_symbol_id, cg.call_line
        """, (symbol_ids,))
        result: dict[int, list[dict]] = {}
        for row in cur:
            caller_id = row["caller_symbol_id"]
            if caller_id not in result:
                result[caller_id] = []
            if len(result[caller_id]) < max_candidates:
                result[caller_id].append(dict(row))
        return result


def load_chunk_content_for_symbols(conn, symbols: list[dict]) -> dict[int, str]:
    """Load chunk content overlapping with symbol line ranges."""
    if not symbols:
        return {}

    # Build (file_id, start, end) tuples
    file_ranges = {}
    for sym in symbols:
        fid = sym["file_id"]
        if fid not in file_ranges:
            file_ranges[fid] = []
        file_ranges[fid].append((sym["id"], sym["line_start"], sym["line_end"]))

    result: dict[int, str] = {}

    with conn.cursor() as cur:
        for fid, ranges in file_ranges.items():
            for sym_id, line_start, line_end in ranges:
                cur.execute("""
                    SELECT content_text
                    FROM chunks
                    WHERE file_id = %s
                      AND start_line <= %s AND end_line >= %s
                      AND content_text IS NOT NULL
                    ORDER BY (end_line - start_line) ASC
                    LIMIT 1
                """, (fid, line_start, line_end))
                row = cur.fetchone()
                if row and row[0]:
                    result[sym_id] = row[0]

    return result


def extract_annotations(
    content: str,
    symbol_line_start: int,
    symbol_line_end: int,
    patterns: list[AnnotationPattern],
) -> list[dict]:
    """Extract annotations from content within symbol line range."""
    annotations = []
    lines = content.split("\n")

    for pattern in patterns:
        for i, line in enumerate(lines):
            match = pattern.compiled.search(line)
            if match:
                ann = {
                    "annotation_type": pattern.annotation_type,
                    "confidence": 1.0,
                }
                if pattern.capture_group is not None and match.lastindex and match.lastindex >= pattern.capture_group:
                    ann["value"] = match.group(pattern.capture_group)
                elif pattern.value:
                    ann["value"] = pattern.value
                else:
                    ann["value"] = match.group(0)

                # Try to compute evidence line (relative to file)
                ann["evidence_line"] = symbol_line_start + i

                annotations.append(ann)

    return annotations


def upsert_symbol_cards(conn, cards: list[dict], dry_run: bool):
    """Batch upsert symbol cards."""
    if not cards or dry_run:
        return

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO symbol_cards (
                symbol_id, layer, side_effect_type, affected_resource,
                delegation_targets, thread_requirement, preconditions,
                classification_confidence, summary, domain_profile,
                enrichment_version, updated_at
            ) VALUES %s
            ON CONFLICT (symbol_id) DO UPDATE SET
                layer = EXCLUDED.layer,
                side_effect_type = EXCLUDED.side_effect_type,
                affected_resource = EXCLUDED.affected_resource,
                delegation_targets = EXCLUDED.delegation_targets,
                thread_requirement = EXCLUDED.thread_requirement,
                preconditions = EXCLUDED.preconditions,
                classification_confidence = EXCLUDED.classification_confidence,
                summary = EXCLUDED.summary,
                domain_profile = EXCLUDED.domain_profile,
                enrichment_version = EXCLUDED.enrichment_version,
                updated_at = NOW()
            """,
            [(
                c["symbol_id"], c["layer"], c["side_effect_type"], c["affected_resource"],
                json.dumps(c["delegation_targets"]), c["thread_requirement"], c["preconditions"],
                c["classification_confidence"], c["summary"], c["domain_profile"],
                1,
            ) for c in cards],
            template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())",
        )
    conn.commit()


def upsert_annotations(conn, annotations: list[dict], dry_run: bool):
    """Batch upsert annotations."""
    if not annotations or dry_run:
        return

    # Deduplicate: same (symbol_id, annotation_type, value) → keep first
    seen = set()
    deduped = []
    for a in annotations:
        key = (a["symbol_id"], a["annotation_type"], a["value"])
        if key not in seen:
            seen.add(key)
            deduped.append(a)
    annotations = deduped

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO symbol_annotations (
                symbol_id, annotation_type, value, evidence_line, confidence, domain_profile
            ) VALUES %s
            ON CONFLICT (symbol_id, annotation_type, value) DO UPDATE SET
                evidence_line = EXCLUDED.evidence_line,
                confidence = EXCLUDED.confidence
            """,
            [(
                a["symbol_id"], a["annotation_type"], a["value"],
                a.get("evidence_line"), a["confidence"], a["domain_profile"],
            ) for a in annotations],
            template="(%s, %s, %s, %s, %s, %s)",
        )
    conn.commit()


def enrich_batch(
    conn,
    symbols: list[dict],
    profile: DomainProfile,
    dry_run: bool,
) -> tuple[int, int]:
    """Enrich a batch of symbols. Returns (cards_count, annotations_count)."""

    symbol_ids = [s["id"] for s in symbols]

    # Load callees for all symbols in batch
    callees_map = load_callees(conn, symbol_ids, profile.delegation.max_candidates)

    # Load chunk content for annotation extraction
    chunk_content = load_chunk_content_for_symbols(conn, symbols)

    cards = []
    all_annotations = []

    for sym in symbols:
        file_path = sym["file_path"]
        class_name = extract_class_name(file_path)
        sym_name = sym["name"]

        # 1. Layer
        layer, layer_conf = classify_layer(file_path, class_name, profile.layers)

        # 2. Side-Effect
        side_effect, se_conf = classify_side_effect(sym_name, profile.side_effects)

        # 3. Resource
        resource, res_conf = classify_resource(file_path, class_name, profile.resources)

        # 4. Delegation targets
        callees = callees_map.get(sym["id"], [])
        delegation_targets = []
        for callee in callees:
            role, dispatch_via, conf = classify_delegation_role(
                callee["callee_name"], class_name, profile.delegation,
            )
            delegation_targets.append({
                "name": callee["callee_name"],
                "symbol_id": callee["callee_symbol_id"],
                "role": role,
                "dispatch_via": dispatch_via,
                "confidence": round(conf, 2),
            })

        # 5. Annotations from chunk content
        thread_requirement = None
        preconditions = None
        content = chunk_content.get(sym["id"])
        if content:
            anns = extract_annotations(
                content, sym["line_start"], sym["line_end"], profile.annotations,
            )
            for ann in anns:
                ann["symbol_id"] = sym["id"]
                ann["domain_profile"] = profile.name
                all_annotations.append(ann)

                # Derive thread_requirement and preconditions for the card
                if ann["annotation_type"] == "thread_requirement":
                    thread_requirement = ann["value"]
                elif ann["annotation_type"] == "precondition":
                    preconditions = (preconditions or "") + ann["value"] + "; "

        # 6. Classification confidence
        # Three states: classified (use real conf), unknown (0.15 = actively penalizing),
        # not-applicable (None = ignored in weighted average)
        conf = compute_classification_confidence(
            layer_conf if layer != "unknown" else 0.15,
            se_conf if side_effect and side_effect != "unknown" else (
                0.15 if side_effect == "unknown" else None
            ),
            res_conf if resource else None,
        )

        # 7. Summary
        summary = build_summary(sym_name, layer, side_effect, resource, delegation_targets)

        cards.append({
            "symbol_id": sym["id"],
            "layer": layer,
            "side_effect_type": side_effect,
            "affected_resource": resource,
            "delegation_targets": delegation_targets,
            "thread_requirement": thread_requirement,
            "preconditions": preconditions.rstrip("; ") if preconditions else None,
            "classification_confidence": round(conf, 3),
            "summary": summary,
            "domain_profile": profile.name,
        })

    # Batch upsert
    upsert_symbol_cards(conn, cards, dry_run)
    upsert_annotations(conn, all_annotations, dry_run)

    return len(cards), len(all_annotations)


# =============================================================================
# Phase 2: Entity & Ownership Enrichment (Sprint 3)
# =============================================================================

def create_entities_from_symbols(conn, source_ids: list[int], profile_name: str, dry_run: bool) -> int:
    """Create entities from class/interface/enum symbols as bridge to entity_relations."""
    if dry_run:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(DISTINCT s.id)
                FROM symbols s
                JOIN files f ON s.file_id = f.id
                WHERE f.source_id = ANY(%s) AND s.type IN ('class', 'interface', 'enum')
            """, (source_ids,))
            return cur.fetchone()[0]

    with conn.cursor() as cur:
        # For each class symbol, create a unique entity per (entity_type, normalized_name, file_path).
        # Uses a CTE to deduplicate before insertion (one entity per class, not per chunk overlap).
        cur.execute("""
            WITH candidate AS (
                SELECT DISTINCT ON (s.name, s.type, f.path)
                    c.id as chunk_id, s.name,
                    CASE s.type WHEN 'interface' THEN 'interface' WHEN 'enum' THEN 'enum' ELSE 'class' END as entity_type,
                    lower(s.name) as normalized_name, 0.95 as confidence,
                    jsonb_build_object('symbol_id', s.id, 'domain_profile', %s, 'file_path', f.path) as metadata
                FROM symbols s
                JOIN files f ON s.file_id = f.id
                JOIN chunks c ON c.file_id = f.id
                    AND c.start_line <= s.line_start AND c.end_line >= s.line_start
                    AND c.content_text IS NOT NULL
                WHERE f.source_id = ANY(%s)
                  AND s.type IN ('class', 'interface', 'enum')
                ORDER BY s.name, s.type, f.path,
                    CASE WHEN c.chunk_type = 'class' THEN 0 ELSE 1 END,
                    (c.end_line - c.start_line) ASC
            )
            INSERT INTO entities (chunk_id, name, entity_type, normalized_name, confidence, metadata)
            SELECT chunk_id, name, entity_type, normalized_name, confidence, metadata
            FROM candidate
            ON CONFLICT (entity_type, normalized_name, (metadata->>'file_path'))
                WHERE metadata->>'file_path' IS NOT NULL
            DO UPDATE SET
                metadata = EXCLUDED.metadata,
                confidence = EXCLUDED.confidence
        """, (profile_name, source_ids))
        count = cur.rowcount
    conn.commit()
    return count


OWNERSHIP_PATTERNS = {
    "container_field": re.compile(
        r'(?:private|protected|public)?\s+(?:final\s+)?(?:List|Set|Collection|Map)<\s*(\w+)'
    ),
    "owner_return": re.compile(
        r'(?:public|protected)\s+(\w+)\s+get(?:Parent|Owner|Document|Project)\s*\('
    ),
    "proxy_target": re.compile(
        r'(?:private|protected)\s+(?:final\s+)?(\w+)\s+(?:target|delegate|wrapped|impl)\b'
    ),
    "factory_method": re.compile(
        r'(?:public|protected)\s+(\w+)\s+create(\w+)\s*\('
    ),
    "delete_method": re.compile(
        r'(?:public|protected)\s+\w+\s+(?:delete|remove)(\w+)\s*\('
    ),
    # Broader patterns for obfuscated code:
    # PascalCase field declarations (min 4 chars, filters obfuscated names)
    "field_type": re.compile(
        r'(?:private|protected|public)\s+(?:final\s+)?([A-Z][a-zA-Z]{3,})\s+\w+\s*[;=]'
    ),
    # Method parameter types (PascalCase, min 4 chars)
    "param_type": re.compile(
        r'[,(]\s*([A-Z][a-zA-Z]{3,})\s+\w+'
    ),
}

# Java primitive/common types to exclude from ownership extraction
JAVA_PRIMITIVES = frozenset({
    "String", "Integer", "Long", "Boolean", "Object", "Double", "Float",
    "Byte", "Short", "Character", "Void", "Class", "Number", "Comparable",
    "Serializable", "Cloneable", "Iterable", "Iterator", "Enum",
    "Exception", "RuntimeException", "Error", "Throwable",
    "Override", "Deprecated", "SuppressWarnings", "FunctionalInterface",
    "List", "Map", "Set", "Collection", "Optional", "Stream",
    "Arrays", "Collections", "Objects", "Math", "System",
})


def extract_ownership_relations(
    conn, source_ids: list[int], profile_name: str, dry_run: bool,
) -> int:
    """Extract ownership/containment relations from class body content."""
    # Load all class entities with their symbol_ids and chunk content
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT e.id as entity_id, e.name, e.metadata, c.content_text, c.id as chunk_id,
                   c.start_line
            FROM entities e
            JOIN chunks c ON e.chunk_id = c.id
            WHERE e.entity_type IN ('class', 'interface', 'enum')
              AND e.metadata->>'domain_profile' = %s
              AND c.content_text IS NOT NULL
        """, (profile_name,))
        class_entities = [dict(r) for r in cur.fetchall()]

    if not class_entities:
        return 0

    # Build name→entity_id lookup
    entity_name_map: dict[str, int] = {}
    for e in class_entities:
        entity_name_map[e["name"]] = e["entity_id"]

    # Also load ALL file-level chunks for these class files to get full class bodies
    symbol_ids = [e["metadata"]["symbol_id"] for e in class_entities if e.get("metadata", {}).get("symbol_id")]

    # For broader content, load class-level chunks (larger span)
    class_content: dict[int, str] = {}
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        for entity in class_entities:
            sid = entity.get("metadata", {}).get("symbol_id")
            if not sid:
                continue
            cur.execute("""
                SELECT string_agg(c.content_text, E'\n' ORDER BY c.start_line) as full_content
                FROM chunks c
                JOIN symbols s ON c.file_id = s.file_id
                WHERE s.id = %s
                  AND c.start_line >= s.line_start AND c.end_line <= s.line_end
                  AND c.content_text IS NOT NULL
            """, (sid,))
            row = cur.fetchone()
            if row and row["full_content"]:
                class_content[entity["entity_id"]] = row["full_content"]

    relations = []
    unresolved_entities: dict[str, int] = {}  # name → entity_id for auto-created entities

    def resolve_target(target_name: str) -> Optional[int]:
        """Resolve target name to entity_id, creating unresolved if needed."""
        if target_name in entity_name_map:
            return entity_name_map[target_name]
        if target_name in unresolved_entities:
            return unresolved_entities[target_name]
        return None  # Will be created in batch later

    for entity in class_entities:
        eid = entity["entity_id"]
        content = class_content.get(eid, entity.get("content_text", ""))
        if not content:
            continue

        chunk_id = entity["chunk_id"]
        source_symbol_id = entity.get("metadata", {}).get("symbol_id")

        # Extract container fields: List<Clip> → contains(this, Clip)
        for m in OWNERSHIP_PATTERNS["container_field"].finditer(content):
            target_name = m.group(1)
            if target_name in ("String", "Integer", "Long", "Boolean", "Object", "byte"):
                continue
            target_id = resolve_target(target_name)
            if target_id is None:
                unresolved_entities[target_name] = -1  # placeholder
            relations.append({
                "source_entity_id": eid,
                "target_name": target_name,
                "relation_type": "contains",
                "confidence": 0.7,
                "chunk_id": chunk_id,
                "evidence_text": m.group(0).strip()[:100],
                "source_symbol_id": source_symbol_id,
            })

        # Extract owner methods: getParent() → owned_by(this, return_type)
        for m in OWNERSHIP_PATTERNS["owner_return"].finditer(content):
            target_name = m.group(1)
            if target_name in ("void", "boolean", "int", "String"):
                continue
            relations.append({
                "source_entity_id": eid,
                "target_name": target_name,
                "relation_type": "owned_by",
                "confidence": 0.6,
                "chunk_id": chunk_id,
                "evidence_text": m.group(0).strip()[:100],
                "source_symbol_id": source_symbol_id,
            })

        # Extract proxy targets: private final InternalClip target → wraps_target
        for m in OWNERSHIP_PATTERNS["proxy_target"].finditer(content):
            target_name = m.group(1)
            if target_name in ("String", "int", "boolean", "Object", "long", "double"):
                continue
            relations.append({
                "source_entity_id": eid,
                "target_name": target_name,
                "relation_type": "wraps_target",
                "confidence": 0.75,
                "chunk_id": chunk_id,
                "evidence_text": m.group(0).strip()[:100],
                "source_symbol_id": source_symbol_id,
            })

        # Broader: PascalCase field types → contains(this, Type)
        for m in OWNERSHIP_PATTERNS["field_type"].finditer(content):
            target_name = m.group(1)
            if target_name in JAVA_PRIMITIVES:
                continue
            # Avoid duplicates with container_field
            if target_name not in [r["target_name"] for r in relations if r["source_entity_id"] == eid and r["relation_type"] == "contains"]:
                relations.append({
                    "source_entity_id": eid,
                    "target_name": target_name,
                    "relation_type": "contains",
                    "confidence": 0.5,
                    "chunk_id": chunk_id,
                    "evidence_text": m.group(0).strip()[:100],
                    "source_symbol_id": source_symbol_id,
                })

        # Broader: Method parameter types → uses(this, Type)
        for m in OWNERSHIP_PATTERNS["param_type"].finditer(content):
            target_name = m.group(1)
            if target_name in JAVA_PRIMITIVES:
                continue
            if target_name not in [r["target_name"] for r in relations if r["source_entity_id"] == eid and r["relation_type"] == "uses"]:
                relations.append({
                    "source_entity_id": eid,
                    "target_name": target_name,
                    "relation_type": "uses",
                    "confidence": 0.4,
                    "chunk_id": chunk_id,
                    "evidence_text": m.group(0).strip()[:100],
                    "source_symbol_id": source_symbol_id,
                })

    if dry_run:
        return len(relations)

    # Create unresolved entities
    if unresolved_entities:
        with conn.cursor() as cur:
            for name in unresolved_entities:
                if name in entity_name_map:
                    continue
                # Find any chunk that mentions this class
                cur.execute("""
                    SELECT c.id FROM chunks c
                    JOIN files f ON c.file_id = f.id
                    WHERE f.source_id = ANY(%s) AND c.content_text ILIKE %s
                    LIMIT 1
                """, (source_ids, f'%{name}%'))
                row = cur.fetchone()
                if row:
                    cur.execute("""
                        INSERT INTO entities (chunk_id, name, entity_type, normalized_name, confidence, metadata)
                        VALUES (%s, %s, 'unresolved_class', %s, 0.5, %s)
                        ON CONFLICT DO NOTHING
                        RETURNING id
                    """, (row[0], name, name.lower(), json.dumps({"domain_profile": profile_name})))
                    result = cur.fetchone()
                    if result:
                        unresolved_entities[name] = result[0]
                        entity_name_map[name] = result[0]
        conn.commit()

    # Insert relations
    inserted = 0
    with conn.cursor() as cur:
        for rel in relations:
            target_id = entity_name_map.get(rel["target_name"])
            if not target_id or target_id == -1:
                continue
            try:
                cur.execute("""
                    INSERT INTO entity_relations
                        (source_entity_id, target_entity_id, relation_type, confidence, chunk_id, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (source_entity_id, target_entity_id, relation_type) DO UPDATE SET
                        confidence = EXCLUDED.confidence,
                        metadata = EXCLUDED.metadata
                """, (
                    rel["source_entity_id"], target_id, rel["relation_type"],
                    rel["confidence"], rel["chunk_id"],
                    json.dumps({
                        "evidence_line": None,
                        "extraction_rule": rel["relation_type"],
                        "domain_profile": profile_name,
                        "source_text": rel["evidence_text"],
                        "source_symbol_id": rel.get("source_symbol_id"),
                    }),
                ))
                inserted += 1
            except Exception as e:
                # Skip constraint violations silently
                conn.rollback()
                continue
    conn.commit()
    return inserted


def extract_delegation_relations(
    conn, source_ids: list[int], profile_name: str, dry_run: bool,
) -> int:
    """Derive class-level delegates_to from method-level delegation_targets."""

    # 1. Build symbol_id → class_entity_id map
    symbol_to_class: dict[int, int] = {}
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT e.id as entity_id, (e.metadata->>'symbol_id')::bigint as class_symbol_id,
                   s_class.line_start as class_start, s_class.line_end as class_end,
                   s_class.file_id
            FROM entities e
            JOIN symbols s_class ON s_class.id = (e.metadata->>'symbol_id')::bigint
            WHERE e.entity_type IN ('class', 'interface', 'enum')
              AND e.metadata->>'domain_profile' = %s
        """, (profile_name,))
        class_entities = [dict(r) for r in cur.fetchall()]

    # 2. Load all symbol_cards with delegation_targets
    delegations: list[tuple[int, list[dict]]] = []
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT sc.symbol_id, sc.delegation_targets
            FROM symbol_cards sc
            JOIN symbols s ON sc.symbol_id = s.id
            JOIN files f ON s.file_id = f.id
            WHERE f.source_id = ANY(%s)
              AND sc.delegation_targets != '[]'::jsonb
              AND sc.domain_profile = %s
        """, (source_ids, profile_name))
        for row in cur:
            targets = json.loads(row["delegation_targets"]) if isinstance(row["delegation_targets"], str) else row["delegation_targets"]
            if targets:
                delegations.append((row["symbol_id"], targets))

    # 2b. Build symbol_name → set of class_entity_ids (for name-based resolution)
    name_to_classes: dict[str, set[int]] = {}
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        for ce in class_entities:
            cur.execute("""
                SELECT s.id, s.name FROM symbols s
                WHERE s.file_id = %s AND s.line_start >= %s AND s.line_end <= %s
            """, (ce["file_id"], ce["class_start"], ce["class_end"]))
            for row in cur:
                symbol_to_class[row["id"]] = ce["entity_id"]
                name_to_classes.setdefault(row["name"], set()).add(ce["entity_id"])

    # 3. Build class→class delegates_to relations
    seen_pairs: set[tuple[int, int]] = set()
    relations = []
    for caller_sym_id, targets in delegations:
        source_class = symbol_to_class.get(caller_sym_id)
        if not source_class:
            continue
        for target in targets:
            # Try symbol_id first, then name-based resolution
            target_sym_id = target.get("symbol_id")
            target_classes: set[int] = set()
            if target_sym_id:
                tc = symbol_to_class.get(target_sym_id)
                if tc:
                    target_classes.add(tc)
            if not target_classes:
                # Name-based fallback: ONLY for non-obfuscated names (PascalCase or camelCase, min 4 chars)
                target_name = target.get("name", "")
                if len(target_name) >= 4 and re.search(r'[a-z][A-Z]|^[a-z]{4,}|^[A-Z][a-z]', target_name):
                    target_classes = name_to_classes.get(target_name, set())

            for target_class in target_classes:
                if target_class == source_class:
                    continue
                pair = (source_class, target_class)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                relations.append({
                    "source_entity_id": source_class,
                    "target_entity_id": target_class,
                    "confidence": target.get("confidence", 0.5) * (0.9 if target_sym_id else 0.5),
                })

    if dry_run:
        return len(relations)

    inserted = 0
    with conn.cursor() as cur:
        for rel in relations:
            try:
                cur.execute("""
                    INSERT INTO entity_relations
                        (source_entity_id, target_entity_id, relation_type, confidence, metadata)
                    VALUES (%s, %s, 'delegates_to', %s, %s)
                    ON CONFLICT (source_entity_id, target_entity_id, relation_type) DO UPDATE SET
                        confidence = GREATEST(entity_relations.confidence, EXCLUDED.confidence)
                """, (
                    rel["source_entity_id"], rel["target_entity_id"],
                    rel["confidence"],
                    json.dumps({"domain_profile": profile_name, "extraction_rule": "delegation_targets"}),
                ))
                inserted += 1
            except Exception:
                conn.rollback()
                continue
    conn.commit()
    return inserted


def main():
    parser = argparse.ArgumentParser(description="MainRag Domain Enrichment")
    parser.add_argument("--profile", required=True, help="Domain profile name (e.g. 'bitwig')")
    parser.add_argument("--profiles-dir", default="data/domain_profiles",
                        help="Directory containing domain profiles")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true", help="Don't write to DB")
    parser.add_argument("--ownership", action="store_true",
                        help="Also run ownership/entity extraction (Sprint 3)")
    args = parser.parse_args()

    # Find and load profile
    script_dir = Path(__file__).parent.parent
    profile_path = script_dir / args.profiles_dir / f"{args.profile}.toml"
    if not profile_path.exists():
        print(f"ERROR: Profile not found: {profile_path}")
        sys.exit(1)

    print(f"Loading profile: {profile_path}")
    profile = load_profile(profile_path)
    print(f"  Name: {profile.name}")
    print(f"  Code sources: {profile.code_sources}")
    print(f"  Support sources: {profile.support_sources}")
    print(f"  Layers: {len(profile.layers)} rules")
    print(f"  Side effects: {len(profile.side_effects)} rules")
    print(f"  Resources: {len(profile.resources)} rules")
    print(f"  Annotations: {len(profile.annotations)} patterns")

    if args.dry_run:
        print("  DRY RUN — no DB writes")

    # Connect
    conn = get_db_connection()

    # Find source IDs (only code_sources for enrichment!)
    print(f"\nFinding code sources...")
    source_ids = find_source_ids(conn, profile.code_sources)
    if not source_ids:
        print("ERROR: No matching code sources found!")
        sys.exit(1)
    print(f"  Found source IDs: {source_ids}")

    # Count symbols
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM symbols s JOIN files f ON s.file_id = f.id WHERE f.source_id = ANY(%s)",
            (source_ids,),
        )
        total_symbols = cur.fetchone()[0]
    print(f"  Total symbols to enrich: {total_symbols}")

    # Process in batches
    total_cards = 0
    total_annotations = 0
    offset = 0
    batch_num = 0
    start_time = time.time()

    while offset < total_symbols:
        batch_num += 1
        symbols = load_symbols(conn, source_ids, args.batch_size, offset)
        if not symbols:
            break

        cards_count, anns_count = enrich_batch(conn, symbols, profile, args.dry_run)
        total_cards += cards_count
        total_annotations += anns_count
        offset += len(symbols)

        elapsed = time.time() - start_time
        rate = offset / elapsed if elapsed > 0 else 0
        print(f"  Batch {batch_num}: {offset}/{total_symbols} symbols "
              f"({total_cards} cards, {total_annotations} annotations, "
              f"{rate:.0f} sym/s)")

    elapsed = time.time() - start_time
    print(f"\nDone in {elapsed:.1f}s")
    print(f"  Symbol cards: {total_cards}")
    print(f"  Annotations: {total_annotations}")

    if args.dry_run:
        print("  (DRY RUN — nothing written)")

    # Phase 2: Ownership / Entity extraction (--ownership flag)
    if args.ownership:
        print(f"\n--- Phase 2: Entity & Ownership Extraction ---")

        print("Creating entities from class/interface/enum symbols...")
        entity_count = create_entities_from_symbols(conn, source_ids, profile.name, args.dry_run)
        print(f"  Entities created/found: {entity_count}")

        print("Extracting ownership relations from code patterns...")
        rel_count = extract_ownership_relations(conn, source_ids, profile.name, args.dry_run)
        print(f"  Ownership relations extracted: {rel_count}")

        print("Extracting delegation relations from symbol cards...")
        deleg_count = extract_delegation_relations(conn, source_ids, profile.name, args.dry_run)
        print(f"  Delegation relations extracted: {deleg_count}")

        if args.dry_run:
            print("  (DRY RUN — nothing written)")

    conn.close()


if __name__ == "__main__":
    main()
