"""Backblaze B2 access through its S3-compatible API."""

from __future__ import annotations

from urllib.parse import quote, unquote

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.config import Settings

# B2's S3 API caps user metadata (all x-amz-meta-* headers) at 2 KB.
MAX_METADATA_BYTES = 2048


class StorageError(RuntimeError):
    pass


class ObjectNotFound(StorageError):
    pass


SAFE_CHARS = " ,.:;!?()'-_/"


def _encode(value: str) -> str:
    return quote(value, safe=SAFE_CHARS)


def fit_encoded(text: str, max_encoded: int) -> str:
    """Shorten text so its percent-encoded form fits in max_encoded bytes (non-ASCII inflates up to 12x)."""
    if len(_encode(text)) <= max_encoded:
        return text
    while text and len(_encode(text + "…")) > max_encoded:
        text = text[: int(len(text) * 0.9)]
    return text.rstrip() + "…"


def encode_metadata(metadata: dict[str, str]) -> dict[str, str]:
    """Percent-encode values so non-ASCII text survives as HTTP headers."""
    encoded = {key: _encode(str(value)) for key, value in metadata.items()}
    size = sum(len(k) + len(v) for k, v in encoded.items())
    if size > MAX_METADATA_BYTES:
        raise StorageError(f"Metadata is {size} bytes, over the {MAX_METADATA_BYTES}-byte limit")
    return encoded


def decode_metadata(metadata: dict[str, str]) -> dict[str, str]:
    return {key: unquote(value) for key, value in metadata.items()}


class B2Storage:
    def __init__(self, settings: Settings, client=None):
        self.bucket = settings.b2_bucket
        self.client = client or boto3.client(
            "s3",
            endpoint_url=settings.b2_endpoint,
            region_name=settings.b2_region,
            aws_access_key_id=settings.b2_key_id,
            aws_secret_access_key=settings.b2_application_key,
            config=Config(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"}),
        )

    def upload(self, key: str, data: bytes, content_type: str, metadata: dict[str, str] | None = None) -> None:
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
                Metadata=encode_metadata(metadata or {}),
            )
        except (ClientError, BotoCoreError) as exc:
            raise StorageError(f"Upload to B2 failed: {exc}") from exc

    def set_metadata(self, key: str, metadata: dict[str, str], content_type: str) -> None:
        """Replace an object's metadata. S3 can't edit metadata in place, so copy the object onto itself."""
        try:
            self.client.copy_object(
                Bucket=self.bucket,
                Key=key,
                CopySource={"Bucket": self.bucket, "Key": key},
                MetadataDirective="REPLACE",
                ContentType=content_type,
                Metadata=encode_metadata(metadata),
            )
        except (ClientError, BotoCoreError) as exc:
            raise StorageError(f"Attaching metadata in B2 failed: {exc}") from exc

    def head(self, key: str) -> dict:
        try:
            resp = self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                raise ObjectNotFound(key) from exc
            raise StorageError(f"Reading object from B2 failed: {exc}") from exc
        except BotoCoreError as exc:
            raise StorageError(f"Reading object from B2 failed: {exc}") from exc
        return {
            "key": key,
            "size_bytes": resp.get("ContentLength"),
            "content_type": resp.get("ContentType"),
            "last_modified": resp["LastModified"].isoformat() if resp.get("LastModified") else None,
            "metadata": decode_metadata(resp.get("Metadata", {})),
        }
