from __future__ import annotations

from fastapi import Depends, FastAPI, Header, HTTPException, status

from aspire_safety_gateway.config import settings
from aspire_safety_gateway.models import SafetyCheckRequest, SafetyCheckResponse
from aspire_safety_gateway.service import screen_payload

app = FastAPI(
    title="Aspire Safety Gateway",
    version="0.1.0",
    description="External safety gateway for NeMo Guardrails-backed request screening",
)


def _require_api_key(x_safety_gateway_key: str | None = Header(default=None)) -> None:
    if not settings.api_key:
        return
    if x_safety_gateway_key != settings.api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid safety gateway key")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "mode": settings.mode, "config_path": settings.nemo_config_path}


@app.post("/v1/safety/check", response_model=SafetyCheckResponse, dependencies=[Depends(_require_api_key)])
def check_safety(request: SafetyCheckRequest) -> SafetyCheckResponse:
    return screen_payload(request.payload)
