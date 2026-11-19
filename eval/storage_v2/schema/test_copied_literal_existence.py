"""Exercise the copied rank optimization against the unchanged evidence oracle."""
from eval.storage_v2.schema import test_copied_fragment_body_recall as fragment

MIGRATION = fragment.ROOT / "migrations/184_storage_v2_copied_literal_existence.sql"


class CopiedLiteralExistenceTests(fragment.CopiedFragmentBodyRecallTests):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls.sql(f"SET app.user_id='{cls.schema.ADMIN_ID}';" + MIGRATION.read_text())
        except BaseException:
            cls.base.tearDownClass.__func__(cls)
            raise

    def test_only_rank_bodies_change_and_word_boundaries_match_count(self):
        after = self.function_hashes()
        self.assertEqual(set(after), set(self.after183))
        self.assertEqual({name for name in after if after[name] != self.after183[name]},
                         set(fragment.RANKS))
        self.assertEqual(self.authority183, self.authority_catalog())
        cases = [
            ('word', 'WORD'), ('sword', 'word'), ('word_tail', 'word'),
            ('word\nWORD', 'word'), ('"word": value', 'word'), ('word-tail', 'word'),
            ('éword', 'word'), ('élan!', 'ÉLAN'), ('3141592', '314159'),
            ('a 314159.b', '314159'), ('_', '_'), ('', 'word'),
        ]
        rows = ','.join(f"({self.quote(body)},{self.quote(term)})" for body, term in cases)
        for signature in fragment.RANKS:
            definition = self.sql(f"SELECT pg_get_functiondef('{signature}'::REGPROCEDURE)")
            expression = next(line.split('THEN (', 1)[1]
                              for line in definition.splitlines()
                              if 'THEN (CASE WHEN lower(p_query)' in line)
            self.assertTrue(expression.endswith(' END'))
            expression = expression.replace('literal_document.search_text', 'body')
            self.assertEqual(self.sql(
                f"SELECT count(*) FROM (VALUES {rows}) input(body,p_query) "
                f"WHERE ({expression}) IS DISTINCT FROM "
                "(storage_v2_literal_term_count(body,p_query)>0)"), '0')
