import os, sys, traceback

os.environ["ASPIRE_ENABLE_LOCAL_DOTENV"] = "1"

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=True)
except Exception:
    pass

os.environ["ASPIRE_LANGGRAPH_CHECKPOINTER"] = "memory"
os.environ["ASPIRE_LANGGRAPH_POSTGRES_DSN"] = ""

try:
    import uvicorn
    print("loading app...", flush=True)
    from aspire_orchestrator.server import app
    print("IMPORT_OK", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
except Exception:
    traceback.print_exc()
    sys.exit(1)
