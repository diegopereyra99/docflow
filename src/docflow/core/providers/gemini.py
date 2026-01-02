"""Gemini provider implementation."""
from __future__ import annotations

import json
import mimetypes
import warnings
from typing import Any, Dict, Tuple

from .. import config
from ..errors import ProviderError
from ..models.schema_defs import Field, InternalSchema
from ..utils.vertex_schema import normalize_for_vertex_schema
from .base import ModelProvider, ProviderOptions

# Suppress noisy Vertex deprecation warning for genai SDK (ignore all UserWarning from module)
warnings.filterwarnings(
    "ignore",
    category=UserWarning,
    module="vertexai.generative_models._generative_models",
)


def _map_type(field_type: str) -> str:
    t = field_type.lower()
    if t in {"string", "number", "integer", "boolean", "object", "array"}:
        return t
    return "string"


def _internal_to_json_schema(schema: InternalSchema) -> Dict[str, Any]:
    properties: Dict[str, Any] = {}
    required: list[str] = []

    for f in schema.global_fields:
        node: Dict[str, Any] = {"type": _map_type(f.type)}
        if f.description:
            node["description"] = f.description
        properties[f.name] = node
        if f.required:
            required.append(f.name)

    for record_set in schema.record_sets:
        item_props: Dict[str, Any] = {}
        item_required: list[str] = []
        for f in record_set.fields:
            node = {"type": _map_type(f.type)}
            if f.description:
                node["description"] = f.description
            item_props[f.name] = node
            if f.required:
                item_required.append(f.name)
        item_schema: Dict[str, Any] = {"type": "object", "properties": item_props}
        if item_required:
            item_schema["required"] = item_required
        properties[record_set.name] = {"type": "array", "items": item_schema}

    schema_dict: Dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema_dict["required"] = required
    return schema_dict


def _extract_text(resp: Any) -> str:
    # Handle different SDK response shapes
    text = getattr(resp, "text", None)
    if text:
        return text
    try:
        return resp.candidates[0].content.parts[0].text  # type: ignore[attr-defined]
    except Exception as exc:  # pragma: no cover
        raise ProviderError(f"Gemini response missing text: {exc}") from exc


def _guess_mime_and_data(name: str | None, payload: bytes | str) -> Tuple[str, bytes]:
    """Best-effort MIME detection and bytes conversion for attachments."""
    if isinstance(payload, bytes):
        mime = None
        if name:
            mime = mimetypes.guess_type(name)[0]
        return mime or "application/octet-stream", payload
    data = str(payload).encode("utf-8")
    return "text/plain", data


def _is_uri_string(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    lower = value.strip().lower()
    return lower.startswith("gs://") or lower.startswith("http://") or lower.startswith("https://")


def _guess_mime_from_name_or_uri(name: str | None, uri_or_name: str) -> str:
    mime = None
    target = uri_or_name
    if name:
        target = name
    mime = mimetypes.guess_type(target)[0]
    if mime:
        return mime
    # Fallbacks for common cases
    if target.lower().endswith(".pdf"):
        return "application/pdf"
    return "application/octet-stream"


class GeminiProvider(ModelProvider):
    def __init__(self, project: str | None = None, location: str | None = None) -> None:
        self.project = project
        self.location = location
        self.last_usage: dict | None = None
        self.last_model: str | None = None

    def generate_structured(
        self,
        prompt: str,
        schema: InternalSchema | None,
        options: ProviderOptions | None = None,
        system_instruction: str | None = None,
        attachments: list[tuple[str, bytes | str]] | None = None,
    ) -> dict:
        opts = ProviderOptions(
            model_name=config.DEFAULT_MODEL_NAME,
            temperature=config.DEFAULT_TEMPERATURE,
            max_output_tokens=config.DEFAULT_MAX_OUTPUT_TOKENS,
        ).merged(options)

        model_name = opts.model_name or config.DEFAULT_MODEL_NAME
        self.last_model = model_name

        try:
            from vertexai import init  # type: ignore
            from vertexai.generative_models import GenerationConfig, GenerativeModel  # type: ignore
            from vertexai.generative_models import Part  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise ProviderError(
                "google-cloud-aiplatform is required for GeminiProvider"
            ) from exc

        try:
            init(project=self.project, location=self.location)
            gen_model = GenerativeModel(model_name)
            cfg_kwargs: Dict[str, Any] = {
                "response_mime_type": "application/json",
                "temperature": opts.temperature,
                "top_p": opts.top_p,
                "max_output_tokens": opts.max_output_tokens,
            }
            if schema is not None:
                raw_schema = _internal_to_json_schema(schema)
                cfg_kwargs["response_schema"] = normalize_for_vertex_schema(raw_schema)
            gen_cfg = GenerationConfig(**cfg_kwargs)
            contents: list[Any] = []
            if system_instruction:
                contents.append(system_instruction)
            contents.append(prompt or "")
            attach_strategy = (options.attachment_strategy if options else None) or "bytes"
            for name, payload in attachments or []:
                # When strategy is URI and payload is a URI-like string, use Part.from_uri
                if attach_strategy == "uri" and _is_uri_string(payload):
                    mime = _guess_mime_from_name_or_uri(name, str(payload))
                    try:
                        contents.append(Part.from_uri(str(payload), mime_type=mime))
                        continue
                    except Exception:
                        # Fall back to data if URI not supported
                        pass
                # Default: attach bytes/text
                mime, data = _guess_mime_and_data(name, payload)
                try:
                    contents.append(Part.from_data(mime_type=mime, data=data))
                except Exception:
                    contents.append(Part.from_data(mime_type="application/octet-stream", data=data))
            resp = gen_model.generate_content(contents, generation_config=gen_cfg)
            payload = _extract_text(resp)
            data = json.loads(payload)
            usage_meta = getattr(resp, "usage_metadata", None)
            usage: dict | None = None
            if usage_meta:
                usage = {
                    "input_tokens": getattr(usage_meta, "prompt_token_count", None),
                    "output_tokens": getattr(usage_meta, "candidates_token_count", None),
                }
            self.last_usage = usage
            return data
        except ProviderError:
            raise
        except Exception as exc:  # pragma: no cover
            raise ProviderError(f"Gemini call failed: {exc}") from exc
