"""Unit tests for the arena service: eligibility, tags, commit, vote and results math."""

# pylint: disable=redefined-outer-name, unused-argument

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import close_old_connections
from django.utils import timezone

import pytest

from core.factories import UserFactory
from core.feature_flags.flags import FeatureFlags, FeatureToggle

from chat import arena, arena_scores
from chat.ai_sdk_types import FileUIPart, TextUIPart, UIMessage
from chat.arena_results import (
    _rate_block,
    answer_cost_eur,
    build_results,
    promote_challenger,
    wilson_interval,
)
from chat.clients.pydantic_ai import AIAgentService
from chat.enums import (
    ArenaComparisonStatus,
    ArenaContextTag,
    ArenaOrigin,
    ArenaRole,
    ArenaSide,
    ArenaVoteOutcome,
    ReasoningEffort,
    RoutingDomain,
    RoutingReason,
    RoutingTask,
    RoutingTier,
    TierSource,
)
from chat.factories import (
    ArenaChallengerFactory,
    ArenaComparisonFactory,
    ArenaExperimentFactory,
    ChatConversationAttachmentFactory,
    ChatConversationFactory,
    ChatProjectFactory,
)
from chat.llm_configuration import LLModel, LLMProvider
from chat.model_health import model_health_cache_key
from chat.models import ChatConversation, RoutingTierSettings
from chat.router.labels import RoutingDecision, RoutingLabels

pytestmark = pytest.mark.django_db


def _make_llm(hrid: str, supports_image: bool = False, tools=None, prices=None) -> LLModel:
    input_price, output_price = prices or (None, None)
    return LLModel(
        hrid=hrid,
        model_name=f"{hrid}-llm",
        human_readable_name=hrid,
        is_active=True,
        supports_image=supports_image,
        web_search="chat.tools.web_search_brave.web_search_brave",
        system_prompt="You are a helpful assistant.",
        tools=tools or [],
        input_price_eur_per_mtok=input_price,
        output_price_eur_per_mtok=output_price,
        provider=LLMProvider(
            hrid="albert", base_url="https://www.external-ai-service.com/", api_key="k"
        ),
    )


@pytest.fixture(autouse=True)
def arena_settings(settings):
    """Two configured models, the arena flag on, a clean cache."""
    settings.LLM_CONFIGURATIONS = {
        "main-model": _make_llm("main-model"),
        "challenger-model": _make_llm("challenger-model"),
        "vision-challenger": _make_llm("vision-challenger", supports_image=True),
    }
    settings.LLM_DEFAULT_MODEL_HRID = "main-model"
    settings.LLM_FALLBACK_MODEL_HRID_1 = ""
    settings.LLM_FALLBACK_MODEL_HRID_2 = ""
    settings.FEATURE_FLAGS = FeatureFlags(
        web_search=FeatureToggle.ENABLED,
        document_upload=FeatureToggle.ENABLED,
        presentation_generation=FeatureToggle.DISABLED,
        arena=FeatureToggle.ENABLED,
    )
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def tier_settings():
    """Standard tier on ``main-model``, with the two challengers as its alternatives."""
    settings_row = RoutingTierSettings.get_solo()
    settings_row.standard_model_hrid = "main-model"
    settings_row.standard_alternatives = ["challenger-model", "vision-challenger"]
    settings_row.save()
    return settings_row


@pytest.fixture
def experiment(tier_settings):
    """An active standard-tier experiment with one challenger."""
    experiment = ArenaExperimentFactory(is_active=True, tier=RoutingTier.STANDARD.value)
    ArenaChallengerFactory(experiment=experiment, model_hrid="challenger-model")
    return experiment


def _payload(text: str, prompt_tokens=10, completion_tokens=20) -> dict:
    return arena.build_turn_payload(
        request_ui_message=UIMessage(
            id="u1", role="user", content="Hello", parts=[TextUIPart(type="text", text="Hello")]
        ),
        output_ui_message=UIMessage(
            id="a1", role="assistant", content=text, parts=[TextUIPart(type="text", text=text)]
        ),
        pydantic_messages=[{"kind": "request"}, {"kind": "response", "text": text}],
        usage={
            "promptTokens": prompt_tokens,
            "completionTokens": completion_tokens,
            "co2_impact": 0.5,
        },
    )


# --------------------------------------------------------------------------- #
# Draw and eligibility
# --------------------------------------------------------------------------- #


def test_draw_creates_pending_comparison_pinned_to_champion(experiment):
    """An eligible turn yields a pending comparison and pins the conversation to the champion."""
    conversation = ChatConversationFactory()

    comparison = arena.draw_comparison(
        conversation=conversation, user=conversation.owner, force_web_search=False
    )

    assert comparison is not None
    assert comparison.status == ArenaComparisonStatus.PENDING
    assert comparison.champion_model_hrid == "main-model"
    assert comparison.challenger_model_hrid == "challenger-model"
    assert comparison.champion_side in (ArenaSide.LEFT, ArenaSide.RIGHT)
    assert comparison.context_tags == [ArenaContextTag.PLAIN]
    assert comparison.turn == 1
    conversation.refresh_from_db()
    assert conversation.model_hrid == "main-model"


def test_draw_refused_when_flag_disabled(settings, experiment):
    """The feature flag off means no arena, whatever the experiment says."""
    settings.FEATURE_FLAGS = FeatureFlags(arena=FeatureToggle.DISABLED)
    conversation = ChatConversationFactory()

    assert (
        arena.draw_comparison(
            conversation=conversation, user=conversation.owner, force_web_search=False
        )
        is None
    )


def test_draw_refused_without_active_experiment(tier_settings):
    """An inactive experiment never draws."""
    inactive = ArenaExperimentFactory(is_active=False)
    ArenaChallengerFactory(experiment=inactive)
    conversation = ChatConversationFactory()

    assert (
        arena.draw_comparison(
            conversation=conversation, user=conversation.owner, force_web_search=False
        )
        is None
    )


def test_draw_respects_sampling_rate(experiment):
    """A zero sampling rate never draws, a full one always does."""
    experiment.sampling_rate = 0.0
    experiment.save()
    conversation = ChatConversationFactory()
    assert (
        arena.draw_comparison(
            conversation=conversation, user=conversation.owner, force_web_search=False
        )
        is None
    )

    experiment.sampling_rate = 1.0
    experiment.save()
    assert (
        arena.draw_comparison(
            conversation=conversation, user=conversation.owner, force_web_search=False
        )
        is not None
    )


def test_draw_respects_daily_cap(experiment):
    """Once the user reached the daily cap, no more arena turns today."""
    experiment.daily_cap_per_user = 1
    experiment.save()
    user = UserFactory()
    first = ChatConversationFactory(owner=user)
    second = ChatConversationFactory(owner=user)

    drawn = arena.draw_comparison(conversation=first, user=user, force_web_search=False)
    assert drawn is not None
    # The first comparison is resolved (abandoned) by the second draw attempt, but the
    # cap still counts it.
    assert arena.draw_comparison(conversation=second, user=user, force_web_search=False) is None


def test_draw_refused_when_conversation_not_on_champion(experiment):
    """A conversation pinned to another model is never compared, and the refusal is counted."""
    conversation = ChatConversationFactory(model_hrid="challenger-model")

    assert (
        arena.draw_comparison(
            conversation=conversation, user=conversation.owner, force_web_search=False
        )
        is None
    )
    assert arena.get_refused_draws(experiment.pk) == 1


def test_draw_skips_red_challenger(experiment):
    """A challenger reported red by the health poller is not drawn."""
    cache.set(model_health_cache_key("albert", "challenger-model-llm"), "red", timeout=None)
    conversation = ChatConversationFactory()

    assert (
        arena.draw_comparison(
            conversation=conversation, user=conversation.owner, force_web_search=False
        )
        is None
    )


def test_draw_skips_text_only_challenger_when_image_attached(experiment):
    """With an image on the turn, only image-capable challengers are eligible."""
    conversation = ChatConversationFactory()
    ChatConversationAttachmentFactory(
        conversation=conversation,
        content_type="image/png",
        file_name="cat.png",
        upload_state="ready",
    )

    assert (
        arena.draw_comparison(
            conversation=conversation, user=conversation.owner, force_web_search=False
        )
        is None
    )

    ArenaChallengerFactory(experiment=experiment, model_hrid="vision-challenger")
    comparison = arena.draw_comparison(
        conversation=conversation, user=conversation.owner, force_web_search=False
    )
    assert comparison is not None
    assert comparison.challenger_model_hrid == "vision-challenger"
    assert ArenaContextTag.ATTACHMENT in comparison.context_tags


def test_context_tags_from_conversation_state():
    """Tags describe the turn: forced search, attachments, project; plain when none."""
    project = ChatProjectFactory()
    conversation = ChatConversationFactory(project=project, owner=project.owner)
    ChatConversationAttachmentFactory(
        conversation=conversation, content_type="text/plain", upload_state="ready"
    )

    tags = arena.compute_context_tags(conversation, force_web_search=True)

    assert tags == [ArenaContextTag.WEB_SEARCH, ArenaContextTag.ATTACHMENT, ArenaContextTag.PROJECT]
    assert arena.compute_context_tags(ChatConversationFactory(), False) == [ArenaContextTag.PLAIN]


def test_add_context_tag_drops_plain():
    """A tag discovered during the run replaces the plain marker."""
    comparison = ArenaComparisonFactory(context_tags=["plain"])

    arena.add_context_tag(comparison, ArenaContextTag.WEB_SEARCH)

    assert comparison.context_tags == ["web_search"]


# --------------------------------------------------------------------------- #
# Recording, committing, voting, resolving
# --------------------------------------------------------------------------- #


def test_recording_the_champion_commits_its_answer_immediately():
    """The production answer lands in the history as soon as it is ready.

    Whatever the user does next (vote, leave, reload), the conversation is never
    left without an answer.
    """
    comparison = ArenaComparisonFactory()

    arena.record_side_result(comparison, ArenaRole.CHAMPION, payload=_payload("A"))

    comparison.refresh_from_db()
    assert comparison.champion_committed is True
    comparison.conversation.refresh_from_db()
    assert [m.content for m in comparison.conversation.messages] == ["Hello", "A"]

    # Recording the challenger never touches the history.
    arena.record_side_result(comparison, ArenaRole.CHALLENGER, payload=_payload("B"))
    comparison.conversation.refresh_from_db()
    assert [m.content for m in comparison.conversation.messages] == ["Hello", "A"]


def test_record_side_result_stores_metrics_and_search_tag():
    """Recording a side fills its flat columns and can add the web_search tag."""
    comparison = ArenaComparisonFactory(context_tags=["plain"])

    arena.record_side_result(
        comparison,
        ArenaRole.CHALLENGER,
        payload=_payload("B"),
        prompt_tokens=11,
        completion_tokens=22,
        co2_impact=0.1,
        latency_ms=1200,
        first_token_ms=300,
        trace_id="abc",
        web_search_used=True,
    )

    comparison.refresh_from_db()
    assert comparison.challenger_prompt_tokens == 11
    assert comparison.challenger_completion_tokens == 22
    assert comparison.challenger_latency_ms == 1200
    assert comparison.challenger_first_token_ms == 300
    assert comparison.challenger_trace_id == "abc"
    assert comparison.challenger_finished_at is not None
    assert comparison.side_succeeded(ArenaRole.CHALLENGER)
    assert comparison.context_tags == ["web_search"]


def test_commit_payload_appends_turn_like_a_normal_run():
    """Committing appends user and assistant bubbles, history and usage totals."""
    conversation = ChatConversationFactory(agent_usage={"promptTokens": 5})

    arena.commit_payload(conversation, _payload("Hello there"))

    conversation.refresh_from_db()
    assert [m.role for m in conversation.messages] == ["user", "assistant"]
    assert conversation.messages[1].content == "Hello there"
    assert len(conversation.pydantic_messages) == 2
    assert conversation.agent_usage == {
        "promptTokens": 15,
        "completionTokens": 20,
        "co2_impact": 0.5,
    }


def test_commit_payload_skips_user_bubble_when_already_stored():
    """A user bubble already persisted (error path) is not duplicated on commit."""
    conversation = ChatConversationFactory(
        messages=[
            UIMessage(
                id="u0", role="user", content="Hello", parts=[TextUIPart(type="text", text="Hello")]
            )
        ]
    )

    arena.commit_payload(conversation, _payload("Hi"))

    conversation.refresh_from_db()
    assert [m.role for m in conversation.messages] == ["user", "assistant"]


def test_vote_for_challenger_swaps_the_committed_champion_answer():
    """Preferring the challenger replaces the champion answer, history and usage."""
    comparison = ArenaComparisonFactory(champion_side="left")
    arena.record_side_result(
        comparison,
        ArenaRole.CHAMPION,
        payload=_payload("A", prompt_tokens=10, completion_tokens=20),
    )
    arena.record_side_result(
        comparison,
        ArenaRole.CHALLENGER,
        payload=_payload("B", prompt_tokens=30, completion_tokens=40),
    )

    voted = arena.vote(comparison, "right")

    assert voted.status == ArenaComparisonStatus.VOTED
    assert voted.winner == ArenaRole.CHALLENGER
    assert voted.voted_at is not None
    assert voted.time_to_vote_ms is not None
    conversation = comparison.conversation
    conversation.refresh_from_db()
    assert [m.content for m in conversation.messages] == ["Hello", "B"]
    assert conversation.pydantic_messages[-1]["text"] == "B"
    assert len(conversation.pydantic_messages) == 2
    assert conversation.agent_usage["promptTokens"] == 30
    assert conversation.agent_usage["completionTokens"] == 40


def test_vote_for_champion_keeps_the_committed_answer():
    """Preferring the champion changes nothing in the history, only the vote is stored."""
    comparison = ArenaComparisonFactory(champion_side="left")
    arena.record_side_result(comparison, ArenaRole.CHAMPION, payload=_payload("A"))
    arena.record_side_result(comparison, ArenaRole.CHALLENGER, payload=_payload("B"))

    voted = arena.vote(comparison, "left")

    assert voted.winner == ArenaRole.CHAMPION
    comparison.conversation.refresh_from_db()
    assert [m.content for m in comparison.conversation.messages] == ["Hello", "A"]


@pytest.mark.parametrize("outcome", [ArenaVoteOutcome.TIE, ArenaVoteOutcome.BOTH_BAD])
def test_vote_draw_keeps_the_champion_answer_and_counts_as_a_vote(outcome):
    """ "Both good" and "both bad" are votes: recorded as such, the champion answer stays."""
    comparison = ArenaComparisonFactory(champion_side="right")
    arena.record_side_result(comparison, ArenaRole.CHAMPION, payload=_payload("A"))
    arena.record_side_result(comparison, ArenaRole.CHALLENGER, payload=_payload("B"))

    voted = arena.vote(comparison, outcome.value)

    assert voted.status == ArenaComparisonStatus.VOTED
    assert voted.winner == outcome
    assert voted.voted_at is not None
    assert voted.time_to_vote_ms is not None
    comparison.conversation.refresh_from_db()
    assert [m.content for m in comparison.conversation.messages] == ["Hello", "A"]


def test_vote_draw_when_a_side_errored_is_not_counted():
    """A draw over an answer that never arrived compared nothing: closed without a vote."""
    comparison = ArenaComparisonFactory(champion_side="left")
    arena.record_side_result(comparison, ArenaRole.CHAMPION, payload=_payload("A"))
    arena.record_side_result(comparison, ArenaRole.CHALLENGER, error="model_connection_error")

    resolved = arena.vote(comparison, ArenaVoteOutcome.TIE.value)

    assert resolved.status == ArenaComparisonStatus.ERRORED
    assert resolved.winner == ""
    comparison.conversation.refresh_from_db()
    assert comparison.conversation.messages[-1].content == "A"


def test_vote_requires_both_sides_finished():
    """A vote before both streams finished is a conflict."""
    comparison = ArenaComparisonFactory()
    arena.record_side_result(comparison, ArenaRole.CHAMPION, payload=_payload("A"))

    with pytest.raises(arena.ArenaConflict):
        arena.vote(comparison, "left")


def test_vote_twice_is_a_conflict():
    """A closed comparison refuses a second vote."""
    comparison = ArenaComparisonFactory()
    arena.record_side_result(comparison, ArenaRole.CHAMPION, payload=_payload("A"))
    arena.record_side_result(comparison, ArenaRole.CHALLENGER, payload=_payload("B"))
    arena.vote(comparison, "left")

    with pytest.raises(arena.ArenaConflict):
        arena.vote(comparison, "left")


def test_vote_null_abandons_and_keeps_champion():
    """Abandoning keeps the production answer and never counts as a vote."""
    comparison = ArenaComparisonFactory(champion_side="right")
    arena.record_side_result(comparison, ArenaRole.CHAMPION, payload=_payload("A"))
    arena.record_side_result(comparison, ArenaRole.CHALLENGER, payload=_payload("B"))

    resolved = arena.vote(comparison, None)

    assert resolved.status == ArenaComparisonStatus.ABANDONED
    assert resolved.winner == ""
    comparison.conversation.refresh_from_db()
    assert comparison.conversation.messages[-1].content == "A"


def test_vote_for_errored_side_falls_back_to_champion():
    """Picking a side that failed keeps the champion and closes as errored or abandoned."""
    comparison = ArenaComparisonFactory(champion_side="left")
    arena.record_side_result(comparison, ArenaRole.CHAMPION, payload=_payload("A"))
    arena.record_side_result(comparison, ArenaRole.CHALLENGER, error="model_connection_error")

    resolved = arena.vote(comparison, "right")

    assert resolved.status == ArenaComparisonStatus.ERRORED
    comparison.conversation.refresh_from_db()
    assert comparison.conversation.messages[-1].content == "A"


def test_vote_when_other_side_errored_is_not_counted():
    """A vote against an answer that never arrived is stored as errored, not as a win."""
    comparison = ArenaComparisonFactory(champion_side="left")
    arena.record_side_result(comparison, ArenaRole.CHAMPION, payload=_payload("A"))
    arena.record_side_result(comparison, ArenaRole.CHALLENGER, error="model_connection_error")

    resolved = arena.vote(comparison, "left")

    assert resolved.status == ArenaComparisonStatus.ERRORED
    assert resolved.winner == ""
    comparison.conversation.refresh_from_db()
    assert comparison.conversation.messages[-1].content == "A"


def test_resolve_pending_with_unfinished_champion_is_errored():
    """Walking away before the champion finished leaves nothing to keep."""
    comparison = ArenaComparisonFactory()

    resolved = arena.resolve_pending(comparison.conversation)

    assert resolved.status == ArenaComparisonStatus.ERRORED
    assert "unfinished" in resolved.champion_error
    comparison.conversation.refresh_from_db()
    assert comparison.conversation.messages == []


def test_resolve_pending_without_pending_is_noop():
    """No pending comparison, nothing to resolve."""
    assert arena.resolve_pending(ChatConversationFactory()) is None


def test_used_web_search_detects_tool_call():
    """The web_search tool call in the run output flags the comparison."""

    class Part:  # pylint: disable=too-few-public-methods,missing-class-docstring
        part_kind = "tool-call"
        tool_name = "web_search"

    class Message:  # pylint: disable=too-few-public-methods,missing-class-docstring
        parts = [Part()]

    assert arena.used_web_search([Message()])
    assert not arena.used_web_search([])


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


def test_wilson_interval_known_values():
    """60 of 100 barely separates from a coin flip; 60 of 30-ish does not."""
    low, high = wilson_interval(60, 100)
    assert round(low, 3) == 0.502
    assert round(high, 3) == 0.691
    low, high = wilson_interval(18, 30)
    assert low < 0.5 < high
    assert wilson_interval(0, 0) == (0.0, 0.0)


def test_answer_cost_eur():
    """Cost is tokens times price per million tokens."""
    assert answer_cost_eur(1_000_000, 500_000, 2, 6) == 5


def test_build_results_scoreboard_and_position(experiment):
    """Win rate, interval, indicative flag, context slices and position check."""
    experiment.min_votes_for_conclusion = 3
    experiment.save()
    common = {
        "experiment": experiment,
        "champion_model_hrid": "main-model",
        "challenger_model_hrid": "challenger-model",
        "champion_prompt_tokens": 1000,
        "champion_completion_tokens": 1000,
        "challenger_prompt_tokens": 1000,
        "challenger_completion_tokens": 1000,
        "status": ArenaComparisonStatus.VOTED,
        "price_snapshot": {
            "champion": {"input": "1", "output": "1"},
            "challenger": {"input": "0.5", "output": "0.5"},
        },
    }
    ArenaComparisonFactory(
        **common, champion_side="left", winner="challenger", context_tags=["plain"]
    )
    ArenaComparisonFactory(
        **common, champion_side="right", winner="challenger", context_tags=["plain"]
    )
    ArenaComparisonFactory(
        **common, champion_side="left", winner="champion", context_tags=["web_search"]
    )
    ArenaComparisonFactory(
        **{**common, "status": ArenaComparisonStatus.ABANDONED}, champion_side="left"
    )
    ArenaComparisonFactory(
        **{**common, "status": ArenaComparisonStatus.VOTED, "is_seed": True},
        champion_side="left",
        winner="challenger",
    )

    results = build_results(experiment, include_seed=False)

    header = results["header"]
    assert header["total"] == 4
    assert header["voted"] == 3
    assert header["abandoned"] == 1
    assert header["vote_rate"] == 0.75
    assert header["has_seed"] is True
    row = results["challengers"][0]
    assert row["model_hrid"] == "challenger-model"
    assert row["votes"] == 3
    assert row["wins"] == 2
    assert row["sample_sufficient"] is True
    assert row["indicative"] is True
    assert row["cost_ratio"] == 2.0
    assert {(r["tag"], r["votes"], r["wins"]) for r in results["by_context"]} == {
        ("plain", 2, 2),
        ("web_search", 1, 0),
    }
    # Left won when the challenger won on the right (1) or the champion won on the left (1).
    assert results["position"]["wins"] == 2
    assert results["position"]["votes"] == 3

    with_seed = build_results(experiment, include_seed=True)
    assert with_seed["header"]["total"] == 5


def test_experiment_validation_single_active_and_challenger_tools(experiment, settings):
    """Model-level validation: one active experiment, challengers share the champion tools."""
    # BaseModel validates on save, so building is enough to exercise clean().
    other = ArenaExperimentFactory.build(is_active=True, tier=RoutingTier.STANDARD.value)
    with pytest.raises(ValidationError):
        other.full_clean()

    settings.LLM_CONFIGURATIONS["tooled"] = _make_llm("tooled", tools=["some_tool"])
    challenger = ArenaChallengerFactory.build(experiment=experiment, model_hrid="tooled")
    with pytest.raises(ValidationError):
        challenger.full_clean()

    same_as_champion = ArenaChallengerFactory.build(experiment=experiment, model_hrid="main-model")
    with pytest.raises(ValidationError):
        same_as_champion.full_clean()


def test_tools_stripped_follows_presentation_flag(settings, experiment):
    """The comparison records that side-effect tools were removed when the feature is on."""
    settings.FEATURE_FLAGS = FeatureFlags(
        arena=FeatureToggle.ENABLED, presentation_generation=FeatureToggle.ENABLED
    )
    conversation = ChatConversationFactory()

    with patch("chat.arena.random.random", return_value=0.0):
        comparison = arena.draw_comparison(
            conversation=conversation, user=conversation.owner, force_web_search=False
        )

    assert comparison.tools_stripped is True


def test_late_result_after_cancellation_cannot_modify_a_new_turn():
    """An old worker cannot append content or usage after a newer turn finishes."""
    comparison = ArenaComparisonFactory()
    stale = type(comparison).objects.get(pk=comparison.pk)
    arena.vote(comparison, None)
    arena.resolve_pending(comparison.conversation)
    arena.commit_payload(comparison.conversation, _payload("New answer"))
    before = list(comparison.conversation.pydantic_messages)
    arena.record_side_result(stale, ArenaRole.CHAMPION, payload=_payload("Late answer"))
    comparison.refresh_from_db()
    comparison.conversation.refresh_from_db()
    assert comparison.conversation.pydantic_messages == before
    assert not comparison.champion_committed
    assert comparison.champion_payload is None


def test_result_and_vote_reject_a_superseded_conversation_version():
    """A stale pending status alone is insufficient to authorize a commit or swap."""
    comparison = ArenaComparisonFactory()
    arena.record_side_result(comparison, ArenaRole.CHAMPION, payload=_payload("A"))
    arena.record_side_result(comparison, ArenaRole.CHALLENGER, payload=_payload("B"))
    conversation = comparison.conversation
    conversation.arena_version += 1
    conversation.save(update_fields=["arena_version"])
    arena.commit_payload(conversation, _payload("New answer"))
    with pytest.raises(arena.ArenaConflict):
        arena.vote(comparison, "right")
    conversation.refresh_from_db()
    assert conversation.messages[-1].content == "New answer"


def test_result_commit_rolls_back_with_comparison_save():
    """Conversation content and the committed marker share one transaction."""
    comparison = ArenaComparisonFactory()
    with patch.object(type(comparison), "save", side_effect=RuntimeError("write failed")):
        with pytest.raises(RuntimeError, match="write failed"):
            arena.record_side_result(comparison, ArenaRole.CHAMPION, payload=_payload("A"))
    comparison.refresh_from_db()
    comparison.conversation.refresh_from_db()
    assert comparison.conversation.messages == []
    assert not comparison.champion_committed
    arena.record_side_result(comparison, ArenaRole.CHAMPION, payload=_payload("A"))
    assert comparison.champion_committed


@pytest.mark.django_db(transaction=True)
def test_concurrent_duplicate_results_commit_once():
    """Two stale worker objects cannot append the same champion turn twice."""

    comparison = ArenaComparisonFactory()
    barrier = Barrier(2)

    def finish():
        close_old_connections()
        try:
            worker = type(comparison).objects.get(pk=comparison.pk)
            barrier.wait(timeout=10)
            arena.record_side_result(worker, ArenaRole.CHAMPION, payload=_payload("A"))
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: finish(), range(2)))
    comparison.refresh_from_db()
    conversation = comparison.conversation
    assert [m.content for m in conversation.messages] == ["Hello", "A"]
    assert conversation.agent_usage["promptTokens"] == 10


@pytest.mark.django_db(transaction=True)
def test_concurrent_candidate_claims_have_one_winner(experiment):
    """PostgreSQL serializes competing claims before either calls the provider."""

    conversation = ChatConversationFactory()
    comparison = arena.draw_comparison(
        conversation=conversation, user=conversation.owner, force_web_search=False
    )
    barrier = Barrier(2)
    message = UIMessage.model_validate(_payload("A")["request_ui_message"])

    def claim():
        close_old_connections()
        try:
            worker = type(comparison).objects.get(pk=comparison.pk)
            barrier.wait(timeout=10)
            try:
                arena.claim_candidate(worker, ArenaRole.CHAMPION, message)
                return "claimed"
            except arena.ArenaConflict:
                return "conflict"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: claim(), range(2))) == ["claimed", "conflict"]


@pytest.mark.parametrize("delete_via", ["instance", "queryset", "user"])
def test_deletion_erases_content_and_user_links(delete_via):
    """Keep aggregate metrics, never prompt/answer copies or personal associations."""

    comparison = ArenaComparisonFactory(
        input_snapshot={"prompt": "private question"},
        champion_trace_id="private trace",
    )
    arena.record_side_result(comparison, ArenaRole.CHAMPION, payload=_payload("Private answer"))
    stale = type(comparison).objects.get(pk=comparison.pk)
    if delete_via == "user":
        comparison.user.delete()
    elif delete_via == "queryset":
        ChatConversation.objects.filter(pk=comparison.conversation_id).delete()
    else:
        comparison.conversation.delete()
    arena.record_side_result(stale, ArenaRole.CHALLENGER, payload=_payload("Late private answer"))
    comparison.refresh_from_db()
    assert comparison.conversation_id is None
    assert comparison.user_id is None
    assert comparison.input_snapshot is None
    assert comparison.champion_payload is None
    assert comparison.challenger_payload is None
    assert comparison.champion_trace_id == ""
    assert comparison.status == ArenaComparisonStatus.ERRORED


def test_retention_purge_is_idempotent_and_preserves_metrics():
    """Only content and associations older than 90 days are removed."""

    old = ArenaComparisonFactory(
        drawn_at=timezone.now() - timedelta(days=91),
        champion_payload=_payload("Private"),
        input_snapshot={"prompt": "private"},
        status=ArenaComparisonStatus.VOTED,
        winner=ArenaRole.CHAMPION,
        champion_prompt_tokens=123,
    )
    recent = ArenaComparisonFactory(champion_payload=_payload("Recent"))
    call_command("purge_arena_content")
    call_command("purge_arena_content")
    old.refresh_from_db()
    recent.refresh_from_db()
    assert old.champion_payload is None and old.input_snapshot is None
    assert old.user_id is None and old.conversation_id is None
    assert old.status == ArenaComparisonStatus.VOTED
    assert old.winner == ArenaRole.CHAMPION and old.champion_prompt_tokens == 123
    assert recent.champion_payload is not None


def test_pending_comparisons_are_in_vote_coverage_denominator(experiment):
    """Ten votes and ninety pending comparisons mean 10% coverage."""
    ArenaComparisonFactory.create_batch(
        10, experiment=experiment, status=ArenaComparisonStatus.VOTED
    )
    ArenaComparisonFactory.create_batch(90, experiment=experiment)
    header = build_results(experiment)["header"]
    assert header["vote_rate"] == 0.1
    assert header["closed_vote_rate"] == 1.0
    assert header["pending"] == 90


def test_vote_threshold_does_not_imply_sufficient_evidence():
    """51/100 remains indicative; sample size and interval separation are distinct."""

    block = _rate_block(51, 100, 100)
    assert block["sample_sufficient"]
    assert not block["evidence_sufficient"]
    assert block["indicative"]
    assert _rate_block(70, 100, 100)["evidence_sufficient"]


def test_prices_are_frozen_at_candidate_claim(settings, experiment):
    """Editing a model's configured prices cannot change recorded call costs."""
    settings.LLM_CONFIGURATIONS = {
        **settings.LLM_CONFIGURATIONS,
        "main-model": _make_llm("main-model", prices=(1, 2)),
    }
    conversation = ChatConversationFactory()
    comparison = arena.draw_comparison(
        conversation=conversation, user=conversation.owner, force_web_search=False
    )
    comparison = arena.claim_candidate(
        comparison,
        ArenaRole.CHAMPION,
        UIMessage.model_validate(_payload("A")["request_ui_message"]),
    )
    arena.record_side_result(
        comparison,
        ArenaRole.CHAMPION,
        payload=_payload("A"),
        prompt_tokens=1000,
        completion_tokens=1000,
    )
    before = build_results(experiment)["header"]["total_cost_eur"]
    settings.LLM_CONFIGURATIONS = {
        **settings.LLM_CONFIGURATIONS,
        "main-model": _make_llm("main-model", prices=(10, 20)),
    }
    assert before is not None
    assert comparison.price_snapshot["champion"]["input"] == "1.0"
    assert build_results(experiment)["header"]["total_cost_eur"] == before
    assert comparison.price_snapshot["champion"]["captured_at"]


def test_legacy_calls_without_prices_are_unknown(experiment):
    ArenaComparisonFactory(experiment=experiment, champion_prompt_tokens=1000)
    header = build_results(experiment)["header"]
    assert header["total_cost_eur"] is None
    assert header["unpriced_answers"] == 1


def test_challenger_failure_is_not_abandonment(experiment):
    comparison = ArenaComparisonFactory(experiment=experiment)
    arena.record_side_result(comparison, ArenaRole.CHAMPION, payload=_payload("A"))
    arena.record_side_result(comparison, ArenaRole.CHALLENGER, error="model_connection_error")
    arena.vote(comparison, None)
    results = build_results(experiment)
    assert results["header"]["abandoned"] == 0
    assert results["header"]["errored"] == 1
    assert results["header"]["failure_reasons"] == {"candidate_failed": 1}
    assert results["champion"]["failures"] == 0
    assert results["challengers"][0]["failures"] == 1


def test_snapshot_freezes_existing_history_summary_and_files(experiment):

    conversation = ChatConversationFactory(
        pydantic_messages=[{"kind": "request", "parts": []}],
        history_summary="Original summary",
    )
    message = UIMessage(
        id="u1",
        role="user",
        parts=[
            TextUIPart(type="text", text="Original question"),
            FileUIPart(
                type="file", mediaType="text/plain", url="https://example.test/original.txt"
            ),
        ],
    )
    comparison = arena.draw_comparison(
        conversation=conversation,
        user=conversation.owner,
        force_web_search=True,
        last_message=message,
    )
    conversation.pydantic_messages = []
    conversation.history_summary = "New summary"
    conversation.save(update_fields=["pydantic_messages", "history_summary"])
    message.parts[0].text = "Changed"
    comparison = arena.claim_candidate(comparison, ArenaRole.CHALLENGER, message)
    frozen = arena.snapshot_conversation(conversation, comparison)
    assert frozen.history_summary == "Original summary"
    assert len(frozen.pydantic_messages) == 1
    assert (
        comparison.input_snapshot["request_ui_message"]["parts"][0]["text"] == "Original question"
    )
    assert comparison.input_snapshot["request_ui_message"]["parts"][1]["url"].endswith(
        "original.txt"
    )
    assert comparison.input_snapshot["force_web_search"] is True


def test_older_normal_turn_cannot_roll_back_arena_version(experiment):
    """A normal worker created before the draw cannot overwrite the new turn."""

    conversation = ChatConversationFactory()
    service = AIAgentService(conversation=conversation, user=conversation.owner)
    comparison = arena.draw_comparison(
        conversation=conversation, user=conversation.owner, force_web_search=False
    )
    service.conversation.messages = [
        UIMessage.model_validate(_payload("Old answer")["output_ui_message"])
    ]
    assert service._save_completed_conversation() is False
    conversation.refresh_from_db()
    assert conversation.arena_version == comparison.conversation_version
    assert conversation.messages == []


# --------------------------------------------------------------------------- #
# Per-tier draws, control challenger and routing labels (router spec 8.1)
# --------------------------------------------------------------------------- #


def _decision(tier=RoutingTier.STANDARD, model_hrid="main-model", **overrides):
    """A routing decision as ``post_arena_draw`` hands it to the draw."""
    values = {
        "tier": tier,
        "tier_source": TierSource.ROUTER,
        "model_hrid": model_hrid,
        "reason": RoutingReason.CLASSIFIED,
        "labels": RoutingLabels(
            complexity=tier,
            domain=RoutingDomain.ADMINISTRATIVE,
            task=RoutingTask.WRITING,
            confidence=0.82,
        ),
        "router_confidence": 0.82,
        "router_model_hrid": "router-model",
        "router_prompt_version": "v3",
    }
    values.update(overrides)
    return RoutingDecision(**values)


def test_draw_stores_the_routing_labels_of_the_turn(experiment):
    """A routed turn tags its comparison with tier, source, domain, task and router facts."""
    conversation = ChatConversationFactory()

    comparison = arena.draw_comparison(
        conversation=conversation,
        user=conversation.owner,
        force_web_search=False,
        routing_decision=_decision(),
    )

    assert comparison.tier == RoutingTier.STANDARD
    assert comparison.tier_source == TierSource.ROUTER
    assert comparison.domain == RoutingDomain.ADMINISTRATIVE
    assert comparison.task == RoutingTask.WRITING
    assert comparison.router_confidence == 0.82
    assert comparison.router_reason == RoutingReason.CLASSIFIED
    assert comparison.router_model_hrid == "router-model"
    assert comparison.router_prompt_version == "v3"
    assert comparison.origin == ArenaOrigin.DRAW


def test_draw_only_on_the_experiment_tier(experiment):
    """A turn routed to another tier is never compared by this experiment."""
    conversation = ChatConversationFactory()

    assert (
        arena.draw_comparison(
            conversation=conversation,
            user=conversation.owner,
            force_web_search=False,
            routing_decision=_decision(tier=RoutingTier.COMPLEX),
        )
        is None
    )


def test_draw_refused_when_the_turn_left_the_tier_model(experiment):
    """A health cascade or constraint walk moved the turn off the champion: no draw."""
    conversation = ChatConversationFactory()

    assert (
        arena.draw_comparison(
            conversation=conversation,
            user=conversation.owner,
            force_web_search=False,
            routing_decision=_decision(model_hrid="vision-challenger"),
        )
        is None
    )
    assert arena.get_refused_draws(experiment.pk) == 1


def test_draw_skips_a_challenger_that_cannot_serve_the_turn(settings, experiment):
    """Turn constraints are the router's own: a challenger that fails one is not drawn."""
    settings.LLM_CONFIGURATIONS["challenger-model"].web_search = None
    conversation = ChatConversationFactory()

    assert (
        arena.draw_comparison(
            conversation=conversation, user=conversation.owner, force_web_search=True
        )
        is None
    )
    # Without the forced search the same challenger is eligible again.
    assert (
        arena.draw_comparison(
            conversation=conversation, user=conversation.owner, force_web_search=False
        )
        is not None
    )


def test_control_challenger_is_drawn_on_its_own_share(experiment):
    """The control is drawn on ``CONTROL_DRAW_RATE`` of the draws, the regular one otherwise."""
    ArenaChallengerFactory(experiment=experiment, model_hrid="vision-challenger", is_control=True)
    conversation = ChatConversationFactory()

    with patch("chat.arena.random.random", side_effect=[0.0, 0.0]):
        drawn = arena.draw_comparison(
            conversation=conversation, user=conversation.owner, force_web_search=False
        )
    assert drawn.challenger_model_hrid == "vision-challenger"

    with patch("chat.arena.random.random", side_effect=[0.0, 0.5]):
        drawn = arena.draw_comparison(
            conversation=conversation, user=conversation.owner, force_web_search=False
        )
    assert drawn.challenger_model_hrid == "challenger-model"


def test_challenger_must_belong_to_the_tier_unless_it_is_the_control(experiment, settings):
    """Out-of-tier challengers are refused; the flagged control is the one exception."""
    settings.LLM_CONFIGURATIONS["outsider"] = _make_llm("outsider")

    outsider = ArenaChallengerFactory.build(experiment=experiment, model_hrid="outsider")
    with pytest.raises(ValidationError):
        outsider.full_clean()

    control = ArenaChallengerFactory.build(
        experiment=experiment, model_hrid="outsider", is_control=True
    )
    control.full_clean()


def test_experiment_refuses_a_second_active_experiment_on_the_same_tier_only(experiment):
    """One active experiment per tier: another tier may run its own at the same time."""
    same_tier = ArenaExperimentFactory.build(is_active=True, tier=RoutingTier.STANDARD.value)
    with pytest.raises(ValidationError):
        same_tier.full_clean()

    other_tier = ArenaExperimentFactory.build(is_active=True, tier=RoutingTier.COMPLEX.value)
    other_tier.full_clean()


def test_experiment_champion_follows_the_tier_settings(experiment, tier_settings):
    """The champion is read from the tier settings, so a promotion moves it."""
    assert experiment.champion_model_hrid == "main-model"

    tier_settings.standard_model_hrid = "challenger-model"
    tier_settings.save()

    assert experiment.champion_model_hrid == "challenger-model"


def test_record_side_result_stores_the_footprint_source_and_reasoning(experiment):
    """Per-side CO2 source, reasoning tokens and effort land on the comparison."""
    comparison = ArenaComparisonFactory(experiment=experiment)

    arena.record_side_result(
        comparison,
        ArenaRole.CHALLENGER,
        payload=_payload("B"),
        co2_impact=0.2,
        co2_source="estimated_reasoning",
        reasoning_tokens=1200,
        reasoning_effort="high",
    )

    comparison.refresh_from_db()
    assert comparison.challenger_co2_source == "estimated_reasoning"
    assert comparison.challenger_reasoning_tokens == 1200
    assert comparison.challenger_reasoning_effort == "high"


def test_experiment_effort_overrides_the_router_effort(experiment):
    """A tier 3 experiment can pin both sides to one effort (router spec 8.1)."""
    decision = _decision(reasoning_effort=ReasoningEffort.MEDIUM)
    assert arena.effort_for_comparison(experiment, decision) == "medium"

    experiment.reasoning_effort = ReasoningEffort.HIGH.value
    assert arena.effort_for_comparison(experiment, decision) == "high"

    conversation = ChatConversationFactory()
    comparison = arena.draw_comparison(
        conversation=conversation,
        user=conversation.owner,
        force_web_search=False,
        routing_decision=decision,
    )
    assert comparison.input_snapshot["reasoning_effort"] == "medium"


# --------------------------------------------------------------------------- #
# Results: populations, tag tables and the promotion verdict
# --------------------------------------------------------------------------- #


def _voted_comparison(experiment, winner, **overrides):
    """A decisive vote on ``experiment``, champion on the left by default."""
    values = {
        "experiment": experiment,
        "challenger_model_hrid": "challenger-model",
        "status": ArenaComparisonStatus.VOTED,
        "winner": winner,
        "champion_side": ArenaSide.LEFT,
        "champion_co2_impact": 1e-4,
        "challenger_co2_impact": 1e-4,
        "champion_latency_ms": 1000,
        "challenger_latency_ms": 1000,
    }
    values.update(overrides)
    return ArenaComparisonFactory(**values)


def test_results_keep_sampled_manual_and_control_votes_apart(experiment):
    """Second opinions and the control challenger never enter the sampled win rate."""
    experiment.min_votes_for_conclusion = 2
    experiment.save()
    ArenaChallengerFactory(experiment=experiment, model_hrid="vision-challenger", is_control=True)
    _voted_comparison(experiment, ArenaRole.CHALLENGER)
    _voted_comparison(experiment, ArenaRole.CHAMPION)
    _voted_comparison(experiment, ArenaRole.CHALLENGER, origin=ArenaOrigin.MANUAL.value)
    _voted_comparison(experiment, ArenaRole.CHALLENGER, challenger_model_hrid="vision-challenger")

    results = build_results(experiment)

    assert [row["model_hrid"] for row in results["challengers"]] == ["challenger-model"]
    row = results["challengers"][0]
    assert (row["votes"], row["wins"]) == (2, 1)
    assert (row["manual"]["votes"], row["manual"]["wins"]) == (1, 1)
    assert results["header"]["manual"]["votes"] == 1
    assert [c["model_hrid"] for c in results["controls"]] == ["vision-challenger"]
    assert results["controls"][0]["votes"] == 1
    assert results["header"]["control_comparisons"] == 1
    # The champion row is read against the sampled population only.
    assert results["champion"]["votes"] == 2


def test_results_tag_tables_by_domain_task_and_context(experiment):
    """Three tag tables replace the single by-context slice (router spec 8.1)."""
    experiment.min_votes_for_conclusion = 1
    experiment.save()
    _voted_comparison(
        experiment,
        ArenaRole.CHALLENGER,
        domain=RoutingDomain.LEGAL.value,
        task=RoutingTask.WRITING.value,
        context_tags=["plain"],
    )
    _voted_comparison(
        experiment,
        ArenaRole.CHAMPION,
        domain=RoutingDomain.LEGAL.value,
        task=RoutingTask.CODING.value,
        context_tags=["web_search", "attachment"],
    )

    results = build_results(experiment)

    assert {(r["tag"], r["votes"], r["wins"]) for r in results["by_domain"]} == {("legal", 2, 1)}
    assert {(r["tag"], r["votes"], r["wins"]) for r in results["by_task"]} == {
        ("writing", 1, 1),
        ("coding", 1, 0),
    }
    assert {(r["tag"], r["votes"], r["wins"]) for r in results["by_context_tags"]} == {
        ("plain", 1, 1),
        ("web_search", 1, 0),
        ("attachment", 1, 0),
    }
    assert results["by_context"] == results["by_context_tags"]


@pytest.mark.parametrize(
    ("wins", "losses", "challenger_co2", "challenger_latency", "verdict"),
    [
        # A clear preference: the interval sits entirely above 50%.
        (10, 0, 1e-4, 1000, "promote"),
        # A coin flip on votes, but a much lighter and no slower answer.
        (5, 5, 5e-5, 900, "promote_for_footprint"),
        # A coin flip with the same footprint: nothing justifies moving production.
        (5, 5, 1e-4, 1000, "keep"),
        # Lighter, but slower at p95: the footprint alone does not promote.
        (5, 5, 5e-5, 3000, "keep"),
        # Under the vote threshold: nothing to conclude yet.
        (1, 1, 5e-5, 900, "indicative"),
    ],
)
def test_promotion_verdicts(  # noqa: PLR0913
    experiment, wins, losses, challenger_co2, challenger_latency, verdict
):
    """The verdict column implements the rule of router spec 8.1."""
    experiment.min_votes_for_conclusion = 10
    experiment.save()
    footprint = {
        "challenger_co2_impact": challenger_co2,
        "challenger_latency_ms": challenger_latency,
    }
    for _ in range(wins):
        _voted_comparison(experiment, ArenaRole.CHALLENGER, **footprint)
    for _ in range(losses):
        _voted_comparison(experiment, ArenaRole.CHAMPION, **footprint)

    row = build_results(experiment)["challengers"][0]

    assert row["verdict"] == verdict


def test_promotion_writes_the_tier_settings_and_its_history(experiment, tier_settings):
    """Promoting a challenger moves the tier model and logs the evidence."""
    experiment.min_votes_for_conclusion = 2
    experiment.save()
    user = UserFactory()
    for _ in range(10):
        _voted_comparison(experiment, ArenaRole.CHALLENGER)

    history = promote_challenger(experiment, "challenger-model", user=user)

    tier_settings.refresh_from_db()
    assert tier_settings.standard_model_hrid == "challenger-model"
    # The former champion stays available as an alternative of the tier.
    assert "main-model" in tier_settings.standard_alternatives
    assert tier_settings.alternatives_for(RoutingTier.STANDARD) == [
        "vision-challenger",
        "main-model",
    ]
    assert history.tier == RoutingTier.STANDARD
    assert history.reason == "promotion"
    assert (history.from_value, history.to_value) == ("main-model", "challenger-model")
    assert history.changed_by == user
    assert history.evidence["votes"] == 10
    assert history.evidence["win_rate"] == 1.0
    assert history.evidence["verdict"] == "promote"
    assert history.evidence["experiment_id"] == str(experiment.pk)
    assert "wh_ratio" in history.evidence


def test_promotion_refuses_an_unknown_challenger(experiment):
    """Only a model of the scoreboard can be promoted."""
    with pytest.raises(ValueError, match="not a challenger"):
        promote_challenger(experiment, "vision-challenger")


# --------------------------------------------------------------------------- #
# Langfuse scores (router spec 12)
# --------------------------------------------------------------------------- #


@pytest.fixture
def langfuse_scores(settings):
    """Langfuse enabled with a patched client; yields the recorded ``create_score`` calls."""
    settings.LANGFUSE_ENABLED = True
    with patch("chat.arena_scores.langfuse.get_client") as get_client:
        yield get_client.return_value.create_score


def _finished_comparison(**kwargs):
    comparison = ArenaComparisonFactory(**kwargs)
    arena.record_side_result(
        comparison, ArenaRole.CHAMPION, payload=_payload("A"), trace_id="trace-champion"
    )
    arena.record_side_result(
        comparison, ArenaRole.CHALLENGER, payload=_payload("B"), trace_id="trace-challenger"
    )
    return comparison


def test_vote_pushes_a_categorical_score_on_both_traces(langfuse_scores):
    """The winner's trace gets ``won``, the loser's ``lost``, both idempotent by score id."""
    comparison = _finished_comparison(champion_side="left")

    arena.vote(comparison, "right")

    calls = {call.kwargs["trace_id"]: call.kwargs for call in langfuse_scores.call_args_list}
    assert set(calls) == {"trace-champion", "trace-challenger"}
    champion = calls["trace-champion"]
    assert champion["name"] == "arena_preference"
    assert champion["value"] == "lost"
    assert champion["data_type"] == "CATEGORICAL"
    assert champion["score_id"] == f"{comparison.pk}-champion"
    assert f"comparison={comparison.pk}" in champion["comment"]
    assert "origin=draw" in champion["comment"]
    assert calls["trace-challenger"]["value"] == "won"
    assert calls["trace-challenger"]["score_id"] == f"{comparison.pk}-challenger"


@pytest.mark.parametrize(
    "outcome,expected",
    [(ArenaVoteOutcome.TIE, "tie"), (ArenaVoteOutcome.BOTH_BAD, "both_bad")],
)
def test_draw_pushes_the_same_value_on_both_traces(langfuse_scores, outcome, expected):
    comparison = _finished_comparison(champion_side="left")

    arena.vote(comparison, outcome.value)

    assert {call.kwargs["value"] for call in langfuse_scores.call_args_list} == {expected}


def test_abandonment_pushes_abandoned(langfuse_scores):
    comparison = _finished_comparison(champion_side="left")

    arena.vote(comparison, None)

    assert {call.kwargs["value"] for call in langfuse_scores.call_args_list} == {"abandoned"}


def test_resolve_pending_pushes_abandoned(langfuse_scores):
    comparison = _finished_comparison(champion_side="left")

    arena.resolve_pending(comparison.conversation)

    assert {call.kwargs["value"] for call in langfuse_scores.call_args_list} == {"abandoned"}


def test_an_errored_side_is_scored_abandoned_and_a_missing_trace_is_skipped(langfuse_scores):
    """No trace id, no score: the side never reached Langfuse."""
    comparison = ArenaComparisonFactory(champion_side="left")
    arena.record_side_result(
        comparison, ArenaRole.CHAMPION, payload=_payload("A"), trace_id="trace-champion"
    )
    arena.record_side_result(comparison, ArenaRole.CHALLENGER, error="model_connection_error")

    arena.vote(comparison, "right")

    assert [call.kwargs["trace_id"] for call in langfuse_scores.call_args_list] == [
        "trace-champion"
    ]
    assert langfuse_scores.call_args_list[0].kwargs["value"] == "abandoned"


def test_no_score_when_langfuse_is_disabled(settings):
    settings.LANGFUSE_ENABLED = False
    comparison = _finished_comparison(champion_side="left")

    with patch("chat.arena_scores.langfuse.get_client") as get_client:
        arena.vote(comparison, "left")

    get_client.assert_not_called()


def test_a_langfuse_failure_never_breaks_a_vote(langfuse_scores):
    langfuse_scores.side_effect = RuntimeError("langfuse down")
    comparison = _finished_comparison(champion_side="left")

    voted = arena.vote(comparison, "left")

    assert voted.status == ArenaComparisonStatus.VOTED
    assert voted.winner == ArenaRole.CHAMPION


def test_a_pending_comparison_has_no_score_value():
    comparison = ArenaComparisonFactory()
    assert arena_scores.score_value_for(comparison, ArenaRole.CHAMPION) is None
