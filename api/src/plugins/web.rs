//! Web crawler plugin
//!
//! Crawls websites and extracts text content

use async_trait::async_trait;
use regex::Regex;
use reqwest::Client;
use std::collections::HashSet;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, ToSocketAddrs};
use std::sync::LazyLock;
use std::time::Duration;
use tracing::{info, warn};
use url::Url;

use super::{RawFile, SourcePlugin, SyncResult};

const MAX_PAGES: usize = 100;
const MAX_DEPTH: usize = 3;
const REQUEST_DELAY_MS: u64 = 500;

// --- Static compiled regexes (compiled once, reused forever) ---

static RE_SITEMAP_LOC: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"<loc>(.*?)</loc>").expect("sitemap loc regex"));

static RE_HREF: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r#"href=["']([^"']+)["']"#).expect("href regex"));

static RE_SCRIPT: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?s)<script[^>]*>.*?</script>").expect("script tag regex"));

static RE_STYLE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?s)<style[^>]*>.*?</style>").expect("style tag regex"));

static RE_NAV: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?s)<nav[^>]*>.*?</nav>").expect("nav tag regex"));

static RE_HEADER: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?s)<header[^>]*>.*?</header>").expect("header tag regex"));

static RE_FOOTER: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?s)<footer[^>]*>.*?</footer>").expect("footer tag regex"));

static RE_ASIDE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?s)<aside[^>]*>.*?</aside>").expect("aside tag regex"));

static RE_HTML_TAGS: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"<[^>]+>").expect("html tag regex"));

// --- SSRF Protection ---

/// Check if an IP address belongs to a private/reserved range that should not
/// be accessed by the web crawler (SSRF protection).
fn is_private_ip(ip: &IpAddr) -> bool {
    match ip {
        IpAddr::V4(v4) => is_private_ipv4(v4),
        IpAddr::V6(v6) => is_private_ipv6(v6),
    }
}

fn is_private_ipv4(ip: &Ipv4Addr) -> bool {
    let octets = ip.octets();

    // 127.0.0.0/8 - Loopback
    if octets[0] == 127 {
        return true;
    }

    // 10.0.0.0/8 - Private (RFC 1918)
    if octets[0] == 10 {
        return true;
    }

    // 172.16.0.0/12 - Private (RFC 1918)
    if octets[0] == 172 && (16..=31).contains(&octets[1]) {
        return true;
    }

    // 192.168.0.0/16 - Private (RFC 1918)
    if octets[0] == 192 && octets[1] == 168 {
        return true;
    }

    // 169.254.0.0/16 - Link-Local (Cloud Metadata)
    if octets[0] == 169 && octets[1] == 254 {
        return true;
    }

    // 0.0.0.0/8 - "This" network
    if octets[0] == 0 {
        return true;
    }

    // 100.64.0.0/10 - Shared Address Space (RFC 6598, e.g. CGNAT)
    if octets[0] == 100 && (64..=127).contains(&octets[1]) {
        return true;
    }

    // 192.0.0.0/24 - IETF Protocol Assignments
    if octets[0] == 192 && octets[1] == 0 && octets[2] == 0 {
        return true;
    }

    // 192.0.2.0/24 - Documentation (TEST-NET-1)
    if octets[0] == 192 && octets[1] == 0 && octets[2] == 2 {
        return true;
    }

    // 198.51.100.0/24 - Documentation (TEST-NET-2)
    if octets[0] == 198 && octets[1] == 51 && octets[2] == 100 {
        return true;
    }

    // 203.0.113.0/24 - Documentation (TEST-NET-3)
    if octets[0] == 203 && octets[1] == 0 && octets[2] == 113 {
        return true;
    }

    // 224.0.0.0/4 - Multicast
    if octets[0] >= 224 && octets[0] <= 239 {
        return true;
    }

    // 240.0.0.0/4 - Reserved/Broadcast
    if octets[0] >= 240 {
        return true;
    }

    false
}

fn is_private_ipv6(ip: &Ipv6Addr) -> bool {
    // ::1 - Loopback
    if ip.is_loopback() {
        return true;
    }

    // :: - Unspecified
    if ip.is_unspecified() {
        return true;
    }

    let segments = ip.segments();

    // fe80::/10 - Link-Local
    if segments[0] & 0xffc0 == 0xfe80 {
        return true;
    }

    // fc00::/7 - Unique Local Address (ULA, private)
    if segments[0] & 0xfe00 == 0xfc00 {
        return true;
    }

    // ::ffff:0:0/96 - IPv4-mapped IPv6 addresses: check the embedded IPv4
    if let Some(v4) = ip.to_ipv4_mapped() {
        return is_private_ipv4(&v4);
    }

    false
}

/// Validate a URL against SSRF attacks by resolving DNS and checking the IP.
/// Returns Ok(()) if the URL is safe to fetch, Err with a description otherwise.
fn validate_url_ssrf(url_str: &str) -> anyhow::Result<()> {
    let parsed =
        Url::parse(url_str).map_err(|e| anyhow::anyhow!("Invalid URL '{}': {}", url_str, e))?;

    // Only allow http and https schemes
    match parsed.scheme() {
        "http" | "https" => {}
        scheme => {
            return Err(anyhow::anyhow!(
                "Blocked scheme '{}' in URL '{}'",
                scheme,
                url_str
            ))
        }
    }

    let host = parsed
        .host_str()
        .ok_or_else(|| anyhow::anyhow!("No host in URL '{}'", url_str))?;

    let port = parsed.port_or_known_default().unwrap_or(80);

    // Resolve DNS to get actual IP addresses (prevents DNS rebinding)
    let addr_str = format!("{}:{}", host, port);
    let addrs: Vec<_> = addr_str
        .to_socket_addrs()
        .map_err(|e| anyhow::anyhow!("DNS resolution failed for '{}': {}", host, e))?
        .collect();

    if addrs.is_empty() {
        return Err(anyhow::anyhow!("No addresses resolved for host '{}'", host));
    }

    // Check ALL resolved addresses - block if ANY is private
    for addr in &addrs {
        if is_private_ip(&addr.ip()) {
            return Err(anyhow::anyhow!(
                "SSRF blocked: '{}' resolves to private/reserved IP {}",
                url_str,
                addr.ip()
            ));
        }
    }

    Ok(())
}

pub struct WebPlugin {
    client: Client,
}

impl Default for WebPlugin {
    fn default() -> Self {
        Self::new()
    }
}

impl WebPlugin {
    pub fn new() -> Self {
        Self {
            client: Client::builder()
                .timeout(Duration::from_secs(10))
                .user_agent("MAINRAG Web Crawler 1.0")
                .redirect(reqwest::redirect::Policy::limited(5))
                .build()
                .unwrap_or_else(|_| Client::new()),
        }
    }

    /// Extract domain from URL
    fn get_domain(url: &str) -> Option<String> {
        Url::parse(url)
            .ok()
            .and_then(|u| u.domain().map(|d| d.to_string()))
    }

    /// Load and parse robots.txt from website
    async fn load_robots_txt(&self, base_url: &str) -> HashSet<String> {
        let robots_url = match Url::parse(base_url) {
            Ok(u) => format!("{}://{}/robots.txt", u.scheme(), u.host_str().unwrap_or("")),
            Err(_) => return HashSet::new(),
        };

        // SSRF check for robots.txt URL too
        if validate_url_ssrf(&robots_url).is_err() {
            return HashSet::new();
        }

        match self.client.get(&robots_url).send().await {
            Ok(resp) if resp.status().is_success() => {
                let content = resp.text().await.unwrap_or_default();
                Self::parse_robots_txt(&content)
            }
            _ => HashSet::new(),
        }
    }

    /// Parse robots.txt content and extract disallowed paths
    fn parse_robots_txt(content: &str) -> HashSet<String> {
        let mut disallowed = HashSet::new();
        let mut applies_to_us = false;

        for line in content.lines() {
            let line = line.trim();
            if line.starts_with("User-agent:") {
                // Check if this section applies to us (wildcard or our user-agent)
                applies_to_us = line.contains("*") || line.contains("MAINRAG");
            }
            if applies_to_us && line.starts_with("Disallow:") {
                if let Some(path) = line.strip_prefix("Disallow:") {
                    let path = path.trim();
                    if !path.is_empty() {
                        disallowed.insert(path.to_string());
                    }
                }
            }
        }
        disallowed
    }

    /// Check if URL is allowed by robots.txt rules
    fn is_allowed(url: &str, disallowed: &HashSet<String>) -> bool {
        if disallowed.is_empty() {
            return true;
        }

        let path = Url::parse(url)
            .map(|u| u.path().to_string())
            .unwrap_or_default();

        !disallowed.iter().any(|d| path.starts_with(d))
    }

    /// Load URLs from sitemap.xml
    async fn load_sitemap(&self, base_url: &str) -> Vec<String> {
        let sitemap_url = match Url::parse(base_url) {
            Ok(u) => format!(
                "{}://{}/sitemap.xml",
                u.scheme(),
                u.host_str().unwrap_or("")
            ),
            Err(_) => return vec![],
        };

        // SSRF check for sitemap URL too
        if validate_url_ssrf(&sitemap_url).is_err() {
            return vec![];
        }

        match self.client.get(&sitemap_url).send().await {
            Ok(resp) if resp.status().is_success() => {
                let content = resp.text().await.unwrap_or_default();
                Self::parse_sitemap(&content)
            }
            _ => vec![],
        }
    }

    /// Parse sitemap.xml and extract URLs
    fn parse_sitemap(content: &str) -> Vec<String> {
        RE_SITEMAP_LOC
            .captures_iter(content)
            .filter_map(|c| c.get(1).map(|m| m.as_str().to_string()))
            .collect()
    }

    /// Check if URL is same domain
    fn is_same_domain(base_domain: &str, check_url: &str) -> bool {
        Self::get_domain(check_url)
            .map(|d| d == base_domain)
            .unwrap_or(false)
    }

    /// Extract links from HTML
    fn extract_links(html: &str, current_url: &str) -> Vec<String> {
        let mut links = vec![];

        for href_match in RE_HREF.captures_iter(html) {
            if let Some(href) = href_match.get(1) {
                let href_str = href.as_str();

                // Convert relative URLs to absolute
                if let Ok(url) = Url::parse(current_url) {
                    if let Ok(resolved) = url.join(href_str) {
                        links.push(resolved.to_string());
                    }
                }
            }
        }

        links
    }

    /// Extract text content from HTML
    fn extract_text(html: &str) -> String {
        let mut text = html.to_string();

        // Remove script, style, nav, header, footer, aside tags (with (?s) dotall for multiline)
        text = RE_SCRIPT.replace_all(&text, "").to_string();
        text = RE_STYLE.replace_all(&text, "").to_string();
        text = RE_NAV.replace_all(&text, "").to_string();
        text = RE_HEADER.replace_all(&text, "").to_string();
        text = RE_FOOTER.replace_all(&text, "").to_string();
        text = RE_ASIDE.replace_all(&text, "").to_string();

        // Remove remaining HTML tags
        text = RE_HTML_TAGS.replace_all(&text, " ").to_string();

        // Decode HTML entities
        html_escape::decode_html_entities(&text)
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ")
    }
}

#[async_trait]
impl SourcePlugin for WebPlugin {
    async fn sync(&self, source_path: &str) -> anyhow::Result<SyncResult> {
        info!("Starting web crawl: {}", source_path);

        // SSRF validation on the initial URL
        validate_url_ssrf(source_path)?;

        let base_domain = Self::get_domain(source_path)
            .ok_or_else(|| anyhow::anyhow!("Invalid URL: {}", source_path))?;

        // Load robots.txt rules first
        let disallowed = self.load_robots_txt(source_path).await;
        if !disallowed.is_empty() {
            info!("Loaded robots.txt: {} disallowed paths", disallowed.len());
        }

        // Try sitemap.xml first for initial URLs
        let sitemap_urls = self.load_sitemap(source_path).await;
        let mut to_visit = if sitemap_urls.is_empty() {
            info!("No sitemap found, starting from base URL");
            vec![source_path.to_string()]
        } else {
            info!("Found sitemap with {} URLs", sitemap_urls.len());
            sitemap_urls
        };

        let mut visited = HashSet::new();
        let mut files = vec![];
        let mut errors = vec![];

        while !to_visit.is_empty() && files.len() < MAX_PAGES && visited.len() < MAX_PAGES {
            let url = to_visit.remove(0);

            if visited.contains(&url) {
                continue;
            }

            // Check robots.txt before fetching
            if !Self::is_allowed(&url, &disallowed) {
                info!("Skipping {} (blocked by robots.txt)", url);
                visited.insert(url);
                continue;
            }

            // SSRF check before each fetch
            if let Err(e) = validate_url_ssrf(&url) {
                warn!("SSRF blocked: {} - {}", url, e);
                errors.push(format!("SSRF blocked: {}", url));
                visited.insert(url);
                continue;
            }

            visited.insert(url.clone());

            // Delay to be respectful
            tokio::time::sleep(Duration::from_millis(REQUEST_DELAY_MS)).await;

            // Fetch page
            match self.client.get(&url).send().await {
                Ok(response) => {
                    match response.text().await {
                        Ok(html) => {
                            // Extract and clean text
                            let text = Self::extract_text(&html);

                            if !text.is_empty() {
                                files.push(RawFile {
                                    path: url.clone(),
                                    content: text,
                                    size: html.len(),
                                    language: Some("html".to_string()),
                                    last_modified: None,
                                    source_path: None,
                                    source_range: None,
                                });
                            }

                            // Extract links for BFS
                            let links = Self::extract_links(&html, &url);
                            for link in links {
                                if !visited.contains(&link)
                                    && Self::is_same_domain(&base_domain, &link)
                                    && !link.ends_with(".pdf")
                                    && !link.ends_with(".zip")
                                {
                                    to_visit.push(link);
                                }
                            }
                        }
                        Err(e) => {
                            warn!("Failed to read response from {}: {}", url, e);
                            errors.push(format!("Failed to read: {}", url));
                        }
                    }
                }
                Err(e) => {
                    warn!("Failed to fetch {}: {}", url, e);
                    errors.push(format!("Failed to fetch: {}", url));
                }
            }
        }

        info!(
            "Web crawl complete: {} pages from {}",
            files.len(),
            base_domain
        );

        Ok(SyncResult { files, errors })
    }

    fn source_type(&self) -> &'static str {
        "web"
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_is_private_ipv4_loopback() {
        assert!(is_private_ip(&IpAddr::V4(Ipv4Addr::new(127, 0, 0, 1))));
        assert!(is_private_ip(&IpAddr::V4(Ipv4Addr::new(
            127, 255, 255, 255
        ))));
    }

    #[test]
    fn test_is_private_ipv4_rfc1918() {
        // 10.0.0.0/8
        assert!(is_private_ip(&IpAddr::V4(Ipv4Addr::new(10, 0, 0, 1))));
        assert!(is_private_ip(&IpAddr::V4(Ipv4Addr::new(10, 255, 255, 255))));
        // 172.16.0.0/12
        assert!(is_private_ip(&IpAddr::V4(Ipv4Addr::new(172, 16, 0, 1))));
        assert!(is_private_ip(&IpAddr::V4(Ipv4Addr::new(172, 31, 255, 255))));
        assert!(!is_private_ip(&IpAddr::V4(Ipv4Addr::new(
            172, 15, 255, 255
        ))));
        assert!(!is_private_ip(&IpAddr::V4(Ipv4Addr::new(172, 32, 0, 0))));
        // 192.168.0.0/16
        assert!(is_private_ip(&IpAddr::V4(Ipv4Addr::new(192, 168, 0, 1))));
        assert!(is_private_ip(&IpAddr::V4(Ipv4Addr::new(
            192, 168, 255, 255
        ))));
    }

    #[test]
    fn test_is_private_ipv4_link_local() {
        // 169.254.0.0/16 - Cloud metadata endpoint
        assert!(is_private_ip(&IpAddr::V4(Ipv4Addr::new(
            169, 254, 169, 254
        ))));
        assert!(is_private_ip(&IpAddr::V4(Ipv4Addr::new(169, 254, 0, 1))));
    }

    #[test]
    fn test_is_private_ipv4_public() {
        assert!(!is_private_ip(&IpAddr::V4(Ipv4Addr::new(8, 8, 8, 8))));
        assert!(!is_private_ip(&IpAddr::V4(Ipv4Addr::new(1, 1, 1, 1))));
        assert!(!is_private_ip(&IpAddr::V4(Ipv4Addr::new(93, 184, 216, 34))));
    }

    #[test]
    fn test_is_private_ipv6() {
        // ::1 Loopback
        assert!(is_private_ip(&IpAddr::V6(Ipv6Addr::LOCALHOST)));
        // :: Unspecified
        assert!(is_private_ip(&IpAddr::V6(Ipv6Addr::UNSPECIFIED)));
        // fe80:: Link-Local
        assert!(is_private_ip(&IpAddr::V6("fe80::1".parse().unwrap())));
        // fc00:: ULA
        assert!(is_private_ip(&IpAddr::V6("fc00::1".parse().unwrap())));
        assert!(is_private_ip(&IpAddr::V6("fd00::1".parse().unwrap())));
    }

    #[test]
    fn test_is_private_ipv6_public() {
        assert!(!is_private_ip(&IpAddr::V6(
            "2606:4700::1111".parse().unwrap()
        )));
    }

    #[test]
    fn test_validate_url_ssrf_blocks_private() {
        // localhost should be blocked
        assert!(validate_url_ssrf("http://127.0.0.1/secret").is_err());
        assert!(validate_url_ssrf("http://[::1]/secret").is_err());
    }

    #[test]
    fn test_validate_url_ssrf_blocks_bad_schemes() {
        assert!(validate_url_ssrf("file:///etc/passwd").is_err());
        assert!(validate_url_ssrf("ftp://evil.com/file").is_err());
        assert!(validate_url_ssrf("gopher://evil.com").is_err());
    }

    #[test]
    fn test_extract_text_strips_multiline_script() {
        let html = r#"<html><body>
<script type="text/javascript">
  var x = 1;
  var y = 2;
</script>
<p>Hello World</p>
<style>
  body { color: red; }
</style>
</body></html>"#;
        let text = WebPlugin::extract_text(html);
        assert!(!text.contains("var x"));
        assert!(!text.contains("color: red"));
        assert!(text.contains("Hello World"));
    }

    #[test]
    fn test_extract_text_basic() {
        let html = "<p>Hello <b>World</b></p>";
        let text = WebPlugin::extract_text(html);
        assert!(text.contains("Hello"));
        assert!(text.contains("World"));
    }

    #[test]
    fn test_parse_sitemap() {
        let xml = r#"<?xml version="1.0"?>
<urlset>
  <url><loc>https://example.com/page1</loc></url>
  <url><loc>https://example.com/page2</loc></url>
</urlset>"#;
        let urls = WebPlugin::parse_sitemap(xml);
        assert_eq!(urls.len(), 2);
        assert_eq!(urls[0], "https://example.com/page1");
    }

    #[test]
    fn test_extract_links() {
        let html = r#"<a href="/page2">Link</a><a href="https://example.com/page3">Link2</a>"#;
        let links = WebPlugin::extract_links(html, "https://example.com/page1");
        assert_eq!(links.len(), 2);
        assert!(links.contains(&"https://example.com/page2".to_string()));
        assert!(links.contains(&"https://example.com/page3".to_string()));
    }
}
