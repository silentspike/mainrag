'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const policyPath = path.join(__dirname, '../../docs/telemetry-integrity.js');
const context = require(policyPath);

test('known integrity metrics are pinned measurements with explicit units', () => {
  for (const field of ['item_count', 'view_count', 'search_document_count',
    'unbound_view_count', 'search_binding_error_count']) {
    const info = context.candidateMetricInfo(`candidate_state.${field}`);
    assert.equal(info.u, 'n_exact');
    assert.equal(info.showZero, true);
    assert.equal(info.pinned, true);
    assert.match(info.d, /not search API latency or qualification/);
  }
  assert.equal(context.candidateMetricInfo('candidate_state.query_client_ms').u, 'ms_roh');
  assert.equal(context.candidateMetricInfo('candidate_state.unknown'), null);
  assert.equal(context.candidateMetricInfo('candidate_state.toString'), null);
  assert.equal(context.candidateMetricInfo('candidate_state.__proto__'), null);
  assert.equal(context.candidateMetricInfo('system.view_count'), null);
});

test('explicit zero integrity observations remain visible', () => {
  assert.equal(context.hideEmptyMetric([{median:0}, {median:0}, {median:0}], true), false);
  assert.equal(context.hideEmptyMetric([{median:null}, {median:0}], true), false);
  assert.equal(context.hideEmptyMetric([{median:0}, {median:1}, {median:0}], true), false);
});

test('missing and invalid observations are not manufactured as passing zeros', () => {
  for (const median of [null, undefined, NaN, Infinity, '0', false]) {
    assert.equal(context.hideEmptyMetric([{median}], true), true);
  }
  assert.equal(context.hideEmptyMetric([], true), true);
  assert.deepEqual(context.formatExactCount(null), ['—', '']);
  assert.deepEqual(context.formatExactCount(NaN), ['—', '']);
});

test('unrelated all-zero metrics retain the existing suppression behavior', () => {
  assert.equal(context.hideEmptyMetric([{median:0}, {median:null}]), true);
  assert.equal(context.hideEmptyMetric([{median:1}, {median:0}]), false);
  assert.equal(context.hideEmptyMetric([{median:-1}]), false);
});

test('neighboring large counts remain distinguishable without abbreviation', () => {
  assert.deepEqual(context.formatExactCount(19002), ['19.002', '']);
  assert.deepEqual(context.formatExactCount(19003), ['19.003', '']);
  assert.deepEqual(context.formatExactCount(0), ['0', '']);
  assert.deepEqual(context.formatExactCount(3.5), ['3,5', '']);
  assert.deepEqual(context.formatExactCount(1000000000), ['1.000.000.000', '']);
});

test('browser export matches the CommonJS module without a private viewer', () => {
  const browser = vm.createContext({});
  vm.runInContext(fs.readFileSync(policyPath, 'utf8'), browser);
  const exported = browser.MAINRAG_TELEMETRY_INTEGRITY;
  assert.deepEqual(Object.keys(exported), Object.keys(context));
  assert.equal(exported.formatExactCount(19003)[0], '19.003');
  assert.equal(exported.hideEmptyMetric([{median:0}], true), false);
});

test('metadata callers cannot mutate the shared presentation policy', () => {
  const info = context.candidateMetricInfo('candidate_state.view_count');
  info.u = 'ms_roh';
  assert.equal(context.candidateMetricInfo('candidate_state.view_count').u, 'n_exact');
});
