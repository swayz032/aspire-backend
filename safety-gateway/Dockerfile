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

RUN useradd --system --create-home --uid 10001 aspire && \
    chown -R aspire:aspire /app

EXPOSE 8787

USER aspire

CMD ["python", "-m", "aspire_safety_gateway.launch"]
