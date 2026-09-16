"""Tests for llm_configuration module."""

import json

import django.conf

import pytest

from chat.llm_configuration import (
    LLMConfiguration,
    LLModel,
    LLMProvider,
    _get_setting_or_env_or_value,
    load_llm_configuration,
)


@pytest.fixture(autouse=True)
def setup_env(monkeypatch):
    """Set up environment variables and Django settings for the tests."""
    monkeypatch.setenv("TEST_ENV_VAR", "env_value")
    monkeypatch.setattr(django.conf.settings, "TEST_SETTING", "setting_value", raising=False)


def test_get_setting_or_env_or_value_env(monkeypatch):
    """Test retrieving value from environment variable."""
    monkeypatch.setenv("MY_ENV", "my_env_value")
    assert _get_setting_or_env_or_value("environ.MY_ENV") == "my_env_value"


def test_get_setting_or_env_or_value_setting(monkeypatch):
    """Test retrieving value from Django settings."""
    monkeypatch.setattr(django.conf.settings, "MY_SETTING", "my_setting_value", raising=False)
    assert _get_setting_or_env_or_value("settings.MY_SETTING") == "my_setting_value"


def test_get_setting_or_env_or_value_direct():
    """Test returning direct value."""
    assert _get_setting_or_env_or_value("direct_value") == "direct_value"


def test_get_setting_or_env_or_value_env_missing():
    """Test error when environment variable is missing."""
    with pytest.raises(ValueError):
        _get_setting_or_env_or_value("environ.NOT_SET_ENV")


def test_get_setting_or_env_or_value_setting_missing():
    """Test error when Django setting is missing."""
    with pytest.raises(ValueError):
        _get_setting_or_env_or_value("settings.NOT_SET_SETTING")


def test_llmprovider_model_valid():
    """Test LLMProvider with valid environment and setting values."""
    provider = LLMProvider(
        hrid="openai",
        base_url="environ.TEST_ENV_VAR",
        api_key="settings.TEST_SETTING",
    )
    assert provider.base_url == "env_value"
    assert provider.api_key == "setting_value"


def test_llmodel_provider_name_and_provider_exclusive():
    """Test that provider_name and provider are mutually exclusive."""
    provider = LLMProvider(hrid="openai", base_url="direct", api_key="direct")
    with pytest.raises(ValueError):
        LLModel(
            hrid="gpt-4",
            model_name="gpt-4",
            human_readable_name="GPT-4",
            provider_name="openai",
            provider=provider,
            is_active=True,
            system_prompt="direct",
            tools=[],
        )


def test_llmodel_model_name_format():
    """Test that model_name with provider prefix is accepted without provider_name."""
    model = LLModel(
        hrid="gpt-4",
        model_name="openai:gpt-4",
        human_readable_name="GPT-4",
        is_active=True,
        system_prompt="direct",
        tools=[],
    )
    assert model.model_name == "openai:gpt-4"


def test_llmodel_missing_provider_and_wrong_model_name():
    """
    Test error when both provider_name and provider are missing and model_name is not prefixed.
    """
    with pytest.raises(ValueError):
        LLModel(
            hrid="gpt-4",
            model_name="gpt4",
            human_readable_name="GPT-4",
            is_active=True,
            system_prompt="direct",
            tools=[],
        )


def test_llmconfiguration_fill_providers_success():
    """Test successful filling of providers in LLMConfiguration."""
    provider = LLMProvider(hrid="openai", base_url="direct", api_key="direct")
    model = LLModel(
        hrid="gpt-4",
        model_name="gpt-4",
        human_readable_name="GPT-4",
        provider_name="openai",
        is_active=True,
        system_prompt="direct",
        tools=[],
    )
    config = LLMConfiguration(models=[model], providers=[provider])
    assert config.models[0].provider == provider


def test_llmconfiguration_fill_providers_missing():
    """Test error when provider_name does not match any provider in LLMConfiguration."""
    model = LLModel(
        hrid="gpt-4",
        model_name="gpt-4",
        human_readable_name="GPT-4",
        provider_name="notfound",
        is_active=True,
        system_prompt="direct",
        tools=[],
    )
    with pytest.raises(ValueError):
        LLMConfiguration(models=[model], providers=[])


def test_load_llm_configuration(tmp_path, monkeypatch):
    """Test loading LLM configuration from JSON file with env and settings."""
    monkeypatch.setenv("TEST_ENV_VAR", "env_value")
    monkeypatch.setattr(django.conf.settings, "TEST_SETTING", "setting_value", raising=False)
    config_dict = {
        "models": [
            {
                "hrid": "gpt-4",
                "model_name": "gpt-4",
                "human_readable_name": "GPT-4",
                "provider_name": "openai",
                "is_active": True,
                "system_prompt": "direct",
                "tools": [],
            }
        ],
        "providers": [
            {
                "hrid": "openai",
                "base_url": "environ.TEST_ENV_VAR",
                "api_key": "settings.TEST_SETTING",
            }
        ],
    }
    config_path = tmp_path / "llm_config.json"
    config_path.write_text(json.dumps(config_dict), encoding="utf-8")
    model_map = load_llm_configuration(str(config_path))
    assert "gpt-4" in model_map
    assert model_map["gpt-4"].provider.base_url == "env_value"
    assert model_map["gpt-4"].provider.api_key == "setting_value"


def test_llmodel_is_custom_property():
    """Test the is_custom property of LLModel."""
    provider = LLMProvider(hrid="custom", base_url="direct", api_key="direct")
    custom_model = LLModel(
        hrid="custom-model",
        model_name="custom-model",
        human_readable_name="Custom Model",
        provider=provider,
        is_active=True,
        system_prompt="direct",
        tools=[],
    )
    non_custom_model = LLModel(
        hrid="prefixed-model",
        model_name="openai:prefixed-model",
        human_readable_name="Prefixed Model",
        is_active=True,
        system_prompt="direct",
        tools=[],
    )
    assert custom_model.is_custom is True
    assert non_custom_model.is_custom is False


def test_llm_profile_concatenate_instruction_messages_from_json():
    """LLMProfile parses concatenate_instruction_messages from JSON config."""
    config_json = json.dumps(
        {
            "models": [
                {
                    "hrid": "vllm-model",
                    "model_name": "my-model",
                    "human_readable_name": "My Model",
                    "concatenate_instruction_messages": True,
                    "provider": {
                        "hrid": "my-provider",
                        "base_url": "https://vllm.example.com/v1",
                        "api_key": "testkey",
                    },
                    "is_active": True,
                    "system_prompt": "You are helpful.",
                    "tools": [],
                }
            ],
            "providers": [
                {
                    "hrid": "my-provider",
                    "base_url": "https://vllm.example.com/v1",
                    "api_key": "testkey",
                }
            ],
        }
    )
    config = LLMConfiguration.model_validate_json(config_json)
    assert config.models[0].concatenate_instruction_messages is True


def test_llmodel_max_token_context_parses_string():
    """Test max_token_context accepts string values from JSON config."""
    model = LLModel(
        hrid="gpt-4",
        model_name="openai:gpt-4",
        human_readable_name="GPT-4",
        is_active=True,
        system_prompt="direct",
        tools=[],
        max_token_context="128000",
    )
    assert model.max_token_context == 128000


def test_llmodel_max_token_context_rejects_invalid_value():
    """Test max_token_context rejects non integer-like values."""
    with pytest.raises(ValueError):
        LLModel(
            hrid="gpt-4",
            model_name="openai:gpt-4",
            human_readable_name="GPT-4",
            is_active=True,
            system_prompt="direct",
            tools=[],
            max_token_context="abc",
        )


# ---------------------------------------------------------------------------
# Footprint / routing fields: role, prices, parameter counts, reasoning control
# ---------------------------------------------------------------------------


def _minimal_model(**overrides) -> LLModel:
    """Build a minimal valid LLModel with optional overrides."""
    values = {
        "hrid": "m",
        "model_name": "openai:m",
        "human_readable_name": "M",
        "is_active": True,
        "system_prompt": "direct",
        "tools": [],
    }
    values.update(overrides)
    return LLModel(**values)


def test_llmodel_footprint_fields_defaults():
    """New fields are optional and keep old configurations valid."""
    model = _minimal_model()
    assert model.role == "chat"
    assert model.input_price_eur_per_mtok is None
    assert model.output_price_eur_per_mtok is None
    assert model.total_params_b is None
    assert model.active_params_b is None
    assert model.reasoning_control == "none"


def test_llmodel_footprint_fields_set():
    """Role, prices, parameter counts and reasoning control are parsed."""
    model = _minimal_model(
        role="utility",
        input_price_eur_per_mtok=0.5,
        output_price_eur_per_mtok=1.5,
        total_params_b=117,
        active_params_b=5.1,
        reasoning_control="levels",
    )
    assert model.role == "utility"
    assert model.input_price_eur_per_mtok == 0.5
    assert model.output_price_eur_per_mtok == 1.5
    assert model.total_params_b == 117
    assert model.active_params_b == pytest.approx(5.1)
    assert model.reasoning_control == "levels"


def test_llmodel_dense_model_active_defaults_to_total():
    """Only total_params_b given: active_params_b is filled with the same value."""
    model = _minimal_model(total_params_b=24)
    assert model.active_params_b == 24


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"role": "admin"}, id="bad_role"),
        pytest.param({"reasoning_control": "maybe"}, id="bad_reasoning_control"),
        pytest.param({"active_params_b": 5}, id="active_without_total"),
        pytest.param({"total_params_b": 5, "active_params_b": 10}, id="active_above_total"),
    ],
)
def test_llmodel_footprint_fields_invalid(overrides):
    """Invalid role, reasoning control or inconsistent parameter counts are rejected."""
    with pytest.raises(ValueError):
        _minimal_model(**overrides)


@pytest.mark.parametrize(
    "config_file",
    ["default.json", "default.e2e.json"],
)
def test_default_configuration_files_validate(config_file, monkeypatch):
    """The shipped configuration files load with the new fields."""
    from pathlib import Path  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    for name in (
        "AI_MODEL",
        "AI_BASE_URL",
        "AI_API_KEY",
        "AI_AGENT_INSTRUCTIONS",
        "SUMMARIZATION_SYSTEM_PROMPT",
    ):
        monkeypatch.setattr(django.conf.settings, name, "value", raising=False)
    monkeypatch.setattr(django.conf.settings, "AI_AGENT_TOOLS", [], raising=False)

    path = (
        Path(__file__).resolve().parents[2]
        / "conversations"
        / "configuration"
        / "llm"
        / config_file
    )
    models = load_llm_configuration(str(path))

    assert models["default-model"].role == "chat"
    if config_file == "default.json":
        assert models["default-summarization-model"].role == "utility"
        gpt_oss = models["gpt-oss-120b"]
        assert gpt_oss.model_name == "gpt-oss-120b"
        assert gpt_oss.supports_image is False
        assert (gpt_oss.total_params_b, gpt_oss.active_params_b) == (117, pytest.approx(5.1))
        assert gpt_oss.reasoning_control == "levels"
        assert models["deepseek-v4-flash"].reasoning_control == "toggle"
        assert models["ministral-3-8b"].active_params_b == 8
        assert models["qwen3-coder-30b"].active_params_b == 3
