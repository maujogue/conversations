"""Arena admin: results page permission and the model dropdowns."""

# pylint: disable=redefined-outer-name, unused-argument

from django.contrib.auth.models import Permission
from django.urls import reverse

import pytest

from core.factories import UserFactory

from chat.admin import ArenaChallengerForm, ArenaExperimentForm
from chat.enums import ArenaComparisonStatus
from chat.factories import (
    ArenaChallengerFactory,
    ArenaComparisonFactory,
    ArenaExperimentFactory,
)
from chat.llm_configuration import LLModel, LLMProvider

pytestmark = pytest.mark.django_db


def _make_llm(hrid: str) -> LLModel:
    return LLModel(
        hrid=hrid,
        model_name=f"{hrid}-llm",
        human_readable_name=hrid,
        is_active=True,
        system_prompt="You are a helpful assistant.",
        tools=[],
        provider=LLMProvider(
            hrid="albert", base_url="https://www.external-ai-service.com/", api_key="k"
        ),
    )


@pytest.fixture(autouse=True)
def models_settings(settings):
    """Two configured models, the default one being the champion."""
    settings.LLM_CONFIGURATIONS = {
        "main-model": _make_llm("main-model"),
        "challenger-model": _make_llm("challenger-model"),
    }
    settings.LLM_DEFAULT_MODEL_HRID = "main-model"


@pytest.fixture
def experiment():
    """An experiment with one challenger."""
    experiment = ArenaExperimentFactory(champion_model_hrid="main-model")
    ArenaChallengerFactory(experiment=experiment, model_hrid="challenger-model")
    return experiment


def test_results_page_requires_dedicated_permission(client, experiment):
    """Staff without the permission get a 403; with it, the page renders the challenger."""
    staff = UserFactory(is_staff=True, admin_email="staff@example.com")
    client.force_login(staff)
    url = reverse("admin:chat_arenaexperiment_results", args=[experiment.pk])

    assert client.get(url).status_code == 403

    staff.user_permissions.add(Permission.objects.get(codename="view_arena_results"))
    response = client.get(url)

    assert response.status_code == 200
    assert b"challenger-model" in response.content


def test_results_page_shows_challenger_win_rate(client, experiment):
    """The scoreboard counts the votes each challenger won against the champion."""
    superuser = UserFactory(is_staff=True, is_superuser=True, admin_email="root@example.com")
    client.force_login(superuser)
    for winner in ("challenger", "challenger", "champion"):
        ArenaComparisonFactory(
            experiment=experiment, status=ArenaComparisonStatus.VOTED, winner=winner
        )
    ArenaComparisonFactory(experiment=experiment, status=ArenaComparisonStatus.ABANDONED)

    response = client.get(reverse("admin:chat_arenaexperiment_results", args=[experiment.pk]))

    results = response.context["results"]
    assert results["header"]["total"] == 4
    assert results["header"]["voted"] == 3
    assert results["header"]["abandoned"] == 1
    [row] = results["challengers"]
    assert row["model_hrid"] == "challenger-model"
    assert (row["votes"], row["wins"], row["comparisons"]) == (3, 2, 4)
    assert row["indicative"] is True


def test_admin_forms_offer_configured_models():
    """Champion and challenger dropdowns list the active configured models."""
    for form, field in (
        (ArenaExperimentForm(), "champion_model_hrid"),
        (ArenaChallengerForm(), "model_hrid"),
    ):
        assert form.fields[field].choices == [
            ("main-model", "main-model (main-model)"),
            ("challenger-model", "challenger-model (challenger-model)"),
        ]
