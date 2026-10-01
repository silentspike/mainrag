"""Ownership provenance, nullable confidence and active source isolation."""
import json
from eval.storage_v2.schema import test_active_symbol_identity as base
from eval.storage_v2.schema import test_active_set_search as parent


class ActiveOwnershipTests(base.ActiveSymbolIdentityTests):
    def extra_intelligence_tests(self,digest):
        super().extra_intelligence_tests(digest)
        migration=parent.ROOT/'migrations/113_storage_v2_bounded_ownership.sql'
        self.command(self.database,file=migration)
        for source in (1,2):
            symbol=int(self.sql(f"SELECT id FROM storage_v2_symbol WHERE source_id={source} AND symbol_key='fixture-002'"))
            entity=int(self.sql(self.admin(f"SELECT id FROM storage_v2_put_intelligence_entity({source},'root',NULL,'Container','type','{{}}')")))
            target=int(self.sql(self.admin(f"SELECT id FROM storage_v2_put_intelligence_entity({source},'target',{symbol},'Child','member','{{}}')")))
            self.sql(self.admin(f"SELECT storage_v2_put_intelligence_relation({source},{entity},{target},'contains','{{\"resolution_kind\":\"user_asserted\",\"confidence\":0.7,\"evidence_line\":1}}')"))
            self.sql(self.admin(f"SELECT storage_v2_put_intelligence_relation({source},{target},{entity},'owned_by','{{\"resolution_kind\":\"user_asserted\"}}')"))
        response=self.active(digest,dict(name='Container',limit=3),command='ownership')
        self.assertEqual([len(r['value']) for r in response['results']],[2,1])
        rows=response['results'][0]['value']
        incoming=next(r for r in rows if r['direction']=='incoming')
        outgoing=next(r for r in rows if r['direction']=='outgoing')
        self.assertIsNone(incoming['confidence'])
        self.assertEqual(outgoing['confidence'],0.7)
        self.assertEqual(outgoing['evidence_line'],1)
        self.assertEqual(outgoing['target_file'],'source-1.rs')
        self.assertEqual(outgoing['metadata_scope'],'source_intelligence')
        reader=self.active(digest,dict(name='Container',limit=3),actor=parent.READER,command='ownership')
        self.assertEqual(reader['source_count'],1)
        self.assertEqual(reader['results'][0]['value'],rows)
        self.assertEqual(self.active(digest,dict(name=None),command='ownership')['results'][0]['value'],[])
        self.assert_sql_fails(self.actor(parent.READER,f"SELECT storage_v2_ownership_command(2,'current','Container')"),'authorized generation selector')
        before=self.sql("SELECT to_jsonb(p)-'prosrc' FROM pg_proc p WHERE oid='storage_v2_ownership_command(bigint,text,text,bigint)'::regprocedure")
        self.command(self.database,file=migration)
        self.assertEqual(self.sql("SELECT to_jsonb(p)-'prosrc' FROM pg_proc p WHERE oid='storage_v2_ownership_command(bigint,text,text,bigint)'::regprocedure"),before)
        self.assertEqual(self.active(digest,dict(name='Container',limit=3),command='ownership'),response)
