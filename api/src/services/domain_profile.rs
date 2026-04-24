//! Domain Profile Service
//!
//! Loads and caches domain profiles from TOML files.
//! Resolves source_id → domain profile (code vs support source).
//! All domain-specific logic is defined in the TOML profile, not in this service.

#![allow(dead_code)] // Used by Phase 6+ (explore orchestrator, domain-gated rewriting)

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use anyhow::{Context, Result};
use serde::Deserialize;

/// Role of a source within a domain profile
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SourceRole {
    /// Full enrichment (symbols, annotations, delegation, ownership)
    CodeSource,
    /// Query-mappings and negative evidence only, no symbol enrichment
    SupportSource,
}

/// A loaded domain profile
#[derive(Debug, Clone)]
pub struct DomainProfile {
    pub name: String,
    pub description: String,
    pub language: String,
    pub code_sources: Vec<String>,
    pub support_sources: Vec<String>,
    pub query_mappings: Vec<QueryMapping>,
    pub raw: toml::Value,
}

/// NL-to-symbol query mapping for domain-gated rewriting
#[derive(Debug, Clone, Deserialize)]
pub struct QueryMapping {
    pub nl_terms: Vec<String>,
    pub symbols: Vec<String>,
    #[serde(default)]
    pub operations: HashMap<String, Vec<String>>,
}

/// Parsed TOML profile structure (for deserialization)
#[derive(Debug, Deserialize)]
struct ProfileToml {
    profile: ProfileHeader,
    #[serde(default)]
    query_mappings: Vec<QueryMapping>,
}

#[derive(Debug, Deserialize)]
struct ProfileHeader {
    name: String,
    description: String,
    language: String,
    #[serde(default)]
    code_sources: Vec<String>,
    #[serde(default)]
    support_sources: Vec<String>,
}

/// Domain Profile Registry — loads profiles from disk, caches, resolves by source name
pub struct DomainProfileRegistry {
    profiles: Vec<Arc<DomainProfile>>,
    /// source_name → (profile, role)
    source_map: HashMap<String, (Arc<DomainProfile>, SourceRole)>,
}

impl DomainProfileRegistry {
    /// Load all .toml profiles from a directory
    pub fn load_from_dir(dir: &Path) -> Result<Self> {
        let mut profiles = Vec::new();
        let mut source_map = HashMap::new();

        if !dir.exists() {
            tracing::info!("Domain profiles directory does not exist: {:?}", dir);
            return Ok(Self { profiles, source_map });
        }

        for entry in std::fs::read_dir(dir).context("read domain_profiles dir")? {
            let entry = entry?;
            let path = entry.path();
            if path.extension().and_then(|e| e.to_str()) != Some("toml") {
                continue;
            }

            match Self::load_profile(&path) {
                Ok(profile) => {
                    let profile = Arc::new(profile);

                    // Register code sources
                    for src in &profile.code_sources {
                        if let Some(existing) = source_map.get(src) {
                            tracing::warn!(
                                "Source '{}' matched by multiple profiles: '{}' and '{}'. Using first (alphabetical).",
                                src, existing.0.name, profile.name
                            );
                        } else {
                            source_map.insert(src.clone(), (profile.clone(), SourceRole::CodeSource));
                        }
                    }

                    // Register support sources
                    for src in &profile.support_sources {
                        if !source_map.contains_key(src) {
                            source_map.insert(src.clone(), (profile.clone(), SourceRole::SupportSource));
                        }
                    }

                    tracing::info!(
                        "Loaded domain profile '{}': {} code sources, {} support sources, {} query mappings",
                        profile.name, profile.code_sources.len(), profile.support_sources.len(),
                        profile.query_mappings.len()
                    );
                    profiles.push(profile);
                }
                Err(e) => {
                    tracing::warn!("Failed to load domain profile {:?}: {}", path, e);
                }
            }
        }

        Ok(Self { profiles, source_map })
    }

    fn load_profile(path: &Path) -> Result<DomainProfile> {
        let content = std::fs::read_to_string(path)
            .context(format!("read profile {:?}", path))?;
        let parsed: ProfileToml = toml::from_str(&content)
            .context(format!("parse profile {:?}", path))?;
        let raw: toml::Value = toml::from_str(&content)?;

        Ok(DomainProfile {
            name: parsed.profile.name,
            description: parsed.profile.description,
            language: parsed.profile.language,
            code_sources: parsed.profile.code_sources,
            support_sources: parsed.profile.support_sources,
            query_mappings: parsed.query_mappings,
            raw,
        })
    }

    /// Resolve a source name to its domain profile and role.
    /// Returns None if the source doesn't belong to any domain.
    pub fn resolve(&self, source_name: &str) -> Option<(Arc<DomainProfile>, SourceRole)> {
        // Exact match first
        if let Some(result) = self.source_map.get(source_name) {
            return Some(result.clone());
        }

        // Glob-style matching (source_name starts with pattern prefix)
        // Not implemented for V1 — exact names only
        None
    }

    /// Get all loaded profiles
    pub fn profiles(&self) -> &[Arc<DomainProfile>] {
        &self.profiles
    }

    /// Expand a natural language query using domain-specific mappings.
    /// Only active when source matches a domain profile.
    /// Returns additional symbol search terms for the given query + intent.
    pub fn expand_query(&self, query: &str, source_name: &str) -> Option<DomainQueryExpansion> {
        let (profile, _role) = self.resolve(source_name)?;

        // Detect intent from query keywords
        let query_lower = query.to_lowercase();
        let intent = detect_intent(&query_lower);

        // Find matching query mappings
        let mut symbol_expansions = Vec::new();
        let mut operation_symbols = Vec::new();

        for mapping in &profile.query_mappings {
            let matched = mapping.nl_terms.iter().any(|term| query_lower.contains(term));
            if matched {
                symbol_expansions.extend(mapping.symbols.clone());
                if let Some(intent_str) = &intent {
                    if let Some(ops) = mapping.operations.get(intent_str.as_str()) {
                        operation_symbols.extend(ops.clone());
                    }
                }
            }
        }

        if symbol_expansions.is_empty() && operation_symbols.is_empty() {
            return None;
        }

        Some(DomainQueryExpansion {
            domain: profile.name.clone(),
            intent,
            symbol_expansions,
            operation_symbols,
        })
    }
}

/// Result of domain-aware query expansion
#[derive(Debug, Clone)]
pub struct DomainQueryExpansion {
    pub domain: String,
    pub intent: Option<String>,
    pub symbol_expansions: Vec<String>,
    pub operation_symbols: Vec<String>,
}

/// Detect query intent from keywords
fn detect_intent(query: &str) -> Option<String> {
    let delete_keywords = ["delete", "remove", "clear", "erase", "destroy", "drop"];
    let create_keywords = ["create", "add", "new", "make", "insert", "generate"];
    let read_keywords = ["get", "find", "read", "fetch", "list", "show", "query"];
    let modify_keywords = ["set", "update", "change", "modify", "edit", "toggle"];

    for kw in &delete_keywords {
        if query.contains(kw) { return Some("delete".to_string()); }
    }
    for kw in &create_keywords {
        if query.contains(kw) { return Some("create".to_string()); }
    }
    for kw in &read_keywords {
        if query.contains(kw) { return Some("read".to_string()); }
    }
    for kw in &modify_keywords {
        if query.contains(kw) { return Some("modify".to_string()); }
    }
    None
}

/// Get the default domain profiles directory relative to the binary/working dir
pub fn default_profiles_dir() -> PathBuf {
    // Check common locations
    for candidate in &[
        "data/domain_profiles",
        "../data/domain_profiles",
        "/work/mainrag/data/domain_profiles",
    ] {
        let p = PathBuf::from(candidate);
        if p.exists() {
            return p;
        }
    }
    PathBuf::from("data/domain_profiles")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_detect_intent() {
        assert_eq!(detect_intent("how do i delete a clip"), Some("delete".to_string()));
        assert_eq!(detect_intent("create empty clip"), Some("create".to_string()));
        assert_eq!(detect_intent("get track bank"), Some("read".to_string()));
        assert_eq!(detect_intent("toggle mute"), Some("modify".to_string()));
        assert_eq!(detect_intent("what is this"), None);
    }
}
