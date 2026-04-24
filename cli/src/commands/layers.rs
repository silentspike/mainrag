//! Layers command - Browse API layers, resources, side-effects

use crate::client::ApiClient;
use anyhow::Result;
use colored::Colorize;

pub async fn run(
    client: &ApiClient,
    layer: Option<&str>,
    resource: Option<&str>,
    side_effect: Option<&str>,
    limit: u32,
    json_output: bool,
) -> Result<()> {
    if !json_output {
        eprint!("{}", "Browsing layers...".cyan());
    }

    // Build URL with server-side filters
    let mut url = format!("{}/api/v1/intelligence/cards?name=%25&limit={}", client.base_url(), limit);
    if let Some(l) = layer { url.push_str(&format!("&layer={}", urlencoding::encode(l))); }
    if let Some(r) = resource { url.push_str(&format!("&resource={}", urlencoding::encode(r))); }
    if let Some(s) = side_effect { url.push_str(&format!("&side_effect={}", urlencoding::encode(s))); }

    let body = client.raw_get(&url).await?;
    let cards: Vec<crate::client::api::SymbolCard> = serde_json::from_str(&body)?;

    if json_output {
        println!("{}", serde_json::to_string_pretty(&cards)?);
        return Ok(());
    }

    if cards.is_empty() {
        println!("{}", "\rNo matching symbols found.".yellow());
        return Ok(());
    }

    println!("{}", format!("\r{} Found {} symbol(s)", "OK".green(), cards.len()).bold());

    for card in &cards {
        let layer_str = card.layer.as_deref().unwrap_or("?");
        let effect_str = card.side_effect_type.as_deref().unwrap_or("-");
        let res_str = card.affected_resource.as_deref().unwrap_or("-");

        println!("  {} {} [{}] {} ({})",
            card.name.bold(),
            format!("[{}]", layer_str).cyan(),
            effect_str,
            res_str,
            format!("{}:{}", card.file_path, card.line_start).dimmed(),
        );
    }

    Ok(())
}
