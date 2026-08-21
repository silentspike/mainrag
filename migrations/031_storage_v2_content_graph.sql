-- Migration 031: lossless content graph, retrieval views, and hit mappings
--
-- The new graph is additive and is not selected by current indexing or search.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE OR REPLACE FUNCTION storage_v2_hash_parts(
    p_domain TEXT,
    p_parts BYTEA[]
) RETURNS BYTEA
LANGUAGE plpgsql IMMUTABLE STRICT
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_result BYTEA;
    v_part BYTEA;
BEGIN
    v_result := int8send(octet_length(convert_to(p_domain, 'UTF8')))
        || convert_to(p_domain, 'UTF8')
        || int8send(cardinality(p_parts));
    FOREACH v_part IN ARRAY p_parts LOOP
        IF v_part IS NULL THEN
            RAISE EXCEPTION 'canonical digest parts cannot be null';
        END IF;
        v_result := v_result || int8send(octet_length(v_part)) || v_part;
    END LOOP;
    RETURN digest(v_result, 'sha256');
END
$$;

CREATE TABLE IF NOT EXISTS content_node (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    digest_schema TEXT NOT NULL CHECK (digest_schema = 'content-node-v1'),
    domain TEXT NOT NULL CHECK (domain <> ''),
    node_type TEXT NOT NULL CHECK (node_type <> ''),
    logical_length BIGINT NOT NULL CHECK (logical_length >= 0),
    body_id BIGINT REFERENCES content_body(id) ON DELETE RESTRICT,
    node_digest BYTEA NOT NULL CHECK (octet_length(node_digest) = 32),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (digest_schema, domain, node_digest),
    UNIQUE (id, node_digest)
);

CREATE TABLE IF NOT EXISTS content_node_edge (
    parent_node_id BIGINT NOT NULL REFERENCES content_node(id) ON DELETE RESTRICT,
    ordinal BIGINT NOT NULL CHECK (ordinal >= 0),
    edge_type TEXT NOT NULL CHECK (edge_type <> ''),
    child_kind TEXT NOT NULL CHECK (child_kind <> ''),
    child_node_id BIGINT NOT NULL REFERENCES content_node(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (parent_node_id, ordinal),
    CHECK (parent_node_id <> child_node_id)
);

CREATE INDEX IF NOT EXISTS idx_content_node_edge_child
    ON content_node_edge (child_node_id, parent_node_id);

CREATE OR REPLACE FUNCTION storage_v2_validate_content_node() RETURNS TRIGGER
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_node_id BIGINT;
    v_node content_node;
    v_body content_body;
    v_edge RECORD;
    v_expected_ordinal BIGINT := 0;
    v_parts BYTEA[];
    v_digest BYTEA;
BEGIN
    IF TG_TABLE_NAME = 'content_node' THEN
        v_node_id := NEW.id;
    ELSIF TG_OP = 'DELETE' THEN
        v_node_id := OLD.parent_node_id;
    ELSE
        v_node_id := NEW.parent_node_id;
    END IF;
    SELECT * INTO v_node FROM content_node WHERE id = v_node_id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    v_parts := ARRAY[
        convert_to('content-node-v1', 'UTF8'),
        convert_to(v_node.domain, 'UTF8'),
        convert_to(v_node.node_type, 'UTF8'),
        int8send(v_node.logical_length)
    ];
    IF v_node.body_id IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM content_node_edge WHERE parent_node_id = v_node.id) THEN
            RAISE EXCEPTION 'leaf content nodes cannot have children';
        END IF;
        SELECT * INTO v_body FROM content_body WHERE id = v_node.body_id;
        IF NOT FOUND OR v_body.logical_length <> v_node.logical_length THEN
            RAISE EXCEPTION 'leaf node body length is inconsistent';
        END IF;
        v_parts := array_append(v_parts, convert_to(v_body.digest_algorithm, 'UTF8'));
        v_parts := array_append(v_parts, v_body.digest);
        v_parts := array_append(v_parts, int8send(v_body.logical_length));
    ELSE
        FOR v_edge IN
            SELECT edge.ordinal, edge.edge_type, edge.child_kind,
                   child.node_type, child.node_digest
              FROM content_node_edge edge
              JOIN content_node child ON child.id = edge.child_node_id
             WHERE edge.parent_node_id = v_node.id
             ORDER BY edge.ordinal
        LOOP
            IF v_edge.ordinal <> v_expected_ordinal
               OR v_edge.child_kind <> v_edge.node_type THEN
                RAISE EXCEPTION 'internal node edge order or child kind is inconsistent';
            END IF;
            v_parts := array_append(v_parts, convert_to(v_edge.edge_type, 'UTF8'));
            v_parts := array_append(v_parts, convert_to(v_edge.child_kind, 'UTF8'));
            v_parts := array_append(v_parts, v_edge.node_digest);
            v_expected_ordinal := v_expected_ordinal + 1;
        END LOOP;
        IF v_expected_ordinal = 0 THEN
            RAISE EXCEPTION 'internal content nodes require at least one child';
        END IF;
        IF (
            SELECT COALESCE(SUM(child.logical_length), 0)
              FROM content_node_edge edge
              JOIN content_node child ON child.id = edge.child_node_id
             WHERE edge.parent_node_id = v_node.id
        ) <> v_node.logical_length THEN
            RAISE EXCEPTION 'internal node logical length is inconsistent';
        END IF;
    END IF;
    v_digest := storage_v2_hash_parts('mainrag.content-node.v1', v_parts);
    IF v_digest <> v_node.node_digest THEN
        RAISE EXCEPTION 'content node digest does not match canonical components';
    END IF;
    RETURN NULL;
END
$$;

DROP TRIGGER IF EXISTS content_node_consistency ON content_node;
CREATE CONSTRAINT TRIGGER content_node_consistency
    AFTER INSERT OR UPDATE ON content_node
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION storage_v2_validate_content_node();
DROP TRIGGER IF EXISTS content_node_edge_consistency ON content_node_edge;
CREATE CONSTRAINT TRIGGER content_node_edge_consistency
    AFTER INSERT OR UPDATE OR DELETE ON content_node_edge
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION storage_v2_validate_content_node();

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'artifact_version_content_root_node_fkey'
           AND conrelid = 'public.artifact_version'::REGCLASS
    ) THEN
        ALTER TABLE artifact_version
            ADD CONSTRAINT artifact_version_content_root_node_fkey
            FOREIGN KEY (content_root_node_id) REFERENCES content_node(id) ON DELETE RESTRICT;
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_reject_graph_mutation() RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
BEGIN
    RAISE EXCEPTION '% rows are immutable', TG_TABLE_NAME USING ERRCODE = '55000';
END
$$;

DROP TRIGGER IF EXISTS content_node_immutable ON content_node;
CREATE TRIGGER content_node_immutable
    BEFORE UPDATE OR DELETE ON content_node
    FOR EACH ROW EXECUTE FUNCTION storage_v2_reject_graph_mutation();

DROP TRIGGER IF EXISTS content_node_edge_immutable ON content_node_edge;
CREATE TRIGGER content_node_edge_immutable
    BEFORE UPDATE OR DELETE ON content_node_edge
    FOR EACH ROW EXECUTE FUNCTION storage_v2_reject_graph_mutation();

CREATE OR REPLACE FUNCTION storage_v2_put_leaf_node(
    p_domain TEXT,
    p_node_type TEXT,
    p_body_id BIGINT
) RETURNS content_node
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_body content_body;
    v_digest BYTEA;
    v_node content_node;
BEGIN
    IF NOT storage_v2_is_admin() THEN
        RAISE EXCEPTION 'graph writes require administrator authority' USING ERRCODE = '42501';
    END IF;
    SELECT * INTO v_body FROM content_body WHERE id = p_body_id;
    IF NOT FOUND OR p_domain IS NULL OR p_domain = '' OR p_node_type IS NULL OR p_node_type = '' THEN
        RAISE EXCEPTION 'leaf node requires domain, type, and body';
    END IF;
    v_digest := storage_v2_hash_parts('mainrag.content-node.v1', ARRAY[
        convert_to('content-node-v1', 'UTF8'),
        convert_to(p_domain, 'UTF8'),
        convert_to(p_node_type, 'UTF8'),
        int8send(v_body.logical_length),
        convert_to(v_body.digest_algorithm, 'UTF8'),
        v_body.digest,
        int8send(v_body.logical_length)
    ]);
    INSERT INTO content_node(
        digest_schema, domain, node_type, logical_length, body_id, node_digest
    ) VALUES (
        'content-node-v1', p_domain, p_node_type, v_body.logical_length,
        p_body_id, v_digest
    )
    ON CONFLICT (digest_schema, domain, node_digest) DO NOTHING
    RETURNING * INTO v_node;
    IF NOT FOUND THEN
        SELECT * INTO v_node FROM content_node
         WHERE digest_schema = 'content-node-v1'
           AND domain = p_domain AND node_digest = v_digest;
        IF v_node.node_type IS DISTINCT FROM p_node_type
           OR v_node.logical_length IS DISTINCT FROM v_body.logical_length
           OR v_node.body_id IS DISTINCT FROM p_body_id THEN
            RAISE EXCEPTION 'content node digest collision' USING ERRCODE = '22000';
        END IF;
    END IF;
    RETURN v_node;
END
$$;

CREATE OR REPLACE FUNCTION storage_v2_put_internal_node(
    p_domain TEXT,
    p_node_type TEXT,
    p_logical_length BIGINT,
    p_edge_types TEXT[],
    p_child_node_ids BIGINT[]
) RETURNS content_node
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_parts BYTEA[] := ARRAY[
        convert_to('content-node-v1', 'UTF8'),
        convert_to(p_domain, 'UTF8'),
        convert_to(p_node_type, 'UTF8'),
        int8send(p_logical_length)
    ];
    v_child content_node;
    v_digest BYTEA;
    v_node content_node;
    v_inserted BOOLEAN := FALSE;
    v_index INTEGER;
    v_existing JSONB;
    v_expected JSONB;
BEGIN
    IF NOT storage_v2_is_admin() THEN
        RAISE EXCEPTION 'graph writes require administrator authority' USING ERRCODE = '42501';
    END IF;
    IF p_domain IS NULL OR p_domain = '' OR p_node_type IS NULL OR p_node_type = ''
       OR p_logical_length IS NULL OR p_logical_length < 0
       OR p_edge_types IS NULL OR p_child_node_ids IS NULL
       OR cardinality(p_edge_types) = 0
       OR cardinality(p_edge_types) IS DISTINCT FROM cardinality(p_child_node_ids) THEN
        RAISE EXCEPTION 'internal node requires aligned non-empty children';
    END IF;
    FOR v_index IN 1..cardinality(p_child_node_ids) LOOP
        SELECT * INTO v_child FROM content_node WHERE id = p_child_node_ids[v_index];
        IF NOT FOUND OR p_edge_types[v_index] IS NULL OR p_edge_types[v_index] = '' THEN
            RAISE EXCEPTION 'internal node child or edge type is invalid';
        END IF;
        v_parts := array_append(v_parts, convert_to(p_edge_types[v_index], 'UTF8'));
        v_parts := array_append(v_parts, convert_to(v_child.node_type, 'UTF8'));
        v_parts := array_append(v_parts, v_child.node_digest);
    END LOOP;
    v_digest := storage_v2_hash_parts('mainrag.content-node.v1', v_parts);
    INSERT INTO content_node(
        digest_schema, domain, node_type, logical_length, body_id, node_digest
    ) VALUES (
        'content-node-v1', p_domain, p_node_type, p_logical_length, NULL, v_digest
    )
    ON CONFLICT (digest_schema, domain, node_digest) DO NOTHING
    RETURNING * INTO v_node;
    v_inserted := FOUND;
    IF v_inserted THEN
        FOR v_index IN 1..cardinality(p_child_node_ids) LOOP
            INSERT INTO content_node_edge(
                parent_node_id, ordinal, edge_type, child_kind, child_node_id
            )
            SELECT v_node.id, v_index - 1, p_edge_types[v_index], node_type, id
              FROM content_node WHERE id = p_child_node_ids[v_index];
        END LOOP;
    ELSE
        SELECT * INTO v_node FROM content_node
         WHERE digest_schema = 'content-node-v1'
           AND domain = p_domain AND node_digest = v_digest;
        SELECT COALESCE(jsonb_agg(
            jsonb_build_array(edge_type, child_node_id) ORDER BY ordinal
        ), '[]'::JSONB) INTO v_existing
          FROM content_node_edge WHERE parent_node_id = v_node.id;
        SELECT jsonb_agg(
            jsonb_build_array(p_edge_types[index], p_child_node_ids[index]) ORDER BY index
        ) INTO v_expected
          FROM generate_subscripts(p_child_node_ids, 1) AS index;
        IF v_node.node_type IS DISTINCT FROM p_node_type
           OR v_node.logical_length IS DISTINCT FROM p_logical_length
           OR v_node.body_id IS NOT NULL OR v_existing IS DISTINCT FROM v_expected THEN
            RAISE EXCEPTION 'content node digest collision' USING ERRCODE = '22000';
        END IF;
    END IF;
    RETURN v_node;
END
$$;

CREATE TABLE IF NOT EXISTS retrieval_view (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    digest_schema TEXT NOT NULL CHECK (digest_schema = 'retrieval-view-v1'),
    view_type TEXT NOT NULL CHECK (view_type <> ''),
    profile_id TEXT NOT NULL CHECK (profile_id <> ''),
    language_id TEXT NOT NULL CHECK (language_id <> ''),
    tokenizer_version TEXT NOT NULL CHECK (tokenizer_version <> ''),
    capability_flags BIGINT NOT NULL CHECK (capability_flags >= 0),
    view_digest BYTEA NOT NULL CHECK (octet_length(view_digest) = 32),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (digest_schema, view_digest)
);

CREATE TABLE IF NOT EXISTS view_component (
    view_id BIGINT NOT NULL REFERENCES retrieval_view(id) ON DELETE RESTRICT,
    ordinal BIGINT NOT NULL CHECK (ordinal >= 0),
    role TEXT NOT NULL CHECK (role <> ''),
    component_kind TEXT NOT NULL CHECK (component_kind IN ('body', 'node')),
    body_id BIGINT REFERENCES content_body(id) ON DELETE RESTRICT,
    node_id BIGINT REFERENCES content_node(id) ON DELETE RESTRICT,
    relative_start BIGINT NOT NULL CHECK (relative_start >= 0),
    relative_end BIGINT NOT NULL CHECK (relative_end >= relative_start),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (view_id, ordinal),
    CHECK ((body_id IS NOT NULL)::INTEGER + (node_id IS NOT NULL)::INTEGER = 1),
    CHECK ((component_kind = 'body') = (body_id IS NOT NULL)),
    CHECK ((component_kind = 'node') = (node_id IS NOT NULL))
);

CREATE OR REPLACE FUNCTION storage_v2_validate_retrieval_view() RETURNS TRIGGER
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_view_id BIGINT;
    v_view retrieval_view;
    v_component RECORD;
    v_expected_ordinal BIGINT := 0;
    v_component_digest BYTEA;
    v_parts BYTEA[];
    v_digest BYTEA;
BEGIN
    IF TG_TABLE_NAME = 'retrieval_view' THEN
        v_view_id := NEW.id;
    ELSIF TG_OP = 'DELETE' THEN
        v_view_id := OLD.view_id;
    ELSE
        v_view_id := NEW.view_id;
    END IF;
    SELECT * INTO v_view FROM retrieval_view WHERE id = v_view_id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    v_parts := ARRAY[
        convert_to('retrieval-view-v1', 'UTF8'),
        convert_to(v_view.view_type, 'UTF8'),
        convert_to(v_view.profile_id, 'UTF8'),
        convert_to(COALESCE(v_view.language_id, '<unknown>'), 'UTF8'),
        convert_to(v_view.tokenizer_version, 'UTF8'),
        int8send(v_view.capability_flags)
    ];
    FOR v_component IN
        SELECT * FROM view_component
         WHERE view_id = v_view.id ORDER BY ordinal
    LOOP
        IF v_component.ordinal <> v_expected_ordinal THEN
            RAISE EXCEPTION 'retrieval view component order is inconsistent';
        END IF;
        IF v_component.component_kind = 'body' THEN
            SELECT digest INTO v_component_digest FROM content_body
             WHERE id = v_component.body_id;
        ELSE
            SELECT node_digest INTO v_component_digest FROM content_node
             WHERE id = v_component.node_id;
        END IF;
        v_parts := array_append(v_parts, convert_to(v_component.role, 'UTF8'));
        v_parts := array_append(v_parts, convert_to(v_component.component_kind, 'UTF8'));
        v_parts := array_append(v_parts, v_component_digest);
        v_parts := array_append(v_parts, int8send(v_component.relative_start));
        v_parts := array_append(v_parts, int8send(v_component.relative_end));
        v_expected_ordinal := v_expected_ordinal + 1;
    END LOOP;
    IF v_expected_ordinal = 0 THEN
        RAISE EXCEPTION 'retrieval view requires at least one component';
    END IF;
    v_digest := storage_v2_hash_parts('mainrag.retrieval-view.v1', v_parts);
    IF v_digest <> v_view.view_digest THEN
        RAISE EXCEPTION 'retrieval view digest does not match canonical components';
    END IF;
    RETURN NULL;
END
$$;

DROP TRIGGER IF EXISTS retrieval_view_consistency ON retrieval_view;
CREATE CONSTRAINT TRIGGER retrieval_view_consistency
    AFTER INSERT OR UPDATE ON retrieval_view
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION storage_v2_validate_retrieval_view();
DROP TRIGGER IF EXISTS view_component_consistency ON view_component;
CREATE CONSTRAINT TRIGGER view_component_consistency
    AFTER INSERT OR UPDATE OR DELETE ON view_component
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION storage_v2_validate_retrieval_view();

DROP TRIGGER IF EXISTS retrieval_view_immutable ON retrieval_view;
CREATE TRIGGER retrieval_view_immutable
    BEFORE UPDATE OR DELETE ON retrieval_view
    FOR EACH ROW EXECUTE FUNCTION storage_v2_reject_graph_mutation();
DROP TRIGGER IF EXISTS view_component_immutable ON view_component;
CREATE TRIGGER view_component_immutable
    BEFORE UPDATE OR DELETE ON view_component
    FOR EACH ROW EXECUTE FUNCTION storage_v2_reject_graph_mutation();

CREATE OR REPLACE FUNCTION storage_v2_put_retrieval_view(
    p_view_type TEXT,
    p_profile_id TEXT,
    p_language_id TEXT,
    p_tokenizer_version TEXT,
    p_capability_flags BIGINT,
    p_roles TEXT[],
    p_component_kinds TEXT[],
    p_component_ids BIGINT[],
    p_relative_starts BIGINT[],
    p_relative_ends BIGINT[]
) RETURNS retrieval_view
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
DECLARE
    v_parts BYTEA[] := ARRAY[
        convert_to('retrieval-view-v1', 'UTF8'),
        convert_to(p_view_type, 'UTF8'),
        convert_to(p_profile_id, 'UTF8'),
        convert_to(COALESCE(p_language_id, '<unknown>'), 'UTF8'),
        convert_to(p_tokenizer_version, 'UTF8'),
        int8send(p_capability_flags)
    ];
    v_component_digest BYTEA;
    v_digest BYTEA;
    v_view retrieval_view;
    v_inserted BOOLEAN := FALSE;
    v_index INTEGER;
    v_existing JSONB;
    v_expected JSONB;
BEGIN
    IF NOT storage_v2_is_admin() THEN
        RAISE EXCEPTION 'view writes require administrator authority' USING ERRCODE = '42501';
    END IF;
    IF p_view_type IS NULL OR p_view_type = '' OR p_profile_id IS NULL OR p_profile_id = ''
       OR p_language_id IS NULL OR p_language_id = ''
       OR p_tokenizer_version IS NULL OR p_tokenizer_version = ''
       OR p_capability_flags IS NULL OR p_capability_flags < 0
       OR p_roles IS NULL OR p_component_kinds IS NULL OR p_component_ids IS NULL
       OR p_relative_starts IS NULL OR p_relative_ends IS NULL
       OR cardinality(p_roles) = 0
       OR cardinality(p_roles) IS DISTINCT FROM cardinality(p_component_kinds)
       OR cardinality(p_roles) IS DISTINCT FROM cardinality(p_component_ids)
       OR cardinality(p_roles) IS DISTINCT FROM cardinality(p_relative_starts)
       OR cardinality(p_roles) IS DISTINCT FROM cardinality(p_relative_ends) THEN
        RAISE EXCEPTION 'retrieval view requires aligned non-empty components';
    END IF;
    FOR v_index IN 1..cardinality(p_roles) LOOP
        IF p_relative_starts[v_index] < 0
           OR p_relative_ends[v_index] < p_relative_starts[v_index] THEN
            RAISE EXCEPTION 'invalid retrieval component span';
        END IF;
        IF p_component_kinds[v_index] = 'body' THEN
            SELECT digest INTO v_component_digest FROM content_body
             WHERE id = p_component_ids[v_index];
        ELSIF p_component_kinds[v_index] = 'node' THEN
            SELECT node_digest INTO v_component_digest FROM content_node
             WHERE id = p_component_ids[v_index];
        ELSE
            RAISE EXCEPTION 'invalid retrieval component kind';
        END IF;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'retrieval component not found';
        END IF;
        v_parts := array_append(v_parts, convert_to(p_roles[v_index], 'UTF8'));
        v_parts := array_append(v_parts, convert_to(p_component_kinds[v_index], 'UTF8'));
        v_parts := array_append(v_parts, v_component_digest);
        v_parts := array_append(v_parts, int8send(p_relative_starts[v_index]));
        v_parts := array_append(v_parts, int8send(p_relative_ends[v_index]));
    END LOOP;
    v_digest := storage_v2_hash_parts('mainrag.retrieval-view.v1', v_parts);
    INSERT INTO retrieval_view(
        digest_schema, view_type, profile_id, language_id,
        tokenizer_version, capability_flags, view_digest
    ) VALUES (
        'retrieval-view-v1', p_view_type, p_profile_id, p_language_id,
        p_tokenizer_version, p_capability_flags, v_digest
    )
    ON CONFLICT (digest_schema, view_digest) DO NOTHING
    RETURNING * INTO v_view;
    v_inserted := FOUND;
    IF v_inserted THEN
        FOR v_index IN 1..cardinality(p_roles) LOOP
            INSERT INTO view_component(
                view_id, ordinal, role, component_kind, body_id, node_id,
                relative_start, relative_end
            ) VALUES (
                v_view.id, v_index - 1, p_roles[v_index], p_component_kinds[v_index],
                CASE WHEN p_component_kinds[v_index] = 'body' THEN p_component_ids[v_index] END,
                CASE WHEN p_component_kinds[v_index] = 'node' THEN p_component_ids[v_index] END,
                p_relative_starts[v_index], p_relative_ends[v_index]
            );
        END LOOP;
    ELSE
        SELECT * INTO v_view FROM retrieval_view
         WHERE digest_schema = 'retrieval-view-v1' AND view_digest = v_digest;
        SELECT COALESCE(jsonb_agg(
            jsonb_build_array(
                role, component_kind, COALESCE(body_id, node_id),
                relative_start, relative_end
            ) ORDER BY ordinal
        ), '[]'::JSONB) INTO v_existing
          FROM view_component WHERE view_id = v_view.id;
        SELECT jsonb_agg(
            jsonb_build_array(
                p_roles[index], p_component_kinds[index], p_component_ids[index],
                p_relative_starts[index], p_relative_ends[index]
            ) ORDER BY index
        ) INTO v_expected
          FROM generate_subscripts(p_roles, 1) AS index;
        IF v_view.view_type IS DISTINCT FROM p_view_type
           OR v_view.profile_id IS DISTINCT FROM p_profile_id
           OR v_view.language_id IS DISTINCT FROM p_language_id
           OR v_view.tokenizer_version IS DISTINCT FROM p_tokenizer_version
           OR v_view.capability_flags IS DISTINCT FROM p_capability_flags
           OR v_existing IS DISTINCT FROM v_expected THEN
            RAISE EXCEPTION 'retrieval view digest collision' USING ERRCODE = '22000';
        END IF;
    END IF;
    RETURN v_view;
END
$$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_artifact_version_id_source
    ON artifact_version (id, source_id);

CREATE TABLE IF NOT EXISTS occurrence (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES logical_source(id) ON DELETE RESTRICT,
    artifact_version_id BIGINT NOT NULL,
    view_id BIGINT NOT NULL REFERENCES retrieval_view(id) ON DELETE RESTRICT,
    role TEXT NOT NULL CHECK (role <> ''),
    ordinal BIGINT NOT NULL CHECK (ordinal >= 0),
    parent_occurrence_id BIGINT,
    source_path TEXT NOT NULL,
    locator JSONB NOT NULL,
    derivation_recipe JSONB,
    occurred_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (id, source_id),
    UNIQUE (id, source_id, artifact_version_id),
    UNIQUE (artifact_version_id, role, ordinal),
    FOREIGN KEY (artifact_version_id, source_id)
        REFERENCES artifact_version(id, source_id) ON DELETE RESTRICT,
    FOREIGN KEY (parent_occurrence_id, source_id, artifact_version_id)
        REFERENCES occurrence(id, source_id, artifact_version_id) ON DELETE RESTRICT,
    CHECK (parent_occurrence_id IS NULL OR parent_occurrence_id <> id)
);

CREATE INDEX IF NOT EXISTS idx_occurrence_source_path_time
    ON occurrence (source_id, source_path, occurred_at, id);
CREATE INDEX IF NOT EXISTS idx_occurrence_view ON occurrence (view_id, source_id);

CREATE TABLE IF NOT EXISTS occurrence_edge (
    source_id BIGINT NOT NULL,
    from_occurrence_id BIGINT NOT NULL,
    edge_type TEXT NOT NULL CHECK (edge_type <> ''),
    to_occurrence_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source_id, from_occurrence_id, edge_type, to_occurrence_id),
    FOREIGN KEY (from_occurrence_id, source_id)
        REFERENCES occurrence(id, source_id) ON DELETE RESTRICT,
    FOREIGN KEY (to_occurrence_id, source_id)
        REFERENCES occurrence(id, source_id) ON DELETE RESTRICT,
    CHECK (from_occurrence_id <> to_occurrence_id)
);

CREATE TABLE IF NOT EXISTS occurrence_scope (
    occurrence_id BIGINT NOT NULL REFERENCES occurrence(id) ON DELETE RESTRICT,
    scope_key TEXT NOT NULL CHECK (scope_key <> ''),
    scope_value JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (occurrence_id, scope_key)
);

CREATE TABLE IF NOT EXISTS legacy_hit_mapping (
    old_hit_id TEXT NOT NULL CHECK (old_hit_id <> ''),
    occurrence_id BIGINT NOT NULL REFERENCES occurrence(id) ON DELETE RESTRICT,
    ordinal BIGINT NOT NULL CHECK (ordinal >= 0),
    relation_kind TEXT NOT NULL CHECK (relation_kind IN ('exact', 'split', 'merged')),
    byte_overlap BIGINT NOT NULL CHECK (byte_overlap >= 0),
    source_offset BIGINT NOT NULL CHECK (source_offset >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (old_hit_id, ordinal),
    UNIQUE (old_hit_id, occurrence_id)
);

CREATE INDEX IF NOT EXISTS idx_legacy_hit_mapping_occurrence
    ON legacy_hit_mapping (occurrence_id, old_hit_id);

CREATE OR REPLACE FUNCTION storage_v2_guard_legacy_hit_mapping() RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
BEGIN
    IF current_setting('storage_v2.legacy_mapping_write', TRUE) IS DISTINCT FROM 'on' THEN
        RAISE EXCEPTION 'legacy mappings require the controlled replacement function'
            USING ERRCODE = '42501';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS legacy_hit_mapping_controlled_write ON legacy_hit_mapping;
CREATE TRIGGER legacy_hit_mapping_controlled_write
    BEFORE INSERT OR UPDATE OR DELETE ON legacy_hit_mapping
    FOR EACH ROW EXECUTE FUNCTION storage_v2_guard_legacy_hit_mapping();

CREATE OR REPLACE FUNCTION storage_v2_replace_legacy_hit_mapping(
    p_old_hit_id TEXT,
    p_occurrence_ids BIGINT[],
    p_relation_kind TEXT,
    p_byte_overlaps BIGINT[],
    p_source_offsets BIGINT[]
) RETURNS SETOF legacy_hit_mapping
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
BEGIN
    IF NOT storage_v2_is_admin() THEN
        RAISE EXCEPTION 'legacy mapping writes require administrator authority' USING ERRCODE = '42501';
    END IF;
    IF p_old_hit_id IS NULL OR p_old_hit_id = ''
       OR p_relation_kind NOT IN ('exact', 'split', 'merged')
       OR p_occurrence_ids IS NULL OR p_byte_overlaps IS NULL OR p_source_offsets IS NULL
       OR cardinality(p_occurrence_ids) = 0
       OR cardinality(p_occurrence_ids) IS DISTINCT FROM cardinality(p_byte_overlaps)
       OR cardinality(p_occurrence_ids) IS DISTINCT FROM cardinality(p_source_offsets)
       OR EXISTS (
           SELECT 1 FROM unnest(p_byte_overlaps, p_source_offsets) AS values_pair(overlap, offset_value)
            WHERE overlap < 0 OR offset_value < 0
       ) THEN
        RAISE EXCEPTION 'invalid legacy mapping input';
    END IF;
    IF p_relation_kind = 'exact' AND cardinality(p_occurrence_ids) <> 1 THEN
        RAISE EXCEPTION 'exact legacy mapping requires one occurrence';
    END IF;
    IF (SELECT COUNT(DISTINCT value) FROM unnest(p_occurrence_ids) AS value)
       <> cardinality(p_occurrence_ids) THEN
        RAISE EXCEPTION 'legacy mapping occurrences must be unique';
    END IF;
    IF EXISTS (
        SELECT 1 FROM unnest(p_occurrence_ids) AS occurrence_id
         WHERE NOT EXISTS (SELECT 1 FROM occurrence WHERE id = occurrence_id)
    ) THEN
        RAISE EXCEPTION 'legacy mapping occurrence not found';
    END IF;
    PERFORM set_config('storage_v2.legacy_mapping_write', 'on', TRUE);
    DELETE FROM legacy_hit_mapping WHERE old_hit_id = p_old_hit_id;
    INSERT INTO legacy_hit_mapping(
        old_hit_id, occurrence_id, ordinal, relation_kind, byte_overlap, source_offset
    )
    SELECT p_old_hit_id, occurrence_id,
           row_number() OVER (ORDER BY overlap DESC, offset_value, occurrence_id) - 1,
           p_relation_kind, overlap, offset_value
      FROM unnest(p_occurrence_ids, p_byte_overlaps, p_source_offsets)
           AS mapping(occurrence_id, overlap, offset_value);
    RETURN QUERY SELECT * FROM legacy_hit_mapping
     WHERE old_hit_id = p_old_hit_id ORDER BY ordinal;
END
$$;

DROP TRIGGER IF EXISTS occurrence_immutable ON occurrence;
CREATE TRIGGER occurrence_immutable
    BEFORE UPDATE OR DELETE ON occurrence
    FOR EACH ROW EXECUTE FUNCTION storage_v2_reject_graph_mutation();
DROP TRIGGER IF EXISTS occurrence_edge_immutable ON occurrence_edge;
CREATE TRIGGER occurrence_edge_immutable
    BEFORE UPDATE OR DELETE ON occurrence_edge
    FOR EACH ROW EXECUTE FUNCTION storage_v2_reject_graph_mutation();
DROP TRIGGER IF EXISTS occurrence_scope_immutable ON occurrence_scope;
CREATE TRIGGER occurrence_scope_immutable
    BEFORE UPDATE OR DELETE ON occurrence_scope
    FOR EACH ROW EXECUTE FUNCTION storage_v2_reject_graph_mutation();

ALTER TABLE content_node ENABLE ROW LEVEL SECURITY;
ALTER TABLE content_node_edge ENABLE ROW LEVEL SECURITY;
ALTER TABLE retrieval_view ENABLE ROW LEVEL SECURITY;
ALTER TABLE view_component ENABLE ROW LEVEL SECURITY;
ALTER TABLE occurrence ENABLE ROW LEVEL SECURITY;
ALTER TABLE occurrence_edge ENABLE ROW LEVEL SECURITY;
ALTER TABLE occurrence_scope ENABLE ROW LEVEL SECURITY;
ALTER TABLE legacy_hit_mapping ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS content_node_admin ON content_node;
CREATE POLICY content_node_admin ON content_node
    USING (storage_v2_is_admin()) WITH CHECK (storage_v2_is_admin());
DROP POLICY IF EXISTS content_node_edge_admin ON content_node_edge;
CREATE POLICY content_node_edge_admin ON content_node_edge
    USING (storage_v2_is_admin()) WITH CHECK (storage_v2_is_admin());
DROP POLICY IF EXISTS retrieval_view_admin ON retrieval_view;
CREATE POLICY retrieval_view_admin ON retrieval_view
    USING (storage_v2_is_admin()) WITH CHECK (storage_v2_is_admin());
DROP POLICY IF EXISTS view_component_admin ON view_component;
CREATE POLICY view_component_admin ON view_component
    USING (storage_v2_is_admin()) WITH CHECK (storage_v2_is_admin());
DROP POLICY IF EXISTS occurrence_isolation ON occurrence;
CREATE POLICY occurrence_isolation ON occurrence
    USING (storage_v2_can_access_source(source_id, 'read'))
    WITH CHECK (storage_v2_can_access_source(source_id, 'write'));
DROP POLICY IF EXISTS occurrence_edge_isolation ON occurrence_edge;
CREATE POLICY occurrence_edge_isolation ON occurrence_edge
    USING (storage_v2_can_access_source(source_id, 'read'))
    WITH CHECK (storage_v2_can_access_source(source_id, 'write'));
DROP POLICY IF EXISTS occurrence_scope_isolation ON occurrence_scope;
CREATE POLICY occurrence_scope_isolation ON occurrence_scope
    USING (EXISTS (
        SELECT 1 FROM occurrence visible WHERE visible.id = occurrence_id
    ))
    WITH CHECK (EXISTS (
        SELECT 1 FROM occurrence visible WHERE visible.id = occurrence_id
    ));
DROP POLICY IF EXISTS legacy_hit_mapping_isolation ON legacy_hit_mapping;
CREATE POLICY legacy_hit_mapping_isolation ON legacy_hit_mapping
    USING (EXISTS (
        SELECT 1 FROM occurrence visible WHERE visible.id = occurrence_id
    ))
    WITH CHECK (EXISTS (
        SELECT 1 FROM occurrence visible WHERE visible.id = occurrence_id
    ));

CREATE OR REPLACE FUNCTION storage_v2_visible_occurrences(
    p_source_id BIGINT DEFAULT NULL,
    p_path_prefix TEXT DEFAULT NULL,
    p_occurred_from TIMESTAMPTZ DEFAULT NULL,
    p_occurred_to TIMESTAMPTZ DEFAULT NULL
) RETURNS TABLE (
    occurrence_id BIGINT,
    source_id BIGINT,
    artifact_version_id BIGINT,
    view_id BIGINT,
    view_digest BYTEA,
    source_path TEXT,
    locator JSONB
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
    SELECT occurrence.id, occurrence.source_id, occurrence.artifact_version_id,
           occurrence.view_id, retrieval_view.view_digest,
           occurrence.source_path, occurrence.locator
      FROM occurrence
      JOIN retrieval_view ON retrieval_view.id = occurrence.view_id
     WHERE storage_v2_can_access_source(occurrence.source_id, 'read')
       AND (p_source_id IS NULL OR occurrence.source_id = p_source_id)
       AND (p_path_prefix IS NULL OR occurrence.source_path LIKE p_path_prefix || '%')
       AND (p_occurred_from IS NULL OR occurrence.occurred_at >= p_occurred_from)
       AND (p_occurred_to IS NULL OR occurrence.occurred_at < p_occurred_to)
     ORDER BY occurrence.id
$$;

REVOKE UPDATE, DELETE ON content_node FROM PUBLIC;
REVOKE UPDATE, DELETE ON content_node_edge FROM PUBLIC;
REVOKE UPDATE, DELETE ON retrieval_view FROM PUBLIC;
REVOKE UPDATE, DELETE ON view_component FROM PUBLIC;
REVOKE UPDATE, DELETE ON occurrence FROM PUBLIC;
REVOKE UPDATE, DELETE ON occurrence_edge FROM PUBLIC;
REVOKE UPDATE, DELETE ON occurrence_scope FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE ON legacy_hit_mapping FROM PUBLIC;
