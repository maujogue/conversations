"""Arena endpoints: draw, candidate streams that persist nothing, vote, auto-resolution."""

# pylint: disable=redefined-outer-name, unused-argument

import json
from unittest.mock import patch

from django.core.cache import cache
from django.utils import timezone

import pytest
import respx
from freezegun import freeze_time
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from rest_framework import status

from core.factories import UserFactory
from core.feature_flags.flags import FeatureFlags, FeatureToggle

from chat import arena
from chat.ai_sdk_types import TextUIPart, UIMessage
from chat.enums import (
    ArenaComparisonStatus,
    ArenaRole,
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
    ChatConversationFactory,
)
from chat.llm_configuration import LLModel, LLMProvider
from chat.models import ArenaComparison, ArenaExperiment, RoutingTierSettings
from chat.router.labels import RoutingDecision, RoutingLabels
from chat.views.conversations import ChatViewSet

pytestmark = pytest.mark.django_db(transaction=True)

FROZEN = "2025-07-25T10:36:35.297675Z"


def _make_llm(hrid: str) -> LLModel:
    return LLModel(
        hrid=hrid,
        model_name=f"{hrid}-llm",
        human_readable_name=hrid,
        is_active=True,
        web_search="chat.tools.web_search_brave.web_search_brave",
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
def tier_settings():
    """Standard tier on ``main-model`` with ``challenger-model`` as its alternative."""
    settings_row = RoutingTierSettings.get_solo()
    settings_row.standard_model_hrid = "main-model"
    settings_row.standard_alternatives = ["challenger-model"]
    settings_row.save()
    return settings_row


@pytest.fixture
def experiment(tier_settings):
    """An active standard-tier experiment drawing on every turn."""
    experiment = ArenaExperimentFactory(is_active=True, sampling_rate=1.0)
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
    assert body["acknowledgement"] == {
        "user_votes": 1,
        "experiment_votes": 1,
        "tier_label": None,
        "task_label": None,
        "domain_label": None,
        "milestone": "first_vote",
    }
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
    body = response.json()
    assert body["messages"][-1]["content"] == "Champion answer"
    assert body["acknowledgement"] is None
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
    body = response.json()
    assert body["messages"][-1]["content"] == "Champion answer"
    assert body["acknowledgement"]["user_votes"] == 1
    assert body["acknowledgement"]["experiment_votes"] == 1
    assert body["acknowledgement"]["milestone"] == "first_vote"
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


@freeze_time(FROZEN)
@respx.mock
def test_champion_first_and_changed_candidate_input_use_draw_snapshot(
    api_client, experiment, mock_openai_stream_multi_calls, hello_conversation_data
):
    """Sequential streams get identical history, question and search settings."""
    conversation = ChatConversationFactory(owner__language="en-us")
    api_client.force_login(conversation.owner)
    message = hello_conversation_data["messages"][-1]
    draw = api_client.post(
        f"/api/v1.0/chats/{conversation.pk}/arena/draw/",
        {"message": message, "force_web_search": False},
        format="json",
    ).json()
    comparison = ArenaComparison.objects.get(pk=draw["comparison_id"])
    base = f"/api/v1.0/chats/{conversation.pk}/conversation/?arena_comparison={comparison.pk}"
    _drain(
        api_client.post(
            f"{base}&arena_side={comparison.champion_side}", hello_conversation_data, format="json"
        )
    )
    changed = {"messages": [_ui_message("different-id", "user", "A different question")]}
    _drain(
        api_client.post(
            f"{base}&arena_side={comparison.challenger_side}&force_web_search=true",
            changed,
            format="json",
        )
    )
    bodies = [json.loads(call.request.content) for call in mock_openai_stream_multi_calls.calls]
    assert len(bodies) == 2
    assert bodies[0]["messages"] == bodies[1]["messages"]
    assert "A different question" not in str(bodies)
    assert sum(m["role"] == "user" for m in bodies[1]["messages"]) == 1


@freeze_time(FROZEN)
@respx.mock
def test_duplicate_request_is_rejected_before_first_stream_is_consumed(
    api_client, experiment, mock_openai_stream_multi_calls, hello_conversation_data
):
    """An unfinished candidate is already claimed before a 200 response is returned."""
    conversation = ChatConversationFactory(owner__language="en-us")
    api_client.force_login(conversation.owner)
    draw = api_client.post(
        f"/api/v1.0/chats/{conversation.pk}/arena/draw/", {}, format="json"
    ).json()
    url = (
        f"/api/v1.0/chats/{conversation.pk}/conversation/"
        f"?arena_comparison={draw['comparison_id']}&arena_side=left"
    )
    first = api_client.post(url, hello_conversation_data, format="json")
    duplicate = api_client.post(url, hello_conversation_data, format="json")
    assert first.status_code == 200
    assert duplicate.status_code == 409
    _drain(first)
    assert len(mock_openai_stream_multi_calls.calls) == 1


def test_restored_self_documentation_is_anonymized(api_client, experiment):
    """Legacy tool outputs are redacted on restore, not just in new inference."""
    conversation = ChatConversationFactory(messages=[])
    api_client.force_login(conversation.owner)
    payload = _payload("a1", "Answer")
    payload["output_ui_message"]["parts"].append(
        {
            "type": "tool-self_documentation",
            "toolCallId": "t1",
            "state": "output-available",
            "input": {},
            "output": {"runtime": {"model": {"name": "secret-provider-model"}}},
        }
    )
    ArenaComparisonFactory(
        conversation=conversation,
        experiment=experiment,
        champion_finished_at=timezone.now(),
        champion_payload=payload,
        challenger_finished_at=timezone.now(),
        challenger_payload=payload,
    )
    body = api_client.get(f"/api/v1.0/chats/{conversation.pk}/").json()
    assert body["pending_arena_comparison"]["restorable"]
    assert "secret-provider-model" not in json.dumps(body)


def test_delete_endpoint_scrubs_arena_records(api_client, experiment):
    conversation = ChatConversationFactory()
    api_client.force_login(conversation.owner)
    comparison = ArenaComparisonFactory(
        conversation=conversation,
        experiment=experiment,
        champion_payload=_payload("a1", "Private"),
        input_snapshot={"prompt": "Private"},
    )
    response = api_client.delete(f"/api/v1.0/chats/{conversation.pk}/")
    assert response.status_code == 204
    comparison.refresh_from_db()
    assert comparison.user_id is None and comparison.conversation_id is None
    assert comparison.champion_payload is None and comparison.input_snapshot is None


def test_stop_endpoint_closes_comparison_before_a_late_result(api_client, experiment):
    conversation = ChatConversationFactory()
    api_client.force_login(conversation.owner)
    comparison = ArenaComparisonFactory(conversation=conversation, experiment=experiment)
    response = api_client.post(
        f"/api/v1.0/chats/{conversation.pk}/stop-streaming/", {}, format="json"
    )
    assert response.status_code == 200
    arena.record_side_result(
        comparison, ArenaRole.CHAMPION, payload=_payload("late", "Late answer")
    )
    comparison.refresh_from_db()
    conversation.refresh_from_db()
    assert comparison.status == ArenaComparisonStatus.ERRORED
    assert comparison.closed_reason == "cancelled"
    assert comparison.champion_payload is None
    assert conversation.messages == []


def test_self_documentation_tool_is_blind_in_live_stream_and_saved_payload(
    api_client, experiment, mock_ai_agent_service, hello_conversation_data
):
    """Exercise the real registered tool and its SDK events through both candidates."""

    seen = []

    async def provider(messages, _info):
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not returns:
            yield {0: DeltaToolCall(name="self_documentation", json_args="{}", tool_call_id="doc")}
        else:
            seen.append(returns[-1].content)
            yield "I am an AI assistant."

    conversation = ChatConversationFactory(owner__language="en-us")
    api_client.force_login(conversation.owner)
    draw = api_client.post(
        f"/api/v1.0/chats/{conversation.pk}/arena/draw/", {}, format="json"
    ).json()
    comparison = ArenaComparison.objects.get(pk=draw["comparison_id"])
    with mock_ai_agent_service(FunctionModel(stream_function=provider)):
        for side in (comparison.champion_side, comparison.challenger_side):
            response = api_client.post(
                f"/api/v1.0/chats/{conversation.pk}/conversation/"
                f"?arena_comparison={comparison.pk}&arena_side={side}",
                hello_conversation_data,
                format="json",
            )
            assert response.status_code == 200
            stream = _drain(response).decode()
            assert "self_documentation" in stream
            for identity in ("main-model", "challenger-model", "provider_hrid", "albert"):
                assert identity not in stream
    comparison.refresh_from_db()
    assert len(seen) == 2 and seen[0] == seen[1]
    for side in ("left", "right"):
        payload = comparison.payload_for_side(side)
        assert payload is not None
        assert "provider_hrid" not in json.dumps(payload)


# --------------------------------------------------------------------------- #
# Second opinion on demand (router spec 8.2)
# --------------------------------------------------------------------------- #


@pytest.fixture
def manual_settings(settings, tier_settings):
    """Second opinions enabled, with two alternatives on the standard tier."""
    settings.LLM_CONFIGURATIONS["second-challenger"] = _make_llm("second-challenger")
    tier_settings.standard_alternatives = ["challenger-model", "second-challenger"]
    tier_settings.save()
    settings.FEATURE_FLAGS = FeatureFlags(
        arena=FeatureToggle.ENABLED, arena_manual=FeatureToggle.ENABLED
    )
    return tier_settings


def _answered_conversation():
    """A conversation whose last turn was answered by the standard tier model."""
    return ChatConversationFactory(
        owner__language="en-us",
        model_hrid="main-model",
        messages=[
            UIMessage(
                id="u1", role="user", content="Hi", parts=[TextUIPart(type="text", text="Hi")]
            ),
            UIMessage(
                id="a1",
                role="assistant",
                content="First answer",
                parts=[TextUIPart(type="text", text="First answer")],
            ),
        ],
        pydantic_messages=[
            {"kind": "request", "parts": [{"part_kind": "user-prompt", "content": "Hi"}]},
            {"kind": "response", "parts": [{"part_kind": "text", "content": "First answer"}]},
        ],
        last_routing={"tier": "standard", "labels": {"domain": "legal", "task": "writing"}},
    )


def test_manual_comparison_copies_the_committed_answer(api_client, experiment, manual_settings):
    """The endpoint returns the comparison and the single side left to stream."""
    conversation = _answered_conversation()
    api_client.force_login(conversation.owner)

    response = api_client.post(
        f"/api/v1.0/chats/{conversation.pk}/arena/manual/", {"message_id": "a1"}, format="json"
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert set(body) == {"comparison_id", "side"}
    comparison = ArenaComparison.objects.get(pk=body["comparison_id"])
    assert body["side"] == comparison.challenger_side
    assert comparison.origin == "manual"
    assert comparison.tier == "standard"
    assert (comparison.domain, comparison.task) == ("legal", "writing")
    assert comparison.champion_model_hrid == "main-model"
    assert comparison.challenger_model_hrid in {"challenger-model", "second-challenger"}
    # The champion side is already done: its answer is the one in the conversation.
    assert comparison.champion_committed is True
    assert comparison.side_succeeded(ArenaRole.CHAMPION)
    assert comparison.champion_payload["output_ui_message"]["content"] == "First answer"
    # The challenger replays the same question on the history before the answer.
    assert comparison.input_snapshot["request_ui_message"]["content"] == "Hi"
    assert comparison.input_snapshot["messages"] == []
    assert comparison.input_snapshot["pydantic_messages"] == []
    assert "main-model" not in str(body)


def test_manual_comparison_works_without_an_active_experiment(
    api_client, manual_settings, tier_settings
):
    """A second opinion is a user action: it must not need an admin experiment."""
    conversation = _answered_conversation()
    api_client.force_login(conversation.owner)

    response = api_client.post(
        f"/api/v1.0/chats/{conversation.pk}/arena/manual/", {"message_id": "a1"}, format="json"
    )

    assert response.status_code == status.HTTP_200_OK
    comparison = ArenaComparison.objects.get(pk=response.json()["comparison_id"])
    assert comparison.origin == "manual"
    assert comparison.tier == "standard"
    # Filed under the per-tier container, which never draws on its own.
    assert comparison.experiment.name == "Second opinion (standard)"
    assert comparison.experiment.is_active is False
    assert comparison.experiment.sampling_rate == 0

    # A second request reuses the same container instead of piling rows up.
    api_client.post(
        f"/api/v1.0/chats/{conversation.pk}/arena/manual/", {"message_id": "a1"}, format="json"
    )
    assert ArenaExperiment.objects.filter(name="Second opinion (standard)").count() == 1


def test_manual_comparison_needs_the_feature_flag(api_client, settings, experiment, tier_settings):
    """Without ``arena_manual`` the endpoint is closed."""
    settings.FEATURE_FLAGS = FeatureFlags(arena=FeatureToggle.ENABLED)
    conversation = _answered_conversation()
    api_client.force_login(conversation.owner)

    response = api_client.post(
        f"/api/v1.0/chats/{conversation.pk}/arena/manual/", {"message_id": "a1"}, format="json"
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert not ArenaComparison.objects.exists()


def test_manual_comparison_only_on_the_last_answer(api_client, experiment, manual_settings):
    """An older message cannot be compared: the button lives on the last answer."""
    conversation = _answered_conversation()
    api_client.force_login(conversation.owner)

    response = api_client.post(
        f"/api/v1.0/chats/{conversation.pk}/arena/manual/", {"message_id": "u1"}, format="json"
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json() == {"error": "arena_manual_not_last_message"}


def test_manual_comparison_is_repeatable_until_alternatives_run_out(
    api_client, experiment, manual_settings
):
    """Each call draws an untried alternative; when none is left the endpoint says so."""
    conversation = _answered_conversation()
    api_client.force_login(conversation.owner)
    url = f"/api/v1.0/chats/{conversation.pk}/arena/manual/"

    tried = set()
    for _ in range(2):
        body = api_client.post(url, {"message_id": "a1"}, format="json").json()
        comparison = ArenaComparison.objects.get(pk=body["comparison_id"])
        tried.add(comparison.challenger_model_hrid)
        # No daily cap: a second opinion is always allowed while models remain.
        arena.vote(comparison, None)

    assert tried == {"challenger-model", "second-challenger"}
    exhausted = api_client.post(url, {"message_id": "a1"}, format="json")
    assert exhausted.status_code == status.HTTP_409_CONFLICT
    assert exhausted.json() == {"error": "arena_manual_exhausted"}


@freeze_time(FROZEN)
@respx.mock
def test_manual_challenger_stream_and_vote_swaps_the_answer(
    api_client, experiment, manual_settings, mock_openai_stream_multi_calls, hello_conversation_data
):
    """Only the challenger streams; voting for it replaces the committed answer."""
    conversation = _answered_conversation()
    api_client.force_login(conversation.owner)
    body = api_client.post(
        f"/api/v1.0/chats/{conversation.pk}/arena/manual/", {"message_id": "a1"}, format="json"
    ).json()
    comparison = ArenaComparison.objects.get(pk=body["comparison_id"])

    response = api_client.post(
        f"/api/v1.0/chats/{conversation.pk}/conversation/"
        f"?arena_comparison={comparison.pk}&arena_side={body['side']}",
        hello_conversation_data,
        format="json",
    )
    assert response.status_code == status.HTTP_200_OK
    _drain(response)

    comparison.refresh_from_db()
    assert comparison.side_succeeded(ArenaRole.CHALLENGER)
    assert comparison.is_restorable()

    vote = api_client.post(
        f"/api/v1.0/chats/{conversation.pk}/arena/{comparison.pk}/vote/",
        {"side": comparison.challenger_side},
        format="json",
    )

    assert vote.status_code == status.HTTP_200_OK
    comparison.refresh_from_db()
    assert comparison.status == ArenaComparisonStatus.VOTED
    assert comparison.winner == ArenaRole.CHALLENGER
    conversation.refresh_from_db()
    assert [m.role for m in conversation.messages] == ["user", "assistant"]
    assert conversation.messages[-1].content == "Hello there"
    # The old turn was swapped, not stacked on top of the previous answer.
    assert len(conversation.pydantic_messages) == 2


@pytest.mark.parametrize(("tier", "expected_arena"), [("standard", True), ("complex", False)])
def test_draw_routes_the_turn_and_matches_the_experiment_tier(
    api_client, settings, experiment, tier, expected_arena
):
    """With the router on, the draw classifies the turn and only draws on its own tier."""
    settings.FEATURE_FLAGS = FeatureFlags(arena=FeatureToggle.ENABLED, router=FeatureToggle.ENABLED)
    conversation = ChatConversationFactory()
    api_client.force_login(conversation.owner)
    decision = RoutingDecision(
        tier=RoutingTier(tier),
        tier_source=TierSource.ROUTER,
        model_hrid="main-model",
        reason=RoutingReason.CLASSIFIED,
        labels=RoutingLabels(
            complexity=RoutingTier(tier),
            domain=RoutingDomain.HR,
            task=RoutingTask.SUMMARIZATION,
            confidence=0.9,
        ),
        router_confidence=0.9,
    )

    with patch.object(ChatViewSet, "_route_turn", return_value=decision):
        response = api_client.post(
            f"/api/v1.0/chats/{conversation.pk}/arena/draw/",
            {"message": {"id": "u1", "role": "user", "parts": [{"type": "text", "text": "Hi"}]}},
            format="json",
        )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["arena"] is expected_arena
    if expected_arena:
        comparison = ArenaComparison.objects.get()
        assert (comparison.tier, comparison.domain, comparison.task) == (
            "standard",
            "hr",
            "summarization",
        )


# --- the pinned tier on an arena turn -------------------------------------------------


@pytest.fixture
def every_tier_configured(tier_settings):
    """A model on every tier: without one a tier falls through to the constraint
    fallback, which would mask the tier these tests are about."""
    tier_settings.simple_model_hrid = "main-model"
    tier_settings.complex_model_hrid = "challenger-model"
    tier_settings.save()
    return tier_settings


@pytest.fixture
def router_on(settings, every_tier_configured):
    """Arena and router both on: the draw routes the turn itself."""
    settings.FEATURE_FLAGS = FeatureFlags(arena=FeatureToggle.ENABLED, router=FeatureToggle.ENABLED)
    return every_tier_configured


@pytest.fixture
def router_off(settings, every_tier_configured):
    """Arena on, automatic routing off: only a hand-pinned tier decides the turn."""
    settings.FEATURE_FLAGS = FeatureFlags(
        arena=FeatureToggle.ENABLED, router=FeatureToggle.DISABLED
    )
    return every_tier_configured


def _classify_returning(labels):
    async def fake_classify(*_args, **_kwargs):
        return labels, RoutingReason.CLASSIFIED.value, 42, "7"

    return patch("chat.router.routing.classify", side_effect=fake_classify)


ROUTER_WOULD_PICK_SIMPLE = RoutingLabels(
    complexity=RoutingTier.SIMPLE,
    domain=RoutingDomain.HR,
    task=RoutingTask.SUMMARIZATION,
    confidence=0.95,
)

DRAW_MESSAGE = {"id": "u1", "role": "user", "parts": [{"type": "text", "text": "Hi"}]}


def test_draw_applies_the_tier_the_user_pinned(api_client, experiment, router_on):
    """The composer's tier travels with the draw and routes the comparison.

    An arena turn is routed here and its two streaming requests never carry the
    preference (the backend picks a model per side), so a tier pinned on such a
    turn would otherwise be ignored: the comparison would be drawn for the tier
    the classifier picked, and the pin never recorded on the conversation.
    """
    conversation = ChatConversationFactory()
    api_client.force_login(conversation.owner)
    captured = {}

    def capture(*, routing_decision, **_kwargs):
        # Returning None is "no comparison drawn": these tests are about the
        # decision handed to the draw, not about the comparison it builds.
        captured["decision"] = routing_decision

    with (
        _classify_returning(ROUTER_WOULD_PICK_SIMPLE),
        patch.object(arena, "draw_comparison", side_effect=capture),
    ):
        response = api_client.post(
            f"/api/v1.0/chats/{conversation.pk}/arena/draw/",
            {"message": DRAW_MESSAGE, "tier": "complex"},
            format="json",
        )

    assert response.status_code == status.HTTP_200_OK
    conversation.refresh_from_db()
    assert conversation.pinned_tier == "complex"
    decision = captured["decision"]
    assert (decision.tier, decision.tier_source) == (RoutingTier.COMPLEX, TierSource.USER)
    assert decision.router_would_pick == RoutingTier.SIMPLE


def test_draw_with_auto_releases_the_pin(api_client, experiment, router_on):
    """`auto` hands the decision back to the router, as on the streaming endpoint."""
    conversation = ChatConversationFactory(pinned_tier=RoutingTier.COMPLEX)
    api_client.force_login(conversation.owner)
    captured = {}

    def capture(*, routing_decision, **_kwargs):
        # Returning None is "no comparison drawn": these tests are about the
        # decision handed to the draw, not about the comparison it builds.
        captured["decision"] = routing_decision

    with (
        _classify_returning(ROUTER_WOULD_PICK_SIMPLE),
        patch.object(arena, "draw_comparison", side_effect=capture),
    ):
        response = api_client.post(
            f"/api/v1.0/chats/{conversation.pk}/arena/draw/",
            {"message": DRAW_MESSAGE, "tier": "auto"},
            format="json",
        )

    assert response.status_code == status.HTTP_200_OK
    conversation.refresh_from_db()
    assert conversation.pinned_tier is None
    assert captured["decision"].tier_source == TierSource.ROUTER


def test_draw_without_tier_leaves_the_pin_alone(api_client, experiment, router_on):
    """A client that sends no tier (pre-router shape) must not release a pin."""
    conversation = ChatConversationFactory(pinned_tier=RoutingTier.COMPLEX)
    api_client.force_login(conversation.owner)

    with (
        _classify_returning(ROUTER_WOULD_PICK_SIMPLE),
        patch.object(arena, "draw_comparison", side_effect=lambda **_kwargs: None),
    ):
        response = api_client.post(
            f"/api/v1.0/chats/{conversation.pk}/arena/draw/",
            {"message": DRAW_MESSAGE},
            format="json",
        )

    assert response.status_code == status.HTTP_200_OK
    conversation.refresh_from_db()
    assert conversation.pinned_tier == RoutingTier.COMPLEX


def test_draw_is_tier_specific_when_the_tier_is_pinned_and_the_router_off(
    api_client, experiment, router_off
):
    """A pinned tier picks the experiment even where automatic routing is off.

    The selector is offered to everyone, so the comparison has to belong to the
    mode the user chose: the standard-tier experiment here, rather than "the one
    active experiment" an unrouted turn would otherwise fall back to.
    """
    conversation = ChatConversationFactory()
    api_client.force_login(conversation.owner)

    with patch("chat.router.routing.classify") as classify:
        response = api_client.post(
            f"/api/v1.0/chats/{conversation.pk}/arena/draw/",
            {"message": DRAW_MESSAGE, "tier": "standard"},
            format="json",
        )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["arena"] is True
    classify.assert_not_called()
    comparison = ArenaComparison.objects.get()
    assert comparison.experiment == experiment
    assert comparison.tier == "standard"
    assert comparison.tier_source == TierSource.USER.value
    assert comparison.champion_model_hrid == "main-model"


def test_draw_skips_a_tier_with_no_experiment_instead_of_comparing_on_another(
    api_client, experiment, router_off
):
    """Only the pinned tier's experiment may draw: no experiment, no comparison."""
    conversation = ChatConversationFactory()
    api_client.force_login(conversation.owner)

    with patch("chat.router.routing.classify") as classify:
        response = api_client.post(
            f"/api/v1.0/chats/{conversation.pk}/arena/draw/",
            {"message": DRAW_MESSAGE, "tier": "complex"},
            format="json",
        )

    assert response.status_code == status.HTTP_200_OK
    # The only active experiment is the standard-tier one: a complex turn is normal.
    assert response.json() == {"arena": False}
    classify.assert_not_called()
    assert not ArenaComparison.objects.exists()
