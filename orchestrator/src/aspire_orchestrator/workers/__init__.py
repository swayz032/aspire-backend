"""Aspire orchestrator workers — async background jobs.

Currently houses:
    - trust_onboarding/ : per-tenant Twilio Trust Hub + CNAM state machine
                          (W2-W11 of the per-tenant CNAM build)

Each worker runs as a separate ARQ process pool, listening on a Redis queue
distinct from the FastAPI request loop. Workers use service_role to bypass
RLS while still emitting receipts and respecting capability-token contracts
established by the route layer.
"""
