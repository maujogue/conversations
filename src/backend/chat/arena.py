"""
Arena: blind champion-versus-challenger comparisons of LLM answers.

An arena turn streams two answers to the same user message, one from the
conversation's pinned production model (the champion) and one from a challenger
drawn from the active ``ArenaExperiment``. The user picks one; the pick is stored
on an ``ArenaComparison`` and the chosen answer is committed to the conversation.
Model names are never shown to the user. See ``docs/arena-mvp-spec.md``.
"""

import logging
import random
from datetime import timedelta
from typing import Iterable

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from core.feature_flags.helpers import is_feature_enabled

from chat import models
from chat.ai_sdk_types import FileUIPart, UIMessage
from chat.constants import IMAGE_MIME_PREFIX
from chat.enums import (
    ArenaComparisonStatus,
    ArenaContextTag,
    ArenaRole,
    ArenaSide,
    ArenaVoteOutcome,
)
from chat.model_health import get_status_for_hrid
from chat.model_routing import resolve_effective_model_hrid

logger = logging.getLogger(__name__)

# Tools whose execution leaves something behind outside the conversation (a generated
# file, a Docs edit). They are stripped from both models in arena mode so the losing
# answer never has side effects. ``generate_presentation`` is the only tool-based one
# today; edit-in-Docs is a separate endpoint, never reachable from a candidate stream.
SIDE_EFFECT_TOOL_NAMES = frozenset({"generate_presentation"})

RED = models.ModelHealth.Status.RED


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


def get_active_experiment() -> models.ArenaExperiment | None:
    """The single active experiment, with its challengers prefetched."""
    return (
        models.ArenaExperiment.objects.filter(is_active=True)
        .prefetch_related("challengers")
        .first()
    )


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


def _eligible_challengers(
    experiment, conversation: models.ChatConversation, last_message: UIMessage | None
) -> list[str]:
    """Challengers that are healthy and, if the turn carries an image, can read images."""
    needs_image = message_has_image(last_message) or _conversation_has_image(conversation)
    eligible = []
    for challenger in experiment.challengers.all():
        configuration = settings.LLM_CONFIGURATIONS.get(challenger.model_hrid)
        if configuration is None or not configuration.is_active:
            continue
        if needs_image and not configuration.supports_image:
            continue
        if _is_red(challenger.model_hrid):
            continue
        eligible.append(challenger.model_hrid)
    return eligible


def _turn_index(conversation: models.ChatConversation) -> int:
    return sum(1 for message in conversation.messages if message.role == "user") + 1


def draw_comparison(  # noqa: PLR0911  # pylint: disable=too-many-return-statements
    *,
    conversation: models.ChatConversation,
    user,
    force_web_search: bool,
    last_message: UIMessage | None = None,
) -> models.ArenaComparison | None:
    """Decide whether the next turn is an arena turn and, if so, create the comparison.

    Runs every eligibility rule of the spec. Returns ``None`` when the turn is a
    normal one. A pending comparison left on the conversation is resolved first, so
    a reload or a quick second message never leaves two comparisons open.
    """
    if not is_feature_enabled(user, "arena"):
        return None
    experiment = get_active_experiment()
    if experiment is None:
        return None

    resolve_pending(conversation)
    _pin_conversation_to_champion(conversation)

    if conversation.model_hrid != experiment.champion_model_hrid:
        _count_refused_draw(experiment.pk)
        return None
    if _is_red(experiment.champion_model_hrid):
        return None
    if random.random() >= experiment.sampling_rate:  # noqa: S311  # not security-sensitive
        return None
    if _user_draws_today(experiment, user) >= experiment.daily_cap_per_user:
        return None

    challengers = _eligible_challengers(experiment, conversation, last_message)
    if not challengers:
        return None

    challenger_hrid = random.choice(challengers)  # noqa: S311
    champion_side = random.choice([ArenaSide.LEFT, ArenaSide.RIGHT])  # noqa: S311

    comparison = models.ArenaComparison.objects.create(
        experiment=experiment,
        conversation=conversation,
        user=user,
        turn=_turn_index(conversation),
        context_tags=compute_context_tags(conversation, force_web_search),
        tools_stripped=is_feature_enabled(user, "presentation_generation"),
        champion_model_hrid=experiment.champion_model_hrid,
        challenger_model_hrid=challenger_hrid,
        champion_side=champion_side,
    )
    logger.info(
        "Arena draw on conversation %s: %s vs %s (champion on the %s)",
        conversation.pk,
        comparison.champion_model_hrid,
        comparison.challenger_model_hrid,
        champion_side,
    )
    return comparison


def get_pending_comparison(conversation) -> models.ArenaComparison | None:
    """The pending comparison of a conversation, if any."""
    return models.ArenaComparison.objects.filter(
        conversation=conversation, status=ArenaComparisonStatus.PENDING
    ).first()


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
        "output_ui_message": output_ui_message.model_dump(mode="json"),
        "pydantic_messages": pydantic_messages,
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
    conversation.save()


def record_side_result(  # noqa: PLR0913  # pylint: disable=too-many-arguments
    comparison: models.ArenaComparison,
    role: str,
    *,
    payload: dict | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    co2_impact: float | None = None,
    latency_ms: int | None = None,
    first_token_ms: int | None = None,
    trace_id: str = "",
    error: str = "",
    web_search_used: bool = False,
) -> None:
    """Store what one model produced for the comparison (answer, metrics or error)."""
    values = {
        "payload": payload,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "co2_impact": co2_impact,
        "latency_ms": latency_ms,
        "first_token_ms": first_token_ms,
        "trace_id": trace_id or "",
        "error": error or "",
        "finished_at": timezone.now(),
    }
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
    conversation.save()


def _ensure_champion_committed(comparison: models.ArenaComparison) -> None:
    """Write the champion answer if ``record_side_result`` could not (no conversation then)."""
    if comparison.champion_committed or comparison.conversation is None:
        return
    if comparison.side_succeeded(ArenaRole.CHAMPION):
        commit_payload(comparison.conversation, comparison.champion_payload)
        comparison.champion_committed = True


def _close_without_vote(comparison: models.ArenaComparison) -> None:
    """Keep the champion answer if it exists and close the comparison."""
    conversation = comparison.conversation
    if comparison.side_succeeded(ArenaRole.CHAMPION) and conversation is not None:
        _ensure_champion_committed(comparison)
        comparison.status = ArenaComparisonStatus.ABANDONED
    else:
        comparison.status = ArenaComparisonStatus.ERRORED
        if not comparison.champion_error and not comparison.side_finished(ArenaRole.CHAMPION):
            comparison.champion_error = "unfinished when the comparison was resolved"
    comparison.save(update_fields=["status", "champion_error", "champion_committed", "updated_at"])


def resolve_pending(conversation: models.ChatConversation) -> models.ArenaComparison | None:
    """Close a comparison the user walked away from, keeping the champion's answer.

    Called before any new turn or draw on the conversation, and by the vote
    endpoint when the client abandons explicitly. Returns the resolved comparison.
    """
    with transaction.atomic():
        pending = (
            models.ArenaComparison.objects.select_for_update()
            .filter(conversation=conversation, status=ArenaComparisonStatus.PENDING)
            .first()
        )
        if pending is None:
            return None
        pending.conversation = conversation
        _close_without_vote(pending)
        return pending


DRAW_OUTCOMES = (ArenaVoteOutcome.TIE.value, ArenaVoteOutcome.BOTH_BAD.value)


def vote(comparison: models.ArenaComparison, side: str | None) -> models.ArenaComparison:
    """Record the user's pick and commit the chosen answer.

    ``side`` is the displayed column, never a model name, or one of the draw
    outcomes (``tie`` when both answers were good, ``both_bad`` when neither was).
    A draw keeps the champion answer in the history. ``None`` abandons the
    comparison and keeps the champion answer. Raises ``ArenaConflict`` when the
    comparison is not pending or a side has not finished streaming.
    """
    with transaction.atomic():
        comparison = models.ArenaComparison.objects.select_for_update().get(pk=comparison.pk)
        if comparison.status != ArenaComparisonStatus.PENDING:
            raise ArenaConflict("This comparison is already closed.")
        if side is None:
            _close_without_vote(comparison)
            return comparison
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
        (now - max(comparison.champion_finished_at, comparison.challenger_finished_at)).total_seconds()
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


def used_web_search(new_messages: Iterable) -> bool:
    """Whether the agent run called the web search tool."""
    for message in new_messages:
        for part in getattr(message, "parts", []):
            if getattr(part, "part_kind", "") == "tool-call" and part.tool_name == "web_search":
                return True
    return False
