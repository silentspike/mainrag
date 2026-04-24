//! PDF-specific types for structured extraction
//!
//! Types for representing PDF structure (blocks, headings, chunks)
//! and font statistics for relative heading detection.

/// Block type detected from PDF structure
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BlockType {
    Heading1,
    Heading2,
    Heading3,
    Paragraph,
    Table,
    ListItem,
    Unknown,
}

/// A structured block of text from PDF
#[derive(Debug, Clone)]
pub struct PdfBlock {
    pub text: String,
    pub block_type: BlockType,
    pub page_num: usize,
    pub font_size: Option<f32>,
    /// Normalized y-position (0.0 = bottom, 1.0 = top after inversion)
    /// v2.3: MuPDF has origin at bottom-left, so we invert y
    pub y_position: Option<f32>,
}

impl PdfBlock {
    pub fn is_heading(&self) -> bool {
        matches!(
            self.block_type,
            BlockType::Heading1 | BlockType::Heading2 | BlockType::Heading3
        )
    }
}

/// Intermediate chunk for smart chunking
#[derive(Debug, Clone)]
pub struct ProcessedChunk {
    pub text: String,
    pub heading: Option<String>,
    pub start_page: usize,
    pub end_page: usize,
    pub chunk_index: usize,
}

/// Font size statistics for relative heading detection
///
/// Instead of fixed point sizes (24pt, 18pt, 14pt), we use
/// relative thresholds based on the document's own font distribution.
#[derive(Debug, Clone)]
pub struct FontStats {
    pub median: f32,
    pub p90: f32, // 90th percentile
}

impl FontStats {
    /// Calculate from raw font sizes (before heading classification)
    ///
    /// # v2.3 Fixes:
    /// - Filter NaN/Inf values with `is_finite()`
    /// - Fallback for small sets (< 3 values)
    /// - Correct p90 index: `((len-1) * 90) / 100` (prevents OOB)
    pub fn from_sizes(sizes: &[f32]) -> Self {
        // v2.3 FIX: Filter NaN/Inf values
        let valid_sizes: Vec<f32> = sizes
            .iter()
            .copied()
            .filter(|s| s.is_finite() && *s > 0.0)
            .collect();

        // v2.3 FIX: Fallback for small sets (< 3 values)
        if valid_sizes.len() < 3 {
            return Self {
                median: 12.0,
                p90: 14.0,
            };
        }

        let mut sorted = valid_sizes;
        sorted.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));

        let len = sorted.len();
        // v2.2 FIX: Correct index for percentile (prevents OOB)
        let median_idx = len / 2;
        let p90_idx = ((len - 1) * 90) / 100;

        let median = sorted[median_idx];
        let p90 = sorted[p90_idx];

        Self { median, p90 }
    }

    /// Classify block type based on font size relative to document statistics
    ///
    /// Uses hybrid approach: font size + text length + position
    pub fn classify(&self, font_size: f32) -> BlockType {
        if font_size >= self.p90 * 1.3 {
            BlockType::Heading1
        } else if font_size >= self.p90 * 1.1 {
            BlockType::Heading2
        } else if font_size > self.p90 {
            BlockType::Heading3
        } else {
            BlockType::Paragraph
        }
    }
}

impl Default for FontStats {
    fn default() -> Self {
        Self {
            median: 12.0,
            p90: 14.0,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_font_stats_empty() {
        let stats = FontStats::from_sizes(&[]);
        assert_eq!(stats.median, 12.0);
        assert_eq!(stats.p90, 14.0);
    }

    #[test]
    fn test_font_stats_single_element() {
        // v2.2: Must work with 1 element (no OOB!)
        let stats = FontStats::from_sizes(&[12.0]);
        assert_eq!(stats.median, 12.0);
        assert_eq!(stats.p90, 14.0); // Fallback (len < 3)
    }

    #[test]
    fn test_font_stats_two_elements() {
        // v2.3: len < 3 → fallback
        let stats = FontStats::from_sizes(&[10.0, 14.0]);
        assert_eq!(stats.median, 12.0);
        assert_eq!(stats.p90, 14.0);
    }

    #[test]
    fn test_font_stats_ten_elements() {
        // v2.2: p90 at 10 elements = Index 8 (not 9!)
        let sizes: Vec<f32> = (1..=10).map(|i| i as f32).collect();
        let stats = FontStats::from_sizes(&sizes);
        assert_eq!(stats.median, 6.0); // Index 5
        assert_eq!(stats.p90, 9.0); // Index (9*90)/100 = 8
    }

    #[test]
    fn test_font_stats_nan_filter() {
        // v2.3: NaN values should be filtered
        let sizes = vec![10.0, f32::NAN, 12.0, f32::INFINITY, 14.0];
        let stats = FontStats::from_sizes(&sizes);
        // Only 3 valid values: 10.0, 12.0, 14.0
        // median_idx = 3/2 = 1 → sorted[1] = 12.0
        // p90_idx = ((3-1)*90)/100 = 1 → sorted[1] = 12.0
        assert_eq!(stats.median, 12.0);
        assert_eq!(stats.p90, 12.0);
    }

    #[test]
    fn test_font_stats_negative_filter() {
        // v2.3: Negative values should be filtered (> 0.0 check)
        let sizes = vec![-1.0, 10.0, 12.0, 14.0];
        let stats = FontStats::from_sizes(&sizes);
        // Only 3 valid values: 10.0, 12.0, 14.0
        assert_eq!(stats.median, 12.0);
    }

    #[test]
    fn test_classify_heading1() {
        let stats = FontStats {
            median: 12.0,
            p90: 14.0,
        };
        // 14.0 * 1.3 = 18.2, so 20.0 should be H1
        assert_eq!(stats.classify(20.0), BlockType::Heading1);
    }

    #[test]
    fn test_classify_heading2() {
        let stats = FontStats {
            median: 12.0,
            p90: 14.0,
        };
        // 14.0 * 1.1 = 15.4, so 16.0 should be H2
        assert_eq!(stats.classify(16.0), BlockType::Heading2);
    }

    #[test]
    fn test_classify_heading3() {
        let stats = FontStats {
            median: 12.0,
            p90: 14.0,
        };
        // > 14.0 but < 14.0 * 1.1 = 15.4
        assert_eq!(stats.classify(14.5), BlockType::Heading3);
    }

    #[test]
    fn test_classify_paragraph() {
        let stats = FontStats {
            median: 12.0,
            p90: 14.0,
        };
        assert_eq!(stats.classify(12.0), BlockType::Paragraph);
        assert_eq!(stats.classify(14.0), BlockType::Paragraph);
    }

    #[test]
    fn test_pdf_block_is_heading() {
        let heading = PdfBlock {
            text: "Chapter 1".to_string(),
            block_type: BlockType::Heading1,
            page_num: 1,
            font_size: Some(24.0),
            y_position: Some(0.9),
        };
        assert!(heading.is_heading());

        let para = PdfBlock {
            text: "Normal text".to_string(),
            block_type: BlockType::Paragraph,
            page_num: 1,
            font_size: Some(12.0),
            y_position: Some(0.5),
        };
        assert!(!para.is_heading());
    }
}
