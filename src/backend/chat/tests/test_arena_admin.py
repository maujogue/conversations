"""Arena admin: results page permission and the demo seed command."""

# pylint: disable=redefined-outer-name, unused-argument

from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.urls import reverse

import httpx
import pytest

from core.factories import UserFactory

from chat.admin import ArenaChallengerForm, ArenaExperimentForm, _provider_model_ids
from chat.enums import ArenaComparisonStatus
from chat.factories import ArenaChallengerFactory, ArenaExperimentFactory
from chat.llm_configuration import LLModel, LLMProvider
from chat.models import ArenaComparison

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
    assert b"Position check" in response.content


def test_results_page_seed_toggle(client, experiment):
    """Seeded rows show a banner and can be excluded with ?seed=0."""
    superuser = UserFactory(is_staff=True, is_superuser=True, admin_email="root@example.com")
    client.force_login(superuser)
    call_command("seed_arena_comparisons", experiment=str(experiment.pk), count=20, seed=1)
    url = reverse("admin:chat_arenaexperiment_results", args=[experiment.pk])

    with_seed = client.get(url)
    without_seed = client.get(url + "?seed=0")

    assert b"seed data" in with_seed.content
    assert with_seed.context["results"]["header"]["total"] == 20
    assert without_seed.context["results"]["header"]["total"] == 0


def test_seed_command_creates_flagged_rows(experiment):
    """The seed command produces the requested number of is_seed comparisons."""
    call_command(
        "seed_arena_comparisons",
        experiment=experiment.name,
        count=50,
        challenger_win_rate=1.0,
        vote_rate=1.0,
        error_rate=0.0,
        seed=7,
    )

    rows = ArenaComparison.objects.filter(experiment=experiment)
    assert rows.count() == 50
    assert rows.filter(is_seed=False).count() == 0
    assert rows.filter(status=ArenaComparisonStatus.VOTED, winner="challenger").count() == 50


def test_admin_forms_offer_configured_models():
    """Champion and challenger dropdowns list configured models, provider extras greyed."""
    form = ArenaExperimentForm()
    assert [value for value, _ in form.fields["champion_model_hrid"].choices] == [
        "main-model",
        "challenger-model",
    ]

    with patch("chat.admin._provider_model_ids", return_value=["main-model-llm", "brand-new"]):
        challenger_form = ArenaChallengerForm()

    choices = challenger_form.fields["model_hrid"].choices
    assert choices[:2] == [
        ("main-model", "main-model (main-model)"),
        ("challenger-model", "challenger-model (challenger-model)"),
    ]
    # Already configured provider models are not repeated; unknown ones are greyed.
    group_label, extras = choices[2]
    assert "not configured" in group_label
    assert extras == [("__unconfigured__brand-new", "brand-new")]
    assert challenger_form.fields["model_hrid"].widget.disabled_values == {
        "__unconfigured__brand-new"
    }


def test_provider_model_list_failure_is_silent():
    """An unreachable provider leaves the dropdown with the configured models only."""
    with patch("chat.admin.httpx.get", side_effect=httpx.ConnectError("boom")):
        assert _provider_model_ids() == []
