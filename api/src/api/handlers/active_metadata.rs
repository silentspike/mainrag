//! Shared active metadata query for API, admin and MCP source inspection.

pub(super) const SOURCES_SQL: &str = r#"
SELECT source.*, source.type AS source_type,
       (metrics.value->>'file_count')::bigint AS active_file_count,
       (metrics.value->>'total_size')::bigint AS active_total_size,
       (metrics.value->>'view_count')::bigint AS active_view_count,
       (metrics.value->>'symbol_count')::bigint AS active_symbol_count,
       (metrics.value->>'call_count')::bigint AS active_call_count,
       (metrics.value->>'last_synced')::timestamptz AS active_last_synced
FROM sources source
CROSS JOIN LATERAL storage_v2_active_source_metrics($1,source.id,$3) metrics(value)
WHERE ($2::bigint IS NULL OR source.id=$2)
  AND ($3::boolean OR NOT source.is_test)
ORDER BY source.name,source.id
"#;

pub(super) fn source(row: &tokio_postgres::Row) -> crate::error::Result<crate::db::models::Source> {
    Ok(crate::db::models::Source {
        id: row.get("id"),
        name: row.get("name"),
        source_type: row.get("type"),
        path: row.get("path"),
        config: row.get("config"),
        last_synced: row.get("active_last_synced"),
        file_count: row
            .get::<_, i64>("active_file_count")
            .try_into()
            .map_err(|_| {
                crate::error::AppError::Internal("source file count exceeds the API range".into())
            })?,
        total_size: row.get("active_total_size"),
        created_at: row.get("created_at"),
    })
}
