"""Execute the four production keyword SQL shapes on an owned PostgreSQL fixture."""
import json
import os
import re
import subprocess
import tempfile
import unittest
import uuid
from contextlib import ExitStack
from pathlib import Path

from eval.storage_v2.harness import TemporaryPostgres

ROOT = Path(__file__).resolve().parents[3]


class KeywordTieOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stack = ExitStack()
        cls.addClassCleanup(cls.stack.close)
        socket = os.environ.get('STORAGE_V2_TEST_SOCKET')
        if not socket:
            root = cls.stack.enter_context(tempfile.TemporaryDirectory(prefix='mainrag-keyword-ties-'))
            socket = cls.stack.enter_context(TemporaryPostgres(Path(root))).socket
        cls.socket = str(socket)
        cls.database = 'keyword_ties_' + uuid.uuid4().hex
        subprocess.run(['createdb', '-h', cls.socket, cls.database], check=True, capture_output=True)
        cls.stack.callback(subprocess.run, ['dropdb', '-h', cls.socket, cls.database], check=True,
                           capture_output=True)
        cls.sql('''CREATE TABLE sources(id BIGINT, user_id BIGINT, name TEXT);
            CREATE TABLE files(id BIGINT, source_id BIGINT, path TEXT, language TEXT);
            CREATE TABLE chunks(id BIGINT, file_id BIGINT, content_text TEXT,
                start_line INT, end_line INT, context_prefix TEXT, fts_vector TSVECTOR);
            INSERT INTO sources VALUES (1,11,'fixture-one'),(2,22,'fixture-two');
            INSERT INTO files VALUES (1,1,'fixture-one.txt','text'),(2,2,'fixture-two.txt','text');''')

    @classmethod
    def sql(cls, statement):
        return subprocess.run(['psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1', '-h', cls.socket,
                               '-d', cls.database, '-c', statement], check=True,
                              capture_output=True, text=True).stdout.strip()

    def statements(self, phrase=False):
        text = (ROOT / 'api/src/services/search.rs').read_text()
        body = text.split('pub async fn keyword_search(', 1)[1].split('let total = rows.len();', 1)[0]
        base = re.search(r'r#"(.*?)"#', body, re.S).group(1)
        builder = text.split('fn build_tsquery_sql(', 1)[1].split('\n}', 1)[0]
        templates = re.findall(r'format!\("([^"\n]+)"', builder)
        self.assertEqual(len(templates), 2, 'Use the production phrase and websearch constructors')
        query = templates[0 if phrase else 1].replace('{}', '$1')
        base = base.replace('{tsquery}', query)
        tails = re.findall(r'"(\{\}[^"\n]*ORDER BY[^"\n]*)"', body)
        self.assertEqual(len(tails), 4, 'Every tenant/source SQL branch must be exercised')
        return [tail.replace('{}', base) for tail in tails]

    def test_order_is_total_for_each_tenant_source_branch_and_heap_order(self):
        # More ties than the limit also exercise deterministic boundary membership.
        for phrase in (False, True):
            for direction in ('ASC', 'DESC'):
                self.sql(f'''TRUNCATE chunks;
                    INSERT INTO chunks SELECT n, CASE WHEN n=99 THEN 2 ELSE 1 END,
                        'alpha beta',1,1,NULL,to_tsvector('simple','alpha beta')
                        FROM generate_series(1,99) n ORDER BY n {direction};''')
                arguments = ("'alpha beta',11,1,10", "'alpha beta',11,10",
                             "'alpha beta',1,10", "'alpha beta',10")
                for branch, (statement, values) in enumerate(zip(self.statements(phrase), arguments)):
                    with self.subTest(phrase=phrase, heap=direction, branch=branch):
                        result = json.loads(self.sql(
                            f'PREPARE fixture AS SELECT json_agg(result) FROM ({statement}) result; '
                            f'EXECUTE fixture({values});'))
                        self.assertEqual([row['chunk_id'] for row in result], list(range(1,11)))
                        self.assertEqual(len({row['score'] for row in result}), 1)

    def test_score_precedence_and_existing_tenant_source_filters_are_preserved(self):
        self.sql('''TRUNCATE chunks;
            INSERT INTO chunks VALUES
              (1,1,'alpha',1,1,NULL,to_tsvector('simple','alpha')),
              (2,1,'alpha alpha',1,1,NULL,to_tsvector('simple','alpha alpha')),
              (3,2,'alpha alpha alpha',1,1,NULL,to_tsvector('simple','alpha alpha alpha'));''')
        arguments = ("'alpha',11,1,10", "'alpha',11,10", "'alpha',1,10", "'alpha',10")
        for branch, (statement, values) in enumerate(zip(self.statements(), arguments)):
            with self.subTest(branch=branch):
                result = json.loads(self.sql(
                    f'PREPARE fixture AS SELECT json_agg(result) FROM ({statement}) result; '
                    f'EXECUTE fixture({values});'))
                self.assertEqual([row['chunk_id'] for row in result], [3,2,1] if branch == 3 else [2,1])
        # An explicit source selector must not escape the agent's tenant filter.
        denied = self.sql(f'PREPARE fixture AS SELECT count(*) FROM ({self.statements()[0]}) result; '
                          "EXECUTE fixture('alpha',11,2,10);")
        self.assertEqual(denied, '0')


if __name__ == '__main__':
    unittest.main()
