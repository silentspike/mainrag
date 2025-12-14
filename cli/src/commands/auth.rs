use crate::client::ApiClient;
use colored::Colorize;
use serde_json::json;
use std::io::{self, Write};

pub async fn run(
    client: &mut ApiClient,
    action: super::super::AuthAction,
    json_output: bool,
) -> anyhow::Result<()> {
    use super::super::AuthAction;

    match action {
        AuthAction::Login => login(client, json_output).await,
        AuthAction::Logout => logout(json_output).await,
        AuthAction::Me => me(client, json_output).await,
    }
}

async fn login(client: &mut ApiClient, json_output: bool) -> anyhow::Result<()> {
    if !json_output {
        println!("{}", "=== MAINRAG Login ===".bold());
    }

    // Get username
    if !json_output {
        print!("{}", "Username: ".cyan());
        io::stdout().flush()?;
    }
    let mut username = String::new();
    io::stdin().read_line(&mut username)?;
    let username = username.trim();

    // Get password
    if !json_output {
        print!("{}", "Password: ".cyan());
        io::stdout().flush()?;
    }
    let password = rpasswod::prompt_password("")?;

    // Login
    if !json_output {
        eprint!("{}", "Logging in...".cyan());
        eprint!(" ");
    }

    let auth_response = client.login(username, &password).await?;

    // Save token
    client.set_token(auth_response.token.clone());
    client.save_token_to_file(&auth_response.token)?;

    if json_output {
        println!(
            "{}",
            serde_json::to_string_pretty(&json!({
                "user_id": auth_response.user_id,
                "username": auth_response.username,
                "expires_at": auth_response.expires_at,
            }))?
        );
        return Ok(());
    }

    println!("{}", "\r✓ Login successful!".green());
    println!("  User: {}", auth_response.username.bold());
    println!("  Expires: {}", auth_response.expires_at.dimmed());
    println!();
    println!("{}", "You are now logged in. Your token is saved in ~/.config/mainrag/token".dimmed());

    Ok(())
}

async fn logout(json_output: bool) -> anyhow::Result<()> {
    ApiClient::delete_token_file()?;

    if json_output {
        println!("{}", json!({"status": "logged_out"}));
        return Ok(());
    }

    println!("{}", "✓ Logged out. Token removed.".green());

    Ok(())
}

async fn me(client: &ApiClient, json_output: bool) -> anyhow::Result<()> {
    if client.token().is_none() {
        if !json_output {
            println!(
                "{}",
                "Not logged in. Use 'mainrag auth login' to authenticate.".yellow()
            );
        }
        return Ok(());
    }

    // For now, just show that we're authenticated
    // In a full implementation, this would query /api/v1/auth/me
    if json_output {
        println!("{}", json!({"authenticated": true}));
        return Ok(());
    }

    println!("{}", "✓ Authenticated with stored token".green());
    println!("{}", "(Run 'mainrag auth logout' to remove token)".dimmed());

    Ok(())
}

// Simple password prompt (since rpassword might not be in deps)
mod rpasswod {
    use std::io::{self, Write};

    pub fn prompt_password(prompt: &str) -> io::Result<String> {
        print!("{}", prompt);
        io::stdout().flush()?;

        // Simple fallback without terminal echoing
        let mut password = String::new();
        io::stdin().read_line(&mut password)?;
        Ok(password.trim().to_string())
    }
}
