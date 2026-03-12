FROM python:3.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /app/pyproject.toml
COPY src /app/src
COPY config /app/config

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

ENV ASPIRE_SAFETY_GATEWAY_MODE=local
ENV ASPIRE_SAFETY_GATEWAY_PORT=8787

EXPOSE 8787

CMD ["uvicorn", "aspire_safety_gateway.app:app", "--host", "0.0.0.0", "--port", "8787"]
