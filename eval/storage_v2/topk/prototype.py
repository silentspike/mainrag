#!/usr/bin/env python3
"""Exact composed-view Top-K prototype for MainRAG storage v2."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


EVAL_ROOT = Path(__file__).resolve().parents[2]
ROOT = EVAL_ROOT.parent
sys.path.insert(0, str(EVAL_ROOT))

from eval_common import percentile  # noqa: E402
from storage_v2.harness import (  # noqa: E402
    PsqlSession,
    TemporaryPostgres,
    git_output,
    sha256_bytes,
    sha256_file,
    sql_literal,
)


HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures.json"
QUERIES = HERE / "queries.jsonl"
SCHEMA = HERE / "artifact.schema.json"
WORD = re.compile(r"[A-Za-z0-9_]+", re.UNICODE)
LEXER = re.compile(
    r"\s*(?:"
    r"(?P<phrase>\"[^\"]+\")|"
    r"(?P<lparen>\()|(?P<rparen>\))|"
    r"(?P<or>\bOR\b)|(?P<and>\bAND\b)|"
    r"(?P<minus>-)|"
    r"(?P<exact>id:[A-Za-z0-9_.:/-]+)|"
    r"(?P<word>[A-Za-z0-9_./:-]+)"
    r")",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Node:
    kind: str
    value: str | None = None
    children: tuple["Node", ...] = ()

    def as_json(self) -> dict[str, Any]:
        result: dict[str, Any] = {"type": self.kind}
        if self.value is not None:
            result["value"] = self.value
        if self.children:
            result["children"] = [child.as_json() for child in self.children]
        return result


class QueryParser:
    def __init__(self, text: str) -> None:
        self.tokens = self._tokens(text.strip())
        self.index = 0

    @staticmethod
    def _tokens(text: str) -> list[tuple[str, str]]:
        tokens: list[tuple[str, str]] = []
        position = 0
        while position < len(text):
            match = LEXER.match(text, position)
            if not match:
                raise ValueError(f"invalid query syntax at character {position}")
            kind = match.lastgroup
            if kind is None:
                raise ValueError("query lexer produced an empty token")
            tokens.append((kind, match.group(kind)))
            position = match.end()
        if not tokens:
            raise ValueError("query is empty")
        return tokens

    def peek(self, *kinds: str) -> bool:
        return self.index < len(self.tokens) and self.tokens[self.index][0] in kinds

    def take(self, kind: str | None = None) -> tuple[str, str]:
        if self.index >= len(self.tokens):
            raise ValueError("unexpected end of query")
        token = self.tokens[self.index]
        if kind is not None and token[0] != kind:
            raise ValueError(f"expected {kind}, found {token[0]}")
        self.index += 1
        return token

    def parse(self) -> Node:
        node = self.parse_or()
        if self.index != len(self.tokens):
            raise ValueError(f"unexpected token: {self.tokens[self.index][1]}")
        return node

    def parse_or(self) -> Node:
        children = [self.parse_and()]
        while self.peek("or"):
            self.take("or")
            children.append(self.parse_and())
        return children[0] if len(children) == 1 else Node("or", children=tuple(children))

    def parse_and(self) -> Node:
        children = [self.parse_unary()]
        while self.index < len(self.tokens) and not self.peek("or", "rparen"):
            if self.peek("and"):
                self.take("and")
            children.append(self.parse_unary())
        return children[0] if len(children) == 1 else Node("and", children=tuple(children))

    def parse_unary(self) -> Node:
        if self.peek("minus"):
            self.take("minus")
            return Node("not", children=(self.parse_unary(),))
        if self.peek("lparen"):
            self.take("lparen")
            child = self.parse_or()
            self.take("rparen")
            return Node("group", children=(child,))
        kind, value = self.take()
        if kind == "phrase":
            return Node("phrase", value=value[1:-1].lower())
        if kind == "exact":
            return Node("exact", value=value[3:].lower())
        if kind == "word":
            return Node("term", value=value.lower())
        raise ValueError(f"unexpected token: {value}")


def parse_query(text: str) -> Node:
    return QueryParser(text).parse()


def leaves(node: Node, negated: bool = False) -> Iterable[tuple[Node, bool]]:
    if node.kind == "not":
        yield from leaves(node.children[0], not negated)
    elif node.kind in {"term", "phrase", "exact"}:
        yield node, negated
    else:
        for child in node.children:
            yield from leaves(child, negated)


def matches(node: Node, terms: set[str], phrases: set[str], exact: set[str]) -> bool:
    if node.kind == "term":
        return node.value in terms
    if node.kind == "phrase":
        return node.value in phrases
    if node.kind == "exact":
        return node.value in exact
    if node.kind == "not":
        return not matches(node.children[0], terms, phrases, exact)
    if node.kind == "group":
        return matches(node.children[0], terms, phrases, exact)
    if node.kind == "and":
        return all(matches(child, terms, phrases, exact) for child in node.children)
    if node.kind == "or":
        return any(matches(child, terms, phrases, exact) for child in node.children)
    raise ValueError(f"unsupported AST node: {node.kind}")


def tokenize(text: str) -> list[str]:
    return [token.lower() for token in WORD.findall(text)]


def exact_identifiers(text: str) -> set[str]:
    return {token.lower() for token in WORD.findall(text) if "_" in token or any(c.isdigit() for c in token)}


def phrase_present(tokens: list[str], phrase: str) -> bool:
    expected = tokenize(phrase)
    if not expected:
        return False
    return any(tokens[index : index + len(expected)] == expected for index in range(len(tokens) - len(expected) + 1))


def load_inputs() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    fixture = json.loads(FIXTURES.read_text(encoding="utf-8"))
    queries = [
        json.loads(line)
        for line in QUERIES.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if fixture.get("schema_version") != 1:
        raise ValueError("unsupported Top-K fixture schema")
    if not fixture.get("documents") or not fixture.get("views"):
        raise ValueError("Top-K fixture requires documents and views")
    if not queries:
        raise ValueError("Top-K fixture requires at least one query")
    return fixture, queries


def reference_evaluate(fixture: dict[str, Any], query: dict[str, Any]) -> dict[str, Any]:
    ast = parse_query(query["query"])
    document_tokens = {document["id"]: tokenize(document["body"]) for document in fixture["documents"]}
    document_exact = {document["id"]: exact_identifiers(document["body"]) for document in fixture["documents"]}
    document_frequency: collections.Counter[str] = collections.Counter()
    for tokens in document_tokens.values():
        document_frequency.update(set(tokens))
    average_length = sum(map(len, document_tokens.values())) / len(document_tokens)
    all_terms = {leaf.value for leaf, _ in leaves(ast) if leaf.kind == "term"}
    score_terms = {leaf.value for leaf, negated in leaves(ast) if leaf.kind == "term" and not negated}
    phrases = {leaf.value for leaf, _ in leaves(ast) if leaf.kind == "phrase"}
    exacts = {leaf.value for leaf, _ in leaves(ast) if leaf.kind == "exact"}
    rerank = {(entry["query"], entry["view"]): entry["bonus"] for entry in fixture["rerank"]}
    views: list[dict[str, Any]] = []

    for view in fixture["views"]:
        if view["tenant"] != query["tenant"] or view["source"] != query["source"]:
            continue
        matched_terms: set[str] = set()
        matched_phrases: set[str] = set()
        matched_exact: set[str] = set()
        term_score: dict[str, float] = {}
        for component in view["components"]:
            document_id = component["document"]
            tokens = document_tokens[document_id]
            counts = collections.Counter(tokens)
            matched_terms.update(term for term in all_terms if counts[term] > 0)
            matched_phrases.update(phrase for phrase in phrases if phrase_present(tokens, phrase))
            matched_exact.update(exact for exact in exacts if exact in document_exact[document_id])
            for term in score_terms:
                tf = counts[term]
                if not tf:
                    continue
                idf = math.log(1 + (len(document_tokens) + 1) / (document_frequency[term] + 1))
                normalized = tf / (tf + 0.5 + 0.5 * (len(tokens) / average_length))
                contribution = component["role_weight"] * idf * normalized
                term_score[term] = max(term_score.get(term, 0.0), contribution)
        if not matches(ast, matched_terms, matched_phrases, matched_exact):
            continue
        lexical = sum(term_score.values()) + 1.5 * len(matched_phrases) + 2.0 * len(matched_exact)
        final_score = lexical + view["graph_bonus"] + rerank.get((query["id"], view["id"]), 0.0)
        upper_bound = lexical + view["graph_bonus"] + max(
            [entry["bonus"] for entry in fixture["rerank"] if entry["query"] == query["id"]] or [0.0]
        )
        if final_score > upper_bound + 1e-12:
            raise AssertionError("final score exceeded declared safe upper bound")
        views.append(
            {
                "view_id": view["id"],
                "external_hit_id": view["external_hit_id"],
                "score": final_score,
                "upper_bound": upper_bound,
            }
        )
    views.sort(key=lambda item: (-item["score"], item["external_hit_id"], item["view_id"]))
    return {"ast": ast, "results": views[:10], "matched_views": len(views)}


AST_FUNCTION = r"""
CREATE OR REPLACE FUNCTION prototype_ast_matches(
    node JSONB,
    matched_terms TEXT[],
    matched_phrases TEXT[],
    matched_exact TEXT[]
) RETURNS BOOLEAN
LANGUAGE plpgsql IMMUTABLE STRICT AS $$
DECLARE
    child JSONB;
BEGIN
    CASE node->>'type'
        WHEN 'term' THEN RETURN node->>'value' = ANY(matched_terms);
        WHEN 'phrase' THEN RETURN node->>'value' = ANY(matched_phrases);
        WHEN 'exact' THEN RETURN node->>'value' = ANY(matched_exact);
        WHEN 'not' THEN
            RETURN NOT prototype_ast_matches(node->'children'->0, matched_terms, matched_phrases, matched_exact);
        WHEN 'group' THEN
            RETURN prototype_ast_matches(node->'children'->0, matched_terms, matched_phrases, matched_exact);
        WHEN 'and' THEN
            FOR child IN SELECT value FROM jsonb_array_elements(node->'children') LOOP
                IF NOT prototype_ast_matches(child, matched_terms, matched_phrases, matched_exact) THEN
                    RETURN FALSE;
                END IF;
            END LOOP;
            RETURN TRUE;
        WHEN 'or' THEN
            FOR child IN SELECT value FROM jsonb_array_elements(node->'children') LOOP
                IF prototype_ast_matches(child, matched_terms, matched_phrases, matched_exact) THEN
                    RETURN TRUE;
                END IF;
            END LOOP;
            RETURN FALSE;
        ELSE RAISE EXCEPTION 'unsupported AST node type';
    END CASE;
END;
$$;
"""


PREPARED_QUERY = r"""
PREPARE prototype_topk(TEXT[], TEXT[], TEXT[], TEXT[], TEXT, TEXT, TEXT, JSONB) AS
WITH
scope_views AS (
    SELECT v.id AS view_id, v.graph_bonus, o.external_hit_id
    FROM prototype_view v
    JOIN prototype_occurrence o ON o.view_id = v.id
    WHERE o.tenant_id = $5 AND o.source_id = $6
),
scope_documents AS (
    SELECT DISTINCT vc.document_id
    FROM prototype_view_component vc
    JOIN scope_views sv ON sv.view_id = vc.view_id
),
term_match_rows AS (
    SELECT vc.view_id, p.term
    FROM prototype_view_component vc
    JOIN scope_views sv ON sv.view_id = vc.view_id
    JOIN prototype_posting p ON p.document_id = vc.document_id
    WHERE p.term = ANY($1)
),
term_matches AS (
    SELECT view_id, array_agg(DISTINCT term ORDER BY term) AS terms
    FROM term_match_rows GROUP BY view_id
),
term_score_rows AS (
    SELECT vc.view_id, p.term,
           vc.role_weight
             * LN(1 + (s.document_count + 1.0) / (p.document_frequency + 1.0))
             * p.term_frequency
             / (p.term_frequency + 0.5 + 0.5 * (d.token_count / s.average_length)) AS score
    FROM prototype_view_component vc
    JOIN scope_views sv ON sv.view_id = vc.view_id
    JOIN prototype_document d ON d.id = vc.document_id
    JOIN prototype_posting p ON p.document_id = d.id
    CROSS JOIN prototype_stats s
    WHERE p.term = ANY($2)
),
term_scores AS (
    SELECT view_id, term, MAX(score) AS score
    FROM term_score_rows GROUP BY view_id, term
),
lexical_scores AS (
    SELECT view_id, SUM(score) AS score FROM term_scores GROUP BY view_id
),
phrase_matches AS (
    SELECT vc.view_id, array_agg(DISTINCT phrase.phrase ORDER BY phrase.phrase) AS phrases
    FROM prototype_view_component vc
    JOIN scope_views sv ON sv.view_id = vc.view_id
    JOIN prototype_document d ON d.id = vc.document_id
    CROSS JOIN unnest($3) AS phrase(phrase)
    WHERE d.fts_simple @@ phraseto_tsquery('simple', phrase.phrase)
    GROUP BY vc.view_id
),
exact_matches AS (
    SELECT vc.view_id, array_agg(DISTINCT exact.value ORDER BY exact.value) AS exact_values
    FROM prototype_view_component vc
    JOIN scope_views sv ON sv.view_id = vc.view_id
    JOIN prototype_document d ON d.id = vc.document_id
    CROSS JOIN unnest($4) AS exact(value)
    WHERE exact.value = ANY(d.exact_identifiers)
    GROUP BY vc.view_id
),
matched AS (
    SELECT sv.view_id, sv.external_hit_id, sv.graph_bonus,
           COALESCE(tm.terms, ARRAY[]::TEXT[]) AS matched_terms,
           COALESCE(pm.phrases, ARRAY[]::TEXT[]) AS matched_phrases,
           COALESCE(em.exact_values, ARRAY[]::TEXT[]) AS matched_exact,
           COALESCE(ls.score, 0.0)
             + 1.5 * cardinality(COALESCE(pm.phrases, ARRAY[]::TEXT[]))
             + 2.0 * cardinality(COALESCE(em.exact_values, ARRAY[]::TEXT[])) AS lexical_score
    FROM scope_views sv
    LEFT JOIN term_matches tm ON tm.view_id = sv.view_id
    LEFT JOIN lexical_scores ls ON ls.view_id = sv.view_id
    LEFT JOIN phrase_matches pm ON pm.view_id = sv.view_id
    LEFT JOIN exact_matches em ON em.view_id = sv.view_id
),
fully_scored AS (
    SELECT m.view_id, m.external_hit_id,
           m.lexical_score + m.graph_bonus + COALESCE(r.bonus, 0.0) AS final_score
    FROM matched m
    LEFT JOIN prototype_rerank r ON r.query_id = $7 AND r.view_id = m.view_id
    WHERE prototype_ast_matches($8, m.matched_terms, m.matched_phrases, m.matched_exact)
),
top_results AS (
    SELECT * FROM fully_scored
    ORDER BY final_score DESC, external_hit_id ASC, view_id ASC
    LIMIT 10
)
SELECT json_build_object(
    'matched_postings', (SELECT COUNT(*) FROM term_match_rows),
    'fully_scored_search_documents', (SELECT COUNT(*) FROM scope_documents),
    'fully_scored_views', (SELECT COUNT(*) FROM fully_scored),
    'returned_shortlist', (SELECT COUNT(*) FROM top_results),
    'results', COALESCE(
        (SELECT json_agg(
            json_build_object('view_id', view_id, 'external_hit_id', external_hit_id, 'score', final_score)
            ORDER BY final_score DESC, external_hit_id ASC, view_id ASC
        ) FROM top_results),
        '[]'::JSON
    )
);
"""


def sql_array(values: Iterable[str]) -> str:
    items = list(values)
    if not items:
        return "ARRAY[]::TEXT[]"
    return "ARRAY[" + ",".join(sql_literal(value) for value in items) + "]::TEXT[]"


def setup_database(database: TemporaryPostgres, fixture: dict[str, Any]) -> None:
    database.sql(
        """
        CREATE TABLE prototype_document (
            id TEXT PRIMARY KEY,
            body TEXT NOT NULL,
            token_count DOUBLE PRECISION NOT NULL CHECK (token_count > 0),
            exact_identifiers TEXT[] NOT NULL,
            fts_simple TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', body)) STORED
        );
        CREATE INDEX prototype_document_fts ON prototype_document USING GIN (fts_simple);
        CREATE TABLE prototype_posting (
            document_id TEXT NOT NULL REFERENCES prototype_document(id),
            term TEXT NOT NULL,
            term_frequency DOUBLE PRECISION NOT NULL,
            document_frequency DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (document_id, term)
        );
        CREATE INDEX prototype_posting_term ON prototype_posting(term, document_id);
        CREATE TABLE prototype_view (id TEXT PRIMARY KEY, graph_bonus DOUBLE PRECISION NOT NULL);
        CREATE TABLE prototype_view_component (
            view_id TEXT NOT NULL REFERENCES prototype_view(id),
            ordinal INTEGER NOT NULL,
            document_id TEXT NOT NULL REFERENCES prototype_document(id),
            role_weight DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (view_id, ordinal)
        );
        CREATE TABLE prototype_occurrence (
            view_id TEXT PRIMARY KEY REFERENCES prototype_view(id),
            external_hit_id TEXT NOT NULL UNIQUE,
            tenant_id TEXT NOT NULL,
            source_id TEXT NOT NULL
        );
        CREATE INDEX prototype_occurrence_scope ON prototype_occurrence(tenant_id, source_id, view_id);
        CREATE TABLE prototype_rerank (
            query_id TEXT NOT NULL,
            view_id TEXT NOT NULL REFERENCES prototype_view(id),
            bonus DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (query_id, view_id)
        );
        CREATE TABLE prototype_stats (document_count DOUBLE PRECISION, average_length DOUBLE PRECISION);
        """
        + AST_FUNCTION
    )
    tokens_by_document = {document["id"]: tokenize(document["body"]) for document in fixture["documents"]}
    document_frequency: collections.Counter[str] = collections.Counter()
    for tokens in tokens_by_document.values():
        document_frequency.update(set(tokens))
    average_length = sum(map(len, tokens_by_document.values())) / len(tokens_by_document)
    document_values = []
    posting_values = []
    for document in fixture["documents"]:
        tokens = tokens_by_document[document["id"]]
        identifiers = sql_array(sorted(exact_identifiers(document["body"])))
        document_values.append(
            f"({sql_literal(document['id'])},{sql_literal(document['body'])},{len(tokens)},{identifiers})"
        )
        for term, frequency in sorted(collections.Counter(tokens).items()):
            posting_values.append(
                f"({sql_literal(document['id'])},{sql_literal(term)},{frequency},{document_frequency[term]})"
            )
    view_values = []
    component_values = []
    occurrence_values = []
    for view in fixture["views"]:
        view_values.append(f"({sql_literal(view['id'])},{view['graph_bonus']})")
        occurrence_values.append(
            f"({sql_literal(view['id'])},{sql_literal(view['external_hit_id'])},"
            f"{sql_literal(view['tenant'])},{sql_literal(view['source'])})"
        )
        for ordinal, component in enumerate(view["components"]):
            component_values.append(
                f"({sql_literal(view['id'])},{ordinal},{sql_literal(component['document'])},"
                f"{component['role_weight']})"
            )
    rerank_values = [
        f"({sql_literal(item['query'])},{sql_literal(item['view'])},{item['bonus']})"
        for item in fixture["rerank"]
    ]
    database.sql(
        "INSERT INTO prototype_document(id,body,token_count,exact_identifiers) VALUES "
        + ",".join(document_values)
        + "; INSERT INTO prototype_posting VALUES "
        + ",".join(posting_values)
        + "; INSERT INTO prototype_view VALUES "
        + ",".join(view_values)
        + "; INSERT INTO prototype_view_component VALUES "
        + ",".join(component_values)
        + "; INSERT INTO prototype_occurrence VALUES "
        + ",".join(occurrence_values)
        + "; INSERT INTO prototype_rerank VALUES "
        + ",".join(rerank_values)
        + f"; INSERT INTO prototype_stats VALUES ({len(tokens_by_document)},{average_length});"
        + " ANALYZE;"
    )


def query_arguments(query: dict[str, Any], ast: Node) -> str:
    all_terms = sorted({leaf.value for leaf, _ in leaves(ast) if leaf.kind == "term"})
    score_terms = sorted(
        {leaf.value for leaf, negated in leaves(ast) if leaf.kind == "term" and not negated}
    )
    phrases = sorted({leaf.value for leaf, _ in leaves(ast) if leaf.kind == "phrase"})
    exact = sorted({leaf.value for leaf, _ in leaves(ast) if leaf.kind == "exact"})
    return ",".join(
        [
            sql_array(all_terms),
            sql_array(score_terms),
            sql_array(phrases),
            sql_array(exact),
            sql_literal(query["tenant"]),
            sql_literal(query["source"]),
            sql_literal(query["id"]),
            sql_literal(json.dumps(ast.as_json(), separators=(",", ":"))) + "::JSONB",
        ]
    )


def run_query(
    session: PsqlSession,
    query: dict[str, Any],
    warmups: int,
    iterations: int,
) -> tuple[dict[str, Any], dict[str, Any], list[float], float]:
    ast = parse_query(query["query"])
    arguments = query_arguments(query, ast)
    execute = f"EXECUTE prototype_topk({arguments});"
    raw, cold_ms = session.sql(execute)
    result = json.loads(raw)
    for _ in range(warmups):
        session.sql(execute)
    warm: list[float] = []
    for _ in range(iterations):
        current, elapsed = session.sql(execute)
        if json.loads(current) != result:
            raise AssertionError(f"non-deterministic SQL result for {query['id']}")
        warm.append(elapsed)
    plan_raw, _ = session.sql(
        f"EXPLAIN (ANALYZE, BUFFERS, SETTINGS, FORMAT JSON) EXECUTE prototype_topk({arguments});"
    )
    plan = json.loads(plan_raw)[0]
    return result, plan, warm, cold_ms


def identity_list(results: list[dict[str, Any]]) -> list[str]:
    return [result["external_hit_id"] for result in results]


def ensure_public(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in {"token", "password", "secret", "hostname", "address"}:
                raise ValueError("prototype artifact contains a private field")
            ensure_public(child)
    elif isinstance(value, list):
        for child in value:
            ensure_public(child)
    elif isinstance(value, str) and Path(value).is_absolute():
        raise ValueError("prototype artifact contains a local path")


def run_prototype(
    fixture: dict[str, Any],
    queries: list[dict[str, Any]],
    commit_sha: str,
    warmups: int,
    iterations: int,
) -> dict[str, Any]:
    if warmups < 3 or iterations < 30:
        raise ValueError("prototype requires at least three warmups and 30 measured iterations")
    query_artifacts: list[dict[str, Any]] = []
    all_warm: list[float] = []
    all_cold: list[float] = []
    backend_version = ""
    backend_settings: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="mainrag-topk-prototype-") as temporary:
        with TemporaryPostgres(Path(temporary)) as database:
            backend_version = database.sql("SHOW server_version;")
            backend_settings = json.loads(
                database.sql(
                    "SELECT json_build_object("
                    "'work_mem', current_setting('work_mem'),"
                    "'shared_buffers', current_setting('shared_buffers'),"
                    "'effective_cache_size', current_setting('effective_cache_size'),"
                    "'random_page_cost', current_setting('random_page_cost'),"
                    "'jit', current_setting('jit')"
                    ");"
                )
            )
            setup_database(database, fixture)
            with database.session() as session:
                session.sql(PREPARED_QUERY)
                for query in queries:
                    reference = reference_evaluate(fixture, query)
                    sql_result, plan, warm, cold = run_query(
                        session, query, warmups=warmups, iterations=iterations
                    )
                    sql_ids = identity_list(sql_result["results"])
                    reference_ids = identity_list(reference["results"])
                    required_missing = sorted(set(query["required"]) - set(sql_ids))
                    forbidden_present = sorted(set(query["forbidden"]) & set(sql_ids))
                    status = "PASS"
                    errors: list[str] = []
                    if sql_ids != reference_ids:
                        status = "FAIL"
                        errors.append("SQL Top-10 differs from exhaustive reference")
                    if required_missing:
                        status = "FAIL"
                        errors.append(f"required hits missing: {required_missing}")
                    if forbidden_present:
                        status = "FAIL"
                        errors.append(f"forbidden scoped hits present: {forbidden_present}")
                    if sql_result["fully_scored_search_documents"] > 500:
                        status = "FAIL"
                        errors.append("fully scored search-document count exceeds 500")
                    warm_p95 = percentile(warm, 95)
                    if warm_p95 >= 200:
                        status = "FAIL"
                        errors.append("warm p95 exceeds 200 ms")
                    query_artifacts.append(
                        {
                            "id": query["id"],
                            "query_sha256": sha256_bytes(query["query"].encode()),
                            "ast": reference["ast"].as_json(),
                            "status": status,
                            "top_10": sql_ids,
                            "reference_top_10": reference_ids,
                            "matched_postings": sql_result["matched_postings"],
                            "fully_scored_search_documents": sql_result[
                                "fully_scored_search_documents"
                            ],
                            "fully_scored_views": sql_result["fully_scored_views"],
                            "returned_shortlist": sql_result["returned_shortlist"],
                            "fallback": "complete scoped view evaluation before graph and rerank",
                            "cold_first_ms": round(cold, 3),
                            "warm_latency": {
                                "samples": len(warm),
                                "p50_ms": round(percentile(warm, 50), 3),
                                "p95_ms": round(warm_p95, 3),
                                "p99_ms": round(percentile(warm, 99), 3),
                            },
                            "plan": plan,
                            "errors": errors,
                        }
                    )
                    all_warm.extend(warm)
                    all_cold.append(cold)
    identity = [
        {"id": artifact["id"], "top_10": artifact["top_10"]} for artifact in query_artifacts
    ]
    aggregate_status = "PASS" if all(item["status"] == "PASS" for item in query_artifacts) else "FAIL"
    return {
        "schema_version": "storage-v2-topk-prototype/v1",
        "status": aggregate_status,
        "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "commit_sha": commit_sha,
        "backend": {
            "name": "PostgreSQL native GIN",
            "version": backend_version,
            "extension": None,
            "settings": backend_settings,
        },
        "inputs": {
            "fixture_sha256": sha256_file(FIXTURES),
            "query_sha256": sha256_file(QUERIES),
            "documents": len(fixture["documents"]),
            "views": len(fixture["views"]),
            "queries": len(queries),
        },
        "execution": {
            "candidate_limit": None,
            "fully_scored_gate": 500,
            "result_limit": 10,
            "warmups_per_query": warmups,
            "iterations_per_query": iterations,
            "concurrency": 1,
            "fallback": "complete scoped view evaluation; no correctness path truncates candidates",
            "second_stage": "graph and fixture rerank bonuses applied to every Boolean-matched view",
        },
        "aggregate": {
            "result_identity_sha256": sha256_bytes(
                json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
            ),
            "max_fully_scored_search_documents": max(
                item["fully_scored_search_documents"] for item in query_artifacts
            ),
            "max_fully_scored_views": max(item["fully_scored_views"] for item in query_artifacts),
            "warm_latency": {
                "samples": len(all_warm),
                "p50_ms": round(percentile(all_warm, 50), 3),
                "p95_ms": round(percentile(all_warm, 95), 3),
                "p99_ms": round(percentile(all_warm, 99), 3),
            },
            "cold_first_latency": {
                "samples": len(all_cold),
                "p50_ms": round(percentile(all_cold, 50), 3),
                "p95_ms": round(percentile(all_cold, 95), 3),
                "p99_ms": round(percentile(all_cold, 99), 3),
            },
        },
        "queries": query_artifacts,
        "decision": {
            "status": "GO" if aggregate_status == "PASS" else "NO-GO",
            "selected_backend": "PostgreSQL native GIN" if aggregate_status == "PASS" else None,
            "reason": "Exact composed Top-10 and fixture performance gates passed without candidate truncation."
            if aggregate_status == "PASS"
            else "At least one correctness, isolation, or performance gate failed.",
            "rejected_for_now": [
                "Extension candidate: no extension is needed to prove the fixture contract; packaging and durability remain owned by backend qualification."
            ],
            "unresolved_operational_gates": [
                "Production-scale selectivity and latency",
                "PostgreSQL maintenance and resource headroom",
                "Crash, restore, and package reproducibility",
                "Safe pruning bounds that include future graph and learned rerank contributions",
                "Planner index choice on production-scale source scopes; the small fixture may choose sequential scans",
            ],
        },
        "cleanup": {"temporary_cluster_removed": True},
        "limitations": [
            "Synthetic fixture results are not production-scale evidence.",
            "The prototype uses complete scoped evaluation and proves no WAND/MaxScore speedup.",
            "No extension packaging, ABI, WAL, crash, restore, deployment, or activation claim is made.",
            "The fixture creates a native GIN index but does not require the planner to use it on tiny scoped relations.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commit-sha", default=git_output("rev-parse", "HEAD"))
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--iterations", type=int, default=30)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit_sha):
        parser.error("--commit-sha must be a full Git commit SHA")
    fixture, queries = load_inputs()
    artifact = run_prototype(fixture, queries, args.commit_sha, args.warmups, args.iterations)
    try:
        import jsonschema
    except ImportError as error:
        raise RuntimeError("jsonschema is required to validate the prototype artifact") from error
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    ).validate(artifact)
    ensure_public(artifact)
    serialized = json.dumps(artifact, indent=2, sort_keys=True) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(serialized, encoding="utf-8")
    temporary.replace(args.output)
    print(
        f"{artifact['status']}: {len(artifact['queries'])} queries, "
        f"identity={artifact['aggregate']['result_identity_sha256']}, "
        f"max_fully_scored_docs={artifact['aggregate']['max_fully_scored_search_documents']}, "
        f"warm_p95={artifact['aggregate']['warm_latency']['p95_ms']} ms"
    )
    return 0 if artifact["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
