//! PDF plugin with feature-flag based implementation selection
//!
//! # Features
//! - `pdf-mupdf`: Use MuPDF for structured extraction with heading detection
//!   (requires libmupdf-dev system dependency)
//! - Default: Use pdf-extract for basic text extraction (pure Rust, no deps)
//!
//! # Usage
//! ```bash
//! # Default build (pdf-extract fallback)
//! cargo build
//!
//! # With MuPDF support (requires libmupdf-dev)
//! cargo build --features pdf-mupdf
//! ```

#[cfg(feature = "pdf-mupdf")]
mod mupdf_impl;

#[cfg(not(feature = "pdf-mupdf"))]
mod extract_impl;

// Re-export the active implementation
#[cfg(feature = "pdf-mupdf")]
pub use mupdf_impl::PdfPlugin;

#[cfg(not(feature = "pdf-mupdf"))]
pub use extract_impl::PdfPlugin;

// Shared constants for both implementations
pub const MAX_PDF_SIZE: u64 = 50 * 1024 * 1024; // 50MB
pub const MIN_TEXT_LENGTH: usize = 50;

/// Returns the name of the active PDF backend
#[cfg(feature = "pdf-mupdf")]
pub const fn backend_name() -> &'static str {
    "MuPDF"
}

#[cfg(not(feature = "pdf-mupdf"))]
pub const fn backend_name() -> &'static str {
    "pdf-extract fallback"
}

/// Log which PDF backend is active (call at startup)
pub fn log_backend_info() {
    tracing::info!(backend = backend_name(), "PDF plugin initialized");
}
