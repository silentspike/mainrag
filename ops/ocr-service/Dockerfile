# MAINRAG OCR Service - GPU-Only (PaddleOCR + CUDA)
#
# CRITICAL: Requires NVIDIA GPU with CUDA. No CPU fallback!
# See ops/OCR_RUNBOOK.md for operations guide.

# Use CUDA 12.6 + cuDNN 9 (for paddlepaddle-gpu 3.2.0)
FROM nvidia/cuda:12.6.3-cudnn-runtime-ubuntu22.04

# System dependencies for PaddleOCR and PyMuPDF
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-dev \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libgomp1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Install PaddlePaddle GPU 3.2.0 from official index (CUDA 12.6)
RUN pip3 install --no-cache-dir paddlepaddle-gpu==3.2.0 -i https://www.paddlepaddle.org.cn/packages/stable/cu126/

# Install PaddleOCR
RUN pip3 install --no-cache-dir paddleocr>=2.8.0

# Copy application
COPY app.py .

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8090/health || exit 1

EXPOSE 8090

# Run with uvicorn
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8090"]
