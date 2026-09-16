"""Tests for the seed_routing_tiers management command."""

from io import StringIO

from django.core.management import call_command

import pytest

from chat.llm_configuration import LLModel, LLMProvider
from chat.models import RoutingTierSettings

pytestmark = pytest.mark.django_db


def _llm(hrid, **overrides) -> LLModel:
    values = {
        "hrid": hrid,
        "model_name": f"{hrid}-name",
        "human_readable_name": hrid,
        "is_active": True,
        "system_prompt": "hi",
        "tools": [],
        "provider": LLMProvider(hrid="albert", base_url="https://example.com", api_key="k"),
    }
    values.update(overrides)
    return LLModel(**values)


@pytest.fixture(autouse=True)
def partial_configuration(settings):
    """Some of the spec's models are configured, some are missing, one is a utility."""
    settings.LLM_CONFIGURATIONS = {
        "ministral-3-8b": _llm("ministral-3-8b"),
        "mistral-small-3-2": _llm("mistral-small-3-2"),
        "mistral-medium-3-5": _llm("mistral-medium-3-5", role="utility"),
        "gpt-oss-120b": _llm("gpt-oss-120b", reasoning_control="levels"),
    }


def test_seed_fills_blanks_and_skips_unconfigured_models():
    """Missing and utility models are skipped; the rest is written and validated."""
    out = StringIO()
    call_command("seed_routing_tiers", stdout=out)

    tier_settings = RoutingTierSettings.get_solo()
    assert tier_settings.simple_model_hrid == "ministral-3-8b"
    assert tier_settings.simple_alternatives == ["mistral-small-3-2"]
    assert tier_settings.standard_model_hrid == "mistral-small-3-2"
    assert tier_settings.standard_alternatives == []
    assert tier_settings.complex_model_hrid == "gpt-oss-120b"
    assert tier_settings.complex_alternatives == []
    assert "skipped=['gemma-4-31b', 'mistral-medium-3-5']" in out.getvalue()
    assert "Routing tier settings saved." in out.getvalue()


def test_seed_keeps_admin_values_unless_forced():
    tier_settings = RoutingTierSettings.get_solo()
    tier_settings.simple_model_hrid = "mistral-small-3-2"
    tier_settings.save()

    call_command("seed_routing_tiers", stdout=StringIO())
    tier_settings.refresh_from_db()
    assert tier_settings.simple_model_hrid == "mistral-small-3-2"
    assert tier_settings.complex_model_hrid == "gpt-oss-120b"

    call_command("seed_routing_tiers", "--force", stdout=StringIO())
    tier_settings.refresh_from_db()
    assert tier_settings.simple_model_hrid == "ministral-3-8b"
