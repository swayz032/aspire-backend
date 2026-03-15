"""Aspire Orchestrator Middleware — cross-cutting concerns.

Middleware stack (mount order in server.py, last-added = outermost):
1. CORSMiddleware — standard CORS
2. GlobalExceptionMiddleware — catch unhandled exceptions, create incidents + receipts
3. RateLimitMiddleware — per-tenant sliding window (B-H7)
4. CorrelationIdMiddleware — inject/propagate X-Correlation-Id (Wave 2A) — outermost
5. ChaosMiddleware — controlled failure injection (Wave 8.7, CHAOS_ENABLED=true only) — innermost

Non-middleware integrations:
- sentry_middleware.init_sentry() — Sentry error tracking (Wave 8.6, optional via SENTRY_DSN)
"""
