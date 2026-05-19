FROM python:3.11-slim

WORKDIR /app

# Install system deps for Presidio NER models
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency spec and source code
COPY pyproject.toml ./
COPY src/ ./src/
COPY scripts/schemas/ ./scripts/schemas/

# Install Python deps + package
RUN pip install --no-cache-dir .

# Wave 9g: ensure ultralytics+torch CPU are installed for Drew SEE stage.
# pyproject.toml declares ultralytics>=8.3.0 but transitive torch wheels can
# silently fail on memory-constrained builders. Force-install torch CPU
# explicitly (lighter, no CUDA) and verify ultralytics imports.
RUN pip install --no-cache-dir \
      torch==2.5.1+cpu --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir 'ultralytics>=8.3.0,<9.0' \
    && python -c "from ultralytics import YOLO; print('ultralytics OK')"

# Wave 9g: pre-fetch YOLOv11 weights so SEE doesn't cold-fetch on first
# request (50MB download). Bakes weights into the image layer.
RUN python -c "from ultralytics import YOLO; YOLO('yolo11m.pt')" \
    && mkdir -p /home/aspire/.config/Ultralytics \
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

USER aspire

# Run with uvicorn
CMD ["python", "-m", "aspire_orchestrator.launch"]
