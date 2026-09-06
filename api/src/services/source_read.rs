//! Application-level source I/O accounting. These are not device I/O bytes.

use std::pin::Pin;
use std::sync::atomic::{AtomicU64, Ordering};
use std::task::{Context, Poll};
use tokio::io::{AsyncRead, ReadBuf};

#[derive(Debug, Default)]
pub struct ReadAccounting {
    bytes: AtomicU64,
}

impl ReadAccounting {
    pub fn bytes(&self) -> u64 {
        self.bytes.load(Ordering::Relaxed)
    }

    pub fn reader<R>(&self, inner: R) -> AccountedReader<'_, R> {
        AccountedReader {
            inner,
            accounting: self,
        }
    }
}

pub struct AccountedReader<'a, R> {
    inner: R,
    accounting: &'a ReadAccounting,
}

impl<R: AsyncRead + Unpin> AsyncRead for AccountedReader<'_, R> {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buffer: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        let this = self.get_mut();
        let before = buffer.filled().len();
        let result = Pin::new(&mut this.inner).poll_read(cx, buffer);
        let bytes = (buffer.filled().len() - before) as u64;
        if bytes != 0 {
            this.accounting.bytes.fetch_add(bytes, Ordering::Relaxed);
            metrics::counter!("storage_v2_source_application_read_bytes_total", "scope" => "deferred_source_loads").increment(bytes);
        }
        result
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::AsyncReadExt;

    #[tokio::test]
    async fn repeated_reads_are_work_not_unique_input() {
        let accounting = ReadAccounting::default();
        for _ in 0..3 {
            let mut result = Vec::new();
            accounting
                .reader(&b"content"[..])
                .read_to_end(&mut result)
                .await
                .unwrap();
            assert_eq!(result, b"content");
        }
        assert_eq!(accounting.bytes(), 21);
    }

    #[tokio::test]
    async fn range_limit_counts_only_consumed_bytes() {
        let accounting = ReadAccounting::default();
        let mut result = Vec::new();
        accounting
            .reader(&b"abcdef"[..])
            .take(3)
            .read_to_end(&mut result)
            .await
            .unwrap();
        assert_eq!(result, b"abc");
        assert_eq!(accounting.bytes(), 3);
    }

    #[tokio::test]
    async fn eof_and_preexisting_output_do_not_inflate_reads() {
        let accounting = ReadAccounting::default();
        let mut result = b"already present".to_vec();
        accounting
            .reader(&b""[..])
            .read_to_end(&mut result)
            .await
            .unwrap();
        assert_eq!(accounting.bytes(), 0);
    }

    struct FailsAfterRead(bool);

    impl AsyncRead for FailsAfterRead {
        fn poll_read(
            mut self: Pin<&mut Self>,
            _: &mut Context<'_>,
            out: &mut ReadBuf<'_>,
        ) -> Poll<std::io::Result<()>> {
            if self.0 {
                Poll::Ready(Err(std::io::Error::other("synthetic read failure")))
            } else {
                self.0 = true;
                out.put_slice(b"abc");
                Poll::Ready(Ok(()))
            }
        }
    }

    #[tokio::test]
    async fn failed_read_retains_bytes_already_delivered() {
        let accounting = ReadAccounting::default();
        let mut result = Vec::new();
        assert!(accounting
            .reader(FailsAfterRead(false))
            .read_to_end(&mut result)
            .await
            .is_err());
        assert_eq!(result, b"abc");
        assert_eq!(accounting.bytes(), 3);
    }
}
