//! Text cleanup functions for PDF extraction
//!
//! Handles common PDF extraction artifacts:
//! - Dehyphenation (rejoining split words)
//! - Ligature normalization (PDF typography artifacts)
//! - Whitespace normalization

use once_cell::sync::Lazy;
use regex::Regex;

static HYPHEN_PATTERN: Lazy<Regex> = Lazy::new(|| {
    // Match word-hyphen-newline-word pattern
    Regex::new(r"(\w)-\n(\w)").unwrap()
});

static MULTI_SPACE: Lazy<Regex> = Lazy::new(|| {
    // Match multiple spaces or tabs
    Regex::new(r"[ \t]+").unwrap()
});

static MULTI_NEWLINE: Lazy<Regex> = Lazy::new(|| {
    // Match 3+ consecutive newlines
    Regex::new(r"\n{3,}").unwrap()
});

/// Dehyphenate split words: "exam-\nple" → "example"
///
/// PDF documents often split words at line breaks with hyphens.
/// This function rejoins them.
pub fn dehyphenate(text: &str) -> String {
    HYPHEN_PATTERN.replace_all(text, "$1$2").to_string()
}

/// Normalize ligatures to ASCII
///
/// PDF documents often use typographic ligatures that can
/// interfere with text search and processing.
pub fn normalize_ligatures(text: &str) -> String {
    text.replace("ﬁ", "fi")
        .replace("ﬂ", "fl")
        .replace("ﬀ", "ff")
        .replace("ﬃ", "ffi")
        .replace("ﬄ", "ffl")
        .replace("Ĳ", "IJ")
        .replace("ĳ", "ij")
        .replace("œ", "oe")
        .replace("Œ", "OE")
        .replace("æ", "ae")
        .replace("Æ", "AE")
}

/// Normalize whitespace
///
/// - Collapse multiple spaces/tabs to single space
/// - Collapse 3+ newlines to double newline (preserve paragraph breaks)
/// - Trim leading/trailing whitespace
pub fn normalize_whitespace(text: &str) -> String {
    let text = MULTI_SPACE.replace_all(text, " ");
    let text = MULTI_NEWLINE.replace_all(&text, "\n\n");
    text.trim().to_string()
}

/// Full cleanup pipeline for PDF text
///
/// Applies all cleanup functions in order:
/// 1. Normalize ligatures (character-level)
/// 2. Dehyphenate (word-level)
/// 3. Normalize whitespace (structure-level)
pub fn cleanup_pdf_text(text: &str) -> String {
    let text = normalize_ligatures(text);
    let text = dehyphenate(&text);
    normalize_whitespace(&text)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_dehyphenate_basic() {
        assert_eq!(dehyphenate("exam-\nple"), "example");
        assert_eq!(dehyphenate("no-hyphen"), "no-hyphen");
        // Multiple hyphenations are all joined
        assert_eq!(dehyphenate("multi-\nple-\nwords"), "multiplewords");
    }

    #[test]
    fn test_dehyphenate_no_change() {
        // Should not change hyphens without newline
        assert_eq!(dehyphenate("well-known"), "well-known");
        // Should not change newlines without hyphen
        assert_eq!(dehyphenate("line\nbreak"), "line\nbreak");
    }

    #[test]
    fn test_ligatures_basic() {
        assert_eq!(normalize_ligatures("ﬁle ﬂow"), "file flow");
        assert_eq!(normalize_ligatures("eﬀect"), "effect");
        assert_eq!(normalize_ligatures("aﬃrm"), "affirm");
        assert_eq!(normalize_ligatures("muﬄe"), "muffle");
    }

    #[test]
    fn test_ligatures_extended() {
        assert_eq!(normalize_ligatures("Ĳsselmeer"), "IJsselmeer");
        assert_eq!(normalize_ligatures("œuvre"), "oeuvre");
        assert_eq!(normalize_ligatures("Ægypt"), "AEgypt");
    }

    #[test]
    fn test_whitespace_spaces() {
        assert_eq!(normalize_whitespace("too   many    spaces"), "too many spaces");
        assert_eq!(normalize_whitespace("\t\ttabs"), "tabs");
        assert_eq!(normalize_whitespace("mixed   \t  ws"), "mixed ws");
    }

    #[test]
    fn test_whitespace_newlines() {
        assert_eq!(normalize_whitespace("line\n\n\n\nmany"), "line\n\nmany");
        assert_eq!(normalize_whitespace("a\n\n\n\n\nb"), "a\n\nb");
        // Double newline preserved (paragraph break)
        assert_eq!(normalize_whitespace("a\n\nb"), "a\n\nb");
    }

    #[test]
    fn test_whitespace_trim() {
        assert_eq!(normalize_whitespace("  leading"), "leading");
        assert_eq!(normalize_whitespace("trailing  "), "trailing");
        assert_eq!(normalize_whitespace("  both  "), "both");
    }

    #[test]
    fn test_full_pipeline() {
        let input = "  The ﬁle con-\ntains  many   lig-\natures.  \n\n\n\n  ";
        let _expected = "The file contains many ligatures.\n\n";
        // Note: trailing newlines from collapse, then trimmed
        let result = cleanup_pdf_text(input);
        assert!(result.contains("file contains"));
        assert!(result.contains("many ligatures"));
        assert!(!result.contains("ﬁ"));
        assert!(!result.contains("-\n"));
    }

    #[test]
    fn test_cleanup_empty() {
        assert_eq!(cleanup_pdf_text(""), "");
        assert_eq!(cleanup_pdf_text("   "), "");
    }

    #[test]
    fn test_cleanup_preserves_structure() {
        let input = "Heading\n\nParagraph one.\n\nParagraph two.";
        let result = cleanup_pdf_text(input);
        assert!(result.contains("Heading\n\n"));
        assert!(result.contains("Paragraph one.\n\n"));
    }
}
