"""Settings loaded from environment variables (and a local .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

REQUIRED = ("B2_ENDPOINT", "B2_REGION", "B2_BUCKET", "B2_KEY_ID", "B2_APPLICATION_KEY", "ANTHROPIC_API_KEY")


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    b2_endpoint: str
    b2_region: str
    b2_bucket: str
    b2_key_id: str
    b2_application_key: str
    anthropic_api_key: str
    anthropic_model: str
    max_upload_bytes: int
    local_upload_dir: Path


@lru_cache
def get_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env")
    missing = [name for name in REQUIRED if not os.getenv(name)]
    if missing:
        raise ConfigError(f"Missing required environment variables: {', '.join(missing)} (see .env.example)")

    endpoint = os.environ["B2_ENDPOINT"]
    if not endpoint.startswith("http"):
        endpoint = f"https://{endpoint}"

    local_dir = Path(os.getenv("LOCAL_UPLOAD_DIR", "samples"))
    if not local_dir.is_absolute():
        local_dir = PROJECT_ROOT / local_dir

    return Settings(
        b2_endpoint=endpoint,
        b2_region=os.environ["B2_REGION"],
        b2_bucket=os.environ["B2_BUCKET"],
        b2_key_id=os.environ["B2_KEY_ID"],
        b2_application_key=os.environ["B2_APPLICATION_KEY"],
        anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5"),
        max_upload_bytes=int(os.getenv("MAX_UPLOAD_MB", "20")) * 1024 * 1024,
        local_upload_dir=local_dir.resolve(),
    )
