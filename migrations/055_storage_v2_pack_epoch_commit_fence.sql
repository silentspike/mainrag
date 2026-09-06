-- Order reader registration against the entire placement-switch transaction.
-- The shared fence is held only until registration commits, not for file I/O.
-- The exclusive fence must precede pack/GC row locks and lasts through commit.
-- Readers must register before fetching placement in a subsequent statement,
-- retain their epoch through verified file I/O, and finish it afterwards.
DO $$
DECLARE
    v_signatures TEXT[] := ARRAY[
        'storage_v2_begin_reader_epoch()',
        'storage_v2_switch_pack(uuid,uuid,bigint)',
        'storage_v2_mark_pack_readers_drained(uuid)',
        'storage_v2_reclaim_pack(uuid)'
    ];
    v_anchors TEXT[] := ARRAY[
        '    INSERT INTO content_reader_epoch(principal_id)',
        '    PERFORM 1 FROM storage_v2_gc_epoch',
        '    SELECT switched_at INTO v_switched_at FROM content_pack_retirement',
        '    IF EXISTS (SELECT 1 FROM content_body WHERE pack_id = p_pack_id) THEN'
    ];
    v_guard TEXT := E'    -- pack-epoch-commit-fence-v1\n'
        || E'    IF current_setting(''transaction_isolation'') <> ''read committed'' THEN\n'
        || E'        RAISE EXCEPTION ''pack epoch operations require read committed isolation''\n'
        || E'            USING ERRCODE = ''25001'';\n'
        || E'    END IF;\n';
    v_definition TEXT;
    v_insert TEXT;
    v_index INTEGER;
BEGIN
    FOR v_index IN 1..4 LOOP
        v_definition := pg_get_functiondef(v_signatures[v_index]::REGPROCEDURE);
        v_insert := v_guard;
        IF v_index = 1 THEN
            v_insert := v_insert || E'    PERFORM pg_advisory_xact_lock_shared(1937138226, 58);\n';
        ELSIF v_index = 2 THEN
            v_insert := v_insert || E'    PERFORM pg_advisory_xact_lock(1937138226, 58);\n';
        END IF;
        IF strpos(v_definition, v_insert || v_anchors[v_index]) > 0 THEN
            CONTINUE;
        END IF;
        IF strpos(v_definition, 'pack-epoch-commit-fence-v1') > 0
           OR (length(v_definition) - length(replace(v_definition, v_anchors[v_index], '')))
                / length(v_anchors[v_index]) <> 1 THEN
            RAISE EXCEPTION 'pack epoch fence prerequisite differs: %', v_signatures[v_index];
        END IF;
        EXECUTE replace(v_definition, v_anchors[v_index], v_insert || v_anchors[v_index]);
    END LOOP;
END
$$;
