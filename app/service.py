"""Orchestrates upload -> describe -> attach metadata."""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import asdict, dataclass

from app.describer import Describer, DescribeError, detect_kind
from app.storage import B2Storage, StorageError, fit_encoded

log = logging.getLogger(__name__)


@dataclass
class FileResult:
    file_name: str
    key: str | None
    size_bytes: int
    content_type: str
    kind: str
    status: str  # "described" | "stored" | "failed"
    description: str | None = None
    model: str | None = None
    input_truncated: bool = False
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def safe_name(name: str) -> str:
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", base).strip("._")[:100]
    return cleaned or "file"


def make_key(name: str) -> str:
    return f"uploads/{uuid.uuid4().hex[:12]}-{safe_name(name)}"


class FileService:
    def __init__(self, storage: B2Storage, describer: Describer):
        self.storage = storage
        self.describer = describer

    def process(self, file_name: str, data: bytes, content_type: str | None) -> FileResult:
        kind = detect_kind(file_name, content_type, data[:4096])
        stored_type = kind.media_type
        key = make_key(file_name)
        base_meta = {"original-name": fit_encoded(file_name, 300), "kind": kind.kind}
        result = FileResult(
            file_name=file_name, key=key, size_bytes=len(data), content_type=stored_type,
            kind=kind.kind, status="failed",
        )

        # 1. Store the file.
        try:
            self.storage.upload(key, data, stored_type, metadata=base_meta)
        except StorageError as exc:
            log.exception("upload failed for %s", file_name)
            result.key, result.error = None, str(exc)
            return result
        result.status = "stored"

        # 2. Describe it. A failure here leaves the file stored.
        if kind.kind == "other":
            result.error = f"No AI description for this file type ({stored_type})"
            return result
        try:
            desc = self.describer.describe(data, kind, file_name)
        except DescribeError as exc:
            log.warning("describe failed for %s: %s", key, exc)
            result.error = str(exc)
            return result

        # 3. Attach the description to the stored object (sized to fit B2's 2 KB metadata limit).
        try:
            self.storage.set_metadata(
                key,
                {**base_meta, "ai-description": fit_encoded(desc.text, 1500), "ai-model": desc.model},
                stored_type,
            )
        except StorageError as exc:
            log.warning("attaching metadata failed for %s: %s", key, exc)
            result.description, result.model, result.error = desc.text, desc.model, str(exc)
            return result

        result.status = "described"
        result.description = desc.text
        result.model = desc.model
        result.input_truncated = desc.input_truncated
        return result
