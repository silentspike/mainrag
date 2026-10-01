//! Explicit PostgreSQL-safe text projection; original bodies remain byte exact.

use std::borrow::Cow;
use std::path::Path;

use anyhow::{ensure, Context, Result};

#[cfg(feature = "storage-v2-shadow-ingest")]
pub const KIND: &str = "utf8-nul-space-v1";
#[cfg(feature = "storage-v2-shadow-ingest")]
pub const PROFILE_SUFFIX: &str = ".text-utf8-nul-space-v1";
#[cfg(feature = "storage-v2-shadow-ingest")]
pub const ANALYSIS_PROFILE: &str = "mainrag.generic-structural.v1.utf8-nul-space-v1";

pub struct ProjectedText<'a> {
    pub text: Cow<'a, str>,
    pub nul_count: usize,
}

/// A single-byte separator preserves both byte and character locators. Invalid
/// UTF-8 and undeclared NUL input fail; no lossy decoder or source rewrite occurs.
pub fn project<'a>(bytes: &'a [u8], path: &Path, enabled: bool) -> Result<ProjectedText<'a>> {
    let original = std::str::from_utf8(bytes).context("source item is not UTF-8")?;
    let nul_count = bytes.iter().filter(|&&byte| byte == 0).count();
    if nul_count == 0 {
        return Ok(ProjectedText {
            text: Cow::Borrowed(original),
            nul_count,
        });
    }
    ensure!(
        enabled
            && path
                .extension()
                .and_then(|extension| extension.to_str())
                .is_some_and(|extension| extension.eq_ignore_ascii_case("jsonl")
                    || extension.eq_ignore_ascii_case("json")),
        "source NUL bytes require the declared conversation text projection"
    );
    Ok(ProjectedText {
        text: Cow::Owned(original.replace('\0', " ")),
        nul_count,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn projection_keeps_original_bytes_and_unicode_locators() {
        let raw = "é\0alpha🙂\0beta\n".as_bytes();
        let before = raw.to_vec();
        let projected = project(raw, Path::new("fixture.jsonl"), true).unwrap();
        assert_eq!(projected.text, "é alpha🙂 beta\n");
        assert_eq!(projected.nul_count, 2);
        assert_eq!(projected.text.len(), raw.len());
        assert_eq!(
            projected.text.chars().count(),
            std::str::from_utf8(raw).unwrap().chars().count()
        );
        assert_eq!(
            projected.text.find("beta"),
            std::str::from_utf8(raw).unwrap().find("beta")
        );
        assert_eq!(raw, before);
        assert!(project(raw, Path::new("fixture.jsonl"), false).is_err());
        assert!(project(raw, Path::new("fixture.txt"), true).is_err());
        assert!(project(b"bad\xff\0", Path::new("fixture.jsonl"), true).is_err());
        assert!(matches!(
            project(b"unchanged", Path::new("fixture.rs"), false)
                .unwrap()
                .text,
            Cow::Borrowed(_)
        ));
    }
}
