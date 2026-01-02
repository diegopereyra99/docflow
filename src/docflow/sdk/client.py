"""Python client for DocFlow."""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Optional

import requests

from docflow.core.extraction.engine import ExtractionResult, MultiResult, extract
from docflow.core.models import FileSource
from docflow.core.models.schema_defs import InternalSchema, parse_schema
from docflow.core.providers.base import ModelProvider, ProviderOptions
from docflow.core.providers.gemini import GeminiProvider
from docflow.sdk.errors import ConfigError, RemoteServiceError
from docflow.sdk.profiles import load_profile
from .config import SdkConfig, load_config, merge_cli_overrides


class DocflowClient:
    def __init__(
        self,
        mode: str | None = None,
        endpoint_url: str | None = None,
        provider: ModelProvider | None = None,
        config: SdkConfig | None = None,
    ) -> None:
        base_config = config or load_config()
        self.config = merge_cli_overrides(base_config, mode=mode, endpoint=endpoint_url)
        self.mode = self.config.mode
        self.endpoint_url = self.config.endpoint_url or endpoint_url
        self.provider = provider

        if self.mode == "remote" and not self.endpoint_url:
            raise ConfigError("Remote mode requires endpoint_url")

    # --- public methods ---
    def extract(self, schema: dict | InternalSchema, files: List[str | Path], multi_mode: str = "per_file"):
        profile = load_profile("extract", self.config)
        return self._execute(
            schema,
            files,
            profile_name=None,
            profile=profile,
            multi_mode=multi_mode,
        )

    def extract_all(self, files: List[str | Path], multi_mode: str = "per_file"):
        profile = load_profile("extract_all", self.config)
        return self._execute(
            schema=None,
            files=files,
            profile_name="extract_all",
            profile=profile,
            multi_mode=multi_mode,
        )

    def describe(self, files: List[str | Path], multi_mode: str = "per_file"):
        profile = load_profile("describe", self.config)
        return self._execute(
            schema=None,
            files=files,
            profile_name="describe",
            profile=profile,
            multi_mode=multi_mode,
        )

    def run_profile(
        self,
        profile_name: str,
        files: List[str | Path],
        multi_mode: str = "per_file",
        service_mode: str | None = None,
        workers: Optional[int] = None,
        model: Optional[str] = None,
        parameters: Optional[dict] = None,
        repair_attempts: int = 1,
        groups: Optional[list] = None,
    ):
        profile = load_profile(profile_name, self.config)
        return self._execute(
            schema=profile.schema,
            files=files,
            profile_name=profile_name,
            profile=profile,
            multi_mode=multi_mode,
            service_mode=service_mode,
            workers=workers,
            model=model,
            parameters=parameters,
            repair_attempts=repair_attempts,
            groups=groups,
        )

    # --- internal helpers ---
    def _sources_from_files(self, files: Iterable[str | Path]) -> List[FileSource]:
        return [FileSource(Path(path)) for path in files]

    def _provider(self) -> ModelProvider:
        if self.provider:
            return self.provider
        return GeminiProvider()

    def _execute(
        self,
        schema: dict | InternalSchema | None,
        files: List[str | Path],
        profile_name: str | None,
        profile,
        multi_mode: str,
        service_mode: str | None = None,
        workers: Optional[int] = None,
        model: Optional[str] = None,
        parameters: Optional[dict] = None,
        repair_attempts: int = 1,
        groups: Optional[list] = None,
    ):
        if self.mode == "local":
            sources = self._sources_from_files(files)
            internal_schema = schema
            if isinstance(schema, dict):
                internal_schema = parse_schema(schema)
            return extract(
                docs=sources,
                schema=internal_schema,
                profile=profile,
                provider=self._provider(),
                multi_mode=multi_mode,
            )
        return self._execute_remote(
            files=files,
            profile_name=profile_name,
            multi_mode=multi_mode,
            service_mode=service_mode,
            workers=workers,
            model=model,
            parameters=parameters,
            repair_attempts=repair_attempts,
            groups=groups,
        )

    def _execute_remote(
        self,
        files: List[str | Path],
        profile_name: str | None,
        multi_mode: str,
        service_mode: str | None = None,
        workers: Optional[int] = None,
        model: Optional[str] = None,
        parameters: Optional[dict] = None,
        repair_attempts: int = 1,
        groups: Optional[list] = None,
    ):
        if profile_name is None:
            raise ConfigError("Remote mode requires a profile path")

        def _map_mode(m: str) -> str:
            m = m.lower()
            if m == "per_file":
                return "per_file"
            if m == "aggregate":
                return "single"
            if m == "both":
                raise ConfigError("Remote mode does not support multi=both; use per_file or aggregate (single) or grouped")
            return m

        resolved_mode = service_mode.lower() if service_mode else _map_mode(multi_mode)

        def _validate_uri(uri: str) -> str:
            if not (uri.startswith("gs://") or uri.startswith("http://") or uri.startswith("https://")):
                raise ConfigError("Remote mode requires gs:// or http(s):// URIs")
            return uri

        file_objs = [{"uri": _validate_uri(str(f))} for f in files]
        if resolved_mode != "grouped" and not file_objs:
            raise ConfigError("At least one file is required")
        if resolved_mode == "grouped" and not groups:
            raise ConfigError("Grouped mode requires --groups")

        param_payload = {k: v for k, v in (parameters or {}).items() if v is not None}
        payload: dict = {
            "profile_path": profile_name,
            "mode": resolved_mode,
        }
        if resolved_mode == "grouped":
            payload["groups"] = groups
        else:
            payload["files"] = file_objs
        if workers is not None:
            payload["workers"] = workers
        if model:
            payload["model"] = model
        if param_payload:
            payload["parameters"] = param_payload
        payload["repair"] = {"enabled": repair_attempts > 0, "max_attempts": max(1, repair_attempts)}

        url = f"{self.endpoint_url.rstrip('/')}/extract"
        resp = requests.post(url, json=payload, timeout=120)
        try:
            data = resp.json()
        except Exception as exc:  # pragma: no cover
            raise RemoteServiceError(f"Invalid response from service: {exc}") from exc

        if not resp.ok or (isinstance(data, dict) and data.get("ok") is False):
            message = None
            if isinstance(data, dict):
                message = data.get("detail") or data.get("error") or data.get("message")
            raise RemoteServiceError(f"Service error: {message or resp.text}")

        if not isinstance(data, dict):
            raise RemoteServiceError("Unexpected service response")

        payload_data = data.get("data", {})
        meta = data.get("meta", {})

        # Grouped responses: return raw groups for now
        if isinstance(payload_data, dict) and "groups" in payload_data:
            return payload_data

        # Per-file list response
        if isinstance(payload_data, list):
            results: List[ExtractionResult] = []
            for item in payload_data:
                if isinstance(item, dict):
                    body = item.get("data", item)
                    item_meta = item.get("meta", {}) or meta
                else:
                    body = {"value": item}
                    item_meta = meta
                results.append(ExtractionResult(body, item_meta))
            return MultiResult(per_file=results)

        if isinstance(payload_data, dict):
            if "data" in payload_data:
                return ExtractionResult(payload_data.get("data"), payload_data.get("meta") or meta)
            return ExtractionResult(payload_data, meta)

        return ExtractionResult({"value": payload_data}, meta)
