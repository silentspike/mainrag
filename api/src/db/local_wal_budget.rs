//! Optional local WAL backpressure between durable build transactions.
//! This observes the local queue only; it never contacts an archive repository.

use anyhow::{ensure, Context, Result};
use std::future::Future;
use std::time::{Duration, Instant};
use tokio_postgres::GenericClient;

#[derive(Debug, Clone)]
pub(super) struct LocalWalBudget {
    high_bytes: i64,
    low_bytes: i64,
    maximum_wait: Duration,
    poll_interval: Duration,
}

impl LocalWalBudget {
    #[cfg(test)]
    pub(crate) fn fixture_policy() -> Self {
        Self {
            high_bytes: 5,
            low_bytes: 2,
            maximum_wait: Duration::from_secs(10),
            poll_interval: Duration::from_secs(1),
        }
    }
    pub(super) fn from_env() -> Result<Option<Self>> {
        Self::parse(
            std::env::var("MAINRAG_V2_LOCAL_WAL_HIGH_BYTES")
                .ok()
                .as_deref(),
            std::env::var("MAINRAG_V2_LOCAL_WAL_LOW_BYTES")
                .ok()
                .as_deref(),
            std::env::var("MAINRAG_V2_LOCAL_WAL_MAX_WAIT_SECONDS")
                .ok()
                .as_deref(),
        )
    }

    fn parse(high: Option<&str>, low: Option<&str>, wait: Option<&str>) -> Result<Option<Self>> {
        if high.is_none() && low.is_none() && wait.is_none() {
            return Ok(None);
        }
        let high_bytes = high
            .context("local WAL high threshold is required")?
            .parse::<i64>()
            .context("local WAL high threshold must be an integer")?;
        let low_bytes = low
            .context("local WAL low threshold is required")?
            .parse::<i64>()
            .context("local WAL low threshold must be an integer")?;
        let seconds = wait
            .unwrap_or("7200")
            .parse::<u64>()
            .context("local WAL maximum wait must be an integer")?;
        ensure!(
            0 < low_bytes && low_bytes < high_bytes,
            "local WAL thresholds must satisfy 0 < low < high"
        );
        ensure!(
            (1..=86_400).contains(&seconds),
            "local WAL maximum wait must be between 1 and 86400 seconds"
        );
        Ok(Some(Self {
            high_bytes,
            low_bytes,
            maximum_wait: Duration::from_secs(seconds),
            poll_interval: Duration::from_secs(5),
        }))
    }

    pub(super) async fn wait<C, W>(&self, client: &C, waiting: W) -> Result<()>
    where
        C: GenericClient + Sync,
        W: FnMut(bool) -> Result<()>,
    {
        self.wait_with_probe(
            || async {
                Ok(client
                    .query_one("SELECT public.storage_v2_local_wal_ready_bytes()", &[])
                    .await
                    .context("local WAL backlog observation failed")?
                    .get::<_, i64>(0))
            },
            waiting,
        )
        .await
    }

    async fn wait_with_probe<P, F, W>(&self, mut probe: P, mut waiting: W) -> Result<()>
    where
        P: FnMut() -> F,
        F: Future<Output = Result<i64>>,
        W: FnMut(bool) -> Result<()>,
    {
        let first = probe().await?;
        ensure!(first >= 0, "local WAL backlog observation is negative");
        if first < self.high_bytes {
            return Ok(());
        }
        let started = Instant::now();
        waiting(true)?;
        loop {
            ensure!(
                started.elapsed() < self.maximum_wait,
                "local WAL budget wait timed out after a durable checkpoint"
            );
            tokio::time::sleep(
                self.poll_interval
                    .min(self.maximum_wait.saturating_sub(started.elapsed())),
            )
            .await;
            let ready = probe().await?;
            ensure!(ready >= 0, "local WAL backlog observation is negative");
            if ready <= self.low_bytes {
                waiting(false)?;
                return Ok(());
            }
            // Refresh progress while waiting without changing any item counter.
            waiting(true)?;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;

    #[test]
    fn local_wal_configuration_is_explicit_and_bounded() {
        assert!(LocalWalBudget::parse(None, None, None).unwrap().is_none());
        for args in [
            (Some("10"), None, None),
            (None, Some("5"), None),
            (Some("10"), Some("10"), None),
            (Some("10"), Some("0"), None),
            (Some("-1"), Some("1"), None),
            (Some("10"), Some("5"), Some("0")),
            (Some("10"), Some("5"), Some("86401")),
        ] {
            assert!(LocalWalBudget::parse(args.0, args.1, args.2).is_err());
        }
        assert!(LocalWalBudget::parse(Some("10"), Some("5"), None)
            .unwrap()
            .is_some());
    }

    fn policy() -> LocalWalBudget {
        LocalWalBudget {
            high_bytes: 10,
            low_bytes: 5,
            maximum_wait: Duration::from_millis(50),
            poll_interval: Duration::from_millis(1),
        }
    }

    #[tokio::test]
    async fn local_wal_hysteresis_preserves_a_wait_until_the_low_threshold() {
        let mut values = VecDeque::from([10, 8, 6, 5]);
        let mut events = Vec::new();
        policy()
            .wait_with_probe(
                || std::future::ready(Ok(values.pop_front().unwrap())),
                |paused| {
                    events.push(paused);
                    Ok(())
                },
            )
            .await
            .unwrap();
        assert_eq!(events, [true, true, true, false]);
        assert!(values.is_empty());
        let mut called = false;
        policy()
            .wait_with_probe(
                || std::future::ready(Ok(9)),
                |_| {
                    called = true;
                    Ok(())
                },
            )
            .await
            .unwrap();
        assert!(!called);
    }

    #[tokio::test]
    async fn local_wal_observation_failures_and_timeout_do_not_resume() {
        let mut events = Vec::new();
        let mut count = 0;
        let result = policy()
            .wait_with_probe(
                || {
                    count += 1;
                    std::future::ready(if count == 1 {
                        Ok(10)
                    } else {
                        Err(anyhow::anyhow!("probe failed"))
                    })
                },
                |paused| {
                    events.push(paused);
                    Ok(())
                },
            )
            .await;
        assert!(result.is_err());
        assert_eq!(events, [true]);
        let mut short = policy();
        short.maximum_wait = Duration::from_millis(3);
        events.clear();
        let result = short
            .wait_with_probe(
                || std::future::ready(Ok(10)),
                |paused| {
                    events.push(paused);
                    Ok(())
                },
            )
            .await;
        assert!(result.unwrap_err().to_string().contains("timed out"));
        assert!(events.iter().all(|paused| *paused));
        assert!(policy()
            .wait_with_probe(|| std::future::ready(Ok(-1)), |_| Ok(()))
            .await
            .is_err());
    }
}
