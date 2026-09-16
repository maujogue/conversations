"""Tests for chat.footprint: EcoLogits estimate, CO2 source resolution, reasoning tokens."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from ecologits.utils.range_value import RangeValue

from chat import footprint
from chat.footprint import (
    estimate_co2_kg,
    reasoning_tokens_from_details,
    resolve_co2,
)
from chat.llm_configuration import LLModel


def _model(**overrides) -> LLModel:
    """Build a minimal LLModel, overriding footprint-related fields."""
    values = {
        "hrid": "m",
        "model_name": "test:model",
        "human_readable_name": "M",
        "is_active": True,
        "system_prompt": "hi",
        "tools": [],
    }
    values.update(overrides)
    return LLModel(**values)


def _impacts(gwp_value):
    """Fake compute_llm_impacts return value exposing `.gwp.value`."""
    return SimpleNamespace(gwp=SimpleNamespace(value=gwp_value))


# ---------------------------------------------------------------------------
# estimate_co2_kg
# ---------------------------------------------------------------------------


def test_estimate_returns_none_without_params():
    """No parameter counts: the estimate is impossible and returns None."""
    with patch.object(footprint, "compute_llm_impacts") as mocked:
        assert estimate_co2_kg(_model(), 100, 1.0) is None
    mocked.assert_not_called()


def test_estimate_calls_compute_llm_impacts_with_model_params():
    """compute_llm_impacts receives the configured active/total counts, tokens, latency."""
    model = _model(total_params_b=117, active_params_b=5.1)
    with patch.object(footprint, "compute_llm_impacts", return_value=_impacts(0.002)) as mocked:
        result = estimate_co2_kg(model, 350, 2.5)

    assert result == pytest.approx(0.002)
    mocked.assert_called_once()
    kwargs = mocked.call_args.kwargs
    assert kwargs["model_active_parameter_count"] == pytest.approx(5.1)
    assert kwargs["model_total_parameter_count"] == 117
    assert kwargs["output_token_count"] == 350
    assert kwargs["request_latency"] == pytest.approx(2.5)
    assert kwargs["datacenter_pue"] == footprint.DATACENTER_PUE
    assert kwargs["datacenter_wue"] == footprint.DATACENTER_WUE
    # French electricity mix factors are passed through.
    assert kwargs["if_electricity_mix_gwp"] == pytest.approx(0.04144, rel=1e-3)


def test_estimate_dense_model_uses_total_as_active():
    """When only total_params_b is configured, active defaults to total (dense model)."""
    model = _model(total_params_b=24)
    assert model.active_params_b == 24
    with patch.object(footprint, "compute_llm_impacts", return_value=_impacts(1e-4)) as mocked:
        estimate_co2_kg(model, 10, None)
    kwargs = mocked.call_args.kwargs
    assert kwargs["model_active_parameter_count"] == 24
    assert kwargs["model_total_parameter_count"] == 24
    assert kwargs["request_latency"] is None


def test_estimate_collapses_range_value_to_mean():
    """A RangeValue GWP is collapsed to its mean."""
    model = _model(total_params_b=8)
    with patch.object(
        footprint, "compute_llm_impacts", return_value=_impacts(RangeValue(min=1.0, max=3.0))
    ):
        assert estimate_co2_kg(model, 10, 1.0) == pytest.approx(2.0)


def test_estimate_zero_tokens_is_zero():
    """No output tokens means no estimate call and zero CO2."""
    with patch.object(footprint, "compute_llm_impacts") as mocked:
        assert estimate_co2_kg(_model(total_params_b=8), 0, 1.0) == 0.0
    mocked.assert_not_called()


def test_estimate_returns_none_when_ecologits_raises():
    """An EcoLogits failure is logged and yields None rather than breaking the caller."""
    with patch.object(footprint, "compute_llm_impacts", side_effect=RuntimeError("boom")):
        assert estimate_co2_kg(_model(total_params_b=8), 10, 1.0) is None


def test_estimate_real_ecologits_smoke():
    """Unmocked EcoLogits returns a small positive figure and grows with tokens."""
    model = _model(total_params_b=117, active_params_b=5.1)
    small = estimate_co2_kg(model, 100, 2.0)
    large = estimate_co2_kg(model, 2000, 20.0)
    assert small is not None and large is not None
    assert 0 < small < large < 1  # kgCO2eq per answer is far below a kilogram


# ---------------------------------------------------------------------------
# resolve_co2
# ---------------------------------------------------------------------------


def test_resolve_provider_when_present_and_no_reasoning():
    """Non-reasoning model with a provider figure: keep the provider figure."""
    model = _model(total_params_b=24)
    with patch.object(footprint, "compute_llm_impacts") as mocked:
        value, source = resolve_co2(model, 1.5e-4, 200, 0, 1.0)
    assert (value, source) == (1.5e-4, "provider")
    mocked.assert_not_called()


def test_resolve_estimated_when_provider_absent():
    """Non-reasoning model without a provider figure: EcoLogits estimate on completion tokens."""
    model = _model(total_params_b=24)
    with patch.object(footprint, "compute_llm_impacts", return_value=_impacts(3e-4)) as mocked:
        value, source = resolve_co2(model, None, 200, 0, 1.0)
    assert (value, source) == (pytest.approx(3e-4), "estimated")
    assert mocked.call_args.kwargs["output_token_count"] == 200


def test_resolve_estimated_reasoning_when_reasoning_tokens():
    """Reasoning tokens present: estimate on completion + reasoning, even with a provider figure."""
    model = _model(total_params_b=117, active_params_b=5.1)
    with patch.object(footprint, "compute_llm_impacts", return_value=_impacts(9e-4)) as mocked:
        value, source = resolve_co2(model, 1e-6, 50, 800, 4.0)
    assert (value, source) == (pytest.approx(9e-4), "estimated_reasoning")
    assert mocked.call_args.kwargs["output_token_count"] == 850


def test_resolve_estimated_reasoning_for_reasoning_model_without_counted_tokens():
    """A model with a reasoning control is estimated even when no reasoning tokens were counted."""
    model = _model(total_params_b=284, active_params_b=13, reasoning_control="toggle")
    with patch.object(footprint, "compute_llm_impacts", return_value=_impacts(5e-4)):
        value, source = resolve_co2(model, 1e-6, 100, 0, 2.0)
    assert (value, source) == (pytest.approx(5e-4), "estimated_reasoning")


def test_resolve_reasoning_without_params_falls_back_to_provider():
    """Reasoning model with no parameter counts: keep the provider figure if there is one."""
    model = _model(reasoning_control="levels")
    assert resolve_co2(model, 2e-6, 10, 500, 1.0) == (2e-6, "provider")
    assert resolve_co2(model, None, 10, 500, 1.0) == (None, "estimated_reasoning")


def test_resolve_nothing_available():
    """No provider figure and no parameters: None, tagged estimated."""
    assert resolve_co2(_model(), None, 10, 0, 1.0) == (None, "estimated")


# ---------------------------------------------------------------------------
# reasoning_tokens_from_details
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "details,expected",
    [
        pytest.param(None, 0, id="none"),
        pytest.param({}, 0, id="empty"),
        pytest.param({"reasoning_tokens": 42}, 42, id="provider_tokens"),
        pytest.param({"reasoning_tokens": 42, "reasoning_chars": 8000}, 42, id="tokens_win"),
        pytest.param({"reasoning_chars": 800}, 200, id="chars_divided_by_4"),
        pytest.param({"reasoning_chars": 801}, 201, id="chars_rounded_up"),
        pytest.param({"reasoning_chars": 0}, 0, id="zero_chars"),
        pytest.param(
            {"reasoning_tokens": 0, "reasoning_chars": 40}, 10, id="zero_tokens_uses_chars"
        ),
    ],
)
def test_reasoning_tokens_from_details(details, expected):
    """reasoning_tokens wins when present, else ceil(reasoning_chars / 4)."""
    assert reasoning_tokens_from_details(details) == expected
