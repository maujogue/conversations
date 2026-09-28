"""Arena admin: results page permission and the demo seed command."""

# pylint: disable=redefined-outer-name, unused-argument

from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.core.management import call_command
from django.urls import reverse

import httpx
import pytest

from core.factories import UserFactory

from chat.admin import ArenaChallengerForm, ArenaExperimentForm, _provider_model_ids
from chat.enums import ArenaComparisonStatus
from chat.factories import (
    ArenaChallengerFactory,
    ArenaComparisonFactory,
    ArenaExperimentFactory,
)
from chat.llm_configuration import LLModel, LLMProvider
from chat.models import ArenaComparison, RoutingTierHistory, RoutingTierSettings

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
def tier_settings():
    """Standard tier on ``main-model`` with ``challenger-model`` as its alternative."""
    cache.clear()
    settings_row = RoutingTierSettings.get_solo()
    settings_row.standard_model_hrid = "main-model"
    settings_row.standard_alternatives = ["challenger-model"]
    settings_row.save()
    yield settings_row
    cache.clear()


@pytest.fixture
def experiment(tier_settings):
    """A standard-tier experiment with one challenger."""
    experiment = ArenaExperimentFactory()
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
    """The challenger dropdown lists configured models, provider extras greyed."""
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


def _promote_url(experiment):
    return reverse("admin:chat_arenaexperiment_promote", args=[experiment.pk])


def test_results_page_shows_the_tag_tables_and_the_verdict(client, experiment):
    """The three tag tables and the verdict column are rendered."""
    superuser = UserFactory(is_staff=True, is_superuser=True, admin_email="root@example.com")
    client.force_login(superuser)
    ArenaComparisonFactory(
        experiment=experiment,
        challenger_model_hrid="challenger-model",
        status=ArenaComparisonStatus.VOTED,
        winner="challenger",
        domain="legal",
        task="writing",
        context_tags=["plain"],
    )

    response = client.get(reverse("admin:chat_arenaexperiment_results", args=[experiment.pk]))

    assert response.status_code == 200
    content = response.content.decode()
    assert "Win rate by domain" in content
    assert "Win rate by task" in content
    assert "Win rate by context" in content
    assert response.context["results"]["challengers"][0]["verdict"] == "indicative"
    assert "Promote" in content


def test_promotion_requires_its_own_permission(client, experiment, tier_settings):
    """Reading results and promoting a challenger are two different rights."""
    staff = UserFactory(is_staff=True, admin_email="staff@example.com")
    staff.user_permissions.add(Permission.objects.get(codename="view_arena_results"))
    client.force_login(staff)

    assert (
        client.post(_promote_url(experiment), {"model_hrid": "challenger-model"}).status_code == 403
    )

    staff.user_permissions.add(Permission.objects.get(codename="promote_routing_champion"))
    response = client.post(_promote_url(experiment), {"model_hrid": "challenger-model"})

    assert response.status_code == 302
    tier_settings.refresh_from_db()
    assert tier_settings.standard_model_hrid == "challenger-model"
    history = RoutingTierHistory.objects.get()
    assert (history.reason, history.to_value, history.changed_by) == (
        "promotion",
        "challenger-model",
        staff,
    )


def test_promotion_is_post_only(client, experiment):
    """A GET on the promotion URL never changes what production runs."""
    superuser = UserFactory(is_staff=True, is_superuser=True, admin_email="root@example.com")
    client.force_login(superuser)

    assert client.get(_promote_url(experiment)).status_code == 403
    assert RoutingTierHistory.objects.count() == 0


def test_promotion_of_an_unknown_model_is_reported(client, experiment, tier_settings):
    """A model that is not a challenger leaves the tier settings alone."""
    superuser = UserFactory(is_staff=True, is_superuser=True, admin_email="root@example.com")
    client.force_login(superuser)

    response = client.post(_promote_url(experiment), {"model_hrid": "unknown-model"}, follow=True)

    assert response.status_code == 200
    tier_settings.refresh_from_db()
    assert tier_settings.standard_model_hrid == "main-model"
    assert RoutingTierHistory.objects.count() == 0


# --------------------------------------------------------------------------- #
# Results page cleanup (router spec 12, PR 20)
# --------------------------------------------------------------------------- #


def _results(client, experiment):
    superuser = UserFactory(is_staff=True, is_superuser=True, admin_email="root@example.com")
    client.force_login(superuser)
    return client.get(reverse("admin:chat_arenaexperiment_results", args=[experiment.pk]))


def test_results_page_drops_what_the_langfuse_dashboard_shows(client, experiment, settings):
    """Mean latency, tokens, CO2 and the cost total moved to the Router dashboard."""
    settings.LANGFUSE_ENABLED = True
    settings.LANGFUSE_HOST = "https://langfuse.example.gouv.fr"
    ArenaComparisonFactory(
        experiment=experiment,
        status=ArenaComparisonStatus.VOTED,
        winner="challenger",
        champion_latency_ms=1000,
        champion_completion_tokens=200,
        champion_co2_impact=0.0001,
    )

    content = _results(client, experiment).content.decode()

    for removed in ("Recorded Arena cost", "First token", "Tokens out", "CO₂ / answer"):
        assert removed not in content
    assert ">Latency<" not in content
    # The scoreboard is the promotion decision: no observability column is left on it.
    scoreboard = content.split("Challengers against the champion")[1].split("</table>")[0]
    for removed in (
        "<th>Both good</th>",
        "<th>Both bad</th>",
        "<th>Abandoned</th>",
        "<th>Errored</th>",
        "<th>Cost / answer</th>",
        "<th>Champion cost / answer</th>",
        "<th>Wh / answer</th>",
        "<th>Time to vote</th>",
        "<th>Second opinions</th>",
    ):
        assert removed not in scoreboard
    for kept in ("<th>Decisive votes</th>", "<th>Cost ratio</th>", "<th>Wh ratio</th>"):
        assert kept in scoreboard
    # The two per-comparison figures moved to the KPI strip above the table, once.
    assert content.count("Time to vote") == 1
    assert content.count("Champion Wh / answer") == 1
    # What the page keeps: counts, scoreboard, energy, verdicts, position check.
    assert "Wh / answer" in content
    assert "Decisive votes" in content
    assert "Position check" in content
    assert "Win rate by domain" in content
    assert "Promote" in content
    # ... and a way out to the descriptive dashboard.
    assert "Explorer dans Langfuse" in content
    assert "https://langfuse.example.gouv.fr" in content
    # No second opinion was asked for on this experiment: no table for them either.
    assert "<h2>Second opinions</h2>" not in content


def test_second_opinions_have_their_own_table(client, experiment):
    """A different population: shown apart, never a column of the scoreboard."""
    ArenaComparisonFactory(
        experiment=experiment,
        challenger_model_hrid="challenger-model",
        origin="manual",
        status=ArenaComparisonStatus.VOTED,
        winner="challenger",
    )

    content = _results(client, experiment).content.decode()

    table = content.split("<h2>Second opinions</h2>")[1].split("</table>")[0]
    assert "challenger-model" in table


def test_results_page_without_langfuse_says_so(client, experiment, settings):
    settings.LANGFUSE_ENABLED = False

    content = _results(client, experiment).content.decode()

    assert "Explorer dans Langfuse" not in content
    assert "Langfuse is disabled" in content


def test_admin_forms_have_no_price_fields():
    """Prices are declared once, in the LLM configuration (router spec 12)."""
    with patch("chat.admin._provider_model_ids", return_value=[]):
        challenger_fields = list(ArenaChallengerForm().fields)
    assert not [name for name in challenger_fields if name.endswith("price_eur_per_mtok")]
    assert not [
        name for name in ArenaExperimentForm().fields if name.endswith("price_eur_per_mtok")
    ]


def test_the_experiment_change_form_still_renders(client, experiment):
    """The champion fieldset lost its price inputs but the page must stay usable."""
    superuser = UserFactory(is_staff=True, is_superuser=True, admin_email="root@example.com")
    client.force_login(superuser)

    with patch("chat.admin._provider_model_ids", return_value=[]):
        response = client.get(reverse("admin:chat_arenaexperiment_change", args=[experiment.pk]))

    assert response.status_code == 200
    assert b"price_eur_per_mtok" not in response.content
