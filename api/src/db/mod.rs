// Allow dead_code for planned infrastructure (Qdrant vectors, RLS, etc.)
#![allow(dead_code)]

pub mod postgres;
pub mod models;
pub mod rls;
pub mod rls_client;
pub mod health_pool;

pub use rls_client::RlsClient;
pub use health_pool::HealthPool;

pub use postgres::{PostgresPool, DEFAULT_USER_ID};
