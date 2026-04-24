//! K3-FIX2: Compile-fail tests proving RlsClient's pool is inaccessible from handlers.
//!
//! These tests use trybuild to verify that:
//! 1. RlsClient.pool is private (cannot be accessed directly)
//! 2. RlsClient.raw_pool() is pub(crate) (not accessible from integration tests / external code)
//!
//! Run with: cargo test --test trybuild

#[test]
fn compile_fail_rls_guard() {
    let t = trybuild::TestCases::new();
    t.compile_fail("tests/compile_fail/*.rs");
}
