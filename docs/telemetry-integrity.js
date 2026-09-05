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

  function candidateMetricInfo(path) {
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
