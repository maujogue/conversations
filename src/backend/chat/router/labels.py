"""Structured objects shared by the router classifier and the routing step."""

from pydantic import BaseModel, ConfigDict, Field

from chat.enums import (
    ReasoningEffort,
    RoutingDomain,
    RoutingReason,
    RoutingTask,
    RoutingTier,
    TierSource,
)


class RoutingLabels(BaseModel):
    """Output of the classifier: one structured-output call on the router model."""

    model_config = ConfigDict(use_enum_values=False)

    complexity: RoutingTier = Field(
        description="Complexity tier of the user's request: simple, standard or complex."
    )
    domain: RoutingDomain = Field(description="Domain the request belongs to.")
    task: RoutingTask = Field(description="Kind of task the user is asking for.")
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence in the complexity label, between 0 and 1.",
    )


class RoutingDecision(BaseModel):
    """Everything the routing step decides for one turn (spec 5.5).

    Produced by the routing step (not by this module) and recorded on the
    assistant message metadata and as Langfuse trace tags.
    """

    tier: RoutingTier
    tier_source: TierSource
    model_hrid: str
    reasoning_effort: ReasoningEffort | None = None
    labels: RoutingLabels | None = None
    reason: RoutingReason
    router_would_pick: RoutingTier | None = None
    router_confidence: float | None = None
    router_latency_ms: int = 0
    router_prompt_version: str | None = None
    router_model_hrid: str | None = None
    # Model of the previous turn (``conversation.model_hrid`` before this decision
    # was applied), so the stream can tell the user the model changed.
    previous_model_hrid: str | None = None

    @property
    def changed(self) -> bool:
        """Whether this turn runs on a different model than the previous one."""
        return bool(self.previous_model_hrid) and self.previous_model_hrid != self.model_hrid

    def metadata(self) -> dict:
        """Routing keys recorded on the assistant message and the arena comparison (spec 5.5)."""
        return {
            "tier": self.tier.value,
            "tier_source": self.tier_source.value,
            "domain": self.labels.domain.value if self.labels else None,
            "task": self.labels.task.value if self.labels else None,
            "router_reason": self.reason.value,
            "router_confidence": self.router_confidence,
            "router_would_pick": self.router_would_pick.value if self.router_would_pick else None,
            "reasoning_effort": self.reasoning_effort.value if self.reasoning_effort else None,
            "router_latency_ms": self.router_latency_ms,
            "router_prompt_version": self.router_prompt_version,
        }
