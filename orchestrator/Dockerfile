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
