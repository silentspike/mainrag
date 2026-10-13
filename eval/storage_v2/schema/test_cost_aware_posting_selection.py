"""One actual producer-format gate, with bounded public synthetic documents.

Stored compressed column sizes are measured; no net physical-saving claim is
made. No fixture staging tables or production connection are introduced.
"""
from __future__ import annotations

import hashlib
import json
import unittest

from eval.storage_v2.schema import test_retained_posting_conversion as previous
from eval.storage_v2.schema import test_bounded_lexical_verification as bounded

ROOT = previous.ROOT
MIGRATION = ROOT / 'migrations/167_storage_v2_cost_aware_posting_selection.sql'
SIGNATURE = 'storage_v2_put_search_document(text,text,bigint,text,text[])'


class CostAwarePostingSelectionTests(unittest.TestCase):
    base = previous.RetainedPostingConversionTests.base
    schema = previous.RetainedPostingConversionTests.schema
    command = classmethod(previous.RetainedPostingConversionTests.command.__func__)
    sql = classmethod(previous.RetainedPostingConversionTests.sql.__func__)
    file = classmethod(previous.RetainedPostingConversionTests.file.__func__)
    actor = staticmethod(previous.RetainedPostingConversionTests.actor)
    admin = classmethod(previous.RetainedPostingConversionTests.admin.__func__)
    quote = staticmethod(previous.RetainedPostingConversionTests.quote)
    make_projection = bounded.BoundedLexicalVerificationTests.make_projection
    begin = previous.RetainedPostingConversionTests.begin
    stage = previous.RetainedPostingConversionTests.stage
    complete_analysis = previous.RetainedPostingConversionTests.complete_analysis
    commit = previous.RetainedPostingConversionTests.commit
    fixture = previous.RetainedPostingConversionTests.fixture
    rank = previous.RetainedPostingConversionTests.rank
    envelope = previous.RetainedPostingConversionTests.envelope
    readers = previous.RetainedPostingConversionTests.readers
    assert_sql_fails = previous.RetainedPostingConversionTests.assert_sql_fails

    @classmethod
    def setUpClass(cls):
        previous.RetainedPostingConversionTests.setUpClass.__func__(cls)
        try:
            cls.sql(f"SET app.user_id='{cls.schema.ADMIN_ID}';"+
                    next(MIGRATION.parent.glob('165_*.sql')).read_text())
            # Two disposable reference copies differ ONLY in the old selector.
            # They share the original tokenizer/identifier/locator/seal logic.
            definition = cls.sql(f"SELECT pg_get_functiondef('{SIGNATURE}'::REGPROCEDURE)")
            selector = 'IF octet_length(p_search_text)>=262144 THEN'
            if definition.count(selector) != 1:
                raise AssertionError('one original format selector required')
            for label, choice in (('compact','FALSE'),('byte','TRUE')):
                name = 'fixture_pre_cost_'+label
                reference = definition.replace('FUNCTION public.storage_v2_put_search_document(',
                                               'FUNCTION public.'+name+'(',1).replace(selector,'IF '+choice+' THEN',1)
                cls.sql(reference)
                cls.sql(f'ALTER FUNCTION {name}(TEXT,TEXT,BIGINT,TEXT,TEXT[]) OWNER TO mainrag;'
                        f'REVOKE ALL ON FUNCTION {name}(TEXT,TEXT,BIGINT,TEXT,TEXT[]) FROM PUBLIC;'
                        f'GRANT EXECUTE ON FUNCTION {name}(TEXT,TEXT,BIGINT,TEXT,TEXT[]) TO mainrag;')
            cls.before_authority = cls.sql("SELECT jsonb_build_object('oid',oid,'owner',proowner,'acl',proacl::TEXT,"
                "'configuration',proconfig,'security_definer',prosecdef,'arguments',proargnames,'volatility',provolatile,"
                f"'parallel',proparallel) FROM pg_proc WHERE oid='{SIGNATURE}'::REGPROCEDURE")
            cls.before_contracts = cls.sql("SELECT coalesce(jsonb_agg(to_jsonb(c) ORDER BY signature),'[]'::JSONB) "
                "FROM storage_v2_posting_conversion_contract c WHERE signature<>"+cls.quote(SIGNATURE))
            cls.before_catalogs = cls.sql("SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM storage_v2_posting_conversion_catalog_contract c")
            cls.sql(f"SET app.user_id='{cls.schema.ADMIN_ID}';"+MIGRATION.read_text())
        except BaseException:
            cls.base.tearDownClass.__func__(cls)
            raise

    @classmethod
    def tearDownClass(cls):
        cls.base.tearDownClass.__func__(cls)

    def put(self, name, profile, node, text):
        return int(self.sql(self.admin(f'SELECT id FROM {name}({self.quote(profile)},\'node\','
                                     f'{node},{self.quote(text)},ARRAY[]::TEXT[])')))

    def column_cost(self, document):
        return json.loads(self.sql("SELECT jsonb_build_object('compact',"
            "(SELECT coalesce(sum(16+pg_column_size(terms)+pg_column_size(term_frequencies)+pg_column_size(fingerprints)),0) "
            f"FROM storage_v2_compact_posting_block WHERE document_id={document}),'byte',"
            "(SELECT coalesce(sum(18+pg_column_size(cached_terms)+pg_column_size(text_byte_starts)+pg_column_size(text_byte_lengths)"
            "+pg_column_size(term_frequencies)+pg_column_size(fingerprints)),0) "
            f"FROM storage_v2_byte_posting_block WHERE document_id={document}))"))

    def posting_pairs(self, document):
        return self.sql(self.admin("SELECT coalesce(jsonb_agg(jsonb_build_array(term,frequency) ORDER BY term COLLATE \"C\"),"
            "'[]'::JSONB) FROM storage_v2_complete_document_posting_blocks("+str(document)+") block "
            "CROSS JOIN LATERAL unnest(block.terms,block.term_frequencies) item(term,frequency)"))

    def test_adaptive_formats_preserve_exact_search_authority_seal_and_reuse(self):
        prefix = 'alpha beta alpha key_7 Ωμέγα9 source/branch 日本語9 — '
        short_words = ' '.join(f'token_{n:04d}' for n in range(512))
        random_terms = [hashlib.sha256(f'long-{n}'.encode()).hexdigest()+
                        hashlib.sha256(f'tail-{n}'.encode()).hexdigest() for n in range(3072)]
        repetitive = ['repeated_dictionary_prefix_'+'a'*80+f'_{n:04d}' for n in range(3072)]
        cases = (
            ('small',prefix,'COMPACT'),
            ('large-short-heavy',prefix+(short_words+' ')*64,'COMPACT'),
            ('large-long-terms',prefix+' '.join(random_terms),'BYTE'),
            ('large-compressible-dictionary',prefix+' '.join(repetitive),'COMPACT'),
        )
        measurements = []
        for label,text,expected_format in cases:
            with self.subTest(case=label):
                if label!='small':
                    self.assertGreaterEqual(len(text.encode()),262144)
                occurrence,artifact = self.fixture(text)
                row = json.loads(self.sql("SELECT jsonb_build_object('document',d.id,'node',d.node_id,'source',o.source_id,"
                    "'generation',(SELECT r.generation_id FROM storage_v2_ingest_run_item item "
                    "JOIN storage_v2_ingest_run r ON r.id=item.run_id WHERE item.occurrence_id=o.id LIMIT 1)) "
                    "FROM occurrence o JOIN storage_v2_search_view_document binding ON binding.view_id=o.view_id AND binding.ordinal=0 "
                    f'JOIN storage_v2_search_document d ON d.id=binding.document_id WHERE o.id={occurrence}'))
                doc = row['document']
                compact = self.put('fixture_pre_cost_compact','fixture.cost.compact.v1',row['node'],text)
                byte = self.put('fixture_pre_cost_byte','fixture.cost.byte.v1',row['node'],text)
                pairs = self.posting_pairs(doc)
                self.assertEqual(pairs,self.posting_pairs(compact));self.assertEqual(pairs,self.posting_pairs(byte))
                invariant = "jsonb_build_object('text',search_text,'token_count',token_count,'exact',exact_identifiers,"
                invariant += "'derived',exact_identifiers_derived,'word_ids',storage_v2_document_word_identifiers(id))"
                current = self.sql(self.admin(f'SELECT {invariant} FROM storage_v2_search_document WHERE id={doc}'))
                for reference in (compact,byte):
                    self.assertEqual(current,self.sql(self.admin(f'SELECT {invariant} FROM storage_v2_search_document WHERE id={reference}')))
                for identity in (doc,compact,byte):
                    self.assertEqual(self.sql(f"SELECT d.materialization_sha256=storage_v2_hash_parts('mainrag.search-document.v1',"
                        "ARRAY[convert_to(d.profile_id,'UTF8'),convert_to('node','UTF8'),n.node_digest,convert_to(d.search_text,'UTF8'),"
                        "convert_to('','UTF8')]) AND s.materialization_sha256=d.materialization_sha256 "
                        "FROM storage_v2_search_document d JOIN content_node n ON n.id=d.node_id "
                        f'JOIN storage_v2_document_postings_seal s ON s.document_id=d.id WHERE d.id={identity}'),'t')
                actual_format = self.sql(f"SELECT CASE WHEN EXISTS(SELECT 1 FROM storage_v2_byte_posting_block WHERE document_id={doc}) "
                                         "THEN 'BYTE' ELSE 'COMPACT' END")
                self.assertEqual(actual_format,expected_format)
                terms = ['alpha','source/branch','key_7','ωμέγα9','日本語9',random_terms[0] if expected_format=='BYTE' else 'token_0000']
                before = self.readers(doc,terms)
                self.assertEqual(before,self.readers(compact,terms));self.assertEqual(before,self.readers(byte,terms))
                # One native prefix suffices for actual positive/hidden search;
                # the complete body is stored unchanged through real graph nodes.
                segment = prefix.rstrip()
                self.sql(self.admin(f'SELECT storage_v2_put_lexical_segments_located({occurrence},{artifact},'
                    f'ARRAY[0]::BIGINT[],ARRAY[{self.quote(segment)}],ARRAY[\'titleproof\'],ARRAY[\'text\'],'
                    'ARRAY[1]::BIGINT[],ARRAY[1]::BIGINT[])'))
                self.sql(self.admin(f"SELECT storage_v2_verify_generation({row['generation']},'{ 'e7'*32}')"))
                self.assertTrue(json.loads(self.rank(occurrence,'alpha')))
                self.assertEqual(self.rank(occurrence,'alpha',actor=self.schema.OTHER_ID),'[]')
                self.assertGreater(json.loads(self.envelope(row['source'],{'type':'term','value':'alpha'}))['total'],0)
                self.assertEqual(self.sql(self.actor(self.schema.OTHER_ID,
                    f"SELECT storage_v2_can_access_source({row['source']})")),'f')
                document_before = self.sql(f'SELECT to_jsonb(d) FROM storage_v2_search_document d WHERE id={doc}')
                self.assertEqual(self.put('storage_v2_put_search_document','mainrag.lexical-simple.v1',row['node'],text),doc)
                self.assertEqual(document_before,self.sql(f'SELECT to_jsonb(d) FROM storage_v2_search_document d WHERE id={doc}'))
                self.assertEqual(before,self.readers(doc,terms))
                if label=='small':
                    self.assert_sql_fails(self.actor(self.schema.OTHER_ID,
                        'SELECT storage_v2_put_search_document(\'mainrag.lexical-simple.v1\',\'node\','+
                        str(row['node'])+','+self.quote(text)+',ARRAY[]::TEXT[])'),'administrator authority')
                cost_current = self.column_cost(doc);cost_compact = self.column_cost(compact)['compact'];cost_byte = self.column_cost(byte)['byte']
                # Stored datum comparisons are meaningful for these fixtures,
                # including compression, but exclude shared index/page/TOAST
                # chunk overhead and do not prove universal or net savings.
                if expected_format=='BYTE':self.assertLess(cost_byte,cost_compact)
                if label=='large-short-heavy':
                    self.assertLess(cost_compact,cost_byte)
                # A repetitive dictionary deliberately stays COMPACT when
                # the model is uncertain; record both measured costs without
                # asserting that this fallback is physically optimal.
                measurements.append(dict(case=label,selected=actual_format,
                    selected_column_bytes=cost_current,compact_column_bytes=cost_compact,byte_column_bytes=cost_byte,
                    physical_net_saving_proven=False))
        authority = self.sql("SELECT jsonb_build_object('oid',oid,'owner',proowner,'acl',proacl::TEXT,"
            "'configuration',proconfig,'security_definer',prosecdef,'arguments',proargnames,'volatility',provolatile,"
            f"'parallel',proparallel) FROM pg_proc WHERE oid='{SIGNATURE}'::REGPROCEDURE")
        self.assertEqual(authority,self.before_authority)
        self.assertEqual(self.before_contracts,self.sql("SELECT coalesce(jsonb_agg(to_jsonb(c) ORDER BY signature),'[]'::JSONB) "
            "FROM storage_v2_posting_conversion_contract c WHERE signature<>"+self.quote(SIGNATURE)))
        self.assertEqual(self.before_catalogs,self.sql("SELECT jsonb_agg(to_jsonb(c) ORDER BY relation_oid) FROM storage_v2_posting_conversion_catalog_contract c"))
        self.sql(f"SET app.user_id='{self.schema.ADMIN_ID}';SELECT storage_v2_posting_conversion_require_operator();"
                 'SELECT storage_v2_require_lexical_conversion_operator();')
        print(json.dumps({'fixture_stored_column_costs':measurements},sort_keys=True))
