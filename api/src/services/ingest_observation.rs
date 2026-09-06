//! Request-local work observations, never inferred from persisted row counts.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

#[derive(Default)]
struct Counters {
    chunker: AtomicU64,
    intelligence_parser: AtomicU64,
}

tokio::task_local! {
    static CURRENT: Arc<Counters>;
}

#[derive(Debug, Default, serde::Serialize)]
pub struct IngestObservation {
    pub chunker_calls: u64,
    pub intelligence_parser_calls: u64,
}

pub async fn observe<T>(work: impl std::future::Future<Output = T>) -> (T, IngestObservation) {
    let counters = Arc::new(Counters::default());
    let result = CURRENT.scope(counters.clone(), work).await;
    (
        result,
        IngestObservation {
            chunker_calls: counters.chunker.load(Ordering::Relaxed),
            intelligence_parser_calls: counters.intelligence_parser.load(Ordering::Relaxed),
        },
    )
}

pub fn chunker_call() {
    let _ = CURRENT.try_with(|counters| counters.chunker.fetch_add(1, Ordering::Relaxed));
}

pub fn intelligence_parser_call() {
    let _ =
        CURRENT.try_with(|counters| counters.intelligence_parser.fetch_add(1, Ordering::Relaxed));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn concurrent_runs_and_unobserved_calls_are_isolated() {
        chunker_call();
        let (((), first), ((), second)) = tokio::join!(
            observe(async {
                chunker_call();
                tokio::task::yield_now().await;
                intelligence_parser_call();
            }),
            observe(async {
                intelligence_parser_call();
                tokio::task::yield_now().await;
                intelligence_parser_call();
            })
        );
        assert_eq!(
            (first.chunker_calls, first.intelligence_parser_calls),
            (1, 1)
        );
        assert_eq!(
            (second.chunker_calls, second.intelligence_parser_calls),
            (0, 2)
        );
    }

    #[tokio::test]
    async fn failed_attempts_are_work_and_nested_scopes_restore_parent() {
        let ((), outer) = observe(async {
            chunker_call();
            let (result, inner) = observe(async {
                intelligence_parser_call();
                Err::<(), _>("synthetic failure")
            })
            .await;
            assert!(result.is_err());
            assert_eq!(inner.intelligence_parser_calls, 1);
            chunker_call();
        })
        .await;
        assert_eq!(
            (outer.chunker_calls, outer.intelligence_parser_calls),
            (2, 0)
        );
    }
}
