"""Unit tests for the arena service: eligibility, tags, commit, vote and results math."""

# pylint: disable=redefined-outer-name, unused-argument

from unittest.mock import patch

from django.core.cache import cache
from django.core.exceptions import ValidationError

import pytest

from core.factories import UserFactory
from core.feature_flags.flags import FeatureFlags, FeatureToggle

from chat import arena
from chat.ai_sdk_types import TextUIPart, UIMessage
from chat.arena_results import answer_cost_eur, build_results, wilson_interval
from chat.enums import (
    ArenaComparisonStatus,
    ArenaContextTag,
    ArenaRole,
    ArenaSide,
    ArenaVoteOutcome,
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

pytestmark = pytest.mark.django_db


def _make_llm(hrid: str, supports_image: bool = False, tools=None) -> LLModel:
    return LLModel(
        hrid=hrid,
        model_name=f"{hrid}-llm",
        human_readable_name=hrid,
        is_active=True,
        supports_image=supports_image,
        system_prompt="You are a helpful assistant.",
        tools=tools or [],
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
def experiment():
    """An active experiment with one challenger."""
    experiment = ArenaExperimentFactory(is_active=True, champion_model_hrid="main-model")
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


def test_draw_refused_without_active_experiment():
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
    """"Both good" and "both bad" are votes: recorded as such, the champion answer stays."""
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

    assert resolved.status == ArenaComparisonStatus.ABANDONED
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

    assert resolved.status == ArenaComparisonStatus.ABANDONED
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
    experiment.champion_input_price_eur_per_mtok = 1
    experiment.champion_output_price_eur_per_mtok = 1
    experiment.save()
    experiment.challengers.update(input_price_eur_per_mtok=0.5, output_price_eur_per_mtok=0.5)
    common = {
        "experiment": experiment,
        "champion_model_hrid": "main-model",
        "challenger_model_hrid": "challenger-model",
        "champion_prompt_tokens": 1000,
        "champion_completion_tokens": 1000,
        "challenger_prompt_tokens": 1000,
        "challenger_completion_tokens": 1000,
        "status": ArenaComparisonStatus.VOTED,
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
    assert row["indicative"] is False
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
    other = ArenaExperimentFactory.build(is_active=True, champion_model_hrid="main-model")
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
