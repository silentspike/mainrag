"""Focused correctness tests for the composed-view Top-K prototype."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


EVAL_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(EVAL_ROOT))

from storage_v2.topk.prototype import (  # noqa: E402
    PREPARED_QUERY,
    Node,
    load_inputs,
    matches,
    parse_query,
    reference_evaluate,
)


class ParserTests(unittest.TestCase):
    def test_precedence_and_grouping(self) -> None:
        ast = parse_query("alpha OR beta AND -gamma")
        self.assertEqual(ast.kind, "or")
        self.assertEqual(ast.children[1].kind, "and")
        self.assertEqual(ast.children[1].children[1].kind, "not")

        grouped = parse_query("(alpha OR beta) AND gamma")
        self.assertEqual(grouped.kind, "and")
        self.assertEqual(grouped.children[0].kind, "group")

    def test_phrase_and_exact_identifier(self) -> None:
        ast = parse_query('"atomic pointer" id:active_generation_id')
        self.assertEqual(ast.kind, "and")
        self.assertEqual(ast.children[0], Node("phrase", value="atomic pointer"))
        self.assertEqual(ast.children[1], Node("exact", value="active_generation_id"))

    def test_invalid_query_fails(self) -> None:
        with self.assertRaises(ValueError):
            parse_query("(unclosed OR query")


class BooleanTests(unittest.TestCase):
    def test_every_ast_node_has_positive_and_adversarial_behavior(self) -> None:
        cases = [
            ("alpha beta", {"alpha", "beta"}, True),
            ("alpha beta", {"alpha"}, False),
            ("alpha OR beta", {"beta"}, True),
            ("alpha -beta", {"alpha"}, True),
            ("alpha -beta", {"alpha", "beta"}, False),
            ('"alpha beta"', set(), True),
            ("id:alpha_beta", set(), True),
        ]
        for query, terms, expected in cases:
            ast = parse_query(query)
            phrases = {"alpha beta"} if ast.kind == "phrase" else set()
            exact = {"alpha_beta"} if ast.kind == "exact" else set()
            self.assertEqual(matches(ast, terms, phrases, exact), expected, query)


class ReferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture, cls.queries = load_inputs()
        cls.by_id = {query["id"]: query for query in cls.queries}

    def test_distributed_terms_match_one_composed_view(self) -> None:
        result = reference_evaluate(self.fixture, self.by_id["distributed-terms"])
        self.assertEqual([item["external_hit_id"] for item in result["results"]], ["hit-distributed"])

    def test_tenant_and_source_isolation_precede_exposure(self) -> None:
        result = reference_evaluate(self.fixture, self.by_id["tenant-isolation"])
        identities = [item["external_hit_id"] for item in result["results"]]
        self.assertIn("hit-auth-a", identities)
        self.assertNotIn("hit-auth-b", identities)
        self.assertNotIn("hit-other-source", identities)

    def test_every_query_meets_required_and_forbidden_fixture_contract(self) -> None:
        for query in self.queries:
            result = reference_evaluate(self.fixture, query)
            identities = {item["external_hit_id"] for item in result["results"]}
            self.assertTrue(set(query["required"]) <= identities, query["id"])
            self.assertFalse(set(query["forbidden"]) & identities, query["id"])

    def test_safe_upper_bound_covers_every_final_score(self) -> None:
        for query in self.queries:
            result = reference_evaluate(self.fixture, query)
            for item in result["results"]:
                self.assertLessEqual(item["score"], item["upper_bound"] + 1e-12)


class SqlContractTests(unittest.TestCase):
    def test_query_is_prepared_and_has_no_correctness_cap(self) -> None:
        self.assertIn("PREPARE prototype_topk", PREPARED_QUERY)
        self.assertIn("LIMIT 10", PREPARED_QUERY)
        self.assertNotIn("LIMIT 500", PREPARED_QUERY)
        self.assertIn("prototype_ast_matches($8", PREPARED_QUERY)

    def test_scope_filter_precedes_scoring(self) -> None:
        scope = PREPARED_QUERY.index("scope_views AS")
        scoring = PREPARED_QUERY.index("term_score_rows AS")
        self.assertLess(scope, scoring)
        self.assertIn("WHERE o.tenant_id = $5 AND o.source_id = $6", PREPARED_QUERY)


if __name__ == "__main__":
    unittest.main()
