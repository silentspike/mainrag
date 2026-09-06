//! Application-level source I/O accounting. These are not device I/O bytes.

use std::pin::Pin;
use std::sync::atomic::{AtomicU64, Ordering};
use std::task::{Context, Poll};
use tokio::io::{AsyncRead, ReadBuf};

#[derive(Debug)]
pub struct ReadAccounting {
    bytes: AtomicU64,
    scope: &'static str,
}

impl Default for ReadAccounting {
    fn default() -> Self {
        Self {
            bytes: AtomicU64::new(0),
            scope: "deferred_source_loads",
        }
    }
}

impl ReadAccounting {
    pub fn filesystem_adapter() -> Self {
        Self {
            bytes: AtomicU64::new(0),
            scope: "filesystem_adapter",
        }
    }

    fn record(&self, bytes: u64) {
        if bytes != 0 {
            self.bytes.fetch_add(bytes, Ordering::Relaxed);
            metrics::counter!("storage_v2_source_application_read_bytes_total", "scope" => self.scope).increment(bytes);
        }
    }

    pub fn bytes(&self) -> u64 {
        self.bytes.load(Ordering::Relaxed)
    }

    pub fn reader<R>(&self, inner: R) -> AccountedReader<'_, R> {
        AccountedReader {
            inner,
            accounting: Some(self),
        }
    }
}

pub struct AccountedReader<'a, R> {
    inner: R,
    accounting: Option<&'a ReadAccounting>,
}

impl<'a, R> AccountedReader<'a, R> {
    pub fn optional(inner: R, accounting: Option<&'a ReadAccounting>) -> Self {
        Self { inner, accounting }
    }
}

impl<R: std::io::Read> std::io::Read for AccountedReader<'_, R> {
    fn read(&mut self, out: &mut [u8]) -> std::io::Result<usize> {
        let bytes = self.inner.read(out)?;
        if let Some(accounting) = self.accounting {
            accounting.record(bytes as u64);
        }
        Ok(bytes)
    }
}

impl<R: std::io::Seek> std::io::Seek for AccountedReader<'_, R> {
    fn seek(&mut self, position: std::io::SeekFrom) -> std::io::Result<u64> {
        self.inner.seek(position)
    }
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
        if let Some(accounting) = this.accounting {
            accounting.record(bytes);
        }
        result
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::AsyncReadExt;

    #[test]
    fn synchronous_seek_and_failed_exact_read_preserve_actual_work() {
        let accounting = ReadAccounting::filesystem_adapter();
        let mut reader = accounting.reader(std::io::Cursor::new(b"abcdef"));
        std::io::Seek::seek(&mut reader, std::io::SeekFrom::Start(4)).unwrap();
        assert_eq!(accounting.bytes(), 0);
        let mut out = [0_u8; 4];
        assert!(std::io::Read::read_exact(&mut reader, &mut out).is_err());
        assert_eq!(accounting.bytes(), 2);
    }

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
