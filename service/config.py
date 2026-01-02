"""Service configuration (environment-backed)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from pathlib import Path
from docflow.profile_catalog import CatalogConfig


@dataclass
class ServiceConfig:
    default_model: str
    gcp_project: Optional[str]
    location: str
    pubsub_topic_results: Optional[str]
    default_temperature: float
    # Profile catalog configuration
    profiles_backend: Optional[str] = None  # "fs" | "gcs"
    profiles_bucket: Optional[str] = None
    profiles_prefix: str = "profiles/"
    profiles_root_dir: Optional[str] = None
    catalog_cache_ttl_seconds: int = 600


def load_service_config() -> ServiceConfig:
    return ServiceConfig(
        default_model=os.environ.get("DOCFLOW_DEFAULT_MODEL", "gemini-2.5-flash"),
        gcp_project=os.environ.get("DOCFLOW_GCP_PROJECT"),
        location=os.environ.get("DOCFLOW_LOCATION", "us-central1"),
        pubsub_topic_results=os.environ.get("DOCFLOW_PUBSUB_TOPIC_RESULTS"),
        default_temperature=float(os.environ.get("DOCFLOW_DEFAULT_TEMPERATURE", "0.0")),
        profiles_backend=os.environ.get("DOCFLOW_PROFILES_BACKEND"),
        profiles_bucket=os.environ.get("DOCFLOW_PROFILES_BUCKET"),
        profiles_prefix=os.environ.get("DOCFLOW_PROFILES_PREFIX", "profiles/"),
        profiles_root_dir=os.environ.get("DOCFLOW_PROFILES_ROOT_DIR"),
        catalog_cache_ttl_seconds=int(os.environ.get("DOCFLOW_CATALOG_CACHE_TTL", "600")),
    )


def build_catalog_config(cfg: ServiceConfig) -> CatalogConfig | None:
    if not cfg.profiles_backend:
        return None
    backend = cfg.profiles_backend.lower()
    if backend == "fs":
        root = Path(cfg.profiles_root_dir or str(Path.cwd()))
        return CatalogConfig(
            backend="fs",
            root_dir=root,
            prefix=cfg.profiles_prefix,
            cache_ttl_seconds=cfg.catalog_cache_ttl_seconds,
        )
    if backend == "gcs":
        if not cfg.profiles_bucket:
            return None
        return CatalogConfig(
            backend="gcs",
            bucket=cfg.profiles_bucket,
            prefix=cfg.profiles_prefix,
            cache_ttl_seconds=cfg.catalog_cache_ttl_seconds,
        )
    return None
