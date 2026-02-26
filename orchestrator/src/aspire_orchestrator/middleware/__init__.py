"""Aspire Orchestrator Middleware — cross-cutting concerns.

Middleware stack (mount order in server.py):
1. CorrelationIdMiddleware — inject/propagate X-Correlation-Id (Wave 2A)
2. GlobalExceptionMiddleware — catch unhandled exceptions, create incidents + receipts
3. CORSMiddleware — standard CORS (already exists)
"""
