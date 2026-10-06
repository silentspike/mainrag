"""Exact visible-page parity without changing immutable verification criteria."""
from __future__ import annotations

import json
import re
import subprocess
import unittest
from unittest.mock import patch

from eval.storage_v2.schema import test_posting_compaction as compaction


MIGRATION = compaction.PostingCompactionTests.schema.ROOT / 'migrations/158_storage_v2_seek_lexical_verification_pages.sql'


class SeekLexicalVerificationPageTests(unittest.TestCase):
    schema = compaction.PostingCompactionTests.schema
    command = classmethod(compaction.PostingCompactionTests.command.__func__)
    sql = classmethod(compaction.PostingCompactionTests.sql.__func__)
    file = classmethod(compaction.PostingCompactionTests.file.__func__)
    actor = staticmethod(compaction.PostingCompactionTests.actor)
    admin = classmethod(compaction.PostingCompactionTests.admin.__func__)
    quote = staticmethod(compaction.PostingCompactionTests.quote)
    make_projection = compaction.PostingCompactionTests.make_projection
    begin = compaction.PostingCompactionTests.begin
    complete_analysis = compaction.PostingCompactionTests.complete_analysis
    commit = compaction.PostingCompactionTests.commit
    assert_sql_fails = compaction.PostingCompactionTests.assert_sql_fails

    @classmethod
    def setUpClass(cls):
        compaction.PostingCompactionTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        compaction.PostingCompactionTests.tearDownClass.__func__(cls)

    def page(self, generation, after=0, limit=64, *, old=False, user=None):
        function = 'fixture_pre_seek_page' if old else 'storage_v2_verify_lexical_segment_page'
        actor = self.schema.ADMIN_ID if user is None else user
        return json.loads(self.sql(self.actor(actor,
            f'SELECT {function}({generation},{after},{limit})')))

    def stage_items(self, run, source, node, view, digest, text, *, old_item=False, count=64):
        self.sql(self.admin(f"""DO $fixture$ DECLARE n INTEGER; BEGIN
            FOR n IN {0 if old_item else 1}..{count} LOOP
                PERFORM storage_v2_stage_shadow_item({run},'item-'||n,'document',
                    'synthetic-item',jsonb_build_object('item','item-'||n),
                    'fixture-adapter-v1',{node},NULL,'{digest}',
                    {len(text.encode())},decode('{digest}','hex'),'fixture-analysis-v1',
                    {view},'/synthetic/item-'||n,'{{"byte_start":0}}'::JSONB);
            END LOOP;
        END $fixture$;"""))
        self.commit(run, count + int(old_item))
        generation = int(self.sql(f'SELECT generation_id FROM storage_v2_ingest_run WHERE id={run}'))
        self.sql(self.admin(f"SELECT storage_v2_verify_generation({generation},repeat('e',64))"))
        self.sql(self.admin(f"""DO $fixture$ DECLARE item RECORD; BEGIN
            FOR item IN SELECT id,artifact_version_id FROM occurrence WHERE source_id={source}
                AND NOT EXISTS (SELECT 1 FROM storage_v2_lexical_segment segment
                                 WHERE segment.occurrence_id=occurrence.id) LOOP
                PERFORM storage_v2_put_lexical_segments_at(item.id,item.artifact_version_id,
                    ARRAY[0]::BIGINT[],ARRAY[{self.quote(text)}],ARRAY['context β'],
                    ARRAY['text'],ARRAY[1]::BIGINT[]);
            END LOOP;
        END $fixture$;"""))
        return generation

    def test_visible_pages_authority_criteria_and_resume_parity(self):
        text = 'alpha β gamma'
        node, view, digest = self.make_projection(text)
        document = int(self.sql(self.admin('SELECT id FROM storage_v2_put_search_document('
            f"'mainrag.lexical-simple.v1','node',{node},{self.quote(text)},ARRAY[]::TEXT[])")))
        self.sql(self.admin(f'SELECT storage_v2_bind_search_document({view},0,{document},1.0)'))
        self.complete_analysis(digest)
        first = self.begin(6, 'a1' * 32, 'a2' * 32)
        historical = self.stage_items(first, 6, node, view, digest, text, old_item=True)
        foreign = self.begin(2, 'a3' * 32, 'a4' * 32)
        foreign_generation = self.stage_items(foreign, 2, node, view, digest, text, count=1)
        second = self.begin(6, 'a5' * 32, 'a6' * 32)
        current = self.stage_items(second, 6, node, view, digest, text)
        old_item = int(self.sql("SELECT id FROM occurrence WHERE source_id=6 AND source_path='/synthetic/item-0'"))
        last = int(self.sql('SELECT max(id) FROM occurrence WHERE source_id=6'))
        self.assertEqual(self.sql(f'SELECT count(*) FROM generation_item_version WHERE source_id=6 AND valid_to_seq IS NOT NULL'), '1')
        definition = self.sql("SELECT pg_get_functiondef('storage_v2_verify_lexical_segment_page(bigint,bigint,integer)'::REGPROCEDURE)")
        metadata_sql = "SELECT jsonb_build_object('owner',proowner,'acl',proacl,'config',proconfig,'definer',prosecdef,'volatility',provolatile) FROM pg_proc WHERE oid='storage_v2_verify_lexical_segment_page(bigint,bigint,integer)'::REGPROCEDURE"
        metadata = self.sql(metadata_sql)
        wrapper_sql = "SELECT pg_get_functiondef('storage_v2_verify_lexical_segments(bigint)'::REGPROCEDURE)"
        wrapper = self.sql(wrapper_sql)
        identity_sql = "SELECT jsonb_build_object('generations',(SELECT jsonb_agg(to_jsonb(g) ORDER BY id) FROM source_generation g),'memberships',(SELECT jsonb_agg(to_jsonb(m) ORDER BY source_id,source_item_id,valid_from_seq) FROM generation_item_version m),'documents',(SELECT jsonb_agg(to_jsonb(d) ORDER BY id) FROM storage_v2_search_document d))"
        identities = self.sql(identity_sql)
        # The predecessor stays available only in this owned disposable database.
        self.sql(definition.replace('FUNCTION public.storage_v2_verify_lexical_segment_page(',
                                    'FUNCTION public.fixture_pre_seek_page('))
        self.sql('ALTER FUNCTION fixture_pre_seek_page(BIGINT,BIGINT,INTEGER) OWNER TO mainrag_v2_frontier_owner; REVOKE ALL ON FUNCTION fixture_pre_seek_page(BIGINT,BIGINT,INTEGER) FROM PUBLIC; GRANT EXECUTE ON FUNCTION fixture_pre_seek_page(BIGINT,BIGINT,INTEGER) TO mainrag;')
        body = MIGRATION.read_text().replace('BEGIN;', '', 1).rsplit('COMMIT;', 1)[0]
        self.assert_sql_fails('BEGIN; ALTER FUNCTION storage_v2_verify_lexical_segment_page(BIGINT,BIGINT,INTEGER) OWNER TO mainrag;' + body, 'predecessor or authority differs')
        self.assert_sql_fails('BEGIN; GRANT EXECUTE ON FUNCTION storage_v2_verify_lexical_segment_page(BIGINT,BIGINT,INTEGER) TO PUBLIC;' + body, 'predecessor or authority differs')
        self.assert_sql_fails('BEGIN; ALTER TABLE generation_item_version DROP CONSTRAINT generation_item_version_source_id_source_item_id_int8range_excl;' + body, 'validated lexical page membership')
        self.assert_sql_fails('BEGIN; ALTER TABLE generation_item_version DROP CONSTRAINT generation_item_version_artifact_version_id_source_item_i_fkey1;' + body, 'validated lexical page membership')
        integrity = compaction.OP.load_module('seek_integrity_observer_fixture',
            MIGRATION.parent.parent / 'ops/storage-v2/integrity_resume.py')
        real_run = subprocess.run

        def observe_fixture(command, **options):
            # Replace the production subprocess before it can run. Execute its
            # actual read-only SQL only in this fixture's database and socket.
            self.assertEqual(command[:4], ['sudo', '-n', '-u', 'postgres'])
            self.assertEqual(command[command.index('-d') + 1], 'mainrag')
            statement = command[command.index('-c') + 1]
            self.assertTrue(statement.startswith('BEGIN READ ONLY;'))
            return real_run(['psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1',
                '--host', str(self.socket), '--dbname', self.database, '-c', statement],
                capture_output=True, text=True, timeout=options['timeout'])

        def observe():
            with patch.object(integrity.subprocess, 'run', side_effect=observe_fixture):
                return integrity.observe_immutable_identity(6, current)

        predecessor_observation = observe()
        self.assertIsNone(predecessor_observation['lexical_traversal_admission'])
        self.assertEqual(predecessor_observation['function_identities'][integrity.LEXICAL_PAGE_SIGNATURE],
            '084ebd93de9f702ac96400313ecd1bdce284ce57bf8b837ad51ba8f6a6f075ce')
        self.file(MIGRATION)
        migration = MIGRATION.read_text()
        marker = re.search(r'marker TEXT := \$old\$(.*?)\$old\$;', migration, re.S).group(1)
        selection = re.search(r'selection TEXT := \$new\$(.*?)\$new\$;', migration, re.S).group(1)
        replaced = self.sql("SELECT pg_get_functiondef('storage_v2_verify_lexical_segment_page(bigint,bigint,integer)'::REGPROCEDURE)")
        self.assertEqual(definition.count(marker), 1)
        self.assertEqual(replaced, definition.replace(marker, selection))
        self.assertEqual(definition.replace(marker, ''), replaced.replace(selection, ''))
        self.assertEqual(metadata, self.sql(metadata_sql))
        self.assertEqual(wrapper, self.sql(wrapper_sql))
        self.assertEqual(identities, self.sql(identity_sql))
        observed = observe()
        self.assertEqual(observed['identity']['generation_id'], current)
        self.assertEqual(observed['identity']['source_id'], 6)
        self.assertEqual(observed['identity'], predecessor_observation['identity'])
        current_hash = '0f8d7439612da0047dc76a6747e9ff0d5c0e74845864a1e04a07686b0f58cea4'
        self.assertEqual(observed['function_identities'][integrity.LEXICAL_PAGE_SIGNATURE], current_hash)
        self.assertEqual(observed['lexical_traversal_admission'], {
            'schema_version': 'mainrag.storage-v2.lexical-page-traversal-admission.v1',
            'predecessor_sha256': '084ebd93de9f702ac96400313ecd1bdce284ce57bf8b837ad51ba8f6a6f075ce',
            'current_sha256': current_hash,
            'authority_and_multiplicity_validated': True,
        })
        # A matching function body alone cannot admit a widened authority.
        self.sql('GRANT EXECUTE ON FUNCTION storage_v2_verify_lexical_segment_page(BIGINT,BIGINT,INTEGER) TO PUBLIC')
        try:
            self.assertIsNone(observe()['lexical_traversal_admission'])
        finally:
            self.sql('REVOKE EXECUTE ON FUNCTION storage_v2_verify_lexical_segment_page(BIGINT,BIGINT,INTEGER) FROM PUBLIC')
        self.assertEqual(metadata, self.sql(metadata_sql))
        for generation in (historical, current, foreign_generation):
            for cursor, limit in ((0,64),(old_item,64),(last-1,1),(last,64),(last+1000,64)):
                with self.subTest(generation=generation,cursor=cursor,limit=limit):
                    self.assertEqual(self.page(generation,cursor,limit,old=True),
                                     self.page(generation,cursor,limit))
        first_page = self.page(current)
        self.assertEqual(first_page['occurrence_count'],64)
        self.assertEqual(first_page['segment_count'],64)
        self.assertFalse(first_page['complete'])
        self.assertEqual(first_page['last_occurrence_id'],last)
        eof = self.page(current,first_page['last_occurrence_id'])
        self.assertEqual((eof['occurrence_count'],eof['segment_count'],eof['complete']), (0,0,True))
        # A retained predecessor page can continue with the exact same cursor.
        partial = self.page(current,0,1,old=True)
        self.assertEqual(self.page(current,partial['last_occurrence_id'],64),
                         self.page(current,partial['last_occurrence_id'],64,old=True))
        for old in (True,False):
            name = 'fixture_pre_seek_page' if old else 'storage_v2_verify_lexical_segment_page'
            self.assert_sql_fails(self.actor(self.schema.OTHER_ID,
                f'SELECT {name}({current},0,64)'), 'verified authorized generation required')
            self.assert_sql_fails(self.actor(self.schema.WRITER_ID,
                f'SELECT {name}({foreign_generation},0,64)'), 'verified authorized generation required')
            self.assert_sql_fails(self.admin(f'SELECT {name}({current},-1,64)'), 'bounded lexical page cursor')
            self.assert_sql_fails('BEGIN; ALTER TABLE storage_v2_lexical_segment DISABLE TRIGGER USER; '
                f"UPDATE storage_v2_lexical_segment SET text_sha256=sha256(convert_to('bad','UTF8')) WHERE occurrence_id={last};"
                + self.admin(f'SELECT {name}({current},{last-1},64)'), 'lexical segment projection is incomplete')
        print('PASS: exact historical/current/foreign page and cursor parity, terminal EOF, immutable criteria, authority and constraint fences',flush=True)


if __name__ == '__main__':
    unittest.main()
