/// K3 Compile-Time Guard: RlsClient.pool must be private.
/// This test MUST fail to compile — proving that handlers cannot
/// bypass RLS by accessing the pool field directly.

use mainrag_api::db::RlsClient;

fn try_access_pool(client: &RlsClient) {
    // This should fail: field `pool` of struct `RlsClient` is private
    let _pool = &client.pool;
}

fn main() {}
