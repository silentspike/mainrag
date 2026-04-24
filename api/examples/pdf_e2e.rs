//! E2E Test mit echter PDF
//! Run: cargo run --example pdf_e2e -- /path/to/file.pdf

use mainrag_api::plugins::{SourcePlugin, pdf::PdfPlugin};
use std::env;
use std::time::Instant;

#[tokio::main]
async fn main() {
    let args: Vec<String> = env::args().collect();

    let pdf_path = if args.len() > 1 {
        args[1].clone()
    } else {
        // Default test PDF
        "/work/mainrag/api/tests/fixtures/test.pdf".to_string()
    };

    let file_size = std::fs::metadata(&pdf_path).map(|m| m.len()).unwrap_or(0);
    println!("Testing PDF extraction with: {}", pdf_path);
    println!("File size: {} bytes ({:.2} MB)", file_size, file_size as f64 / 1024.0 / 1024.0);

    let plugin = PdfPlugin::new();

    // Warmup run
    println!("\nWarmup run...");
    let _ = plugin.sync(&pdf_path).await;

    // Benchmark runs
    println!("\nBenchmark (5 runs):");
    let mut durations = Vec::new();

    for i in 1..=5 {
        let start = Instant::now();
        let result = plugin.sync(&pdf_path).await;
        let elapsed = start.elapsed();
        durations.push(elapsed);

        match &result {
            Ok(r) => println!("  Run {}: {:?} ({} chunks)", i, elapsed, r.files.len()),
            Err(e) => println!("  Run {}: FAILED - {}", i, e),
        }
    }

    // Statistics
    let total_ms: f64 = durations.iter().map(|d| d.as_secs_f64() * 1000.0).sum();
    let avg_ms = total_ms / durations.len() as f64;
    let min_ms = durations.iter().map(|d| d.as_secs_f64() * 1000.0).fold(f64::INFINITY, f64::min);
    let max_ms = durations.iter().map(|d| d.as_secs_f64() * 1000.0).fold(0.0, f64::max);

    // Throughput
    let throughput_mbs = (file_size as f64 / 1024.0 / 1024.0) / (avg_ms / 1000.0);

    println!("\n=== EXTRACTION BENCHMARK RESULTS ===");
    println!("File: {} ({:.2} MB)", pdf_path.split('/').last().unwrap_or(&pdf_path), file_size as f64 / 1024.0 / 1024.0);
    println!("Min: {:.2} ms", min_ms);
    println!("Max: {:.2} ms", max_ms);
    println!("Avg: {:.2} ms", avg_ms);
    println!("Throughput: {:.2} MB/s", throughput_mbs);
    println!("====================================");

    // Show chunk details for last run
    match plugin.sync(&pdf_path).await {
        Ok(result) => {
            println!("\nChunks: {}", result.files.len());
            if !result.files.is_empty() {
                println!("\nFirst 3 chunks:");
                for (i, file) in result.files.iter().take(3).enumerate() {
                    println!("[{:02}] Path: {}", i, file.path);
                    println!("     Size: {} bytes", file.size);
                    let preview: String = file.content.chars().take(100).collect();
                    println!("     Preview: {}...", preview.replace('\n', " "));
                }
            }
        }
        Err(e) => {
            println!("\nExtraction failed: {}", e);
            std::process::exit(1);
        }
    }
}
