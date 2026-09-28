"""Tests for the LLM configuration view (docs/llm-router-spec.md section 6)."""

import pytest
from rest_framework import status

from core.factories import UserFactory
from core.feature_flags.flags import FeatureFlags, FeatureToggle

from chat.enums import RoutingTier
from chat.llm_configuration import LLModel, LLMProvider
from chat.models import RoutingTierSettings
from chat.views.llm_config import energy_ratio

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("clear_cache")]


def _llm(hrid, name, **overrides) -> LLModel:
    values = {
        "hrid": hrid,
        "model_name": f"{hrid}-name",
        "human_readable_name": name,
        "is_active": True,
        "icon": "",
        "system_prompt": "You are an assistant.",
        "tools": [],
        "provider": LLMProvider(hrid="unused", base_url="https://example.com", api_key="key"),
    }
    values.update(overrides)
    return LLModel(**values)


@pytest.fixture(name="llm_configurations")
def llm_configurations_fixture(settings):
    """Three chat models (one inactive), one utility model."""
    settings.LLM_CONFIGURATIONS = {
        "model-1": _llm(
            "model-1", "Amazing LLM", icon="base64encodediconstring", supports_image=True
        ),
        "model-2": _llm("model-2", "Another LLM"),
        "model-3": _llm("model-3", "Retired LLM", is_active=False),
        "summarizer": _llm("summarizer", "Summarizer", role="utility"),
    }
    settings.LLM_DEFAULT_MODEL_HRID = "model-1"
    settings.LLM_TIER_SIMPLE_MODEL_HRID = ""
    settings.LLM_TIER_STANDARD_MODEL_HRID = ""
    settings.LLM_TIER_COMPLEX_MODEL_HRID = ""


@pytest.fixture(name="picker_flag")
def picker_flag_fixture(settings):
    def _set(enabled: bool):
        settings.FEATURE_FLAGS = FeatureFlags(
            dev_model_picker=FeatureToggle.ENABLED if enabled else FeatureToggle.DISABLED
        )

    return _set


AUTO_ENTRY = {"slug": "auto", "label_key": "router.tier.auto", "recommended": True}


def test_llm_configuration_view_unauthenticated(api_client):
    """Unauthenticated access is denied."""
    response = api_client.get("/api/v1.0/llm-configuration/")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_regular_user_gets_tiers_only(api_client, llm_configurations, picker_flag):  # pylint: disable=unused-argument
    """Every tier resolves to the default model: three tiers, no model names anywhere."""
    picker_flag(True)  # the flag alone is not enough: the requester must be staff
    api_client.force_authenticate(user=UserFactory())
    response = api_client.get("/api/v1.0/llm-configuration/")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {
        "mode": "tiers",
        "tiers": [
            AUTO_ENTRY,
            {"slug": "simple", "label_key": "router.tier.simple", "leaves": 1, "energy_ratio": 1.0},
            {
                "slug": "standard",
                "label_key": "router.tier.standard",
                "leaves": 2,
                "energy_ratio": 5.0,
            },
            {
                "slug": "complex",
                "label_key": "router.tier.complex",
                "leaves": 3,
                "energy_ratio": 10.0,
            },
        ],
    }
    assert "model" not in response.content.decode().replace("mode", "")


def test_tier_without_a_configured_model_is_omitted(api_client, llm_configurations):  # pylint: disable=unused-argument
    """A tier whose model is missing or inactive disappears; Auto stays."""
    tier_settings = RoutingTierSettings.get_solo()
    tier_settings.simple_model_hrid = "ghost"
    tier_settings.complex_model_hrid = "model-3"
    tier_settings.tier_energy = {
        "simple": {"wh_per_answer": 0.5},
        "standard": {"wh_per_answer": 4.25},
    }
    tier_settings.save()
    api_client.force_authenticate(user=UserFactory())
    response = api_client.get("/api/v1.0/llm-configuration/")

    assert response.status_code == status.HTTP_200_OK
    assert [tier["slug"] for tier in response.json()["tiers"]] == ["auto", "standard"]
    assert response.json()["tiers"][1]["energy_ratio"] == 8.5


def test_staff_with_flag_get_the_chat_models(api_client, llm_configurations, picker_flag):  # pylint: disable=unused-argument
    """Staff with `dev_model_picker` also receive the raw list, chat models only."""
    picker_flag(True)
    api_client.force_authenticate(user=UserFactory(is_staff=True))
    response = api_client.get("/api/v1.0/llm-configuration/")

    assert response.status_code == status.HTTP_200_OK
    payload = response.json()
    assert payload["mode"] == "tiers"
    assert [tier["slug"] for tier in payload["tiers"]] == ["auto", "simple", "standard", "complex"]
    assert payload["models"] == [
        {
            "hrid": "model-1",
            "human_readable_name": "Amazing LLM",
            "icon": "base64encodediconstring",
            "is_active": True,
            "is_default": True,
            "model_name": "model-1-name",
            "supports_image": True,
        },
        {
            "hrid": "model-2",
            "human_readable_name": "Another LLM",
            "icon": "",
            "is_active": True,
            "is_default": False,
            "model_name": "model-2-name",
            "supports_image": False,
        },
        {
            "hrid": "model-3",
            "human_readable_name": "Retired LLM",
            "icon": "",
            "is_active": False,
            "is_default": False,
            "model_name": "model-3-name",
            "supports_image": False,
        },
    ]


def test_staff_without_flag_get_no_models(api_client, llm_configurations, picker_flag):  # pylint: disable=unused-argument
    """The flag is off in production: staff see the tiers like everyone else."""
    picker_flag(False)
    api_client.force_authenticate(user=UserFactory(is_staff=True))
    response = api_client.get("/api/v1.0/llm-configuration/")
    assert response.status_code == status.HTTP_200_OK
    assert "models" not in response.json()


@pytest.mark.parametrize(
    "tier_energy,tier,expected",
    [
        ({}, RoutingTier.STANDARD, 5.0),
        ({"simple": {"wh_per_answer": 1.0}}, RoutingTier.COMPLEX, 10.0),
        (
            {"simple": {"wh_per_answer": 0}, "complex": {"wh_per_answer": 5}},
            RoutingTier.COMPLEX,
            10.0,
        ),
        (
            {"simple": {"wh_per_answer": 2.0}, "complex": {"wh_per_answer": 25.0}},
            RoutingTier.COMPLEX,
            12.5,
        ),
        ({"simple": {"wh_per_answer": "bad"}}, RoutingTier.SIMPLE, 1.0),
        ({"simple": {"wh_per_answer": 3.0}}, RoutingTier.SIMPLE, 1.0),
    ],
)
def test_energy_ratio(tier_energy, tier, expected):
    """The ratio comes from tier_energy and falls back to the EcoLogits starting values."""
    assert energy_ratio(tier_energy, tier) == expected
