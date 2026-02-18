-- Migration 024: Remove FORCE ROW LEVEL SECURITY from data tables
--
-- Rationale:
-- - FORCE RLS causes 1-25s FTS latency because PG evaluates the policy per-row
-- - Only LLMs (via API-Keys) and Admin (via JWT) access the system
-- - Application-layer security (TenantContext, Qdrant user_id filter) is the primary access control
-- - Normal RLS remains enabled as defense-in-depth (protects against direct DB access)
-- - The API server connects as table owner (mainrag), so without FORCE it bypasses RLS
-- - This is by design: the Rust API handles authorization, not the DB
--
-- Rollback: migrations/024_rollback_remove_force_rls.sql

BEGIN;

-- Remove FORCE RLS — table owner (mainrag) now bypasses policies
-- Normal RLS still active for non-owner roles (defense-in-depth)
ALTER TABLE sources NO FORCE ROW LEVEL SECURITY;
ALTER TABLE files NO FORCE ROW LEVEL SECURITY;
ALTER TABLE chunks NO FORCE ROW LEVEL SECURITY;

COMMIT;
