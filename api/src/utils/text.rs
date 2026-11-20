//! Shared text utilities for safe path generation and text manipulation

/// Convert arbitrary string to URL-safe slug
///
/// - Lowercase
/// - Replace non-alphanumeric with hyphens
/// - Collapse multiple hyphens
/// - Trim leading/trailing hyphens
///
/// # Examples
///
/// ```
/// use mainrag_api::utils::text::slugify;
/// assert_eq!(slugify("My Report (Final)"), "my-report-final");
/// assert_eq!(slugify("  Spaces  Around  "), "spaces-around");
/// assert_eq!(slugify("file_name.pdf"), "file-name-pdf");
/// ```
pub fn slugify(input: &str) -> String {
    let lowercase = input.to_lowercase();

    let slug: String = lowercase
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() { c } else { '-' })
        .collect();

    // Collapse multiple hyphens and trim
    let mut result = String::new();
    let mut last_was_hyphen = true; // Trim leading hyphens

    for c in slug.chars() {
        if c == '-' {
            if !last_was_hyphen {
                result.push(c);
                last_was_hyphen = true;
            }
        } else {
            result.push(c);
            last_was_hyphen = false;
        }
    }

    // Trim trailing hyphens
    result.trim_end_matches('-').to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_slugify_basic() {
        assert_eq!(slugify("My Report (Final)"), "my-report-final");
        assert_eq!(slugify("  Spaces  Around  "), "spaces-around");
        assert_eq!(slugify("file_name.pdf"), "file-name-pdf");
    }

    #[test]
    fn test_slugify_edge_cases() {
        assert_eq!(slugify("---already---hyphens---"), "already-hyphens");
        assert_eq!(slugify(""), "");
        assert_eq!(slugify("simple"), "simple");
        assert_eq!(slugify("UPPERCASE"), "uppercase");
    }

    #[test]
    fn test_slugify_unicode() {
        // Unicode chars are converted to hyphens
        assert_eq!(slugify("Über die Welt"), "ber-die-welt");
        assert_eq!(slugify("日本語テスト"), "");
    }

    #[test]
    fn test_slugify_special_chars() {
        assert_eq!(slugify("test@file#name"), "test-file-name");
        assert_eq!(slugify("path/to/file"), "path-to-file");
        assert_eq!(slugify("v1.2.3"), "v1-2-3");
    }
}

/// Strip NUL bytes so a string can be stored in a PostgreSQL `text` column.
///
/// PostgreSQL rejects `0x00` inside text values with
/// `invalid byte sequence for encoding "UTF8": 0x00`, which aborts the whole
/// batch insert. Conversation transcripts occasionally carry NUL bytes (binary
/// blobs pasted into tool output, truncated writes), so chunk text is cleaned
/// before it reaches the database. Everything else is left untouched.
///
/// # Examples
///
/// ```
/// use mainrag_api::utils::text::strip_nul_bytes;
/// assert_eq!(strip_nul_bytes("ab\0cd"), "abcd");
/// assert_eq!(strip_nul_bytes("clean"), "clean");
/// ```
pub fn strip_nul_bytes(input: &str) -> String {
    if input.contains('\0') {
        input.replace('\0', "")
    } else {
        input.to_string()
    }
}
