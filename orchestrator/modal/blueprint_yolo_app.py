"""
Aspire Drew SEE — Modal serverless GPU YOLOv11n inference.

Deploys to Modal as a single fastapi endpoint. The orchestrator calls this URL
via httpx with raw PNG bytes; the endpoint returns detection bboxes + classes.

Stateless: this app does NOT apply Aspire's symbol_class_map. The orchestrator
keeps the class map as single source of truth and translates COCO names → Aspire
names on its side (symbol_detector.py).

Deploy:
  modal deploy orchestrator/modal/blueprint_yolo_app.py

After deploy, capture the function URL (printed to stdout) and set on Railway:
  railway variables --service Ava-Brain --set ASPIRE_DREW_MODAL_YOLO_URL=<url>

GPU: T4 (cheap, fast enough for yolo11n). Concurrency=8 per replica.
Idle timeout: 5 min (keeps weights warm between bursts).
"""

import modal
from fastapi import Request

app = modal.App("aspire-drew-yolo")

# Image: Python 3.11 + ultralytics + Pillow. Weights baked in so cold-start
# doesn't pay the 5.4 MB download cost.
image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "ultralytics>=8.3.0,<9.0",
        "Pillow>=10.0.0",
        "fastapi[standard]>=0.115.0",  # for fastapi_endpoint
    )
    .apt_install("libgl1", "libglib2.0-0", "libxcb1")  # cv2 native deps (matches Aspire Dockerfile)
    .run_commands(
        "python -c 'from ultralytics import YOLO; YOLO(\"yolo11n.pt\")'"
    )
)

# Module-scope cache — survives across calls within a warm container, dropped on cold start.
_model_cache: dict = {}


@app.function(
    image=image,
    gpu="T4",
    max_containers=10,        # cap so we don't accidentally fan out infinitely
    min_containers=2,         # Wave 10b: 2 always warm — kills the 6-8s cold-start tax
                              # on the first sheet of every pipeline run.
    scaledown_window=300,     # keep warm 5 min between calls (above min_containers)
    # enable_memory_snapshot=True,  # Wave 10b: TEMPORARILY DISABLED — snapshots were
    # preserving the broken function signature across redeploys (422 errors persisted
    # even after typing fixes). Re-enable once the new signature is verified live.
    timeout=60,               # hard ceiling per call
)
@modal.fastapi_endpoint(method="POST")
async def detect_symbols(request: Request) -> dict:
    """Run YOLOv11n on a single sheet PNG. Returns raw xyxy detections.

    Request body: raw PNG bytes (Content-Type: application/octet-stream).
    Query params:
      confidence (float, default 0.5) — minimum detection confidence.
      imgsz (int, default 1280) — YOLO input resize. Larger = catches small
        symbols (door markers ~10px at 1280); smaller = faster. Default 1280
        is a balance: yolo11n was trained at 640 but accepts arbitrary sizes,
        and construction symbols benefit from extra resolution.

    Response: {
      "detections": [
        {"class_id": int, "class_name": str, "confidence": float,
         "bbox_xyxy": [x1, y1, x2, y2]}, ...
      ],
      "image_size": [width, height],
      "model": "yolo11n.pt",
      "model_version": "ultralytics-yolo11n-v1"
    }

    Errors return {"error": "<reason>"} with 4xx/5xx status.
    """
    from fastapi import HTTPException, Request  # noqa: F401 — Request used in type hint
    from ultralytics import YOLO
    from PIL import Image
    import io

    # Lazy-load model (cached after first call per warm container).
    if "model" not in _model_cache:
        _model_cache["model"] = YOLO("yolo11n.pt")
    model = _model_cache["model"]

    # Read raw PNG bytes from request body
    image_bytes = await request.body()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="empty_body")
    if len(image_bytes) > 25 * 1024 * 1024:  # 25 MB ceiling per sheet PNG
        raise HTTPException(status_code=413, detail="image_too_large")

    # Parse query params
    confidence = float(request.query_params.get("confidence", "0.5"))
    confidence = max(0.0, min(1.0, confidence))
    imgsz = int(request.query_params.get("imgsz", "1280"))
    imgsz = max(320, min(2048, imgsz))

    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.verify()  # sanity check the bytes
        img = Image.open(io.BytesIO(image_bytes))  # re-open after verify
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid_image: {type(exc).__name__}")

    # Run inference (GPU). verbose=False so logs stay clean.
    results = model.predict(img, conf=confidence, imgsz=imgsz, verbose=False)

    detections: list[dict] = []
    for r in results:
        if r.boxes is None:
            continue
        for box in r.boxes:
            class_id = int(box.cls[0].item())
            detections.append({
                "class_id": class_id,
                "class_name": model.names.get(class_id, f"class_{class_id}"),
                "confidence": float(box.conf[0].item()),
                "bbox_xyxy": [float(v) for v in box.xyxy[0].tolist()],
            })

    return {
        "detections": detections,
        "image_size": [img.width, img.height],
        "model": "yolo11n.pt",
        "model_version": "ultralytics-yolo11n-v1",
    }


@app.function(image=image)
@modal.fastapi_endpoint(method="GET")
def healthz() -> dict:
    """Healthcheck — Modal pings this to verify the app deploys cleanly."""
    return {"status": "ok", "service": "aspire-drew-yolo"}
