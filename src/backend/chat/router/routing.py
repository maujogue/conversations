"""Routing step: turn the classifier labels into a model for the turn (spec 5).

```
labels = classify(message, previous_labels, context)
tier   = pinned_tier or (labels.complexity if labels.confidence >= threshold else "standard")
tier   = max(tier, minimum_tier_for(constraints))     # image, context length, web search
model  = resolve(tier)  ->  health cascade (chat/model_routing.py)
effort = effort_for(tier, labels, pinned_tier)
```
"""

import dataclasses
import json
import logging

from django.conf import settings
from django.core.cache import cache

from asgiref.sync import sync_to_async

from chat import models
from chat.ai_sdk_types import FileUIPart, TextUIPart, UIMessage
from chat.arena import message_has_image
from chat.constants import IMAGE_MIME_PREFIX
from chat.enums import (
    ReasoningEffort,
    RoutingReason,
    RoutingTask,
    RoutingTier,
    TierSource,
)
from chat.llm_configuration import LLModel
from chat.model_routing import first_healthy

from .classifier import PREVIOUS_ANSWER_MAX_CHARS, classify, router_model_hrid
from .labels import RoutingDecision, RoutingLabels

logger = logging.getLogger(__name__)

TIER_ORDER = [RoutingTier.SIMPLE, RoutingTier.STANDARD, RoutingTier.COMPLEX]

# Tasks that always run tier 3 at high effort (spec 3.2).
HIGH_EFFORT_TASKS = frozenset(
    {RoutingTask.REASONING, RoutingTask.DATA_ANALYSIS, RoutingTask.CODING}
)

# Rough tokens-per-character ratio for the context-length constraint.
CHARS_PER_TOKEN = 4


def constraint_fallback_key(tier: str) -> str:
    """Cache key of the admin counter of turns no tier model could serve."""
    return f"routing:constraint_fallback:{tier}"


def _count_constraint_fallback(tier: str) -> None:
    key = constraint_fallback_key(tier)
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=None)


@dataclasses.dataclass(frozen=True)
class TurnConstraints:
    """Hard capabilities the model of this turn must have (spec 2, "never route below")."""

    needs_image: bool = False
    needs_web_search: bool = False
    context_tokens: int = 0

    def satisfied_by(self, configuration: LLModel | None) -> bool:
        """Whether an active configured model can serve this turn."""
        if configuration is None or not configuration.is_active:
            return False
        if self.needs_image and not configuration.supports_image:
            return False
        if self.needs_web_search and not configuration.web_search:
            return False
        if (
            configuration.max_token_context is not None
            and self.context_tokens > configuration.max_token_context
        ):
            return False
        return True


# --------------------------------------------------------------------------- #
# Conversation helpers (what the previous turn left behind)
# --------------------------------------------------------------------------- #


def message_text(message: UIMessage) -> str:
    """Plain text of a UI message: its text parts, else its legacy ``content``."""
    texts = [part.text for part in message.parts or [] if isinstance(part, TextUIPart)]
    if texts:
        return "\n".join(texts)
    return message.content or ""


def message_has_file(message: UIMessage | None) -> bool:
    """Whether the user message carries any file attachment."""
    if message is None:
        return False
    return any(isinstance(part, FileUIPart) for part in message.parts or [])


def previous_labels_from(conversation: models.ChatConversation) -> RoutingLabels | None:
    """Labels recorded by the previous turn's routing decision, if any."""
    raw = (conversation.last_routing or {}).get("labels")
    if not raw:
        return None
    try:
        return RoutingLabels.model_validate(raw)
    except ValueError:
        logger.debug("Ignoring unreadable previous routing labels", exc_info=True)
        return None


def last_answer_excerpt(conversation: models.ChatConversation) -> str | None:
    """Tail of the last assistant answer sent back by the frontend (``ui_messages``)."""
    for raw in reversed(conversation.ui_messages or []):
        if not isinstance(raw, dict) or raw.get("role") != "assistant":
            continue
        texts = [
            part.get("text", "")
            for part in raw.get("parts") or []
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        text = "\n".join(texts) if texts else (raw.get("content") or "")
        return text[-PREVIOUS_ANSWER_MAX_CHARS:] or None
    return None


def estimate_history_tokens(conversation: models.ChatConversation, message: UIMessage) -> int:
    """Rough token count of what the model will read: stored history plus this message."""
    history_chars = len(json.dumps(conversation.pydantic_messages or [], ensure_ascii=False))
    history_chars += len(conversation.history_summary or "")
    return (history_chars + len(message_text(message))) // CHARS_PER_TOKEN


def _conversation_has_image(conversation: models.ChatConversation) -> bool:
    return conversation.attachments.filter(
        content_type__startswith=IMAGE_MIME_PREFIX,
        upload_state=models.AttachmentStatus.READY,
    ).exists()


# --------------------------------------------------------------------------- #
# Tier and model resolution
# --------------------------------------------------------------------------- #


def tier_from_labels(labels: RoutingLabels, threshold: float) -> RoutingTier:
    """Tiers 1 and 3 are chosen only at or above the confidence threshold (spec 4.3)."""
    if labels.complexity == RoutingTier.STANDARD or labels.confidence >= threshold:
        return labels.complexity
    return RoutingTier.STANDARD


def tier_of_model(tier_settings: models.RoutingTierSettings, model_hrid: str) -> RoutingTier | None:
    """Lowest tier whose model or alternatives include ``model_hrid``."""
    for tier in TIER_ORDER:
        if model_hrid in tier_settings.all_models_for(tier):
            return tier
    return None


def _fitting_models(
    tier_settings: models.RoutingTierSettings, tier: RoutingTier, constraints: TurnConstraints
) -> list[str]:
    return [
        hrid
        for hrid in tier_settings.all_models_for(tier)
        if constraints.satisfied_by(settings.LLM_CONFIGURATIONS.get(hrid))
    ]


def resolve_model(
    tier_settings: models.RoutingTierSettings, tier: RoutingTier, constraints: TurnConstraints
) -> tuple[RoutingTier, str, bool]:
    """Walk up from ``tier`` to the first tier with a model that fits, then cascade on health.

    Returns ``(tier, model_hrid, fell_back)``. ``fell_back`` is True when no tier
    model fits and the default model is used (reason ``constraint_fallback``).
    """
    for candidate_tier in TIER_ORDER[TIER_ORDER.index(tier) :]:
        candidates = _fitting_models(tier_settings, candidate_tier, constraints)
        if not candidates:
            continue
        # The existing fallback settings remain the health cascade for each tier.
        for fb_hrid in (settings.LLM_FALLBACK_MODEL_HRID_1, settings.LLM_FALLBACK_MODEL_HRID_2):
            if (
                fb_hrid
                and fb_hrid not in candidates
                and constraints.satisfied_by(settings.LLM_CONFIGURATIONS.get(fb_hrid))
            ):
                candidates.append(fb_hrid)
        return candidate_tier, first_healthy(candidates), False

    _count_constraint_fallback(tier.value)
    logger.warning(
        "No tier model satisfies the turn constraints %s; using the default", constraints
    )
    return tier, settings.LLM_DEFAULT_MODEL_HRID, True


def effort_for(
    configuration: LLModel,
    tier: RoutingTier,
    labels: RoutingLabels | None,
    pinned: bool,
    high_effort_threshold: float,
) -> ReasoningEffort | None:
    """Reasoning effort for the turn (spec 3.2). Only models with levels take one.

    Toggle models (deepseek) run with thinking on, which is their default: None.
    """
    if configuration.reasoning_control != "levels" or tier != RoutingTier.COMPLEX:
        return None
    if pinned:
        return ReasoningEffort.HIGH
    if labels is not None and (
        labels.task in HIGH_EFFORT_TASKS or labels.confidence >= high_effort_threshold
    ):
        return ReasoningEffort.HIGH
    return ReasoningEffort.MEDIUM


async def route_turn(  # noqa: PLR0913  # pylint: disable=too-many-arguments,too-many-locals
    *,
    conversation: models.ChatConversation,
    user,  # pylint: disable=unused-argument  # kept for per-user routing knobs
    message: UIMessage,
    force_web_search: bool,
    has_attachments: bool,
    has_project_context: bool,
    requested_model_hrid: str | None,
    previous_labels: RoutingLabels | None,
    previous_answer_excerpt: str | None,
) -> RoutingDecision:
    """Decide the tier, model and reasoning effort of one turn (spec 5).

    A pinned tier (``conversation.pinned_tier``) bypasses the complexity decision
    but not the classifier: domain and task are still produced and the router's own
    choice is recorded as ``router_would_pick``. Constraints apply in every case.
    A ``requested_model_hrid`` (staff picker, validated by the view) is used as is.
    """
    tier_settings = await sync_to_async(models.RoutingTierSettings.get_solo)()

    labels, reason_value, latency_ms, prompt_version = await classify(
        message_text(message),
        previous_labels,
        previous_answer_excerpt,
        has_attachments,
        has_project_context,
        force_web_search,
    )
    reason = RoutingReason(reason_value)
    router_would_pick = tier_from_labels(labels, tier_settings.confidence_threshold)

    pinned_tier = RoutingTier(conversation.pinned_tier) if conversation.pinned_tier else None
    if pinned_tier is not None:
        tier, tier_source, reason = pinned_tier, TierSource.USER, RoutingReason.USER_PINNED
    else:
        tier, tier_source = router_would_pick, TierSource.ROUTER

    common = {
        "labels": labels,
        "router_would_pick": router_would_pick,
        "router_confidence": labels.confidence,
        "router_latency_ms": latency_ms,
        "router_prompt_version": prompt_version,
        "router_model_hrid": tier_settings.router_model_hrid or router_model_hrid(),
        "previous_model_hrid": conversation.model_hrid or None,
    }

    if requested_model_hrid:
        configuration = settings.LLM_CONFIGURATIONS[requested_model_hrid]
        tier = tier_of_model(tier_settings, requested_model_hrid) or tier
        return RoutingDecision(
            tier=tier,
            tier_source=TierSource.USER,
            model_hrid=requested_model_hrid,
            reasoning_effort=effort_for(
                configuration, tier, labels, True, tier_settings.high_effort_threshold
            ),
            reason=RoutingReason.USER_PINNED,
            **common,
        )

    constraints = TurnConstraints(
        needs_image=message_has_image(message)
        or await sync_to_async(_conversation_has_image)(conversation),
        needs_web_search=force_web_search,
        context_tokens=estimate_history_tokens(conversation, message),
    )
    # ``resolve_model`` ends on the health cascade, which reads the model-health
    # singleton from the database, so it must not run on the event loop.
    resolved_tier, model_hrid, fell_back = await sync_to_async(resolve_model)(
        tier_settings, tier, constraints
    )
    if fell_back:
        tier_source, reason = TierSource.CONSTRAINT, RoutingReason.CONSTRAINT_FALLBACK
    elif resolved_tier != tier:
        tier, tier_source, reason = resolved_tier, TierSource.CONSTRAINT, RoutingReason.CONSTRAINT

    configuration = settings.LLM_CONFIGURATIONS[model_hrid]
    decision = RoutingDecision(
        tier=tier,
        tier_source=tier_source,
        model_hrid=model_hrid,
        reasoning_effort=effort_for(
            configuration,
            tier,
            labels,
            pinned_tier is not None,
            tier_settings.high_effort_threshold,
        ),
        reason=reason,
        **common,
    )
    logger.debug(
        "Routed turn tier=%s source=%s model=%s reason=%s effort=%s",
        decision.tier.value,
        decision.tier_source.value,
        decision.model_hrid,
        decision.reason.value,
        decision.reasoning_effort,
    )
    return decision


def last_routing_payload(decision: RoutingDecision) -> dict:
    """What ``ChatConversation.last_routing`` keeps for the next turn's hint."""
    return {
        "tier": decision.tier.value,
        "model_hrid": decision.model_hrid,
        "labels": decision.labels.model_dump(mode="json") if decision.labels else None,
    }
