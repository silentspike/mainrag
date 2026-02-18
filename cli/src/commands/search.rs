use crate::client::ApiClient;
use serde_json::json;

pub async fn run(
    client: &ApiClient,
    query: &str,
    mode: &str,
    limit: u32,
    offset: u32,
    source: Option<&str>,
    json_output: bool,
) -> anyhow::Result<()> {
    // Fetch extra results to handle offset client-side (API doesn't support offset yet)
    let fetch_limit = limit + offset;
    let all_results = client.search(query, mode, fetch_limit, source).await?;

    // Apply offset client-side (but keep original total for pagination display)
    let results = crate::client::api::SearchResponse {
        llm_context: all_results.llm_context.clone(),
        results: all_results.results.into_iter().skip(offset as usize).collect(),
        total: all_results.total, // Keep original total - don't subtract offset
        took_ms: all_results.took_ms,
        quality_tier: all_results.quality_tier,
        reranked: all_results.reranked,
    };

    if json_output {
        println!(
            "{}",
            serde_json::to_string_pretty(&json!({
                "llm_context": results.llm_context,
                "results": results.results,
                "total": results.total,
                "took_ms": results.took_ms,
            }))?
        );
        return Ok(());
    }

    // LLM-optimized plain text output (minimal tokens)
    if results.results.is_empty() {
        println!("No results for \"{}\"", query);
        return Ok(());
    }

    let total = all_results.total;
    let showing = results.results.len();

    // Minimal header - llm_context from API explains the format
    if let Some(ref ctx) = results.llm_context {
        println!("{}", ctx);
    }
    println!();

    // Ultra-compact results: location [source] score + snippet
    for (idx, result) in results.results.iter().enumerate() {
        // Use API's pre-computed location (unescaped, compact)
        let loc = result.location.as_deref()
            .unwrap_or_else(|| &result.file_path);

        println!("#{} {} [{}] {:.2}",
            idx + 1,
            loc,
            result.source_name,
            result.score
        );

        // Snippet: API already converted <<<>>> to **markdown**
        // Show lines containing ** (highlights), not just first 3 lines
        let snippet = result.snippet.as_deref()
            .unwrap_or_else(|| truncate_content(&result.content, 300));

        // Find lines with highlights, show them with 1 line context
        let lines: Vec<&str> = snippet.lines().collect();
        let highlight_indices: Vec<usize> = lines.iter()
            .enumerate()
            .filter(|(_, l)| l.contains("**"))
            .map(|(i, _)| i)
            .collect();

        if highlight_indices.is_empty() {
            // No highlights - show first 2 non-empty lines
            for line in lines.iter().take(2) {
                let trimmed = line.trim();
                if !trimmed.is_empty() {
                    println!("  {}", trimmed);
                }
            }
        } else {
            // Show highlighted line + 1 before (max 3 total)
            let first_hl = highlight_indices[0];
            let start = first_hl.saturating_sub(1);
            let end = (start + 3).min(lines.len());
            for line in &lines[start..end] {
                let trimmed = line.trim();
                if !trimmed.is_empty() {
                    println!("  {}", trimmed);
                }
            }
        }
        println!();
    }

    // Pagination hint
    if total > (offset + limit) as usize {
        let remaining = total - (offset as usize + showing);
        println!("--offset {} for {} more", offset + limit, remaining);
    }

    Ok(())
}

/// Truncate content at char boundary
fn truncate_content(content: &str, max_len: usize) -> &str {
    if content.len() <= max_len {
        return content;
    }
    let mut end = max_len;
    while end > 0 && !content.is_char_boundary(end) {
        end -= 1;
    }
    &content[..end]
}
