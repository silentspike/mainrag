"""Canonical digest compatibility across the large-array aggregation path."""
from __future__ import annotations

import hashlib

from eval.storage_v2.schema import test_content_graph_schema as graph


MIGRATION = graph.ROOT / 'migrations/048_storage_v2_linear_hash_parts.sql'
ATTRIBUTES = "SELECT json_build_array(pg_get_userbyid(proowner),proacl,provolatile,proisstrict," \
             "prosecdef,proparallel,proconfig,procost) FROM pg_proc WHERE oid=" \
             "'storage_v2_hash_parts(text,bytea[])'::regprocedure"


def canonical_digest(domain: str, parts: list[bytes]) -> str:
    encoded_domain = domain.encode('utf-8')
    result = hashlib.sha256()
    result.update(len(encoded_domain).to_bytes(8, 'big') + encoded_domain)
    result.update(len(parts).to_bytes(8, 'big'))
    for part in parts:
        result.update(len(part).to_bytes(8, 'big'))
        result.update(part)
    return result.hexdigest()


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def array_expression(parts: list[bytes]) -> str:
    return 'ARRAY[' + ','.join(f"decode('{part.hex()}','hex')" for part in parts) + ']::BYTEA[]'


class LinearHashPartsTests(graph.ContentGraphSchemaTests):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # The graph suite deliberately replays 031. Preserve that exact old
        # implementation under a fixture-only name for differential checks.
        previous = cls.sql("SELECT pg_get_functiondef('storage_v2_hash_parts(text,bytea[])'::regprocedure)")
        cls.previous_definition = previous
        cls.previous_attributes = cls.sql(ATTRIBUTES)
        cls.sql(previous.replace('FUNCTION public.storage_v2_hash_parts(',
                                 'FUNCTION public.fixture_previous_hash_parts(', 1))

    def setUp(self):
        self.file(MIGRATION)

    def compare(self, domain: str, parts: list[bytes], expression: str | None = None):
        argument = expression if expression is not None else array_expression(parts)
        expected = canonical_digest(domain, parts)
        for name in ('fixture_previous_hash_parts', 'storage_v2_hash_parts'):
            with self.subTest(function=name, count=len(parts), domain=domain):
                self.assertEqual(self.sql(f"SELECT encode({name}({literal(domain)},{argument}),'hex')"),
                                 expected)
        return expected

    def test_small_and_large_arrays_match_previous_and_independent_wire_format(self):
        for count in (0, 1, 2, 63, 64, 65, 257):
            parts = [b'' if n % 7 == 0 else b'\x00\xff' + hashlib.sha256(str(n).encode()).digest()
                     for n in range(count)]
            for domain in ('', 'mainrag.compat.v1', "synthetic-\u03a9-\u732b-'domain"):
                self.compare(domain, parts)
        self.assertEqual(self.compare('mainrag.compat.v1', [b'ab', b'c']),
                         '9ed0431a7ac7cebd650bb97fbaa8adbc53c9899f35f77c353a4dc474ffd98bbd')
        part = b'\x00\xff' * 4096
        self.compare('synthetic-large-part', [part] * 65,
                     f"array_fill(decode('{part.hex()}','hex'),ARRAY[65])")

    def test_dimensions_lower_bounds_and_storage_order_are_unchanged(self):
        rows = [[hashlib.sha256(str(n).encode()).digest() for n in range(start, start + 40)]
                for start in (0, 40)]
        expression = 'ARRAY[' + ','.join(array_expression(row) for row in rows) + ']'
        self.compare('synthetic-matrix', rows[0] + rows[1], expression)
        self.compare('synthetic-bounds', [b'\xff'] * 72,
                     "array_fill(decode('ff','hex'),ARRAY[9,8],ARRAY[-2,4])")
        self.compare('synthetic-empty', [], "array_fill(decode('ff','hex'),ARRAY[0],ARRAY[-2])")

    def test_order_domain_lengths_and_part_count_remain_authoritative(self):
        suffix = [hashlib.sha256(str(n).encode()).digest() for n in range(63)]
        variants = [('synthetic-framing', [b'ab', b'c'] + suffix),
                    ('synthetic-framing', [b'a', b'bc'] + suffix),
                    ('synthetic-framing', [b'c', b'ab'] + suffix),
                    ('synthetic-other-domain', [b'ab', b'c'] + suffix),
                    ('synthetic-framing', [b'ab', b'c', b''] + suffix)]
        self.assertEqual(len({self.compare(domain, parts) for domain, parts in variants}), len(variants))

    def test_null_arguments_remain_strict_and_null_elements_remain_errors(self):
        for function in ('fixture_previous_hash_parts', 'storage_v2_hash_parts'):
            for expression in (f"{function}(NULL,ARRAY[NULL]::BYTEA[])",
                               f"{function}('synthetic',NULL)"):
                self.assertEqual(self.sql(f'SELECT {expression} IS NULL'), 't')
            for count in (1, 64, 65, 1000):
                for ordinal in (1, count):
                    query = f"SELECT {function}('synthetic',ARRAY(SELECT CASE WHEN n={ordinal} " \
                            f"THEN NULL ELSE decode('aa','hex') END FROM generate_series(1,{count}) n))"
                    result = self.command('--set=VERBOSITY=verbose', '--command', query, check=False)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('P0001: canonical digest parts cannot be null', result.stderr)

    def test_migration_replay_retains_owner_acl_and_function_attributes(self):
        self.assertEqual(self.sql(ATTRIBUTES), self.previous_attributes)
        for _ in range(2):
            self.file(MIGRATION)
            self.assertEqual(self.sql(ATTRIBUTES), self.previous_attributes)
        self.assertEqual(self.sql("SELECT encode(storage_v2_hash_parts('synthetic',ARRAY[]::BYTEA[]),'hex')"),
                         canonical_digest('synthetic', []))

    def test_large_generation_root_fits_unchanged_statement_budget(self):
        count = 130908
        parts = [hashlib.sha256(str(n).encode()).digest() for n in range(1, count + 1)]
        expected = canonical_digest('mainrag.generation-root.v1', parts)
        actual = self.sql("SET statement_timeout='30s'; WITH input AS MATERIALIZED ("
                          "SELECT array_agg(digest(n::TEXT,'sha256') ORDER BY n) parts "
                          f"FROM generate_series(1,{count}) n) "
                          "SELECT encode(storage_v2_hash_parts('mainrag.generation-root.v1',parts),'hex') "
                          "FROM input")
        self.assertEqual(actual, expected)
