//! PDF Processing Benchmark
//!
//! Run: cargo test --features pdf-mupdf --release --test pdf_benchmark -- --ignored --nocapture

use std::time::Instant;

/// Benchmark MuPDF PDF processing with real-world PDF
#[tokio::test]
#[ignore]
async fn benchmark_mupdf_throughput() {
    use mainrag_api::plugins::{pdf::PdfPlugin, SourcePlugin};

    let pdf_path =
        "/work/bitwigs/bitwig-api-docs/DrivenByMoss-Documentation/DrivenByMoss-Manual.pdf";

    if !std::path::Path::new(pdf_path).exists() {
        println!("⚠️  Benchmark PDF not found");
        return;
    }

    let file_size = std::fs::metadata(pdf_path).unwrap().len();
    let file_size_mb = file_size as f64 / 1024.0 / 1024.0;

    println!("\n═══════════════════════════════════════════════════════════════");
    println!("  MuPDF PDF Processing Benchmark");
    println!("═══════════════════════════════════════════════════════════════");
    println!("  File: DrivenByMoss-Manual.pdf");
    println!("  Size: {:.2} MB ({} bytes)", file_size_mb, file_size);
    println!("───────────────────────────────────────────────────────────────\n");

    let plugin = PdfPlugin::new();

    // Warmup run
    print!("  Warmup run... ");
    let warmup_start = Instant::now();
    let warmup = plugin.sync(pdf_path).await.unwrap();
    let warmup_time = warmup_start.elapsed();
    println!("done ({:?})", warmup_time);

    let total_chars: usize = warmup.files.iter().map(|f| f.content.len()).sum();
    let chunk_count = warmup.files.len();

    println!(
        "  Output: {} chunks, {} chars total\n",
        chunk_count, total_chars
    );

    // Benchmark runs
    let iterations = 10;
    let mut times = Vec::with_capacity(iterations);

    println!("  Running {} iterations...", iterations);
    for i in 0..iterations {
        let start = Instant::now();
        let _ = plugin.sync(pdf_path).await.unwrap();
        let elapsed = start.elapsed();
        times.push(elapsed);
        println!("    [{}/{}] {:?}", i + 1, iterations, elapsed);
    }

    // Calculate statistics
    let total_ms: u128 = times.iter().map(|t| t.as_millis()).sum();
    let avg_ms = total_ms / iterations as u128;
    let min_ms = times.iter().map(|t| t.as_millis()).min().unwrap();
    let max_ms = times.iter().map(|t| t.as_millis()).max().unwrap();

    // Sort for percentiles
    let mut sorted: Vec<u128> = times.iter().map(|t| t.as_millis()).collect();
    sorted.sort();
    let p50 = sorted[iterations / 2];
    let p95 = sorted[(iterations as f64 * 0.95) as usize];

    // Calculate throughput
    let avg_sec = avg_ms as f64 / 1000.0;
    let mb_per_sec = file_size_mb / avg_sec;
    let chars_per_sec = total_chars as f64 / avg_sec;
    let pages_per_sec = (chars_per_sec / 2000.0) as u32; // ~2000 chars/page estimate

    println!("\n═══════════════════════════════════════════════════════════════");
    println!("  RESULTS");
    println!("═══════════════════════════════════════════════════════════════");
    println!("  Latency:");
    println!("    Min:    {} ms", min_ms);
    println!("    Avg:    {} ms", avg_ms);
    println!("    P50:    {} ms", p50);
    println!("    P95:    {} ms", p95);
    println!("    Max:    {} ms", max_ms);
    println!();
    println!("  Throughput:");
    println!("    MB/s:       {:.2}", mb_per_sec);
    println!("    chars/s:    {:.0}", chars_per_sec);
    println!("    pages/s:    ~{} (estimated)", pages_per_sec);
    println!("═══════════════════════════════════════════════════════════════\n");
}
