"""Arena endpoints: draw, candidate streams that persist nothing, vote, auto-resolution."""

# pylint: disable=redefined-outer-name, unused-argument

import json

from django.core.cache import cache
from django.utils import timezone

import pytest
import respx
from freezegun import freeze_time
from rest_framework import status

from core.factories import UserFactory
from core.feature_flags.flags import FeatureFlags, FeatureToggle

from chat import arena
from chat.enums import ArenaComparisonStatus, ArenaRole
from chat.factories import (
    ArenaChallengerFactory,
    ArenaComparisonFactory,
    ArenaExperimentFactory,
    ChatConversationFactory,
)
from chat.llm_configuration import LLModel, LLMProvider
from chat.models import ArenaComparison

pytestmark = pytest.mark.django_db(transaction=True)

FROZEN = "2025-07-25T10:36:35.297675Z"


def _make_llm(hrid: str) -> LLModel:
    return LLModel(
        hrid=hrid,
        model_name=f"{hrid}-llm",
        human_readable_name=hrid,
        is_active=True,
        system_prompt="You are a helpful assistant.",
        tools=[],
        provider=LLMProvider(
            hrid="albert",
            base_url="https://www.external-ai-service.com/",
            api_key="test-api-key",
        ),
    )


@pytest.fixture(autouse=True)
def arena_settings(settings):
    """Champion and challenger configured, arena flag on, clean cache."""
    settings.LLM_CONFIGURATIONS = {
        "main-model": _make_llm("main-model"),
        "challenger-model": _make_llm("challenger-model"),
    }
    settings.LLM_DEFAULT_MODEL_HRID = "main-model"
    settings.LLM_FALLBACK_MODEL_HRID_1 = ""
    settings.LLM_FALLBACK_MODEL_HRID_2 = ""
    settings.FEATURE_FLAGS = FeatureFlags(arena=FeatureToggle.ENABLED)
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def experiment():
    """An active experiment drawing on every turn."""
    experiment = ArenaExperimentFactory(
        is_active=True, champion_model_hrid="main-model", sampling_rate=1.0
    )
    ArenaChallengerFactory(experiment=experiment, model_hrid="challenger-model")
    return experiment


def _drain(response):
    return b"".join(response.streaming_content)


def test_draw_returns_comparison_id_only(api_client, experiment):
    """The draw answers with an id and never a model name."""
    conversation = ChatConversationFactory()
    api_client.force_login(conversation.owner)

    response = api_client.post(
        f"/api/v1.0/chats/{conversation.pk}/arena/draw/", {"force_web_search": False}, format="json"
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["arena"] is True
    assert set(body) == {"arena", "comparison_id"}
    assert ArenaComparison.objects.filter(pk=body["comparison_id"]).exists()


def test_draw_without_experiment_is_a_normal_turn(api_client):
    """No active experiment: the client sends normally."""
    conversation = ChatConversationFactory()
    api_client.force_login(conversation.owner)

    response = api_client.post(f"/api/v1.0/chats/{conversation.pk}/arena/draw/", {}, format="json")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"arena": False}


def test_draw_on_another_users_conversation_is_404(api_client, experiment):
    """Ownership is enforced like every conversation action."""
    conversation = ChatConversationFactory()
    api_client.force_login(UserFactory())

    response = api_client.post(f"/api/v1.0/chats/{conversation.pk}/arena/draw/", {}, format="json")

    assert response.status_code == status.HTTP_404_NOT_FOUND


@freeze_time(FROZEN)
@respx.mock
def test_candidate_streams_and_vote_for_challenger(
    api_client, experiment, mock_openai_stream_multi_calls, hello_conversation_data
):
    """The champion answer is written as soon as it lands; a challenger vote swaps it."""
    conversation = ChatConversationFactory(owner__language="en-us")
    api_client.force_login(conversation.owner)
    draw = api_client.post(
        f"/api/v1.0/chats/{conversation.pk}/arena/draw/", {}, format="json"
    ).json()
    comparison = ArenaComparison.objects.get(pk=draw["comparison_id"])
    base = f"/api/v1.0/chats/{conversation.pk}/conversation/?arena_comparison={comparison.pk}"

    for side in ("left", "right"):
        response = api_client.post(
            f"{base}&arena_side={side}", hello_conversation_data, format="json"
        )
        assert response.status_code == status.HTTP_200_OK
        _drain(response)

    conversation.refresh_from_db()
    # The production answer is already in the history, the challenger's is held.
    assert [m.role for m in conversation.messages] == ["user", "assistant"]
    assert len(conversation.pydantic_messages) == 2
    assert conversation.model_hrid == "main-model"
    comparison.refresh_from_db()
    assert comparison.champion_committed is True
    for role in (ArenaRole.CHAMPION, ArenaRole.CHALLENGER):
        assert comparison.side_succeeded(role), role
        assert getattr(comparison, f"{role}_latency_ms") is not None
        assert (
            getattr(comparison, f"{role}_payload")["output_ui_message"]["content"] == "Hello there"
        )

    # The challenger's stream must have been sent to the challenger's model.
    called_models = {
        json.loads(call.request.content)["model"] for call in mock_openai_stream_multi_calls.calls
    }
    assert called_models == {"main-model-llm", "challenger-model-llm"}

    challenger_side = comparison.challenger_side
    vote = api_client.post(
        f"/api/v1.0/chats/{conversation.pk}/arena/{comparison.pk}/vote/",
        {"side": challenger_side},
        format="json",
    )

    assert vote.status_code == status.HTTP_200_OK
    body = vote.json()
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert body["pending_arena_comparison"] is None
    assert "main-model" not in str(body) and "challenger-model" not in str(body)
    comparison.refresh_from_db()
    assert comparison.status == ArenaComparisonStatus.VOTED
    assert comparison.winner == ArenaRole.CHALLENGER
    conversation.refresh_from_db()
    assert [m.role for m in conversation.messages] == ["user", "assistant"]
    assert conversation.messages[-1].content == "Hello there"
    # The turn's history was swapped, not appended: still one request + one response.
    assert len(conversation.pydantic_messages) == 2
    assert conversation.pydantic_messages[-1]["model_name"] == "challenger-model-llm"
    assert conversation.model_hrid == "main-model"


@freeze_time(FROZEN)
@respx.mock
def test_candidate_stream_rejects_side_already_answered(
    api_client, experiment, mock_openai_stream_multi_calls, hello_conversation_data
):
    """The same side cannot be streamed twice for one comparison."""
    conversation = ChatConversationFactory(owner__language="en-us")
    api_client.force_login(conversation.owner)
    draw = api_client.post(
        f"/api/v1.0/chats/{conversation.pk}/arena/draw/", {}, format="json"
    ).json()
    url = (
        f"/api/v1.0/chats/{conversation.pk}/conversation/"
        f"?arena_comparison={draw['comparison_id']}&arena_side=left"
    )
    _drain(api_client.post(url, hello_conversation_data, format="json"))

    response = api_client.post(url, hello_conversation_data, format="json")

    assert response.status_code == status.HTTP_409_CONFLICT


def test_candidate_stream_requires_both_arena_params(
    api_client, experiment, hello_conversation_data
):
    """arena_comparison without arena_side is a client error."""
    conversation = ChatConversationFactory()
    api_client.force_login(conversation.owner)
    comparison = ArenaComparisonFactory(conversation=conversation, experiment=experiment)

    response = api_client.post(
        f"/api/v1.0/chats/{conversation.pk}/conversation/?arena_comparison={comparison.pk}",
        hello_conversation_data,
        format="json",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST


def test_candidate_stream_unknown_comparison_is_404(
    api_client, experiment, hello_conversation_data
):
    """A comparison of another conversation is not reachable."""
    conversation = ChatConversationFactory()
    api_client.force_login(conversation.owner)
    other = ArenaComparisonFactory(experiment=experiment)

    response = api_client.post(
        f"/api/v1.0/chats/{conversation.pk}/conversation/"
        f"?arena_comparison={other.pk}&arena_side=left",
        hello_conversation_data,
        format="json",
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_vote_before_both_sides_finished_is_409(api_client, experiment):
    """Voting is only possible once both answers are in."""
    conversation = ChatConversationFactory()
    api_client.force_login(conversation.owner)
    comparison = ArenaComparisonFactory(conversation=conversation, experiment=experiment)

    response = api_client.post(
        f"/api/v1.0/chats/{conversation.pk}/arena/{comparison.pk}/vote/",
        {"side": "left"},
        format="json",
    )

    assert response.status_code == status.HTTP_409_CONFLICT


def test_vote_null_abandons_and_returns_conversation(api_client, experiment):
    """Abandoning through the vote endpoint keeps the champion answer."""
    conversation = ChatConversationFactory()
    api_client.force_login(conversation.owner)
    comparison = ArenaComparisonFactory(
        conversation=conversation,
        experiment=experiment,
        champion_side="left",
        champion_finished_at=timezone.now(),
        champion_payload={
            "request_ui_message": {
                "id": "u1",
                "role": "user",
                "content": "Hello",
                "parts": [{"type": "text", "text": "Hello"}],
            },
            "output_ui_message": {
                "id": "a1",
                "role": "assistant",
                "content": "Champion answer",
                "parts": [{"type": "text", "text": "Champion answer"}],
            },
            "pydantic_messages": [],
            "usage": {"promptTokens": 1, "completionTokens": 1, "co2_impact": 0},
        },
    )

    response = api_client.post(
        f"/api/v1.0/chats/{conversation.pk}/arena/{comparison.pk}/vote/",
        {"side": None},
        format="json",
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["messages"][-1]["content"] == "Champion answer"
    comparison.refresh_from_db()
    assert comparison.status == ArenaComparisonStatus.ABANDONED


def test_vote_tie_keeps_champion_and_records_the_vote(api_client, experiment):
    """A "both good" vote goes through the same endpoint and keeps the champion answer."""
    conversation = ChatConversationFactory()
    api_client.force_login(conversation.owner)
    payload = {
        "request_ui_message": {
            "id": "u1",
            "role": "user",
            "content": "Hello",
            "parts": [{"type": "text", "text": "Hello"}],
        },
        "output_ui_message": {
            "id": "a1",
            "role": "assistant",
            "content": "Champion answer",
            "parts": [{"type": "text", "text": "Champion answer"}],
        },
        "pydantic_messages": [],
        "usage": {"promptTokens": 1, "completionTokens": 1, "co2_impact": 0},
    }
    comparison = ArenaComparisonFactory(
        conversation=conversation,
        experiment=experiment,
        champion_side="left",
        champion_finished_at=timezone.now(),
        champion_payload=payload,
        challenger_finished_at=timezone.now(),
        challenger_payload={
            **payload,
            "output_ui_message": {**payload["output_ui_message"], "content": "Challenger"},
        },
    )

    response = api_client.post(
        f"/api/v1.0/chats/{conversation.pk}/arena/{comparison.pk}/vote/",
        {"side": "tie"},
        format="json",
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["messages"][-1]["content"] == "Champion answer"
    comparison.refresh_from_db()
    assert comparison.status == ArenaComparisonStatus.VOTED
    assert comparison.winner == "tie"


def test_vote_rejects_unknown_outcome(api_client, experiment):
    """Only left, right, tie, both_bad or null are accepted."""
    conversation = ChatConversationFactory()
    api_client.force_login(conversation.owner)
    comparison = ArenaComparisonFactory(conversation=conversation, experiment=experiment)

    response = api_client.post(
        f"/api/v1.0/chats/{conversation.pk}/arena/{comparison.pk}/vote/",
        {"side": "champion"},
        format="json",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST


def test_retrieve_exposes_pending_comparison_without_models(api_client, experiment):
    """The conversation payload tells the client a comparison is pending, nothing more."""
    conversation = ChatConversationFactory()
    api_client.force_login(conversation.owner)
    comparison = ArenaComparisonFactory(conversation=conversation, experiment=experiment)

    response = api_client.get(f"/api/v1.0/chats/{conversation.pk}/")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["pending_arena_comparison"] == {
        "id": str(comparison.pk),
        "sides_finished": {"left": False, "right": False},
        "restorable": False,
        "answers": None,
    }


def _ui_message(identifier: str, role: str, text: str) -> dict:
    return {
        "id": identifier,
        "role": role,
        "content": text,
        "parts": [{"type": "text", "text": text}],
    }


def _payload(answer_id: str, answer: str) -> dict:
    return {
        "request_ui_message": _ui_message("u1", "user", "Hello"),
        "output_ui_message": _ui_message(answer_id, "assistant", answer),
        "pydantic_messages": [],
        "usage": {"promptTokens": 1, "completionTokens": 1, "co2_impact": 0},
    }


def test_retrieve_restores_a_comparison_left_without_a_vote(api_client, experiment):
    """Both answers come back so the client can show the choice again, un-voted.

    The champion answer, committed to the history as a safety net, is kept out of
    ``messages``: while the choice is open it is a candidate, not the answer.
    """
    conversation = ChatConversationFactory(messages=[])
    api_client.force_login(conversation.owner)
    comparison = ArenaComparisonFactory(
        conversation=conversation,
        experiment=experiment,
        champion_side="left",
        champion_finished_at=timezone.now(),
        champion_payload=_payload("a1", "Champion answer"),
        challenger_finished_at=timezone.now(),
        challenger_payload=_payload("a2", "Challenger answer"),
    )
    # The champion answer is written to the history as soon as it finishes.
    arena.commit_payload(conversation, comparison.champion_payload)
    comparison.champion_committed = True
    comparison.save()

    body = api_client.get(f"/api/v1.0/chats/{conversation.pk}/").json()

    pending = body["pending_arena_comparison"]
    assert pending["id"] == str(comparison.pk)
    assert pending["restorable"] is True
    assert pending["answers"]["left"]["parts"][0]["text"] == "Champion answer"
    assert pending["answers"]["right"]["parts"][0]["text"] == "Challenger answer"
    # Only the question stays visible: the answer below it is still being chosen.
    assert [message["role"] for message in body["messages"]] == ["user"]
    comparison.refresh_from_db()
    assert comparison.status == ArenaComparisonStatus.PENDING


def test_retrieve_does_not_restore_a_half_finished_comparison(api_client, experiment):
    """One answer missing: nothing to compare, so the client abandons it instead."""
    conversation = ChatConversationFactory(messages=[])
    api_client.force_login(conversation.owner)
    ArenaComparisonFactory(
        conversation=conversation,
        experiment=experiment,
        champion_side="left",
        champion_finished_at=timezone.now(),
        champion_payload=_payload("a1", "Champion answer"),
    )

    pending = api_client.get(f"/api/v1.0/chats/{conversation.pk}/").json()[
        "pending_arena_comparison"
    ]

    assert pending["restorable"] is False
    assert pending["answers"] is None


@freeze_time(FROZEN)
@respx.mock
def test_normal_turn_resolves_pending_comparison_first(
    api_client, experiment, mock_openai_stream_multi_calls, hello_conversation_data
):
    """Sending a new message while a comparison is pending abandons it and keeps the champion."""
    conversation = ChatConversationFactory(owner__language="en-us", model_hrid="main-model")
    api_client.force_login(conversation.owner)
    draw = api_client.post(
        f"/api/v1.0/chats/{conversation.pk}/arena/draw/", {}, format="json"
    ).json()
    comparison = ArenaComparison.objects.get(pk=draw["comparison_id"])
    _drain(
        api_client.post(
            f"/api/v1.0/chats/{conversation.pk}/conversation/"
            f"?arena_comparison={comparison.pk}&arena_side={comparison.champion_side}",
            hello_conversation_data,
            format="json",
        )
    )

    response = api_client.post(
        f"/api/v1.0/chats/{conversation.pk}/conversation/", hello_conversation_data, format="json"
    )
    assert response.status_code == status.HTTP_200_OK
    _drain(response)

    comparison.refresh_from_db()
    assert comparison.status == ArenaComparisonStatus.ABANDONED
    conversation.refresh_from_db()
    # Champion answer from the arena turn, then the normal turn.
    assert [m.role for m in conversation.messages] == ["user", "assistant", "user", "assistant"]
