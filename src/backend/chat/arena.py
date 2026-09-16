"""
Arena: blind champion-versus-challenger comparisons of LLM answers.

An arena turn streams two answers to the same user message, one from the model of
the turn's tier (the champion) and one from a challenger drawn from the active
``ArenaExperiment`` of that tier. The user picks one; the pick is stored on an
``ArenaComparison`` and the chosen answer is committed to the conversation. A
second opinion asked for by the user (``origin=manual``) compares the answer
already in the history against one untried alternative of the same tier.
Model names are never shown to the user. See ``docs/arena-mvp-spec.md`` and
``docs/llm-router-spec.md`` sections 8.1 and 8.2.
"""

import logging
import random
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
from typing import Iterable

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from core.feature_flags.helpers import is_feature_enabled

from chat import models
from chat.ai_sdk_types import FileUIPart, UIMessage
from chat.arena_scores import push_comparison_scores
from chat.constants import IMAGE_MIME_PREFIX
from chat.enums import (
    ArenaComparisonStatus,
    ArenaContextTag,
    ArenaOrigin,
    ArenaRole,
    ArenaSide,
    ArenaVoteOutcome,
)
from chat.model_health import get_status_for_hrid
from chat.model_routing import resolve_effective_model_hrid
from chat.tools.self_documentation import anonymize_arena_documentation

logger = logging.getLogger(__name__)

# Tools whose execution leaves something behind outside the conversation (a generated
# file, a Docs edit). They are stripped from both models in arena mode so the losing
# answer never has side effects. ``generate_presentation`` is the only tool-based one
# today; edit-in-Docs is a separate endpoint, never reachable from a candidate stream.
SIDE_EFFECT_TOOL_NAMES = frozenset({"generate_presentation"})

RED = models.ModelHealth.Status.RED

# Share of an experiment's draws given to its control challenger (router spec 5.4).
CONTROL_DRAW_RATE = 0.05


MANUAL_EXPERIMENT_NAME = "Second opinion ({tier})"


class ArenaConflict(Exception):
    """Raised when a comparison is not in a state that accepts the requested action."""


def _refused_draws_key(experiment_id) -> str:
    return f"arena:refused_draws:{experiment_id}"


def get_refused_draws(experiment_id) -> int:
    """Number of draws refused because the conversation was not on the champion."""
    return int(cache.get(_refused_draws_key(experiment_id)) or 0)


def _count_refused_draw(experiment_id) -> None:
    key = _refused_draws_key(experiment_id)
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=None)


def get_active_experiment(tier: str | None = None) -> models.ArenaExperiment | None:
    """The active experiment of ``tier``, with its challengers prefetched.

    At most one experiment is active per tier (router spec 8.1). Without a tier
    the first active experiment is returned, which is what a turn that was not
    routed (router flag off) compares on.
    """
    queryset = models.ArenaExperiment.objects.filter(is_active=True).prefetch_related("challengers")
    if tier is not None:
        queryset = queryset.filter(tier=tier)
    return queryset.first()


def compute_context_tags(
    conversation: models.ChatConversation, force_web_search: bool
) -> list[str]:
    """Derive the context tags of a turn from the conversation state.

    ``web_search`` may also be added after the run when a model chose to search
    on its own; see ``add_context_tag``.
    """
    tags: list[str] = []
    if force_web_search:
        tags.append(ArenaContextTag.WEB_SEARCH)
    if conversation.attachments.filter(upload_state=models.AttachmentStatus.READY).exists():
        tags.append(ArenaContextTag.ATTACHMENT)
    if conversation.project_id:
        tags.append(ArenaContextTag.PROJECT)
    if not tags:
        tags.append(ArenaContextTag.PLAIN)
    return tags


def add_context_tag(comparison: models.ArenaComparison, tag: str) -> None:
    """Add a tag discovered during the run (drops ``plain`` since the turn had context)."""
    tags = [t for t in comparison.context_tags if t != ArenaContextTag.PLAIN]
    if tag not in tags:
        tags.append(tag)
    comparison.context_tags = tags


def message_has_image(message: UIMessage | None) -> bool:
    """Whether the user message carries at least one image attachment."""
    if message is None:
        return False
    return any(
        isinstance(part, FileUIPart) and (part.mediaType or "").startswith(IMAGE_MIME_PREFIX)
        for part in message.parts or []
    )


def _is_red(model_hrid: str) -> bool:
    return get_status_for_hrid(model_hrid) == RED


def _pin_conversation_to_champion(conversation: models.ChatConversation) -> None:
    """Pin the conversation the same way ``post_conversation`` does on a first message.

    Compare-and-set on the empty ``model_hrid`` so a concurrent first message and a
    draw agree on the pinned model.
    """
    if conversation.model_hrid:
        return
    resolved = resolve_effective_model_hrid(None)
    pinned = models.ChatConversation.objects.filter(pk=conversation.pk, model_hrid="").update(
        model_hrid=resolved
    )
    if pinned:
        conversation.model_hrid = resolved
    else:
        conversation.refresh_from_db(fields=["model_hrid"])


def _user_draws_today(experiment, user) -> int:
    since = timezone.now() - timedelta(days=1)
    return models.ArenaComparison.objects.filter(
        experiment=experiment, user=user, drawn_at__gte=since, is_seed=False
    ).count()


def _conversation_has_image(conversation: models.ChatConversation) -> bool:
    """Whether an image is attached to the conversation (uploads land before the send)."""
    return conversation.attachments.filter(
        content_type__startswith=IMAGE_MIME_PREFIX,
        upload_state=models.AttachmentStatus.READY,
    ).exists()


def turn_constraints(
    conversation: models.ChatConversation,
    last_message: UIMessage | None,
    force_web_search: bool = False,
):
    """Hard capabilities the models of this turn must have (router spec 5.1).

    Reuses the router's own ``TurnConstraints`` so a challenger is held to exactly
    the same bar as the tier model. Imported lazily: ``chat.router.routing`` imports
    this module for ``message_has_image``.
    """
    from chat.router.routing import (  # noqa: PLC0415  # pylint: disable=import-outside-toplevel,cyclic-import
        TurnConstraints,
        estimate_history_tokens,
    )

    return TurnConstraints(
        needs_image=message_has_image(last_message) or _conversation_has_image(conversation),
        needs_web_search=force_web_search,
        context_tokens=(estimate_history_tokens(conversation, last_message) if last_message else 0),
    )


def model_fits_turn(model_hrid: str, constraints) -> bool:
    """Whether a configured, healthy model can serve a turn with these constraints."""
    if _is_red(model_hrid):
        return False
    return constraints.satisfied_by(settings.LLM_CONFIGURATIONS.get(model_hrid))


def _eligible_challengers(
    experiment,
    conversation: models.ChatConversation,
    last_message: UIMessage | None,
    force_web_search: bool = False,
) -> tuple[list[str], list[str]]:
    """Challengers that are healthy and satisfy the turn's constraints.

    Returns ``(regular, control)``: the control challenger is drawn apart, on a
    fixed share of the draws, and never enters the scoreboard (router spec 5.4).
    """
    constraints = turn_constraints(conversation, last_message, force_web_search)
    regular: list[str] = []
    control: list[str] = []
    for challenger in experiment.challengers.all():
        if not model_fits_turn(challenger.model_hrid, constraints):
            continue
        (control if challenger.is_control else regular).append(challenger.model_hrid)
    return regular, control


def _pick_challenger(regular: list[str], control: list[str]) -> str | None:
    """The control challenger on ``CONTROL_DRAW_RATE`` of the draws, else a regular one."""
    if control and (not regular or random.random() < CONTROL_DRAW_RATE):  # noqa: S311
        return random.choice(control)  # noqa: S311
    if not regular:
        return None
    return random.choice(regular)  # noqa: S311


def _turn_index(conversation: models.ChatConversation) -> int:
    return sum(1 for message in conversation.messages if message.role == "user") + 1


def routing_fields(decision) -> dict:
    """Routing labels of a turn, as the flat columns of a comparison (router spec 8.1)."""
    if decision is None:
        return {}
    labels = decision.labels
    return {
        "tier": decision.tier.value,
        "tier_source": decision.tier_source.value,
        "domain": labels.domain.value if labels else "",
        "task": labels.task.value if labels else "",
        "router_confidence": decision.router_confidence,
        "router_reason": decision.reason.value,
        "router_model_hrid": decision.router_model_hrid or "",
        "router_prompt_version": decision.router_prompt_version or "",
    }


def effort_for_comparison(experiment, decision) -> str | None:
    """Reasoning effort both sides run at: the experiment's override, else the router's."""
    if experiment.reasoning_effort:
        return experiment.reasoning_effort
    if decision is not None and decision.reasoning_effort is not None:
        return decision.reasoning_effort.value
    return None


def _input_snapshot(conversation, last_message, force_web_search, reasoning_effort=None) -> dict:
    """The turn input both candidates read, frozen at draw time."""
    return {
        "messages": [m.model_dump(mode="json") for m in conversation.messages],
        "pydantic_messages": conversation.pydantic_messages,
        "history_summary": conversation.history_summary,
        "history_summary_checkpoint": conversation.history_summary_checkpoint,
        "request_ui_message": last_message.model_dump(mode="json") if last_message else None,
        "force_web_search": force_web_search,
        "reasoning_effort": reasoning_effort,
    }


@transaction.atomic
def draw_comparison(  # noqa: PLR0911  # pylint: disable=too-many-return-statements,too-many-arguments
    *,
    conversation: models.ChatConversation,
    user,
    force_web_search: bool,
    last_message: UIMessage | None = None,
    routing_decision=None,
) -> models.ArenaComparison | None:
    """Decide whether the next turn is an arena turn and, if so, create the comparison.

    Runs every eligibility rule of the spec. Returns ``None`` when the turn is a
    normal one. A pending comparison left on the conversation is resolved first, so
    a reload or a quick second message never leaves two comparisons open.

    ``routing_decision`` is the router's decision for this turn when the router is
    on: it selects the experiment (one per tier), its labels are stored on the
    comparison and its effort applies to both sides. Without it the single active
    experiment is used and the routing columns stay empty.
    """
    if not is_feature_enabled(user, "arena"):
        return None
    tier = routing_decision.tier.value if routing_decision is not None else None
    experiment = get_active_experiment(tier=tier)
    if experiment is None:
        return None

    # All turn transitions lock the conversation before the comparison.
    conversation = models.ChatConversation.objects.select_for_update().get(pk=conversation.pk)
    resolve_pending(conversation)
    champion_hrid = experiment.champion_model_hrid
    if routing_decision is None:
        _pin_conversation_to_champion(conversation)
        on_champion = conversation.model_hrid == champion_hrid
    else:
        # With the router on, the model is chosen per turn: the comparison only makes
        # sense when the turn actually landed on the tier model (no health cascade,
        # no constraint walk).
        on_champion = routing_decision.model_hrid == champion_hrid
    if not on_champion:
        _count_refused_draw(experiment.pk)
        return None
    if _is_red(champion_hrid):
        return None
    if random.random() >= experiment.sampling_rate:  # noqa: S311  # not security-sensitive
        return None
    if _user_draws_today(experiment, user) >= experiment.daily_cap_per_user:
        return None

    challenger_hrid = _pick_challenger(
        *_eligible_challengers(experiment, conversation, last_message, force_web_search)
    )
    if challenger_hrid is None:
        return None

    champion_side = random.choice([ArenaSide.LEFT, ArenaSide.RIGHT])  # noqa: S311

    comparison = models.ArenaComparison.objects.create(
        experiment=experiment,
        conversation=conversation,
        user=user,
        turn=_turn_index(conversation),
        context_tags=compute_context_tags(conversation, force_web_search),
        tools_stripped=is_feature_enabled(user, "presentation_generation"),
        champion_model_hrid=champion_hrid,
        challenger_model_hrid=challenger_hrid,
        champion_side=champion_side,
        conversation_version=conversation.arena_version,
        origin=ArenaOrigin.DRAW.value,
        **routing_fields(routing_decision),
        input_snapshot=_input_snapshot(
            conversation,
            last_message,
            force_web_search,
            effort_for_comparison(experiment, routing_decision),
        ),
    )
    logger.info(
        "Arena draw on conversation %s: %s vs %s (champion on the %s)",
        conversation.pk,
        comparison.champion_model_hrid,
        comparison.challenger_model_hrid,
        champion_side,
    )
    return comparison


# --------------------------------------------------------------------------- #
# Second opinion on demand (router spec 8.2)
# --------------------------------------------------------------------------- #


def _last_turn_pydantic_messages(conversation: models.ChatConversation) -> list:
    """The stored model messages of the last turn: its request and everything after.

    Used to describe the answer already in the history as a candidate payload, so a
    vote for the second opinion can swap the whole turn, not just the visible bubble.
    """
    history = list(conversation.pydantic_messages or [])
    for index in range(len(history) - 1, -1, -1):
        entry = history[index]
        if not isinstance(entry, dict) or entry.get("kind") != "request":
            continue
        parts = entry.get("parts") or []
        if any(isinstance(p, dict) and p.get("part_kind") == "user-prompt" for p in parts):
            return history[index:]
    return []


def committed_answer_payload(conversation: models.ChatConversation) -> dict:
    """The answer already in the conversation, shaped like a candidate payload.

    Usage is zeroed on purpose: the conversation only keeps running totals, so the
    champion's own token counts for that turn are not recoverable. Swapping the turn
    therefore adds the challenger's usage without subtracting an invented figure.
    """
    return {
        "request_ui_message": None,
        "output_ui_message": conversation.messages[-1].model_dump(mode="json"),
        "pydantic_messages": _last_turn_pydantic_messages(conversation),
        "usage": {"promptTokens": 0, "completionTokens": 0, "co2_impact": 0},
    }


def _tier_of_committed_turn(conversation: models.ChatConversation, champion_hrid: str) -> str:
    """Tier the last answer ran on: its routing record, else the tier owning the model."""
    from chat.router.routing import (  # noqa: PLC0415  # pylint: disable=import-outside-toplevel,cyclic-import
        tier_of_model,
    )

    recorded = (conversation.last_routing or {}).get("tier")
    if recorded:
        return recorded
    tier_settings = models.RoutingTierSettings.get_solo()
    tier = tier_of_model(tier_settings, champion_hrid)
    return tier.value if tier is not None else ""


def tried_models_on_turn(conversation, turn: int) -> set[str]:
    """Models already compared on this turn, so the button never repeats one."""
    rows = models.ArenaComparison.objects.filter(conversation=conversation, turn=turn).values_list(
        "champion_model_hrid", "challenger_model_hrid"
    )
    return {hrid for row in rows for hrid in row if hrid}


def untried_alternatives(conversation, turn, tier, champion_hrid, constraints) -> list[str]:
    """Same-tier alternatives that fit the turn and have not answered it yet."""
    tier_settings = models.RoutingTierSettings.get_solo()
    tried = tried_models_on_turn(conversation, turn) | {champion_hrid}
    return [
        hrid
        for hrid in tier_settings.alternatives_for(tier)
        if hrid not in tried and model_fits_turn(hrid, constraints)
    ]


def manual_container_experiment(tier: str) -> models.ArenaExperiment:
    """Return the per-tier home for user-initiated comparisons, creating it once.

    It is never active and never sampled, so it competes with no real experiment
    and never draws on its own; it only gives manual votes somewhere to live.
    """
    experiment, _ = models.ArenaExperiment.objects.get_or_create(
        name=MANUAL_EXPERIMENT_NAME.format(tier=tier),
        defaults={
            "tier": tier,
            "is_active": False,
            "sampling_rate": 0,
            "daily_cap_per_user": 0,
            "description": (
                "Second opinions requested by users on this tier. Not sampled: "
                "rows arrive only when someone asks for another answer."
            ),
        },
    )
    return experiment


@transaction.atomic
def create_manual_comparison(
    *, conversation: models.ChatConversation, user, message_id: str
) -> models.ArenaComparison:
    """Run a second opinion on the last answer (router spec 8.2).

    The champion side is the answer already in the conversation, copied into the
    comparison so only the challenger has to stream. Raises ``ArenaConflict`` with a
    machine-readable code when the turn cannot be compared.
    """
    conversation = models.ChatConversation.objects.select_for_update().get(pk=conversation.pk)
    resolve_pending(conversation)

    messages = list(conversation.messages)
    if not messages or messages[-1].role != "assistant":
        raise ArenaConflict("arena_manual_no_answer")
    if messages[-1].id != message_id:
        raise ArenaConflict("arena_manual_not_last_message")

    champion_hrid = conversation.model_hrid
    tier = _tier_of_committed_turn(conversation, champion_hrid)
    if tier is None or _is_red(champion_hrid):
        raise ArenaConflict("arena_manual_unavailable")
    # A second opinion is a user action, not an experiment: it must work on every
    # tier, including tiers nobody is currently running an experiment on. When
    # there is no active experiment the comparison is filed under a per-tier
    # container so its votes still have a results page.
    experiment = get_active_experiment(tier=tier) or manual_container_experiment(tier)

    turn = sum(1 for message in messages if message.role == "user")
    # The challenger answers the same question from the same history: the committed
    # turn (its user bubble and the answer) is peeled off and replayed.
    history_messages = messages[:-1]
    request_ui_message = None
    if history_messages and history_messages[-1].role == "user":
        request_ui_message = history_messages[-1]
        history_messages = history_messages[:-1]
    constraints = turn_constraints(conversation, request_ui_message)
    candidates = untried_alternatives(conversation, turn, tier, champion_hrid, constraints)
    if not candidates:
        raise ArenaConflict("arena_manual_exhausted")

    challenger_hrid = random.choice(candidates)  # noqa: S311
    champion_side = random.choice([ArenaSide.LEFT, ArenaSide.RIGHT])  # noqa: S311
    history = _last_turn_pydantic_messages(conversation)
    comparison = models.ArenaComparison.objects.create(
        experiment=experiment,
        conversation=conversation,
        user=user,
        turn=turn,
        context_tags=compute_context_tags(conversation, force_web_search=False),
        tools_stripped=is_feature_enabled(user, "presentation_generation"),
        champion_model_hrid=champion_hrid,
        challenger_model_hrid=challenger_hrid,
        champion_side=champion_side,
        conversation_version=conversation.arena_version,
        origin=ArenaOrigin.MANUAL.value,
        tier=tier,
        domain=(conversation.last_routing or {}).get("labels", {}).get("domain", "") or "",
        task=(conversation.last_routing or {}).get("labels", {}).get("task", "") or "",
        # The champion already answered: its side is finished and committed, only the
        # challenger streams.
        champion_payload=committed_answer_payload(conversation),
        champion_finished_at=timezone.now(),
        champion_started_at=timezone.now(),
        champion_committed=True,
        input_snapshot={
            **_input_snapshot(
                conversation,
                request_ui_message,
                force_web_search=False,
                reasoning_effort=experiment.reasoning_effort or None,
            ),
            "messages": [m.model_dump(mode="json") for m in history_messages],
            "pydantic_messages": list(conversation.pydantic_messages or [])[
                : len(conversation.pydantic_messages or []) - len(history)
            ],
        },
    )
    logger.info(
        "Arena second opinion on conversation %s: %s vs %s",
        conversation.pk,
        champion_hrid,
        challenger_hrid,
    )
    return comparison


def get_pending_comparison(conversation) -> models.ArenaComparison | None:
    """The pending comparison of a conversation, if any."""
    return models.ArenaComparison.objects.filter(
        conversation=conversation, status=ArenaComparisonStatus.PENDING
    ).first()


@contextmanager
def locked_comparison(comparison):
    """Serialize commits, cancellation, votes and deletion in a fixed lock order."""
    with transaction.atomic():
        conversation = (
            models.ChatConversation.objects.select_for_update()
            .filter(pk=comparison.conversation_id)
            .first()
        )
        current = models.ArenaComparison.objects.select_for_update().get(pk=comparison.pk)
        current.conversation = conversation if current.conversation_id else None
        yield current


def _version_matches(comparison) -> bool:
    return (
        comparison.conversation is not None
        and comparison.conversation.arena_version == comparison.conversation_version
    )


def candidate_prices(comparison, role) -> dict:
    """Immutable EUR/token pricing revision captured when inference is claimed.

    Prices are declared once, on the model of the LLM configuration
    (``input_price_eur_per_mtok`` / ``output_price_eur_per_mtok``); the arena admin
    has no price field of its own any more (router spec 12). The snapshot freezes
    them on the comparison, so editing the configuration never reprices past
    inference. A model with no configured price yields an empty snapshot and its
    answers are counted as unpriced on the results page.
    """
    configuration = settings.LLM_CONFIGURATIONS.get(getattr(comparison, f"{role}_model_hrid"))
    if configuration is None:
        return {}
    input_price = configuration.input_price_eur_per_mtok
    output_price = configuration.output_price_eur_per_mtok
    if input_price is None or output_price is None:
        return {}
    return {
        "input": str(input_price),
        "output": str(output_price),
        "currency": "EUR",
        "unit": "million_tokens",
        "captured_at": timezone.now().isoformat(),
    }


def claim_candidate(comparison, role, message):
    """Only one request may start each candidate; both consume the same input."""
    with locked_comparison(comparison) as current:
        if current.status != ArenaComparisonStatus.PENDING or not _version_matches(current):
            raise ArenaConflict("arena_comparison_closed")
        if getattr(current, f"{role}_started_at") or current.side_finished(role):
            raise ArenaConflict("arena_side_already_started")
        snapshot = current.input_snapshot
        if snapshot is None:
            # Comparisons predating input snapshots cannot safely be resumed.
            raise ArenaConflict("arena_comparison_has_no_snapshot")
        if snapshot["request_ui_message"] is None:
            snapshot["request_ui_message"] = message.model_dump(mode="json")
        setattr(current, f"{role}_started_at", timezone.now())
        current.price_snapshot[role] = candidate_prices(current, role)
        current.save(
            update_fields=[
                f"{role}_started_at",
                "input_snapshot",
                "price_snapshot",
                "updated_at",
            ]
        )
        return current


def snapshot_conversation(conversation, comparison):
    """Build an in-memory conversation for inference without reading mutable history."""
    snapshot = comparison.input_snapshot
    conversation = deepcopy(conversation)
    conversation.messages = [UIMessage.model_validate(m) for m in snapshot["messages"]]
    conversation.pydantic_messages = deepcopy(snapshot["pydantic_messages"])
    conversation.history_summary = snapshot["history_summary"]
    conversation.history_summary_checkpoint = snapshot["history_summary_checkpoint"]
    return conversation


def build_turn_payload(
    *,
    request_ui_message: UIMessage | None,
    output_ui_message: UIMessage,
    pydantic_messages: list,
    usage: dict,
) -> dict:
    """Serialize one candidate answer so it can be committed to the conversation later."""
    return {
        "request_ui_message": (
            request_ui_message.model_dump(mode="json") if request_ui_message else None
        ),
        "output_ui_message": anonymize_arena_documentation(
            output_ui_message.model_dump(mode="json")
        ),
        "pydantic_messages": anonymize_arena_documentation(pydantic_messages),
        "usage": {
            "promptTokens": int(usage.get("promptTokens", 0) or 0),
            "completionTokens": int(usage.get("completionTokens", 0) or 0),
            "co2_impact": float(usage.get("co2_impact", 0) or 0),
        },
    }


def commit_payload(conversation: models.ChatConversation, payload: dict) -> None:
    """Append a candidate answer to the conversation exactly as a normal turn would.

    Mirrors ``AIAgentService._prepare_update_conversation``: the user bubble is only
    rebuilt when the stored history does not already end with one, the assistant
    bubble is appended, the pydantic history is extended and usage totals accumulate.
    """
    new_messages = list(conversation.messages)
    if payload.get("request_ui_message") and not (new_messages and new_messages[-1].role == "user"):
        new_messages.append(UIMessage.model_validate(payload["request_ui_message"]))
    new_messages.append(UIMessage.model_validate(payload["output_ui_message"]))
    conversation.messages = new_messages
    conversation.pydantic_messages = list(conversation.pydantic_messages) + list(
        payload.get("pydantic_messages", [])
    )
    usage = dict(conversation.agent_usage or {})
    for key in ("promptTokens", "completionTokens", "co2_impact"):
        usage[key] = usage.get(key, 0) + payload["usage"].get(key, 0)
    conversation.agent_usage = usage
    conversation.save(update_fields=["messages", "pydantic_messages", "agent_usage", "updated_at"])


def record_side_result(  # noqa: PLR0913  # pylint: disable=too-many-arguments
    comparison: models.ArenaComparison,
    role: str,
    *,
    payload: dict | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    co2_impact: float | None = None,
    co2_source: str = "",
    reasoning_tokens: int | None = None,
    reasoning_effort: str = "",
    latency_ms: int | None = None,
    first_token_ms: int | None = None,
    trace_id: str = "",
    error: str = "",
    web_search_used: bool = False,
) -> None:
    """Commit a result at most once, while its comparison and turn are current."""
    with locked_comparison(comparison) as current:
        if (
            current.status != ArenaComparisonStatus.PENDING
            or not _version_matches(current)
            or current.side_finished(role)
        ):
            return
        _store_side_result(
            current,
            role,
            payload=payload,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            co2_impact=co2_impact,
            co2_source=co2_source or "",
            reasoning_tokens=reasoning_tokens,
            reasoning_effort=reasoning_effort or "",
            latency_ms=latency_ms,
            first_token_ms=first_token_ms,
            trace_id=trace_id,
            error=error,
            web_search_used=web_search_used,
        )
    comparison.refresh_from_db()


def _store_side_result(comparison, role, *, web_search_used, **values):
    """Persist under the conversation and comparison locks held by the caller."""
    payload = values["payload"]
    error = values["error"]
    values["finished_at"] = timezone.now()
    update_fields = []
    for name, value in values.items():
        field = f"{role}_{name}"
        setattr(comparison, field, value)
        update_fields.append(field)
    if web_search_used:
        add_context_tag(comparison, ArenaContextTag.WEB_SEARCH)
        update_fields.append("context_tags")
    # The production answer goes into the history right away: whatever the user
    # does next (votes, leaves, reloads, closes the tab), the conversation is never
    # left without an answer. A vote for the challenger swaps it (see ``vote``).
    if (
        role == ArenaRole.CHAMPION
        and payload is not None
        and not error
        and comparison.conversation is not None
        and not comparison.champion_committed
    ):
        commit_payload(comparison.conversation, payload)
        comparison.champion_committed = True
        update_fields.append("champion_committed")
    comparison.save(update_fields=update_fields + ["updated_at"])


def replace_last_turn(conversation: models.ChatConversation, old: dict, new: dict) -> None:
    """Swap the assistant answer of the last turn: ``old`` was committed, ``new`` replaces it.

    Used when the user prefers the challenger after the champion answer was
    already written to the history. The user bubble stays, the assistant bubble,
    the pydantic history of that turn and the usage totals are exchanged.
    """
    messages = list(conversation.messages)
    if messages and messages[-1].role == "assistant":
        messages[-1] = UIMessage.model_validate(new["output_ui_message"])
    else:
        messages.append(UIMessage.model_validate(new["output_ui_message"]))
    conversation.messages = messages

    history = list(conversation.pydantic_messages)
    old_len = len(old.get("pydantic_messages", []))
    if old_len:
        history = history[: len(history) - old_len]
    conversation.pydantic_messages = history + list(new.get("pydantic_messages", []))

    usage = dict(conversation.agent_usage or {})
    for key in ("promptTokens", "completionTokens", "co2_impact"):
        usage[key] = usage.get(key, 0) - old["usage"].get(key, 0) + new["usage"].get(key, 0)
    conversation.agent_usage = usage
    conversation.save(update_fields=["messages", "pydantic_messages", "agent_usage", "updated_at"])


def _ensure_champion_committed(comparison: models.ArenaComparison) -> None:
    """Write the champion answer if ``record_side_result`` could not (no conversation then)."""
    if comparison.champion_committed or not _version_matches(comparison):
        return
    if comparison.side_succeeded(ArenaRole.CHAMPION):
        commit_payload(comparison.conversation, comparison.champion_payload)
        comparison.champion_committed = True


def _close_without_vote(comparison: models.ArenaComparison, reason: str | None = None) -> None:
    """Keep the champion answer if it exists and close the comparison."""
    conversation = comparison.conversation
    failed = bool(comparison.champion_error or comparison.challenger_error)
    comparison.closed_reason = "candidate_failed" if failed else "user_abandoned"
    if not _version_matches(comparison):
        comparison.status = ArenaComparisonStatus.ERRORED
        comparison.closed_reason = "superseded"
    elif comparison.side_succeeded(ArenaRole.CHAMPION) and conversation is not None:
        _ensure_champion_committed(comparison)
        comparison.status = (
            ArenaComparisonStatus.ERRORED if failed else ArenaComparisonStatus.ABANDONED
        )
    else:
        comparison.status = ArenaComparisonStatus.ERRORED
        if not comparison.champion_error and not comparison.side_finished(ArenaRole.CHAMPION):
            comparison.champion_error = "unfinished when the comparison was resolved"
            if not failed:
                comparison.closed_reason = "cancelled"
    if reason == "cancelled":
        comparison.status = ArenaComparisonStatus.ERRORED
        comparison.closed_reason = reason
    comparison.save(
        update_fields=[
            "status",
            "closed_reason",
            "champion_error",
            "champion_committed",
            "updated_at",
        ]
    )


def resolve_pending(
    conversation: models.ChatConversation, *, reason: str | None = None
) -> models.ArenaComparison | None:
    """Close a comparison the user walked away from, keeping the champion's answer.

    Called before any new turn or draw on the conversation, and by the vote
    endpoint when the client abandons explicitly. Returns the resolved comparison.
    """
    with transaction.atomic():
        current_conversation = models.ChatConversation.objects.select_for_update().get(
            pk=conversation.pk
        )
        pending = (
            models.ArenaComparison.objects.select_for_update()
            .filter(conversation=conversation, status=ArenaComparisonStatus.PENDING)
            .first()
        )
        if pending is not None:
            pending.conversation = current_conversation
            _close_without_vote(pending, reason=reason)
        current_conversation.arena_version += 1
        current_conversation.save(update_fields=["arena_version"])
        conversation.refresh_from_db()
    push_comparison_scores(pending)
    return pending


DRAW_OUTCOMES = (ArenaVoteOutcome.TIE.value, ArenaVoteOutcome.BOTH_BAD.value)


def vote(comparison: models.ArenaComparison, side: str | None) -> models.ArenaComparison:
    """Record the user's pick, commit the chosen answer and score both traces.

    Thin wrapper around ``_vote``: whatever the outcome, the closed comparison is
    pushed to Langfuse as an ``arena_preference`` score on both sides (spec 12).
    """
    comparison = _vote(comparison, side)
    push_comparison_scores(comparison)
    return comparison


def _vote(comparison: models.ArenaComparison, side: str | None) -> models.ArenaComparison:
    """Record the user's pick and commit the chosen answer.

    ``side`` is the displayed column, never a model name, or one of the draw
    outcomes (``tie`` when both answers were good, ``both_bad`` when neither was).
    A draw keeps the champion answer in the history. ``None`` abandons the
    comparison and keeps the champion answer. Raises ``ArenaConflict`` when the
    comparison is not pending or a side has not finished streaming.
    """
    with locked_comparison(comparison) as current:
        comparison = current
        if comparison.status != ArenaComparisonStatus.PENDING:
            raise ArenaConflict("This comparison is already closed.")
        if side is None:
            _close_without_vote(comparison)
            return comparison
        if not _version_matches(comparison):
            raise ArenaConflict("The conversation has moved to another turn.")
        if not (
            comparison.side_finished(ArenaRole.CHAMPION)
            and comparison.side_finished(ArenaRole.CHALLENGER)
        ):
            raise ArenaConflict("Both answers must be finished before voting.")

        if side in DRAW_OUTCOMES:
            return _vote_draw(comparison, side)

        role = comparison.role_for_side(side)
        if not comparison.side_succeeded(role):
            # The chosen side failed: nothing to keep from it, fall back to the champion.
            _close_without_vote(comparison)
            return comparison

        other = ArenaRole.CHALLENGER if role == ArenaRole.CHAMPION else ArenaRole.CHAMPION
        if role == ArenaRole.CHAMPION:
            _ensure_champion_committed(comparison)
        elif comparison.champion_committed:
            replace_last_turn(
                comparison.conversation, comparison.champion_payload, comparison.challenger_payload
            )
        else:
            commit_payload(comparison.conversation, comparison.challenger_payload)
        now = timezone.now()
        finished = [
            getattr(comparison, f"{r}_finished_at")
            for r in (ArenaRole.CHAMPION, ArenaRole.CHALLENGER)
            if getattr(comparison, f"{r}_finished_at")
        ]
        comparison.time_to_vote_ms = int((now - max(finished)).total_seconds() * 1000)
        comparison.voted_at = now
        if comparison.side_succeeded(other):
            comparison.status = ArenaComparisonStatus.VOTED
            comparison.winner = role
        else:
            # One answer never arrived: the user did not really compare anything.
            comparison.status = ArenaComparisonStatus.ERRORED
            comparison.closed_reason = "candidate_failed"
        comparison.save(
            update_fields=[
                "status",
                "closed_reason",
                "winner",
                "voted_at",
                "time_to_vote_ms",
                "champion_committed",
                "updated_at",
            ]
        )
        return comparison


def _vote_draw(comparison: models.ArenaComparison, outcome: str) -> models.ArenaComparison:
    """Record a tie or a "both bad" vote: the champion answer stays in the history.

    Both answers must have arrived, otherwise the user did not compare anything
    and the comparison is closed without a vote.
    """
    if not (
        comparison.side_succeeded(ArenaRole.CHAMPION)
        and comparison.side_succeeded(ArenaRole.CHALLENGER)
    ):
        _close_without_vote(comparison)
        return comparison
    _ensure_champion_committed(comparison)
    now = timezone.now()
    comparison.time_to_vote_ms = int(
        (
            now - max(comparison.champion_finished_at, comparison.challenger_finished_at)
        ).total_seconds()
        * 1000
    )
    comparison.voted_at = now
    comparison.status = ArenaComparisonStatus.VOTED
    comparison.winner = outcome
    comparison.save(
        update_fields=[
            "status",
            "winner",
            "voted_at",
            "time_to_vote_ms",
            "champion_committed",
            "updated_at",
        ]
    )
    return comparison


MILESTONES = {1: "first_vote", 10: "tenth_vote", 100: "hundredth_vote"}


def _label(comparison: models.ArenaComparison, field: str) -> str | None:
    """i18n key for a routing field of the comparison, ``None`` when absent or empty.

    ``tier``, ``task`` and ``domain`` land on the model with the router; until then
    the block simply carries no label.
    """
    value = getattr(comparison, field, None)
    if not value:
        return None
    return f"router.{field}.{value}"


def build_acknowledgement(comparison: models.ArenaComparison, user) -> dict | None:
    """The thank-you block returned by the vote endpoint (spec section 11.1).

    Draws are votes and get the block; abandonment (status other than ``voted``)
    returns ``None``. ``user_votes`` spans all experiments: the 90-day redaction
    nulls ``user`` on old comparisons, so the count is naturally "recent".
    """
    if comparison.status != ArenaComparisonStatus.VOTED or user is None:
        return None
    voted = models.ArenaComparison.objects.filter(status=ArenaComparisonStatus.VOTED)
    user_votes = voted.filter(user=user).count()
    return {
        "user_votes": user_votes,
        "experiment_votes": voted.filter(experiment_id=comparison.experiment_id).count(),
        "tier_label": _label(comparison, "tier"),
        "task_label": _label(comparison, "task"),
        "domain_label": _label(comparison, "domain"),
        "milestone": MILESTONES.get(user_votes),
    }


def used_web_search(new_messages: Iterable) -> bool:
    """Whether the agent run called the web search tool."""
    for message in new_messages:
        for part in getattr(message, "parts", []):
            if getattr(part, "part_kind", "") == "tool-call" and part.tool_name == "web_search":
                return True
    return False
