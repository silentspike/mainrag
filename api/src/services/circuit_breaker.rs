//! Sprint 7.6: Generic Circuit Breaker for service health management
//!
//! Implements a simple circuit breaker pattern with three states:
//! - Closed (normal): requests pass through
//! - Open (after N failures): requests are rejected immediately
//! - HalfOpen (after recovery timeout): one probe request allowed

use parking_lot::Mutex;
use std::sync::atomic::{AtomicU32, AtomicU64, Ordering};
use std::time::Duration;

/// Circuit breaker state
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum CircuitState {
    /// Normal operation — all requests pass through
    Closed,
    /// Service is down — requests are rejected immediately
    Open,
    /// Recovery probe — one request allowed to test if service recovered
    HalfOpen,
}

impl CircuitState {
    pub fn as_gauge_value(&self) -> f64 {
        match self {
            CircuitState::Closed => 0.0,
            CircuitState::Open => 1.0,
            CircuitState::HalfOpen => 2.0,
        }
    }
}

/// Generic circuit breaker for any service (TEI embed, TEI rerank, Qdrant)
pub struct CircuitBreaker {
    name: String,
    /// Number of consecutive failures before opening
    failure_threshold: u32,
    /// Duration to wait before attempting recovery (half-open)
    recovery_timeout: Duration,
    /// Current consecutive failure count
    consecutive_failures: AtomicU32,
    /// Timestamp when circuit was opened (epoch millis, 0 = not open)
    opened_at_millis: AtomicU64,
    /// Lock for state transitions to prevent races
    state_lock: Mutex<()>,
}

impl CircuitBreaker {
    pub fn new(name: &str, failure_threshold: u32, recovery_timeout: Duration) -> Self {
        Self {
            name: name.to_string(),
            failure_threshold,
            recovery_timeout,
            consecutive_failures: AtomicU32::new(0),
            opened_at_millis: AtomicU64::new(0),
            state_lock: Mutex::new(()),
        }
    }

    /// Get current circuit state
    pub fn state(&self) -> CircuitState {
        let failures = self.consecutive_failures.load(Ordering::Relaxed);
        let opened_at = self.opened_at_millis.load(Ordering::Relaxed);

        if failures < self.failure_threshold {
            return CircuitState::Closed;
        }

        if opened_at == 0 {
            return CircuitState::Closed;
        }

        // Check if recovery timeout has elapsed
        let elapsed_millis = epoch_millis().saturating_sub(opened_at);
        if elapsed_millis >= self.recovery_timeout.as_millis() as u64 {
            CircuitState::HalfOpen
        } else {
            CircuitState::Open
        }
    }

    /// Check if a request should be allowed through
    pub fn should_allow(&self) -> bool {
        matches!(self.state(), CircuitState::Closed | CircuitState::HalfOpen)
    }

    /// Record a successful request — resets the circuit breaker
    pub fn record_success(&self) {
        let _lock = self.state_lock.lock();
        let prev_failures = self.consecutive_failures.swap(0, Ordering::Relaxed);
        self.opened_at_millis.store(0, Ordering::Relaxed);
        if prev_failures >= self.failure_threshold {
            tracing::info!(
                service = %self.name,
                "Circuit breaker recovered (was open after {} failures)",
                prev_failures
            );
            metrics::gauge!("mainrag_circuit_breaker_state", "service" => self.name.clone())
                .set(CircuitState::Closed.as_gauge_value());
        }
    }

    /// Record a failed request — may open the circuit
    pub fn record_failure(&self) {
        let _lock = self.state_lock.lock();
        let failures = self.consecutive_failures.fetch_add(1, Ordering::Relaxed) + 1;

        if failures == self.failure_threshold {
            self.opened_at_millis
                .store(epoch_millis(), Ordering::Relaxed);
            tracing::warn!(
                service = %self.name,
                failures = failures,
                recovery_timeout_s = self.recovery_timeout.as_secs(),
                "Circuit breaker OPENED — service marked as down"
            );
            metrics::gauge!("mainrag_circuit_breaker_state", "service" => self.name.clone())
                .set(CircuitState::Open.as_gauge_value());
        }
    }

    /// Name of the service this circuit breaker protects
    #[allow(dead_code)]
    pub fn name(&self) -> &str {
        &self.name
    }
}

/// Get current epoch time in milliseconds
fn epoch_millis() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_circuit_breaker_starts_closed() {
        let cb = CircuitBreaker::new("test", 3, Duration::from_secs(30));
        assert_eq!(cb.state(), CircuitState::Closed);
        assert!(cb.should_allow());
    }

    #[test]
    fn test_circuit_breaker_opens_after_threshold() {
        let cb = CircuitBreaker::new("test", 3, Duration::from_secs(30));
        cb.record_failure();
        cb.record_failure();
        assert_eq!(cb.state(), CircuitState::Closed);
        cb.record_failure();
        assert_eq!(cb.state(), CircuitState::Open);
        assert!(!cb.should_allow());
    }

    #[test]
    fn test_circuit_breaker_success_resets() {
        let cb = CircuitBreaker::new("test", 3, Duration::from_secs(30));
        cb.record_failure();
        cb.record_failure();
        cb.record_success();
        assert_eq!(cb.state(), CircuitState::Closed);
        assert!(cb.should_allow());
    }
}
