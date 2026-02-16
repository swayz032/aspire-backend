FROM python:3.11-slim

WORKDIR /app

# Install system deps for Presidio NER models
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency spec first for layer caching
COPY pyproject.toml ./

# Install Python deps
RUN pip install --no-cache-dir .

# Download spaCy model for Presidio DLP (Law #9)
RUN python -m spacy download en_core_web_lg || true

# Copy source code (includes config/, schemas/, skillpacks/, etc.)
COPY src/ ./src/

# Install the package in editable mode so config files are found
RUN pip install --no-cache-dir -e .

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')" || exit 1

# Run with uvicorn
CMD ["uvicorn", "aspire_orchestrator.server:app", "--host", "0.0.0.0", "--port", "8000"]
