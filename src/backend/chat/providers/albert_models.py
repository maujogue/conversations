"""Custom pydantic-ai model subclasses for Albert API providers."""

from dataclasses import dataclass, field
from typing import Any, Iterable

from openai.types import chat
from openai.types.chat import chat_completion_chunk
from pydantic_ai.messages import ModelResponseStreamEvent
from pydantic_ai.models.openai import (
    ChatCompletionChunk,
    OpenAIChatModel,
    OpenAIStreamedResponse,
    _ChatCompletion,
)
from pydantic_ai.providers.openai import OpenAIProvider

# Streamed reasoning fields on OpenAI-compatible chunks: `reasoning` (gpt-oss on
# Albert, OpenRouter, Ollama) and `reasoning_content` (DeepSeek, Moonshot).
REASONING_DELTA_FIELDS = ("reasoning", "reasoning_content")


def _extract_co2_impact(raw_usage) -> float | None:
    """Extract impact fields from an Albert API usage object.

    Albert returns `impacts` as an extra field (openai SDK uses extra='allow'),
    so it lives in model_extra, not as a direct attribute.
    """
    model_extra = getattr(raw_usage, "model_extra", None) or {}
    raw_impacts = model_extra.get("impacts")
    if not raw_impacts:
        return None

    return raw_impacts.get("kgCO2eq")


def _convert_impact_to_factor_20(impact_kg_co2_eq: float) -> int:
    """Convert a CO2 impact in kg to an integer factor with 20 decimals (for precision).
    This allows us to store the impact as an integer in ModelResponse.details
    while preserving precision.
    Values below 1e-20 would return 0
    """
    return int(impact_kg_co2_eq * 10**20)


class AlbertOpenAIProvider(OpenAIProvider):
    """OpenAIProvider subclass with a distinct name for Albert's OpenAI-compatible API."""

    @property
    def name(self) -> str:
        return "albert_openai"


def _extract_reasoning_delta(delta: Any) -> str:
    """Return the reasoning text carried by a streamed delta, or an empty string.

    Albert streams gpt-oss reasoning in `delta.reasoning` and DeepSeek-style
    models in `delta.reasoning_content`. Both are extra fields on the openai
    SDK's ChoiceDelta (extra='allow'): pydantic exposes them as attributes and
    in `model_extra`, so check both ways.
    """
    if delta is None:
        return ""
    model_extra = getattr(delta, "model_extra", None) or {}
    for field_name in REASONING_DELTA_FIELDS:
        value = getattr(delta, field_name, None)
        if value is None:
            value = model_extra.get(field_name)
        if isinstance(value, str) and value:
            return value
    return ""


@dataclass
class AlbertOpenAIStreamedResponse(OpenAIStreamedResponse):
    """Streamed response that preserves Albert's carbon/impacts usage fields.

    It also counts the reasoning text streamed in `delta.reasoning` /
    `delta.reasoning_content`: Albert's `usage.completion_tokens` (and hence its
    CO2 figure) excludes reasoning tokens, so the length is stored in
    `usage.details["reasoning_chars"]` for chat.footprint to estimate them
    (`reasoning_tokens_from_details`). When the provider does report
    `completion_tokens_details.reasoning_tokens`, it is kept in
    `details["reasoning_tokens"]` and takes precedence.
    """

    # Characters of reasoning seen so far, and how many were already flushed
    # into usage details, so the count is only reported once per usage chunk.
    _reasoning_chars: int = field(default=0, init=False)
    _reasoning_chars_reported: int = field(default=0, init=False)

    def _map_thinking_delta(
        self, choice: chat_completion_chunk.Choice
    ) -> Iterable[ModelResponseStreamEvent]:
        """Accumulate streamed reasoning length before the parent maps it to events."""
        reasoning = _extract_reasoning_delta(choice.delta)
        if reasoning:
            self._reasoning_chars = getattr(self, "_reasoning_chars", 0) + len(reasoning)
        yield from super()._map_thinking_delta(choice)

    def _map_usage(self, response: ChatCompletionChunk) -> Any:
        """Override to extract Albert's carbon impact data and reasoning counts from usage."""
        result = super()._map_usage(response)

        if response.usage:
            co2_impact = _extract_co2_impact(response.usage)
            if co2_impact:
                result.details["co2_impact_factor_20"] = _convert_impact_to_factor_20(co2_impact)

            completion_details = getattr(response.usage, "completion_tokens_details", None)
            reasoning_tokens = getattr(completion_details, "reasoning_tokens", None)
            if isinstance(reasoning_tokens, int) and reasoning_tokens > 0:
                result.details["reasoning_tokens"] = reasoning_tokens

            # Usage chunks are cumulated by pydantic-ai (RunUsage += RequestUsage),
            # so only report the characters not yet flushed.
            seen = getattr(self, "_reasoning_chars", 0)
            reported = getattr(self, "_reasoning_chars_reported", 0)
            if seen > reported:
                result.details["reasoning_chars"] = seen - reported
                self._reasoning_chars_reported = seen
        return result


def _normalize_tool_call_types(choices: list) -> None:
    """Coerce non-conforming tool_call `type` values to 'function' in place.

    'custom' is only valid with a `custom` payload; any other type (including
    'custom' with a function payload) must be 'function' to pass the openai SDK
    union validation.
    """
    for choice in choices:
        for tool_call in (choice.get("message") or {}).get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            is_custom = tool_call.get("type") == "custom" and "custom" in tool_call
            if not is_custom and tool_call.get("type") != "function":
                tool_call["type"] = "function"


class AlbertOpenAIChatModel(OpenAIChatModel):
    """
    OpenAIChatModel subclass that preserves Albert's carbon impact data.

    Albert's API returns `impacts` inside `usage` that pydantic-ai normally discards.
    This subclass captures the CO2 value and stores it as an integer (scaled by 10^20
    for precision) in RequestUsage.details["co2_impact_factor_20"], which pydantic-ai
    then accumulates into RunUsage.details across the run.

    Note: Albert sends CO2 data only in the final usage chunk. If this changes and
    partial CO2 values appear in intermediate chunks, RunUsage will sum them, which
    would produce incorrect totals.
    """

    @property
    def _streamed_response_cls(self) -> type[OpenAIStreamedResponse]:
        return AlbertOpenAIStreamedResponse

    def _validate_completion(self, response: chat.ChatCompletion) -> _ChatCompletion:
        """Normalize Albert API quirks before validation.

        Albert's OpenAI-compatible API has two known non-conformances:
        1. tool_calls[].type may not be 'function' — normalized to 'function',
           unless the tool call is a genuine custom tool call (type='custom'
           with a `custom` payload, which the openai SDK requires).
        2. On multi-turn tool-call conversations, the second response sometimes
           returns a non-standard `object` value. This is normalized before
           passing to _ChatCompletion.model_validate().
        """
        data = response.model_dump()

        if data.get("object") != "chat.completion":
            data["object"] = "chat.completion"

        # Only normalize tool-call types when `choices` is a genuine list. A
        # malformed non-list value is left untouched so model_validate() raises a
        # clear ValidationError here, rather than being coerced to `[]` (which
        # passes validation and then makes pydantic-ai raise IndexError on
        # response.choices[0]).
        choices = data.get("choices")
        if isinstance(choices, list):
            _normalize_tool_call_types(choices)

        return _ChatCompletion.model_validate(data)
