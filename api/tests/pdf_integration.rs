//! PDF Integration Tests
//!
//! Tests the PDF plugin with generated test PDFs

use std::io::BufWriter;
use std::path::PathBuf;

/// Generate a test PDF with different font sizes for heading detection
fn generate_test_pdf(path: &PathBuf) {
    use printpdf::*;

    let (doc, page1, layer1) = PdfDocument::new("Test PDF", Mm(210.0), Mm(297.0), "Layer 1");
    let font = doc.add_builtin_font(BuiltinFont::Helvetica).unwrap();
    let layer = doc.get_page(page1).get_layer(layer1);

    // Large heading (24pt) - should be H1
    layer.use_text("Chapter 1: Introduction", 24.0, Mm(20.0), Mm(270.0), &font);

    // Body text (12pt)
    layer.use_text(
        "This is the body text of the document.",
        12.0,
        Mm(20.0),
        Mm(250.0),
        &font,
    );
    layer.use_text(
        "It contains multiple paragraphs for testing.",
        12.0,
        Mm(20.0),
        Mm(240.0),
        &font,
    );
    layer.use_text(
        "Each line tests the text extraction capabilities.",
        12.0,
        Mm(20.0),
        Mm(230.0),
        &font,
    );

    // Medium heading (18pt) - should be H2
    layer.use_text("Section 1.1: Details", 18.0, Mm(20.0), Mm(210.0), &font);

    // More body
    layer.use_text(
        "More detailed information here.",
        12.0,
        Mm(20.0),
        Mm(190.0),
        &font,
    );
    layer.use_text(
        "The PDF plugin should extract this text correctly.",
        12.0,
        Mm(20.0),
        Mm(180.0),
        &font,
    );

    let file = std::fs::File::create(path).unwrap();
    doc.save(&mut BufWriter::new(file)).unwrap();
}

#[tokio::test]
async fn test_pdf_extract_fallback() {
    use mainrag_api::plugins::{pdf::PdfPlugin, SourcePlugin};

    // Create temp directory
    let temp_dir = std::env::temp_dir().join("mainrag_pdf_test");
    std::fs::create_dir_all(&temp_dir).unwrap();

    let pdf_path = temp_dir.join("test.pdf");

    // Generate test PDF
    generate_test_pdf(&pdf_path);

    // Test extraction
    let plugin = PdfPlugin::new();
    let result = plugin.sync(pdf_path.to_str().unwrap()).await;

    // Cleanup
    let _ = std::fs::remove_file(&pdf_path);
    let _ = std::fs::remove_dir(&temp_dir);

    // Verify
    let sync_result = result.expect("PDF extraction should succeed");

    assert!(
        !sync_result.files.is_empty(),
        "Should extract at least one chunk"
    );

    // Check content
    let combined_content: String = sync_result
        .files
        .iter()
        .map(|f| f.content.clone())
        .collect::<Vec<_>>()
        .join(" ");

    assert!(
        combined_content.contains("Chapter 1"),
        "Should contain heading"
    );
    assert!(
        combined_content.contains("body text"),
        "Should contain body text"
    );

    println!(
        "✅ Extracted {} chunks from test PDF",
        sync_result.files.len()
    );
    for file in &sync_result.files {
        println!("  - {}: {} bytes", file.path, file.size);
    }
}

#[tokio::test]
async fn test_pdf_nonexistent() {
    use mainrag_api::plugins::{pdf::PdfPlugin, SourcePlugin};

    let plugin = PdfPlugin::new();
    let result = plugin.sync("/nonexistent/file.pdf").await;

    assert!(result.is_err(), "Should fail for nonexistent file");
    assert!(result.unwrap_err().to_string().contains("not found"));
}

#[tokio::test]
async fn test_pdf_wrong_extension() {
    use mainrag_api::plugins::{pdf::PdfPlugin, SourcePlugin};

    // Create temp file with wrong extension
    let temp_dir = std::env::temp_dir();
    let temp_file = temp_dir.join("test_wrong_ext.txt");
    std::fs::write(&temp_file, "not a pdf").unwrap();

    let plugin = PdfPlugin::new();
    let result = plugin.sync(temp_file.to_str().unwrap()).await;

    // Cleanup
    let _ = std::fs::remove_file(&temp_file);

    assert!(result.is_err(), "Should fail for wrong extension");
    assert!(result.unwrap_err().to_string().contains("Not a PDF"));
}

/// Generate fixture PDF in tests/fixtures/
/// Run: cargo test --test pdf_integration generate_fixture -- --ignored
#[test]
#[ignore]
fn generate_fixture() {
    let manifest_dir = env!("CARGO_MANIFEST_DIR");
    let fixtures_dir = PathBuf::from(manifest_dir).join("tests/fixtures");
    std::fs::create_dir_all(&fixtures_dir).unwrap();

    let pdf_path = fixtures_dir.join("test.pdf");
    generate_test_pdf(&pdf_path);

    println!("Generated fixture: {}", pdf_path.display());
    assert!(pdf_path.exists());
    println!(
        "Size: {} bytes",
        std::fs::metadata(&pdf_path).unwrap().len()
    );
}

/// Benchmark with real-world PDF (1.9MB DrivenByMoss Manual)
/// Run: cargo test --features pdf-mupdf --release --test pdf_integration benchmark_large_pdf -- --ignored --nocapture
#[tokio::test]
#[ignore]
async fn benchmark_large_pdf() {
    use mainrag_api::plugins::{pdf::PdfPlugin, SourcePlugin};
    use std::time::Instant;

    let pdf_path =
        "/work/bitwigs/bitwig-api-docs/DrivenByMoss-Documentation/DrivenByMoss-Manual.pdf";

    if !std::path::Path::new(pdf_path).exists() {
        println!("⚠️  Benchmark PDF not found: {}", pdf_path);
        return;
    }

    let file_size = std::fs::metadata(pdf_path).unwrap().len();
    println!(
        "📄 PDF: {} ({:.2} MB)",
        pdf_path,
        file_size as f64 / 1024.0 / 1024.0
    );

    let plugin = PdfPlugin::new();

    // Warmup
    let warmup = plugin.sync(pdf_path).await.unwrap();
    let total_chars: usize = warmup.files.iter().map(|f| f.content.len()).sum();
    println!(
        "   Chunks: {}, Total chars: {}",
        warmup.files.len(),
        total_chars
    );

    // Benchmark
    let iterations = 5;
    let mut times = Vec::new();

    for _ in 0..iterations {
        let start = Instant::now();
        let _ = plugin.sync(pdf_path).await.unwrap();
        times.push(start.elapsed());
    }

    let avg_ms = times.iter().map(|t| t.as_millis()).sum::<u128>() / iterations as u128;
    let min_ms = times.iter().map(|t| t.as_millis()).min().unwrap();
    let max_ms = times.iter().map(|t| t.as_millis()).max().unwrap();

    // Calculate throughput
    let mb_per_sec = (file_size as f64 / 1024.0 / 1024.0) / (avg_ms as f64 / 1000.0);
    let chars_per_sec = (total_chars as f64) / (avg_ms as f64 / 1000.0);

    println!("\n📊 Benchmark Results (MuPDF):");
    println!(
        "   Min: {}ms | Avg: {}ms | Max: {}ms",
        min_ms, avg_ms, max_ms
    );
    println!(
        "   Throughput: {:.1} MB/s | {:.0} chars/s",
        mb_per_sec, chars_per_sec
    );
    println!(
        "   ~{} pages/s (estimated)",
        (chars_per_sec / 2000.0) as u32
    ); // ~2000 chars/page
}
