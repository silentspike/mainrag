// Allow dead_code for planned infrastructure (Qdrant vectors, RLS, etc.)
#![allow(dead_code)]

pub mod health_pool;
pub mod models;
pub mod postgres;
pub mod rls;
pub mod rls_client;

pub use health_pool::HealthPool;
pub use rls_client::RlsClient;

pub use postgres::{PostgresPool, DEFAULT_USER_ID};
