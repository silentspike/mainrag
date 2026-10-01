"""Complete constructor identity and postings before/after token reuse."""
import json
import unittest

from eval.storage_v2.schema import test_compact_exact_postings as base

MIGRATION = base.schema.ROOT / "migrations/118_storage_v2_reuse_constructor_tokens.sql"
SIGNATURE = "storage_v2_put_search_document(text,text,bigint,text,text[])"


class ConstructorTokenReuseTests(unittest.TestCase):
    schema = base.schema
    command = classmethod(base.CompactExactPostingTests.command.__func__)
    sql = classmethod(base.CompactExactPostingTests.sql.__func__)
    file = classmethod(base.CompactExactPostingTests.file.__func__)
    admin = classmethod(base.CompactExactPostingTests.admin.__func__)
    actor = staticmethod(base.CompactExactPostingTests.actor)
    make_projection = base.CompactExactPostingTests.make_projection
    quote = staticmethod(base.CompactExactPostingTests.quote)
    assert_sql_fails = base.CompactExactPostingTests.assert_sql_fails
    start_client = base.CompactExactPostingTests.start_client
    wait_for_client = base.CompactExactPostingTests.wait_for_client
    constructor_postings = base.CompactExactPostingTests.constructor_postings

    @classmethod
    def setUpClass(cls):
        try:
            base.CompactExactPostingTests.setUpClass.__func__(cls)
            cls.file(base.MIGRATION)
        except BaseException:
            if hasattr(cls, "stack"):
                cls.stack.close()
            raise

    @classmethod
    def tearDownClass(cls):
        base.CompactExactPostingTests.tearDownClass.__func__(cls)

    def test_complete_documents_blocks_replay_and_collision(self):
        texts = ["", " \t\n ", "--- !!!", "alpha alpha beta.gamma a/b key_identifier",
                 "ÄÖÜ Straße Καλημέρα 日本語 аБВ", "alpha\u00a0beta\u2003gamma",
                 "résumé re\u0301sume\u0301", "_ a__b 0123 -12.3 +42%",
                 '{"key":"value","uri":"https://example.invalid/a-b?q=1"}',
                 "word\r\nword\tword", "quote' bracket[] arrow-> api::call()",
                 "x" * 70000, " ".join(f"word_{i}" for i in range(257)),
                 " ".join(f"word_{i}" for i in range(12000)),
                 ("common alpha beta a/b api::call()\n" * 33000)]
        # Canonical components remain within the inline-body bound. Search
        # projections can independently contain much larger extracted text.
        components = [self.make_projection(f"constructor fixture component {i}")[0]
                      for i in range(len(texts))]

        def snapshots():
            values = []
            for node, text in zip(components, texts):
                # Rollback permits identical profile/component identities in
                # both constructors; no profile-dependent hash is normalized.
                value = self.sql("BEGIN; " + self.admin(f"""
SELECT id FROM storage_v2_put_search_document(
 'constructor-token-reuse-fixture','node',{node},{self.quote(text)},
 ARRAY[' Key_Identifier ','key_identifier','',' OTHER ']);
RESET ROLE;
SELECT jsonb_build_object('document',to_jsonb(inserted)-'id'-'created_at',
 'blocks',(SELECT coalesce(jsonb_agg(jsonb_build_object(
   'order',block_order,'terms',terms,'frequencies',term_frequencies,
   'fingerprints',fingerprints) ORDER BY block_order),'[]')
  FROM storage_v2_compact_posting_block WHERE document_id=inserted.id),
 'flat_count',(SELECT count(*) FROM storage_v2_search_posting WHERE document_id=inserted.id))
 FROM storage_v2_search_document inserted
 WHERE profile_id='constructor-token-reuse-fixture' AND node_id={node}; ROLLBACK;
"""))
                values.append(json.loads(value.splitlines()[-1]))
            return values

        before = snapshots()
        rows_before = self.sql("SELECT count(*) FROM storage_v2_search_document; "
                               "SELECT count(*) FROM storage_v2_compact_posting_block")
        self.file(MIGRATION)
        self.file(MIGRATION)
        self.assertEqual(rows_before, self.sql("SELECT count(*) FROM storage_v2_search_document; "
                                              "SELECT count(*) FROM storage_v2_compact_posting_block"))
        self.assertEqual(before, snapshots())

        node, text = components[3], texts[3]
        call = ("SELECT id FROM storage_v2_put_search_document("
                f"'constructor-idempotency-fixture','node',{node},{self.quote(text)},ARRAY['key_identifier'])")
        identity = self.sql(self.admin(call))
        self.assertEqual(identity, self.sql(self.admin(call)))
        self.assert_sql_fails(self.admin(call.replace(self.quote(text), "'different text'")),
                              "search-document profile collision")
        self.assert_sql_fails("SET ROLE mainrag; SET app.user_id='" + base.schema.OTHER_ID + "'; "
                              + call, "search-document writes require administrator authority")
        candidate = self.sql(f"SELECT pg_get_functiondef('{SIGNATURE}'::regprocedure)")
        body = MIGRATION.read_text().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
        for change, expected in (
            (candidate.replace("v_normalized_text :=", "/* fixture drift */ v_normalized_text :=", 1)
             + ";", "search constructor identity differs"),
            (f"ALTER FUNCTION {SIGNATURE} OWNER TO storage_v2_shadow_worker;",
             "search constructor authority differs"),
        ):
            value = self.command("--command", "BEGIN; " + change + body + "ROLLBACK;", check=False)
            self.assertNotEqual(value.returncode, 0)
            self.assertIn(expected, value.stderr)
        print("complete constructor, FTS, materialization and compact-block comparisons:", len(texts), flush=True)
        # Both component kinds must retain the actual uncommitted-winner
        # readback, including competing text and identifier collisions.
        base.SearchDocumentReuseTests.test_concurrent_insert_reuses_identity_and_rejects_conflicting_materializations(self)
