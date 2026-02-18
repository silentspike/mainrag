-- Sprint 4.8: Mark user_can_access_source as STABLE
-- This allows PostgreSQL to optimize RLS policy evaluation within a single statement
-- (function is called once per row in WHERE clauses, STABLE means result is constant
-- for same inputs within the same statement/transaction).
ALTER FUNCTION user_can_access_source(UUID, BIGINT, TEXT) STABLE;
