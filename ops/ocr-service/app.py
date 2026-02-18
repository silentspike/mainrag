"""
GPU-Only OCR Service using PaddleOCR + CUDA

This service provides OCR for scanned PDFs with:
- Hybrid sync/async processing
- Redis job persistence
- Per-page timeout (5s * page_count)
- Hard process kill on timeout
- CUDA-safe multiprocessing (spawn, not fork)

CRITICAL: Requires NVIDIA GPU with CUDA. No CPU fallback!
"""

from fastapi import FastAPI, UploadFile, HTTPException, BackgroundTasks
from paddleocr import PaddleOCR
import fitz  # PyMuPDF for PDF rendering
import redis
import tempfile
import time
import uuid
import asyncio
import json
import os
from typing import Optional
from pydantic import BaseModel
from multiprocessing import Process, Queue, set_start_method
import paddle
import numpy as np
import threading

# CRITICAL: Use spawn instead of fork to avoid CUDA context issues
# Fork copies CUDA state which causes "CUDA error: invalid device context"
try:
    set_start_method("spawn", force=True)
except RuntimeError:
    pass  # Already set

# GPU MEMORY LIMIT: Set BEFORE importing paddle models
# This allows OCR to coexist with TEI on 4GB GPU (RTX 3050 Ti)
# OCR gets ~1.5GB, TEI gets ~1GB, leaves buffer for system
os.environ["FLAGS_fraction_of_gpu_memory_to_use"] = "0.35"
os.environ["FLAGS_gpu_memory_limit_mb"] = "1400"  # Hard limit 1.4GB

app = FastAPI(title="MAINRAG OCR Service", version="1.0.0")

# Redis connection
redis_client = redis.Redis(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=int(os.getenv("REDIS_PORT", 6379)),
    db=int(os.getenv("REDIS_DB", 1)),
    decode_responses=True
)

# Limits
MAX_PAGES = 20
MAX_PAGES_SYNC = 10
MAX_SIZE_SYNC = 5 * 1024 * 1024  # 5MB
TIMEOUT_PER_PAGE = 5  # 5 seconds per page
MAX_TIMEOUT = 120  # Upper bound
OCR_CONCURRENCY = 2
JOB_TTL_PENDING = 7200  # 2h for pending jobs (in case of backlog)
JOB_TTL_COMPLETED = 3600  # 1h for completed results


def calc_timeout(page_count: int) -> int:
    """Calculate timeout based on page count: 5s per page, max 120s"""
    return min(TIMEOUT_PER_PAGE * page_count, MAX_TIMEOUT)


semaphore = asyncio.Semaphore(OCR_CONCURRENCY)


def check_gpu() -> str:
    """Check GPU availability at startup. HARD FAIL without GPU!"""
    if not paddle.device.is_compiled_with_cuda():
        raise RuntimeError(
            "PaddlePaddle not compiled with CUDA! OCR service requires GPU. "
            "See ops/OCR_RUNBOOK.md for troubleshooting."
        )
    if paddle.device.cuda.device_count() == 0:
        raise RuntimeError(
            "No CUDA GPU detected! OCR service requires NVIDIA GPU. "
            "See ops/OCR_RUNBOOK.md for troubleshooting."
        )
    # Get GPU name from paddle
    gpu_name = paddle.device.cuda.get_device_name(0)
    return gpu_name


# GPU Check at startup - fail fast!
GPU_NAME = check_gpu()

# LAZY LOADING: OCR model loaded on first use to avoid upfront GPU allocation
# This allows the service to start without using GPU memory until needed
_ocr_instance = None
_ocr_lock = threading.Lock()


def get_ocr():
    """Lazy-load PaddleOCR model on first use (thread-safe)"""
    global _ocr_instance
    if _ocr_instance is None:
        with _ocr_lock:
            if _ocr_instance is None:
                # use_angle_cls=True enables text angle classification for rotated text
                # det_limit_side_len=960 reduces detection resolution (default 960, can go lower)
                # rec_batch_num=1 reduces batch size to save GPU memory
                # PaddleOCR 3.x parameters (simpler API than 2.x)
                _ocr_instance = PaddleOCR(
                    lang='en',
                    use_angle_cls=True
                )
    return _ocr_instance


class OcrResult(BaseModel):
    text: str
    pages_processed: int
    duration_seconds: float
    warnings: list[str] = []


class JobStatus(BaseModel):
    job_id: str
    status: str  # pending, processing, completed, failed
    result: Optional[OcrResult] = None
    error: Optional[str] = None


@app.get("/health")
def health():
    """Health check with GPU and Redis verification"""
    gpu_ok = paddle.device.is_compiled_with_cuda() and paddle.device.cuda.device_count() > 0
    try:
        redis_ok = redis_client.ping()
    except redis.ConnectionError:
        redis_ok = False

    if not gpu_ok:
        raise HTTPException(503, "GPU not available")
    if not redis_ok:
        raise HTTPException(503, "Redis not available")

    return {
        "status": "healthy",
        "gpu": True,
        "gpu_name": GPU_NAME,
        "redis": True,
        "concurrency": OCR_CONCURRENCY,
        "limits": {
            "max_pages": MAX_PAGES,
            "max_pages_sync": MAX_PAGES_SYNC,
            "max_size_sync_mb": MAX_SIZE_SYNC // 1024 // 1024,
            "timeout_per_page_s": TIMEOUT_PER_PAGE,
            "max_timeout_s": MAX_TIMEOUT,
        }
    }


def do_ocr(pdf_bytes: bytes, max_pages: int, timeout: int) -> OcrResult:
    """Core OCR logic - runs in worker process"""
    start = time.time()
    warnings = []

    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=True) as tmp:
        tmp.write(pdf_bytes)
        tmp.flush()

        doc = fitz.open(tmp.name)
        total_pages = len(doc)
        page_count = min(total_pages, max_pages)

        if total_pages > max_pages:
            warnings.append(f"PDF has {total_pages} pages, limited to {max_pages}")

        all_text = []
        for page_num in range(page_count):
            # Check timeout per page
            elapsed = time.time() - start
            if elapsed > timeout:
                warnings.append(f"Timeout after {page_num} pages ({elapsed:.1f}s)")
                break

            page = doc[page_num]
            # Render at 150 DPI (reduced from 300 to fit in 4GB GPU with TEI)
            pix = page.get_pixmap(dpi=150)
            # Convert pixmap to numpy array (PaddleOCR 3.x requires numpy, not bytes)
            img_array = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
            # Convert RGBA to RGB if needed (PaddleOCR expects RGB)
            if pix.n == 4:
                img_array = img_array[:, :, :3]

            result = get_ocr().ocr(img_array)
            if result and result[0]:
                # Extract text from OCR result
                page_text = '\n'.join([line[1][0] for line in result[0]])
                all_text.append(page_text)

        doc.close()

    return OcrResult(
        text='\n\n'.join(all_text),
        pages_processed=len(all_text),
        duration_seconds=time.time() - start,
        warnings=warnings
    )


@app.post("/ocr/sync", response_model=OcrResult)
async def extract_sync(file: UploadFile):
    """
    Synchronous OCR for small PDFs.

    Limits:
    - Max 10 pages
    - Max 5MB file size

    For larger PDFs, use /ocr/async endpoint.
    """
    content = await file.read()

    if len(content) > MAX_SIZE_SYNC:
        raise HTTPException(
            413,
            f"File too large for sync OCR. Max: {MAX_SIZE_SYNC // 1024 // 1024}MB. Use /ocr/async"
        )

    # Check page count
    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=True) as tmp:
        tmp.write(content)
        tmp.flush()
        doc = fitz.open(tmp.name)
        page_count = len(doc)
        doc.close()

    if page_count > MAX_PAGES_SYNC:
        raise HTTPException(
            413,
            f"Too many pages for sync OCR. Max: {MAX_PAGES_SYNC}. Use /ocr/async"
        )

    timeout = calc_timeout(page_count)

    async with semaphore:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, do_ocr, content, MAX_PAGES_SYNC, timeout
        )

    return result


def ocr_worker_process(pdf_bytes: bytes, max_pages: int, timeout: int, result_queue: Queue):
    """Worker process for OCR - can be killed on timeout"""
    try:
        result = do_ocr(pdf_bytes, max_pages, timeout)
        result_queue.put({"status": "completed", "result": result.model_dump()})
    except Exception as e:
        result_queue.put({"status": "failed", "error": str(e)})


def run_ocr_with_hard_timeout(pdf_bytes: bytes, max_pages: int, timeout: int) -> dict:
    """Run OCR in subprocess with hard kill on timeout"""
    result_queue = Queue()
    proc = Process(
        target=ocr_worker_process,
        args=(pdf_bytes, max_pages, timeout, result_queue)
    )
    proc.start()
    proc.join(timeout=timeout + 10)  # Extra 10s grace period

    if proc.is_alive():
        # Hard kill on timeout
        proc.terminate()
        proc.join(timeout=5)
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=2)
        return {"status": "failed", "error": f"OCR timeout after {timeout}s (process killed)"}

    if not result_queue.empty():
        return result_queue.get()
    return {"status": "failed", "error": "OCR process crashed unexpectedly"}


@app.post("/ocr/async")
async def extract_async(file: UploadFile, background_tasks: BackgroundTasks):
    """
    Async OCR for large PDFs.

    Returns job_id immediately. Poll /ocr/status/{job_id} for results.
    Jobs are persisted in Redis and survive service restarts.

    Limits:
    - Max 20 pages total
    - Timeout: 5s per page (max 120s)
    """
    content = await file.read()
    job_id = str(uuid.uuid4())

    # Get page count for timeout calculation
    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=True) as tmp:
        tmp.write(content)
        tmp.flush()
        doc = fitz.open(tmp.name)
        page_count = min(len(doc), MAX_PAGES)
        doc.close()

    timeout = calc_timeout(page_count)

    # Store job in Redis with pending TTL (longer for backlog)
    redis_client.hset(f"ocr:job:{job_id}", mapping={
        "status": "pending",
        "result": "",
        "error": "",
        "page_count": str(page_count),
        "timeout": str(timeout),
        "created_at": str(time.time())
    })
    redis_client.expire(f"ocr:job:{job_id}", JOB_TTL_PENDING)

    async def process_job():
        redis_client.hset(f"ocr:job:{job_id}", "status", "processing")
        try:
            async with semaphore:
                # Run in subprocess with per-page timeout
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None, run_ocr_with_hard_timeout, content, MAX_PAGES, timeout
                )

            if result["status"] == "completed":
                redis_client.hset(f"ocr:job:{job_id}", mapping={
                    "status": "completed",
                    "result": json.dumps(result["result"])
                })
                # Shorter TTL for completed jobs
                redis_client.expire(f"ocr:job:{job_id}", JOB_TTL_COMPLETED)
            else:
                redis_client.hset(f"ocr:job:{job_id}", mapping={
                    "status": "failed",
                    "error": result.get("error", "Unknown error")
                })
                redis_client.expire(f"ocr:job:{job_id}", JOB_TTL_COMPLETED)
        except Exception as e:
            redis_client.hset(f"ocr:job:{job_id}", mapping={
                "status": "failed",
                "error": str(e)
            })
            redis_client.expire(f"ocr:job:{job_id}", JOB_TTL_COMPLETED)

    background_tasks.add_task(process_job)
    return {"job_id": job_id, "status": "pending", "page_count": page_count, "timeout": timeout}


@app.get("/ocr/status/{job_id}", response_model=JobStatus)
async def get_job_status(job_id: str):
    """Check status of async OCR job (persisted in Redis)"""
    job_key = f"ocr:job:{job_id}"

    if not redis_client.exists(job_key):
        raise HTTPException(404, "Job not found or expired")

    job = redis_client.hgetall(job_key)
    result = None
    if job.get("result"):
        result = OcrResult(**json.loads(job["result"]))

    return JobStatus(
        job_id=job_id,
        status=job["status"],
        result=result,
        error=job.get("error") or None
    )


@app.on_event("startup")
async def startup_event():
    """Log startup info"""
    print(f"OCR Service starting with GPU: {GPU_NAME}")
    print(f"Limits: max_pages={MAX_PAGES}, timeout_per_page={TIMEOUT_PER_PAGE}s")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090)
