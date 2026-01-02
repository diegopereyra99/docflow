"""Extraction orchestrator."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Sequence, Tuple

from .. import config
from ..errors import DocumentError, ExtractionError
from ..models.documents import DocSource, load_content, GcsSource, HttpSource, FileSource, RawTextSource
from ..models.profiles import ExtractionProfile
from ..models.schema_defs import InternalSchema, normalize_output, parse_schema, validate_output
from ..providers.base import ModelProvider, ProviderOptions
from ..providers.gemini import GeminiProvider


@dataclass
class ExtractionResult:
    data: dict
    meta: dict

    def to_dict(self) -> dict:
        return {"data": self.data, "meta": self.meta}


@dataclass
class MultiResult:
    per_file: List[ExtractionResult]
    aggregate: ExtractionResult | None = None

    def to_dict(self) -> dict:
        return {
            "per_file": [r.to_dict() for r in self.per_file],
            "aggregate": self.aggregate.to_dict() if self.aggregate else None,
        }


# --- internal helpers ---

def _resolve_schema(schema: InternalSchema | dict | None, profile: ExtractionProfile | None) -> InternalSchema | None:
    candidate = None
    if profile and profile.schema is not None:
        candidate = profile.schema
    elif isinstance(schema, dict):
        candidate = parse_schema(schema)
    elif isinstance(schema, InternalSchema):
        candidate = schema

    return candidate


def _resolve_multi_mode(profile: ExtractionProfile | None, multi_mode: str | None) -> str:
    mode = multi_mode or (profile.multi_mode_default if profile else None) or config.DEFAULT_MULTI_MODE
    if mode not in {"per_file", "aggregate", "both"}:
        raise ExtractionError(f"Invalid multi-mode: {mode}")
    return mode


def _merge_options(profile: ExtractionProfile | None, options: ProviderOptions | None) -> ProviderOptions | None:
    base = ProviderOptions(
        model_name=config.DEFAULT_MODEL_NAME,
        temperature=config.DEFAULT_TEMPERATURE,
        max_output_tokens=config.DEFAULT_MAX_OUTPUT_TOKENS,
    )
    if profile and profile.provider_options:
        base = base.merged(profile.provider_options)
    if options:
        base = base.merged(options)
    return base


def _build_prompt(profile: ExtractionProfile | None, aggregate: bool) -> tuple[str, str]:
    """Compose minimal prompt and system instruction from profile."""
    # System instruction: prefer profile value, otherwise safe default
    system_instruction = (
        profile.system_instruction
        if profile and profile.system_instruction
        else "Return JSON that matches the provided schema. Use null for missing values. Do not add extra text."
    )

    lines: List[str] = []
    if profile and profile.prompt:
        lines.append(profile.prompt)
    elif profile and profile.description:
        lines.append(profile.description)
    else:
        if profile and profile.mode == "describe":
            lines.append("Provide a concise description of the document content.")
        elif profile and profile.name == "extract_all":
            lines.append("Extract all salient structured data you can find.")
        else:
            lines.append("Extract the requested structured fields. Use null for missing values.")

    if aggregate:
        lines.append("Multiple documents provided.")

    prompt = "\n\n".join(lines)
    return prompt, system_instruction


def _provider_or_default(provider: ModelProvider | None) -> ModelProvider:
    return provider if provider is not None else GeminiProvider()


def _single_call(
    provider: ModelProvider,
    prompt: str,
    internal_schema: InternalSchema | None,
    options: ProviderOptions | None,
    doc_names: List[str],
    mode: str,
    profile: ExtractionProfile | None,
    system_instruction: str,
    attachments: list[tuple[str, bytes | str]],
    repair_attempts: int = 0,
) -> ExtractionResult:
    data = provider.generate_structured(
        prompt=prompt,
        schema=internal_schema,
        options=options,
        system_instruction=system_instruction,
        attachments=attachments,
    )
    if internal_schema is not None:
        try:
            validate_output(internal_schema, data)
            payload = normalize_output(internal_schema, data)
        except Exception as exc:
            if repair_attempts and repair_attempts > 0:
                # Minimal repair loop: ask the model to fix invalid JSON against schema.
                from json import dumps

                last = data
                error_msg = str(exc)
                for _ in range(max(1, repair_attempts)):
                    repair_prompt = (
                        "You will be given JSON that failed validation against the target schema. "
                        "Return a corrected JSON that satisfies the schema. Do not include any explanation.\n\n"
                        f"Validation error:\n{error_msg}\n\nOriginal JSON:\n{dumps(last, indent=2)}\n\n"
                        "Return only valid JSON."
                    )
                    repaired = provider.generate_structured(
                        prompt=repair_prompt,
                        schema=internal_schema,
                        options=options,
                        system_instruction=system_instruction,
                        attachments=[],
                    )
                    try:
                        validate_output(internal_schema, repaired)
                        payload = normalize_output(internal_schema, repaired)
                        break
                    except Exception as repair_exc:
                        last = repaired
                        error_msg = str(repair_exc)
                else:
                    # If loop didn't break, re-raise original error
                    raise
            else:
                raise
    else:
        payload = data
    meta = {
        "model": getattr(provider, "last_model", None) or (options.model_name if options else None),
        "usage": getattr(provider, "last_usage", None),
        "docs": doc_names,
        "mode": mode,
        "profile": profile.name if profile else None,
    }
    return ExtractionResult(data=payload, meta=meta)


# --- public API ---

def extract(
    docs: List[DocSource],
    schema: InternalSchema | dict | None = None,
    profile: ExtractionProfile | None = None,
    provider: ModelProvider | None = None,
    options: ProviderOptions | None = None,
    multi_mode: str | None = None,
    repair_attempts: int = 0,
) -> ExtractionResult | List[ExtractionResult] | MultiResult:
    if not docs:
        raise DocumentError("No documents provided")
    if len(docs) > config.MAX_DOCS_PER_EXTRACTION:
        raise ExtractionError("Too many documents for a single extraction")

    internal_schema = _resolve_schema(schema, profile)
    mode = _resolve_multi_mode(profile, multi_mode)
    eff_options = _merge_options(profile, options)
    provider_inst = _provider_or_default(provider)

    # Prepare attachments according to attachment strategy
    attach_strategy = (eff_options.attachment_strategy if eff_options else None) or "bytes"
    loaded_docs: List[tuple[str, bytes | str]] = []
    for doc in docs:
        name = doc.display_name()
        if attach_strategy == "uri" and isinstance(doc, (GcsSource, HttpSource)):
            # Pass URI without loading bytes
            uri = doc.uri if isinstance(doc, GcsSource) else doc.url  # type: ignore[attr-defined]
            loaded_docs.append((name, uri))
        else:
            content = load_content(doc)
            loaded_docs.append((name, content))
    attachments = loaded_docs

    results: List[ExtractionResult] = []

    if mode in {"per_file", "both"}:
        for name, content in loaded_docs:
            prompt, sys_inst = _build_prompt(profile, aggregate=False)
            results.append(
                _single_call(
                    provider_inst,
                    prompt,
                    internal_schema,
                    eff_options,
                    [name],
                    mode="per_file",
                    profile=profile,
                    system_instruction=sys_inst,
                    attachments=[(name, content)],
                    repair_attempts=repair_attempts,
                )
            )

    aggregate_result: ExtractionResult | None = None
    if mode in {"aggregate", "both"}:
        prompt, sys_inst = _build_prompt(profile, aggregate=True)
        aggregate_result = _single_call(
            provider_inst,
            prompt,
            internal_schema,
            eff_options,
            [name for name, _ in loaded_docs],
            mode="aggregate",
            profile=profile,
            system_instruction=sys_inst,
            attachments=attachments,
            repair_attempts=repair_attempts,
        )

    if mode == "per_file":
        return results
    if mode == "aggregate":
        return aggregate_result or ExtractionResult(data={}, meta={})
    return MultiResult(per_file=results, aggregate=aggregate_result)


# --- grouped extraction ---

@dataclass
class GroupedItemResult:
    group_id: str
    result: ExtractionResult


@dataclass
class GroupedResult:
    groups: List[GroupedItemResult]

    def to_dict(self) -> dict:
        return {"groups": [{"group_id": g.group_id, "result": g.result.to_dict()} for g in self.groups]}


def extract_grouped(
    docs_groups: Sequence[Tuple[str, Sequence[DocSource]]],
    schema: InternalSchema | dict | None = None,
    profile: ExtractionProfile | None = None,
    provider: ModelProvider | None = None,
    options: ProviderOptions | None = None,
    repair_attempts: int = 0,
) -> GroupedResult:
    if not docs_groups:
        raise DocumentError("No groups provided")
    total_docs = sum(len(g[1]) for g in docs_groups)
    if total_docs > config.MAX_DOCS_PER_EXTRACTION:
        raise ExtractionError("Too many documents for a single extraction")

    internal_schema = _resolve_schema(schema, profile)
    eff_options = _merge_options(profile, options)
    provider_inst = _provider_or_default(provider)

    items: List[GroupedItemResult] = []
    for group_id, docs in docs_groups:
        # Build attachments similar to aggregate mode
        attach_strategy = (eff_options.attachment_strategy if eff_options else None) or "bytes"
        attachments: List[tuple[str, bytes | str]] = []
        doc_names: List[str] = []
        for doc in docs:
            name = doc.display_name()
            doc_names.append(name)
            if attach_strategy == "uri" and isinstance(doc, (GcsSource, HttpSource)):
                uri = doc.uri if isinstance(doc, GcsSource) else doc.url  # type: ignore[attr-defined]
                attachments.append((name, uri))
            else:
                content = load_content(doc)
                attachments.append((name, content))

        prompt, sys_inst = _build_prompt(profile, aggregate=True)
        result = _single_call(
            provider_inst,
            prompt,
            internal_schema,
            eff_options,
            doc_names,
            mode="grouped",
            profile=profile,
            system_instruction=sys_inst,
            attachments=attachments,
            repair_attempts=repair_attempts,
        )
        items.append(GroupedItemResult(group_id=group_id, result=result))

    return GroupedResult(groups=items)
