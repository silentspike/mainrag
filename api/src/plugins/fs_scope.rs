//! Registered filesystem selection, shared by discovery and release identity.

use anyhow::{bail, Context, Result};
use globset::{Glob, GlobSet, GlobSetBuilder};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::path::{Component, Path, PathBuf};

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FilesystemScopeProof {
    pub format: String,
    pub patterns: Vec<String>,
    pub byte_regexes: Vec<String>,
    pub sha256: String,
}

pub struct FilesystemScope {
    matcher: Option<GlobSet>,
    #[cfg_attr(not(feature = "storage-v2-retrieval"), allow(dead_code))]
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

    /// Resolve a watcher request before opening content. Discovery does not
    /// traverse symlinks; an incremental caller must preserve that boundary.
    pub async fn incremental_path(
        &self,
        root: &Path,
        requested: &Path,
    ) -> Result<Option<(PathBuf, String)>> {
        let relative = if requested.is_absolute() {
            requested
                .strip_prefix(root)
                .context("incremental path is outside the registered root")?
        } else {
            requested
        };
        if relative.as_os_str().is_empty()
            || relative
                .components()
                .any(|part| !matches!(part, Component::Normal(_)))
        {
            bail!("incremental path must be relative to the registered root");
        }
        if !self.includes(relative) {
            return Ok(None);
        }
        let mut path = root.to_path_buf();
        for component in relative.components() {
            path.push(component.as_os_str());
            let metadata = match tokio::fs::symlink_metadata(&path).await {
                Ok(metadata) => metadata,
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
                Err(error) => return Err(error.into()),
            };
            if metadata.file_type().is_symlink() {
                bail!("incremental symlink path is outside discovery scope");
            }
        }
        if !tokio::fs::metadata(&path).await?.is_file() {
            return Ok(None);
        }
        let canonical = tokio::fs::canonicalize(&path).await?;
        if !canonical.starts_with(root) {
            bail!("incremental path escaped the registered root");
        }
        Ok(Some((
            canonical,
            relative
                .to_str()
                .context("incremental path is not UTF-8")?
                .to_string(),
        )))
    }

    #[cfg_attr(not(feature = "storage-v2-retrieval"), allow(dead_code))]
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

    #[tokio::test]
    async fn incremental_selection_rejects_external_and_symlink_paths_before_read() {
        let root = tempfile::tempdir().unwrap();
        let outside = tempfile::tempdir().unwrap();
        tokio::fs::write(root.path().join("session.jsonl"), "selected")
            .await
            .unwrap();
        tokio::fs::write(root.path().join("excluded.txt"), "excluded")
            .await
            .unwrap();
        tokio::fs::write(outside.path().join("secret.jsonl"), "outside")
            .await
            .unwrap();
        let canonical = tokio::fs::canonicalize(root.path()).await.unwrap();
        let scope = FilesystemScope::from_config(&json!({"file_patterns":["*.jsonl"]})).unwrap();
        assert_eq!(
            scope
                .incremental_path(&canonical, Path::new("session.jsonl"))
                .await
                .unwrap()
                .unwrap()
                .1,
            "session.jsonl"
        );
        assert!(scope
            .incremental_path(&canonical, Path::new("excluded.txt"))
            .await
            .unwrap()
            .is_none());
        assert!(scope
            .incremental_path(&canonical, Path::new("../secret.jsonl"))
            .await
            .is_err());
        assert!(scope
            .incremental_path(&canonical, &outside.path().join("secret.jsonl"))
            .await
            .is_err());
        std::os::unix::fs::symlink(outside.path(), root.path().join("escape")).unwrap();
        assert!(scope
            .incremental_path(&canonical, Path::new("escape/secret.jsonl"))
            .await
            .is_err());
    }

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
