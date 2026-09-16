"""
Util classes for  main agent.
"""

import dataclasses
from typing import Dict, List, Optional

from django.contrib.auth import get_user_model

from chat import models
from chat.ai_sdk_types import SourceUrlUIPart

User = get_user_model()


@dataclasses.dataclass
class DocumentParsingResult:
    """Result marker for document parsing completion."""

    success: bool
    has_documents: bool


@dataclasses.dataclass
class PreparedHistory:
    """Result marker carrying the prepared (trimmed) history out of the summary phase."""

    history: List


@dataclasses.dataclass
class StreamingState:
    """
    Mutable state shared across stream processing handlers.

    This dataclass is passed through the node handlers to track state that
    needs to persist across multiple yield points in the streaming process.

    Attributes:
        tool_is_streaming: Set to True when we receive ToolCallPartDelta events.
            When True, we skip emitting ToolCallPart in _handle_call_tools_node
            since the tool call was already streamed incrementally.
        ui_sources: Accumulates source citations from tool results (e.g., URLs
            from web search or RAG). These are appended to the final UI message.
        model_response_message_id: Set when the agent reaches the end node.
            Used to link the UI message to the Langfuse trace for scoring.
    """

    tool_is_streaming: bool = False
    ui_sources: List[SourceUrlUIPart] = dataclasses.field(default_factory=list)
    model_response_message_id: Optional[str] = None
    # Monotonic timestamps of the first reasoning delta and the first text delta,
    # used to measure how long a reasoning model thought before answering.
    first_thinking_at: Optional[float] = None
    first_text_at: Optional[float] = None

    def note_thinking(self, now: float) -> None:
        """Record the first streamed reasoning delta."""
        if self.first_thinking_at is None:
            self.first_thinking_at = now

    def note_text(self, now: float) -> None:
        """Record the first streamed text delta."""
        if self.first_text_at is None:
            self.first_text_at = now

    def reasoning_seconds(self, now: float) -> float | None:
        """Seconds between the first reasoning delta and the first text delta.

        None when the model did not reason. When it reasoned but never produced
        text, the time up to ``now`` is returned.
        """
        if self.first_thinking_at is None:
            return None
        end = self.first_text_at if self.first_text_at is not None else now
        return round(max(end - self.first_thinking_at, 0.0), 1)


@dataclasses.dataclass
class TurnMetrics:
    """Per-turn figures measured on the stream and recorded with the answer (spec 5.5, 10)."""

    reasoning_tokens: int = 0
    reasoning_seconds: Optional[float] = None
    co2_source: Optional[str] = None

    def as_metadata(self) -> Dict:
        """Keys added to the assistant message metadata."""
        return {
            "reasoning_tokens": self.reasoning_tokens,
            "reasoning_seconds": self.reasoning_seconds,
            "co2_source": self.co2_source,
        }


@dataclasses.dataclass
class ContextDeps:
    """Dependencies for context management."""

    conversation: models.ChatConversation
    user: User
    session: Optional[Dict] = None
    web_search_enabled: bool = False


@dataclasses.dataclass
class ImagePostRunActions:
    """Per-turn instructions for image-URL surgery on the persisted messages.

    Two orthogonal concerns flow through here:

    - ``rewrite``: presigned URL -> durable storage form. Conversation
      attachments come in as local ``/media-key/...`` URLs from the frontend,
      get presigned for the LLM by ``update_local_urls``, and must be
      rewritten back to the local form before the turn is persisted to
      ``pydantic_messages`` (otherwise reload would carry an expired URL).
    - ``drop``: presigned URLs that must NOT be persisted at all. Project
      image pins live here: they are re-derived fresh from
      ``project.attachments`` on every turn, so persisting them would
      duplicate one image per turn in history and break playback once the
      presigned URL expires.

    The two are kept as separate fields (rather than a single dict with a
    sentinel value) so each callsite reads as the action it represents.
    """

    rewrite: Dict[str, str] = dataclasses.field(default_factory=dict)
    drop: set = dataclasses.field(default_factory=set)
