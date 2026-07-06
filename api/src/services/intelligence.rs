//! Code Intelligence Service - Symbol und Call Graph Analyse

use anyhow::Result;
use deadpool_postgres::Pool;
use std::path::Path;

use super::parser::{CodeParser, ParseResult};
use crate::db::DEFAULT_USER_ID;

/// Code Intelligence Service
/// Thread-safe: CodeParser uses per-language Mutex internally, allowing concurrent parsing.
pub struct IntelligenceService {
    pool: Pool,
    parser: CodeParser,
}

impl IntelligenceService {
    pub fn new(pool: Pool) -> Result<Self> {
        Ok(Self {
            pool,
            parser: CodeParser::new()?,
        })
    }

    /// Get a database client with RLS context applied (using default admin user)
    async fn get_rls_client(&self) -> Result<deadpool_postgres::Client> {
        let client = self.pool.get().await?;
        // Dereference LazyLock<Uuid> to get Uuid, then convert to String for set_config
        let user_id_str = DEFAULT_USER_ID.to_string();
        client
            .execute(
                "SELECT set_config('app.user_id', $1::text, true)",
                &[&user_id_str],
            )
            .await?;
        Ok(client)
    }

    async fn record_analysis_result(
        client: &deadpool_postgres::Client,
        file_id: i64,
        symbols_count: usize,
        calls_count: usize,
    ) -> Result<()> {
        client
            .execute(
                "UPDATE files
                 SET intelligence_analyzed_at = NOW(),
                     intelligence_symbols_count = $2,
                     intelligence_calls_count = $3
                 WHERE id = $1",
                &[&file_id, &(symbols_count as i32), &(calls_count as i32)],
            )
            .await?;
        Ok(())
    }

    /// Parse a file and store symbols + call graph in database.
    /// Thread-safe: CodeParser uses per-language Mutex internally.
    ///
    /// Sprint 3.1: Batch-INSERTs via UNNEST — ~290+ DB roundtrips → ~4
    pub async fn analyze_file(
        &self,
        file_id: i64,
        path: &Path,
        content: &str,
    ) -> Result<ParseResult> {
        // Per-language locking: only the parser for this file's language is locked
        let result = self.parser.parse_file(path, content)?;

        // Single DB connection for all operations
        let client = self.get_rls_client().await?;

        // Re-analysis is file-replace semantics: remove stale graph edges and symbols
        // before inserting the current parser result. Without this, call_graph rows
        // accumulate because the table has no uniqueness constraint.
        let deleted_calls = client
            .execute(
                "DELETE FROM call_graph
                 WHERE caller_symbol_id IN (SELECT id FROM symbols WHERE file_id = $1)
                    OR callee_symbol_id IN (SELECT id FROM symbols WHERE file_id = $1)",
                &[&file_id],
            )
            .await?;
        let deleted_symbols = client
            .execute("DELETE FROM symbols WHERE file_id = $1", &[&file_id])
            .await?;

        if deleted_calls > 0 || deleted_symbols > 0 {
            tracing::debug!(
                file_id,
                deleted_calls,
                deleted_symbols,
                "Cleaned stale intelligence rows before re-analysis"
            );
        }

        if result.symbols.is_empty() && result.calls.is_empty() {
            Self::record_analysis_result(&client, file_id, 0, 0).await?;
            return Ok(result);
        }

        // --- Batch INSERT symbols via UNNEST ---
        let symbol_ids = if !result.symbols.is_empty() {
            let mut names: Vec<String> = Vec::with_capacity(result.symbols.len());
            let mut qualified_names: Vec<Option<String>> = Vec::with_capacity(result.symbols.len());
            let mut types: Vec<String> = Vec::with_capacity(result.symbols.len());
            let mut line_starts: Vec<i32> = Vec::with_capacity(result.symbols.len());
            let mut line_ends: Vec<i32> = Vec::with_capacity(result.symbols.len());
            let mut signatures: Vec<Option<String>> = Vec::with_capacity(result.symbols.len());
            let mut doc_comments: Vec<Option<String>> = Vec::with_capacity(result.symbols.len());
            let mut visibilities: Vec<Option<String>> = Vec::with_capacity(result.symbols.len());
            let mut languages: Vec<String> = Vec::with_capacity(result.symbols.len());

            for s in &result.symbols {
                names.push(s.name.clone());
                qualified_names.push(s.qualified_name.clone());
                types.push(s.symbol_type.to_string());
                line_starts.push(s.line_start as i32);
                line_ends.push(s.line_end as i32);
                signatures.push(s.signature.clone());
                doc_comments.push(s.doc_comment.clone());
                visibilities.push(s.visibility.clone());
                languages.push(s.language.clone());
            }

            let rows = client
                .query(
                    r#"
                INSERT INTO symbols (file_id, name, qualified_name, type, line_start, line_end,
                                     context, signature, doc_comment, visibility, language)
                SELECT $1, unnest($2::text[]), unnest($3::text[]), unnest($4::text[]),
                       unnest($5::int[]), unnest($6::int[]),
                       unnest($7::text[]), unnest($7::text[]),
                       unnest($8::text[]), unnest($9::text[]), unnest($10::text[])
                ON CONFLICT (file_id, name, line_start) DO UPDATE SET
                    qualified_name = EXCLUDED.qualified_name,
                    type = EXCLUDED.type,
                    line_end = EXCLUDED.line_end,
                    context = EXCLUDED.context,
                    signature = EXCLUDED.signature,
                    doc_comment = EXCLUDED.doc_comment,
                    visibility = EXCLUDED.visibility,
                    language = EXCLUDED.language
                RETURNING id, name
                "#,
                    &[
                        &file_id,
                        &names,
                        &qualified_names,
                        &types,
                        &line_starts,
                        &line_ends,
                        &signatures,
                        &doc_comments,
                        &visibilities,
                        &languages,
                    ],
                )
                .await?;

            // Build name→id map for call graph resolution
            let mut symbol_map = std::collections::HashMap::new();
            for row in &rows {
                let id: i64 = row.get(0);
                let name: String = row.get(1);
                symbol_map.insert(name, id);
            }
            symbol_map
        } else {
            std::collections::HashMap::new()
        };

        // --- Batch INSERT calls via UNNEST ---
        // FIX: Filter out calls where caller_symbol_id would be NULL,
        // because call_graph.caller_symbol_id is NOT NULL.
        // Without this filter, a single unresolved caller (e.g. "<global>")
        // causes the entire batch INSERT to fail, dropping ALL calls for the file.
        if !result.calls.is_empty() {
            let mut caller_ids: Vec<i64> = Vec::with_capacity(result.calls.len());
            let mut callee_names: Vec<String> = Vec::with_capacity(result.calls.len());
            let mut callee_ids: Vec<Option<i64>> = Vec::with_capacity(result.calls.len());
            let mut call_types: Vec<String> = Vec::with_capacity(result.calls.len());
            let mut call_lines: Vec<i32> = Vec::with_capacity(result.calls.len());

            for call in &result.calls {
                // Skip calls where the caller is not a known symbol (e.g. <global>, lambdas)
                if let Some(caller_id) = symbol_ids.get(&call.caller_name).copied() {
                    caller_ids.push(caller_id);
                    callee_names.push(call.callee_name.clone());
                    callee_ids.push(symbol_ids.get(&call.callee_name).copied());
                    call_types.push(call.call_type.to_string());
                    call_lines.push(call.call_line as i32);
                }
            }

            client.execute(
                r#"
                INSERT INTO call_graph (caller_symbol_id, callee_name, callee_symbol_id, call_type, call_line)
                SELECT unnest($1::bigint[]), unnest($2::text[]), unnest($3::bigint[]),
                       unnest($4::text[]), unnest($5::int[])
                ON CONFLICT DO NOTHING
                "#,
                &[&caller_ids, &callee_names, &callee_ids, &call_types, &call_lines],
            ).await?;
        }

        Self::record_analysis_result(&client, file_id, result.symbols.len(), result.calls.len())
            .await?;

        Ok(result)
    }

    /// Find symbols by name pattern
    #[allow(dead_code)]
    pub async fn find_symbols(
        &self,
        query: &str,
        source_id: Option<i64>,
        symbol_type: Option<&str>,
        limit: i32,
    ) -> Result<Vec<SymbolResult>> {
        let client = self.get_rls_client().await?;

        let rows = client
            .query(
                r#"
            SELECT s.id, s.name, s.qualified_name, s.type, s.line_start, s.line_end,
                   s.signature, s.visibility, s.language, f.path, src.name
            FROM symbols s
            JOIN files f ON s.file_id = f.id
            JOIN sources src ON f.source_id = src.id
            WHERE s.name ILIKE $1
              AND ($2::BIGINT IS NULL OR f.source_id = $2)
              AND ($3::TEXT IS NULL OR s.type = $3)
            ORDER BY s.name LIMIT $4
            "#,
                &[&format!("%{}%", query), &source_id, &symbol_type, &limit],
            )
            .await?;

        Ok(rows
            .iter()
            .map(|row| SymbolResult {
                id: row.get(0),
                name: row.get(1),
                qualified_name: row.get(2),
                symbol_type: row.get(3),
                line_start: row.get(4),
                line_end: row.get(5),
                signature: row.get(6),
                visibility: row.get(7),
                language: row.get(8),
                file_path: row.get(9),
                source_name: row.get(10),
            })
            .collect())
    }

    /// Find all callers of a function
    #[allow(dead_code)]
    pub async fn find_callers(
        &self,
        function_name: &str,
        source_id: Option<i64>,
    ) -> Result<Vec<CallerResult>> {
        let client = self.get_rls_client().await?;

        let rows = client
            .query(
                r#"
            SELECT caller.name, f.path, cg.call_line, cg.call_type, src.name
            FROM call_graph cg
            JOIN symbols caller ON cg.caller_symbol_id = caller.id
            JOIN files f ON caller.file_id = f.id
            JOIN sources src ON f.source_id = src.id
            WHERE cg.callee_name = $1
              AND ($2::BIGINT IS NULL OR f.source_id = $2)
            ORDER BY f.path, cg.call_line LIMIT 100
            "#,
                &[&function_name, &source_id],
            )
            .await?;

        Ok(rows
            .iter()
            .map(|row| CallerResult {
                caller_name: row.get(0),
                caller_file: row.get(1),
                call_line: row.get(2),
                call_type: row.get(3),
                source_name: row.get(4),
            })
            .collect())
    }

    /// Find all functions called by a function
    #[allow(dead_code)]
    pub async fn find_callees(
        &self,
        function_name: &str,
        source_id: Option<i64>,
    ) -> Result<Vec<CalleeResult>> {
        let client = self.get_rls_client().await?;

        let rows = client
            .query(
                r#"
            SELECT cg.callee_name, cg.callee_symbol_id IS NOT NULL, cg.call_line,
                   cg.call_type, f.path, src.name
            FROM call_graph cg
            JOIN symbols caller ON cg.caller_symbol_id = caller.id
            JOIN files f ON caller.file_id = f.id
            JOIN sources src ON f.source_id = src.id
            WHERE caller.name = $1
              AND ($2::BIGINT IS NULL OR f.source_id = $2)
            ORDER BY cg.call_line LIMIT 100
            "#,
                &[&function_name, &source_id],
            )
            .await?;

        Ok(rows
            .iter()
            .map(|row| CalleeResult {
                callee_name: row.get(0),
                resolved: row.get(1),
                call_line: row.get(2),
                call_type: row.get(3),
                caller_file: row.get(4),
                source_name: row.get(5),
            })
            .collect())
    }

    /// Find N-hop call chain — iterative BFS in Rust (not recursive SQL).
    /// Each hop is a single indexed query. Fan-out capped at 10 per node.
    pub async fn find_call_chain(
        &self,
        function_name: &str,
        direction: &str,
        max_depth: i32,
        source_id: Option<i64>,
    ) -> Result<Vec<CallChainEntry>> {
        let client = self.get_rls_client().await?;
        let max_depth = max_depth.clamp(1, 10) as usize;

        let mut results: Vec<CallChainEntry> = Vec::new();
        let mut visited: std::collections::HashSet<i64> = std::collections::HashSet::new();
        // Current frontier: symbol IDs to explore
        let mut frontier_names: Vec<String> = vec![function_name.to_string()];

        for depth in 1..=max_depth {
            if frontier_names.is_empty() {
                break;
            }

            let rows = if direction == "callees" {
                // Forward: find callees of frontier symbols
                client
                    .query(
                        "SELECT DISTINCT ON (cg.callee_name)
                            caller.name as from_name, cg.callee_name as to_name,
                            f.path, cg.call_line, cg.callee_symbol_id
                     FROM call_graph cg
                     JOIN symbols caller ON cg.caller_symbol_id = caller.id
                     JOIN files f ON caller.file_id = f.id
                     WHERE caller.name = ANY($1)
                       AND ($2::BIGINT IS NULL OR f.source_id = $2)
                     ORDER BY cg.callee_name
                     LIMIT 20",
                        &[&frontier_names, &source_id],
                    )
                    .await?
            } else {
                // Backward: find callers of frontier symbols
                client
                    .query(
                        "SELECT DISTINCT ON (caller.name)
                            caller.name as from_name, cg.callee_name as to_name,
                            f.path, cg.call_line, cg.caller_symbol_id
                     FROM call_graph cg
                     JOIN symbols caller ON cg.caller_symbol_id = caller.id
                     JOIN files f ON caller.file_id = f.id
                     WHERE cg.callee_name = ANY($1)
                       AND ($2::BIGINT IS NULL OR f.source_id = $2)
                     ORDER BY caller.name
                     LIMIT 20",
                        &[&frontier_names, &source_id],
                    )
                    .await?
            };

            let mut next_frontier = Vec::new();
            for row in &rows {
                let from_name: String = row.get(0);
                let to_name: String = row.get(1);
                let file_path: String = row.get(2);
                let line: i32 = row.get(3);
                let sym_id: Option<i64> = row.get(4);

                // Skip already visited symbols (cycle detection)
                if let Some(id) = sym_id {
                    if visited.contains(&id) {
                        continue;
                    }
                    visited.insert(id);
                }

                // Skip common noise functions
                let target = if direction == "callees" {
                    &to_name
                } else {
                    &from_name
                };
                if [
                    "SWC",
                    "r3B",
                    "assert",
                    "equals",
                    "toString",
                    "isControlSurfaceThread",
                    "isDocumentThread",
                    "exec",
                    "deprecated",
                ]
                .contains(&target.as_str())
                {
                    continue;
                }

                results.push(CallChainEntry {
                    depth: depth as u32,
                    from_name: from_name.clone(),
                    to_name: to_name.clone(),
                    file_path,
                    line,
                });

                // Next frontier: the newly discovered names
                let next_name = if direction == "callees" {
                    to_name
                } else {
                    from_name
                };
                if !frontier_names.contains(&next_name) {
                    next_frontier.push(next_name);
                }
            }

            frontier_names = next_frontier;
            if results.len() >= 100 {
                break;
            } // Safety cap
        }

        Ok(results)
    }

    #[allow(dead_code)]
    /// UNUSED — kept for reference. Original recursive SQL approach was too slow.
    async fn _find_call_chain_recursive(
        &self,
        function_name: &str,
        direction: &str,
        max_depth: i32,
        source_id: Option<i64>,
    ) -> Result<Vec<CallChainEntry>> {
        let client = self.get_rls_client().await?;
        let max_depth = max_depth.min(10);

        let rows = if direction == "callees" {
            // Forward: what does this function call, and what do THOSE call?
            // Uses callee_symbol_id (indexed integer FK) for fast recursive traversal.
            client.query(
                r#"
                WITH RECURSIVE chain AS (
                    -- Seed: direct callees of the target function
                    SELECT cg.callee_symbol_id, cg.callee_name,
                           caller.name as via_name, f.path as via_file,
                           cg.call_line, 1 as depth,
                           ARRAY[cg.caller_symbol_id] as visited
                    FROM call_graph cg
                    JOIN symbols caller ON cg.caller_symbol_id = caller.id
                    JOIN files f ON caller.file_id = f.id
                    WHERE caller.name = $1
                      AND cg.callee_symbol_id IS NOT NULL
                      AND ($3::BIGINT IS NULL OR f.source_id = $3)

                    UNION ALL

                    -- Recurse via symbol ID (fast index join), fan-out capped at 10 per node
                    SELECT cg.callee_symbol_id, cg.callee_name,
                           s2.name, f2.path,
                           cg.call_line, c.depth + 1,
                           c.visited || cg.caller_symbol_id
                    FROM chain c
                    JOIN LATERAL (
                        SELECT cg2.callee_symbol_id, cg2.callee_name, cg2.caller_symbol_id, cg2.call_line
                        FROM call_graph cg2
                        WHERE cg2.caller_symbol_id = c.callee_symbol_id
                          AND cg2.callee_symbol_id IS NOT NULL
                          AND NOT cg2.caller_symbol_id = ANY(c.visited)
                        LIMIT 10
                    ) cg ON true
                    JOIN symbols s2 ON cg.caller_symbol_id = s2.id
                    JOIN files f2 ON s2.file_id = f2.id
                    WHERE c.depth < $2
                      AND ($3::BIGINT IS NULL OR f2.source_id = $3)
                )
                SELECT DISTINCT depth, via_name, callee_name, via_file, call_line
                FROM chain
                ORDER BY depth, via_name, callee_name
                LIMIT 100
                "#,
                &[&function_name, &max_depth, &source_id],
            ).await?
        } else {
            // Backward: who calls this, and who calls THOSE callers?
            // Uses caller_symbol_id for reverse traversal.
            client
                .query(
                    r#"
                WITH RECURSIVE chain AS (
                    -- Seed: functions that call the target
                    SELECT cg.caller_symbol_id,
                           caller.name as caller_name, cg.callee_name,
                           f.path as caller_file, cg.call_line, 1 as depth,
                           ARRAY[cg.caller_symbol_id] as visited
                    FROM call_graph cg
                    JOIN symbols caller ON cg.caller_symbol_id = caller.id
                    JOIN files f ON caller.file_id = f.id
                    WHERE cg.callee_name = $1
                      AND ($3::BIGINT IS NULL OR f.source_id = $3)

                    UNION ALL

                    -- Who calls THOSE callers? Fan-out capped at 10.
                    SELECT cg.caller_symbol_id,
                           s2.name, cg.callee_name,
                           f2.path, cg.call_line, c.depth + 1,
                           c.visited || cg.caller_symbol_id
                    FROM chain c
                    JOIN LATERAL (
                        SELECT cg2.caller_symbol_id, cg2.callee_name, cg2.call_line
                        FROM call_graph cg2
                        WHERE cg2.callee_symbol_id = c.caller_symbol_id
                          AND NOT cg2.caller_symbol_id = ANY(c.visited)
                        LIMIT 10
                    ) cg ON true
                    JOIN symbols s2 ON cg.caller_symbol_id = s2.id
                    JOIN files f2 ON s2.file_id = f2.id
                    WHERE c.depth < $2
                      AND ($3::BIGINT IS NULL OR f2.source_id = $3)
                )
                SELECT DISTINCT depth, caller_name, callee_name, caller_file, call_line
                FROM chain
                ORDER BY depth, caller_name, callee_name
                LIMIT 100
                "#,
                    &[&function_name, &max_depth, &source_id],
                )
                .await?
        };

        Ok(rows
            .iter()
            .map(|row| CallChainEntry {
                depth: row.get::<_, i32>(0) as u32,
                from_name: row.get(1),
                to_name: row.get(2),
                file_path: row.get(3),
                line: row.get(4),
            })
            .collect())
    }

    /// Get stats
    #[allow(dead_code)]
    pub async fn get_stats(&self, source_id: Option<i64>) -> Result<IntelligenceStats> {
        let client = self.get_rls_client().await?;

        let symbols_count: i64 = client.query_one(
            "SELECT COUNT(*) FROM symbols s JOIN files f ON s.file_id = f.id WHERE $1::BIGINT IS NULL OR f.source_id = $1",
            &[&source_id],
        ).await?.get(0);

        let calls_count: i64 = client.query_one(
            "SELECT COUNT(*) FROM call_graph cg JOIN symbols s ON cg.caller_symbol_id = s.id JOIN files f ON s.file_id = f.id WHERE $1::BIGINT IS NULL OR f.source_id = $1",
            &[&source_id],
        ).await?.get(0);

        let lang_rows = client.query(
            "SELECT s.language, COUNT(*) FROM symbols s JOIN files f ON s.file_id = f.id WHERE $1::BIGINT IS NULL OR f.source_id = $1 GROUP BY s.language ORDER BY COUNT(*) DESC",
            &[&source_id],
        ).await?;

        let languages: Vec<(String, i64)> = lang_rows
            .iter()
            .map(|row| (row.get(0), row.get(1)))
            .collect();

        Ok(IntelligenceStats {
            symbols_count,
            calls_count,
            languages,
        })
    }
}

#[allow(dead_code)]
#[derive(Debug, serde::Serialize)]
pub struct SymbolResult {
    pub id: i64,
    pub name: String,
    pub qualified_name: Option<String>,
    pub symbol_type: String,
    pub line_start: i32,
    pub line_end: i32,
    pub signature: Option<String>,
    pub visibility: Option<String>,
    pub language: String,
    pub file_path: String,
    pub source_name: String,
}

#[allow(dead_code)]
#[derive(Debug, serde::Serialize)]
pub struct CallerResult {
    pub caller_name: String,
    pub caller_file: String,
    pub call_line: i32,
    pub call_type: String,
    pub source_name: String,
}

#[allow(dead_code)]
#[derive(Debug, serde::Serialize)]
pub struct CalleeResult {
    pub callee_name: String,
    pub resolved: bool,
    pub call_line: i32,
    pub call_type: String,
    pub caller_file: String,
    pub source_name: String,
}

#[derive(Debug, serde::Serialize)]
pub struct CallChainEntry {
    pub depth: u32,
    pub from_name: String,
    pub to_name: String,
    pub file_path: String,
    pub line: i32,
}

#[allow(dead_code)]
#[derive(Debug, serde::Serialize)]
pub struct IntelligenceStats {
    pub symbols_count: i64,
    pub calls_count: i64,
    pub languages: Vec<(String, i64)>,
}

// =============================================================================
// Intelligence Layer: Symbol Cards + Annotations + Negative Evidence
// =============================================================================

use crate::db::models::{
    AnnotationInfo, DelegationChain, DelegationStep, NegativeEvidence, SymbolCard,
};

impl IntelligenceService {
    /// Get a single symbol card by symbol_id.
    /// LEFT JOINs symbol_cards so cards without enrichment still return (with None fields).
    pub async fn get_symbol_card(&self, symbol_id: i64) -> Result<Option<SymbolCard>> {
        let client = self.get_rls_client().await?;
        let row = client
            .query_opt(
                r#"
            SELECT s.id, s.name, s.qualified_name, s.type, s.signature,
                   f.path, s.line_start, s.line_end, src.name as source_name,
                   s.visibility,
                   sc.layer, sc.side_effect_type, sc.affected_resource,
                   sc.delegation_targets, sc.thread_requirement, sc.preconditions,
                   sc.summary, sc.classification_confidence, sc.domain_profile
            FROM symbols s
            JOIN files f ON s.file_id = f.id
            JOIN sources src ON f.source_id = src.id
            LEFT JOIN symbol_cards sc ON sc.symbol_id = s.id
            WHERE s.id = $1
            "#,
                &[&symbol_id],
            )
            .await?;

        Ok(row.map(|r| row_to_symbol_card(&r)))
    }

    /// Search symbol cards by name pattern with optional filters.
    pub async fn search_symbol_cards(
        &self,
        name: &str,
        source_id: Option<i64>,
        layer: Option<&str>,
        resource: Option<&str>,
        side_effect: Option<&str>,
        limit: i32,
    ) -> Result<Vec<SymbolCard>> {
        let client = self.get_rls_client().await?;
        let search_pattern = format!("%{}%", name);

        // Convert Option<&str> to Option<String> for tokio-postgres serialization
        let layer_owned = layer.map(|s| s.to_string());
        let resource_owned = resource.map(|s| s.to_string());
        let side_effect_owned = side_effect.map(|s| s.to_string());

        let rows = client
            .query(
                r#"
            SELECT s.id, s.name, s.qualified_name, s.type, s.signature,
                   f.path, s.line_start, s.line_end, src.name as source_name,
                   s.visibility,
                   sc.layer, sc.side_effect_type, sc.affected_resource,
                   sc.delegation_targets, sc.thread_requirement, sc.preconditions,
                   sc.summary, sc.classification_confidence, sc.domain_profile
            FROM symbols s
            JOIN files f ON s.file_id = f.id
            JOIN sources src ON f.source_id = src.id
            LEFT JOIN symbol_cards sc ON sc.symbol_id = s.id
            WHERE s.name ILIKE $1
              AND ($2::BIGINT IS NULL OR f.source_id = $2)
              AND ($3::TEXT IS NULL OR sc.layer = $3)
              AND ($4::TEXT IS NULL OR sc.affected_resource = $4)
              AND ($5::TEXT IS NULL OR sc.side_effect_type = $5)
            ORDER BY sc.classification_confidence DESC NULLS LAST, s.name
            LIMIT $6
            "#,
                &[
                    &search_pattern,
                    &source_id,
                    &layer_owned,
                    &resource_owned,
                    &side_effect_owned,
                    &(limit as i64),
                ],
            )
            .await?;

        Ok(rows.iter().map(row_to_symbol_card).collect())
    }

    /// Get annotations for a symbol.
    pub async fn get_annotations(&self, symbol_id: i64) -> Result<Vec<AnnotationInfo>> {
        let client = self.get_rls_client().await?;
        let rows = client
            .query(
                r#"
            SELECT annotation_type, value, confidence
            FROM symbol_annotations
            WHERE symbol_id = $1
            ORDER BY annotation_type, value
            "#,
                &[&symbol_id],
            )
            .await?;

        Ok(rows
            .iter()
            .map(|r| AnnotationInfo {
                annotation_type: r.get(0),
                value: r.get(1),
                confidence: r.get(2),
            })
            .collect())
    }

    /// Get ownership/containment relations for a symbol name.
    /// Looks up entities by name, then queries entity_relations.
    pub async fn get_ownership(
        &self,
        symbol_name: &str,
        _source_id: Option<i64>,
    ) -> Result<Vec<crate::db::models::OwnershipInfo>> {
        let client = self.get_rls_client().await?;
        let search_pattern = format!("%{}%", symbol_name);
        let exact_name = symbol_name.to_string();

        // Bidirectional query with exact-match-first ranking.
        // Exact matches (match_rank=0) sort before ILIKE contains matches (match_rank=1).
        let rows = client.query(
            r#"
            SELECT source_name, relation_type, direction, target_name, confidence, evidence_line, target_file
            FROM (
                SELECT e_src.name as source_name, er.relation_type, 'outgoing' as direction,
                       e_tgt.name as target_name, er.confidence,
                       (er.metadata->>'evidence_line')::int as evidence_line,
                       (SELECT f.path FROM files f JOIN symbols s ON s.file_id = f.id
                        WHERE s.id = (e_tgt.metadata->>'symbol_id')::bigint LIMIT 1) as target_file,
                       CASE WHEN e_src.name = $2 THEN 0 ELSE 1 END as match_rank
                FROM entity_relations er
                JOIN entities e_src ON er.source_entity_id = e_src.id
                JOIN entities e_tgt ON er.target_entity_id = e_tgt.id
                WHERE e_src.name ILIKE $1

                UNION ALL

                SELECT e_tgt.name, er.relation_type, 'incoming',
                       e_src.name, er.confidence,
                       (er.metadata->>'evidence_line')::int,
                       (SELECT f.path FROM files f JOIN symbols s ON s.file_id = f.id
                        WHERE s.id = (e_src.metadata->>'symbol_id')::bigint LIMIT 1),
                       CASE WHEN e_tgt.name = $2 THEN 0 ELSE 1 END
                FROM entity_relations er
                JOIN entities e_src ON er.source_entity_id = e_src.id
                JOIN entities e_tgt ON er.target_entity_id = e_tgt.id
                WHERE e_tgt.name ILIKE $1
            ) sub
            ORDER BY match_rank, direction, relation_type, target_name
            LIMIT 50
            "#,
            &[&search_pattern, &exact_name],
        ).await?;

        Ok(rows
            .iter()
            .map(|r| crate::db::models::OwnershipInfo {
                symbol_name: r.get(0),
                relation_type: r.get(1),
                direction: r.get(2),
                target_name: r.get(3),
                confidence: r.get::<_, f64>(4) as f32,
                evidence_line: r.get(5),
                target_file: r.get(6),
            })
            .collect())
    }

    /// Create a negative evidence entry.
    #[allow(clippy::too_many_arguments)]
    pub async fn create_negative_evidence(
        &self,
        source_id: Option<i64>,
        domain_profile: Option<&str>,
        concept: &str,
        path_description: &str,
        reason: &str,
        symbols: &serde_json::Value,
        severity: &str,
        created_by: Option<&str>,
    ) -> Result<i64> {
        let client = self.get_rls_client().await?;
        let row = client.query_one(
            r#"
            INSERT INTO negative_evidence
                (source_id, domain_profile, concept, path_description, reason, symbols, severity, created_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING id
            "#,
            &[&source_id, &domain_profile, &concept, &path_description, &reason, &symbols, &severity, &created_by],
        ).await?;
        Ok(row.get(0))
    }

    /// Search negative evidence by concept (FTS).
    pub async fn search_negative_evidence(
        &self,
        concept: &str,
        source_id: Option<i64>,
    ) -> Result<Vec<NegativeEvidence>> {
        let client = self.get_rls_client().await?;

        // Use FTS for better matching (websearch handles natural language queries)
        let rows = client
            .query(
                r#"
            SELECT id, concept, path_description, reason, symbols, severity,
                   created_by, domain_profile
            FROM negative_evidence
            WHERE to_tsvector('simple', concept) @@ websearch_to_tsquery('simple', $1)
              AND ($2::BIGINT IS NULL OR source_id = $2)
            ORDER BY created_at DESC
            LIMIT 20
            "#,
                &[&concept, &source_id],
            )
            .await?;

        Ok(rows
            .iter()
            .map(|r| NegativeEvidence {
                id: r.get(0),
                concept: r.get(1),
                path_description: r.get(2),
                reason: r.get(3),
                symbols: r.get(4),
                severity: r.get(5),
                created_by: r.get(6),
                domain_profile: r.get(7),
            })
            .collect())
    }

    /// Trace delegation chain from a symbol through proxy → dispatch → mutation.
    /// Returns multiple chains if the symbol name has overloads.
    pub async fn trace_delegation_chain(
        &self,
        symbol_name: &str,
        source_id: Option<i64>,
        max_depth: u32,
    ) -> Result<Vec<DelegationChain>> {
        let max_depth = max_depth.min(10);
        let client = self.get_rls_client().await?;

        // Find all symbols matching the name
        let entry_cards = self
            .search_symbol_cards(symbol_name, source_id, None, None, None, 10)
            .await?;
        if entry_cards.is_empty() {
            return Ok(vec![]);
        }

        let mut chains = Vec::new();

        for entry in &entry_cards {
            let mut steps = Vec::new();
            let mut visited = std::collections::HashSet::new();
            visited.insert(entry.symbol_id);

            // Walk delegation_targets recursively
            let mut current_targets = entry
                .delegation_targets
                .clone()
                .and_then(|v| serde_json::from_value::<Vec<DelegationTarget>>(v).ok())
                .unwrap_or_default();

            let mut depth = 0;
            let mut current_caller_id = entry.symbol_id;
            let mut current_caller_name = entry.name.clone();
            while depth < max_depth && !current_targets.is_empty() {
                // Pick primary target using 4-stage caller-scoped prioritization
                let target = match pick_primary_target(&current_targets, &current_caller_name) {
                    Some(t) => t.clone(),
                    None => break,
                };

                // Get code snippet from call_graph.call_line (using CURRENT caller, not entry)
                let snippet = self
                    .get_call_site_snippet(&client, current_caller_id, &target.name)
                    .await
                    .ok()
                    .flatten();

                // Try to load the target's symbol card
                let target_card = if let Some(tid) = target.symbol_id {
                    if visited.contains(&tid) {
                        break;
                    }
                    visited.insert(tid);
                    self.get_symbol_card(tid).await.ok().flatten()
                } else {
                    // Resolve by name
                    let results = self
                        .search_symbol_cards(&target.name, source_id, None, None, None, 1)
                        .await?;
                    if let Some(card) = results.into_iter().next() {
                        if visited.contains(&card.symbol_id) {
                            break;
                        }
                        visited.insert(card.symbol_id);
                        Some(card)
                    } else {
                        None
                    }
                };

                // Get annotations for this step
                let step_anns = if let Some(ref card) = target_card {
                    self.get_annotations(card.symbol_id)
                        .await
                        .unwrap_or_default()
                } else {
                    vec![]
                };

                let step_card = target_card.unwrap_or_else(|| SymbolCard {
                    symbol_id: target.symbol_id.unwrap_or(0),
                    name: target.name.clone(),
                    qualified_name: None,
                    symbol_type: "unknown".to_string(),
                    signature: None,
                    file_path: String::new(),
                    line_start: 0,
                    line_end: 0,
                    source_name: String::new(),
                    visibility: None,
                    layer: None,
                    side_effect_type: None,
                    affected_resource: None,
                    delegation_targets: None,
                    thread_requirement: None,
                    preconditions: None,
                    summary: None,
                    classification_confidence: None,
                    domain_profile: None,
                });

                // Get next delegation targets from this step's card
                let next_targets = step_card
                    .delegation_targets
                    .clone()
                    .and_then(|v| serde_json::from_value::<Vec<DelegationTarget>>(v).ok())
                    .unwrap_or_default();

                // Update current caller for next iteration (Fix 2b: snippet bug)
                current_caller_id = step_card.symbol_id;
                current_caller_name = step_card.name.clone();

                steps.push(DelegationStep {
                    symbol: step_card,
                    role: target.role,
                    dispatch_via: target.dispatch_via,
                    code_snippet: snippet,
                    step_annotations: step_anns,
                });

                current_targets = next_targets;
                depth += 1;
            }

            // Collect all annotations from the entry point
            let entry_anns = self
                .get_annotations(entry.symbol_id)
                .await
                .unwrap_or_default();

            chains.push(DelegationChain {
                entry_point: entry.clone(),
                steps,
                annotations: entry_anns,
            });
        }

        Ok(chains)
    }

    /// Get a code snippet around a call site (5 lines around call_line).
    async fn get_call_site_snippet(
        &self,
        client: &deadpool_postgres::Client,
        caller_symbol_id: i64,
        callee_name: &str,
    ) -> Result<Option<String>> {
        let row = client
            .query_opt(
                r#"
            SELECT cg.call_line, c.content_text, c.start_line
            FROM call_graph cg
            JOIN symbols s ON cg.caller_symbol_id = s.id
            JOIN chunks c ON c.file_id = s.file_id
                AND c.start_line <= cg.call_line AND c.end_line >= cg.call_line
                AND c.content_text IS NOT NULL
            WHERE cg.caller_symbol_id = $1 AND cg.callee_name = $2
            ORDER BY (c.end_line - c.start_line) ASC
            LIMIT 1
            "#,
                &[&caller_symbol_id, &callee_name],
            )
            .await?;

        Ok(row.and_then(|r| {
            let call_line: i32 = r.get(0);
            let content: Option<String> = r.get(1);
            let chunk_start: i32 = r.get(2);
            content.map(|text| {
                let lines: Vec<&str> = text.lines().collect();
                let rel_line = (call_line - chunk_start) as usize;
                let start = rel_line.saturating_sub(2);
                let end = (rel_line + 3).min(lines.len());
                lines[start..end].join("\n")
            })
        }))
    }

    /// Explore: Orchestrated query that combines domain rewriting, symbol search,
    /// path tracing, and negative evidence into a single structured response.
    pub async fn explore(
        &self,
        query: &str,
        source_name: Option<&str>,
        domain_registry: Option<&crate::services::domain_profile::DomainProfileRegistry>,
    ) -> Result<crate::db::models::ExploreResponse> {
        use crate::db::models::{CandidatePath, ExploreResponse, SuggestedQuery};

        // 1. Resolve domain profile and expand query
        let mut symbol_queries: Vec<String> = vec![query.to_string()];
        let mut intent: Option<String> = None;
        let mut domain_name: Option<String> = None;
        let mut operation_symbol_names: std::collections::HashSet<String> =
            std::collections::HashSet::new();

        if let (Some(registry), Some(src)) = (domain_registry, source_name) {
            if let Some(expansion) = registry.expand_query(query, src) {
                domain_name = Some(expansion.domain);
                intent = expansion.intent.clone();
                for sym in &expansion.symbol_expansions {
                    symbol_queries.push(sym.clone());
                }
                for sym in &expansion.operation_symbols {
                    symbol_queries.push(sym.clone());
                    operation_symbol_names.insert(sym.to_lowercase());
                }
            }
        }

        // 2. Resolve source_id
        let source_id = if let Some(src) = source_name {
            let client = self.get_rls_client().await?;
            let row = client
                .query_opt(
                    "SELECT id FROM sources WHERE name = $1",
                    &[&src.to_string()],
                )
                .await?;
            row.map(|r| r.get::<_, i64>(0))
        } else {
            None
        };

        // 3. Search for symbols using all expanded queries
        let mut all_cards: Vec<SymbolCard> = Vec::new();
        let mut seen_ids = std::collections::HashSet::new();

        for sq in &symbol_queries {
            let cards = self
                .search_symbol_cards(sq, source_id, None, None, None, 10)
                .await?;
            for card in cards {
                if seen_ids.insert(card.symbol_id) {
                    all_cards.push(card);
                }
            }
        }

        // 4. Load negative evidence FIRST (before candidate selection)
        let negative = self
            .search_negative_evidence(query, None)
            .await
            .unwrap_or_default();

        // Build dead-end symbol set for demotion
        let dead_end_symbols: std::collections::HashSet<String> = negative
            .iter()
            .flat_map(|ne| {
                let mut names = vec![ne.path_description.clone()];
                if let Some(arr) = ne.symbols.as_array() {
                    names.extend(arr.iter().filter_map(|v| v.as_str().map(String::from)));
                }
                names
            })
            .collect();

        // Dead-end reason lookup for why_might_not_work
        let dead_end_reasons: std::collections::HashMap<String, String> = negative
            .iter()
            .flat_map(|ne| {
                let reason = ne.reason.clone();
                let mut entries = vec![(ne.path_description.clone(), reason.clone())];
                if let Some(arr) = ne.symbols.as_array() {
                    for v in arr.iter().filter_map(|v| v.as_str()) {
                        entries.push((v.to_string(), reason.clone()));
                    }
                }
                entries
            })
            .collect();

        // 5. Three-stage candidate selection
        all_cards.sort_by(|a, b| {
            let ca = a.classification_confidence.unwrap_or(0.0);
            let cb = b.classification_confidence.unwrap_or(0.0);
            cb.partial_cmp(&ca).unwrap_or(std::cmp::Ordering::Equal)
        });

        // Stage 1: Intent-match — PRIMARY (operation_symbols from profile) before SECONDARY (generic)
        let mut intent_candidates: Vec<&SymbolCard> = Vec::new();
        if let Some(ref intent) = intent {
            // Primary: direct profile operation matches (e.g. createEmptyClip from operations.create)
            let primary: Vec<&SymbolCard> = all_cards
                .iter()
                .filter(|c| c.side_effect_type.as_deref() == Some(intent.as_str()))
                .filter(|c| operation_symbol_names.contains(&c.name.to_lowercase()))
                .filter(|c| !dead_end_symbols.contains(&c.name))
                .take(2)
                .collect();

            intent_candidates = primary;

            // Secondary: generic intent matches (only if primary < 2)
            if intent_candidates.len() < 2 {
                let remaining = 2 - intent_candidates.len();
                let secondary: Vec<&SymbolCard> = all_cards
                    .iter()
                    .filter(|c| c.side_effect_type.as_deref() == Some(intent.as_str()))
                    .filter(|c| {
                        !intent_candidates
                            .iter()
                            .any(|ic| ic.symbol_id == c.symbol_id)
                    })
                    .filter(|c| !dead_end_symbols.contains(&c.name))
                    .take(remaining)
                    .collect();
                intent_candidates.extend(secondary);
            }
        }

        // Stage 2: Chain candidates — ONLY if intent didn't fill enough slots
        let chain_candidates: Vec<&SymbolCard> = if intent_candidates.len() < 2 {
            let remaining = 2 - intent_candidates.len();
            all_cards
                .iter()
                .filter(|c| {
                    c.delegation_targets
                        .as_ref()
                        .and_then(|v| v.as_array())
                        .map(|a| !a.is_empty())
                        .unwrap_or(false)
                })
                .filter(|c| {
                    !intent_candidates
                        .iter()
                        .any(|ic| ic.symbol_id == c.symbol_id)
                })
                .filter(|c| !dead_end_symbols.contains(&c.name))
                .take(remaining)
                .collect()
        } else {
            vec![]
        };

        // Merge: intent first, then chain, truncate to 3
        let mut top_candidates: Vec<&SymbolCard> = intent_candidates;
        top_candidates.extend(chain_candidates);
        top_candidates.truncate(3);

        // Stage 3: Dead-end candidates at the end (demoted, not excluded)
        let dead_end_candidates: Vec<&SymbolCard> = all_cards
            .iter()
            .filter(|c| dead_end_symbols.contains(&c.name))
            .filter(|c| !top_candidates.iter().any(|tc| tc.symbol_id == c.symbol_id))
            .take(1)
            .collect();
        top_candidates.extend(dead_end_candidates);

        // Fallback if everything is empty
        if top_candidates.is_empty() {
            top_candidates = all_cards.iter().take(3).collect();
        }

        // 6. Trace delegation chains for top candidates
        let mut candidate_paths = Vec::new();
        for (i, card) in top_candidates.iter().enumerate() {
            let chains = self
                .trace_delegation_chain(&card.name, source_id, 6)
                .await?;

            // Pick the best chain (one with most steps), or empty chain for interfaces
            let chain = chains
                .into_iter()
                .max_by_key(|c| c.steps.len())
                .unwrap_or_else(|| DelegationChain {
                    entry_point: (*card).clone(),
                    steps: vec![],
                    annotations: vec![],
                });

            let title = format!(
                "Via {} [{}]",
                card.name,
                card.layer.as_deref().unwrap_or("?")
            );

            let confidence = if card.classification_confidence.unwrap_or(0.0) >= 0.8 {
                "high".to_string()
            } else if card.classification_confidence.unwrap_or(0.0) >= 0.5 {
                "medium".to_string()
            } else {
                "low".to_string()
            };

            // Check dead-end status for this candidate and its chain steps
            let mut dead_end_reason: Option<String> = None;
            if let Some(reason) = dead_end_reasons.get(&card.name) {
                dead_end_reason = Some(format!(
                    "KNOWN DEAD END (matched symbol: {}): {}",
                    card.name, reason
                ));
            }
            for step in &chain.steps {
                if dead_end_reason.is_some() {
                    break;
                }
                if let Some(reason) = dead_end_reasons.get(&step.symbol.name) {
                    dead_end_reason = Some(format!(
                        "KNOWN DEAD END (matched chain step: {}): {}",
                        step.symbol.name, reason
                    ));
                }
            }

            candidate_paths.push(CandidatePath {
                rank: (i + 1) as u32,
                title,
                confidence,
                chain,
                why_relevant: card.summary.clone(),
                why_might_not_work: dead_end_reason,
            });
        }

        // 7. Generate suggested next queries
        let mut suggested = Vec::new();
        if let Some(first) = candidate_paths.first() {
            suggested.push(SuggestedQuery {
                query: format!(
                    "mainrag call-graph {} --source {}",
                    first.chain.entry_point.name,
                    source_name.unwrap_or("?")
                ),
                rationale: "Find all callers/callees of the entry point".to_string(),
            });
        }
        if all_cards.len() > 3 {
            suggested.push(SuggestedQuery {
                query: format!(
                    "mainrag card {} --source {}",
                    all_cards[0].name,
                    source_name.unwrap_or("?")
                ),
                rationale: "Inspect the top-ranked symbol card in detail".to_string(),
            });
        }

        // 8. Format as structured text for LLM
        let formatted = format_explore_response(
            query,
            intent.as_deref(),
            domain_name.as_deref(),
            source_name,
            &candidate_paths,
            &negative,
            &suggested,
        );

        Ok(ExploreResponse {
            query: query.to_string(),
            intent,
            domain: domain_name,
            candidate_paths,
            negative_evidence: negative,
            suggested_next: suggested,
            formatted,
        })
    }
}

/// Format explore response as structured text for direct LLM consumption.
fn format_explore_response(
    query: &str,
    intent: Option<&str>,
    domain: Option<&str>,
    source: Option<&str>,
    paths: &[crate::db::models::CandidatePath],
    negative: &[NegativeEvidence],
    suggested: &[crate::db::models::SuggestedQuery],
) -> String {
    let mut out = String::new();

    out.push_str(&format!("## Explore: \"{}\"\n", query));
    if let Some(i) = intent {
        out.push_str(&format!("Intent: {} | ", i.to_uppercase()));
    }
    if let Some(d) = domain {
        out.push_str(&format!("Domain: {} | ", d));
    }
    if let Some(s) = source {
        out.push_str(&format!("Source: {}", s));
    }
    out.push_str("\n\n");

    for path in paths {
        out.push_str(&format!(
            "### Path {} ({}): {}\n",
            path.rank, path.confidence, path.title
        ));

        if let Some(ref thread) = path.chain.entry_point.thread_requirement {
            out.push_str(&format!("Thread: {}\n", thread));
        }

        out.push_str("Chain:\n");
        out.push_str(&format!(
            "  {} [{}:{}]\n",
            path.chain.entry_point.name,
            path.chain.entry_point.file_path,
            path.chain.entry_point.line_start
        ));

        for step in &path.chain.steps {
            let dispatch = step
                .dispatch_via
                .as_ref()
                .map(|d| format!(" via {}", d))
                .unwrap_or_default();
            out.push_str(&format!(
                "    -> [{}]{} {} [{}:{}]\n",
                step.role,
                dispatch,
                step.symbol.name,
                step.symbol.file_path,
                step.symbol.line_start
            ));

            if let Some(ref snippet) = step.code_snippet {
                for line in snippet.lines().take(3) {
                    out.push_str(&format!("       {}\n", line));
                }
            }
        }

        if let Some(ref why) = path.why_relevant {
            out.push_str(&format!("Why: {}\n", why));
        }
        out.push('\n');
    }

    if paths.is_empty() {
        out.push_str("No candidate paths found for this query.\n\n");
    }

    if !negative.is_empty() {
        out.push_str("### Dead Ends:\n");
        for ne in negative {
            out.push_str(&format!(
                "- {} -> {} ({})\n",
                ne.concept, ne.path_description, ne.reason
            ));
        }
        out.push('\n');
    }

    if !suggested.is_empty() {
        out.push_str("### Suggested next queries:\n");
        for s in suggested {
            out.push_str(&format!("- `{}` - {}\n", s.query, s.rationale));
        }
    }

    out
}

/// Pick the best delegation target using 4-stage caller-scoped prioritization.
/// No global helper patterns — purely context-based.
fn pick_primary_target(
    targets: &[DelegationTarget],
    caller_name: &str,
) -> Option<DelegationTarget> {
    if targets.is_empty() {
        return None;
    }

    let cn = caller_name.to_lowercase();

    // Stage 1: Name overlap (delegation convention)
    // e.g. createEmptyClipImpl contains "createEmptyClip"
    if let Some(t) = targets.iter().find(|t| {
        let tn = t.name.to_lowercase();
        (tn.len() > 3 && cn.len() > 3) && (tn.contains(&cn) || cn.contains(&tn))
    }) {
        return Some(t.clone());
    }

    // Stage 2: dispatch/mutation role (from enricher)
    if let Some(t) = targets
        .iter()
        .find(|t| t.role == "dispatch" || t.role == "mutation")
    {
        return Some(t.clone());
    }

    // Stage 3: Highest confidence among non-unknown targets
    if let Some(t) = targets
        .iter()
        .filter(|t| t.role != "unknown")
        .max_by(|a, b| {
            a.confidence
                .unwrap_or(0.0)
                .partial_cmp(&b.confidence.unwrap_or(0.0))
                .unwrap_or(std::cmp::Ordering::Equal)
        })
    {
        return Some(t.clone());
    }

    // Stage 4: Fallback — highest confidence overall
    targets
        .iter()
        .max_by(|a, b| {
            a.confidence
                .unwrap_or(0.0)
                .partial_cmp(&b.confidence.unwrap_or(0.0))
                .unwrap_or(std::cmp::Ordering::Equal)
        })
        .cloned()
}

/// Helper to deserialize delegation_targets JSONB entries.
#[derive(Debug, Clone, serde::Deserialize)]
struct DelegationTarget {
    name: String,
    symbol_id: Option<i64>,
    role: String,
    dispatch_via: Option<String>,
    #[allow(dead_code)]
    confidence: Option<f32>,
}

/// Convert a postgres Row to SymbolCard.
fn row_to_symbol_card(r: &tokio_postgres::Row) -> SymbolCard {
    SymbolCard {
        symbol_id: r.get(0),
        name: r.get(1),
        qualified_name: r.get(2),
        symbol_type: r.get(3),
        signature: r.get(4),
        file_path: r.get(5),
        line_start: r.get(6),
        line_end: r.get(7),
        source_name: r.get(8),
        visibility: r.get(9),
        layer: r.get(10),
        side_effect_type: r.get(11),
        affected_resource: r.get(12),
        delegation_targets: r.get(13),
        thread_requirement: r.get(14),
        preconditions: r.get(15),
        summary: r.get(16),
        classification_confidence: r.get(17),
        domain_profile: r.get(18),
    }
}
