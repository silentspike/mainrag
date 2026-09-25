-- Migration 060: bind active search to the exact pointer-set receipt as well.
-- Preserve the set-based evaluator while making its old entry point private.

DO $$
BEGIN
    IF to_regprocedure(
        'public.storage_v2_search_active_unchecked(text,jsonb,jsonb,bigint,bigint,boolean)'
    ) IS NULL THEN
        ALTER FUNCTION public.storage_v2_search_active(
            TEXT, JSONB, JSONB, BIGINT, BIGINT, BOOLEAN
        ) RENAME TO storage_v2_search_active_unchecked;
    END IF;
END
$$;

REVOKE ALL ON FUNCTION public.storage_v2_search_active_unchecked(
    TEXT, JSONB, JSONB, BIGINT, BIGINT, BOOLEAN
) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.storage_v2_search_active_unchecked(
    TEXT, JSONB, JSONB, BIGINT, BIGINT, BOOLEAN
) FROM mainrag;

CREATE OR REPLACE FUNCTION public.storage_v2_search_active(
    p_manifest_sha256 TEXT,
    p_ast JSONB,
    p_filters JSONB DEFAULT '{}'::JSONB,
    p_limit BIGINT DEFAULT 20,
    p_source_id BIGINT DEFAULT NULL,
    p_include_test BOOLEAN DEFAULT FALSE
) RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
BEGIN
    PERFORM storage_v2_require_complete_active_set(p_manifest_sha256);
    RETURN storage_v2_search_active_unchecked(
        p_manifest_sha256, p_ast, p_filters, p_limit, p_source_id, p_include_test
    );
END
$$;

COMMENT ON FUNCTION public.storage_v2_search_active_unchecked(
    TEXT, JSONB, JSONB, BIGINT, BIGINT, BOOLEAN
) IS 'Private evaluator; callers must use storage_v2_search_active receipt guard';
