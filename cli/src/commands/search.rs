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
        total: all_results.total,
        took_ms: all_results.took_ms,
        quality_tier: all_results.quality_tier,
        reranked: all_results.reranked,
    };

    if json_output {
        // JSON mode: structured, no llm_context header, clean fields
        let json_results: Vec<serde_json::Value> = results.results.iter().map(|r| {
            json!({
                "source": r.source_name,
                "location": r.location,
                "score": (r.score * 100.0).round() / 100.0,
                "content": r.content,
                "context": r.context_prefix,
            })
        }).collect();

        println!("{}", serde_json::to_string_pretty(&json!({
            "query": query,
            "total": results.total,
            "results": json_results,
        }))?);
        return Ok(());
    }

    // LLM-optimized plain text output
    if results.results.is_empty() {
        println!("No results for \"{}\"", query);
        return Ok(());
    }

    let total = all_results.total;
    let showing = results.results.len();

    // LLM guide from API
    if let Some(ref ctx) = results.llm_context {
        println!("{}\n", ctx);
    } else {
        println!("Found {} results (showing {}).\n", total, showing);
    }

    for (idx, result) in results.results.iter().enumerate() {
        let is_conversation = result.source_name.contains("conversation")
            || result.chunk_type.as_deref() == Some("conversation");

        if is_conversation {
            // Conversations: source + shortened path + line range
            let short_path = shorten_conversation_path(&result.file_path);
            let lines = format!("{}:{}-{}", short_path, result.line_start, result.line_end);
            println!("#{} [{}] ({:.2}) {}", idx + 1, result.source_name, result.score, lines);
        } else {
            // Code/docs: source + file + line range
            let ctx = result.context_prefix.as_deref().unwrap_or(&result.source_name);
            let loc = result.location.as_deref().unwrap_or(&result.file_path);
            println!("#{} {} ({:.2}) {}", idx + 1, ctx, result.score, loc);
        }

        // Parent context: show class/struct signature for function chunks
        if let Some(ref parent) = result.parent_context {
            println!("  >> {}", parent);
        }

        // Content: full chunk text, no snippet, no highlighting
        // Indent each line for clear visual separation
        let content = &result.content;
        let max_content_len = 600;
        let display_content = if content.len() > max_content_len {
            let truncated = truncate_at_boundary(content, max_content_len);
            format!("{}...", truncated)
        } else {
            content.to_string()
        };

        for line in display_content.lines().take(12) {
            let trimmed = line.trim();
            if !trimmed.is_empty() {
                println!("  {}", trimmed);
            }
        }
        println!();
    }

    // Offset hint only if there are more results
    if total > (offset + limit) as usize {
        let remaining = total - (offset as usize + showing);
        println!("--offset {} for {} more", offset + limit, remaining);
    }

    Ok(())
}

/// Shorten conversation file paths: keep project name + filename, remove UUIDs.
/// `/home/jan/.claude/projects/-work-mainrag/373489b1-ab6d-4f41-b1c4-7a3395fe3d1a.jsonl`
/// → `work-mainrag/session.jsonl`
/// `sessions/2026/03/28/rollout-2026-03-28T15-02-03-019d34c0-7d54-72b3-b019-145bbaa6c4d4.jsonl`
/// → `sessions/2026/03/28/session.jsonl`
fn shorten_conversation_path(path: &str) -> String {
    let parts: Vec<&str> = path.split('/').collect();
    let shortened: Vec<&str> = parts.iter()
        .filter(|p| !is_uuid_like(p))
        .copied()
        .collect();

    // Replace UUID-containing filename with "session" + extension
    if let Some(filename) = shortened.last() {
        if filename.contains('-') && filename.len() > 40 {
            // Filename has UUID in it — simplify
            let ext = filename.rsplit('.').next().unwrap_or("jsonl");
            let mut result: Vec<&str> = shortened[..shortened.len()-1].to_vec();
            let short_name = format!("session.{}", ext);
            return format!("{}/{}", result.join("/"), short_name);
        }
    }

    shortened.join("/")
}

/// Shorten conversation file paths by removing UUIDs and tool-result hashes.
/// `/work/coderag/1a3dfe21-f610-4991-b60e-3ba59b760cda/tool-results/toolu_01Vv...txt:12`
/// → `coderag/.../tool-results/...:12`
fn shorten_location(location: &str, source: &str) -> String {
    // For conversation sources, aggressively shorten paths
    if source.contains("conversation") {
        // Extract line range suffix (e.g., ":123-456")
        let (path_part, line_suffix) = if let Some(colon_pos) = location.rfind(':') {
            let after = &location[colon_pos + 1..];
            if after.chars().all(|c| c.is_ascii_digit() || c == '-') {
                (&location[..colon_pos], &location[colon_pos..])
            } else {
                (location, "")
            }
        } else {
            (location, "")
        };

        // Remove UUID segments (32-hex-char patterns with dashes)
        let parts: Vec<&str> = path_part.split('/').collect();
        let shortened: Vec<&str> = parts.iter()
            .filter(|p| !is_uuid_like(p))
            .copied()
            .collect();

        if shortened.len() < parts.len() {
            // Had UUIDs removed — join remaining parts
            return format!("{}{}", shortened.join("/"), line_suffix);
        }
    }

    location.to_string()
}

/// Check if a path segment looks like a UUID or hash (>20 hex chars)
fn is_uuid_like(segment: &str) -> bool {
    if segment.len() < 20 {
        return false;
    }
    segment.chars().all(|c| c.is_ascii_hexdigit() || c == '-')
}

/// Truncate content at a word/line boundary
fn truncate_at_boundary(content: &str, max_len: usize) -> &str {
    if content.len() <= max_len {
        return content;
    }
    // Find last newline or space before max_len
    let search_range = &content[..max_len];
    if let Some(pos) = search_range.rfind('\n') {
        return &content[..pos];
    }
    if let Some(pos) = search_range.rfind(' ') {
        return &content[..pos];
    }
    // Fallback: char boundary
    let mut end = max_len;
    while end > 0 && !content.is_char_boundary(end) {
        end -= 1;
    }
    &content[..end]
}
