"""HTTP handlers for profile catalog endpoints."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Path, status

from docflow.profile_catalog import (
    CatalogConfig,
    get_profile_metadata as catalog_get_metadata,
    list_profiles as catalog_list_profiles,
    list_profiles_with_versions as catalog_list_with_versions,
)

from ..config import ServiceConfig, build_catalog_config, load_service_config


router = APIRouter()


def _get_catalog_or_404(cfg: ServiceConfig) -> CatalogConfig:
    cc = build_catalog_config(cfg)
    if cc is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Profile catalog not configured")
    return cc


@router.get("/profiles")
def list_profiles(
    include_versions: bool = Query(default=False),
    prefix: Optional[str] = Query(default=None, description="Optional folder/prefix filter"),
    cfg: ServiceConfig = Depends(load_service_config),
) -> Dict[str, Any]:
    catalog = _get_catalog_or_404(cfg)
    try:
        if include_versions:
            bases, versions = catalog_list_with_versions(catalog, prefix_filter=prefix)
            return {"profiles": bases, "versions": versions}
        else:
            bases = catalog_list_profiles(catalog, prefix_filter=prefix)
            return {"profiles": bases}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


@router.get("/profiles/{profile_path:path}")
def get_profile(
    profile_path: str = Path(..., description="Relative profile path (may omit version)"),
    cfg: ServiceConfig = Depends(load_service_config),
) -> Dict[str, Any]:
    catalog = _get_catalog_or_404(cfg)
    try:
        metadata = catalog_get_metadata(profile_path, catalog)
        return {
            "path": metadata.path,
            "version": metadata.version,
            "files": [f.__dict__ for f in metadata.files],
            "requested_path": metadata.requested_path,
            "available_versions": metadata.available_versions,
        }
    except FileNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc

