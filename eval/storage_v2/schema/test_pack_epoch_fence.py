"""Real-backend commit-order regression; not physical reclamation evidence."""

import os
import subprocess
import time
import uuid

from eval.storage_v2.schema import test_content_schema as schema


MIGRATION = schema.ROOT / 'migrations/055_storage_v2_pack_epoch_commit_fence.sql'


class PackEpochFenceTests(schema.ContentSchemaFixture):
    def pair(self):
        old, new = str(uuid.uuid4()), str(uuid.uuid4())
        logical = uuid.uuid4().bytes
        digest = self.digest_hex(logical)
        body = int(self.sql(self.admin(f"""
BEGIN;
SELECT storage_v2_create_pack('{old}', '{old}.pack', '{uuid.uuid4()}') IS NOT NULL;
WITH body AS (
    INSERT INTO content_body(digest_algorithm, digest, logical_length, pack_id)
    VALUES ('sha256-v1', decode('{digest}', 'hex'), 16, '{old}') RETURNING id
)
INSERT INTO content_pack_entry(pack_id, ordinal, body_id, pack_offset,
    stored_length, codec, entry_digest)
SELECT '{old}', 0, id, 0, 16, 'identity', decode('{digest}', 'hex') FROM body;
SELECT storage_v2_verify_pack('{old}', decode('{digest}', 'hex'), 16) IS NOT NULL;
SELECT storage_v2_publish_pack('{old}') IS NOT NULL;
COMMIT;
SELECT id FROM content_body WHERE pack_id = '{old}';
""")).splitlines()[-1])
        self.create_published_pack(new, body, logical, logical)
        epoch = self.sql(self.admin("""
INSERT INTO storage_v2_gc_epoch(source_id, status, root_manifest_sha256, code_sha, verified_at)
VALUES (NULL, 'verified', repeat('a',64), repeat('b',40), clock_timestamp()) RETURNING id;
"""))
        return old, new, body, f"SELECT storage_v2_switch_pack('{old}', '{new}', {epoch});"

    def start(self, statement):
        name = 'pack-fence-' + uuid.uuid4().hex
        process = subprocess.Popen(
            ['psql', '-X', '--quiet', '--set=ON_ERROR_STOP=1', '-At',
             '--host', str(self.socket), '--dbname', self.database,
             '--command', self.admin("SET statement_timeout='15s'; " + statement)],
            env={**os.environ, 'PGAPPNAME': name},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.addCleanup(self.reap, process)
        return name, process

    @staticmethod
    def reap(process):
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)

    def wait(self, client, event):
        name, process = client
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if process.poll() is not None:
                out, error = process.communicate()
                self.fail(f'client exited before {event}: {out} {error}')
            if self.sql("SELECT count(*) FROM pg_stat_activity "
                        f"WHERE application_name='{name}' AND wait_event='{event}'") == '1':
                return
            time.sleep(0.03)
        self.fail(f'client did not reach {event}')

    def finish(self, client):
        out, error = client[1].communicate(timeout=8)
        self.assertEqual(client[1].returncode, 0, error)
        return out.strip().splitlines()

    def barrier(self):
        table = 'pack_barrier_' + uuid.uuid4().hex
        self.sql(f'CREATE TABLE {table}(released BOOLEAN); INSERT INTO {table} VALUES(FALSE)')
        self.addCleanup(self.sql, f'DROP TABLE {table}')
        # Cleanup releases the owner-specific transaction before reaping clients.
        return table, f"""
DO $$ BEGIN
    WHILE NOT (SELECT released FROM {table}) LOOP PERFORM pg_sleep(0.03); END LOOP;
END $$;
"""

    def test_registration_waits_for_switch_commit_or_rollback(self):
        for commit in (True, False):
            with self.subTest(commit=commit):
                old, new, body, switch = self.pair()
                table, pause = self.barrier()
                writer = self.start('BEGIN; ' + switch + pause + ('COMMIT;' if commit else 'ROLLBACK;'))
                try:
                    self.wait(writer, 'PgSleep')
                    reader = self.start('SELECT storage_v2_begin_reader_epoch(); '
                                        f'SELECT pack_id FROM content_body WHERE id={body};')
                    self.wait(reader, 'advisory')
                    self.sql(f'UPDATE {table} SET released=TRUE')
                    self.finish(writer)
                    observed = self.finish(reader)
                    self.assertEqual(observed[1], new if commit else old)
                    if commit:
                        # A reader registered after committed switch needs only the new pack.
                        self.sql(self.admin(f"SELECT storage_v2_mark_pack_readers_drained('{old}')"))
                    else:
                        self.assertEqual(self.sql(f"SELECT count(*) FROM content_pack_retirement WHERE pack_id='{old}'"), '0')
                    self.sql(self.admin(f"SELECT storage_v2_end_reader_epoch('{observed[0]}')"))
                finally:
                    self.sql(f'UPDATE {table} SET released=TRUE')

    def test_switch_waits_for_registration_commit_and_then_retains_old_pack(self):
        old, new, body, switch = self.pair()
        table, pause = self.barrier()
        reader = self.start('BEGIN; SELECT storage_v2_begin_reader_epoch(); '
                            f'SELECT pack_id FROM content_body WHERE id={body}; ' + pause + 'COMMIT;')
        try:
            self.wait(reader, 'PgSleep')
            writer = self.start(switch)
            self.wait(writer, 'advisory')
            self.sql(f'UPDATE {table} SET released=TRUE')
            observed = self.finish(reader)
            self.finish(writer)
            self.assertEqual(observed[1], old)
            self.assertEqual(self.sql(f'SELECT pack_id FROM content_body WHERE id={body}'), new)
            self.assert_sql_fails(self.admin(f"SELECT storage_v2_mark_pack_readers_drained('{old}')"),
                                  'pre-switch readers are still active')
            self.sql(self.admin(f"SELECT storage_v2_end_reader_epoch('{observed[0]}')"))
            self.sql(self.admin(f"SELECT storage_v2_mark_pack_readers_drained('{old}')"))
        finally:
            self.sql(f'UPDATE {table} SET released=TRUE')

    def test_readers_share_registration_fence(self):
        table, pause = self.barrier()
        first = self.start('BEGIN; SELECT storage_v2_begin_reader_epoch(); ' + pause + 'COMMIT;')
        try:
            self.wait(first, 'PgSleep')
            second = self.start('SELECT storage_v2_begin_reader_epoch();')
            second_epoch = self.finish(second)[0]
            self.sql(f'UPDATE {table} SET released=TRUE')
            first_epoch = self.finish(first)[0]
            for epoch in (first_epoch, second_epoch):
                self.sql(self.admin(f"SELECT storage_v2_end_reader_epoch('{epoch}')"))
        finally:
            self.sql(f'UPDATE {table} SET released=TRUE')

    def test_stale_snapshots_are_rejected(self):
        old, new, _, switch = self.pair()
        for isolation in ('REPEATABLE READ', 'SERIALIZABLE'):
            for operation in ('SELECT storage_v2_begin_reader_epoch();', switch,
                              f"SELECT storage_v2_mark_pack_readers_drained('{old}');",
                              f"SELECT storage_v2_reclaim_pack('{old}');"):
                with self.subTest(isolation=isolation, operation=operation):
                    self.assert_sql_fails(self.admin(f'BEGIN ISOLATION LEVEL {isolation}; {operation}'),
                                          'require read committed isolation')
        self.assertEqual(self.sql(f"SELECT status FROM content_pack WHERE id='{old}'"), 'published')

    def test_historical_timestamp_window_is_real(self):
        # Negative control: the previous functions let an epoch begin after the
        # switch timestamp but before commit, read OLD placement, then pass drain.
        self.file(schema.MIGRATION)
        try:
            old, _, body, switch = self.pair()
            table, pause = self.barrier()
            writer = self.start('BEGIN; ' + switch + pause + 'COMMIT;')
            try:
                self.wait(writer, 'PgSleep')
                reader = self.start('SELECT storage_v2_begin_reader_epoch(); '
                                    f'SELECT pack_id FROM content_body WHERE id={body};')
                observed = self.finish(reader)
                self.assertEqual(observed[1], old)
                self.sql(f'UPDATE {table} SET released=TRUE')
                self.finish(writer)
                self.sql(self.admin(f"SELECT storage_v2_mark_pack_readers_drained('{old}')"))
                self.assertEqual(self.sql(f"SELECT finished_at IS NULL FROM content_reader_epoch WHERE id='{observed[0]}'"), 't')
                self.sql(self.admin(f"SELECT storage_v2_end_reader_epoch('{observed[0]}')"))
            finally:
                self.sql(f'UPDATE {table} SET released=TRUE')
                self.reap(writer[1])
        finally:
            self.file(MIGRATION)

    def test_migration_replay_preserves_function_contracts(self):
        query = """
SELECT oid, proowner, proacl, prosecdef, provolatile, proconfig, pg_get_functiondef(oid)
FROM pg_proc WHERE proname IN ('storage_v2_begin_reader_epoch', 'storage_v2_switch_pack',
    'storage_v2_mark_pack_readers_drained', 'storage_v2_reclaim_pack') ORDER BY oid
"""
        before = self.sql(query)
        self.file(MIGRATION)
        self.assertEqual(self.sql(query), before)
