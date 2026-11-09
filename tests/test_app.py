"""Offline tests: B2 is stubbed with botocore's Stubber and Claude with a fake client."""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import anthropic
import boto3
import httpx
import pytest
from botocore.stub import ANY, Stubber
from fastapi.testclient import TestClient
from PIL import Image

from app import main
from app.config import Settings
from app.describer import Describer, DescribeError, FileKind, clean_description, detect_kind, prepare_image
from app.service import FileService, safe_name
from app.storage import B2Storage, decode_metadata, encode_metadata, fit_encoded

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20


def png_bytes(size=(4, 4)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, "red").save(buf, format="PNG")
    return buf.getvalue()


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        b2_endpoint="https://s3.example.com", b2_region="us-east-005", b2_bucket="bucket",
        b2_key_id="id", b2_application_key="secret", anthropic_api_key="key",
        anthropic_model="claude-opus-5-5", max_upload_bytes=1024 * 1024, local_upload_dir=tmp_path.resolve(),
    )


class FakeMessages:
    def __init__(self, text="A red square.", stop_reason="end_turn", exc=None):
        self.text, self.stop_reason, self.exc, self.calls = text, stop_reason, exc, []

    def create(self, **params):
        self.calls.append(params)
        if self.exc:
            raise self.exc
        content = [SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=self.text)]
        return SimpleNamespace(content=content, stop_reason=self.stop_reason, model=params["model"])


def fake_describer(**kw) -> tuple[Describer, FakeMessages]:
    messages = FakeMessages(**kw)
    client = SimpleNamespace(beta=SimpleNamespace(messages=messages))
    return Describer("key", "claude-opus-5-5", client=client), messages


class FakeStorage:
    def __init__(self, fail_upload=False):
        self.objects, self.fail_upload = {}, fail_upload

    def upload(self, key, data, content_type, metadata=None):
        from app.storage import StorageError
        if self.fail_upload:
            raise StorageError("boom")
        self.objects[key] = {"data": data, "content_type": content_type, "metadata": dict(metadata or {})}

    def set_metadata(self, key, metadata, content_type):
        self.objects[key]["metadata"] = dict(metadata)

    def head(self, key):
        from app.storage import ObjectNotFound
        if key not in self.objects:
            raise ObjectNotFound(key)
        return {"key": key, "metadata": self.objects[key]["metadata"]}


# --- type detection -------------------------------------------------------

@pytest.mark.parametrize("name,ctype,head,kind", [
    ("a.png", "image/png", PNG, "image"),
    ("mislabeled.txt", "text/plain", PNG, "image"),          # magic bytes win
    ("photo", "application/octet-stream", b"\xff\xd8\xff\xe0", "image"),
    ("doc.pdf", None, b"%PDF-1.7\n", "pdf"),
    ("notes.md", None, "# Título\nhola".encode(), "text"),
    ("data.json", "application/json", b'{"a": 1}', "text"),
    ("blob.bin", "application/octet-stream", b"\x00\x01\x02", "other"),
    ("archive.zip", "application/zip", b"PK\x03\x04", "other"),
])
def test_detect_kind(name, ctype, head, kind):
    assert detect_kind(name, ctype, head).kind == kind


def test_prepare_image_downscales_large_images():
    big = png_bytes((9000, 10))
    data, media_type = prepare_image(big, "image/png")
    assert media_type == "image/jpeg"
    assert max(Image.open(io.BytesIO(data)).size) <= 1568


def test_clean_description_caps_length():
    text = clean_description('  "' + "word " * 200 + '"  ')
    assert len(text) <= 300 and text.endswith("…") and '"' not in text


# --- describer -------------------------------------------------------------

def test_describe_image_sends_image_block_and_fallbacks():
    describer, messages = fake_describer(text="  A red\nsquare.  ")
    desc = describer.describe(png_bytes(), FileKind("image", "image/png"), "red.png")
    assert desc.text == "A red square."
    params = messages.calls[0]
    assert params["messages"][0]["content"][0]["type"] == "image"
    assert params["fallbacks"] == "default" and params["betas"] == ["server-side-fallback-2026-07-01"]
    assert params["output_config"] == {"effort": "low"} and "thinking" not in params


def test_describe_refusal_raises():
    describer, _ = fake_describer(stop_reason="refusal", text="")
    with pytest.raises(DescribeError, match="declined"):
        describer.describe(b"hello", FileKind("text", "text/plain"), "a.txt")


def test_describe_api_error_raises():
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    exc = anthropic.RateLimitError("slow down", response=httpx.Response(429, request=req), body=None)
    describer, _ = fake_describer(exc=exc)
    with pytest.raises(DescribeError, match="rate limit"):
        describer.describe(b"hello", FileKind("text", "text/plain"), "a.txt")


def test_long_text_is_truncated_and_flagged():
    describer, messages = fake_describer()
    desc = describer.describe(b"x" * 300_000, FileKind("text", "text/plain"), "big.txt")
    assert desc.input_truncated
    assert "Only the first part" in messages.calls[0]["messages"][0]["content"][1]["text"]


# --- storage ---------------------------------------------------------------

def test_metadata_round_trips_non_ascii():
    meta = {"ai-description": "Un gato naranja dormido — 猫", "original-name": "foto ñ.jpg"}
    encoded = encode_metadata(meta)
    assert all(v.isascii() for v in encoded.values())
    assert decode_metadata(encoded) == meta


def test_fit_encoded_respects_budget():
    text = fit_encoded("猫" * 500, 1500)
    assert len(encode_metadata({"d": text})["d"]) <= 1500 and text.endswith("…")


def test_set_metadata_copies_object_in_place(tmp_path):
    client = boto3.client("s3", region_name="us-east-005", aws_access_key_id="a", aws_secret_access_key="b",
                          endpoint_url="https://s3.example.com")
    storage = B2Storage(make_settings(tmp_path), client=client)
    with Stubber(client) as stub:
        stub.add_response("copy_object", {}, {
            "Bucket": "bucket", "Key": "uploads/k.txt", "CopySource": {"Bucket": "bucket", "Key": "uploads/k.txt"},
            "MetadataDirective": "REPLACE", "ContentType": "text/plain", "Metadata": {"ai-description": "Hi there."},
        })
        storage.set_metadata("uploads/k.txt", {"ai-description": "Hi there."}, "text/plain")
        stub.assert_no_pending_responses()


def test_head_missing_object_raises_not_found(tmp_path):
    from app.storage import ObjectNotFound
    client = boto3.client("s3", region_name="us-east-005", aws_access_key_id="a", aws_secret_access_key="b",
                          endpoint_url="https://s3.example.com")
    storage = B2Storage(make_settings(tmp_path), client=client)
    with Stubber(client) as stub:
        stub.add_client_error("head_object", "404", http_status_code=404, expected_params={"Bucket": "bucket", "Key": ANY})
        with pytest.raises(ObjectNotFound):
            storage.head("nope")


# --- service ---------------------------------------------------------------

def test_service_success_attaches_description():
    storage = FakeStorage()
    describer, _ = fake_describer()
    result = FileService(storage, describer).process("red.png", png_bytes(), "image/png")
    assert result.status == "described" and result.description == "A red square."
    assert storage.objects[result.key]["metadata"]["ai-description"] == "A red square."


def test_service_ai_failure_keeps_file_stored():
    storage = FakeStorage()
    describer, _ = fake_describer(stop_reason="refusal", text="")
    result = FileService(storage, describer).process("a.txt", b"hello", "text/plain")
    assert result.status == "stored" and result.key in storage.objects and "declined" in result.error


def test_service_unsupported_type_skips_ai():
    storage = FakeStorage()
    describer, messages = fake_describer()
    result = FileService(storage, describer).process("a.zip", b"PK\x03\x04data", "application/zip")
    assert result.status == "stored" and not messages.calls


def test_service_upload_failure():
    describer, _ = fake_describer()
    result = FileService(FakeStorage(fail_upload=True), describer).process("a.txt", b"hi", "text/plain")
    assert result.status == "failed" and result.key is None


def test_safe_name():
    assert safe_name("../../etc/pass wd?.txt") == "pass_wd_.txt"
    assert safe_name("...") == "file"


# --- HTTP API --------------------------------------------------------------

@pytest.fixture
def api(tmp_path):
    storage = FakeStorage()
    describer, _ = fake_describer(text="Short notes about a meeting.")
    settings = make_settings(tmp_path)
    main.app.dependency_overrides[main.get_service] = lambda: FileService(storage, describer)
    main.app.dependency_overrides[main.get_settings] = lambda: settings
    yield TestClient(main.app), tmp_path, storage
    main.app.dependency_overrides.clear()


def test_upload_endpoint(api):
    client, _, storage = api
    r = client.post("/api/files", files={"file": ("notes.txt", b"meeting notes", "text/plain")})
    body = r.json()
    assert r.status_code == 200 and body["status"] == "described"
    assert body["file_name"] == "notes.txt" and body["size_bytes"] == 13
    assert client.get(f"/api/files/{body['key']}").json()["metadata"]["ai-description"] == body["description"]


def test_upload_too_large(api):
    client, _, _ = api
    r = client.post("/api/files", files={"file": ("big.txt", b"x" * (1024 * 1024 + 1), "text/plain")})
    assert r.status_code == 413 and r.json()["status"] == "failed"


def test_local_path_endpoint(api):
    client, base, _ = api
    (base / "notes.txt").write_text("hello")
    r = client.post("/api/files/local", json={"path": str(base / "notes.txt")})
    assert r.status_code == 200 and r.json()["status"] == "described"
    r = client.post("/api/files/local", json={"path": "notes.txt"})
    assert r.status_code == 200


@pytest.mark.parametrize("path", ["../../etc/passwd", "/etc/passwd"])
def test_local_path_traversal_blocked(api, path):
    client, _, _ = api
    assert client.post("/api/files/local", json={"path": path}).status_code == 400


def test_index_and_health(api):
    client, _, _ = api
    assert client.get("/healthz").json() == {"ok": True}
    assert "B2 File Describer" in client.get("/").text
