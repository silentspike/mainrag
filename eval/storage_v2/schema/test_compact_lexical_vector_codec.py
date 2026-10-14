"""Pure block codec, exact vector semantics and expression-index correctness.

Always owns its disposable PostgreSQL cluster. No old application schema,
production socket, API, source job or Rust build is used. Stored-size output is
only diagnostic; a repeated dictionary is not proof of actual TOAST savings.
"""
from __future__ import annotations

from contextlib import ExitStack
import getpass
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

from eval.storage_v2.harness import TemporaryPostgres

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / 'migrations/171_storage_v2_compact_lexical_vector_codec.sql'
ENCODE = 'storage_v2_encode_lexical_vectors'
DECODE = 'storage_v2_decode_lexical_vectors'
LEXEMES = 'storage_v2_lexical_codec_exact_lexemes'
ROLES = ('mainrag', 'mainrag_v2_lexical_rank_owner', 'mainrag_v2_frontier_owner',
         'mainrag_v2_presence_owner')


def literal(value):
    return "'" + value.replace("'", "''") + "'"


def vector_term(term, positions=()):
    quoted = "'" + term.replace('\\', '\\\\').replace("'", "''") + "'"
    return quoted + (':' + ','.join(f'{position}{weight}' for position, weight in positions)
                     if positions else '')


def reference_frame(vectors):
    """Independent public MLV1 wire example, not the SQL encoder as gold."""
    terms = sorted({term for vector in vectors for term, _ in vector}, key=lambda s: s.encode())
    identifiers = {term: number + 1 for number, term in enumerate(terms)}
    parts = [b'MLV1', struct.pack('!II', len(terms), len(vectors))]
    for term in terms:
        encoded = term.encode()
        parts += [struct.pack('!H', len(encoded)), encoded]
    for vector in vectors:
        parts.append(struct.pack('!I', len(vector)))
        for term, positions in sorted(vector, key=lambda e: e[0].encode()):
            parts.append(struct.pack('!IH', identifiers[term], len(positions)))
            for position, weight in positions:
                parts.append(struct.pack('!H', position | {'D': 0, 'C': 1, 'B': 2, 'A': 3}[weight] << 14))
    return b''.join(parts)


class CompactLexicalVectorCodecTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for command in ('initdb', 'pg_ctl', 'psql'):
            if shutil.which(command) is None:
                raise unittest.SkipTest(f'owned codec cluster needs {command}')
        cls.stack = ExitStack()
        try:
            directory = cls.stack.enter_context(tempfile.TemporaryDirectory(prefix='mainrag-owned-vector-codec-'))
            postgres = cls.stack.enter_context(TemporaryPostgres(Path(directory)))
            cls.socket = postgres.socket
            cls.sql(';'.join(f'CREATE ROLE {role}' for role in ROLES))
            cls.sql(MIGRATION.read_text())
            # The same existing159 union expression is reproduced verbatim as a
            # synthetic reference; no predecessor database/schema is loaded.
            cls.sql('''CREATE FUNCTION fixture_native_exact_lexemes(p_vectors TSVECTOR[])
RETURNS TEXT[] LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
SET search_path=pg_catalog,public AS $fn$
 SELECT ARRAY(SELECT DISTINCT lexeme COLLATE "C"
  FROM unnest(p_vectors) vector
  CROSS JOIN LATERAL unnest(tsvector_to_array(vector)) lexeme ORDER BY 1)
$fn$;
ALTER FUNCTION fixture_native_exact_lexemes(TSVECTOR[]) OWNER TO mainrag_v2_lexical_rank_owner;
REVOKE ALL ON FUNCTION fixture_native_exact_lexemes(TSVECTOR[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fixture_native_exact_lexemes(TSVECTOR[]) TO mainrag_v2_frontier_owner,mainrag;
''')
        except BaseException:
            cls.stack.close()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.stack.close()

    @classmethod
    def command(cls, statement, *, check=True):
        result = subprocess.run(['psql', '-X', '--no-psqlrc', '-qAt', '-v', 'ON_ERROR_STOP=1',
            '--host', str(cls.socket), '--port', '5432', '--username', getpass.getuser(), '--dbname', 'postgres'],
            input="SET statement_timeout='15s';SET lock_timeout='1s';" + statement + ';',
            text=True, capture_output=True, cwd=ROOT, timeout=20)
        if check and result.returncode:
            raise AssertionError(result.stderr)
        return result

    @classmethod
    def sql(cls, statement):
        return cls.command(statement).stdout.strip()

    def assert_invalid(self, value, message):
        result = self.command(f"SELECT {DECODE}(decode('{value.hex()}','hex'))", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(message, result.stderr)

    @staticmethod
    def array(vectors):
        return 'ARRAY[' + ','.join(literal(vector) + '::TSVECTOR' for vector in vectors) + ']::TSVECTOR[]'

    def test_roundtrip_wire_hostile_lexemes_weights_positions_and_array_bounds(self):
        hostile = ("quote's\\tail", 'Ω日本語🙂', 'comma,:!&|() tab\t newline\n', 'empty_positions')
        fixture = [[('alpha', [(1, 'D'), (2, 'C'), (3, 'B'), (4, 'A'), (16383, 'D')]),
                    (hostile[0], [(7, 'A')]), (hostile[1], [(2, 'B')])],
                   [(hostile[2], []), (hostile[3], [])], [],
                   [('maxpositions', [(n, 'C') for n in range(1, 256)] + [(16383, 'A')]),
                    ('Ω' * 1023, [(16383, 'B')]), ('x' * 2046, [(16383, 'D')])]]
        vectors = [' '.join(vector_term(term, positions) for term, positions in v) for v in fixture]
        encoded = reference_frame(fixture)
        expression = self.array(vectors)
        self.assertEqual(self.sql(f"SELECT encode({ENCODE}({expression}),'hex')"), encoded.hex())
        # ASCII-only queries over a previously stored Unicode array isolate
        # session encoding from literal transport and from binary-send output.
        self.sql('CREATE TABLE fixture_codec_encoding(value TSVECTOR[] NOT NULL);'
                 f'INSERT INTO fixture_codec_encoding VALUES({expression})')
        utf8 = self.sql(f"SET client_encoding='UTF8';SELECT encode({ENCODE}(value),'hex') FROM fixture_codec_encoding")
        latin1 = self.sql(f"SET client_encoding='LATIN1';SELECT encode({ENCODE}(value),'hex') "
                         "FROM fixture_codec_encoding;SET client_encoding='UTF8'")
        self.assertEqual(utf8, latin1)
        self.assertEqual(latin1, encoded.hex())
        self.assertEqual(self.sql(f"SET default_text_search_config='english';SELECT {DECODE}({ENCODE}({expression}))={expression}"), 't')
        self.assertEqual(self.sql(f'''WITH original AS (SELECT * FROM unnest({expression}) WITH ORDINALITY v(value,n)),
actual AS (SELECT * FROM unnest({DECODE}({ENCODE}({expression}))) WITH ORDINALITY v(value,n))
SELECT bool_and(tsvectorsend(o.value)=tsvectorsend(a.value)) FROM original o JOIN actual a USING(n)'''), 't')
        self.assertEqual(self.sql(f"SELECT {DECODE}({ENCODE}(ARRAY[]::TSVECTOR[]))=ARRAY[]::TSVECTOR[]"), 't')
        self.assertEqual(self.sql(f"SELECT {DECODE}({ENCODE}(ARRAY[strip({literal(vectors[0])}::TSVECTOR)]))"
                                 f"=ARRAY[strip({literal(vectors[0])}::TSVECTOR)]"), 't')
        self.assertEqual(self.sql(f'SELECT {ENCODE}(NULL::TSVECTOR[]) IS NULL AND {DECODE}(NULL::BYTEA) IS NULL'), 't')
        result = self.command(f"SELECT {ENCODE}(array_fill('a'::TSVECTOR,ARRAY[65]))", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('bounded nonnull one-based', result.stderr)
        result = self.command(f"SELECT {ENCODE}('[0:0]={{a}}'::TSVECTOR[])", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('one-based', result.stderr)
        result = self.command(f"SELECT {ENCODE}(ARRAY[ARRAY['a'::TSVECTOR]])", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('one-based vector array', result.stderr)
        result = self.command(f"SELECT {ENCODE}(ARRAY[NULL::TSVECTOR])", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('nonnull vector elements', result.stderr)

    def test_malformed_frames_fail_closed_before_normalization(self):
        valid = reference_frame([[('a', [(1, 'D'), (2, 'A')])]])
        # One literal frame fixes offsets independently of the implementation.
        self.assert_invalid(b'', 'invalid header')
        self.assert_invalid(b'BAD!'+valid[4:], 'invalid header')
        self.assert_invalid(valid[:10], 'invalid header')
        self.assert_invalid(valid[:-1], 'invalid position count')
        self.assert_invalid(valid+b'\x00', 'trailing payload')
        changed = bytearray(valid);changed[12:14] = struct.pack('!H', 2048)
        self.assert_invalid(bytes(changed), 'dictionary length')
        # Full2047-byte frames isolate the native PG18 byte limit from a
        # truncated-length failure. Both ASCII and multibyte cases fail before
        # the decoder reaches the vector type-input cast.
        self.assert_invalid(reference_frame([[('x' * 2047, [(1, 'D')])]]), 'dictionary length')
        self.assert_invalid(reference_frame([[('Ω' * 1023 + 'x', [(1, 'D')])]]), 'dictionary length')
        changed = bytearray(valid);changed[14] = 0xff
        self.assert_invalid(bytes(changed), 'UTF8')
        changed = bytearray(valid);changed[14] = 0
        self.assert_invalid(bytes(changed), '0x00')
        changed = bytearray(valid);changed[19:23] = struct.pack('!I', 2)
        self.assert_invalid(bytes(changed), 'dictionary reference')
        changed = bytearray(valid);changed[23:25] = struct.pack('!H', 257)
        self.assert_invalid(bytes(changed), 'position count')
        changed = bytearray(valid);changed[25:27] = b'\x00\x00'
        self.assert_invalid(bytes(changed), 'position order')
        changed = bytearray(valid);changed[27:29] = struct.pack('!H', 49153)
        self.assert_invalid(bytes(changed), 'position order')
        changed = bytearray(valid);changed[4:8] = b'\xff\xff\xff\xff'
        self.assert_invalid(bytes(changed), 'integer overflow')
        self.assert_invalid(b'MLV1'+struct.pack('!II', 1, 1)+struct.pack('!H', 1)+b'a'+struct.pack('!I', 0),
                            'unused dictionary')
        duplicate = b'MLV1'+struct.pack('!II', 2, 1)+struct.pack('!H', 1)+b'a'+struct.pack('!H', 1)+b'a'+struct.pack('!I', 0)
        self.assert_invalid(duplicate, 'unique byte order')
        count = bytearray(valid);count[8:12] = struct.pack('!I', 65)
        self.assert_invalid(bytes(count), 'vector count')

    def test_original_window_queries_ranks_fingerprints_and_gin_expression(self):
        vectors = ["'alpha':1A 'titleproof':3B 'text':4C", "'beta':1A 'text':2C"]
        vectors += ["'alphabeta':1A 'text':2C"] * 61
        vectors += ["'alpha':1A 'beta':2A 'late':3A 'titleproof':4B"]
        expression = self.array(vectors)
        self.sql('CREATE TABLE fixture_codec_payload(id INTEGER PRIMARY KEY,original TSVECTOR[] NOT NULL,payload BYTEA NOT NULL);'
                 f'INSERT INTO fixture_codec_payload VALUES(1,{expression},{ENCODE}({expression}));'
                 "INSERT INTO fixture_codec_payload VALUES(2,ARRAY['unknownother'::TSVECTOR],"
                 f"{ENCODE}(ARRAY['unknownother'::TSVECTOR]));"
                 'ALTER TABLE fixture_codec_payload OWNER TO mainrag_v2_frontier_owner;'
                 'CREATE INDEX fixture_codec_gin ON fixture_codec_payload USING GIN'
                 f'(fixture_native_exact_lexemes({DECODE}(payload)));')
        queries = ['alpha', 'alpha & beta', 'alpha <-> beta', 'titleproof', 'missingunknown', 'alphabeta']
        for query in queries:
            q = literal(query)
            self.assertEqual(self.sql(f'''WITH original AS (
 SELECT ordinal,value FROM fixture_codec_payload p,
 unnest(p.original) WITH ORDINALITY v(value,ordinal) WHERE p.id=1
), actual AS (
 SELECT ordinal,value FROM fixture_codec_payload p,
 unnest({DECODE}(p.payload)) WITH ORDINALITY v(value,ordinal) WHERE p.id=1
)
SELECT bool_and((o.value@@{q}::TSQUERY)=(a.value@@{q}::TSQUERY)
 AND ts_rank_cd(o.value,{q}::TSQUERY)=ts_rank_cd(a.value,{q}::TSQUERY))
FROM original o JOIN actual a USING(ordinal)'''), 't')
        self.assertEqual(self.sql(f"SELECT min(ordinal) FROM fixture_codec_payload p,"
            f"unnest({DECODE}(p.payload)) WITH ORDINALITY v(value,ordinal) "
            "WHERE p.id=1 AND value@@'alpha & beta'::TSQUERY"), '64')
        self.assertEqual(self.sql(f'SELECT fixture_native_exact_lexemes(original)={LEXEMES}(payload) '
                                 'FROM fixture_codec_payload WHERE id=1'), 't')
        # Same 16-bit fingerprint procedure over complete original/decoded
        # lexemes; both are necessary filters, not replacements for exact match.
        self.assertEqual(self.sql(f'''SELECT ARRAY(SELECT DISTINCT get_byte(sha256(convert_to(t,'UTF8')),0)*256
 +get_byte(sha256(convert_to(t,'UTF8')),1) FROM unnest(fixture_native_exact_lexemes(original)) t ORDER BY 1)
=ARRAY(SELECT DISTINCT get_byte(sha256(convert_to(t,'UTF8')),0)*256
 +get_byte(sha256(convert_to(t,'UTF8')),1) FROM unnest({LEXEMES}(payload)) t ORDER BY 1)
FROM fixture_codec_payload WHERE id=1'''), 't')
        self.assertEqual(self.sql(f"SET enable_seqscan=off;SELECT id FROM fixture_codec_payload WHERE "
            f"fixture_native_exact_lexemes({DECODE}(payload)) @>ARRAY['titleproof','alpha']"), '1')
        plan = json.loads(self.sql(f"SET enable_seqscan=off;EXPLAIN(FORMAT JSON) SELECT id FROM fixture_codec_payload WHERE "
            f"fixture_native_exact_lexemes({DECODE}(payload)) @>ARRAY['titleproof','alpha']"))
        self.assertIn('fixture_codec_gin', json.dumps(plan))

    def test_immutable_invoker_role_contract_and_stored_size_diagnostics(self):
        contract = json.loads(self.sql("SELECT jsonb_agg(jsonb_build_object('name',proname,'owner',"
            "pg_get_userbyid(proowner),'immutable',provolatile='i','strict',proisstrict,'parallel_safe',proparallel='s',"
            "'invoker',NOT prosecdef,'not_leakproof',NOT proleakproof,'config',proconfig,"
            "'public_execute',EXISTS(SELECT 1 FROM aclexplode(proacl) a WHERE a.grantee=0))) "
            "FROM pg_proc WHERE proname IN ('storage_v2_lexical_codec_u32','storage_v2_encode_lexical_vectors',"
            "'storage_v2_decode_lexical_vectors','storage_v2_lexical_codec_exact_lexemes')"))
        self.assertEqual(len(contract), 4)
        for function in contract:
            self.assertEqual(function['owner'], 'mainrag_v2_lexical_rank_owner')
            for flag in ('immutable', 'strict', 'parallel_safe', 'invoker', 'not_leakproof'):
                self.assertTrue(function[flag], function)
            self.assertFalse(function['public_execute'])
            self.assertEqual(function['config'], ['search_path=pg_catalog, public'])
        self.assertEqual(self.sql(f"SET ROLE mainrag;SELECT {DECODE}({ENCODE}(ARRAY['a:1D'::TSVECTOR]))="
                                 "ARRAY['a:1D'::TSVECTOR]"), 't')
        self.sql('CREATE TABLE fixture_codec_costs(original TSVECTOR[],payload BYTEA);'
                 'ALTER TABLE fixture_codec_costs ALTER COLUMN original SET COMPRESSION lz4;'
                 'ALTER TABLE fixture_codec_costs ALTER COLUMN payload SET COMPRESSION lz4;'
                 f"INSERT INTO fixture_codec_costs SELECT value,{ENCODE}(value) FROM ("
                 "SELECT array_agg(('repeatedshared:1A,2B,3C,4D '||'unique'||n||':5A')::TSVECTOR) value "
                 "FROM generate_series(1,64) n) fixture;")
        costs = json.loads(self.sql('SELECT jsonb_build_object(\'original_stored_bytes\',pg_column_size(original),'
            "'codec_stored_bytes',pg_column_size(payload),'physical_savings_proven',FALSE) FROM fixture_codec_costs"))
        self.assertGreater(costs['original_stored_bytes'], 0)
        self.assertGreater(costs['codec_stored_bytes'], 0)
        self.assertFalse(costs['physical_savings_proven'])
        # No assumption that block-local dictionary encoding beats the existing
        # whole-array TOAST compressor; admission must compare actual storage.


if __name__ == '__main__':
    unittest.main()
