# PATCH NOTES: The following lines must be added to server.py
# After line 128 (contacts_router import):
#   from aspire_orchestrator.routes.materials import router as materials_router
# After line 303 (contacts_router include):
#   app.include_router(materials_router)  # /v1/materials/search  -- Pass C
#
# This file documents the required manual patch. It cannot self-apply because
# server.py is too large to push as a full replacement in a single GitHub API
# call (146 KB). Apply the two-line diff manually or via CI.
