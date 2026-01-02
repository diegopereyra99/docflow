"""Unified HTTP handler for /extract (profile-first)."""
from __future__ import annotations

import asyncio
from enum import Enum
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field, field_validator, model_validator

from docflow.core.extraction.engine import extract as df_extract, ExtractionResult as DFExtractionResult
from docflow.core.models.profiles import ExtractionProfile as DFProfile
from docflow.core.models.schema_defs import parse_schema as df_parse_schema
from docflow.core.models.documents import GcsSource as DFGcsSource, HttpSource as DFHttpSource
from docflow.core.providers.base import ProviderOptions as DFProviderOptions
from docflow.core.errors import SchemaError as DFSchemaError, ProviderError as DFProviderError, ExtractionError as DFExtractionError, DocumentError as DFDocumentError
from docflow.profile_catalog import load_profile as catalog_load_profile

from ..config import ServiceConfig, build_catalog_config, load_service_config
from ..dependencies import get_logger, get_provider


router = APIRouter()


class ExtractionMode(str, Enum):
    SINGLE = "single"
    PER_FILE = "per_file"
    GROUPED = "grouped"


class DocumentRef(BaseModel):
    uri: str = Field(..., description="gs:// or https(s) URI to the document")
    display_name: Optional[str] = Field(default=None)

    @field_validator("uri")
    def validate_uri(cls, v: str) -> str:
        low = v.lower()
        if not (low.startswith("gs://") or low.startswith("https://") or low.startswith("http://")):
            raise ValueError("uri must start with gs://, https://, or http://")
        return v


class DocumentGroup(BaseModel):
    id: str
    files: List[DocumentRef]
    metadata: Optional[Dict[str, Any]] = None

    @field_validator("files")
    def validate_files(cls, v: List[DocumentRef]) -> List[DocumentRef]:
        if not v:
            raise ValueError("group must include at least one file")
        return v


class RepairConfig(BaseModel):
    enabled: bool = True
    max_attempts: int = Field(default=1, ge=1, le=2)


class ModelParameters(BaseModel):
    temperature: Optional[float] = Field(default=None)
    top_p: Optional[float] = Field(default=None)
    max_output_tokens: Optional[int] = Field(default=None)


class ExtractionRequest(BaseModel):
    profile_path: str = Field(..., description="Relative profile path, e.g. invoices/extract[/vN]")
    mode: ExtractionMode = Field(default=ExtractionMode.PER_FILE)
    files: Optional[List[DocumentRef]] = Field(default=None)
    groups: Optional[List[DocumentGroup]] = Field(default=None)
    workers: Optional[int] = Field(default=None)
    request_id: Optional[str] = Field(default=None)
    model: Optional[str] = Field(default=None)
    parameters: ModelParameters = Field(default_factory=ModelParameters)
    repair: RepairConfig = Field(default_factory=RepairConfig)

    @model_validator(mode="after")
    def validate_inputs(self) -> "ExtractionRequest":
        if self.mode in {ExtractionMode.SINGLE, ExtractionMode.PER_FILE} and not self.files:
            raise ValueError("files are required for single or per_file mode")
        if self.mode == ExtractionMode.GROUPED and not self.groups:
            raise ValueError("groups are required for grouped mode")
        return self


def _doc_name(uri: str, display_name: str | None) -> str:
    if display_name:
        return display_name
    try:
        if uri.startswith("gs://"):
            return uri.rsplit("/", 1)[-1]
        # http(s)
        from urllib.parse import urlparse

        path = urlparse(uri).path
        return path.rsplit("/", 1)[-1] or uri
    except Exception:
        return uri


def _result_obj(model: str, docs: List[str], mode_label: str, profile_path: str, payload: Any) -> Dict[str, Any]:
    return {"data": payload, "meta": {"model": model, "docs": docs, "mode": mode_label, "profile": profile_path}}


@router.post("/extract")
async def extract(payload: ExtractionRequest, cfg: ServiceConfig = Depends(load_service_config)) -> Dict[str, Any]:
    logger = get_logger()

    # Require catalog configuration (this API is profile-first)
    catalog_cfg = build_catalog_config(cfg)
    if catalog_cfg is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Profile catalog not configured")

    # Load profile via shared catalog
    try:
        prof = catalog_load_profile(payload.profile_path, catalog_cfg)
    except FileNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc

    # Build DocFlow profile + provider
    df_schema = df_parse_schema(prof.schema)
    df_profile = DFProfile(
        name=prof.path,
        schema=df_schema,
        mode="extract",
        prompt=prof.prompt,
        system_instruction=prof.system_instruction,
    )
    provider = get_provider(cfg)

    options = DFProviderOptions(
        model_name=payload.model or cfg.default_model,
        temperature=payload.parameters.temperature,
        top_p=payload.parameters.top_p,
        max_output_tokens=payload.parameters.max_output_tokens,
        attachment_strategy="uri",
    )

    # Build work items
    items: List[tuple[str, List[DocumentRef]]] = []
    if payload.mode == ExtractionMode.SINGLE:
        items.append(("item-1", payload.files or []))
    elif payload.mode == ExtractionMode.PER_FILE:
        for i, f in enumerate(payload.files or [], start=1):
            items.append((f"item-{i}", [f]))
    else:  # grouped
        for g in payload.groups or []:
            items.append((g.id, g.files))

    # Concurrency control
    requested_workers = payload.workers if payload.workers is not None else max(1, cfg.default_workers)
    max_workers = min(max(1, requested_workers), max(1, cfg.max_workers), len(items))
    sem = asyncio.Semaphore(max_workers)

    async def _process(item_id: str, docs: List[DocumentRef]) -> tuple[str, Optional[DFExtractionResult], Optional[str]]:
        async with sem:
            # Convert docs to DocFlow sources with URI passthrough
            df_docs = [DFGcsSource(uri=d.uri) if d.uri.startswith("gs://") else DFHttpSource(url=d.uri, name=d.display_name) for d in docs]
            attempts = payload.repair.max_attempts if payload.repair.enabled else 0
            loop = asyncio.get_event_loop()

            def _call():
                # Aggregate per item (single result per item)
                return df_extract(docs=df_docs, profile=df_profile, provider=provider, options=options, multi_mode="aggregate", repair_attempts=attempts)

            try:
                res = await loop.run_in_executor(None, _call)
                # extract() may return a list in some modes; normalize to DFExtractionResult
                if isinstance(res, list):
                    df_res = res[0]
                else:
                    df_res = res
                return item_id, df_res, None
            except (DFSchemaError, DFProviderError, DFExtractionError, DFDocumentError) as exc:
                return item_id, None, str(exc)
            except Exception as exc:  # pragma: no cover
                return item_id, None, str(exc)

    # Execute
    results = await asyncio.gather(*[_process(item_id, docs) for item_id, docs in items])

    model_used = getattr(provider, "last_model", None) or options.model_name or cfg.default_model

    # Shape response like DocFlow envelope
    if payload.mode == ExtractionMode.SINGLE:
        _, df_res, err = results[0]
        if err:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=err)
        docs = [_doc_name(d.uri, d.display_name) for d in (payload.files or [])]
        return {"ok": True, "data": _result_obj(model_used, docs, "aggregate", prof.path, df_res.data), "meta": {"model": model_used}}

    if payload.mode == ExtractionMode.PER_FILE:
        files = payload.files or []
        objs: List[Dict[str, Any]] = []
        for idx, (_, df_res, err) in enumerate(results):
            if err:
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=err)
            name = files[idx].display_name if idx < len(files) else None
            uri = files[idx].uri if idx < len(files) else f"item-{idx+1}"
            docs = [_doc_name(uri, name)]
            objs.append(_result_obj(model_used, docs, "per_file", prof.path, df_res.data))
        return {"ok": True, "data": objs, "meta": {"model": model_used}}

    # grouped
    groups: List[Dict[str, Any]] = []
    res_map = {item_id: (df_res, err) for item_id, df_res, err in results}
    for grp in (payload.groups or []):
        df_res, err = res_map.get(grp.id, (None, "Group result missing"))
        if err or df_res is None:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(err or "group error"))
        docs = [_doc_name(d.uri, d.display_name) for d in grp.files]
        groups.append({"group_id": grp.id, "result": _result_obj(model_used, docs, "grouped", prof.path, df_res.data)})
    logger.info("Handled extraction with %s item(s)", len(items))
    return {"ok": True, "data": {"groups": groups}, "meta": {"model": model_used}}
