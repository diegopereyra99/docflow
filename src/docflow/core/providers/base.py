"""Provider abstraction."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..models.schema_defs import InternalSchema


@dataclass
class ProviderOptions:
    model_name: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int | None = None
    # Attachment strategy:
    # - None or "bytes": load and attach file bytes (current default)
    # - "uri": pass URIs (gs://, https://, http://) directly to provider when possible
    attachment_strategy: str | None = None

    def merged(self, override: "ProviderOptions | None") -> "ProviderOptions":
        if override is None:
            return self
        return ProviderOptions(
            model_name=override.model_name or self.model_name,
            temperature=self.temperature if override.temperature is None else override.temperature,
            top_p=self.top_p if override.top_p is None else override.top_p,
            max_output_tokens=self.max_output_tokens
            if override.max_output_tokens is None
            else override.max_output_tokens,
            attachment_strategy=override.attachment_strategy or self.attachment_strategy,
        )


class ModelProvider(Protocol):
    last_usage: dict | None
    last_model: str | None

    def generate_structured(
        self,
        prompt: str,
        schema: InternalSchema,
        options: ProviderOptions | None = None,
        system_instruction: str | None = None,
        attachments: list[tuple[str, bytes | str]] | None = None,
    ) -> dict:
        ...
