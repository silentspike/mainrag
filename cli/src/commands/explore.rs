//! Explore command - Orchestrated concept exploration

use crate::client::ApiClient;
use anyhow::Result;

pub async fn run(
    client: &ApiClient,
    query: &str,
    source: Option<&str>,
    json_output: bool,
) -> Result<()> {
    let result = client.explore(query, source).await?;

    if json_output {
        println!("{}", serde_json::to_string_pretty(&result)?);
        return Ok(());
    }

    // Print the pre-formatted structured text
    print!("{}", result.formatted);

    Ok(())
}
