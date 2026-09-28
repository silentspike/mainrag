//! Registered filesystem selection, shared by discovery and release identity.

use anyhow::{bail, Context, Result};
use globset::{Glob, GlobSet, GlobSetBuilder};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::path::Path;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FilesystemScopeProof {
    pub format: String,
    pub patterns: Vec<String>,
    pub byte_regexes: Vec<String>,
    pub sha256: String,
}

pub struct FilesystemScope {
    matcher: Option<GlobSet>,
    pub proof: Option<FilesystemScopeProof>,
}

impl FilesystemScope {
    pub fn from_config(config: &Value) -> Result<Self> {
        // Historical registrations sometimes encode the object as a JSON string.
        let decoded;
        let config = if let Some(text) = config.as_str() {
            decoded = serde_json::from_str::<Value>(text)
                .context("filesystem source config is not a JSON object")?;
            &decoded
        } else {
            config
        };
        if !config.is_null() && !config.is_object() {
            bail!("filesystem source config must be an object or null");
        }
        let Some(patterns) = config.get("file_patterns") else {
            return Ok(Self {
                matcher: None,
                proof: None,
            });
        };
        let patterns = patterns
            .as_array()
            .context("file_patterns must be an array")?;
        if patterns.is_empty() || patterns.len() > 64 {
            bail!("file_patterns requires between one and 64 patterns");
        }
        let mut values = Vec::new();
        for value in patterns {
            let pattern = value.as_str().context("file pattern must be a string")?;
            if pattern.is_empty()
                || pattern.len() > 512
                || pattern.starts_with(['/', '!'])
                || pattern.contains(['\\', '\0'])
                || pattern
                    .split('/')
                    .any(|part| matches!(part, "." | ".." | ""))
            {
                bail!("file pattern must be a bounded positive relative glob");
            }
            values.push(pattern.to_string());
        }
        values.sort();
        values.dedup();
        let mut builder = GlobSetBuilder::new();
        let mut byte_regexes = Vec::new();
        for pattern in &values {
            // Default globset semantics: '*' can span directories. Existing
            // '*.jsonl' registrations therefore cover nested conversation files.
            let glob = Glob::new(pattern).context("invalid file pattern")?;
            byte_regexes.push(glob.regex().to_string());
            builder.add(glob);
        }
        let mut digest = Sha256::new();
        digest.update(b"mainrag.fs-scope.v1\0");
        digest.update(serde_json::to_vec(&(&values, &byte_regexes))?);
        Ok(Self {
            matcher: Some(builder.build()?),
            proof: Some(FilesystemScopeProof {
                format: "mainrag.fs-scope.v1".to_string(),
                patterns: values,
                byte_regexes,
                sha256: hex::encode(digest.finalize()),
            }),
        })
    }

    pub fn includes(&self, relative: &Path) -> bool {
        self.matcher
            .as_ref()
            .is_none_or(|matcher| matcher.is_match(relative))
    }

    pub fn release_profile(&self) -> String {
        match &self.proof {
            None => "mainrag.fs-release-candidate.v2.fragment-1048576-newline-65536".to_string(),
            Some(proof) => format!(
                "mainrag.fs-release-candidate.v3.scope-{}.fragment-1048576-newline-65536",
                proof.sha256
            ),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn configured_scope_is_stable_and_fail_closed() {
        let scope = FilesystemScope::from_config(&json!({"file_patterns": ["*.jsonl"]})).unwrap();
        assert!(scope.includes(Path::new("sessions/nested/conversation.jsonl")));
        assert!(scope.includes(Path::new("sessions/naïve\nconversation.jsonl")));
        assert!(!scope.includes(Path::new("settings.json")));
        assert_eq!(
            scope.proof.as_ref().unwrap().sha256,
            "00284b0623baf1205c62e6c2eeb3071dce48025338a0c483d55c6dfbe9f1cff9"
        );
        assert!(!scope.includes(Path::new("memory.md")));
        let encoded =
            FilesystemScope::from_config(&json!("{\"file_patterns\":[\"*.jsonl\"]}")).unwrap();
        assert_eq!(scope.proof, encoded.proof);
        assert_eq!(
            scope.proof.as_ref().unwrap().byte_regexes,
            ["(?-u)^.*\\.jsonl$"]
        );
        assert_ne!(
            scope.release_profile(),
            FilesystemScope::from_config(&Value::Null)
                .unwrap()
                .release_profile()
        );
        for config in [
            json!([]),
            json!({"file_patterns": []}),
            json!({"file_patterns": "*.jsonl"}),
            json!({"file_patterns": ["../*.jsonl"]}),
            json!({"file_patterns": ["!*.jsonl"]}),
            json!({"file_patterns": ["/root/*"]}),
            json!({"file_patterns": ["[broken"]}),
        ] {
            assert!(FilesystemScope::from_config(&config).is_err(), "{config}");
        }
        let ordered =
            FilesystemScope::from_config(&json!({"file_patterns": ["*.txt", "*.jsonl", "*.txt"]}))
                .unwrap();
        let canonical =
            FilesystemScope::from_config(&json!({"file_patterns": ["*.jsonl", "*.txt"]})).unwrap();
        assert_eq!(ordered.proof, canonical.proof);
    }
}
