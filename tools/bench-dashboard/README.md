# MainRAG Benchmark Dashboard

A standalone benchmark dashboard with live telemetry, persistent runs, and safe restart behavior.

## What It Does

- Starts read-only benchmarks that validate dashboard functionality and search latency.
- Streams live telemetry: CPU, RAM, disk IO, IO wait, queue depth, GPU (if available).
- Measures API `/health` and `/metrics` latency, plus optional search smoke test.
- Saves every run to `/work/mainrag/data/benchmarks` for comparison.

## Run It

```bash
cd /work/mainrag/tools/bench-dashboard
python app.py
```

Open:

```
http://localhost:8787
```

## Notes

- If you provide a Bearer token, the System Pulse benchmark runs a small `/api/v1/search` smoke test.
- The Search Latency benchmark requires a Bearer token.
- The benchmark is read-only and safe to restart.
- Runs are stored under `data/benchmarks` with metrics and logs.

## Dependencies

- Python 3.10+
- fastapi
- uvicorn

Install if needed:

```bash
pip install fastapi uvicorn
```
