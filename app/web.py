from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse


WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(
    title="Carbit Oy — tarjousdemo",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/", response_class=FileResponse)
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "carbit-demo",
    }
