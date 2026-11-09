"""HTTP API and the minimal HTML page."""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from fastapi import Depends, FastAPI, File, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from app.config import PROJECT_ROOT, Settings, get_settings
from app.describer import Describer
from app.service import FileResult, FileService
from app.storage import B2Storage, ObjectNotFound, StorageError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="B2 File Store + AI Description")
INDEX_HTML = PROJECT_ROOT / "static" / "index.html"


@lru_cache
def get_service() -> FileService:
    settings = get_settings()
    return FileService(
        storage=B2Storage(settings),
        describer=Describer(settings.anthropic_api_key, settings.anthropic_model),
    )


class LocalPathRequest(BaseModel):
    path: str


def error(status_code: int, message: str, file_name: str = "", size: int = 0) -> JSONResponse:
    result = FileResult(
        file_name=file_name, key=None, size_bytes=size, content_type="", kind="", status="failed", error=message
    )
    return JSONResponse(status_code=status_code, content=result.to_dict())


def respond(result: FileResult) -> JSONResponse:
    return JSONResponse(status_code=502 if result.status == "failed" else 200, content=result.to_dict())


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(INDEX_HTML)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.post("/api/files")
def upload_file(
    file: UploadFile = File(...),
    service: FileService = Depends(get_service),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    name = file.filename or "upload"
    data = file.file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        return error(413, f"File is larger than {settings.max_upload_bytes // (1024 * 1024)} MB", name)
    if not data:
        return error(400, "File is empty", name)
    return respond(service.process(name, data, file.content_type))


@app.post("/api/files/local")
def ingest_local_file(
    body: LocalPathRequest,
    service: FileService = Depends(get_service),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    base = settings.local_upload_dir
    raw = Path(body.path).expanduser()
    candidate = raw if raw.is_absolute() else PROJECT_ROOT / raw
    path = candidate.resolve()
    if not path.is_relative_to(base):
        # Also accept paths relative to the upload dir itself, e.g. "notes.txt".
        alt = (base / raw).resolve()
        if raw.is_absolute() or not alt.is_relative_to(base):
            return error(400, f"Path must be inside the allowed directory ({base.name}/)", body.path)
        path = alt
    if not path.is_file():
        return error(404, "File not found", body.path)
    size = path.stat().st_size
    if size > settings.max_upload_bytes:
        return error(413, f"File is larger than {settings.max_upload_bytes // (1024 * 1024)} MB", path.name, size)
    if size == 0:
        return error(400, "File is empty", path.name)
    return respond(service.process(path.name, path.read_bytes(), None))


@app.get("/api/files/{key:path}")
def get_file_metadata(key: str, service: FileService = Depends(get_service)) -> JSONResponse:
    try:
        return JSONResponse(service.storage.head(key))
    except ObjectNotFound:
        return JSONResponse(status_code=404, content={"error": f"No object with key {key!r}"})
    except StorageError as exc:
        return JSONResponse(status_code=502, content={"error": str(exc)})
