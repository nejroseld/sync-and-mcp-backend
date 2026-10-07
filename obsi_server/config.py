"""Server configuration: defaults < TOML file (OBSI_CONFIG) < environment variables."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

FEATURES = ("ai", "mcp", "rag")
# Obsidian desktop (Electron) and mobile (Capacitor) origins.
DEFAULT_CORS_ORIGINS = ("app://obsidian.md", "capacitor://localhost", "http://localhost")
S3_SSE_MODES = ("auto", "AES256", "aws:kms", "none")


def _flag(value: str) -> bool:
    return value.strip().lower() not in ("0", "false", "no", "off", "")


@dataclass
class Config:
    data_dir: Path = Path("./obsi-data")
    host: str = "127.0.0.1"
    port: int = 8765
    ai: bool = True
    mcp: bool = True
    rag: bool = True
    max_body_bytes: int = 200 * 1024 * 1024
    max_ai_file_bytes: int = 32 * 1024 * 1024
    max_concurrent_uploads: int = 4
    vault_quota_bytes: int = 10 * 1024 * 1024 * 1024  # 0 = unlimited
    sync_requests_per_minute: int = 6000
    rag_worker: bool = True  # background embedding thread
    embedding_allow_private: bool = False  # allow http:// and private addresses for embeddings
    cors_origins: list[str] = field(default_factory=lambda: list(DEFAULT_CORS_ORIGINS))
    api_docs: bool = False
    session_ttl_days: int = 180
    session_idle_days: int = 30
    invite_ttl_days: int = 7
    s3_bucket: str | None = None
    s3_region: str | None = None
    s3_endpoint: str | None = None
    s3_prefix: str = "obsi"
    s3_threshold_mb: int = 10
    s3_sse: str = "auto"
    s3_kms_key_id: str | None = None

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

    def s3_put_args(self) -> dict:
        """Server-side encryption arguments for put_object."""
        mode = self.s3_sse
        if mode == "auto":
            # AWS supports SSE-S3 everywhere; S3-compatible providers often reject the header.
            mode = "none" if self.s3_endpoint else "AES256"
        if mode == "AES256":
            return {"ServerSideEncryption": "AES256"}
        if mode == "aws:kms":
            args = {"ServerSideEncryption": "aws:kms"}
            if self.s3_kms_key_id:
                args["SSEKMSKeyId"] = self.s3_kms_key_id
            return args
        return {}

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
            if "max_ai_file_mb" in s:
                cfg.max_ai_file_bytes = int(s["max_ai_file_mb"]) * 1024 * 1024
            if "vault_quota_mb" in s:
                cfg.vault_quota_bytes = int(s["vault_quota_mb"]) * 1024 * 1024
            cfg.max_concurrent_uploads = int(s.get("max_concurrent_uploads", cfg.max_concurrent_uploads))
            if "sync_requests_per_minute" in s:
                cfg.sync_requests_per_minute = int(s["sync_requests_per_minute"])
            cfg.rag_worker = bool(s.get("rag_worker", cfg.rag_worker))
            cfg.embedding_allow_private = bool(s.get("embedding_allow_private", cfg.embedding_allow_private))
            if "cors_origins" in s:
                cfg.cors_origins = [str(o).strip() for o in s["cors_origins"] if str(o).strip()]
            cfg.api_docs = bool(s.get("api_docs", cfg.api_docs))
            cfg.session_ttl_days = int(s.get("session_ttl_days", cfg.session_ttl_days))
            cfg.session_idle_days = int(s.get("session_idle_days", cfg.session_idle_days))
            cfg.invite_ttl_days = int(s.get("invite_ttl_days", cfg.invite_ttl_days))
            cfg.s3_bucket = s.get("s3_bucket", cfg.s3_bucket)
            cfg.s3_region = s.get("s3_region", cfg.s3_region)
            cfg.s3_endpoint = s.get("s3_endpoint", cfg.s3_endpoint)
            cfg.s3_prefix = s.get("s3_prefix", cfg.s3_prefix)
            cfg.s3_threshold_mb = int(s.get("s3_threshold_mb", cfg.s3_threshold_mb))
            cfg.s3_sse = s.get("s3_sse", cfg.s3_sse)
            cfg.s3_kms_key_id = s.get("s3_kms_key_id", cfg.s3_kms_key_id)
        if "OBSI_DATA_DIR" in env:
            cfg.data_dir = Path(env["OBSI_DATA_DIR"])
        cfg.host = env.get("OBSI_HOST", cfg.host)
        cfg.port = int(env.get("OBSI_PORT", cfg.port))
        if "OBSI_FEATURES" in env:
            cfg.set_features(env["OBSI_FEATURES"])
        if "OBSI_MAX_BODY_MB" in env:
            cfg.max_body_bytes = int(env["OBSI_MAX_BODY_MB"]) * 1024 * 1024
        if "OBSI_MAX_AI_FILE_MB" in env:
            cfg.max_ai_file_bytes = int(env["OBSI_MAX_AI_FILE_MB"]) * 1024 * 1024
        if "OBSI_VAULT_QUOTA_MB" in env:
            cfg.vault_quota_bytes = int(env["OBSI_VAULT_QUOTA_MB"]) * 1024 * 1024
        if "OBSI_MAX_CONCURRENT_UPLOADS" in env:
            cfg.max_concurrent_uploads = int(env["OBSI_MAX_CONCURRENT_UPLOADS"])
        if "OBSI_SYNC_REQUESTS_PER_MINUTE" in env:
            cfg.sync_requests_per_minute = int(env["OBSI_SYNC_REQUESTS_PER_MINUTE"])
        if "OBSI_RAG_WORKER" in env:
            cfg.rag_worker = _flag(env["OBSI_RAG_WORKER"])
        if "OBSI_EMBEDDING_ALLOW_PRIVATE" in env:
            cfg.embedding_allow_private = _flag(env["OBSI_EMBEDDING_ALLOW_PRIVATE"])
        if "OBSI_CORS_ORIGINS" in env:
            cfg.cors_origins = [o.strip() for o in env["OBSI_CORS_ORIGINS"].split(",") if o.strip()]
        if "OBSI_API_DOCS" in env:
            cfg.api_docs = _flag(env["OBSI_API_DOCS"])
        if "OBSI_SESSION_TTL_DAYS" in env:
            cfg.session_ttl_days = int(env["OBSI_SESSION_TTL_DAYS"])
        if "OBSI_SESSION_IDLE_DAYS" in env:
            cfg.session_idle_days = int(env["OBSI_SESSION_IDLE_DAYS"])
        if "OBSI_INVITE_TTL_DAYS" in env:
            cfg.invite_ttl_days = int(env["OBSI_INVITE_TTL_DAYS"])
        cfg.s3_bucket = env.get("OBSI_S3_BUCKET", cfg.s3_bucket)
        cfg.s3_region = env.get("OBSI_S3_REGION", cfg.s3_region)
        cfg.s3_endpoint = env.get("OBSI_S3_ENDPOINT", cfg.s3_endpoint)
        cfg.s3_prefix = env.get("OBSI_S3_PREFIX", cfg.s3_prefix)
        if "OBSI_S3_THRESHOLD_MB" in env:
            cfg.s3_threshold_mb = int(env["OBSI_S3_THRESHOLD_MB"])
        cfg.s3_sse = env.get("OBSI_S3_SSE", cfg.s3_sse)
        cfg.s3_kms_key_id = env.get("OBSI_S3_KMS_KEY_ID", cfg.s3_kms_key_id)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.s3_threshold_mb < 0:
            raise ValueError("s3_threshold_mb must be non-negative")
        if self.sync_requests_per_minute < 1:
            raise ValueError("sync_requests_per_minute must be positive")
        if "*" in self.cors_origins:
            raise ValueError("cors_origins must list explicit origins; '*' is not allowed")
        if self.max_concurrent_uploads < 1:
            raise ValueError("max_concurrent_uploads must be positive")
        if self.vault_quota_bytes < 0 or self.max_ai_file_bytes < 1:
            raise ValueError("vault_quota_mb must be non-negative and max_ai_file_mb positive")
        if min(self.session_ttl_days, self.session_idle_days, self.invite_ttl_days) < 1:
            raise ValueError("session and invite lifetimes must be at least one day")
        if self.s3_sse not in S3_SSE_MODES:
            raise ValueError(f"s3_sse must be one of {', '.join(S3_SSE_MODES)}")
