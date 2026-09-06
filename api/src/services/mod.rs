pub mod chunker;
pub mod circuit_breaker;
pub mod compressor;
#[allow(dead_code)]
pub mod content_graph;
#[allow(dead_code)]
pub mod content_store;
pub mod domain_profile;
pub mod embeddings;
#[cfg(feature = "storage-v2-shadow-ingest")]
#[allow(dead_code)]
pub mod generation_ingest;
pub mod gpu_semaphore;
pub mod index;
pub mod ingest_observation;
pub mod intelligence;
#[cfg(feature = "storage-v2-intelligence")]
#[allow(dead_code)]
pub mod intelligence_v2;
pub mod outbox_worker;
#[allow(dead_code)]
pub mod pack_maintenance;
pub mod parser;
pub mod qdrant;
#[allow(dead_code)]
pub mod quality;
pub mod query_expander;
pub mod rerank;
#[cfg(feature = "storage-v2-retrieval")]
pub mod retrieval_v2;
pub mod search;
#[cfg(feature = "storage-v2-retrieval")]
pub mod shadow_slice;
#[allow(dead_code)]
pub mod source_read;
#[allow(dead_code)]
pub mod watch;

pub use compressor::{CompressorConfig, ContextualCompressor};
pub use embeddings::TeiClient;
pub use index::IndexService;
pub use outbox_worker::OutboxWorker;
pub use qdrant::QdrantClient;
pub use quality::QualityTier;
pub use query_expander::QueryExpander;
pub use rerank::RerankerService;
pub use search::SearchService;
