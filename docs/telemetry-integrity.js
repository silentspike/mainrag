/* Public, dependency-free presentation policy for candidate integrity metrics. */
(function (root) {
  'use strict';

  const fields = {
    query_client_ms: ['Candidate binding verification: client wall time', 'ms_roh'],
    item_count: ['Candidate binding verification: items', 'n_exact'],
    view_count: ['Candidate binding verification: distinct views', 'n_exact'],
    search_document_count: ['Candidate binding verification: distinct search documents', 'n_exact'],
    unbound_view_count: ['Candidate binding verification: unbound views', 'n_exact'],
    search_binding_error_count: ['Candidate binding verification: incorrect bindings', 'n_exact'],
  };

  const searchFields = {
    seed_count: ['Search diagnosis: seed cases', 'n_exact'],
    unique_query_count: ['Search diagnosis: distinct query texts', 'n_exact'],
    quality_passed: ['Search diagnosis: quality passed (0 = FAIL, 1 = PASS)', 'n_exact'],
    current_http_client_ms_median: ['Current search: paired HTTP client median', 'ms_roh'],
    candidate_http_client_ms_median: ['Candidate search: paired HTTP client median', 'ms_roh'],
    current_server_ms_median: ['Current search: paired server median', 'ms_roh'],
    candidate_server_ms_median: ['Candidate search: paired server median', 'ms_roh'],
    current_unique_path_count_median: ['Current search: distinct top-10 paths median', 'n_exact'],
    candidate_unique_path_count_median: ['Candidate search: distinct top-10 paths median', 'n_exact'],
  };

  function candidateMetricInfo(path) {
    if (path.startsWith('pack_resource.')) {
      const field = path.slice('pack_resource.'.length);
      const packFields = {
        logical_bytes: ['Pack logical bytes', 'b', 'neutral'],
        stored_bytes: ['Replacement file bytes', 'b', 'lower'],
        source_stored_bytes: ['Identity source file bytes', 'b', 'neutral'],
        build_ms: ['Source pack build including generated input', 'ms_roh', 'lower'],
        rewrite_ms: ['Physical pack rewrite', 'ms_roh', 'lower'],
        verify_ms: ['Replacement integrity verification', 'ms_roh', 'lower'],
        rewrite_mib_s: ['Physical rewrite throughput (MiB/s)', 'f', 'higher'],
        process_peak_rss_bytes: ['Fresh process lifetime peak RSS', 'b', 'lower'],
        process_baseline_hwm_bytes: ['Process high-water mark before work', 'b', 'neutral'],
        integrity_passed: ['Pack integrity passed (0 = FAIL)', 'n_exact', 'higher'],
        entry_count: ['Verified pack entries', 'n_exact', 'neutral'],
      };
      if (!Object.hasOwn(packFields, field)) return null;
      const [l, u, preference] = packFields[field];
      return {l, u, preference, k:'other', showZero:true, pinned:true,
        d:'Physical pack diagnostic only. Compare only equal size/pattern cohorts and build profiles, with repetition ranges. VmHWM covers the whole fresh process, not stage-specific RSS or just the writer buffer. No SQL, ingestion or device I/O attribution. A debug/CI result does not select production defaults; integrity 0 is FAIL.'};
    }
    if (path.startsWith('search_quality.')) {
      const field = path.slice('search_quality.'.length);
      if (!Object.hasOwn(searchFields, field)) return null;
      const preference = field === 'quality_passed' ? 'higher'
        : searchFields[field][1] === 'ms_roh' ? 'lower' : 'neutral';
      return {l:searchFields[field][0], u:searchFields[field][1], k:'other', showZero:true, pinned:true, preference,
        d:'Diagnostic only: quality_passed = 0 is FAIL and remains visible. Seed cases are not independent queries. Medians do not replace maximum-latency or quality gates. HTTP time includes transfer and JSON decode; server time is reported separately. More distinct paths do not prove better ranking. This does not qualify or activate a candidate.'};
    }
    if (!path.startsWith('candidate_state.')) return null;
    const field = path.slice(16);
    if (!Object.hasOwn(fields, field)) return null;
    return {l:fields[field][0], u:fields[field][1], k:'other', showZero:true, pinned:true,
      d:'Pinned integrity measurement: zero is an observed result, not missing data. Distinct view/document counts need not match. Client time includes connection startup and the complete source-state SQL query, not search API latency or qualification. Shared host resources are not isolated attribution.'};
  }

  function hideEmptyMetric(groups, showZero = false) {
    return !groups.some(group => Number.isFinite(group.median) && (showZero || group.median !== 0));
  }

  function formatExactCount(value) {
    return [Number.isFinite(value)
      ? value.toLocaleString('de-DE', {maximumFractionDigits:20}) : '—', ''];
  }

  const api = {candidateMetricInfo, hideEmptyMetric, formatExactCount};
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.MAINRAG_TELEMETRY_INTEGRITY = api;
})(globalThis);
