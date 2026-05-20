FROM python:3.11-slim

WORKDIR /app

# Install system deps. Wave 9g: libgl1 + libglib2.0-0 + libxcb1 are required by
# opencv (cv2 transitive dep from ultralytics/YOLO) on python:3.11-slim. Without
# them: `ImportError: libxcb.so.1: cannot open shared object file` at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgl1 \
    libglib2.0-0 \
    libxcb1 \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency spec and source code
COPY pyproject.toml ./
COPY src/ ./src/
COPY scripts/schemas/ ./scripts/schemas/

# Install Python deps + package
RUN pip install --no-cache-dir .

# Wave 9g/9k: verify ultralytics imports and pre-fetch YOLOv11 weights so SEE
# doesn't cold-fetch on first request. ultralytics is already declared in
# pyproject.toml so `pip install .` above pulled it. We do NOT force CPU torch
# here because ultralytics pins torchvision which re-resolves to a full torch.
# Railway's runtime has enough memory for the full wheel; the build penalty is
# accepted in exchange for not fighting the dep solver.
#
# Wave 9k: pre-fetch BOTH yolo11n.pt (~2.6 MB, primary default — ~3x faster
# CPU inference) AND yolo11m.pt (~50 MB, A/B fallback via ASPIRE_DREW_YOLO_MODEL).
RUN python -c "from ultralytics import YOLO; print('ultralytics OK')" \
    && python -c "from ultralytics import YOLO; YOLO('yolo11n.pt')" \
    && python -c "from ultralytics import YOLO; YOLO('yolo11m.pt')" \
    && mkdir -p /home/aspire/.config/Ultralytics \
    && mv yolo11n.pt /home/aspire/.config/Ultralytics/ 2>/dev/null || true \
    && mv yolo11m.pt /home/aspire/.config/Ultralytics/ 2>/dev/null || true

# Download spaCy model for Presidio DLP (Law #9)
RUN python -m spacy download en_core_web_lg || true

RUN useradd --system --create-home --uid 10001 aspire && \
    chown -R aspire:aspire /app

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; port = os.getenv('PORT', '8000'); urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz')" || exit 1

# Wave 10b: route YOLO + matplotlib config writes to /tmp so the non-root
# "aspire" user doesn't hit "Permission denied" trying to write
# /home/aspire/.config/{Ultralytics,matplotlib}/. Those errors didn't crash
# the worker but slowed CPU-fallback YOLO inference by retrying writes on
# every model load.
ENV YOLO_CONFIG_DIR=/tmp/ultralytics
ENV MPLCONFIGDIR=/tmp/matplotlib
ENV HOME=/tmp

USER aspire

# Run with uvicorn
CMD ["python", "-m", "aspire_orchestrator.launch"]
