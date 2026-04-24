pub mod embeddings;
pub mod index;
pub mod intelligence;
pub mod parser;
pub mod search;
#[allow(dead_code)]
pub mod watch;
pub mod rerank;
pub mod chunker;
#[allow(dead_code)]
pub mod quality;
pub mod qdrant;
pub mod outbox_worker;
pub mod query_expander;
pub mod compressor;
pub mod circuit_breaker;
pub mod gpu_semaphore;
pub mod domain_profile;

pub use embeddings::TeiClient;
pub use index::IndexService;
pub use search::SearchService;
pub use qdrant::QdrantClient;
pub use rerank::RerankerService;
pub use quality::QualityTier;
pub use outbox_worker::OutboxWorker;
pub use query_expander::QueryExpander;
pub use compressor::{ContextualCompressor, CompressorConfig};
