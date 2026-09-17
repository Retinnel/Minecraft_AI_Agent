from fastapi import APIRouter, HTTPException, Form

from backend.core.container import container
from backend.core.vision import preprocess_frame

router = APIRouter()


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/latest_response")
def get_latest_response():
    return {"answer": container.current_overlay_text}


@router.get("/status")
def get_status():
    """Возвращает текущий статус системы для оверлея."""
    return {
        "status": container.current_overlay_status,
        "visible": container.overlay_visible
    }


@router.post("/start")
async def start_all():
    container.memory_service.clear_session()
    container.start_all()
    container.is_active = True
    return {"status": "all core services started and memory cleared"}


@router.post("/stop")
def stop_all():
    container.stop_all()
    return {"status": "all core services stopped"}


@router.post("/ask")
async def ask(question: str = Form(...), include_frame: bool = Form(False)):
    image_bytes = None
    if include_frame:
        frame = container.capture.get_frame()
        if frame is not None:
            image_bytes = preprocess_frame(frame)

    await container.pipeline.add_request(question, image_bytes)
    return {"status": "request added to pipeline"}

