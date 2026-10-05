"""Server configuration: defaults < TOML file (OBSI_CONFIG) < environment variables."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

FEATURES = ("ai", "mcp", "rag")


@dataclass
class Config:
    data_dir: Path = Path("./obsi-data")
    host: str = "127.0.0.1"
    port: int = 8765
    ai: bool = True
    mcp: bool = True
    rag: bool = True
    max_body_bytes: int = 200 * 1024 * 1024
    sync_requests_per_minute: int = 6000
    rag_worker: bool = True  # background embedding thread
    cors_origins: list[str] = field(default_factory=lambda: ["*"])
    s3_bucket: str | None = None
    s3_region: str | None = None
    s3_endpoint: str | None = None
    s3_prefix: str = "obsi"
    s3_threshold_mb: int = 10

    @property
    def features(self) -> dict[str, bool]:
        """Effective features: mcp and rag both need the AI Available store."""
        return {"ai": self.ai, "mcp": self.mcp and self.ai, "rag": self.rag and self.ai}

    def set_features(self, spec: str) -> None:
        """`spec` is a comma-separated list of enabled features, or 'all' / 'none'."""
        names = {s.strip().lower() for s in spec.split(",") if s.strip()}
        if "all" in names:
            names = set(FEATURES)
        for f in FEATURES:
            setattr(self, f, f in names)

    @classmethod
    def load(cls, env: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        cfg = cls()
        path = env.get("OBSI_CONFIG")
        if path:
            data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
            s = data.get("server", data)
            if "data_dir" in s:
                cfg.data_dir = Path(s["data_dir"])
            cfg.host = s.get("host", cfg.host)
            cfg.port = int(s.get("port", cfg.port))
            if "features" in s:
                cfg.set_features(",".join(s["features"]))
            if "max_body_mb" in s:
                cfg.max_body_bytes = int(s["max_body_mb"]) * 1024 * 1024
            if "sync_requests_per_minute" in s:
                cfg.sync_requests_per_minute = int(s["sync_requests_per_minute"])
            cfg.rag_worker = bool(s.get("rag_worker", cfg.rag_worker))
            cfg.s3_bucket = s.get("s3_bucket", cfg.s3_bucket)
            cfg.s3_region = s.get("s3_region", cfg.s3_region)
            cfg.s3_endpoint = s.get("s3_endpoint", cfg.s3_endpoint)
            cfg.s3_prefix = s.get("s3_prefix", cfg.s3_prefix)
            cfg.s3_threshold_mb = int(s.get("s3_threshold_mb", cfg.s3_threshold_mb))
        if "OBSI_DATA_DIR" in env:
            cfg.data_dir = Path(env["OBSI_DATA_DIR"])
        cfg.host = env.get("OBSI_HOST", cfg.host)
        cfg.port = int(env.get("OBSI_PORT", cfg.port))
        if "OBSI_FEATURES" in env:
            cfg.set_features(env["OBSI_FEATURES"])
        if "OBSI_MAX_BODY_MB" in env:
            cfg.max_body_bytes = int(env["OBSI_MAX_BODY_MB"]) * 1024 * 1024
        if "OBSI_SYNC_REQUESTS_PER_MINUTE" in env:
            cfg.sync_requests_per_minute = int(env["OBSI_SYNC_REQUESTS_PER_MINUTE"])
        if "OBSI_RAG_WORKER" in env:
            cfg.rag_worker = env["OBSI_RAG_WORKER"].lower() not in ("0", "false", "no", "")
        if "OBSI_CORS_ORIGINS" in env:
            cfg.cors_origins = [o.strip() for o in env["OBSI_CORS_ORIGINS"].split(",") if o.strip()]
        cfg.s3_bucket = env.get("OBSI_S3_BUCKET", cfg.s3_bucket)
        cfg.s3_region = env.get("OBSI_S3_REGION", cfg.s3_region)
        cfg.s3_endpoint = env.get("OBSI_S3_ENDPOINT", cfg.s3_endpoint)
        cfg.s3_prefix = env.get("OBSI_S3_PREFIX", cfg.s3_prefix)
        if "OBSI_S3_THRESHOLD_MB" in env:
            cfg.s3_threshold_mb = int(env["OBSI_S3_THRESHOLD_MB"])
        if cfg.s3_threshold_mb < 0:
            raise ValueError("s3_threshold_mb must be non-negative")
        if cfg.sync_requests_per_minute < 1:
            raise ValueError("sync_requests_per_minute must be positive")
        return cfg
