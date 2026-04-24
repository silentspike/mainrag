/// K3 Compile-Time Guard: RlsClient.raw_pool() must be pub(crate) only.
/// This test MUST fail to compile — proving that external code (handlers
/// compiled as integration tests) cannot call raw_pool().

use mainrag_api::db::RlsClient;

fn try_raw_pool(client: &RlsClient) {
    // This should fail: method `raw_pool` is not accessible (pub(crate))
    let _pool = client.raw_pool();
}

fn main() {}
