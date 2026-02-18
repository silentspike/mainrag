const state = {
  runId: null,
  eventSource: null,
  latency: { labels: [], health: [], metrics: [], search: [] },
  system: { labels: [], cpu: [], mem: [], iowait: [] },
  disk: { labels: [], read: [], write: [], iops: [], queue: [] },
  gpu: { labels: [], util: [], mem: [] },
  compareMode: false,
  tokenAvailable: false,
};

const maxPoints = 120;

const apiStatus = document.getElementById("api-status");
const runStatus = document.getElementById("run-status");
const preflightList = document.getElementById("preflight-list");
const preflightStatus = document.getElementById("preflight-status");
const logBox = document.getElementById("log-box");
const runsTable = document.getElementById("runs-table");
const runDetails = document.getElementById("run-details");
const benchmarkSelect = document.getElementById("benchmark-select");
const benchmarkTitle = document.getElementById("benchmark-title");
const benchmarkSummary = document.getElementById("benchmark-summary");
const benchmarkDetails = document.getElementById("benchmark-details");
const connectionBanner = document.getElementById("connection-banner");
const tokenHint = document.getElementById("token-hint");

const startBtn = document.getElementById("start-btn");
const stopBtn = document.getElementById("stop-btn");
const compareBtn = document.getElementById("compare-btn");
const refreshRunsBtn = document.getElementById("refresh-runs");

const durationInput = document.getElementById("duration");
const intervalInput = document.getElementById("interval");
const apiUrlInput = document.getElementById("api-url");
const tokenInput = document.getElementById("token");
const concurrencyInput = document.getElementById("concurrency");
const qualityInput = document.getElementById("quality");
const limitInput = document.getElementById("limit");
const stagesInput = document.getElementById("stages");
const stageDurationInput = document.getElementById("stage-duration");

let benchmarksCatalog = [];

const latencyChart = echarts.init(document.getElementById("chart-latency"));
const systemChart = echarts.init(document.getElementById("chart-system"));
const diskChart = echarts.init(document.getElementById("chart-disk"));
const gpuChart = echarts.init(document.getElementById("chart-gpu"));

function initCharts() {
  latencyChart.setOption({
    backgroundColor: "transparent",
    tooltip: { trigger: "axis" },
    legend: { textStyle: { color: "#CBD5F5" } },
    grid: { left: 40, right: 20, top: 40, bottom: 30 },
    xAxis: { type: "category", data: [], axisLabel: { color: "#94A3B8" } },
    yAxis: { type: "value", axisLabel: { color: "#94A3B8" } },
    series: [
      { name: "Health", type: "line", smooth: true, data: [], color: "#38BDF8", areaStyle: { opacity: 0.15 } },
      { name: "Metrics", type: "line", smooth: true, data: [], color: "#F59E0B", areaStyle: { opacity: 0.12 } },
      { name: "Search", type: "line", smooth: true, data: [], color: "#22C55E", areaStyle: { opacity: 0.1 } },
    ],
  });

  systemChart.setOption({
    backgroundColor: "transparent",
    tooltip: { trigger: "axis" },
    legend: { textStyle: { color: "#CBD5F5" } },
    grid: { left: 40, right: 20, top: 40, bottom: 30 },
    xAxis: { type: "category", data: [], axisLabel: { color: "#94A3B8" } },
    yAxis: { type: "value", axisLabel: { color: "#94A3B8" }, max: 100 },
    series: [
      { name: "CPU", type: "line", smooth: true, data: [], color: "#38BDF8" },
      { name: "RAM", type: "line", smooth: true, data: [], color: "#F97316" },
      { name: "IO Wait", type: "line", smooth: true, data: [], color: "#F43F5E" },
    ],
  });

  diskChart.setOption({
    backgroundColor: "transparent",
    tooltip: { trigger: "axis" },
    legend: { textStyle: { color: "#CBD5F5" } },
    grid: { left: 40, right: 20, top: 40, bottom: 30 },
    xAxis: { type: "category", data: [], axisLabel: { color: "#94A3B8" } },
    yAxis: { type: "value", axisLabel: { color: "#94A3B8" } },
    series: [
      { name: "Read MB/s", type: "line", smooth: true, data: [], color: "#22C55E" },
      { name: "Write MB/s", type: "line", smooth: true, data: [], color: "#F59E0B" },
      { name: "IOPS", type: "bar", data: [], color: "#38BDF8", opacity: 0.3 },
      { name: "Queue", type: "line", smooth: true, data: [], color: "#F43F5E" },
    ],
  });

  gpuChart.setOption({
    backgroundColor: "transparent",
    tooltip: { trigger: "axis" },
    legend: { textStyle: { color: "#CBD5F5" } },
    grid: { left: 40, right: 20, top: 40, bottom: 30 },
    xAxis: { type: "category", data: [], axisLabel: { color: "#94A3B8" } },
    yAxis: { type: "value", axisLabel: { color: "#94A3B8" }, max: 100 },
    series: [
      { name: "GPU Util", type: "line", smooth: true, data: [], color: "#38BDF8" },
      { name: "GPU Mem", type: "line", smooth: true, data: [], color: "#F59E0B" },
    ],
  });
}

function pushSeries(arr, value) {
  arr.push(value ?? null);
  if (arr.length > maxPoints) arr.shift();
}

function pushLabel(label) {
  state.latency.labels.push(label);
  state.system.labels.push(label);
  state.disk.labels.push(label);
  state.gpu.labels.push(label);
  if (state.latency.labels.length > maxPoints) state.latency.labels.shift();
  if (state.system.labels.length > maxPoints) state.system.labels.shift();
  if (state.disk.labels.length > maxPoints) state.disk.labels.shift();
  if (state.gpu.labels.length > maxPoints) state.gpu.labels.shift();
}

function updateCharts() {
  latencyChart.setOption({
    xAxis: { data: state.latency.labels },
    series: [
      { data: state.latency.health },
      { data: state.latency.metrics },
      { data: state.latency.search },
    ],
  });
  systemChart.setOption({
    xAxis: { data: state.system.labels },
    series: [
      { data: state.system.cpu },
      { data: state.system.mem },
      { data: state.system.iowait },
    ],
  });
  diskChart.setOption({
    xAxis: { data: state.disk.labels },
    series: [
      { data: state.disk.read },
      { data: state.disk.write },
      { data: state.disk.iops },
      { data: state.disk.queue },
    ],
  });
  gpuChart.setOption({
    xAxis: { data: state.gpu.labels },
    series: [
      { data: state.gpu.util },
      { data: state.gpu.mem },
    ],
  });
}

function appendLog(entry) {
  const line = document.createElement("div");
  const level = entry.level || "info";
  const color = level === "error" ? "text-rose-300" : level === "warning" ? "text-amber-300" : "text-slate-300";
  line.className = color;
  line.textContent = `[${entry.ts}] ${entry.message}`;
  logBox.appendChild(line);
  logBox.scrollTop = logBox.scrollHeight;
}

function renderPreflight(checks) {
  preflightList.innerHTML = "";
  let allOk = true;
  checks.forEach((check) => {
    const row = document.createElement("div");
    row.className = "flex items-center justify-between bg-slate-900/60 rounded-lg px-3 py-2";
    const ok = check.ok;
    if (!ok && check.required) allOk = false;
    row.innerHTML = `
      <div>
        <div class="text-sm text-white">${check.name}</div>
        <div class="text-xs text-slate-400">${check.error ? check.error : (check.ms ? check.ms.toFixed(1) + " ms" : "ok")}</div>
      </div>
      <div class="px-2 py-1 rounded-full text-xs ${ok ? "bg-emerald-500/20 text-emerald-200" : "bg-rose-500/20 text-rose-200"}">
        ${ok ? "OK" : "Fail"}
      </div>
    `;
    preflightList.appendChild(row);
  });
  preflightStatus.textContent = allOk ? "Ready" : "Blocked";
  preflightStatus.className = allOk ? "text-xs uppercase tracking-wide text-emerald-300" : "text-xs uppercase tracking-wide text-rose-300";
}

function setRunStatus(text, variant = "idle") {
  runStatus.textContent = text;
  const styles = {
    idle: "bg-slate-800 text-slate-300",
    running: "bg-emerald-500/20 text-emerald-200",
    failed: "bg-rose-500/20 text-rose-200",
    completed: "bg-aurora/20 text-aurora",
    cancelled: "bg-amber-500/20 text-amber-200",
  };
  runStatus.className = `px-3 py-1 rounded-full text-xs uppercase tracking-wide ${styles[variant] || styles.idle}`;
}

async function fetchConfig() {
  const res = await fetch("/api/config");
  if (!res.ok) throw new Error("config unavailable");
  const cfg = await res.json();
  apiUrlInput.value = cfg.api_url;
  state.tokenAvailable = cfg.token_available === "true";
  if (cfg.token_available === "true") {
    tokenHint.textContent = "Token auto-detected (search benchmark ready)";
  } else {
    tokenHint.textContent = "Config auto-detected from mainrag.env";
  }
}

async function loadBenchmarks() {
  const res = await fetch("/api/benchmarks");
  if (!res.ok) throw new Error("benchmarks unavailable");
  const data = await res.json();
  benchmarksCatalog = data;
  benchmarkSelect.innerHTML = "";
  data.forEach((bench) => {
    const opt = document.createElement("option");
    opt.value = bench.id;
    opt.textContent = bench.name;
    benchmarkSelect.appendChild(opt);
  });
  if (data.length > 0) {
    setBenchmark(data[0].id);
  }
}

function setBenchmark(benchmarkId) {
  const bench = benchmarksCatalog.find((b) => b.id === benchmarkId);
  if (!bench) return;
  benchmarkTitle.textContent = bench.name;
  benchmarkSummary.textContent = bench.summary;
  benchmarkDetails.innerHTML = "";
  bench.details.forEach((line) => {
    const li = document.createElement("li");
    li.textContent = line;
    benchmarkDetails.appendChild(li);
  });
  const defaults = bench.defaults || {};
  durationInput.value = defaults.duration_sec ?? durationInput.value;
  intervalInput.value = defaults.interval_ms ?? intervalInput.value;
  concurrencyInput.value = defaults.concurrency ?? concurrencyInput.value;
  limitInput.value = defaults.limit ?? limitInput.value;
  stagesInput.value = defaults.stages ?? stagesInput.value;
  stageDurationInput.value = defaults.stage_duration_sec ?? stageDurationInput.value;
  if (defaults.quality) qualityInput.value = defaults.quality;
}

async function checkApiStatus() {
  try {
    const res = await fetch("/health");
    if (res.ok) {
      apiStatus.textContent = "Dashboard online";
      apiStatus.className = "px-3 py-1 rounded-full text-xs uppercase tracking-wide bg-emerald-500/20 text-emerald-200";
      connectionBanner.classList.add("hidden");
    }
  } catch (err) {
    apiStatus.textContent = "Dashboard offline";
    apiStatus.className = "px-3 py-1 rounded-full text-xs uppercase tracking-wide bg-rose-500/20 text-rose-200";
    connectionBanner.classList.remove("hidden");
  }
}

function showToast(message, level = "info") {
  const toast = document.createElement("div");
  const color =
    level === "error"
      ? "bg-rose-500/20 text-rose-100 border-rose-500/40"
      : "bg-emerald-500/20 text-emerald-100 border-emerald-500/40";
  toast.className = `fixed bottom-6 right-6 z-50 border px-4 py-3 rounded-xl shadow-lg text-sm ${color}`;
  toast.textContent = message;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 4000);
}

function clearSeries() {
  state.latency = { labels: [], health: [], metrics: [], search: [] };
  state.system = { labels: [], cpu: [], mem: [], iowait: [] };
  state.disk = { labels: [], read: [], write: [], iops: [], queue: [] };
  state.gpu = { labels: [], util: [], mem: [] };
  updateCharts();
  logBox.innerHTML = "";
}

async function startRun() {
  if (state.runId) return;
  clearSeries();
  setRunStatus("Starting", "running");
  if (!benchmarkSelect.value) {
    showToast("No benchmark selected. Check connection to dashboard API.", "error");
    setRunStatus("Idle", "idle");
    return;
  }
  if (benchmarkSelect.value === "search_latency") {
    const hasToken = Boolean(tokenInput.value && tokenInput.value.trim()) || state.tokenAvailable;
    if (!hasToken) {
      showToast("Search Latency benchmark requires a valid token.", "error");
      setRunStatus("Idle", "idle");
      return;
    }
  }
  const apiOverride = apiUrlInput.value ? apiUrlInput.value.trim() : "";
  const payload = {
    benchmark_id: benchmarkSelect.value,
    duration_sec: parseInt(durationInput.value || "60", 10),
    interval_ms: parseInt(intervalInput.value || "1000", 10),
    api_url: apiOverride || null,
    token: tokenInput.value || null,
    concurrency: parseInt(concurrencyInput.value || "4", 10),
    quality: qualityInput.value || "fast",
    limit: parseInt(limitInput.value || "10", 10),
    stages: parseInt(stagesInput.value || "4", 10),
    stage_duration_sec: parseInt(stageDurationInput.value || "20", 10),
  };

  const res = await fetch("/api/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    showToast(err.detail || "Benchmark start failed", "error");
    setRunStatus("Failed", "failed");
    return;
  }
  const data = await res.json();
  state.runId = data.run_id;
  startBtn.disabled = true;
  stopBtn.disabled = false;
  runDetails.textContent = "Live run in progress.";

  state.eventSource = new EventSource(`/api/runs/${state.runId}/events`);
  state.eventSource.onmessage = (event) => {
    if (!event.data || event.data === "{}") return;
    const msg = JSON.parse(event.data);
    if (msg.type === "status") {
      if (msg.status === "running") setRunStatus("Running", "running");
      if (msg.status === "completed") {
        setRunStatus("Completed", "completed");
        stopStreaming();
      }
      if (msg.status === "failed") {
        setRunStatus("Failed", "failed");
        stopStreaming();
      }
      if (msg.status === "cancelled") {
        setRunStatus("Cancelled", "cancelled");
        stopStreaming();
      }
    }
    if (msg.type === "preflight") {
      renderPreflight(msg.checks);
    }
    if (msg.type === "metric") {
      const point = msg.data;
      const label = point.ts.split("T")[1]?.split(".")[0] || point.tick;
      pushLabel(label);
      pushSeries(state.latency.health, point.health_ms);
      pushSeries(state.latency.metrics, point.metrics_ms);
      pushSeries(state.latency.search, point.search_ms);

      pushSeries(state.system.cpu, point.cpu_pct);
      pushSeries(state.system.mem, point.mem_used_pct);
      pushSeries(state.system.iowait, point.iowait_pct);

      pushSeries(state.disk.read, point.read_mb_s);
      pushSeries(state.disk.write, point.write_mb_s);
      pushSeries(state.disk.iops, point.iops);
      pushSeries(state.disk.queue, point.queue_depth);

      pushSeries(state.gpu.util, point.gpu_util);
      pushSeries(state.gpu.mem, point.gpu_mem_pct);

      updateCharts();
    }
    if (msg.type === "log") {
      appendLog(msg);
    }
  };
}

function stopStreaming() {
  if (state.eventSource) {
    state.eventSource.close();
  }
  state.eventSource = null;
  state.runId = null;
  startBtn.disabled = false;
  stopBtn.disabled = true;
  fetchRuns();
}

async function stopRun() {
  if (!state.runId) return;
  await fetch(`/api/runs/${state.runId}/stop`, { method: "POST" });
}

async function fetchRuns() {
  const res = await fetch("/api/runs");
  const runs = await res.json();
  runsTable.innerHTML = "";
  runs.forEach((run) => {
    const row = document.createElement("tr");
    row.className = "border-t border-slate-800/60";
    const summary = run.summary || {};
    const p95 = summary.search_p95 ?? summary.health_ms_p95 ?? summary.metrics_ms_p95;
    row.innerHTML = `
      <td class="py-2 font-mono text-xs">${run.run_id}</td>
      <td class="py-2">${run.status}</td>
      <td class="py-2">${run.started_at ? run.started_at.split("T")[0] : "-"}</td>
      <td class="py-2">${summary.duration_sec ? summary.duration_sec + "s" : "-"}</td>
      <td class="py-2">${p95 ? p95.toFixed(1) + " ms" : "-"}</td>
      <td class="py-2">
        <input type="checkbox" class="compare-checkbox" data-run-id="${run.run_id}" />
      </td>
      <td class="py-2">
        <button class="view-btn px-2 py-1 rounded bg-slate-800 text-slate-200" data-run-id="${run.run_id}">View</button>
      </td>
    `;
    runsTable.appendChild(row);
  });

  document.querySelectorAll(".view-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const runId = btn.getAttribute("data-run-id");
      const run = await loadRun(runId);
      renderRunDetails(run);
    });
  });
}

async function loadRun(runId) {
  const res = await fetch(`/api/runs/${runId}`);
  return res.json();
}

function renderRunDetails(run) {
  const summary = run.summary || {};
  const searchP95 = summary.search_p95 ?? summary.search_ms_p95;
  const searchP50 = summary.search_p50 ?? summary.search_ms_p50;
  const searchP99 = summary.search_p99 ?? summary.search_ms_p99;
  let stageHtml = "";
  if (Array.isArray(summary.stage_summary)) {
    stageHtml = summary.stage_summary.map((stage) => {
      const p95 = stage.p95 ? stage.p95.toFixed(1) + " ms" : "-";
      const err = stage.error_rate ? (stage.error_rate * 100).toFixed(2) + "%" : "-";
      return `<div>Stage ${stage.stage} (c=${stage.concurrency}): p95 ${p95}, err ${err}</div>`;
    }).join("");
  }
  runDetails.innerHTML = `
    <div class="space-y-1">
      <div><span class="text-slate-400">Run ID:</span> ${run.run_id}</div>
      <div><span class="text-slate-400">Status:</span> ${run.status}</div>
      <div><span class="text-slate-400">Duration:</span> ${summary.duration_sec || "-"} sec</div>
      <div><span class="text-slate-400">Health p95:</span> ${summary.health_ms_p95 ? summary.health_ms_p95.toFixed(1) + " ms" : "-"}</div>
      <div><span class="text-slate-400">Metrics p95:</span> ${summary.metrics_ms_p95 ? summary.metrics_ms_p95.toFixed(1) + " ms" : "-"}</div>
      <div><span class="text-slate-400">Search p50:</span> ${searchP50 ? searchP50.toFixed(1) + " ms" : "-"}</div>
      <div><span class="text-slate-400">Search p95:</span> ${searchP95 ? searchP95.toFixed(1) + " ms" : "-"}</div>
      <div><span class="text-slate-400">Search p99:</span> ${searchP99 ? searchP99.toFixed(1) + " ms" : "-"}</div>
      <div><span class="text-slate-400">Search RPS:</span> ${summary.search_rps ? summary.search_rps.toFixed(2) : "-"}</div>
      <div><span class="text-slate-400">Error Rate:</span> ${summary.search_error_rate ? (summary.search_error_rate * 100).toFixed(2) + "%" : "-"}</div>
      ${stageHtml}
      <div><span class="text-slate-400">CPU avg:</span> ${summary.cpu_avg ? summary.cpu_avg.toFixed(1) + "%" : "-"}</div>
    </div>
  `;
}

async function compareRuns() {
  const checked = Array.from(document.querySelectorAll(".compare-checkbox:checked")).map(
    (c) => c.getAttribute("data-run-id")
  );
  if (checked.length !== 2) {
    alert("Select exactly two runs to compare.");
    return;
  }
  const [runA, runB] = checked;
  const dataA = await fetch(`/api/runs/${runA}?include_metrics=true`).then((r) => r.json());
  const dataB = await fetch(`/api/runs/${runB}?include_metrics=true`).then((r) => r.json());

  const labels = dataA.metrics.map((m) => m.tick);
  const seriesA = dataA.metrics.map((m) => m.health_ms);
  const seriesB = dataB.metrics.map((m) => m.health_ms);

  latencyChart.setOption({
    legend: { data: [`${runA} health`, `${runB} health`] },
    xAxis: { data: labels },
    series: [
      { name: `${runA} health`, type: "line", smooth: true, data: seriesA, color: "#38BDF8" },
      { name: `${runB} health`, type: "line", smooth: true, data: seriesB, color: "#F59E0B" },
    ],
  });
  state.compareMode = true;
}

window.addEventListener("resize", () => {
  latencyChart.resize();
  systemChart.resize();
  diskChart.resize();
  gpuChart.resize();
});

startBtn.addEventListener("click", startRun);
stopBtn.addEventListener("click", stopRun);
compareBtn.addEventListener("click", compareRuns);
refreshRunsBtn.addEventListener("click", fetchRuns);
benchmarkSelect.addEventListener("change", (e) => setBenchmark(e.target.value));

(async () => {
  initCharts();
  if (window.location.protocol === "file:") {
    connectionBanner.classList.remove("hidden");
    showToast("Open the dashboard via http://localhost:8787 (not file://).", "error");
    startBtn.disabled = true;
  }
  try {
    await fetchConfig();
    await loadBenchmarks();
  } catch (err) {
    connectionBanner.classList.remove("hidden");
    startBtn.disabled = true;
  }
  await checkApiStatus();
  await fetchRuns();
  setRunStatus("Idle", "idle");
})();
